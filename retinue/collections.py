"""The owner's collections (docs/collections.md): one SQLite file beside the platform's own, `collections.sqlite` on
the platform's volume, and the files he uploads, kept by their SHA-256 in `attachments/` beside it.

One thread, one connection. Every read and every write goes through a single dedicated thread that opened the file
itself; the loop only awaits it. The platform's other databases live on the loop's thread, and SQLite's busy
handler sleeps where it is called: a writer that waited on the loop would hold up the hooks, the gate and the face
with it. Here a lock that does not come within 2 s (the night's backup copy, an import) is `Refused` at once, for
the caller to say «try again», never waited out for 30 s.

The file uses a rollback journal, like every database the backup copies (deploy/backup/retinue-backup.py). It holds
`files`, and the records: `schemas` (a collection's form), `records` (each one's current state, a JSON document),
`changes` (every change of one, append-only: the history and the provenance) and `batches` (changes that go
together). Every write of a record is one `apply`, checked whole against the form before anything is kept.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

FILE_MAX = 40 * 2**20       # an uploaded file, whatever it is: a bank's statement, an export, a photo of a receipt
BUSY_S = 2.0                # how long the thread waits for a lock before it refuses
FILES_LISTED = 1000         # the newest files `files()` returns
NAME_MAX = 200              # characters of a file's name kept
BLOBS = "attachments"       # the files' folder, beside the database
FREE_MIN = 2**30            # what an upload leaves free on the volume, which holds the platform's databases too
BUSY = "The store is busy (an import or the night's backup copy) — try again in a moment."
FULL = "The server's disk is nearly full — nothing was kept. Free some space, then upload again."
SHA256 = re.compile(r"[0-9a-f]{64}")
MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")
SCHEMA = """
    CREATE TABLE IF NOT EXISTS files (
        sha256 TEXT PRIMARY KEY,            -- the bytes' SHA-256, hex: the blob's name in attachments/
        name TEXT NOT NULL,                 -- as the owner's device named it, the first time it came
        size INTEGER NOT NULL,
        mime TEXT NOT NULL,                 -- as the device said; never sniffed, never parsed
        created REAL NOT NULL
    ) STRICT;
    CREATE INDEX IF NOT EXISTS files_created ON files (created);
    CREATE TABLE IF NOT EXISTS schemas (
        collection TEXT PRIMARY KEY,
        space TEXT NOT NULL,                -- the Space the collection belongs to
        form TEXT NOT NULL CHECK (json_valid(form)),
        rights TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rights)),
        views TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(views)),
        rev INTEGER NOT NULL,               -- the form's revision: 1 at its first define, one more at each after
        updated REAL NOT NULL
    ) STRICT;
    CREATE TABLE IF NOT EXISTS records (
        collection TEXT NOT NULL,
        id TEXT NOT NULL,                   -- a ULID the store gave: never changed, never used again
        data TEXT NOT NULL CHECK (json_valid(data)),  -- the fields; one that is unknown (null) is left out
        version INTEGER NOT NULL,           -- 1 at its add, one more at each change
        created REAL NOT NULL,
        updated REAL NOT NULL,
        deleted REAL,                       -- when it was deleted; deleting is soft, a restore clears it
        PRIMARY KEY (collection, id)
    ) STRICT;
    CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY,                -- a ULID
        kind TEXT NOT NULL CHECK (kind IN ('import', 'define', 'revert', 'assistant')),
        state TEXT NOT NULL CHECK (state IN ('preview', 'applying', 'applied', 'reverted')),
        actor TEXT NOT NULL,
        created REAL NOT NULL,
        updated REAL NOT NULL
    ) STRICT;
    CREATE TABLE IF NOT EXISTS changes (
        seq INTEGER PRIMARY KEY,            -- the order within this file only: renumbered by a restore, never shown
        change_id TEXT NOT NULL UNIQUE,     -- a ULID, growing: what everything outside the file names a change by
        ts REAL NOT NULL,
        as_of TEXT,                         -- the date the world became so, when the writer knows it
        batch TEXT,
        collection TEXT NOT NULL,
        id TEXT,                            -- the record; none for a define
        op TEXT NOT NULL CHECK (op IN ('add', 'set', 'delete', 'restore', 'verify', 'define', 'revert')),
        before TEXT CHECK (before IS NULL OR json_valid(before)),  -- what it changed, as it was and as it became
        after TEXT CHECK (after IS NULL OR json_valid(after)),
        actor TEXT NOT NULL,
        model TEXT,
        via TEXT NOT NULL,
        src TEXT,
        "foreign" TEXT,                     -- what foreign text the writer had read, comma-separated
        idem TEXT,
        reverts TEXT                        -- the change_id a revert takes back
    ) STRICT;
    CREATE INDEX IF NOT EXISTS changes_record ON changes (collection, id, seq);
    CREATE INDEX IF NOT EXISTS changes_collection ON changes (collection, seq);
    CREATE TRIGGER IF NOT EXISTS changes_append_only_u BEFORE UPDATE ON changes
        BEGIN SELECT RAISE(ABORT, 'changes are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS changes_append_only_d BEFORE DELETE ON changes
        BEGIN SELECT RAISE(ABORT, 'changes are append-only'); END;
"""
FILE_COLUMNS = "sha256, name, size, mime, created"

NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")      # a collection's name, and a field's
ENUM_VALUE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # as spaces.py names a Space
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ACTOR = re.compile(r"owner|assistant|session:[A-Za-z0-9_-]{1,64}|mac:[a-z0-9][a-z0-9_-]{0,39}/[A-Za-z0-9_-]{1,64}"
                   r"|code:[a-z][a-z0-9_-]{0,39}|import:[0-9A-HJKMNP-TV-Z]{26}|define:[1-9][0-9]{0,8}")
VIA = ("screen", "session", "mac", "assistant", "mail", "import", "define")
SRC = re.compile(r"(?:mail|tg|owner_words|cal|file|record|web):\S{1,1000}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")      # a mark of foreign text the writer read (core.FOREIGN's keys)
TYPES = {  # each type, and the keys its field takes beside `type` and `required`
    "text": (), "int": (), "enum": ("values",), "date": (), "bool": (), "ref": ("to",),
}
OP_KEYS = {  # each operation of `apply`, and the keys it takes
    "add": {"op", "fields", "as_of", "src"},
    "set": {"op", "id", "version", "fields", "as_of", "src"},
    "delete": {"op", "id", "version", "as_of", "src"},
    "restore": {"op", "id", "version", "as_of", "src"},
    "revert": {"op", "change_id", "version", "src"},
}
DELETED = "_deleted"        # in a change's before/after, the record's deleted state: no field's name starts with _
TEXT_MAX = 2000             # characters of a text field
RECORD_MAX = 16 * 2**10     # bytes of a record's fields, as JSON
FORM_MAX = 64 * 2**10       # bytes of a form, as JSON
OPS_MAX = 200               # operations in one apply
ENUM_MAX = 200              # values of one enum
INT_MAX = 2**53 - 1         # what a JSON number keeps exactly
HISTORY = 50                # the newest changes `history` returns
RECORD_COLUMNS = "id, data, version, created, updated, deleted"
CHANGE_COLUMNS = ('change_id, ts, as_of, batch, collection, id, op, before, after, actor, model, via, src, "foreign", '
                  "reverts")


class Refused(Exception):
    """The file was locked past `BUSY_S`, or the store is closing: nothing was written; the same call may simply be
    made again."""


class Full(Exception):
    """The file would leave less than `FREE_MIN` on the volume: nothing was written."""


class Rejected(Exception):
    """A write the store will not make as asked — a field the form does not have, a value of the wrong type, a version
    that is not the record's — with the reason in words a writer (a model included) can correct its call by. Nothing
    of the call was written. `record` is the record as it is now and `change` its last change, when they help: a
    version that did not match comes with both."""

    def __init__(self, reason: str, record: dict | None = None, change: dict | None = None) -> None:
        super().__init__(reason)
        self.reason, self.record, self.change = reason, record, change


@dataclass(frozen=True)
class Writer:
    """Who writes, as every change keeps it: `actor` of a closed kind (`owner`, `assistant`, `session:<id>`,
    `mac:<device>/<session>`, `code:<rule>`, `import:<batch>`, `define:<rev>`), `via` the path it came by, `model`
    the model behind it if any, `foreign` the marks of foreign text it had read."""

    actor: str
    via: str
    model: str | None = None
    foreign: tuple[str, ...] = ()


def clean_name(name: str) -> str:
    """A file's name as kept and shown: its last path part, without control, bidi, private-use or lone surrogate
    characters (a header's raw bytes that were not UTF-8), at most NAME_MAX characters; `file` when nothing is left."""
    name = re.split(r"[/\\]", name)[-1]
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")).strip()
    return name[:NAME_MAX] or "file"


def clean_mime(mime: str) -> str:
    """The media type the device gave, when it is one; else `application/octet-stream`."""
    mime = mime.split(";")[0].strip().lower()
    return mime if MIME.fullmatch(mime) else "application/octet-stream"


def _row(row: tuple) -> dict:
    return dict(zip(("sha256", "name", "size", "mime", "created"), row))


def clean_text(text: str) -> str:
    """A text field's value as kept: without control (but newlines and tabs), bidi, private-use or lone surrogate
    characters, so a value cannot disguise itself on a screen or in an agent's prompt."""
    return "".join(c for c in text
                   if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))


def _plain(text: object, most: int) -> bool:
    return (isinstance(text, str) and 0 < len(text) <= most
            and not any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for c in text))


def _date(value: object) -> bool:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Ulids:
    """ULIDs (a 48-bit millisecond time and 80 random bits, in Crockford's base 32) that only grow: one in the same
    millisecond as the last, or under a clock that stepped back, is the last plus one. Seeded with the file's newest
    id at its opening, so a restart does not go back either — the Router's cursor and the evening list name changes
    by these ids, and an id that went back would be skipped."""

    def __init__(self, last: str | None = None) -> None:
        self.last = sum(CROCKFORD.index(c) << 5 * (25 - n) for n, c in enumerate(last)) if last else 0

    def next(self, now: float) -> str:
        value = (int(now * 1000) << 80) | secrets.randbits(80)
        if value <= self.last:
            value = self.last + 1
        self.last = value
        return "".join(CROCKFORD[(value >> 5 * n) & 31] for n in range(25, -1, -1))


def parse_form(form: Any) -> dict:
    """A collection's form, checked strictly: a closed set of keys and types, any other stops it. Returns it as
    kept. Raises Rejected with the reason."""
    if not isinstance(form, dict):
        raise Rejected("a form is a JSON object: {\"fields\": {...}}")
    if len(_json(form).encode()) > FORM_MAX:
        raise Rejected(f"a form is at most {FORM_MAX // 1024} KB")
    if unknown := set(form) - {"fields"}:
        raise Rejected(f"a form takes only \"fields\", not {', '.join(sorted(map(repr, unknown)))}")
    fields = form.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise Rejected("a form's \"fields\" is an object of at least one field: {\"name\": {\"type\": \"text\"}}")
    kept = {}
    for name, spec in fields.items():
        if not NAME.fullmatch(name):
            raise Rejected(f"a field's name is lowercase letters, digits and _, starting with a letter: not {name!r}")
        if not isinstance(spec, dict) or spec.get("type") not in TYPES:
            raise Rejected(f"field {name!r}: \"type\" is one of {', '.join(TYPES)}")
        extra = TYPES[spec["type"]]
        if unknown := set(spec) - {"type", "required", *extra}:
            raise Rejected(f"field {name!r}: a {spec['type']} takes no {', '.join(sorted(map(repr, unknown)))}")
        if not isinstance(spec.get("required", False), bool):
            raise Rejected(f"field {name!r}: \"required\" is true or false")
        if spec["type"] == "enum":
            values = spec.get("values")
            if (not isinstance(values, list) or not 0 < len(values) <= ENUM_MAX or len(set(values)) != len(values)
                    or not all(isinstance(v, str) and ENUM_VALUE.fullmatch(v) for v in values)):
                raise Rejected(f"field {name!r}: \"values\" is a list of 1 to {ENUM_MAX} distinct values, each "
                               "lowercase letters, digits and _, starting with a letter")
        if spec["type"] == "ref" and not (isinstance(spec.get("to"), str) and NAME.fullmatch(spec["to"])):
            raise Rejected(f"field {name!r}: \"to\" names the collection its records are in")
        kept[name] = dict(spec)
    return {"fields": kept}


class Collections:
    """The store. Construct it anywhere; the thread opens the file on its first call (`open`, at the platform's
    start, so the file is there for the backup before anything is uploaded)."""

    def __init__(self, path: str, blobs: str | Path | None = None) -> None:
        self.path = path
        if blobs is not None:
            self.blobs = Path(blobs)
        elif path == ":memory:":
            self.blobs = Path(tempfile.mkdtemp())
        else:
            self.blobs = Path(path).parent / BLOBS
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="collections")
        self._db: sqlite3.Connection | None = None
        self.thread: int | None = None  # the writer's thread, once it has opened the file
        self._ulids = Ulids()
        self._closed = False

    # --- the thread -------------------------------------------------------------------------------------------------

    async def _run(self, work: Callable[..., T], *args) -> T:
        if self._closed:  # an upload still on its way while the platform stops: «try again», not a 500
            raise Refused(BUSY)
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._call, work, args)

    def _call(self, work: Callable[..., T], args: tuple) -> T:
        if self._db is None:
            self._connect()
        try:
            return work(*args)
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise Refused(BUSY) from exc
            raise

    def _connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: no implicit BEGIN, so `BEGIN IMMEDIATE` is ours (`_write`).
        db = sqlite3.connect(self.path, isolation_level=None, timeout=BUSY_S)
        db.executescript(SCHEMA)
        last = db.execute("SELECT max(id) FROM (SELECT max(change_id) AS id FROM changes "
                          "UNION ALL SELECT max(id) FROM batches)").fetchone()[0]
        self._ulids = Ulids(last)
        self.blobs.mkdir(parents=True, exist_ok=True)
        for part in self.blobs.glob(".*.part"):  # a blob a crash cut while it was written: never named by a row
            part.unlink(missing_ok=True)
        self._db, self.thread = db, threading.get_ident()

    @contextmanager
    def _write(self):
        """One transaction, taken for writing at its start: committed whole, or rolled back — a commit that met a
        reader past BUSY_S included."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def _close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    async def open(self) -> None:
        """The file and the folder made, if they are not there yet."""
        await self._run(lambda: None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.get_running_loop().run_in_executor(self._pool, self._close)
        self._pool.shutdown(wait=True)

    # --- files ------------------------------------------------------------------------------------------------------

    async def put_file(self, data: bytes, name: str, mime: str, now: float) -> tuple[dict, bool]:
        """A file the owner uploaded: kept by its SHA-256, the same bytes once however often they come — `(row,
        True)` the first time, `(the row as it was, False)` after. The blob is written and synced before its row, so
        no row ever names a blob that is not there. Raises Refused, and Full when the volume would be nearly full."""
        if not 0 < len(data) <= FILE_MAX:
            raise ValueError(f"a file is 1 byte to {FILE_MAX // 2**20} MB")
        return await self._run(self._put_file, data, clean_name(name), clean_mime(mime), now)

    def _put_file(self, data: bytes, name: str, mime: str, now: float) -> tuple[dict, bool]:
        sha = hashlib.sha256(data).hexdigest()
        blob = self.blobs / sha
        kept = blob.is_file() and blob.stat().st_size == len(data)
        if (found := self._file(sha)) is not None:
            if not kept:  # its blob lost (a partial restore, a hand's delete): the same bytes put it back
                self._put_blob(blob, data)
            return found, False
        if not kept:
            self._put_blob(blob, data)
        with self._write() as db:
            if (found := self._file(sha)) is not None:
                return found, False
            db.execute(f"INSERT INTO files ({FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?)", (sha, name, len(data), mime, now))
        return {"sha256": sha, "name": name, "size": len(data), "mime": mime, "created": now}, True

    def _put_blob(self, blob: Path, data: bytes) -> None:
        """The bytes at `blob`, whole or not at all, and durable before anything names them: written to a `.part`,
        synced, renamed, and the folder synced so the rename survives a power cut too."""
        if shutil.disk_usage(self.blobs).free - len(data) < FREE_MIN:
            raise Full(FULL)
        part = self.blobs / f".{blob.name}.{secrets.token_hex(4)}.part"
        try:
            with open(part, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(part, blob)
        except BaseException:
            part.unlink(missing_ok=True)  # a full disk stays full otherwise, and it holds the platform's databases
            raise
        folder = os.open(self.blobs, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    def _file(self, sha: str) -> dict | None:
        row = self._db.execute(f"SELECT {FILE_COLUMNS} FROM files WHERE sha256 = ?", (sha,)).fetchone()
        return _row(row) if row else None

    async def file(self, sha: str) -> tuple[dict, Path] | None:
        """One file's row and its blob's path, or None. Raises Refused."""
        if not SHA256.fullmatch(sha):
            return None
        found = await self._run(self._file, sha)
        return (found, self.blobs / sha) if found is not None else None

    async def files(self, limit: int = FILES_LISTED) -> list[dict]:
        """The files, the newest first. Raises Refused."""
        return await self._run(self._files, limit)

    def _files(self, limit: int) -> list[dict]:
        rows = self._db.execute(f"SELECT {FILE_COLUMNS} FROM files ORDER BY created DESC, rowid DESC LIMIT ?",
                                (limit,))
        return [_row(row) for row in rows]

    # --- records ----------------------------------------------------------------------------------------------------

    async def define(self, writer: Writer, collection: str, space: str, form: Any, now: float) -> dict:
        """A collection's form, new or in place of the one it had, as a `define` change: `{name, rev}`. Raises
        Rejected, Refused."""
        return await self._run(self._define, writer, collection, space, form, now)

    def _define(self, writer: Writer, collection: str, space: str, form: Any, now: float) -> dict:
        _check_writer(writer)
        if not NAME.fullmatch(collection):
            raise Rejected(f"a collection's name is lowercase letters, digits and _, starting with a letter: not "
                           f"{collection!r}")
        if not isinstance(space, str) or not SPACE.fullmatch(space):
            raise Rejected(f"not a Space's name: {space!r}")
        form = parse_form(form)
        with self._write() as db:
            for name, spec in form["fields"].items():
                if spec["type"] == "ref" and spec["to"] != collection and self._form(spec["to"]) is None:
                    raise Rejected(f"field {name!r}: there is no collection {spec['to']!r}")
            old = db.execute("SELECT form, rev FROM schemas WHERE collection = ?", (collection,)).fetchone()
            rev = old[1] + 1 if old else 1
            db.execute("INSERT INTO schemas (collection, space, form, rev, updated) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (collection) DO UPDATE SET space = excluded.space, form = excluded.form, "
                       "rev = excluded.rev, updated = excluded.updated", (collection, space, _json(form), rev, now))
            self._change(writer, now, collection, None, "define", json.loads(old[0]) if old else None, form)
            return {"name": collection, "rev": self._rev(collection)}

    async def start_batch(self, kind: str, writer: Writer, now: float) -> str:
        """A batch, `applying`, for changes that go together (an import, a form's application): its id, to pass to
        `apply`. Raises Rejected, Refused."""
        return await self._run(self._start_batch, kind, writer, now)

    def _start_batch(self, kind: str, writer: Writer, now: float) -> str:
        _check_writer(writer)
        batch = self._ulids.next(now)
        with self._write() as db:
            try:
                db.execute("INSERT INTO batches (id, kind, state, actor, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                           (batch, kind, "applying", writer.actor, now, now))
            except sqlite3.IntegrityError as exc:
                raise Rejected(f"a batch is an import, a define, a revert or an assistant's: not {kind!r}") from exc
        return batch

    async def finish_batch(self, batch: str, now: float) -> None:
        """The batch `applied`: no change joins it after. Raises Rejected, Refused."""
        await self._run(self._finish_batch, batch, now)

    def _finish_batch(self, batch: str, now: float) -> None:
        with self._write() as db:
            if not db.execute("UPDATE batches SET state = 'applied', updated = ? WHERE id = ? AND state = 'applying'",
                              (now, batch)).rowcount:
                raise Rejected(f"no batch {batch!r} is applying")

    async def apply(self, writer: Writer, collection: str, ops: Any, now: float, batch: str | None = None) -> dict:
        """The one way a record is written: up to OPS_MAX operations on one collection, checked against its form and
        kept whole in one transaction, or none of them (docs/collections.md#records). Each is `add`, `set`, `delete`,
        `restore` or `revert`. Returns `{name, rev, change_ids, records}`: the collection, its newest change_id
        (what a screen compares to know it is behind), and for each operation the change it made (None when it
        changed nothing: the record was already so) and the record after it. Raises Rejected, Refused."""
        return await self._run(self._apply, writer, collection, ops, now, batch)

    def _apply(self, writer: Writer, collection: str, ops: Any, now: float, batch: str | None) -> dict:
        _check_writer(writer)
        if not isinstance(ops, list) or not 0 < len(ops) <= OPS_MAX:
            raise Rejected(f"ops is a list of 1 to {OPS_MAX} operations")
        with self._write() as db:
            form = self._form(collection)
            if form is None:
                raise Rejected(f"there is no collection {collection!r}")
            if batch is not None and db.execute("SELECT 1 FROM batches WHERE id = ? AND state = 'applying'",
                                                (batch,)).fetchone() is None:
                raise Rejected(f"no batch {batch!r} is applying")
            change_ids, records = [], []
            for n, op in enumerate(ops):
                try:
                    record, change = self._op(writer, collection, form, op, now, batch)
                except Rejected as exc:
                    raise Rejected(f"ops[{n}]: {exc.reason}", exc.record, exc.change) from None
                change_ids.append(change)
                records.append(record)
            return {"name": collection, "rev": self._rev(collection), "change_ids": change_ids, "records": records}

    def _op(self, writer: Writer, collection: str, form: dict, op: Any, now: float,
            batch: str | None) -> tuple[dict, str | None]:
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in OP_KEYS:
            raise Rejected(f"an operation is an object whose \"op\" is one of {', '.join(OP_KEYS)}")
        if unknown := set(op) - OP_KEYS[kind]:
            raise Rejected(f"{kind} takes {', '.join(sorted(OP_KEYS[kind]))}; not {', '.join(sorted(unknown))}")
        as_of, src = op.get("as_of"), op.get("src")
        if as_of is not None and not _date(as_of):
            raise Rejected(f"as_of is the date the world became so, YYYY-MM-DD: not {as_of!r}")
        if src is not None and not (SRC.fullmatch(src) if _plain(src, 1100) else False):
            raise Rejected("src is mail:<account>/<msgid>, tg:<chat>/<msg>, owner_words:<event>, "
                           "cal:<account>/<event>, file:<sha256>#row=<n>, record:<id> or web:<url>")
        stamp = {"writer": writer, "now": now, "collection": collection, "as_of": as_of, "src": src, "batch": batch}
        if kind == "add":
            fields = self._fields(collection, form, op.get("fields"), {})
            rid = self._ulids.next(now)
            self._db.execute(f"INSERT INTO records (collection, {RECORD_COLUMNS}) VALUES (?, ?, ?, 1, ?, ?, NULL)",
                             (collection, rid, _json(fields), now, now))
            change = self._change(**stamp, rid=rid, op="add", before=None, after=fields)
            return self._record(collection, rid), change
        if kind == "revert":
            return self._revert(stamp, form, op)
        record = self._at(collection, op)
        if kind == "set":
            if record["deleted"] is not None:
                raise Rejected(f"{record['id']} is deleted: restore it first", record)
            fields = self._fields(collection, form, op.get("fields"), record["fields"])
            before = {k: record["fields"].get(k) for k in op["fields"] if record["fields"].get(k) != fields.get(k)
                      or type(record["fields"].get(k)) is not type(fields.get(k))}
            after = {k: fields.get(k) for k in before}
        else:
            gone = record["deleted"] is not None
            before = {DELETED: record["deleted"]} if gone != (kind == "delete") else {}
            after = {DELETED: now if kind == "delete" else None} if before else {}
        if not before:  # already so: nothing is written, the version stays
            return record, None
        return self._put(stamp, record, before, after, kind)

    def _revert(self, stamp: dict, form: dict, op: dict) -> tuple[dict, str | None]:
        """A change taken back, as a change of its own: what it changed set back to what it was — unless something
        has changed it since, which is Rejected with who did. Taking back an add deletes the record."""
        cid = op.get("change_id")
        row = self._db.execute(f"SELECT seq, {CHANGE_COLUMNS} FROM changes WHERE change_id = ?",
                               (cid,)).fetchone() if isinstance(cid, str) else None
        if row is None or row[5] != stamp["collection"]:
            raise Rejected(f"change not found: {cid!r}")
        seq, target = row[0], _change(row[1:])
        if target["op"] not in OP_KEYS:
            raise Rejected(f"a {target['op']} is not taken back by a revert")
        record = self._at(stamp["collection"], {**op, "id": target["id"]})
        later = [_change(r) for r in self._db.execute(
            f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection = ? AND id = ? AND seq > ? ORDER BY seq",
            (stamp["collection"], target["id"], seq))]
        if target["op"] == "add":
            touched, goal = None, {DELETED: stamp["now"]}
        else:
            touched, goal = set(target["after"]), target["before"]
        for change in later:
            if touched is None or touched & set(change["after"] or {}):
                what = ", ".join(sorted("deleted" if k == DELETED else k
                                        for k in change["after"] if touched is None or k in touched))
                raise Rejected(f"changed after it: {what} by {change['actor']} ({change['op']}, "
                               f"{change['change_id']}) — revert that first, or set the value", record, change)
        fields = {k: v for k, v in goal.items() if k != DELETED}
        if fields and record["deleted"] is not None and goal.get(DELETED, record["deleted"]) is not None:
            raise Rejected(f"{record['id']} is deleted: restore it first", record)
        data = {k: v for k, v in {**record["fields"], **fields}.items() if v is not None}
        self._required(form, data)
        before = {k: record["deleted"] if k == DELETED else record["fields"].get(k) for k in goal}
        if before == goal:
            return record, None
        return self._put({**stamp, "as_of": None}, record, before, goal, "revert", reverts=target["change_id"])

    def _put(self, stamp: dict, record: dict, before: dict, after: dict, op: str,
             reverts: str | None = None) -> tuple[dict, str]:
        """The record changed by `after` (its fields, and DELETED for its deleted state), its version one more, and
        the change kept."""
        data = {**record["fields"], **{k: v for k, v in after.items() if k != DELETED}}
        data = {k: v for k, v in data.items() if v is not None}
        if len(_json(data).encode()) > RECORD_MAX:
            raise Rejected(f"a record is at most {RECORD_MAX // 1024} KB of fields")
        deleted = after.get(DELETED, record["deleted"])
        self._db.execute("UPDATE records SET data = ?, version = version + 1, updated = ?, deleted = ? "
                         "WHERE collection = ? AND id = ?",
                         (_json(data), stamp["now"], deleted, stamp["collection"], record["id"]))
        change = self._change(**stamp, rid=record["id"], op=op, before=before, after=after, reverts=reverts)
        return self._record(stamp["collection"], record["id"]), change

    def _fields(self, collection: str, form: dict, given: Any, current: dict) -> dict:
        """`given` checked against the form and laid over `current`: the record's fields as they will be. A value of
        null makes a field unknown."""
        if not isinstance(given, dict) or not given:
            raise Rejected("fields is an object of at least one field: {\"name\": value}")
        specs, data = form["fields"], dict(current)
        for name, value in given.items():
            if name not in specs:
                raise Rejected(f"{collection} has no field {name!r}; its fields: {', '.join(specs)}")
            if value is None:
                data.pop(name, None)
            else:
                data[name] = self._value(name, specs[name], value)
        self._required(form, data)
        if len(_json(data).encode()) > RECORD_MAX:
            raise Rejected(f"a record is at most {RECORD_MAX // 1024} KB of fields")
        return data

    def _value(self, name: str, spec: dict, value: Any) -> Any:
        kind = spec["type"]
        if kind == "text":
            if not isinstance(value, str):
                raise Rejected(f"{name!r} is text, a string: not {value!r}")
            value = clean_text(value)
            if len(value) > TEXT_MAX:
                raise Rejected(f"{name!r} is text of at most {TEXT_MAX} characters: this is {len(value)}")
        elif kind == "int":
            if type(value) is not int or abs(value) > INT_MAX:
                raise Rejected(f"{name!r} is a whole number: not {value!r}")
        elif kind == "enum":
            if value not in spec["values"]:
                raise Rejected(f"{name!r} is one of {', '.join(spec['values'])}: not {value!r}")
        elif kind == "date":
            if not _date(value):
                raise Rejected(f"{name!r} is a date, YYYY-MM-DD: not {value!r}")
        elif kind == "bool":
            if type(value) is not bool:
                raise Rejected(f"{name!r} is true or false: not {value!r}")
        elif kind == "ref":
            found = self._db.execute("SELECT deleted FROM records WHERE collection = ? AND id = ?",
                                     (spec["to"], value)).fetchone() if isinstance(value, str) else None
            if found is None or found[0] is not None:
                raise Rejected(f"{name!r} is the id of a record in {spec['to']}: there is no such record as {value!r}")
        return value

    @staticmethod
    def _required(form: dict, data: dict) -> None:
        if missing := [name for name, spec in form["fields"].items() if spec.get("required") and name not in data]:
            raise Rejected(f"{', '.join(map(repr, missing))} {'is' if len(missing) == 1 else 'are'} required")

    def _at(self, collection: str, op: dict) -> dict:
        """The record `op` names, at the version it gives: else Rejected, with the record as it is and its last
        change when the version is not its own."""
        rid = op.get("id")
        record = self._record(collection, rid) if isinstance(rid, str) else None
        if record is None:
            raise Rejected(f"{collection} has no record {rid!r}")
        if op.get("version") != record["version"] or type(op.get("version")) is not int:
            last = self._history(collection, record["id"], 1)
            raise Rejected(f"{record['id']} is at version {record['version']}, not {op.get('version')!r}: read it "
                           "again, it may have changed since", record, last[0] if last else None)
        return record

    def _change(self, writer: Writer, now: float, collection: str, rid: str | None, op: str, before: Any, after: Any,
                as_of: str | None = None, src: str | None = None, batch: str | None = None,
                reverts: str | None = None) -> str:
        cid = self._ulids.next(now)
        self._db.execute(f"INSERT INTO changes ({CHANGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (cid, now, as_of, batch, collection, rid, op, _json(before), _json(after), writer.actor,
                          writer.model, writer.via, src, ",".join(sorted(writer.foreign)) or None, reverts))
        return cid

    def _form(self, collection: str) -> dict | None:
        row = self._db.execute("SELECT form FROM schemas WHERE collection = ?", (collection,)).fetchone()
        return json.loads(row[0]) if row else None

    def _rev(self, collection: str) -> str | None:
        row = self._db.execute("SELECT change_id FROM changes WHERE collection = ? ORDER BY seq DESC LIMIT 1",
                               (collection,)).fetchone()
        return row[0] if row else None

    def _record(self, collection: str, rid: str) -> dict | None:
        row = self._db.execute(f"SELECT {RECORD_COLUMNS} FROM records WHERE collection = ? AND id = ?",
                               (collection, rid)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "fields": json.loads(row[1]), "version": row[2], "created": row[3], "updated": row[4],
                "deleted": row[5]}

    def _history(self, collection: str, rid: str, limit: int) -> list[dict]:
        rows = self._db.execute(f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection = ? AND id = ? "
                                "ORDER BY seq DESC LIMIT ?", (collection, rid, limit))
        return [_change(row) for row in rows]

    async def get(self, collection: str, rid: str) -> dict | None:
        """One record, a deleted one included (its `deleted` says so), or None. Raises Refused."""
        return await self._run(self._record, collection, rid)

    async def history(self, collection: str, rid: str, limit: int = HISTORY) -> list[dict]:
        """A record's changes, the newest first. Raises Refused."""
        return await self._run(self._history, collection, rid, limit)


def _change(row: tuple) -> dict:
    change = dict(zip(("change_id", "ts", "as_of", "batch", "collection", "id", "op", "before", "after", "actor",
                       "model", "via", "src", "foreign", "reverts"), row))
    change["before"] = json.loads(change["before"]) if change["before"] is not None else None
    change["after"] = json.loads(change["after"]) if change["after"] is not None else None
    change["foreign"] = change["foreign"].split(",") if change["foreign"] else []
    return change


def _check_writer(writer: Writer) -> None:
    if not isinstance(writer, Writer) or not isinstance(writer.actor, str) or not ACTOR.fullmatch(writer.actor):
        raise Rejected("not a writer the store knows")
    if writer.via not in VIA:
        raise Rejected(f"via is one of {', '.join(VIA)}: not {writer.via!r}")
    if writer.model is not None and not _plain(writer.model, 100):
        raise Rejected(f"not a model's name: {writer.model!r}")
    if not all(isinstance(label, str) and LABEL.fullmatch(label) for label in writer.foreign):
        raise Rejected(f"not a mark of foreign text: {writer.foreign!r}")
