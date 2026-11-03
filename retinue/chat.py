"""The app's chat with the assistant (docs/face.md#talk-to-retinue): the platform's side of the router's app channel
(`channels/app.py`). The router pushes the conversation's rows and what she is writing; the face reads the rows
(`GET /api/chat`) and follows them on its stream (`chat` events, `chat-draft` frames); what the owner writes or
presses there is journaled for the router as notes (`chat`, `chat-press`). The platform never calls the router.

- **A row** is named by the router's archive id: pushed again, it is the same row, merged — what a push leaves out
  (her files, a press's toast) stays. His own message from the app is kept at once under the id the archive will give
  it (`app:<input id>`), `pending` until the router has taken it; the same input id again is the same message, and no
  second note.
- **What she is writing** is never stored: the last draft in memory, for a face that opens meanwhile; her next row
  ends it.
- **Her files** are kept beside their row, FILE_MAX each, FILES_MB in all — the oldest go first — and served only as
  downloads, under the sandbox policy of a session's handed-over file.
- At most ROWS rows: the oldest go, with their files.

Trust: a row is the router's word, pushed with its token; what the app sends is his, behind the face's login, and
reaches the router only as a note of a kind fixed here — his text, or a button id the row it is pressed on carries.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from typing import TYPE_CHECKING
from collections.abc import Callable

from aiohttp import web

from . import migrate, outfiles
from .face import FILE_CSP, INPUT_ID, _attachment, _body, _error

if TYPE_CHECKING:
    from .platformd import Platform

SCHEMA = 1
ROWS = 1000                   # rows kept
SHOWN = 200                   # rows a read gives: the newest
PUSH_ROWS = 100               # rows in one push of the router's
PUSH_BYTES = 8 * 2**20        # one push of rows, at most
DRAFT_BYTES = 256 * 1024
DRAFT_S = 30.0                # a draft not refreshed this long is over: the router went away mid-turn
TEXT_CHARS = 100_000
OWNER_CHARS = 20_000          # his message, as the router takes it (`core.MAX_INPUT_CHARS`)
FILE_MAX = 20 * 2**20         # one file of hers (`channels/app.py`)
FILE_BYTES = FILE_MAX * 4 // 3 + 64 * 1024  # its push, base64 and the rest
FILES_MB = 200
FILES_PER_ROW = 10
BUTTONS_PER_ROW = 24
BY = ("owner", "her", "notice")
ROW_ID = re.compile(r"[\w.:@-]{1,200}")
BUTTON = re.compile(r"[A-Za-z0-9_-]{8,64}")  # the router's button id (`protocol.Store.add_button`)
FILE_ID = re.compile(r"[0-9a-f]{32}")
NO_FILES = "The app's chat takes text for now: send files to the bot in Telegram."
GONE = "This button is no longer on the card."

STEPS: tuple[migrate.Step, ...] = (
    """CREATE TABLE IF NOT EXISTS chat (
        id TEXT PRIMARY KEY,         -- the archive's id: `app:<input id>` for what he wrote in the app
        at REAL NOT NULL,            -- the archive's time: the order the face shows
        rev INTEGER NOT NULL,        -- bumped at each change: the face keeps the newest it has seen
        data TEXT NOT NULL           -- the row as the face gets it
    );
    CREATE INDEX IF NOT EXISTS chat_at ON chat (at);
    CREATE TABLE IF NOT EXISTS chat_files (
        id TEXT PRIMARY KEY,         -- random: what the face's download names
        row TEXT NOT NULL,           -- the row it came with; it goes with it
        name TEXT NOT NULL,
        mime TEXT NOT NULL,          -- what the router said it is: shown, never served as
        size INTEGER NOT NULL,
        body BLOB NOT NULL,
        at REAL NOT NULL
    )""",
)


class Refused(ValueError):
    """A row or a file off the contract: refused, the rest of the push goes on."""


def clean_row(row: object) -> dict:
    """A row as the router may push it: `id`, and only the fields of the contract, each of its shape."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not ROW_ID.fullmatch(row["id"]):
        raise Refused("id: the archive's id")
    out: dict = {"id": row["id"]}
    if "at" in row:
        if not isinstance(row["at"], (int, float)) or isinstance(row["at"], bool):
            raise Refused("at: a number")
        out["at"] = float(row["at"])
    if "by" in row:
        if row["by"] not in BY:
            raise Refused(f"by: one of {', '.join(BY)}")
        out["by"] = row["by"]
    for key, limit in (("text", TEXT_CHARS), ("via", 32), ("toast", 300)):
        if key in row:
            if not isinstance(row[key], str) or len(row[key]) > limit:
                raise Refused(f"{key}: a text of {limit} characters at most")
            out[key] = row[key]
    if "buttons" in row:
        buttons = row["buttons"]
        if not isinstance(buttons, list) or len(buttons) > BUTTONS_PER_ROW or not all(
                isinstance(b, list) and len(b) == 2 and isinstance(b[0], str) and 0 < len(b[0]) <= 64
                and isinstance(b[1], str) and BUTTON.fullmatch(b[1]) for b in buttons):
            raise Refused(f"buttons: up to {BUTTONS_PER_ROW} of [label, button id]")
        out["buttons"] = [[label, button] for label, button in buttons]
    if "files" in row:
        files = row["files"]
        if not isinstance(files, list) or len(files) > FILES_PER_ROW:
            raise Refused(f"files: up to {FILES_PER_ROW}")
        out["files"] = [_clean_file(f) for f in files]
    return out


def _clean_file(f: object) -> dict:
    if not isinstance(f, dict) or not (f.get("id") is None or isinstance(f["id"], str) and FILE_ID.fullmatch(f["id"])) \
            or not isinstance(f.get("name"), str) or not 0 < len(f["name"]) <= 255 \
            or type(f.get("size")) is not int or f["size"] < 0 \
            or not isinstance(f.get("mime"), str) or len(f["mime"]) > 100 \
            or not isinstance(f.get("why", ""), str) or len(f.get("why", "")) > 200:
        raise Refused("files: {id, name, size, mime, why?}")
    out = {"id": f["id"], "name": outfiles.shown(f["name"]), "size": f["size"], "mime": f["mime"]}
    return {**out, "why": f["why"]} if f.get("why") else out


class Chat:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        migrate.run(self.store.db, "chat", STEPS)
        self.draft: dict | None = None  # {text, at}: what she is writing, as last pushed
        self.on_draft: Callable[[dict], None] | None = None  # the face's streams (`face.Hub.chat_draft`)

    def routes(self) -> list:
        return [web.get("/api/chat", self.face_chat), web.post("/api/chat", self.face_say),
                web.post("/api/chat/press", self.face_press),
                web.get(r"/api/chat/files/{id:[0-9a-f]{32}}", self.face_file)]

    def owner_routes(self) -> list:
        return [web.post("/owner/chat", self.owner_chat), web.post("/owner/chat/draft", self.owner_draft),
                web.post("/owner/chat/file", self.owner_file)]

    # --- the rows -------------------------------------------------------------------------------------------------

    def rows(self) -> list[dict]:
        found = self.store.db.execute("SELECT rev, data FROM chat ORDER BY at DESC, rowid DESC LIMIT ?",
                                      (SHOWN,)).fetchall()
        return [{**json.loads(data), "rev": rev} for rev, data in reversed(found)]

    def _row(self, row_id: str) -> dict | None:
        found = self.store.db.execute("SELECT data FROM chat WHERE id = ?", (row_id,)).fetchone()
        return json.loads(found[0]) if found else None

    def _put(self, row: dict) -> bool:
        """One row merged into the one there, and its event; inside the caller's transaction. False when it changed
        nothing — a backfill of rows the face has is no news."""
        old = self._row(row["id"])
        merged = {**(old or {}), **row}
        if old == merged:
            return False
        if not all(key in merged for key in ("at", "by", "text")):
            raise Refused("a new row: at, by and text")
        rev = self.store.db.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM chat").fetchone()[0]
        self.store.db.execute("INSERT INTO chat (id, at, rev, data) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO UPDATE "
                              "SET at = excluded.at, rev = excluded.rev, data = excluded.data",
                              (merged["id"], merged["at"], rev, json.dumps(merged, ensure_ascii=False)))
        self.store._event("chat", "", merged["id"], {**merged, "rev": rev})
        if old is None and merged["by"] != "owner":  # her answer, or the system's word: the draft is over
            self.draft = None
        return True

    def _cap(self) -> None:
        self.store.db.execute("DELETE FROM chat WHERE id NOT IN (SELECT id FROM chat ORDER BY at DESC LIMIT ?)",
                              (ROWS,))
        self.store.db.execute("DELETE FROM chat_files WHERE row NOT IN (SELECT id FROM chat) AND at < ?",
                              (self.platform.clock() - 3600,))  # a file comes before its row: an hour's grace
        total = 0
        for file_id, size in self.store.db.execute("SELECT id, size FROM chat_files ORDER BY at DESC").fetchall():
            total += size
            if total > FILES_MB * 2**20:
                self.store.db.execute("DELETE FROM chat_files WHERE id = ?", (file_id,))

    # --- the router's side ----------------------------------------------------------------------------------------

    async def owner_chat(self, request: web.Request) -> web.Response:
        """Rows from the router `{rows}`, each merged by its id; a row off the contract is dropped and named."""
        if request.content_length is None or request.content_length > PUSH_BYTES:
            return web.json_response({"error": f"rows over {PUSH_BYTES} bytes"}, status=413)
        rows = (await _body(request)).get("rows")
        if not isinstance(rows, list) or len(rows) > PUSH_ROWS:
            return web.json_response({"error": f"rows: a list of {PUSH_ROWS} at most"}, status=400)
        changed, dropped = 0, []
        for row in rows:
            try:
                clean = clean_row(row)
                clean["pending"] = False  # the router has it
                changed += self._put(clean)
            except Refused as exc:
                dropped.append({"id": row.get("id") if isinstance(row, dict) else None, "why": str(exc)})
        self._cap()
        self.store.db.commit()
        return web.json_response({"ok": True, "rows": changed, "dropped": dropped})

    async def owner_draft(self, request: web.Request) -> web.Response:
        """What she is writing `{text?}`, the whole text so far; without `text` she still works, the text as it was."""
        if request.content_length is None or request.content_length > DRAFT_BYTES:
            return web.json_response({"error": f"a draft over {DRAFT_BYTES} bytes"}, status=413)
        body = await _body(request)
        text = body.get("text", (self.draft or {}).get("text", ""))
        if not isinstance(text, str):
            return web.json_response({"error": "text: a text"}, status=400)
        self.draft = {"text": text, "at": self.platform.clock()}
        if self.on_draft is not None:
            self.on_draft(self.draft)
        return web.json_response({"ok": True})

    async def owner_file(self, request: web.Request) -> web.Response:
        """One of her files `{row, name, mime, data}` (base64), kept for the app: `{file}`, the row's entry."""
        if request.content_length is None or request.content_length > FILE_BYTES:
            return web.json_response({"error": f"a file over {FILE_MAX // 2**20} MB"}, status=413)
        body = await _body(request)
        row, name, mime, data = body.get("row"), body.get("name"), body.get("mime"), body.get("data")
        if not isinstance(row, str) or not ROW_ID.fullmatch(row) or not isinstance(name, str) or \
                not 0 < len(name) <= 255 or not isinstance(mime, str) or len(mime) > 100 or not isinstance(data, str):
            return web.json_response({"error": "Need row, name, mime and data (base64)."}, status=400)
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return web.json_response({"error": "data: not base64"}, status=400)
        if len(raw) > FILE_MAX:
            return web.json_response({"error": f"a file over {FILE_MAX // 2**20} MB"}, status=413)
        file_id = secrets.token_hex(16)
        self.store.db.execute("INSERT INTO chat_files (id, row, name, mime, size, body, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (file_id, row, outfiles.shown(name), mime, len(raw), raw, self.platform.clock()))
        self._cap()
        self.store.db.commit()
        return web.json_response({"file": {"id": file_id, "name": outfiles.shown(name), "size": len(raw),
                                           "mime": mime}})

    # --- the face -------------------------------------------------------------------------------------------------

    async def face_chat(self, request: web.Request) -> web.Response:
        """The conversation's newest rows, oldest first, and what she is writing now, if she is."""
        draft = self.draft if self.draft and self.platform.clock() - self.draft["at"] < DRAFT_S else None
        return web.json_response({"schema": SCHEMA, "rows": self.rows(), "draft": draft})

    async def face_say(self, request: web.Request) -> web.Response:
        """His message `{input_id, text}`: kept as his row, `pending`, and a `chat` note for the router — once per
        input id, however often the app sends it."""
        body = await _body(request)
        input_id, text = body.get("input_id"), body.get("text")
        if body.get("attachments"):
            return _error(400, NO_FILES)
        if not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id) or not isinstance(text, str) or \
                not text.strip() or len(text) > OWNER_CHARS:
            return _error(400, f"input_id (face:<uuid4>) and text, {OWNER_CHARS} characters at most")
        row_id = f"app:{input_id}"
        if (row := self._row(row_id)) is not None:
            return web.json_response({"ok": True, "row": row})
        row = {"id": row_id, "at": self.platform.clock(), "by": "owner", "text": text, "pending": True}
        self._put(row)
        self.store._note("chat", "", {"text": text, "input_id": input_id})
        self._cap()
        self.store.db.commit()
        return web.json_response({"ok": True, "row": row})

    async def face_press(self, request: web.Request) -> web.Response:
        """A button pressed on a row `{id, row}`: one that row carries, as a `chat-press` note; the router decides."""
        body = await _body(request)
        button, row_id = body.get("id"), body.get("row")
        if not isinstance(button, str) or not BUTTON.fullmatch(button) or not isinstance(row_id, str):
            return _error(400, "Need id — the button's — and row, the card it is on.")
        row = self._row(row_id) if ROW_ID.fullmatch(row_id) else None
        if row is None or button not in [b for _, b in row.get("buttons") or []]:
            return _error(410, GONE)
        self.store.add_note("chat-press", "", {"id": button, "row": row_id})
        return web.json_response({"ok": True})

    async def face_file(self, request: web.Request) -> web.StreamResponse:
        """One of her files, as a download: served as the type its extension is on the list with — never one a
        browser runs —, under the sandbox policy."""
        found = self.store.db.execute("SELECT name, body FROM chat_files WHERE id = ?",
                                      (request.match_info["id"],)).fetchone()
        if found is None:
            return _error(404, "No such file.")
        name, data = found
        mime = outfiles.mime_of(name)
        if mime is None or mime.split(";")[0] in outfiles.ACTIVE:
            mime = "application/octet-stream"
        return web.Response(body=data, headers={"Content-Type": mime, "Content-Security-Policy": FILE_CSP,
                                                "Content-Disposition": _attachment(name)})
