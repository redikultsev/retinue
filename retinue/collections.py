"""The owner's collections (docs/collections.md): one SQLite file beside the platform's own, `collections.sqlite` on
the platform's volume, and the files he uploads, kept by their SHA-256 in `attachments/` beside it.

One thread, one connection. Every read and every write goes through a single dedicated thread that opened the file
itself; the loop only awaits it. The platform's other databases live on the loop's thread, and SQLite's busy
handler sleeps where it is called: a writer that waited on the loop would hold up the hooks, the gate and the face
with it. Here a lock that does not come within 2 s (the night's backup copy, an import) is `Refused` at once, for
the caller to say «try again», never waited out for 30 s.

The file uses a rollback journal, like every database the backup copies (deploy/backup/retinue-backup.py). It holds
`files`, and the records: `schemas` (a collection's form), `records` (each one's current state, a JSON document),
`changes` (every change of one, append-only: the history and the provenance), `batches` (changes that go
together) and `idems` (the calls already made, so a repeat makes none). Every write of a record is one `apply`,
checked whole against the form and the collection's rights before anything is kept; every read is `get`, `query` or
`count`, whose SQL only code assembles, from names the form declares.
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
        idem TEXT,                          -- the idem of the call that made it, when it gave one
        reverts TEXT                        -- the change_id a revert takes back
    ) STRICT;
    CREATE TABLE IF NOT EXISTS idems (
        scope TEXT NOT NULL,                -- the writer's class: owner, assistant, session, mac, code:<rule>, …
        collection TEXT NOT NULL,
        idem TEXT NOT NULL,                 -- call:<the writer's idem>, or mail:… one the store derived
        answer TEXT NOT NULL CHECK (json_valid(answer)),  -- what the first time made: its change_ids, its records
        ts REAL NOT NULL,
        PRIMARY KEY (scope, collection, idem)
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
TYPES = {  # each type, and the keys its field takes beside `type`, `required` and `index`
    "text": (), "int": (), "enum": ("values",), "date": (), "bool": (), "ref": ("to",),
}
OP_KEYS = {  # each operation of `apply`, and the keys it takes
    "add": {"op", "fields", "as_of", "src", "distinct"},
    "set": {"op", "id", "version", "fields", "as_of", "src", "distinct"},
    "delete": {"op", "id", "version", "as_of", "src"},
    "restore": {"op", "id", "version", "as_of", "src", "distinct"},
    "revert": {"op", "change_id", "version", "src"},
}
OWNERS = ("owner", "import", "define")  # classes that may do anything: the owner, and what he runs from the terminal
WRITER_CLASS = re.compile(r"assistant|session|mac|code:[a-z][a-z0-9_-]{0,39}")  # the classes rights name
SET_BY_CLASS = re.compile(r"owner|assistant|session|mac|code:[a-z][a-z0-9_-]{0,39}")
DEFAULT_RIGHTS = {  # a new collection's: sessions add and set, deleting by a proposal; the Mac without deleting;
    "writers": {    # the assistant and every code: rule nothing, until the owner gives them some
        "session": {"add": True, "set": "*", "delete": "propose"},
        "mac": {"add": True, "set": "*", "delete": False},
    },
    "set_by": {},
}
FILTER_OPS = ("eq", "in", "between", "empty", "not_empty", "ref_is")
KEY_MAX = 8                 # fields in a key, or in one unique
UNIQUE_MAX = 8              # uniques in one form
FILTER_MAX = 16             # conditions in one filter
IN_MAX = 200                # values of one `in`
IDEM_MAX = 200              # characters of a call's idem
QUERY_ROWS = 200            # rows of one `query`…
QUERY_BYTES = 60 * 2**10    # …and bytes of them as JSON: what an agent's tool call can take back (25 000 tokens)
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
                  "reverts, idem")


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
    if unknown := set(form) - {"fields", "key", "unique"}:
        raise Rejected(f"a form takes only \"fields\", \"key\" and \"unique\", not "
                       f"{', '.join(sorted(map(repr, unknown)))}")
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
        if unknown := set(spec) - {"type", "required", "index", *extra}:
            raise Rejected(f"field {name!r}: a {spec['type']} takes no {', '.join(sorted(map(repr, unknown)))}")
        for flag in ("required", "index"):
            if not isinstance(spec.get(flag, False), bool):
                raise Rejected(f"field {name!r}: \"{flag}\" is true or false")
        if spec["type"] == "enum":
            values = spec.get("values")
            if (not isinstance(values, list) or not 0 < len(values) <= ENUM_MAX or len(set(values)) != len(values)
                    or not all(isinstance(v, str) and ENUM_VALUE.fullmatch(v) for v in values)):
                raise Rejected(f"field {name!r}: \"values\" is a list of 1 to {ENUM_MAX} distinct values, each "
                               "lowercase letters, digits and _, starting with a letter")
        if spec["type"] == "ref" and not (isinstance(spec.get("to"), str) and NAME.fullmatch(spec["to"])):
            raise Rejected(f"field {name!r}: \"to\" names the collection its records are in")
        kept[name] = dict(spec)
    form_kept: dict = {"fields": kept}
    if "key" in form:
        form_kept["key"] = _names(form["key"], kept, "\"key\"")
    if "unique" in form:
        unique = form["unique"]
        if not isinstance(unique, list) or not 0 < len(unique) <= UNIQUE_MAX:
            raise Rejected(f"\"unique\" is a list of 1 to {UNIQUE_MAX} lists of fields: [[\"n\"], [\"a\", \"b\"]]")
        form_kept["unique"] = [_names(names, kept, f"unique[{n}]") for n, names in enumerate(unique)]
        if len({tuple(names) for names in form_kept["unique"]}) != len(unique):
            raise Rejected("\"unique\" names each list of fields once")
    return form_kept


def _names(names: Any, fields: dict, what: str) -> list[str]:
    if (not isinstance(names, list) or not 0 < len(names) <= KEY_MAX or not all(isinstance(n, str) for n in names)
            or len(set(names)) != len(names)):
        raise Rejected(f"{what} is a list of 1 to {KEY_MAX} distinct fields of the form")
    if unknown := [n for n in names if n not in fields]:
        raise Rejected(f"{what}: the form has no field {', '.join(map(repr, unknown))}")
    return list(names)


def parse_rights(rights: Any, form: dict) -> dict:
    """A collection's rights, checked strictly against its form: `writers` — for each class, whether it adds, which
    fields it sets (\"*\" for all), whether it deletes (true, false, or \"propose\": by a proposal the owner
    applies) — and `set_by` — for an enum's value, the only classes that give a field it. Returns them as kept, every
    key filled in. Raises Rejected with the reason."""
    if not isinstance(rights, dict):
        raise Rejected("rights are a JSON object: {\"writers\": {...}, \"set_by\": {...}}")
    if len(_json(rights).encode()) > FORM_MAX:
        raise Rejected(f"rights are at most {FORM_MAX // 1024} KB")
    if unknown := set(rights) - {"writers", "set_by"}:
        raise Rejected(f"rights take only \"writers\" and \"set_by\", not {', '.join(sorted(map(repr, unknown)))}")
    fields, writers, set_by = form["fields"], rights.get("writers", {}), rights.get("set_by", {})
    if not isinstance(writers, dict) or not isinstance(set_by, dict):
        raise Rejected("\"writers\" and \"set_by\" are objects")
    kept: dict = {"writers": {}, "set_by": {}}
    for cls, grant in writers.items():
        if not WRITER_CLASS.fullmatch(cls):
            raise Rejected(f"writers: {cls!r} is not a writer's class: assistant, session, mac or code:<rule> "
                           "(the owner may do anything, and is not listed)")
        if not isinstance(grant, dict) or (unknown := set(grant) - {"add", "set", "delete"}):
            raise Rejected(f"writers.{cls} takes \"add\", \"set\" and \"delete\"")
        add, open_, delete = grant.get("add", False), grant.get("set", []), grant.get("delete", False)
        if not isinstance(add, bool):
            raise Rejected(f"writers.{cls}.add is true or false")
        if open_ != "*":
            open_ = _names(open_, fields, f"writers.{cls}.set") if open_ != [] else []
        if delete not in (True, False, "propose") or type(delete) is int:
            raise Rejected(f"writers.{cls}.delete is true, false or \"propose\"")
        kept["writers"][cls] = {"add": add, "set": open_, "delete": delete}
    for name, values in set_by.items():
        spec = fields.get(name)
        if spec is None or spec["type"] != "enum":
            raise Rejected(f"set_by: {name!r} is not an enum of the form")
        if not isinstance(values, dict) or not values:
            raise Rejected(f"set_by.{name} is an object: {{\"<value>\": [\"<class>\", ...]}}")
        for value, who in values.items():
            if value not in spec["values"]:
                raise Rejected(f"set_by.{name}: {value!r} is not one of its values")
            if (not isinstance(who, list) or not who or len(set(map(str, who))) != len(who)
                    or not all(isinstance(c, str) and SET_BY_CLASS.fullmatch(c) for c in who)):
                raise Rejected(f"set_by.{name}.{value} is a list of classes: owner, assistant, session, mac, "
                               "code:<rule>")
        kept["set_by"][name] = {value: list(who) for value, who in values.items()}
    return kept


def _path(field: str) -> str:
    """A field's value in a record, as SQL: the one place a name goes into a statement, so an index and the query
    meant to use it say it the same — and only a name the form declared, which is letters, digits and _."""
    assert NAME.fullmatch(field), field
    return f"json_extract(data, '$.{field}')"


def _within(collection: str) -> str:
    """The records of a collection that are not deleted, as SQL: a partial index's WHERE, and every query's, so the
    planner sees that one implies the other."""
    assert NAME.fullmatch(collection), collection
    return f"collection = '{collection}' AND deleted IS NULL"


def _indexes(collection: str, form: dict) -> dict[str, str]:
    """The indexes a form asks for, by their name: a unique one for each `unique`, a plain one for each field marked
    `index`, all on the collection's records that are not deleted."""
    made = {}
    for names in form.get("unique", ()):
        name = f"records:{collection}:unique:{','.join(names)}"
        made[name] = (f'CREATE UNIQUE INDEX "{name}" ON records ({", ".join(map(_path, names))}) '
                      f"WHERE {_within(collection)}")
    for field, spec in form["fields"].items():
        if spec.get("index"):
            name = f"records:{collection}:index:{field}"
            made[name] = f'CREATE INDEX "{name}" ON records ({_path(field)}) WHERE {_within(collection)}'
    return made


def writer_class(actor: str) -> str:
    """The class a writer's rights and its idems go by: a `code:` rule its own, every other actor its kind
    (`session:<id>` is a session)."""
    return actor if actor.startswith("code:") else actor.split(":")[0]


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
        """A collection's form, new or in place of the one it had, as a `define` change: `{name, rev}`. A new
        collection gets the default rights (DEFAULT_RIGHTS); a form is the owner's to apply. Raises Rejected,
        Refused."""
        return await self._run(self._define, writer, collection, space, form, now)

    def _define(self, writer: Writer, collection: str, space: str, form: Any, now: float) -> dict:
        _check_writer(writer)
        if writer_class(writer.actor) not in ("owner", "define"):
            raise Rejected("a form is the owner's to apply, from the server's terminal")
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
            old = db.execute("SELECT form, rev, rights FROM schemas WHERE collection = ?", (collection,)).fetchone()
            if old is not None:
                try:
                    parse_rights(json.loads(old[2]), form)
                except Rejected as exc:
                    raise Rejected(f"the collection's rights do not fit this form: {exc.reason}") from None
            rev = old[1] + 1 if old else 1
            db.execute("INSERT INTO schemas (collection, space, form, rights, rev, updated) VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT (collection) DO UPDATE SET space = excluded.space, form = excluded.form, "
                       "rev = excluded.rev, updated = excluded.updated",
                       (collection, space, _json(form), _json(DEFAULT_RIGHTS), rev, now))
            self._index(collection, form)
            self._change(writer, now, collection, None, "define", {"form": json.loads(old[0])} if old else None,
                         {"form": form})
            return {"name": collection, "rev": self._rev(collection)}

    def _index(self, collection: str, form: dict) -> None:
        """The collection's indexes made what its form asks: those it no longer asks for dropped, the new ones made
        over the records already there — a unique one the records break Rejected."""
        prefix = f"records:{collection}:"
        have = {name for (name,) in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'records' AND substr(name, 1, ?) = ?",
            (len(prefix), prefix))}
        wanted = _indexes(collection, form)
        for name in have - set(wanted):
            self._db.execute(f'DROP INDEX "{name}"')
        for name in set(wanted) - have:
            try:
                self._db.execute(wanted[name])
            except sqlite3.IntegrityError:
                names = name.rsplit(":", 1)[1].replace(",", ", ")
                raise Rejected(f"records of {collection} share a {names}: make them distinct before it is "
                               "unique") from None

    async def rights(self, writer: Writer, collection: str, rights: Any, now: float) -> dict:
        """A collection's rights (`parse_rights`) in place of the ones it had, as a `define` change: `{name, rev}`.
        The owner's alone — no agent's path reaches here (docs/collections.md#rights). Raises Rejected, Refused."""
        return await self._run(self._rights, writer, collection, rights, now)

    def _rights(self, writer: Writer, collection: str, rights: Any, now: float) -> dict:
        _check_writer(writer)
        if writer.actor != "owner":
            raise Rejected("rights are the owner's alone, set from the server's terminal")
        with self._write() as db:
            row = db.execute("SELECT form, rights FROM schemas WHERE collection = ?", (collection,)).fetchone()
            if row is None:
                raise Rejected(f"there is no collection {collection!r}")
            old, new = json.loads(row[1]), parse_rights(rights, json.loads(row[0]))
            if new != old:
                db.execute("UPDATE schemas SET rights = ?, updated = ? WHERE collection = ?",
                           (_json(new), now, collection))
                self._change(writer, now, collection, None, "define", {"rights": old}, {"rights": new})
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

    async def apply(self, writer: Writer, collection: str, ops: Any, now: float, batch: str | None = None,
                    idem: str | None = None) -> dict:
        """The one way a record is written: up to OPS_MAX operations on one collection, checked against its form and
        its rights and kept whole in one transaction, or none of them (docs/collections.md#records). Each is `add`,
        `set`, `delete`, `restore` or `revert`. Returns `{name, rev, change_ids, records}`: the collection, its newest
        change_id (what a screen compares to know it is behind), and for each operation the change it made (None when
        it changed nothing: the record was already so) and the record after it. `idem` names the call: made again by
        the same class of writer, it changes nothing and answers the first time's change_ids, with the records as
        they are now. Raises Rejected, Refused."""
        return await self._run(self._apply, writer, collection, ops, now, batch, idem)

    def _apply(self, writer: Writer, collection: str, ops: Any, now: float, batch: str | None,
               idem: str | None) -> dict:
        _check_writer(writer)
        if idem is not None and not _plain(idem, IDEM_MAX):
            raise Rejected(f"idem is the call's own name, 1 to {IDEM_MAX} plain characters")
        if not isinstance(ops, list) or not 0 < len(ops) <= OPS_MAX:
            raise Rejected(f"ops is a list of 1 to {OPS_MAX} operations")
        cls = writer_class(writer.actor)
        with self._write() as db:
            schema = self._schema(collection)
            if schema is None:
                raise Rejected(f"there is no collection {collection!r}")
            if idem is not None and (first := self._idem(cls, collection, f"call:{idem}")) is not None:
                return {"name": collection, "rev": self._rev(collection), "change_ids": first["change_ids"],
                        "records": [self._record(collection, rid) for rid in first["ids"]]}
            if batch is not None and db.execute("SELECT 1 FROM batches WHERE id = ? AND state = 'applying'",
                                                (batch,)).fetchone() is None:
                raise Rejected(f"no batch {batch!r} is applying")
            change_ids, records, adds = [], [], {}
            for n, op in enumerate(ops):
                try:
                    record, change = self._op(writer, collection, schema, op, now, batch, idem, adds)
                except Rejected as exc:
                    raise Rejected(f"ops[{n}]: {exc.reason}", exc.record, exc.change) from None
                change_ids.append(change)
                records.append(record)
            if idem is not None:
                self._remember(cls, collection, f"call:{idem}",
                               {"change_ids": change_ids, "ids": [r["id"] for r in records]}, now)
            return {"name": collection, "rev": self._rev(collection), "change_ids": change_ids, "records": records}

    def _op(self, writer: Writer, collection: str, schema: tuple[dict, dict], op: Any, now: float,
            batch: str | None, idem: str | None, adds: dict) -> tuple[dict, str | None]:
        form, rights = schema
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in OP_KEYS:
            raise Rejected(f"an operation is an object whose \"op\" is one of {', '.join(OP_KEYS)}")
        if unknown := set(op) - OP_KEYS[kind]:
            raise Rejected(f"{kind} takes {', '.join(sorted(OP_KEYS[kind]))}; not {', '.join(sorted(unknown))}")
        as_of, src, distinct = op.get("as_of"), op.get("src"), op.get("distinct", False)
        if as_of is not None and not _date(as_of):
            raise Rejected(f"as_of is the date the world became so, YYYY-MM-DD: not {as_of!r}")
        if src is not None and not (SRC.fullmatch(src) if _plain(src, 1100) else False):
            raise Rejected("src is mail:<account>/<msgid>, tg:<chat>/<msg>, owner_words:<event>, "
                           "cal:<account>/<event>, file:<sha256>#row=<n>, record:<id> or web:<url>")
        if not isinstance(distinct, bool):
            raise Rejected("distinct is true or false: true when the record is another than the one it matches")
        cls = writer_class(writer.actor)
        mail = src if src is not None and src.startswith("mail:") else None  # its idems the store derives
        stamp = {"writer": writer, "now": now, "collection": collection, "as_of": as_of, "src": src, "batch": batch,
                 "idem": idem}
        if kind == "add":
            self._may(collection, rights, cls, "add", op.get("fields"))
            fields = self._fields(collection, form, op.get("fields"), {})
            if mail is not None:
                adds[mail] = adds.get(mail, 0) + 1
                derived = f"{mail}#add/{adds[mail]}"
                if (first := self._idem(cls, collection, derived)) is not None:
                    return self._record(collection, first["id"]), None
            rid = self._ulids.next(now)
            self._keys(collection, form, fields, rid, None, not distinct)
            self._db.execute(f"INSERT INTO records (collection, {RECORD_COLUMNS}) VALUES (?, ?, ?, 1, ?, ?, NULL)",
                             (collection, rid, _json(fields), now, now))
            change = self._change(**stamp, rid=rid, op="add", before=None, after=fields)
            if mail is not None:
                self._remember(cls, collection, derived, {"change_id": change, "id": rid}, now)
            return self._record(collection, rid), change
        if kind == "revert":
            return self._revert(stamp, schema, op)
        self._may(collection, rights, cls, kind, op.get("fields"))
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
        if mail is not None:  # what this mail made this class of writer change already is not changed again
            for done in [k for k in before if self._idem(cls, collection, f"{mail}#{record['id']}/{k}") is not None]:
                del before[done], after[done]
        if not before:  # already so: nothing is written, the version stays
            return record, None
        record, change = self._put(stamp, form, record, before, after, kind, check_key=not distinct)
        if mail is not None:
            for k in before:
                self._remember(cls, collection, f"{mail}#{record['id']}/{k}", {"change_id": change}, now)
        return record, change

    def _may(self, collection: str, rights: dict, cls: str, kind: str, fields: Any = None) -> None:
        """Rejected unless the class may `kind` (add, set, delete, restore) in the collection, with these `fields`
        and the values they are given, by the collection's rights; the owner, and what he runs from the server's
        terminal, may do anything."""
        if cls in OWNERS:
            return
        grant = rights["writers"].get(cls, {})
        if kind == "add" and not grant.get("add"):
            raise Rejected(f"{cls} does not add records to {collection}: propose it to the owner instead")
        if kind in ("delete", "restore"):
            if grant.get("delete") == "propose":
                raise Rejected(f"a {kind} by {cls} in {collection} is the owner's to make: propose it to him")
            if grant.get("delete") is not True:
                raise Rejected(f"{cls} does not {kind} records in {collection}")
            return
        if not isinstance(fields, dict):
            return  # `_fields` says what is wrong with it
        open_ = grant.get("set", [])
        if blocked := [name for name in fields if open_ != "*" and name not in open_]:
            raise Rejected(f"{', '.join(map(repr, blocked))} {'is' if len(blocked) == 1 else 'are'} not open to "
                           f"{cls} in {collection}: propose the change to the owner instead")
        for name, value in fields.items():
            who = rights["set_by"].get(name, {}).get(value) if isinstance(value, str) else None
            if who is not None and cls not in who:
                by = ", ".join(dict.fromkeys(["the owner", *(c for c in who if c != "owner")]))
                raise Rejected(f"{name!r} becomes {value!r} only by {by}: not by {cls}")

    def _keys(self, collection: str, form: dict, data: dict, rid: str, changed: set | None, check_key: bool) -> None:
        """Rejected when the record, as it will be, holds what one of the form's `unique` gives one record only and
        another record holds it (`taken`) — or, unless the writer said the record is `distinct`, matches another on
        the form's `key` (`possible duplicate`). `changed` is the fields the write changes; None when the record
        comes into being, or back."""
        for names in form.get("unique", ()):
            if (changed is None or changed & set(names)) and all(name in data for name in names):
                row = self._db.execute(
                    f"SELECT id FROM records WHERE {_within(collection)} AND "
                    + " AND ".join(f"{_path(name)} = ?" for name in names) + " AND id != ? LIMIT 1",
                    (*(data[name] for name in names), rid)).fetchone()
                if row is not None:
                    what = ", ".join(f"{name} {data[name]!r}" for name in names)
                    raise Rejected(f"taken: {row[0]} has {what}", self._record(collection, row[0]))
        key = form.get("key")
        if not check_key or not key or not (changed is None or changed & set(key)) or not all(k in data for k in key):
            return
        same = [f"lower(trim({_path(k)})) = lower(trim(?))" if form["fields"][k]["type"] == "text"
                else f"{_path(k)} = ?" for k in key]
        row = self._db.execute(f"SELECT id FROM records WHERE {_within(collection)} AND {' AND '.join(same)} "
                               "AND id != ? LIMIT 1", (*(data[k] for k in key), rid)).fetchone()
        if row is not None:
            raise Rejected(f"possible duplicate of {row[0]}: the same {', '.join(key)}. Change that record instead, "
                           "or give \"distinct\": true if this one is another", self._record(collection, row[0]))

    def _revert(self, stamp: dict, schema: tuple[dict, dict], op: dict) -> tuple[dict, str | None]:
        """A change taken back, as a change of its own: what it changed set back to what it was — unless something
        has changed it since, which is Rejected with who did. Taking back an add deletes the record."""
        form, rights = schema
        cid = op.get("change_id")
        row = self._db.execute(f"SELECT seq, {CHANGE_COLUMNS} FROM changes WHERE change_id = ?",
                               (cid,)).fetchone() if isinstance(cid, str) else None
        if row is None or row[5] != stamp["collection"]:
            raise Rejected(f"change not found: {cid!r}")
        seq, target = row[0], _change(row[1:])
        if target["op"] not in OP_KEYS:
            raise Rejected(f"a {target['op']} is not taken back by a revert")
        if target["op"] == "add":
            touched, goal = None, {DELETED: stamp["now"]}
        else:
            touched, goal = set(target["after"]), target["before"]
        fields = {k: v for k, v in goal.items() if k != DELETED}
        cls = writer_class(stamp["writer"].actor)
        if DELETED in goal:
            self._may(stamp["collection"], rights, cls, "delete" if goal[DELETED] is not None else "restore")
        if fields:
            self._may(stamp["collection"], rights, cls, "set", fields)
        record = self._at(stamp["collection"], {**op, "id": target["id"]})
        later = [_change(r) for r in self._db.execute(
            f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection = ? AND id = ? AND seq > ? ORDER BY seq",
            (stamp["collection"], target["id"], seq))]
        for change in later:
            if touched is None or touched & set(change["after"] or {}):
                what = ", ".join(sorted("deleted" if k == DELETED else k
                                        for k in change["after"] if touched is None or k in touched))
                raise Rejected(f"changed after it: {what} by {change['actor']} ({change['op']}, "
                               f"{change['change_id']}) — revert that first, or set the value", record, change)
        if fields and record["deleted"] is not None and goal.get(DELETED, record["deleted"]) is not None:
            raise Rejected(f"{record['id']} is deleted: restore it first", record)
        data = {k: v for k, v in {**record["fields"], **fields}.items() if v is not None}
        self._required(form, data)
        before = {k: record["deleted"] if k == DELETED else record["fields"].get(k) for k in goal}
        if before == goal:
            return record, None
        return self._put({**stamp, "as_of": None}, form, record, before, goal, "revert", reverts=target["change_id"],
                         check_key=False)

    def _put(self, stamp: dict, form: dict, record: dict, before: dict, after: dict, op: str,
             reverts: str | None = None, check_key: bool = True) -> tuple[dict, str]:
        """The record changed by `after` (its fields, and DELETED for its deleted state), its version one more, and
        the change kept — unless it would take another's unique value, or (`check_key`) match another on the key."""
        data = {**record["fields"], **{k: v for k, v in after.items() if k != DELETED}}
        data = {k: v for k, v in data.items() if v is not None}
        if len(_json(data).encode()) > RECORD_MAX:
            raise Rejected(f"a record is at most {RECORD_MAX // 1024} KB of fields")
        deleted = after.get(DELETED, record["deleted"])
        if deleted is None:
            changed = None if record["deleted"] is not None else set(after)
            self._keys(stamp["collection"], form, data, record["id"], changed, check_key)
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

    def _value(self, name: str, spec: dict, value: Any, probe: bool = False) -> Any:
        """A field's value checked against its type, as it is kept. `probe` for a value a filter compares with: a
        ref is only an id then, not one that has to exist."""
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
            if not isinstance(value, str) or value not in spec["values"]:
                raise Rejected(f"{name!r} is one of {', '.join(spec['values'])}: not {value!r}")
        elif kind == "date":
            if not _date(value):
                raise Rejected(f"{name!r} is a date, YYYY-MM-DD: not {value!r}")
        elif kind == "bool":
            if type(value) is not bool:
                raise Rejected(f"{name!r} is true or false: not {value!r}")
        elif kind == "ref" and probe:
            if not isinstance(value, str) or not ULID.fullmatch(value):
                raise Rejected(f"{name!r} is the id of a record in {spec['to']}: not {value!r}")
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
                reverts: str | None = None, idem: str | None = None) -> str:
        cid = self._ulids.next(now)
        self._db.execute(f"INSERT INTO changes ({CHANGE_COLUMNS}) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (cid, now, as_of, batch, collection, rid, op, _json(before), _json(after), writer.actor,
                          writer.model, writer.via, src, ",".join(sorted(writer.foreign)) or None, reverts, idem))
        return cid

    def _idem(self, scope: str, collection: str, idem: str) -> dict | None:
        row = self._db.execute("SELECT answer FROM idems WHERE scope = ? AND collection = ? AND idem = ?",
                               (scope, collection, idem)).fetchone()
        return json.loads(row[0]) if row else None

    def _remember(self, scope: str, collection: str, idem: str, answer: dict, now: float) -> None:
        self._db.execute("INSERT INTO idems (scope, collection, idem, answer, ts) VALUES (?, ?, ?, ?, ?)",
                         (scope, collection, idem, _json(answer), now))

    def _form(self, collection: str) -> dict | None:
        row = self._db.execute("SELECT form FROM schemas WHERE collection = ?", (collection,)).fetchone()
        return json.loads(row[0]) if row else None

    def _schema(self, collection: str) -> tuple[dict, dict] | None:
        """A collection's form and rights, or None."""
        row = self._db.execute("SELECT form, rights FROM schemas WHERE collection = ?", (collection,)).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def _rev(self, collection: str) -> str | None:
        row = self._db.execute("SELECT change_id FROM changes WHERE collection = ? ORDER BY seq DESC LIMIT 1",
                               (collection,)).fetchone()
        return row[0] if row else None

    def _record(self, collection: str, rid: str) -> dict | None:
        row = self._db.execute(f"SELECT {RECORD_COLUMNS} FROM records WHERE collection = ? AND id = ?",
                               (collection, rid)).fetchone()
        return _record(row) if row else None

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

    # --- reading ----------------------------------------------------------------------------------------------------

    async def query(self, collection: str, filter: Any = None, fields: Any = None, limit: int = QUERY_ROWS,
                    after: str | None = None, deleted: bool = False) -> dict:
        """The records that match `filter` (docs/collections.md#reading), oldest first: `{rows, next}` — at most
        `limit` (≤ QUERY_ROWS) rows and QUERY_BYTES of them, each with only `fields` when they are named, and `next`
        the id to pass as `after` for the rest, None when there is none. Deleted records only when `deleted`.
        Raises Rejected, Refused."""
        return await self._run(self._query, collection, filter, fields, limit, after, deleted)

    def _query(self, collection: str, filter: Any, fields: Any, limit: Any, after: Any, deleted: bool) -> dict:
        form = self._reading(collection)
        where, params = self._where(collection, form, filter, deleted)
        if fields is not None:
            fields = _names(fields, form["fields"], "fields")
        if type(limit) is not int or not 0 < limit <= QUERY_ROWS:
            raise Rejected(f"limit is 1 to {QUERY_ROWS}")
        if after is not None:
            if not isinstance(after, str) or not ULID.fullmatch(after):
                raise Rejected(f"after is the id `next` gave: not {after!r}")
            where, params = f"{where} AND id > ?", [*params, after]
        rows, size, more = [], 0, False
        for row in self._db.execute(f"SELECT {RECORD_COLUMNS} FROM records WHERE {where} ORDER BY id LIMIT ?",
                                    (*params, limit + 1)):
            record = _record(row)
            if fields is not None:
                record["fields"] = {k: v for k, v in record["fields"].items() if k in fields}
            size += len(_json(record).encode()) + 1
            if len(rows) == limit or size > QUERY_BYTES:
                more = True
                break
            rows.append(record)
        return {"rows": rows, "next": rows[-1]["id"] if more and rows else None}

    async def count(self, collection: str, filter: Any = None, group: str | None = None) -> int | list[dict]:
        """How many records match `filter`, deleted ones aside; with `group` (an enum's or a bool's name), how many
        have each of its values: `[{value, count}]`, the most first, `value` None for those it is unknown in. Raises
        Rejected, Refused."""
        return await self._run(self._count, collection, filter, group)

    def _count(self, collection: str, filter: Any, group: Any) -> int | list[dict]:
        form = self._reading(collection)
        where, params = self._where(collection, form, filter, False)
        if group is None:
            return self._db.execute(f"SELECT count(*) FROM records WHERE {where}", params).fetchone()[0]
        spec = form["fields"].get(group) if isinstance(group, str) else None
        if spec is None or spec["type"] not in ("enum", "bool"):
            raise Rejected(f"group is an enum or a bool of {collection}: not {group!r}")
        rows = self._db.execute(f"SELECT {_path(group)}, count(*) FROM records WHERE {where} GROUP BY 1 "
                                "ORDER BY 2 DESC, 1", params)
        return [{"value": bool(value) if spec["type"] == "bool" and value is not None else value, "count": n}
                for value, n in rows]

    def _reading(self, collection: str) -> dict:
        form = self._form(collection) if isinstance(collection, str) and NAME.fullmatch(collection) else None
        if form is None:
            raise Rejected(f"there is no collection {collection!r}")
        return form

    def _where(self, collection: str, form: dict, filter: Any, deleted: bool) -> tuple[str, list]:
        """A filter as SQL and its parameters: conditions that all hold, each `{field, op, value}`, every name checked
        against the form before it goes into the statement, every value a parameter."""
        where = _within(collection) if not deleted else f"collection = '{collection}'"
        if filter is None:
            return where, []
        conditions = [filter] if isinstance(filter, dict) else filter
        if not isinstance(conditions, list) or len(conditions) > FILTER_MAX:
            raise Rejected(f"filter is a condition, or a list of up to {FILTER_MAX} that all hold: "
                           "{\"field\": \"stage\", \"op\": \"in\", \"value\": [\"applied\", \"interview\"]}")
        terms, params = [where], []
        for n, condition in enumerate(conditions):
            try:
                term, values = self._condition(collection, form, condition)
            except Rejected as exc:
                raise Rejected(f"filter[{n}]: {exc.reason}") from None
            terms.append(term)
            params += values
        return " AND ".join(terms), params

    def _condition(self, collection: str, form: dict, condition: Any) -> tuple[str, list]:
        if not isinstance(condition, dict) or set(condition) - {"field", "op", "value"}:
            raise Rejected("a condition is {\"field\": …, \"op\": …, \"value\": …}")
        field, op = condition.get("field"), condition.get("op")
        if op not in FILTER_OPS:
            raise Rejected(f"\"op\" is one of {', '.join(FILTER_OPS)}: not {op!r}")
        if not isinstance(field, str):
            raise Rejected("\"field\" names a field of the form, or a ref's field: \"lead.stage\"")
        name, _, inner = field.partition(".")
        spec = form["fields"].get(name)
        if spec is None:
            raise Rejected(f"{collection} has no field {name!r}; its fields: {', '.join(form['fields'])}")
        if not inner:
            return self._match(name, spec, op, condition)
        if spec["type"] != "ref":
            raise Rejected(f"{name!r} is not a ref: it has no fields of its own")
        if op not in ("eq", "in"):
            raise Rejected(f"a ref's field is matched by eq or in: not {op}")
        target = self._form(spec["to"])
        inner_spec = target["fields"].get(inner) if target else None
        if inner_spec is None:
            raise Rejected(f"{spec['to']} has no field {inner!r}")
        term, values = self._match(inner, inner_spec, op, condition)
        return f"{_path(name)} IN (SELECT id FROM records WHERE {_within(spec['to'])} AND {term})", values

    def _match(self, name: str, spec: dict, op: str, condition: dict) -> tuple[str, list]:
        value = condition.get("value")
        if op in ("empty", "not_empty"):
            if "value" in condition:
                raise Rejected(f"{op} takes no value")
            return f"{_path(name)} IS {'NOT ' if op == 'not_empty' else ''}NULL", []
        if op == "eq" or op == "ref_is":
            if op == "ref_is" and spec["type"] != "ref":
                raise Rejected(f"{name!r} is not a ref: match it by eq")
            return f"{_path(name)} = ?", [self._value(name, spec, value, probe=True)]
        if op == "in":
            if not isinstance(value, list) or not 0 < len(value) <= IN_MAX:
                raise Rejected(f"in takes a list of 1 to {IN_MAX} values")
            return (f"{_path(name)} IN ({', '.join('?' * len(value))})",
                    [self._value(name, spec, v, probe=True) for v in value])
        if spec["type"] != "date":
            raise Rejected(f"between is for dates: {name!r} is a {spec['type']}")
        if not isinstance(value, list) or len(value) != 2 or not all(map(_date, value)) or value[0] > value[1]:
            raise Rejected("between takes [first, last], two dates YYYY-MM-DD, the first not after the last")
        return f"{_path(name)} BETWEEN ? AND ?", list(value)


def _record(row: tuple) -> dict:
    return {"id": row[0], "fields": json.loads(row[1]), "version": row[2], "created": row[3], "updated": row[4],
            "deleted": row[5]}


def _change(row: tuple) -> dict:
    change = dict(zip(("change_id", "ts", "as_of", "batch", "collection", "id", "op", "before", "after", "actor",
                       "model", "via", "src", "foreign", "reverts", "idem"), row))
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
