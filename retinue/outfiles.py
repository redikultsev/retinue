"""Files a session hands to the owner. The convention: what the agent makes for him — a report, a PDF, a picture, an
HTML page — it saves under `out/` of its working directory (the lanes' instructions say so:
`deploy/lanes/instructions/out.md`). When a turn ends, the platform looks there through its read-only mount of the
lane's /work and keeps every file that changed during the session's turns since it last looked; the router sends each
to the owner in Telegram, under the session's last message, so a reply to it goes to the session; the face lists them.

The platform never runs, opens or interprets a file: it reads bytes, hashes them and passes them on. What it reads is
bounded before it reads it — the types on the list, the size, the count and the bytes of one turn — and links are
followed as the gate follows them (`approvals._in_mount`): a link that leads out of /work is refused, and the file is
then opened one directory at a time with `O_NOFOLLOW`, so a link swapped in after the check is refused too, never
followed into the platform's own files.

A file is kept by value: its bytes go into the platform's SQLite (`out_blobs`, by SHA-256) at the end of the turn, so
what the owner gets is what was there then, whatever the session writes over it later. A file sent once is not sent
again while its bytes stay the same. Everything is pruned after 30 days, with the face's closed sessions, and the
bytes kept are capped in all and per day.

A name is shown, stored and sent only as valid UTF-8 with its direction marks and control characters written as
visible labels (`shown`), so a name cannot pass for another type («report‮fdp.exe»); a name that is not UTF-8 at all is
left out. Two sessions may share one directory: a file goes to the session whose own calls named it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import secrets
import stat
from dataclasses import dataclass, field

from . import approvals
from .approvals import Unbindable

OUT = "out"                       # the folder of the session's directory that holds what it hands over
MAX_FILE = 20 * 1024 * 1024       # one file; Telegram's bots may send 50 MB, and the bytes cross the router as JSON
MAX_TURN = 50 * 1024 * 1024       # every file of one turn together
MAX_FILES = 10                    # files of one turn; the rest are named, not sent
MAX_ENTRIES = 500                 # files and folders one look walks at most: a tree dumped in out/ costs no more
MAX_DEPTH = 3                     # folders under out/
SLACK_S = 2.0                     # the lane's file times and the platform's clock: one kernel, a write in the same second
FRESH_S = 2.0                     # a file written this recently may be half written: it goes with the next look
KEEP_S = 30 * 86400               # the face keeps a closed session as long
DAY_S = 86400
PHOTO_MAX = 5 * 1024 * 1024       # a picture this small goes to Telegram as a photo; bigger, as a document
# What may be handed over, by extension, and the type it is served as — never what the file says it is.
TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}
PHOTOS = ("image/png", "image/jpeg", "image/webp")  # what Telegram shows as a photo; a GIF would lose its frames
# What a browser runs when the file is opened by itself: never sent to Telegram as a raw document (`bridge._file`).
ACTIVE = ("text/html", "image/svg+xml")
MB = 1024 * 1024
# Direction marks and overrides, and control characters: shown as labels, so a name reads as what it is.
HIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SHARED = "two sessions worked in the folder"
EDIT_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


@dataclass(frozen=True)
class Found:
    path: str        # relative to out/, with `/`, as `shown`
    mime: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class Skipped:
    path: str        # as `shown`
    why: str         # Russian: the owner reads it


@dataclass
class Look:
    found: list[Found] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)  # files another session may have made too (`attribute`)


def mime_of(path: str) -> str | None:
    return TYPES.get(posixpath.splitext(path)[1].lower())


def utf8(name: str) -> bool:
    """A name as the file system gave it (undecodable bytes as surrogates) is valid UTF-8."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def shown(name: str) -> str:
    """A name as the owner may read it: valid UTF-8 (a byte that is not, as U+FFFD), and every direction mark,
    override and control character as a visible label ⟦U+202E⟧."""
    text = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return HIDDEN.sub(lambda m: f"⟦U+{ord(m.group()):04X}⟧", text)


def _parts(path: str) -> list[str]:
    return [p for p in path.split(os.sep) if p not in ("", os.curdir)]


def _open(root: str, parts: list[str]) -> int:
    """A file under `root` opened one directory at a time, no link followed at any step: the path was checked with
    its links resolved, and one swapped in since is refused here instead of followed."""
    if not parts:
        raise IsADirectoryError("the work volume itself")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            step = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = step
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _Entry:
    path: str            # in out/, as the file system gave it
    parts: list[str]     # the lane path's parts under /work
    changed: float       # the entry's own time (a link's, not its target's): what is new since the last look


def _walk(out_real: str, root: str, look: Look) -> list[_Entry]:
    """The entries of out/, files and folders counted, by directory fd (no folder link is entered), sorted."""
    entries: list[_Entry] = []
    count = 0
    for dirpath, dirnames, filenames, dirfd in os.fwalk(out_real, follow_symlinks=False):
        inside = _parts(os.path.relpath(dirpath, out_real))
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and len(inside) < MAX_DEPTH)
        count += len(dirnames)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            count += 1
            if count > MAX_ENTRIES:
                break
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                changed = max(st.st_mtime, st.st_ctime)
            except OSError:
                changed = float("inf")  # gone or unreadable as an entry: said, as new
            entries.append(_Entry("/".join([*inside, name]), [*_parts(os.path.relpath(dirpath, root)), name], changed))
        if count > MAX_ENTRIES:
            look.skipped.append(Skipped(f"{OUT}/", f"the folder has over {MAX_ENTRIES} files and folders — looked at the first ones"))
            break
    return entries


def scan(work_mount: str, cwd: str, since: float, known: set[tuple[str, str]], now: float) -> Look:
    """What `out/` of the lane directory `cwd` (relative to /work) holds for the owner at `now`: the files on the list
    that changed at or after `since` and whose bytes are not in `known` ((path, sha256) already handed over from this
    directory), within the limits; what was left out, and why — an entry that cannot be read only when it is itself
    new since then, so a stale bad link is not said after every turn. A file written in the last FRESH_S is left for
    the next look. A missing `out/` is an empty look."""
    look = Look()
    root = os.path.realpath(work_mount)
    lane_out = posixpath.join(approvals.WORK, cwd, OUT)
    try:
        out_real = approvals._in_mount(approvals._lane_relative(lane_out) or [], root)
    except Unbindable:
        look.skipped.append(Skipped(f"{OUT}/", "the folder leads outside /work — not looking"))
        return look
    if not os.path.isdir(out_real) or os.path.islink(out_real):
        return look
    total = 0
    for entry in _walk(out_real, root, look):
        new, path = entry.changed >= since - SLACK_S, shown(entry.path)
        if not utf8(entry.path):
            if new:
                look.skipped.append(Skipped(path, "the file name is not UTF-8"))
            continue
        mime = mime_of(path)
        try:
            real = approvals._in_mount(entry.parts, root)
            fd = _open(root, _parts(os.path.relpath(real, root)))
        except Unbindable:
            if new:
                look.skipped.append(Skipped(path, "the link leads outside /work"))
            continue
        except IsADirectoryError:
            continue  # a link to a folder, /work itself too: not a file
        except OSError as exc:
            if new:
                look.skipped.append(Skipped(path, f"the platform cannot read it ({type(exc).__name__})"))
            continue
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or max(st.st_mtime, st.st_ctime) < since - SLACK_S \
                or st.st_mtime > now - FRESH_S:
            os.close(fd)
            continue  # not a file, not touched in these turns, or still being written: nothing to say about it now
        with os.fdopen(fd, "rb") as f:
            # Every limit before the bytes are read: a turn that leaves a thousand big files costs no more to look at.
            why = _over(mime, st.st_size, len(look.found), total)
            if why is None:
                try:
                    data = f.read(MAX_FILE + 1)
                except OSError as exc:
                    why = f"the platform cannot read it ({type(exc).__name__})"
                else:
                    why = _over(mime, len(data), len(look.found), total)  # it changed between the look and the read
        if why is not None:
            look.skipped.append(Skipped(path, why))
            continue
        sha = hashlib.sha256(data).hexdigest()
        if (path, sha) in known:
            continue  # handed over before with these very bytes
        total += len(data)
        look.found.append(Found(path, mime, data, sha))
    return look


def _over(mime: str | None, size: int, count: int, total: int) -> str | None:
    """Why a file of this type and size is not taken after `count` files of `total` bytes; None: it is."""
    if mime is None:
        return "type not on the list"
    if size == 0:
        return "empty"
    if size > MAX_FILE:
        return f"over {MAX_FILE // MB} MB"
    if count >= MAX_FILES:
        return f"over {MAX_FILES} files in a turn"
    if total + size > MAX_TURN:
        return f"over {MAX_TURN // MB} MB in a turn"
    return None


def named(lines: list[str], cwd: str, path: str) -> bool:
    """Whether a session's raw lines name the file `out/<path>` of the lane directory `cwd` in one of its own calls:
    the path a Write, Edit, MultiEdit or NotebookEdit changed, or a Bash command whose text holds the file's name."""
    target = posixpath.normpath(posixpath.join(approvals.WORK, cwd, OUT, path))
    base = posixpath.basename(path)
    for line in lines:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("type") != "assistant" or not isinstance(data.get("message"), dict):
            continue
        content = data["message"].get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or \
                    not isinstance(block.get("input"), dict):
                continue
            name, given = block.get("name"), block["input"]
            if name in EDIT_KEYS and isinstance(changed := given.get(EDIT_KEYS[name]), str) and \
                    posixpath.normpath(posixpath.join(approvals.WORK, cwd, changed)) == target:
                return True
            if name == "Bash" and isinstance(command := given.get("command"), str) and base in shown(command):
                return True
    return False


def attribute(look: Look, cwd: str, mine: list[str], theirs: list[list[str]]) -> Look:
    """A look of a directory another session also ran in during the window: a file named by the other session's calls
    alone is left for it (its own look takes it); one named by both or by neither stays here, said as shared."""
    kept = Look(skipped=look.skipped)
    for found in look.found:
        here, there = named(mine, cwd, found.path), any(named(lines, cwd, found.path) for lines in theirs)
        if there and not here:
            continue
        if here == there:
            kept.shared.append(found.path)
        kept.found.append(found)
    return kept


def _code(path: str) -> str:
    return f"`{path}`" if "`" not in path else path


def notice_text(look: Look) -> str:
    """What the owner is told beside the files, as Markdown the router renders: what was left out and why, and the
    files another session may have made."""
    parts = []
    if look.skipped:
        parts.append("Not sent from out/:\n" + "\n".join(f"- {_code(s.path)} — {s.why}" for s in look.skipped))
    if look.shared:
        parts.append(f"Warning: {SHARED} — these files may have been made by the other one: "
                     + ", ".join(_code(p) for p in look.shared))
    return "\n\n".join(parts)


class OutFiles:
    """The files sessions handed over, in the platform's SQLite: each file's row, its bytes by hash, and per session
    the time from which a changed file is its to hand over, with a flag that a turn ended and a look is owed (and the
    number of the owing, so a turn that ends during a look is looked at again). A look is recorded in one transaction
    — rows, notes for the router, events for the face, the new time — so a crash before it looks again, and one after
    has nothing left to do."""

    def __init__(self, store, store_mb: int = 500, day_mb: int = 200) -> None:
        self.store, self.db = store, store.db
        self.store_cap, self.day_cap = store_mb * MB, day_mb * MB
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS out_files (
                id TEXT PRIMARY KEY,                -- 16 hex: the face's and the router's handle
                session TEXT NOT NULL,
                lane TEXT NOT NULL,
                cwd TEXT NOT NULL,                  -- the track's directory under /work, as it was looked at
                path TEXT NOT NULL,                 -- under out/, with `/`, as `shown`
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mime TEXT NOT NULL,                 -- from TYPES, by extension
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS out_files_session ON out_files (session, at);
            CREATE INDEX IF NOT EXISTS out_files_seen ON out_files (lane, cwd);
            CREATE TABLE IF NOT EXISTS out_blobs (
                sha256 TEXT PRIMARY KEY,
                data BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS out_looks (
                session TEXT PRIMARY KEY,
                since REAL NOT NULL,                -- a file changed from then on is this session's to hand over
                due INTEGER NOT NULL DEFAULT 0,     -- a turn ended and the look is owed
                gen INTEGER NOT NULL DEFAULT 0      -- how many times it was owed: a look clears only its own
            );
            """)
        self.db.commit()

    def owe(self, session: str, created: float) -> None:
        """A turn of `session` ended: a look is owed; its first look counts from the session's creation."""
        self.db.execute("INSERT INTO out_looks (session, since, due, gen) VALUES (?, ?, 1, 1) "
                        "ON CONFLICT (session) DO UPDATE SET due = 1, gen = gen + 1", (session, created))
        self.db.commit()

    def owed(self) -> list[dict]:
        rows = self.db.execute("SELECT session, since, gen FROM out_looks WHERE due = 1 ORDER BY rowid")
        return [{"session": session, "since": since, "gen": gen} for session, since, gen in rows]

    def known(self, lane: str, cwd: str) -> set[tuple[str, str]]:
        return {(path, sha) for path, sha in self.db.execute(
            "SELECT path, sha256 FROM out_files WHERE lane = ? AND cwd = ?", (lane, cwd))}

    def rivals(self, session: str, tracks: list[str], since: float, until: float, lane: str = "",
               cwd: str | None = None) -> list[str]:
        """The other sessions in the same lane directory with a run in [since, until]: those made in `cwd` of `lane`
        (a sphere's directory may differ from its track's), and older ones, made before sessions kept their
        directory, on `tracks` (those of the same lane and directory)."""
        marks = ", ".join("?" * len(tracks)) or "NULL"
        rows = self.db.execute(
            f"SELECT DISTINCT r.session FROM runs r JOIN sessions s ON s.id = r.session WHERE r.session != ? "
            f"AND ((s.cwd IS NULL AND s.track IN ({marks})) OR (s.cwd IS NOT NULL AND s.lane = ? AND s.cwd = ?)) "
            f"AND r.started <= ? AND (r.ended IS NULL OR r.ended >= ?)",
            (session, *tracks, lane, cwd, until, since))
        return [row[0] for row in rows]

    def lines(self, session: str, since: float, until: float) -> list[str]:
        """The session's raw lines that came in [since, until]: its calls of the window."""
        rows = self.db.execute("SELECT line FROM raw WHERE session = ? AND at >= ? AND at <= ? ORDER BY id",
                               (session, since - SLACK_S, until))
        return [row[0] for row in rows]

    def capped(self, look: Look, now: float) -> Look:
        """The look within the platform's storage: bytes not kept yet must fit under the cap in all and per day; a
        file over either is left out and said."""
        stored = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM (SELECT MAX(size) size FROM out_files "
                                 "GROUP BY sha256)").fetchone()[0]
        today = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM out_files WHERE at >= ?",
                                (now - DAY_S,)).fetchone()[0]
        kept = Look(skipped=list(look.skipped), shared=look.shared)
        for found in look.found:
            size = len(found.data)
            fresh = self.db.execute("SELECT 1 FROM out_blobs WHERE sha256 = ?", (found.sha256,)).fetchone() is None
            if fresh and stored + size > self.store_cap:
                kept.skipped.append(Skipped(found.path, f"the platform's file store is full ({self.store_cap // MB} MB)"))
                continue
            if fresh and today + size > self.day_cap:
                kept.skipped.append(Skipped(found.path, f"over {self.day_cap // MB} MB of files in a day"))
                continue
            stored, today = stored + (size if fresh else 0), today + size
            kept.found.append(found)
        kept.shared = [p for p in look.shared if any(f.path == p for f in kept.found)]
        return kept

    def record(self, session: dict, cwd: str, look: Look, looked: float, gen: int) -> list[str]:
        """The look's files kept, the router given a note per file and one for what it should know besides, the face
        an event per file; the session's next look counts from `looked`, and this look's owing is cleared unless a
        turn ended again meanwhile. The new files' ids."""
        ids = []
        with self.db:
            for found in look.found:
                file_id = secrets.token_hex(8)
                self.db.execute("INSERT OR IGNORE INTO out_blobs VALUES (?, ?)", (found.sha256, found.data))
                self.db.execute("INSERT INTO out_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (file_id, session["id"], session["lane"], cwd, found.path, len(found.data),
                                 found.sha256, found.mime, looked))
                self.store._note("file", session["id"], {"id": file_id, "path": found.path,
                                                         "size": len(found.data), "mime": found.mime})
                self.store._event("file", session["id"], file_id, self.face_file(file_id))
                ids.append(file_id)
            if text := notice_text(look):
                self.store._note("file_skipped", session["id"], {"head": _head(session), "text": text})
            self._looked(session["id"], looked, gen)
        return ids

    def failed(self, session: dict, looked: float, gen: int, why: str) -> None:
        """A look that broke: said once, and owed no more — the next turn's look counts from `looked`."""
        with self.db:
            self.store._note("file_skipped", session["id"], {"head": _head(session),
                                                             "text": f"Files from out/ were not collected: {why}."})
            self._looked(session["id"], looked, gen)

    def _looked(self, session: str, looked: float, gen: int) -> None:
        self.db.execute("UPDATE out_looks SET since = ?, due = CASE WHEN gen = ? THEN 0 ELSE due END "
                        "WHERE session = ?", (looked, gen, session))

    def settle(self, session: str) -> None:
        """A look that cannot be made (its lane or track gone): owed no more."""
        self.db.execute("UPDATE out_looks SET due = 0 WHERE session = ?", (session,))
        self.db.commit()

    FACE = ("id", "session", "path", "size", "mime", "at")

    def face_file(self, file_id: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self.FACE)} FROM out_files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return None
        data = dict(zip(self.FACE, row))
        return {**data, "name": posixpath.basename(data["path"])}

    def face_files(self, session: str) -> list[dict]:
        ids = self.db.execute("SELECT id FROM out_files WHERE session = ? ORDER BY at, rowid", (session,)).fetchall()
        return [self.face_file(file_id) for (file_id,) in ids]

    def content(self, file_id: str) -> tuple[dict, bytes] | None:
        """A file and its bytes, for the router and the face's download."""
        data = self.face_file(file_id)
        if data is None:
            return None
        row = self.db.execute("SELECT b.data FROM out_files f JOIN out_blobs b ON b.sha256 = f.sha256 WHERE f.id = ?",
                              (file_id,)).fetchone()
        return (data, bytes(row[0])) if row else None

    def prune(self, now: float) -> int:
        """Files older than 30 days go, and the bytes no file names any more; the looks of sessions gone."""
        with self.db:
            cursor = self.db.execute("DELETE FROM out_files WHERE at < ?", (now - KEEP_S,))
            self.db.execute("DELETE FROM out_blobs WHERE sha256 NOT IN (SELECT sha256 FROM out_files)")
            self.db.execute("DELETE FROM out_looks WHERE session NOT IN (SELECT id FROM sessions)")
        return cursor.rowcount


def _head(session: dict) -> str:
    return f"Files · {session['track']} · session {session['id']}"


def router_payload(data: dict, body: bytes) -> bytes:
    """`/owner/file`'s answer for a file: its name, type and bytes in base64, as JSON — built off the event loop."""
    return json.dumps({"name": data["name"], "path": data["path"], "mime": data["mime"],
                       "data": base64.b64encode(body).decode()}, ensure_ascii=False).encode()
