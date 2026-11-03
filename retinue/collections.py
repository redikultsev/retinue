"""The owner's collections (docs/collections.md): one SQLite file beside the platform's own, `collections.sqlite` on
the platform's volume, and the files he uploads, kept by their SHA-256 in `attachments/` beside it.

One thread, one connection. Every read and every write goes through a single dedicated thread that opened the file
itself; the loop only awaits it. The platform's other databases live on the loop's thread, and SQLite's busy
handler sleeps where it is called: a writer that waited on the loop would hold up the hooks, the gate and the face
with it. Here a lock that does not come within 2 s (the night's backup copy, an import) is `Refused` at once, for
the caller to say «try again», never waited out for 30 s.

The file uses a rollback journal, like every database the backup copies (deploy/backup/retinue-backup.py). It holds
`files`, and the records: `schemas` (a collection's form, rights and views), `records` (each one's current state, a JSON document),
`changes` (every change of one, append-only: the history and the provenance), `batches` (changes that go
together), `idems` (the calls already made, so a repeat makes none) and `devices` (the owner's Macs that reach the
collections at `/mcp/mac`, each by the SHA-256 of its token). Every write of a record is one `apply`,
checked whole against the form and the collection's rights before anything is kept; every read is `get`, `query` or
`count`, whose SQL only code assembles, from names the form declares — or a `Reader`'s, in one transaction: what the
views, the agents' tools and the events read beside them. No other module names a table or a column of the file.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
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
        scope TEXT NOT NULL,                -- whose they are (`idem_scope`): a session's own, the assistant's, …
        collection TEXT NOT NULL,
        idem TEXT NOT NULL,                 -- call:<the writer's idem>, or mail:… one the store derived
        answer TEXT NOT NULL CHECK (json_valid(answer)),  -- what the first time made, and of which operations
        ts REAL NOT NULL,
        PRIMARY KEY (scope, collection, idem)
    ) STRICT;
    CREATE TABLE IF NOT EXISTS devices (
        name TEXT PRIMARY KEY,              -- the device: its changes are by mac:<name>/<session>; never given twice
        token_sha256 TEXT NOT NULL UNIQUE,  -- of its token, which only the device keeps
        scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
        created REAL NOT NULL,
        revoked REAL                        -- when the owner took it back: the token opens nothing from then on
    ) STRICT;
    CREATE TABLE IF NOT EXISTS projects (
        project TEXT PRIMARY KEY,           -- the project (a Space): its collections are those of its `space`
        description TEXT NOT NULL CHECK (json_valid(description)),  -- its project.yaml as applied (`project.py`)
        rev INTEGER NOT NULL,               -- 1 at its first apply, one more at each after
        updated REAL NOT NULL
    ) STRICT;
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,                -- a ULID
        source TEXT NOT NULL,               -- what fired it: change:<change_id>, or call:<writer>/<idem>
        project TEXT NOT NULL,              -- the project that declares it, and its name there
        name TEXT NOT NULL,
        collection TEXT,
        record TEXT,
        depth INTEGER NOT NULL,             -- 0 for a change no reaction made; one more along a chain
        at REAL NOT NULL,
        outcome TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(outcome)),  -- what each reaction did
        UNIQUE (source, project, name)
    ) STRICT;
    CREATE INDEX IF NOT EXISTS changes_record ON changes (collection, id, seq);
    CREATE INDEX IF NOT EXISTS changes_collection ON changes (collection, seq);
    CREATE TRIGGER IF NOT EXISTS changes_append_only_u BEFORE UPDATE ON changes
        BEGIN SELECT RAISE(ABORT, 'changes are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS changes_append_only_d BEFORE DELETE ON changes
        BEGIN SELECT RAISE(ABORT, 'changes are append-only'); END;
"""
FILE_COLUMNS = "sha256, name, size, mime, created"
DEVICE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")  # a Mac's name, as `mac:<device>/<session>` names it
SCOPES = ("read", "write")  # a device's token: describe and query; or those, apply and propose too

NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")      # a collection's name, and a field's
ENUM_VALUE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # as spaces.py names a Space
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z")  # a moment in UTC: 2026-11-04T17:30:00Z
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PROJECT = r"[a-z0-9][a-z0-9_-]{0,63}"           # a project's name, as a Space's (`SPACE` below)
ACTOR = re.compile(r"owner|assistant|session:[A-Za-z0-9_-]{1,64}|mac:[a-z0-9][a-z0-9_-]{0,39}/[A-Za-z0-9_-]{1,64}"
                   rf"|code:[a-z][a-z0-9_-]{{0,39}}|project:{PROJECT}|import:[0-9A-HJKMNP-TV-Z]{{26}}"
                   r"|define:[1-9][0-9]{0,8}")
VIA = ("screen", "session", "mac", "assistant", "mail", "code", "event", "import", "define")
SRC = re.compile(r"(?:mail|tg|owner_words|cal|file|record|web):\S{1,1000}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")      # a mark of foreign text the writer read (core.FOREIGN's keys)
TYPES = {  # each type, and the keys its field takes beside `type`, `required` and `index`
    "text": (), "int": (), "enum": ("values", "labels"), "date": (), "instant": (), "bool": (), "ref": ("to",),
    "money": ("currency",), "file": (),
}
CURRENCY = re.compile(r"[A-Z]{3}")              # an ISO 4217 code: a money field's one currency
MONEY = re.compile(r"-?\d{1,13}(?:\.\d{1,2})?")  # an amount as given: at most two places, so cents add up exactly
MONEY_MAX = 10**13                              # an amount's whole part stays under this
OP_KEYS = {  # each operation of `apply`, and the keys it takes
    "add": {"op", "fields", "as_of", "src", "distinct"},
    "set": {"op", "id", "version", "fields", "as_of", "src", "distinct"},
    "delete": {"op", "id", "version", "as_of", "src"},
    "restore": {"op", "id", "version", "as_of", "src", "distinct"},
    "revert": {"op", "change_id", "version", "src"},
}
OWNERS = ("owner", "import", "define")  # classes that may do anything: the owner, and what he runs from the terminal
WRITER_CLASS = re.compile(rf"assistant|session|mac|code:[a-z][a-z0-9_-]{{0,39}}|project:{PROJECT}")  # rights' classes
SET_BY_CLASS = re.compile(rf"owner|assistant|session|mac|code:[a-z][a-z0-9_-]{{0,39}}|project:{PROJECT}")
DEFAULT_RIGHTS = {  # a new collection's: sessions add and set, deleting by a proposal; the Mac without deleting;
    "writers": {    # the assistant and every code: rule nothing, until the owner gives them some
        "session": {"add": True, "set": "*", "delete": "propose"},
        "mac": {"add": True, "set": "*", "delete": False},
    },
    "set_by": {},
}
FILTER_OPS = ("eq", "in", "between", "empty", "not_empty", "ref_is")
VIEW_KEYS = {  # each kind of view (`views.py`), and the keys it takes
    "stat": {"name", "kind", "title", "tab", "count", "filter", "date", "field", "to", "window", "goal"},
    "board": {"name", "kind", "title", "tab", "group", "filter", "fields", "folded", "rot", "add"},
    "queue": {"name", "kind", "title", "tab", "filter", "fields", "since", "sort", "mark", "rot", "done", "add", "latest"},
    "agenda": {"name", "kind", "title", "tab", "filter", "date", "fields", "days"},
    "chart": {"name", "kind", "title", "tab", "filter", "date", "group", "weeks"},
    "months": {"name", "kind", "title", "tab", "filter", "date", "sum", "group", "months"},
    "year": {"name", "kind", "title", "tab", "filter", "date", "sum"},
}
MONTHS = (2, 24)            # the months a `months` view sums, this one among them
MARKS = ("act", "wait")     # a queue's rows: the owner's to act on, or his to wait for
TAB_MAX = 24                # characters of a view's tab
LABEL_WORDS_MAX = 40        # characters of an enum value's label
AGENDA_DAYS = (1, 31)       # the days an agenda looks ahead, today among them
CHART_WEEKS = (2, 26)       # the weeks a chart counts, this one among them
DONE_KEYS = {"field", "value", "label"}  # a queue's one press on a row: its field set to the value
ADD_KEYS = {"label", "fields", "values"}  # a view's «add»: the fields its form asks for, and what they start as
LATEST_KEYS = {"collection", "field", "arrows"}  # a queue's newest record of another collection that names a row
ARROWS = ("in", "out")      # which way that record went: towards the owner, or from him
LABEL_MAX = 40              # characters of a button's label
WINDOWS = ("week", "month")
VIEWS_MAX = 20              # views of one collection
FIELDS_SHOWN = 32           # names in a view's `fields`, `folded` or `to`
TITLE_MAX = 80              # characters of a view's title
MARKS_MAX = 16              # names in a form's `goals`, or its `rot`
MARK_MAX = 1_000_000
KEY_MAX = 8                 # fields in a key, or in one unique
UNIQUE_MAX = 8              # uniques in one form
FILTER_MAX = 16             # conditions in one filter
IN_MAX = 200                # values of one `in`
ZONE_AHEAD_S = 14 * 3600    # the store has no zone: «today» is the owner's in any, from UTC-12 to UTC+14
ZONE_BEHIND_S = 12 * 3600
IDEM_MAX = 200              # characters of a call's idem
QUERY_ROWS = 200            # rows of one `query`…
QUERY_BYTES = 60 * 2**10    # …and bytes of them as JSON: what an agent's tool call can take back (25 000 tokens)
DELETED = "_deleted"        # in a change's before/after, the record's deleted state: no field's name starts with _
TEXT_MAX = 2000             # characters of a text field
RECORD_MAX = 16 * 2**10     # bytes of a record's fields, as JSON
FORM_MAX = 64 * 2**10       # bytes of a form, as JSON
PROJECT_MAX = 2 * 2**20     # bytes of a project's description, its own views' pages in it
OPS_MAX = 200               # operations in one apply
ENUM_MAX = 200              # values of one enum
INT_MAX = 2**53 - 1         # what a JSON number keeps exactly
HISTORY = 50                # the newest changes `history` returns
IMPORT_MAX = 100_000        # lines of one import
IMPORT_CHUNK = 500          # lines of an import in one transaction: between them the platform's own writes get in
FAILURES_SHOWN = 20         # records a form's trial run names, of those that do not fit it
RIGHTS_KEYS = {"rights", "writers", "set_by", "sensitive", "ats_senders"}  # rights' own words, never in a form
RIGHTS_ELSEWHERE = ("rights are the owner's alone, set from the server's terminal with `retinue-platform collections "
                    "rights <collection> <file>` — never with a form")
GROWS = ("a form only grows for now: removing or renaming a field, changing its type or `to`, and taking a value out "
         "of an enum wait for their first real case (⏸)")
RECORD_COLUMNS = "id, data, version, created, updated, deleted"
CHANGE_COLUMNS = ('change_id, ts, as_of, batch, collection, id, op, before, after, actor, model, via, src, "foreign", '
                  "reverts, idem")
SCHEMA_COLUMNS = "collection, space, form, rights, views"
EVENT_COLUMNS = "id, source, project, name, collection, record, depth, at, outcome"
IDS_CHUNK = 500             # ids in one statement's IN (…)
UPDATED = "_updated"        # `Reader.records` by when a record last changed: no field's name starts with _
# The machines' changes — the assistant's, the rules', the projects' reactions (`events.py`): what the evening list names.
MACHINES = "(actor = 'assistant' OR actor LIKE 'code:%' OR actor LIKE 'project:%')"


class Refused(Exception):
    """The file was locked past `BUSY_S`, or the store is closing: nothing was written; the same call may simply be
    made again."""


class Full(Exception):
    """The file would leave less than `FREE_MIN` on the volume: nothing was written."""


class _Stopped(Exception):
    """An import's batch, no longer `applying` at its next chunk: taken back from another terminal while it ran."""


class _Undo(Exception):
    """A trial run's answer, raised out of its transaction so that nothing of it is kept."""

    def __init__(self, answer: dict) -> None:
        super().__init__()
        self.answer = answer


class Rejected(Exception):
    """A write the store will not make as asked — a field the form does not have, a value of the wrong type, a version
    that is not the record's — with the reason in words a writer (a model included) can correct its call by. Nothing
    of the call was written. `record` is the record as it is now and `change` its last change, when they help: a
    version that did not match comes with both. `kind` names the refusals a screen answers with its own question:
    `stale` (a version not the record's: the record moved on), `after` (a revert of a change something changed after),
    `duplicate` (the form's key matches another record), `taken` (a unique value another holds); and `propose` the
    writes the rights leave to the owner's press — a delete they give "propose", a value `set_by` keeps for others —
    which an agent's tool puts to him as a proposal (`colltools`)."""

    def __init__(self, reason: str, record: dict | None = None, change: dict | None = None,
                 kind: str | None = None) -> None:
        super().__init__(reason)
        self.reason, self.record, self.change, self.kind = reason, record, change, kind


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


def plain(text: object, most: int) -> bool:
    return (isinstance(text, str) and 0 < len(text) <= most
            and not any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for c in text))


def _utc_day(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).date().isoformat()


def _date(value: object) -> bool:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _instant(value: object) -> str | None:
    """A moment in UTC as it is kept — `YYYY-MM-DDTHH:MM:SSZ`, seconds always written, so that the kept strings sort as
    the moments do — or None when it is not one."""
    if not isinstance(value, str) or not INSTANT.fullmatch(value):
        return None
    try:
        moment = datetime.datetime.strptime(value[:-1] if len(value) == 20 else value[:-1] + ":00", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def money(value: object) -> str | None:
    """An amount as it is kept: a decimal string with two places (`"12.50"`), from a string or a JSON number of at
    most two places — so a sum of them is exact in cents — or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        value = repr(value)
    text = str(value)
    if not MONEY.fullmatch(text):
        return None
    amount = decimal.Decimal(text)
    return f"{amount:.2f}" if abs(amount) < MONEY_MAX else None


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Ulids:
    """ULIDs (a 48-bit millisecond time and 80 random bits, in Crockford's base 32) that only grow: one in the same
    millisecond as the last, or under a clock that stepped back, is the last plus one. Seeded with the file's newest
    id at its opening, so a restart does not go back either — the Router's cursor and the evening list name changes
    by these ids, and an id that went back would be skipped."""

    def __init__(self, last: str | None = None) -> None:
        self.last = 0
        self.see(last)

    def see(self, last: str | None) -> None:
        """An id made elsewhere — by another process writing the same file — that the next must be above."""
        if last:
            self.last = max(self.last, sum(CROCKFORD.index(c) << 5 * (25 - n) for n, c in enumerate(last)))

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
    if set(form) & RIGHTS_KEYS:
        raise Rejected(RIGHTS_ELSEWHERE)
    if unknown := set(form) - {"fields", "key", "unique", "goals", "rot"}:
        raise Rejected(f"a form takes only \"fields\", \"key\", \"unique\", \"goals\" and \"rot\", not "
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
        if set(spec) & RIGHTS_KEYS:
            raise Rejected(RIGHTS_ELSEWHERE)
        if unknown := set(spec) - {"type", "required", "index", "hide", *extra}:
            raise Rejected(f"field {name!r}: a {spec['type']} takes no {', '.join(sorted(map(repr, unknown)))}")
        for flag in ("required", "index", "hide"):
            if not isinstance(spec.get(flag, False), bool):
                raise Rejected(f"field {name!r}: \"{flag}\" is true or false")
        if spec["type"] == "enum":
            values = spec.get("values")
            if (not isinstance(values, list) or not 0 < len(values) <= ENUM_MAX or len(set(values)) != len(values)
                    or not all(isinstance(v, str) and ENUM_VALUE.fullmatch(v) for v in values)):
                raise Rejected(f"field {name!r}: \"values\" is a list of 1 to {ENUM_MAX} distinct values, each "
                               "lowercase letters, digits and _, starting with a letter")
            labels = spec.get("labels", {})
            if (not isinstance(labels, dict) or not set(labels) <= set(values)
                    or not all(plain(v, LABEL_WORDS_MAX) for v in labels.values())):
                raise Rejected(f"field {name!r}: \"labels\" gives some of its values words, plain text of at most "
                               f"{LABEL_WORDS_MAX} characters: {{\"pack_ready\": \"Ready to send\"}}")
        if spec["type"] == "ref" and not (isinstance(spec.get("to"), str) and NAME.fullmatch(spec["to"])):
            raise Rejected(f"field {name!r}: \"to\" names the collection its records are in")
        if spec["type"] == "money" and not (isinstance(spec.get("currency"), str)
                                            and CURRENCY.fullmatch(spec["currency"])):
            raise Rejected(f"field {name!r}: \"currency\" is its one currency, three capital letters: \"EUR\"")
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
    for part, least, what in (("goals", 1, "a goal, a whole number of at least 1"),
                              ("rot", 0, "a threshold, a whole number of days")):
        if part in form:
            given = form[part]
            if (not isinstance(given, dict) or not 0 < len(given) <= MARKS_MAX
                    or not all(isinstance(k, str) and NAME.fullmatch(k) for k in given)
                    or not all(type(v) is int and least <= v <= MARK_MAX for v in given.values())):
                raise Rejected(f"\"{part}\" is an object of 1 to {MARKS_MAX} names, each with {what} "
                               f"(at most {MARK_MAX}): {{\"weekly\": 5}}")
            form_kept[part] = dict(given)
    return form_kept


def parse_views(views: Any) -> list[dict]:
    """A collection's views (docs/collections.md#views), checked strictly in their shape: a closed set of kinds and
    keys, any other stops it. The fields they name are not held to the form here — a view whose field the form lacks
    shows «field not found» instead of its rows (`views.py`). Returns them as kept. Raises Rejected with the reason."""
    if not isinstance(views, list) or len(views) > VIEWS_MAX:
        raise Rejected(f"views are a list of up to {VIEWS_MAX}: [{{\"name\": \"board\", \"kind\": \"board\", "
                       "\"group\": \"stage\"}]")
    if len(_json(views).encode()) > FORM_MAX:
        raise Rejected(f"views are at most {FORM_MAX // 1024} KB")
    kept, names = [], set()
    for n, view in enumerate(views):
        kind = view.get("kind") if isinstance(view, dict) else None
        if kind not in VIEW_KEYS:
            raise Rejected(f"views[{n}]: \"kind\" is one of {', '.join(VIEW_KEYS)}")
        where = f"views[{n}] ({kind})"
        if unknown := set(view) - VIEW_KEYS[kind]:
            raise Rejected(f"{where} takes {', '.join(sorted(VIEW_KEYS[kind]))}; not "
                           f"{', '.join(sorted(map(repr, unknown)))}")
        name = view.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise Rejected(f"{where}: \"name\" is lowercase letters, digits and _, starting with a letter, and "
                           "another view's in the collection")
        names.add(name)
        if "title" in view and not plain(view["title"], TITLE_MAX):
            raise Rejected(f"{where}: \"title\" is plain text of at most {TITLE_MAX} characters")
        if "tab" in view and not plain(view["tab"], TAB_MAX):
            raise Rejected(f"{where}: \"tab\" is plain text of at most {TAB_MAX} characters: the tab it shows in")
        if "mark" in view and view["mark"] not in MARKS:
            raise Rejected(f"{where}: \"mark\" is one of {', '.join(MARKS)}")
        for key, (least, most) in (("days", AGENDA_DAYS), ("weeks", CHART_WEEKS), ("months", MONTHS)):
            if key in view and not (type(view[key]) is int and least <= view[key] <= most):
                raise Rejected(f"{where}: \"{key}\" is a whole number from {least} to {most}")
        for key in ("group", "date", "field", "since", "sort", "goal", "rot", "sum"):
            if key in view and not (isinstance(view[key], str) and NAME.fullmatch(view[key])):
                raise Rejected(f"{where}: \"{key}\" names a field of the form")
        for key in ("fields", "folded", "to"):
            given = view.get(key, [])
            if (not isinstance(given, list) or len(given) > FIELDS_SHOWN or len(set(map(str, given))) != len(given)
                    or not all(isinstance(v, str) and NAME.fullmatch(v) for v in given)):
                raise Rejected(f"{where}: \"{key}\" is a list of up to {FIELDS_SHOWN} distinct names")
        if "filter" in view and not isinstance(view["filter"], (dict, list)):
            raise Rejected(f"{where}: \"filter\" is a condition or a list of them, as a query takes")
        if "done" in view:
            _press(view["done"], where)
        if "add" in view:
            _adding(view["add"], where)
        if "latest" in view:
            _latest_shape(view["latest"], where)
        if kind == "board" and "group" not in view:
            raise Rejected(f"{where}: \"group\" names the enum its columns are")
        if kind in ("agenda", "chart", "months", "year") and "date" not in view:
            raise Rejected(f"{where}: \"date\" names the date field it goes by")
        if kind == "stat":
            count, window = view.get("count", "records"), view.get("window")
            if count not in ("records", "changed"):
                raise Rejected(f"{where}: \"count\" is records or changed")
            if window not in (None, *WINDOWS):
                raise Rejected(f"{where}: \"window\" is one of {', '.join(WINDOWS)}, or none for all the time")
            if count == "changed" and "field" not in view:
                raise Rejected(f"{where}: a count of changed records names the \"field\" they changed")
            if count == "records" and window is not None and "date" not in view:
                raise Rejected(f"{where}: a count of records in a {window} names the \"date\" field it goes by")
            if count == "records" and ("field" in view or "to" in view):
                raise Rejected(f"{where}: \"field\" and \"to\" are a count of changed records'")
        kept.append(dict(view))
    return kept


def _scalar(value: Any) -> bool:
    return (isinstance(value, bool) or type(value) is int and abs(value) <= INT_MAX
            or isinstance(value, str) and plain(value, TEXT_MAX))


def _press(done: Any, where: str) -> None:
    """A queue's `done`: `{"field": "state", "value": "sent", "label": "Sent"}` — the button on each of its rows."""
    if (not isinstance(done, dict) or set(done) != DONE_KEYS or not isinstance(done["field"], str)
            or not NAME.fullmatch(done["field"]) or not _scalar(done["value"]) or not plain(done["label"], LABEL_MAX)):
        raise Rejected(f"{where}: \"done\" is {{\"field\": <a field>, \"value\": <what it becomes>, \"label\": <the "
                       f"button's words, at most {LABEL_MAX} characters>}}")


def _latest_shape(latest: Any, where: str) -> None:
    """A queue's `latest`: `{"collection": "touches", "field": "direction", "arrows": {"in": "in", "out": "out"}}` —
    on each row, the newest record of that collection whose ref names it: its day, its enum `field`, and the arrow
    each of the field's values is drawn as."""
    arrows = latest.get("arrows", {}) if isinstance(latest, dict) else None
    if (not isinstance(latest, dict) or {"collection", "field"} - set(latest) or set(latest) - LATEST_KEYS
            or not all(isinstance(latest[k], str) and NAME.fullmatch(latest[k]) for k in ("collection", "field"))
            or not isinstance(arrows, dict) or len(arrows) > MARKS_MAX
            or not all(isinstance(k, str) and NAME.fullmatch(k) and v in ARROWS for k, v in arrows.items())):
        raise Rejected(f"{where}: \"latest\" is {{\"collection\": <one whose ref names these records>, \"field\": "
                       f"<its enum>, \"arrows\": {{<value>: {' or '.join(ARROWS)}}}}}, the arrows optional")


def _adding(add: Any, where: str) -> None:
    """A view's `add`: `{"label": "Add a touch", "fields": [...], "values": {...}}` — the button that adds a record,
    the fields its sheet asks for (the form's, without them) and the values they start as."""
    shape = (f"{where}: \"add\" is {{\"label\": <the button's words, at most {LABEL_MAX} characters>, \"fields\": "
             "[<fields it asks for>], \"values\": {<field>: <what it starts as>}}, the last two optional")
    if not isinstance(add, dict) or "label" not in add or set(add) - ADD_KEYS or not plain(add["label"], LABEL_MAX):
        raise Rejected(shape)
    fields, values = add.get("fields", []), add.get("values", {})
    if (not isinstance(fields, list) or len(fields) > FIELDS_SHOWN or len(set(map(str, fields))) != len(fields)
            or not all(isinstance(v, str) and NAME.fullmatch(v) for v in fields)):
        raise Rejected(shape)
    if (not isinstance(values, dict) or len(values) > FIELDS_SHOWN
            or not all(NAME.fullmatch(k) and _scalar(v) for k, v in values.items())):
        raise Rejected(shape)


def _names(names: Any, fields: dict, what: str, most: int = KEY_MAX) -> list[str]:
    if (not isinstance(names, list) or not 0 < len(names) <= most or not all(isinstance(n, str) for n in names)
            or len(set(names)) != len(names)):
        raise Rejected(f"{what} is a list of 1 to {most} distinct fields of the form")
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
            raise Rejected(f"writers: {cls!r} is not a writer's class: assistant, session, mac, code:<rule> or "
                           "project:<name> (the owner may do anything, and is not listed)")
        if not isinstance(grant, dict) or (unknown := set(grant) - {"add", "set", "delete"}):
            raise Rejected(f"writers.{cls} takes \"add\", \"set\" and \"delete\"")
        add, open_, delete = grant.get("add", False), grant.get("set", []), grant.get("delete", False)
        if not isinstance(add, bool):
            raise Rejected(f"writers.{cls}.add is true or false")
        if open_ != "*":
            # any of the form's fields, each once: a writer's fields are not a key's, and need not fit in KEY_MAX
            open_ = _names(open_, fields, f"writers.{cls}.set", len(fields)) if open_ != [] else []
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
                               "code:<rule>, project:<name>")
        kept["set_by"][name] = {value: list(who) for value, who in values.items()}
    return kept


def _grows(old: dict, new: dict) -> None:
    """Rejected unless `new` is `old` grown: every field still there, of the same type (a ref to the same
    collection), every value of an enum still one. What else changes — a field added, a value added, `required`,
    `index`, `key`, `unique` — the trial run over the records settles."""
    for name, spec in old["fields"].items():
        now = new["fields"].get(name)
        if now is None:
            raise Rejected(f"field {name!r} would be gone: {GROWS}")
        if (now["type"] != spec["type"] or now.get("to") != spec.get("to")
                or now.get("currency") != spec.get("currency")):
            raise Rejected(f"field {name!r} would become another type: {GROWS}")
        if lost := [v for v in spec.get("values", ()) if v not in now["values"]]:
            raise Rejected(f"field {name!r} would lose {', '.join(map(repr, lost))}: {GROWS}")


def _path(field: str) -> str:
    """A field's value in a record, as SQL: the one place a name goes into a statement, so an index and the query
    meant to use it say it the same — and only a name the form declared, which is letters, digits and _."""
    if not NAME.fullmatch(field):
        raise ValueError(field)
    return f"json_extract(data, '$.{field}')"


def _within(collection: str) -> str:
    """The records of a collection that are not deleted, as SQL: a partial index's WHERE, and every query's, so the
    planner sees that one implies the other."""
    if not NAME.fullmatch(collection):
        raise ValueError(collection)
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
    """The class a writer's rights and its idems go by: a `code:` rule and a project's reactions (`project:<name>`,
    `events.py`) their own, every other actor its kind (`session:<id>` is a session)."""
    return actor if actor.startswith(("code:", "project:")) else actor.split(":")[0]


def idem_scope(actor: str, derived: bool = False) -> str:
    """Whose idems a writer's are. A call's (`idem`) are a session's own actor's — another session's `add-1` is
    another call —, a Mac's its device's (`mac:<device>`: Claude Code makes a call again on a new connection, which is
    another MCP session of the same device), and its class's for the rest. One the store derives from a mail is its
    class's, so the assistant reading a mail again finds what it made the first time, whichever run it is. An
    import's, of both kinds, are its own batch's: a failed import, taken back, leaves none behind that a corrected one
    would skip."""
    cls = writer_class(actor)
    if cls == "mac" and not derived:
        return actor.split("/")[0]
    return actor if cls == "import" or (not derived and cls == "session") else cls


def json_digest(value: Any) -> str:
    """A JSON value's SHA-256, its keys sorted: the same for the same value however its keys were ordered."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode()).hexdigest()


def _identity(form: dict, fields: dict, distinct: bool) -> str:
    """What an `add` from a mail is, for the idem the store derives: its key's values, compared as `_keys` compares
    them (a text by its letters, case and the spaces around it aside) — so the mail read again finds the record it
    added whatever else it adds beside it, and in whatever order. Without a key, or given as `distinct`: all its
    fields."""
    key = form.get("key")
    if key and not distinct and all(k in fields for k in key):
        return "key:" + json_digest([fields[k].strip(" ").lower() if form["fields"][k]["type"] == "text" else fields[k]
                                     for k in key])
    return "fields:" + json_digest(fields)


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
        self._seen = 0  # the file's data_version this connection last looked at (`_fresh`)
        self._closed = False
        # Told, on the loop, of every write of this store that changed a collection: its name, its Space and its
        # newest change_id (`PlatformStore.collection_event`, for the open apps). `moved` finds those another process
        # wrote.
        self.changed: Callable[[str, str | None, str | None], None] | None = None
        # Told, on the loop, of every `apply` that changed records: its writer, the collection and the changes it made
        # — what a project's events are fired by (`events.Events`). An import, a define and a batch's taking back
        # fire none.
        self.applied: Callable[[Writer, str, list[str]], None] | None = None
        self._spaces: dict[str, str] = {}  # each collection's Space, as this process last read or wrote it
        self._watched: tuple[int, dict[str, str | None]] | None = None  # `moved`: the data_version, the revs

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
        self._ulids = Ulids(_newest(db))
        self._spaces = dict(db.execute("SELECT collection, space FROM schemas").fetchall())
        self._seen = db.execute("PRAGMA data_version").fetchone()[0]
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
            self._fresh(db)
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def _fresh(self, db: sqlite3.Connection) -> None:
        """What another connection committed since this one last wrote — the server's terminal commands write the
        file from a process of their own — taken in, under the lock that keeps anything else from committing now: the
        ids go on from the file's newest, so none of ours sorts before one of theirs. Forms and rights need nothing:
        every call reads them from the file."""
        seen = db.execute("PRAGMA data_version").fetchone()[0]
        if seen != self._seen:
            self._seen = seen
            self._ulids.see(_newest(db))

    def _close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    async def open(self) -> None:
        """The file and the folder made, if they are not there yet."""
        await self._run(lambda: None)

    def _tell(self, collection: str, rev: str | None) -> None:
        if self.changed is not None:
            self.changed(collection, self._spaces.get(collection), rev)

    async def moved(self) -> list[tuple[str, str, str | None]]:
        """The collections another process (the server's terminal) changed since the last call, with their Space and
        newest change_id: `[(name, space, rev)]`. The first call only takes note. Cheap when nothing happened: SQLite's
        `data_version` moves only with another connection's commit. Raises Refused."""
        return await self._run(self._moved)

    def _moved(self) -> list[tuple[str, str, str | None]]:
        version = self._db.execute("PRAGMA data_version").fetchone()[0]
        if self._watched is not None and self._watched[0] == version:
            return []
        self._spaces = dict(self._db.execute("SELECT collection, space FROM schemas").fetchall())
        revs = {name: self._rev(name) for name in self._spaces}
        last, self._watched = self._watched, (version, revs)
        return [] if last is None else [(name, self._spaces[name], rev) for name, rev in sorted(revs.items())
                                        if last[1].get(name) != rev]

    async def read(self, work: Callable[[Reader], T]) -> T:
        """`work(reader)` on the thread, in one read transaction, so all it reads is of one moment — a view and the
        rev it is of (`views.py`), the changes the evening list names (`colltools.py`). Raises Refused."""
        return await self._run(self._read, lambda: work(Reader(self)))

    def _read(self, work: Callable[[], T]) -> T:
        self._db.execute("BEGIN")
        try:
            return work()
        finally:
            self._db.execute("COMMIT")

    async def write(self, work: Callable[[Logging], T]) -> T:
        """`work(logging)` on the thread, in one transaction taken for writing: kept whole, or not at all — the
        events' log beside the records (`events.py`), with what it reads to decide. Raises Refused."""
        return await self._run(self._write_with, work)

    def _write_with(self, work: Callable[[Logging], T]) -> T:
        with self._write():
            return work(Logging(self))

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

    async def define(self, writer: Writer, collection: str, space: str, form: Any, now: float,
                     dry: bool = False, views: Any = None) -> dict:
        """A collection's form, new or grown from the one it had (`_grows`), and its views (`parse_views`; None keeps
        the ones it has), as a `define` change, after a trial run over its records: `{name, rev, form_rev, before,
        after, views_before, views_after, space, unchanged, records, failures, failed}` — `rev`
        its newest change_id, `form_rev` the form's revision it becomes, `before` the form it had (None for a new
        collection), `records` how many it checked and `failures` the first FAILURES_SHOWN that do not fit (`{id,
        reason}`), of `failed`. `dry` keeps nothing: what the server's terminal shows before it asks. A new
        collection gets the default rights (DEFAULT_RIGHTS). The writer is the owner, or `define:<rev>` naming the
        revision the form becomes — so a form changed since its trial run is not applied over. Raises Rejected
        (records that do not fit included), Refused."""
        answer = await self._run(self._define, writer, collection, space, form, now, dry, views)
        if not dry and not answer["unchanged"]:
            self._spaces[collection] = answer["space"]
            self._tell(collection, answer["rev"])
        return answer

    def _define(self, writer: Writer, collection: str, space: str, form: Any, now: float, dry: bool,
                views: Any = None) -> dict:
        check_writer(writer)
        if writer_class(writer.actor) not in ("owner", "define"):
            raise Rejected("a form is the owner's to apply, from the server's terminal")
        if not NAME.fullmatch(collection):
            raise Rejected(f"a collection's name is lowercase letters, digits and _, starting with a letter: not "
                           f"{collection!r}")
        if not isinstance(space, str) or not SPACE.fullmatch(space):
            raise Rejected(f"not a Space's name: {space!r}")
        form = parse_form(form)
        views = parse_views(views) if views is not None else None
        try:
            with self._write() as db:
                for name, spec in form["fields"].items():
                    if spec["type"] == "ref" and spec["to"] != collection and self._form(spec["to"]) is None:
                        raise Rejected(f"field {name!r}: there is no collection {spec['to']!r}")
                old = db.execute("SELECT form, rev, space, views FROM schemas WHERE collection = ?",
                                 (collection,)).fetchone()
                before = json.loads(old[0]) if old else None
                views_before = json.loads(old[3]) if old else []
                views = views_before if views is None else views
                if before is not None:
                    _grows(before, form)
                rev = old[1] + 1 if old else 1
                if writer_class(writer.actor) == "define" and writer.actor != f"define:{rev}":
                    raise Rejected(f"the form of {collection} is at revision {rev - 1}, not the one this was tried "
                                   "on: try it again")
                answer = {"name": collection, "rev": self._rev(collection), "form_rev": rev, "before": before,
                          "after": form, "views_before": views_before, "views_after": views, "space": space,
                          "unchanged": before == form and old[2] == space and views_before == views}
                if answer["unchanged"]:  # already so: nothing is written, the revision stays
                    answer.update(form_rev=old[1], records=self._count(collection, None, None), failures=[],
                                  failed=0)
                    return answer
                db.execute("INSERT INTO schemas (collection, space, form, rights, views, rev, updated) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (collection) DO UPDATE SET space = excluded.space, "
                           "form = excluded.form, views = excluded.views, rev = excluded.rev, "
                           "updated = excluded.updated",
                           (collection, space, _json(form), _json(DEFAULT_RIGHTS), _json(views), rev, now))
                records, failures = self._trial(collection, form)
                try:
                    self._index(collection, form)
                except Rejected as exc:
                    failures.append({"id": None, "reason": exc.reason})
                answer.update(records=records, failures=failures[:FAILURES_SHOWN], failed=len(failures))
                if dry:
                    raise _Undo(answer)
                if failures:
                    shown = "; ".join(f"{f['id']}: {f['reason']}" if f["id"] else f["reason"] for f in failures[:3])
                    raise Rejected(f"{len(failures)} of {records} records do not fit the form: {shown}")
                moved, shown = old is not None and old[2] != space, views != views_before
                self._change(writer, now, collection, None, "define",
                             {"form": before, **({"space": old[2]} if moved else {}),
                              **({"views": views_before} if shown else {})} if old else None,
                             {"form": form, **({"space": space} if moved else {}),
                              **({"views": views} if shown else {})})
                answer["rev"] = self._rev(collection)
                return answer
        except _Undo as undo:
            return undo.answer

    def _trial(self, collection: str, form: dict) -> tuple[int, list[dict]]:
        """The collection's records, not deleted, tried against a form: how many, and those that do not fit it —
        all that a form's growing can break is a field now required that a record does not have. A unique the
        records break `_index` finds."""
        required = [name for name, spec in form["fields"].items() if spec.get("required")]
        records, failures = 0, []
        for rid, data in self._db.execute(f"SELECT id, data FROM records WHERE {_within(collection)} ORDER BY id"):
            records += 1
            data = json.loads(data)
            if missing := [name for name in required if name not in data]:
                failures.append({"id": rid, "reason": f"{', '.join(map(repr, missing))} "
                                                      f"{'is' if len(missing) == 1 else 'are'} required"})
        return records, failures

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

    async def rights(self, writer: Writer, collection: str, rights: Any, now: float, dry: bool = False) -> dict:
        """A collection's rights (`parse_rights`) in place of the ones it had, as a `define` change: `{name, rev,
        before, after, unchanged}`; `dry` keeps nothing. The owner's alone — no agent's path reaches here
        (docs/collections.md#rights). Raises Rejected, Refused."""
        answer = await self._run(self._rights, writer, collection, rights, now, dry)
        if not dry and not answer["unchanged"]:
            self._tell(collection, answer["rev"])
        return answer

    def _rights(self, writer: Writer, collection: str, rights: Any, now: float, dry: bool) -> dict:
        check_writer(writer)
        if writer.actor != "owner":
            raise Rejected("rights are the owner's alone, set from the server's terminal")
        with self._write() as db:
            row = db.execute("SELECT form, rights FROM schemas WHERE collection = ?", (collection,)).fetchone()
            if row is None:
                raise Rejected(f"there is no collection {collection!r}")
            old, new = json.loads(row[1]), parse_rights(rights, json.loads(row[0]))
            if new != old and not dry:
                db.execute("UPDATE schemas SET rights = ?, updated = ? WHERE collection = ?",
                           (_json(new), now, collection))
                self._change(writer, now, collection, None, "define", {"rights": old}, {"rights": new})
            return {"name": collection, "rev": self._rev(collection), "before": old, "after": new,
                    "unchanged": new == old}

    async def start_batch(self, kind: str, writer: Writer, now: float) -> str:
        """A batch, `applying`, for changes that go together (an import, a form's application): its id, to pass to
        `apply`. Raises Rejected, Refused."""
        return await self._run(self._start_batch, kind, writer, now)

    def _start_batch(self, kind: str, writer: Writer, now: float) -> str:
        check_writer(writer)
        with self._write() as db:
            batch = self._ulids.next(now)
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
                    idem: str | None = None, dry: bool = False) -> dict:
        """The one way a record is written: up to OPS_MAX operations on one collection, checked against its form and
        its rights and kept whole in one transaction, or none of them (docs/collections.md#records). Each is `add`,
        `set`, `delete`, `restore` or `revert`. Returns `{name, rev, change_ids, records}`: the collection, its newest
        change_id (what a screen compares to know it is behind), and for each operation the change it made (None when
        it changed nothing: the record was already so) and the record after it. `idem` names the call: made again by
        the same writer (`idem_scope`) with the same operations, it changes nothing and answers the first time's
        change_ids, with the records as they are now; with other operations it is Rejected. The writers of the
        server's terminal (`import:`, `define:`) write through `import_ops` and `define` only. `dry` tries the call
        and keeps nothing: the answer then has `changes` too, each operation's change as it would be (None where it
        would change nothing) — what a proposal shows the owner before his press. Raises Rejected, Refused."""
        answer = await self._run(self._apply, writer, collection, ops, now, batch, idem, dry)
        made = [] if dry or batch is not None else [c for c in answer["change_ids"] if c]
        if not dry and any(answer["change_ids"]):
            self._tell(collection, answer["rev"])
        if made and self.applied is not None:  # a call made again answers its first changes: their events are known
            self.applied(writer, collection, made)
        return answer

    def _apply(self, writer: Writer, collection: str, ops: Any, now: float, batch: str | None,
               idem: str | None, dry: bool = False) -> dict:
        check_writer(writer)
        if idem is not None and not plain(idem, IDEM_MAX):
            raise Rejected(f"idem is the call's own name, 1 to {IDEM_MAX} plain characters")
        if not isinstance(ops, list) or not 0 < len(ops) <= OPS_MAX:
            raise Rejected(f"ops is a list of 1 to {OPS_MAX} operations")
        if writer_class(writer.actor) in ("import", "define"):
            raise Rejected("an import: or define: writer writes through import_ops or define, not apply")
        if dry and (idem is not None or batch is not None):
            raise Rejected("a trial is of a call alone: no idem, no batch")
        scope, digest = idem_scope(writer.actor), json_digest(ops)
        try:
            return self._applying(writer, collection, ops, now, batch, idem, dry, scope, digest)
        except _Undo as undo:
            return undo.answer

    def _applying(self, writer: Writer, collection: str, ops: list, now: float, batch: str | None,
                  idem: str | None, dry: bool, scope: str, digest: str) -> dict:
        with self._write() as db:
            schema = self._schema(collection)
            if schema is None:
                raise Rejected(f"there is no collection {collection!r}")
            if idem is not None and (first := self._idem(scope, collection, f"call:{idem}")) is not None:
                if first["ops"] != digest:
                    raise Rejected(f"idem {idem!r} was used for other operations: give this call an idem of its own")
                return {"name": collection, "rev": self._rev(collection), "change_ids": first["change_ids"],
                        "records": [self._record(collection, rid) for rid in first["ids"]]}
            if batch is not None and db.execute("SELECT 1 FROM batches WHERE id = ? AND state = 'applying'",
                                                (batch,)).fetchone() is None:
                raise Rejected(f"no batch {batch!r} is applying")
            change_ids, records = [], []
            for n, op in enumerate(ops):
                try:
                    record, change = self._op(writer, collection, schema, op, now, batch, idem)
                except Rejected as exc:
                    raise Rejected(f"ops[{n}]: {exc.reason}", exc.record, exc.change, exc.kind) from None
                change_ids.append(change)
                records.append(record)
            if dry:  # rolled back by `_write`: nothing of it is kept, the ids it took aside
                raise _Undo({"name": collection, "rev": None, "change_ids": [None] * len(ops), "records": records,
                             "changes": [self._change_of(cid) if cid else None for cid in change_ids]})
            if idem is not None:
                self._remember(scope, collection, f"call:{idem}",
                               {"ops": digest, "change_ids": change_ids, "ids": [r["id"] for r in records]}, now)
            return {"name": collection, "rev": self._rev(collection), "change_ids": change_ids, "records": records}

    def _op(self, writer: Writer, collection: str, schema: tuple[dict, dict], op: Any, now: float,
            batch: str | None, idem: str | None) -> tuple[dict, str | None]:
        form, rights = schema
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in OP_KEYS:
            raise Rejected(f"an operation is an object whose \"op\" is one of {', '.join(OP_KEYS)}")
        if unknown := set(op) - OP_KEYS[kind]:
            raise Rejected(f"{kind} takes {', '.join(sorted(OP_KEYS[kind]))}; not "
                           f"{', '.join(sorted(map(repr, unknown)))}")
        as_of, src, distinct = op.get("as_of"), op.get("src"), op.get("distinct", False)
        if as_of is not None and not _date(as_of):
            raise Rejected(f"as_of is the date the world became so, YYYY-MM-DD: not {as_of!r}")
        if src is not None and not (SRC.fullmatch(src) if plain(src, 1100) else False):
            raise Rejected("src is mail:<account>/<msgid>, tg:<chat>/<msg>, owner_words:<event>, "
                           "cal:<account>/<event>, file:<sha256>#row=<n>, record:<id> or web:<url>")
        if not isinstance(distinct, bool):
            raise Rejected("distinct is true or false: true when the record is another than the one it matches")
        cls, scope = writer_class(writer.actor), idem_scope(writer.actor, derived=True)
        if as_of is not None and cls not in OWNERS and as_of > _utc_day(now + ZONE_AHEAD_S):
            raise Rejected(f"as_of {as_of} has not come yet: the date the world became so is today or before")
        mail = src if src is not None and src.startswith("mail:") else None  # its idems the store derives
        stamp = {"writer": writer, "now": now, "collection": collection, "as_of": as_of, "src": src, "batch": batch,
                 "idem": idem}
        if kind == "add":
            self._may(collection, rights, cls, "add", op.get("fields"))
            fields = self._fields(collection, form, op.get("fields"), {})
            if mail is not None:
                derived = f"{mail}#add/{_identity(form, fields, distinct)}"
                if (first := self._idem(scope, collection, derived)) is not None:
                    return self._record(collection, first["id"]), None
            rid = self._ulids.next(now)
            self._keys(collection, form, fields, rid, None, not distinct)
            self._db.execute(f"INSERT INTO records (collection, {RECORD_COLUMNS}) VALUES (?, ?, ?, 1, ?, ?, NULL)",
                             (collection, rid, _json(fields), now, now))
            change = self._change(**stamp, rid=rid, op="add", before=None, after=fields)
            if mail is not None:
                self._remember(scope, collection, derived, {"change_id": change, "id": rid}, now)
            return self._record(collection, rid), change
        if kind == "revert":
            return self._revert(stamp, schema, op)
        self._may(collection, rights, cls, kind, op.get("fields"))
        record = self._at(collection, op)
        if as_of is not None and cls not in OWNERS and as_of < _utc_day(record["created"] - ZONE_BEHIND_S):
            # A change dated before its record was: a week's numbers count changes by `as_of`, and one a writer
            # moves back would rewrite a week gone by. An add may be dated back — the honest way to record late.
            raise Rejected(f"as_of {as_of} is before {record['id']} was made: a change is of its record's lifetime",
                           record)
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
            for done in [k for k in before if self._idem(scope, collection, f"{mail}#{record['id']}/{k}") is not None]:
                del before[done], after[done]
        if not before:  # already so: nothing is written, the version stays
            return record, None
        record, change = self._put(stamp, form, record, before, after, kind, check_key=not distinct)
        if mail is not None:
            for k in before:
                self._remember(scope, collection, f"{mail}#{record['id']}/{k}", {"change_id": change}, now)
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
                raise Rejected(f"a {kind} by {cls} in {collection} is the owner's to make: propose it to him",
                               kind="propose")
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
                raise Rejected(f"{name!r} becomes {value!r} only by {by}: not by {cls}", kind="propose")

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
                    raise Rejected(f"taken: {row[0]} has {what}", self._record(collection, row[0]), kind="taken")
        key = form.get("key")
        if not check_key or not key or not (changed is None or changed & set(key)) or not all(k in data for k in key):
            return
        same = [f"lower(trim({_path(k)})) = lower(trim(?))" if form["fields"][k]["type"] == "text"
                else f"{_path(k)} = ?" for k in key]
        row = self._db.execute(f"SELECT id FROM records WHERE {_within(collection)} AND {' AND '.join(same)} "
                               "AND id != ? LIMIT 1", (*(data[k] for k in key), rid)).fetchone()
        if row is not None:
            raise Rejected(f"possible duplicate of {row[0]}: the same {', '.join(key)}. Change that record instead, "
                           "or give \"distinct\": true if this one is another", self._record(collection, row[0]),
                           kind="duplicate")

    def _revert(self, stamp: dict, schema: tuple[dict, dict], op: dict,
                aside: Callable[[dict], bool] | None = None) -> tuple[dict, str | None]:
        """A change taken back, as a change of its own: what it changed set back to what it was — unless something
        has changed it since, which is Rejected with who did; `aside` names the later changes that do not count (a
        batch's own, as a batch is taken back). Taking back an add deletes the record."""
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
        for change in (c for c in later if aside is None or not aside(c)):
            if touched is None or touched & set(change["after"] or {}):
                what = ", ".join(sorted("deleted" if k == DELETED else k
                                        for k in change["after"] if touched is None or k in touched))
                raise Rejected(f"changed after it: {what} by {change['actor']} ({change['op']}, "
                               f"{change['change_id']}) — revert that first, or set the value", record, change, "after")
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
        elif kind == "instant":
            kept = _instant(value)
            if kept is None:
                raise Rejected(f"{name!r} is a moment in UTC, YYYY-MM-DDTHH:MM:SSZ: not {value!r}")
            value = kept
        elif kind == "bool":
            if type(value) is not bool:
                raise Rejected(f"{name!r} is true or false: not {value!r}")
        elif kind == "money":
            kept = money(value)
            if kept is None:
                raise Rejected(f"{name!r} is an amount of {spec['currency']}, at most two places: \"12.50\" or 12.5, "
                               f"not {value!r}")
            value = kept
        elif kind == "file" and probe:
            if not isinstance(value, str) or not SHA256.fullmatch(value):
                raise Rejected(f"{name!r} is an uploaded file's sha256: not {value!r}")
        elif kind == "file":
            if not isinstance(value, str) or self._file(value) is None:
                raise Rejected(f"{name!r} is an uploaded file's sha256 (POST /api/c/_files): there is no such file "
                               f"as {value!r}")
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
                           "again, it may have changed since", record, last[0] if last else None, "stale")
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

    def _change_of(self, change_id: str) -> dict | None:
        row = self._db.execute(f"SELECT {CHANGE_COLUMNS} FROM changes WHERE change_id = ?", (change_id,)).fetchone()
        return _change(row) if row else None

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

    # --- the server's terminal -----------------------------------------------------------------------------------

    async def import_ops(self, collection: str, ops: Any, now: float, dry: bool = False) -> dict:
        """Operations, one for each line of a file the owner brought (`add` with its `as_of` and `src`, mostly), as
        one batch an `import:<batch>` writer makes: `{batch, name, rev, lines, added, changed, same}`. Kept by
        IMPORT_CHUNK lines a transaction, so the platform's own writes get in between; a line refused takes back the
        lines before it with the batch (`revert_batch`), so the file is kept whole or not at all. `dry` tries every
        line in one transaction and keeps nothing: what the terminal shows before it asks. Raises Rejected (naming the
        line), Refused."""
        if not isinstance(ops, list) or not 0 < len(ops) <= IMPORT_MAX:
            raise Rejected(f"an import is 1 to {IMPORT_MAX} lines")
        if dry:
            return await self._run(self._import_dry, collection, ops, now)
        owner = Writer("owner", "import")
        batch = await self.start_batch("import", owner, now)
        counts = {"lines": len(ops), "added": 0, "changed": 0, "same": 0}
        try:
            for start in range(0, len(ops), IMPORT_CHUNK):
                await self._run(self._import_chunk, collection, ops[start:start + IMPORT_CHUNK], start, now, batch,
                                counts)
        except _Stopped as exc:  # taken back from another terminal while it ran: nothing is left to take back
            raise Rejected(f"{exc}: what it had kept is as that revert-batch left it") from None
        except (Rejected, Refused) as exc:
            reason = exc.reason if isinstance(exc, Rejected) else str(exc)
            try:
                await self.revert_batch(owner, batch, now)
            except (Rejected, Refused) as undo:
                raise Rejected(f"{reason} — and the lines before it are still kept in batch {batch}: take them back "
                               f"with revert-batch {batch} ({undo})") from exc
            raise Rejected(f"{reason} — nothing of the file is kept") from exc
        await self.finish_batch(batch, now)
        rev = await self._run(self._rev, collection)
        if counts["added"] or counts["changed"]:
            self._tell(collection, rev)
        return {"batch": batch, "name": collection, "rev": rev, **counts}

    def _import_dry(self, collection: str, ops: list, now: float) -> dict:
        counts = {"lines": len(ops), "added": 0, "changed": 0, "same": 0}
        try:
            with self._write() as db:
                batch = self._ulids.next(now)
                db.execute("INSERT INTO batches (id, kind, state, actor, created, updated) "
                           "VALUES (?, 'import', 'applying', 'owner', ?, ?)", (batch, now, now))
                self._lines(collection, ops, 0, now, batch, counts)
                raise _Undo({"batch": None, "name": collection, "rev": self._rev(collection), **counts})
        except _Undo as undo:
            return undo.answer

    def _import_chunk(self, collection: str, ops: list, start: int, now: float, batch: str, counts: dict) -> None:
        with self._write() as db:
            if db.execute("SELECT 1 FROM batches WHERE id = ? AND state = 'applying'", (batch,)).fetchone() is None:
                raise _Stopped(f"line {start + 1}: batch {batch} was taken back while the import ran")
            self._lines(collection, ops, start, now, batch, counts)

    def _lines(self, collection: str, ops: list, start: int, now: float, batch: str, counts: dict) -> None:
        schema = self._schema(collection)
        if schema is None:
            raise Rejected(f"there is no collection {collection!r}")
        writer = Writer(f"import:{batch}", "import")
        for n, op in enumerate(ops, start):
            try:
                _, change = self._op(writer, collection, schema, op, now, batch, None)
            except Rejected as exc:
                raise Rejected(f"line {n + 1}: {exc.reason}", exc.record, exc.change, exc.kind) from None
            kind = "same" if change is None else "added" if op["op"] == "add" else "changed"
            counts[kind] += 1

    async def revert_batch(self, writer: Writer, batch: str, now: float, dry: bool = False) -> dict:
        """A batch taken back, the owner's from the server's terminal: each record it changed set back to what it
        was before it, as a `revert` batch of its own — unless something outside it has changed the record since,
        which is left as it is and named in `touched` (`{collection, id, by, op, change_id}`). Answers `{batch,
        undo, reverted, records, touched, state, revs}`: the revert batch (None when it changed nothing), the changes
        taken back, the records they were of, the batch's state — `reverted` once nothing of it is left to take back —
        and the newest change_id of each collection it was in.
        Made again after the touched records are dealt with, it takes back what it left. `dry` keeps nothing. Raises
        Rejected, Refused."""
        answer = await self._run(self._revert_batch, writer, batch, now, dry)
        if not dry and answer["reverted"]:
            for collection, rev in answer["revs"].items():
                self._tell(collection, rev)
        return answer

    def _revert_batch(self, writer: Writer, batch: str, now: float, dry: bool) -> dict:
        check_writer(writer)
        if writer.actor != "owner":
            raise Rejected("a batch is taken back by the owner, from the server's terminal")
        try:
            with self._write() as db:
                row = db.execute("SELECT state FROM batches WHERE id = ?",
                                 (batch,)).fetchone() if isinstance(batch, str) else None
                if row is None:
                    raise Rejected(f"no batch {batch!r}")
                if row[0] == "reverted":
                    raise Rejected(f"batch {batch} is taken back already")
                ids, groups = set(), {}
                for seq, *rest in db.execute(f"SELECT seq, {CHANGE_COLUMNS} FROM changes WHERE batch = ? "
                                             "ORDER BY seq", (batch,)):
                    change = _change(tuple(rest))
                    ids.add(change["change_id"])
                    groups.setdefault((change["collection"], change["id"]), []).append((seq, change))
                done = {cid for (cid,) in db.execute(
                    "SELECT reverts FROM changes WHERE reverts IN (SELECT change_id FROM changes WHERE batch = ?)",
                    (batch,))}
                undo = self._ulids.next(now)
                db.execute("INSERT INTO batches (id, kind, state, actor, created, updated) "
                           "VALUES (?, 'revert', 'applied', ?, ?, ?)", (undo, writer.actor, now, now))

                def aside(change: dict) -> bool:  # the batch's own changes, and what took them back
                    return change["batch"] in (batch, undo) or change["reverts"] in ids

                touched, reverted, records = [], 0, 0
                for (collection, rid), changes in groups.items():
                    left = [change for _, change in changes if change["change_id"] not in done]
                    if not left:
                        continue
                    outside = next((c for c in map(_change, db.execute(
                        f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection = ? AND id = ? AND seq > ? "
                        "ORDER BY seq", (collection, rid, changes[0][0]))) if not aside(c)), None)
                    if outside is not None:
                        touched.append({"collection": collection, "id": rid, "by": outside["actor"],
                                        "op": outside["op"], "change_id": outside["change_id"]})
                        continue
                    schema = self._schema(collection)
                    stamp = {"writer": writer, "now": now, "collection": collection, "as_of": None, "src": None,
                             "batch": undo, "idem": None}
                    for change in reversed(left):
                        version = self._record(collection, rid)["version"]
                        _, made = self._revert(stamp, schema, {"change_id": change["change_id"], "version": version},
                                               aside)
                        reverted += made is not None
                    records += 1
                if not reverted:
                    db.execute("DELETE FROM batches WHERE id = ?", (undo,))
                # a batch taken back takes no more changes: an import still running stops at its next chunk
                state = "reverted" if not touched else "applied" if row[0] == "applying" else row[0]
                db.execute("UPDATE batches SET state = ?, updated = ? WHERE id = ?", (state, now, batch))
                answer = {"batch": batch, "undo": undo if reverted else None, "reverted": reverted,
                          "records": records, "touched": touched, "state": state,
                          "revs": {collection: self._rev(collection) for collection, _ in groups}}
                if dry:
                    raise _Undo(answer)
                return answer
        except _Undo as undo_:
            return undo_.answer

    async def export(self, folder: str | Path) -> dict:
        """Everything, to a folder that is empty or not there yet (docs/collections.md#export): `schemas.json`, each
        collection's `records.jsonl` and `changes.jsonl` in a folder of its name, `batches.jsonl`, `files.jsonl` and
        the files themselves in `attachments/`. One moment's: the file is first copied whole by SQLite's backup, the
        only time the platform's writes wait, and everything is read from that copy. Refused before anything is
        written when it would leave less than FREE_MIN on the folder's volume. Answers how many of each. Raises
        Rejected, Refused."""
        return await self._run(self._export, Path(folder))

    def _export(self, folder: Path) -> dict:
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise Rejected(f"{folder} is not an empty folder")
        pages, page = (self._db.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "page_size"))
        need = 2 * pages * page + self._db.execute("SELECT coalesce(sum(size), 0) FROM files").fetchone()[0]
        volume = next(p for p in (folder, *folder.parents) if p.exists())
        if shutil.disk_usage(volume).free - need < FREE_MIN:
            raise Rejected(f"the export needs {need // 2**20} MB and would leave less than {FREE_MIN // 2**30} GB "
                           f"free on {volume}: nothing was written")
        folder.mkdir(parents=True, exist_ok=True)
        counts = {"collections": 0, "records": 0, "changes": 0, "files": 0}
        snapshot = folder / ".snapshot.sqlite"
        db = sqlite3.connect(snapshot)
        try:
            self._db.backup(db)
            schemas = [{"collection": c, "space": sp, "form": json.loads(f), "rights": json.loads(r),
                        "views": json.loads(v), "rev": rev, "updated": u} for c, sp, f, r, v, rev, u in db.execute(
                "SELECT collection, space, form, rights, views, rev, updated FROM schemas ORDER BY collection")]
            (folder / "schemas.json").write_text(json.dumps(schemas, ensure_ascii=False, indent=2) + "\n")
            for schema in schemas:
                name = schema["collection"]
                (folder / name).mkdir()
                counts["collections"] += 1
                counts["records"] += _jsonl(folder / name / "records.jsonl", map(_record, db.execute(
                    f"SELECT {RECORD_COLUMNS} FROM records WHERE collection = ? ORDER BY id", (name,))))
                counts["changes"] += _jsonl(folder / name / "changes.jsonl", map(_change, db.execute(
                    f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection = ? ORDER BY seq", (name,))))
            _jsonl(folder / "batches.jsonl", (dict(zip(("id", "kind", "state", "actor", "created", "updated"), row))
                                              for row in db.execute("SELECT id, kind, state, actor, created, updated "
                                                                    "FROM batches ORDER BY id")))
            files = [_row(row) for row in db.execute(f"SELECT {FILE_COLUMNS} FROM files ORDER BY created, sha256")]
            _jsonl(folder / "files.jsonl", files)
        finally:
            db.close()
            snapshot.unlink(missing_ok=True)
        (folder / BLOBS).mkdir()
        for file in files:  # a blob never changes
            if (self.blobs / file["sha256"]).is_file():
                shutil.copyfile(self.blobs / file["sha256"], folder / BLOBS / file["sha256"])
                counts["files"] += 1
        return counts

    # --- the Macs -----------------------------------------------------------------------------------------------------

    async def add_device(self, name: str, scope: str, now: float) -> str:
        """A Mac that reaches the collections at `/mcp/mac`: its token, shown once — the file keeps only its SHA-256.
        A name is given once, revoked or not: the changes it made keep naming that device. Raises Rejected, Refused."""
        if not isinstance(name, str) or not DEVICE.fullmatch(name):
            raise Rejected(f"a device's name is 1 to 40 of a-z, 0-9, - and _, starting with a letter or digit: not "
                           f"{name!r}")
        if scope not in SCOPES:
            raise Rejected(f"scope is read or write: not {scope!r}")
        token = secrets.token_urlsafe(32)
        return await self._run(self._add_device, name, scope, hashlib.sha256(token.encode()).hexdigest(), token, now)

    def _add_device(self, name: str, scope: str, sha: str, token: str, now: float) -> str:
        with self._write() as db:
            if db.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone():
                raise Rejected(f"there is a device {name!r} already, revoked or not: a name is given once, so its "
                               "changes keep naming one device — choose another")
            db.execute("INSERT INTO devices (name, token_sha256, scope, created) VALUES (?, ?, ?, ?)",
                       (name, sha, scope, now))
        return token

    async def revoke_device(self, name: str, now: float) -> bool:
        """The device's token opens nothing from now on: True, or False when it was revoked already. Raises Rejected
        for no such device, Refused."""
        return await self._run(self._revoke_device, name, now)

    def _revoke_device(self, name: str, now: float) -> bool:
        with self._write() as db:
            row = db.execute("SELECT revoked FROM devices WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise Rejected(f"there is no device {name!r}")
            if row[0] is not None:
                return False
            db.execute("UPDATE devices SET revoked = ? WHERE name = ?", (now, name))
            return True

    async def devices(self) -> list[dict]:
        """Every device: `{name, scope, created, revoked, written}`, `written` the moment of its last change, if any.
        Raises Refused."""
        return await self._run(self._read, lambda: [
            {"name": name, "scope": scope, "created": created, "revoked": revoked,
             "written": self._db.execute("SELECT max(ts) FROM changes WHERE substr(actor, 1, ?) = ?",
                                         (len(name) + 5, f"mac:{name}/")).fetchone()[0]}
            for name, scope, created, revoked in self._db.execute(
                "SELECT name, scope, created, revoked FROM devices ORDER BY created, name").fetchall()])

    async def device(self, token: str) -> dict | None:
        """The device a token is, unless revoked: `{name, scope, sha256}`, or None. Raises Refused."""
        sha = hashlib.sha256(token.encode()).hexdigest()
        row = await self._run(self._read, lambda: self._db.execute(
            "SELECT name, scope FROM devices WHERE token_sha256 = ? AND revoked IS NULL", (sha,)).fetchone())
        return {"name": row[0], "scope": row[1], "sha256": sha} if row else None

    async def revoked(self, name: str) -> bool:
        """Whether a device was taken back, or never was one. Raises Refused."""
        row = await self._run(self._read, lambda: self._db.execute(
            "SELECT revoked FROM devices WHERE name = ?", (name,)).fetchone())
        return row is None or row[0] is not None

    async def catalog(self) -> list[dict]:
        """Every collection: `{name, space, form_rev, records, deleted, rev}`. Raises Refused."""
        return await self._run(self._catalog)

    def _catalog(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT s.collection, s.space, s.rev, count(r.id) - count(r.deleted), count(r.deleted) FROM schemas s "
            "LEFT JOIN records r ON r.collection = s.collection GROUP BY s.collection ORDER BY s.collection")
        return [{"name": name, "space": space, "form_rev": rev, "records": records, "deleted": deleted,
                 "rev": self._rev(name)} for name, space, rev, records, deleted in rows.fetchall()]

    async def schema(self, collection: str) -> dict | None:
        """One collection as the terminal shows it: `{name, space, form, rights, views, form_rev, records, deleted,
        rev, batches}`, `batches` the newest ten that changed it (`{id, kind, state, actor, created, changes}`), or
        None. Raises Refused."""
        return await self._run(self._schema_shown, collection)

    def _schema_shown(self, collection: str) -> dict | None:
        row = self._db.execute("SELECT space, form, rights, views, rev FROM schemas WHERE collection = ?",
                               (collection,)).fetchone()
        if row is None:
            return None
        (shown,) = [c for c in self._catalog() if c["name"] == collection]
        batches = self._db.execute(
            "SELECT b.id, b.kind, b.state, b.actor, b.created, count(*) FROM batches b JOIN changes c ON c.batch = b.id "
            "WHERE c.collection = ? GROUP BY b.id ORDER BY b.id DESC LIMIT 10", (collection,))
        return {**shown, "form": json.loads(row[1]), "rights": json.loads(row[2]), "views": json.loads(row[3]),
                "batches": [dict(zip(("id", "kind", "state", "actor", "created", "changes"), b)) for b in batches]}

    # --- projects -----------------------------------------------------------------------------------------------------

    async def project(self, name: str) -> dict | None:
        """A project's description as applied (`project.py`): `{project, description, rev, updated}`, or None.
        Raises Refused."""
        return await self._run(self._read, lambda: self._project(name))

    def _project(self, name: str) -> dict | None:
        row = self._db.execute("SELECT project, description, rev, updated FROM projects WHERE project = ?",
                               (name,)).fetchone()
        return {"project": row[0], "description": json.loads(row[1]), "rev": row[2], "updated": row[3]} if row else None

    def _projects(self) -> list[dict]:
        """On the thread: every project's description as applied, by name."""
        return [{"project": p, "description": json.loads(d), "rev": r, "updated": u} for p, d, r, u in self._db.execute(
            "SELECT project, description, rev, updated FROM projects ORDER BY project").fetchall()]

    async def put_project(self, writer: Writer, name: str, description: dict, now: float, dry: bool = False) -> dict:
        """A project's events, reactions and own views in place of the ones it had — its collections are defined
        apart (`project.apply`): `{project, rev, before, after, unchanged}`. The owner's alone: from the server's
        terminal, or his «Apply» of a proposal. Raises Rejected, Refused."""
        check_writer(writer)
        if writer_class(writer.actor) not in ("owner", "define"):
            raise Rejected("a project's description is the owner's to apply: propose it to him")
        if not isinstance(name, str) or not SPACE.fullmatch(name):
            raise Rejected(f"not a project's name: {name!r}")
        if not isinstance(description, dict) or len(_json(description).encode()) > PROJECT_MAX:
            raise Rejected(f"a project's description is an object of at most {PROJECT_MAX // 2**10} KB")
        answer = await self._run(self._put_project, name, description, now, dry)
        if not dry and not answer["unchanged"]:
            for collection in sorted({v["collection"] for v in description.get("views", [])}
                                     | {v["collection"] for v in (answer["before"] or {}).get("views", [])}):
                self._tell(collection, await self._run(self._rev, collection))
        return answer

    def _put_project(self, name: str, description: dict, now: float, dry: bool) -> dict:
        with self._write() as db:
            old = self._project(name)
            before = old["description"] if old else None
            answer = {"project": name, "rev": old["rev"] if old else 0, "before": before, "after": description,
                      "unchanged": before == description}
            if answer["unchanged"] or dry:
                return answer
            answer["rev"] += 1
            db.execute("INSERT INTO projects (project, description, rev, updated) VALUES (?, ?, ?, ?) ON CONFLICT "
                       "(project) DO UPDATE SET description = excluded.description, rev = excluded.rev, "
                       "updated = excluded.updated", (name, _json(description), answer["rev"], now))
            return answer

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
                           "{\"field\": \"stage\", \"op\": \"in\", \"value\": [\"qualified\", \"proposal\"]}")
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
            raise Rejected("\"field\" names a field of the form, or a ref's field: \"deal.stage\"")
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
        if spec["type"] == "instant":
            kept = [_instant(v) for v in value] if isinstance(value, list) and len(value) == 2 else [None]
            if None in kept or kept[0] > kept[1]:
                raise Rejected("between takes [first, last], two moments YYYY-MM-DDTHH:MM:SSZ, the first not after "
                               "the last")
            return f"{_path(name)} BETWEEN ? AND ?", kept
        if spec["type"] != "date":
            raise Rejected(f"between is for dates and moments: {name!r} is a {spec['type']}")
        if not isinstance(value, list) or len(value) != 2 or not all(map(_date, value)) or value[0] > value[1]:
            raise Rejected("between takes [first, last], two dates YYYY-MM-DD, the first not after the last")
        return f"{_path(name)} BETWEEN ? AND ?", list(value)


class Reader:
    """The collections as one `read` sees them, on the store's thread and in its one transaction: what the views, the
    agents' tools and the events read beside `query` and `count` — forms and rights, records, changes, and the words a
    screen shows them by — so that no table, column or statement of the file is named outside this module. Made for
    one `work`; not kept past it."""

    def __init__(self, store: Collections) -> None:
        self._store = store

    # --- forms, rights, projects ------------------------------------------------------------------------------------

    def form(self, collection: str) -> dict | None:
        return self._store._form(collection)

    def schema(self, collection: str) -> dict | None:
        """A collection's `{name, space, form, rights, views}`, or None."""
        row = self._store._db.execute(f"SELECT {SCHEMA_COLUMNS} FROM schemas WHERE collection = ?",
                                      (collection,)).fetchone()
        return _schema(row) if row else None

    def schemas(self, space: str | None = None) -> list[dict]:
        """Every collection by its name — a Space's alone, when one is named — each as `schema` gives it."""
        sql, params = f"SELECT {SCHEMA_COLUMNS} FROM schemas", ()
        if space is not None:
            sql, params = sql + " WHERE space = ?", (space,)
        return [_schema(row) for row in self._store._db.execute(sql + " ORDER BY collection", params).fetchall()]

    def rev(self, collection: str) -> str | None:
        """The collection's newest change_id: what a view is of."""
        return self._store._rev(collection)

    def project(self, name: str) -> dict | None:
        return self._store._project(name)

    def projects(self) -> list[dict]:
        return self._store._projects()

    def rules(self) -> set[str]:
        """Every `code:` rule some collection's rights name, as a writer or in `set_by`."""
        named: set[str] = set()
        for schema in self.schemas():
            rights = schema["rights"]
            named.update(c for c in rights.get("writers", {}) if c.startswith("code:"))
            named.update(c for values in rights.get("set_by", {}).values() for who in values.values() for c in who
                         if c.startswith("code:"))
        return named

    def writable(self, cls: str) -> list[dict]:
        """The collections a writer's class may write at all: `[{name, space, add, set}]`, `set` the fields it sets
        ("*" for all)."""
        out = []
        for schema in self.schemas():
            grant = schema["rights"].get("writers", {}).get(cls, {})
            if grant.get("add") or grant.get("set"):
                out.append({"name": schema["name"], "space": schema["space"], "add": bool(grant.get("add")),
                            "set": grant.get("set") or []})
        return out

    # --- records ----------------------------------------------------------------------------------------------------

    def record(self, collection: str, rid: str) -> dict | None:
        """One record, a deleted one included, or None."""
        return self._store._record(collection, rid)

    def records(self, collection: str, filter: Any = None, *, limit: int, by: str | None = None,
                newest: bool = False) -> list[dict]:
        """At most `limit` of the records that match `filter`, deleted ones aside: in the order of their ids — or of
        the field `by` names, or of when they last changed (`UPDATED`), the id deciding a tie — the oldest first, or
        the newest when `newest`. Raises Rejected."""
        store = self._store
        where, params = store._where(collection, store._reading(collection), filter, False)
        way = " DESC" if newest else ""
        order = [] if by is None else ["updated" + way] if by == UPDATED else [_path(by) + way]
        return [_record(row) for row in store._db.execute(
            f"SELECT {RECORD_COLUMNS} FROM records WHERE {where} ORDER BY {', '.join([*order, 'id' + way])} LIMIT ?",
            (*params, limit))]

    def count(self, collection: str, filter: Any = None, group: str | None = None) -> int | list[dict]:
        """As `Collections.count`, in this read. Raises Rejected."""
        return self._store._count(collection, filter, group)

    def probe(self, name: str, spec: dict, value: Any) -> Any:
        """A value a filter or a press gives a field, checked against its type and as it is kept — a ref only an id
        then, not one that has to exist. Raises Rejected."""
        return self._store._value(name, spec, value, probe=True)

    def latest(self, collection: str, ref: str, date: str, ids: list[str]) -> dict[str, dict]:
        """For each of these ids, the fields of the newest record of `collection` whose `ref` names it, by its `date`
        (the id deciding a tie); an id nothing names has none."""
        store = self._store
        form, out = store._reading(collection), {}
        for rid in ids:
            where, params = store._where(collection, form, [{"field": ref, "op": "ref_is", "value": rid}], False)
            found = store._db.execute(f"SELECT {RECORD_COLUMNS} FROM records WHERE {where} AND {_path(date)} IS NOT "
                                      f"NULL ORDER BY {_path(date)} DESC, id DESC LIMIT 1", params).fetchone()
            if found:
                out[rid] = _record(found)["fields"]
        return out

    def sums(self, collection: str, filter: Any, date: str, total: str | None,
             group: str | None) -> dict[tuple[str, str | None], decimal.Decimal]:
        """The records that match `filter` added up by the month (`YYYY-MM`) their `date` falls in and by `group`'s
        value: their `total` field — exactly, an amount in cents — or one each. Raises Rejected."""
        store = self._store
        where, params = store._where(collection, store._reading(collection), filter, False)
        value = _path(total) if total else "1"
        split = _path(group) if group else "NULL"
        sums: dict[tuple[str, str | None], decimal.Decimal] = {}
        for month, part, amount in store._db.execute(
                f"SELECT substr({_path(date)}, 1, 7), {split}, {value} FROM records WHERE {where}", params):
            if amount is not None:
                sums[month, part] = sums.get((month, part), decimal.Decimal(0)) + decimal.Decimal(str(amount))
        return sums

    def words(self, collection: str, ids) -> dict[str, str]:
        """The words of these records of `collection`: the first `text` field of its form, as each has it now — so a
        ref reads «Acme», not an id. A deleted record, or one without that field, is not among them."""
        ids = sorted({i for i in ids if isinstance(i, str)})
        form = self._store._form(collection) if ids else None
        text = next((k for k, s in form["fields"].items() if s["type"] == "text"), None) if form else None
        said: dict[str, str] = {}
        if text is None:
            return said
        for at in range(0, len(ids), IDS_CHUNK):
            chunk = ids[at:at + IDS_CHUNK]
            said.update(self._store._db.execute(
                f"SELECT id, {_path(text)} FROM records WHERE collection = ? AND id IN ({', '.join('?' * len(chunk))})"
                f" AND deleted IS NULL AND {_path(text)} IS NOT NULL", (collection, *chunk)).fetchall())
        return said

    def ref_words(self, form: dict, values) -> dict[str, str]:
        """The words of every id among `values` (dicts of fields) that one of the form's ref fields names."""
        words: dict[str, str] = {}
        for name, spec in form["fields"].items():
            if spec["type"] == "ref":
                words.update(self.words(spec["to"], (v.get(name) for v in values if isinstance(v, dict))))
        return words

    def title(self, form: dict, fields: dict, parts: int = 2) -> str | None:
        """A record in a few words, for a list of changes or a ref to pick: its first `parts` text or ref fields that
        have a value, in the form's order, a ref by its record's words — «Acme · Backend». None when it has none."""
        words = []
        for name, spec in form["fields"].items():
            value = fields.get(name)
            if spec["type"] == "text" and isinstance(value, str):
                words.append(value)
            elif spec["type"] == "ref" and (said := self.words(spec["to"], [value]).get(value)):
                words.append(said)
            if len(words) == parts:
                break
        return " · ".join(words) or None

    def file_names(self, shas) -> dict[str, str]:
        """The names the owner's device gave these uploaded files: a `file` field reads «acme-invoice.pdf»."""
        shas = sorted({s for s in shas if isinstance(s, str)})
        named: dict[str, str] = {}
        for at in range(0, len(shas), IDS_CHUNK):
            chunk = shas[at:at + IDS_CHUNK]
            named.update(self._store._db.execute(
                f"SELECT sha256, name FROM files WHERE sha256 IN ({', '.join('?' * len(chunk))})", chunk).fetchall())
        return named

    # --- changes ----------------------------------------------------------------------------------------------------

    def change(self, change_id: str) -> dict | None:
        return self._store._change_of(change_id)

    def history(self, collection: str, rid: str, limit: int = HISTORY) -> list[dict]:
        """A record's changes, the newest first."""
        return self._store._history(collection, rid, limit)

    def marks(self, collection: str, ids: list[str],
              field: str | None = None) -> tuple[dict[str, tuple[str | None, float]], dict[str, str]]:
        """For each of these records: its last change — of `field`, when one is named — as `(as_of, ts)`, and the
        `src` of its last change that had one. Only their own changes are read, by the index on a record's
        changes."""
        if field is not None and not NAME.fullmatch(field):
            raise ValueError(field)
        touched = f"json_type(after, '$.{field}') IS NOT NULL" if field is not None else "1"
        last, srcs = {}, {}
        for at in range(0, len(ids), IDS_CHUNK):
            chunk = ids[at:at + IDS_CHUNK]
            sql = (f"SELECT id, {{}} FROM changes WHERE seq IN (SELECT max(seq) FROM changes WHERE collection = ? AND id "
                   f"IN ({', '.join('?' * len(chunk))}) AND {{}} GROUP BY id)")
            for rid, as_of, ts in self._store._db.execute(sql.format("as_of, ts", touched), (collection, *chunk)):
                last[rid] = (as_of, ts)
            srcs.update(self._store._db.execute(sql.format("src", "src IS NOT NULL"), (collection, *chunk)).fetchall())
        return last, srcs

    def changed(self, collection: str, filter: Any, field: str, to: list,
                span: tuple[str, str, float, float] | None = None) -> int:
        """How many records the filter matches now whose `field` a change gave a value (one of `to`, when it names
        some) — within `span`, `(first, last, start, end)`, when one is given: a change dated by its `as_of` from the
        day `first` to `last`, else by the moment it was made, from `start` to before `end`. A change taken back, and
        the revert that took it, move nothing: a drag and its «Undo» count none. Raises Rejected."""
        store = self._store
        if not NAME.fullmatch(field):
            raise ValueError(field)
        after = f"json_extract(after, '$.{field}')"  # as `_path`, of what a change made the field
        where, params = store._where(collection, store._reading(collection), filter, False)
        sql = [f"SELECT count(DISTINCT id) FROM changes WHERE collection = ? AND {after} IS NOT NULL",
               "AND reverts IS NULL AND change_id NOT IN (SELECT reverts FROM changes WHERE collection = ? AND reverts IS "
               f"NOT NULL) AND id IN (SELECT id FROM records WHERE {where})"]
        values: list[Any] = [collection, collection, *params]
        if to:
            sql.append(f"AND {after} IN ({', '.join('?' * len(to))})")
            values += to
        if span is not None:
            sql.append("AND (as_of BETWEEN ? AND ? OR as_of IS NULL AND ts >= ? AND ts < ?)")
            values += list(span)
        return store._db.execute(" ".join(sql), values).fetchone()[0]

    def taken(self, change_ids: list[str]) -> set[str]:
        """Which of these changes a revert took back."""
        if not change_ids:
            return set()
        return {r[0] for r in self._store._db.execute(
            f"SELECT reverts FROM changes WHERE reverts IN ({', '.join('?' * len(change_ids))})", change_ids)}

    def space_changes(self, space: str, before: str | None, limit: int) -> dict:
        """The newest `limit` changes to the records of a Space's collections — before the change `before`, when one
        is named — each with its record's title, version and deleted state now, for its «Undo», and whether a revert
        took it back already: `{changes, next, words}`, `next` the change to read on from."""
        forms = {schema["name"]: schema["form"] for schema in self.schemas(space)}
        if not forms:
            return {"changes": [], "next": None, "words": {}}
        sql = (f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection IN ({', '.join('?' * len(forms))}) "
               "AND id IS NOT NULL")
        params: list[Any] = list(forms)
        if before is not None:
            sql += " AND seq < coalesce((SELECT seq FROM changes WHERE change_id = ?), 0)"
            params.append(before)
        rows = [_change(r) for r in self._store._db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*params, limit + 1))]
        shown, words = [], {}
        taken = self.taken([c["change_id"] for c in rows])
        for change in rows[:limit]:
            form, record = forms[change["collection"]], self.record(change["collection"], change["id"])
            words.update(self.ref_words(form, [change["before"], change["after"]]))
            shown.append({**change, "title": self.title(form, record["fields"]) if record else None,
                          "version": record["version"] if record else None,
                          "deleted": record["deleted"] if record else None, "reverted": change["change_id"] in taken})
        return {"changes": shown, "words": words, "next": shown[-1]["change_id"] if len(rows) > limit else None}

    def machine_changes(self, after: str | None, since: float | None, limit: int,
                        exclude: tuple[str, ...] = ()) -> dict:
        """The assistant's, the rules' and the projects' reactions' changes to records, oldest first: after the change
        `after` (a ULID: the cursor survives a restore, whose new changes are always later), or since the moment
        `since` — `{changes, next, words}`, each with its collection's Space, its record's title, whether a revert took
        it back, and, for a revert, whose change it took back (`reverts_actor`); `next` set when more are left than
        `limit`. The rules in `exclude` are left out."""
        db = self._store._db
        sql, params = f"SELECT {CHANGE_COLUMNS} FROM changes WHERE {MACHINES} AND id IS NOT NULL", []
        if exclude:
            sql, params = sql + f" AND actor NOT IN ({', '.join('?' * len(exclude))})", [*exclude]
        if after is not None:
            sql, params = sql + " AND change_id > ?", [*params, after]
        elif since is not None:
            sql, params = sql + " AND ts > ?", [*params, since]
        rows = [_change(r) for r in db.execute(sql + " ORDER BY change_id LIMIT ?", (*params, limit + 1))]
        shown, words, schemas = rows[:limit], {}, {}
        taken = self.taken([c["change_id"] for c in shown])
        targets = [c["reverts"] for c in shown if c["reverts"]]
        actors = dict(db.execute(f"SELECT change_id, actor FROM changes WHERE change_id IN "
                                 f"({', '.join('?' * len(targets))})", targets).fetchall()) if targets else {}
        out = []
        for change in shown:
            if change["collection"] not in schemas:
                schemas[change["collection"]] = self.schema(change["collection"])
            schema = schemas[change["collection"]]
            record = self.record(change["collection"], change["id"])
            words.update(self.ref_words(schema["form"], [change["before"], change["after"]]))
            out.append({**change, "space": schema["space"],
                        "title": self.title(schema["form"], record["fields"]) if record else None,
                        "reverted": change["change_id"] in taken, "reverts_actor": actors.get(change["reverts"])})
        return {"changes": out, "words": words, "next": shown[-1]["change_id"] if len(rows) > limit else None}

    def foreign(self, collection: str, ids: list[str]) -> bool:
        """Whether any of these records was written by a writer that had read someone else's text, or by a session or
        the Mac — which carry no mark of their own: a session's page, a Mac's anything."""
        if not ids:
            return False
        marks = ", ".join("?" * len(ids))
        return self._store._db.execute(
            f'SELECT 1 FROM changes WHERE collection = ? AND id IN ({marks}) AND ("foreign" IS NOT NULL OR actor LIKE '
            "'session:%' OR actor LIKE 'mac:%') LIMIT 1", (collection, *ids)).fetchone() is not None

    def owner_changed(self, since: float, until: float) -> bool:
        """Whether the owner changed any record from `since` to before `until` — on a screen of his, or from a Mac."""
        return self._store._db.execute(
            "SELECT 1 FROM changes WHERE ts >= ? AND ts < ? AND (actor = 'owner' OR actor LIKE 'mac:%') LIMIT 1",
            (since, until)).fetchone() is not None

    # --- the events' log --------------------------------------------------------------------------------------------

    def events(self, project: str | None = None, limit: int = 50) -> list[dict]:
        """The newest events, a project's alone when one is named: each `{id, source, project, name, collection,
        record, depth, at, outcome}`."""
        sql, args = f"SELECT {EVENT_COLUMNS} FROM events", []
        if project is not None:
            sql, args = sql + " WHERE project = ?", [project]
        rows = self._store._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        keys = ("id", "source", "project", "name", "collection", "record", "depth", "at", "outcome")
        return [dict(zip(keys, row[:-1] + (json.loads(row[-1]),))) for row in rows]


class Logging(Reader):
    """A `Reader` in a transaction taken for writing (`Collections.write`), that also keeps the events' log: kept
    whole, or not at all."""

    def log_event(self, source: str, project: str, name: str, collection: str | None, record: str | None,
                  depth: int, at: float) -> str | None:
        """An event logged once by what fired it — `change:<change_id>`, or `call:<writer>/<idem>`: its new id, or None
        when `source` fired it already."""
        eid = self._store._ulids.next(at)
        made = self._store._db.execute(
            "INSERT OR IGNORE INTO events (id, source, project, name, collection, record, depth, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (eid, source, project, name, collection, record, depth, at)).rowcount
        return eid if made else None

    def keep_outcome(self, event: str, outcome: dict) -> None:
        """What an event's reactions did, kept with it: its log's `outcome` is `[outcome]`."""
        self._store._db.execute("UPDATE events SET outcome = ? WHERE id = ?", (_json([outcome]), event))


def _schema(row: tuple) -> dict:
    return {"name": row[0], "space": row[1], "form": json.loads(row[2]), "rights": json.loads(row[3]),
            "views": json.loads(row[4])}


def shown_fields(form: dict) -> dict:
    """A form's fields as a screen asks for them: each one's type, and its values, labels, `to`, `currency`,
    `required` and `hide` — a field kept for the agents, which a record's sheet leaves out."""
    return {name: {k: v for k, v in spec.items()
                   if k in ("type", "values", "labels", "to", "currency", "required", "hide")}
            for name, spec in form["fields"].items()}


def _newest(db: sqlite3.Connection) -> str | None:
    """The newest id the file holds, a change's or a batch's: where the next must start above."""
    return db.execute("SELECT max(id) FROM (SELECT max(change_id) AS id FROM changes "
                      "UNION ALL SELECT max(id) FROM batches)").fetchone()[0]


def _jsonl(path: Path, rows) -> int:
    """Rows written as JSON lines; how many."""
    n = 0
    with open(path, "w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


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


def check_writer(writer: Writer) -> None:
    if not isinstance(writer, Writer) or not isinstance(writer.actor, str) or not ACTOR.fullmatch(writer.actor):
        raise Rejected("not a writer the store knows")
    if writer.via not in VIA:
        raise Rejected(f"via is one of {', '.join(VIA)}: not {writer.via!r}")
    if writer.model is not None and not plain(writer.model, 100):
        raise Rejected(f"not a model's name: {writer.model!r}")
    if not all(isinstance(label, str) and LABEL.fullmatch(label) for label in writer.foreign):
        raise Rejected(f"not a mark of foreign text: {writer.foreign!r}")
