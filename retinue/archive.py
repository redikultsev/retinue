"""Raw archive: the verbatim record of everything said to the owner and by the owner. Append-only, searchable.

It lives in the router process, in its own SQLite file. Agents never get the file: they search it through the
router's bus (`POST /archive/search`), the same way they ask each other. A status is a new event that refers to
an older one (`ref`), never an edit of a row.

What the owner sent besides text is kept next to it: the `attachments` table says what the model got from each
file (the number `#N` it sees, a transcript or a document's text), and the files it got as content blocks (a JPEG,
a PDF) lie in the folder `attachments/` beside the database, one subfolder per owner message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("retinue.archive")

OWNER, ASSISTANT, SYSTEM = "owner", "assistant", "system"  # who wrote the text
KINDS = (OWNER, ASSISTANT, SYSTEM)

_WORD = re.compile(r"\w+")


@dataclass
class Event:
    id: str
    ts: float
    kind: str
    text: str
    conversation_id: str
    channel: str
    ref: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Attachment:
    id: int                 # the number the model sees: #12
    event_id: str           # the owner message it came with
    kind: str               # photo | document | voice | audio | video | video_note
    what: str               # «фото 1280×960», «голосовое 0:42»
    origin: str             # «своё» | «переслано от …»
    text: str               # a transcript, a document's text, a PDF's text layer
    files: list = field(default_factory=list)  # [[media type, path under the attachments folder], ...]

    def mark(self) -> str:
        return f"[вложение #{self.id}: {self.what} · {self.origin}]"


SUFFIXES = {"image/jpeg": ".jpg", "application/pdf": ".pdf"}


def event_id(kind: str, ts: float, text: str, channel: str = "", native_id: str | None = None) -> str:
    """An id that survives a rebuild of the archive: the messenger's own message id, or a hash of the content."""
    if native_id:
        return f"{channel}:{native_id}"
    return hashlib.sha256(f"{kind}\n{ts!r}\n{text}".encode()).hexdigest()[:20]


def fts_query(query: str) -> str:
    """Words of the query as FTS5 prefixes joined by OR. Long words lose two letters, so that Russian endings
    match: «Еревану» finds «Ереван» and «Ереване»."""
    words = [w for w in _WORD.findall(query.lower()) if len(w) > 1][:12]
    return " OR ".join(f'"{w[:-2] if len(w) > 5 else w}"*' for w in words)


class Archive:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Files the model got as content blocks; an archive in memory (tests) keeps them in a temporary folder.
        self.folder = Path(path).parent / "attachments" if path != ":memory:" else Path(tempfile.mkdtemp())
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,                 -- owner | assistant | system
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,              -- where it was said: telegram, matrix, system
                ref TEXT,                           -- id of the event this one answers
                text TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS events_ref ON events (ref);
            CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
                USING fts5(text, content='events', content_rowid='seq', tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events
                BEGIN INSERT INTO events_fts (rowid, text) VALUES (new.seq, new.text); END;
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,   -- the #N the model sees
                ts REAL NOT NULL,
                event_id TEXT NOT NULL,                 -- the owner message it came with
                kind TEXT NOT NULL,
                what TEXT NOT NULL,
                origin TEXT NOT NULL,
                text TEXT NOT NULL,
                files TEXT NOT NULL                     -- JSON: [[media type, path under attachments/], ...]
            );
            CREATE INDEX IF NOT EXISTS attachments_event ON attachments (event_id);
            CREATE TRIGGER IF NOT EXISTS attachments_append_only_u BEFORE UPDATE ON attachments
                BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS attachments_append_only_d BEFORE DELETE ON attachments
                BEGIN SELECT RAISE(ABORT, 'archive is append-only'); END;
            """
        )
        self.db.commit()

    _COLUMNS = "id, ts, kind, text, conversation_id, channel, ref, meta"

    @staticmethod
    def _event(row) -> Event:
        return Event(row[0], row[1], row[2], row[3], row[4], row[5], row[6], json.loads(row[7]))

    def append(self, kind: str, text: str, *, conversation_id: str, channel: str, ref: str | None = None,
               native_id: str | None = None, meta: dict | None = None, ts: float | None = None) -> tuple[Event, bool]:
        """Write one event. Returns (event, fresh); fresh is False when this id is already in the archive,
        e.g. the messenger delivered the same message twice."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        ts = time.time() if ts is None else ts
        event = Event(event_id(kind, ts, text, channel, native_id), ts, kind, text, conversation_id, channel, ref,
                      meta or {})
        try:
            self.db.execute(
                f"INSERT INTO events ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.id, event.ts, event.kind, event.text, event.conversation_id, event.channel, event.ref,
                 json.dumps(event.meta, ensure_ascii=False)),
            )
        except sqlite3.IntegrityError:
            self.db.rollback()
            return self.get(event.id), False
        self.db.commit()
        return event, True

    def get(self, event_id: str) -> Event | None:
        row = self.db.execute(f"SELECT {self._COLUMNS} FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def answered(self, event_id: str) -> bool:
        """Has anything been said in reply to this event? One reply to several owner messages names them all in
        `meta.covers`; `ref` points at the last of them."""
        return self.db.execute(
            "SELECT 1 FROM events WHERE kind != ?2 AND (ref = ?1 OR (meta LIKE '%\"covers\"%' AND EXISTS"
            " (SELECT 1 FROM json_each(events.meta, '$.covers') WHERE value = ?1))) LIMIT 1",
            (event_id, OWNER)).fetchone() is not None

    def reply_to(self, event_id: str) -> Event | None:
        """The assistant's answer to this owner message: its own reply, or one reply to several messages."""
        row = self.db.execute(
            f"SELECT {self._COLUMNS} FROM events WHERE kind = ?2 AND (ref = ?1 OR (meta LIKE '%\"covers\"%' AND"
            " EXISTS (SELECT 1 FROM json_each(events.meta, '$.covers') WHERE value = ?1))) ORDER BY seq LIMIT 1",
            (event_id, ASSISTANT)).fetchone()
        return self._event(row) if row else None

    def unanswered(self, since: float) -> list[Event]:
        """Owner messages since `since` that nobody answered: the router died between the record and the reply.
        A refused photo and a pressed button are not questions."""
        rows = self.db.execute(
            f"SELECT {self._COLUMNS} FROM events WHERE kind = ? AND ts >= ?"
            " AND json_extract(meta, '$.unsupported') IS NULL AND text NOT LIKE '[кнопка] %' ORDER BY seq",
            (OWNER, since)).fetchall()
        return [event for event in map(self._event, rows) if not self.answered(event.id)]

    def spoke(self, conversation_id: str) -> bool:
        """Has the assistant answered anything in this conversation yet?"""
        return self.db.execute("SELECT 1 FROM events WHERE conversation_id = ? AND kind = ? LIMIT 1",
                               (conversation_id, ASSISTANT)).fetchone() is not None

    def since(self, conversation_id: str, ts: float, limit: int) -> list[Event]:
        """The last `limit` events of a conversation from `ts` on, oldest first."""
        rows = self.db.execute(
            f"SELECT {self._COLUMNS} FROM events WHERE conversation_id = ? AND ts >= ? ORDER BY seq DESC LIMIT ?",
            (conversation_id, ts, limit)).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def count(self, kind: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?", (kind, since)).fetchone()[0]

    def recent(self, conversation_id: str, limit: int, before: str | None = None) -> list[Event]:
        """The last `limit` events of a conversation, oldest first. With `before`, that owner message and the
        owner messages after it are left out: they wait in the queue and are not history yet."""
        rows = self.db.execute(
            f"SELECT {self._COLUMNS} FROM events WHERE conversation_id = ?1 AND NOT (kind = 'owner' AND seq >= "
            "COALESCE((SELECT seq FROM events WHERE id = ?2), 9223372036854775807)) ORDER BY seq DESC LIMIT ?3",
            (conversation_id, before, limit),
        ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def search(self, query: str, limit: int = 8, exclude: str | None = None) -> list[tuple[Event, str]]:
        """Full-text search. Returns (event, snippet) pairs, best match first."""
        match = fts_query(query)
        if not match:
            return []
        rows = self.db.execute(
            "SELECT e.id, e.ts, e.kind, e.text, e.conversation_id, e.channel, e.ref, e.meta,"
            " snippet(events_fts, 0, '', '', '…', 48)"
            " FROM events_fts JOIN events e ON e.seq = events_fts.rowid"
            " WHERE events_fts MATCH ? AND e.id IS NOT ? ORDER BY bm25(events_fts), e.seq DESC LIMIT ?",
            (match, exclude, limit),
        ).fetchall()
        return [(self._event(row), row[8]) for row in rows]

    def coverage(self) -> tuple[int, float | None, float | None]:
        """(number of events, time of the first, time of the last): what an empty search result must name."""
        return tuple(self.db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone())

    _ATTACHMENT = "id, event_id, kind, what, origin, text, files"

    @staticmethod
    def _attachment(row) -> Attachment:
        return Attachment(row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]))

    def attach(self, event_id: str, kind: str, what: str, origin: str, text: str,
               files: list[tuple[str, bytes]], commit: bool = True) -> Attachment:
        """Keep one attachment of an owner message: its files first (named by their content, so a second write of
        the same file changes nothing), then the row that numbers it. `commit=False` leaves the row to the
        transaction of the event it belongs to (`append` commits): a failure in between rolls both back."""
        folder = self.folder / event_id.replace(":", "_").replace("/", "_")
        stored = []
        for media_type, data in files:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{hashlib.sha256(data).hexdigest()[:16]}{SUFFIXES.get(media_type, '.bin')}"
            path.write_bytes(data)
            stored.append([media_type, str(path.relative_to(self.folder))])
        cursor = self.db.execute(
            "INSERT INTO attachments (ts, event_id, kind, what, origin, text, files) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), event_id, kind, what, origin, text, json.dumps(stored)))
        if commit:
            self.db.commit()
        return Attachment(cursor.lastrowid, event_id, kind, what, origin, text, stored)

    def attachment(self, number: int) -> Attachment | None:
        row = self.db.execute(f"SELECT {self._ATTACHMENT} FROM attachments WHERE id = ?", (number,)).fetchone()
        return self._attachment(row) if row else None

    def attachments_of(self, event_id: str) -> list[Attachment]:
        rows = self.db.execute(f"SELECT {self._ATTACHMENT} FROM attachments WHERE event_id = ? ORDER BY id",
                               (event_id,)).fetchall()
        return [self._attachment(row) for row in rows]

    def read(self, attachment: Attachment) -> list[tuple[str, bytes]]:
        """The files of an attachment as (media type, bytes). A file gone from the disk is left out."""
        out = []
        for media_type, path in attachment.files:
            try:
                out.append((media_type, (self.folder / path).read_bytes()))
            except OSError:
                log.warning("attachment #%s: file %s is missing", attachment.id, path)
        return out
