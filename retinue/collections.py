"""The owner's collections (docs/collections.md): one SQLite file beside the platform's own, `collections.sqlite` on
the platform's volume, and the files he uploads, kept by their SHA-256 in `attachments/` beside it.

One thread, one connection. Every read and every write goes through a single dedicated thread that opened the file
itself; the loop only awaits it. The platform's other databases live on the loop's thread, and SQLite's busy
handler sleeps where it is called: a writer that waited on the loop would hold up the hooks, the gate and the face
with it. Here a lock that does not come within 2 s (the night's backup copy, an import) is `Refused` at once, for
the caller to say «try again», never waited out for 30 s.

The file uses a rollback journal, like every database the backup copies (deploy/backup/retinue-backup.py). Slice 0
has one table, `files`; the records, their changes, batches and schemas are later tables in the same file, opened
by the same thread.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from pathlib import Path
from typing import TypeVar

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
"""
FILE_COLUMNS = "sha256, name, size, mime, created"


class Refused(Exception):
    """The file was locked past `BUSY_S`, or the store is closing: nothing was written; the same call may simply be
    made again."""


class Full(Exception):
    """The file would leave less than `FREE_MIN` on the volume: nothing was written."""


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
