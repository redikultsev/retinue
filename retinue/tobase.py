"""The Base and the sessions: who changed what in the owner's knowledge base (the Base), and the platform's own commit
after every turn — the session writes, the platform commits and pushes, the owner reads one line. The clone and the
commit itself are the Base's own (`base.BaseRepo`, `docs/base.md`); this module never runs git and never touches the
tree.

**Who changed what.** Every session on the Base's lane — its folder in the clone or not: a notes session at `/work`
can Edit `/work/base/…` too; a side question only reads, so never — gets the Base files its runs changed. Each run is
watched through windows: a `snapshot()` of the tree (path → digest) when it starts, and again at each of its marks — a
`result` line (a turn ended; one run may hold several turns, the owner's message going into a live run) and its end.
What differs between the two snapshots of a window is the run's, and is recorded for its session in SQLite
(`base_files`), with the paths its own file tools named in that window (`changes.Scan` over its raw lines) as a
cross-check: a path both name is `both`, one only the tools name (a stat the snapshot missed) is still recorded, as
is one only the snapshot names — `bash` when one of the window's Bash commands names it and writes (`bash_writes`: a
`sed -i`, a heredoc's `write_text`), else `snapshot`: the edit of a script that names no file, or another session's run
in the same window. A `snapshot` claim gives way to any other: a run whose own tools or command wrote the file takes it
from every session that only saw it change, whichever mark comes first. Only a path the clone says is uncommitted
(`changed()`) is ever recorded or kept: a file committed or refreshed to the hub's content leaves every session's list
(`_prune`), at every mark, every start, after a commit and once a minute. The marks are taken after the poll that
ingested their lines (`settle`) and before any new run on the lane starts, so a window never takes in a later run's
edits; every window's state is a row (`base_runs`), so a restart picks up where it was.

**The commit after a turn.** When a run of a session ends and no run on the lane is open (every window marked), the
platform commits by itself everything the session changed — added, edited, deleted, shared with another session —
and the files no session claims, pushed to the hub as the owner's own, as the owner's agents on the Mac commit
(`base.commit`, his rules; what the platform never commits — the Base's scripts, hidden paths — stays out). It is a
card (`base_cards`): never shown while it commits, said in **one line** under the session's latest ping and on its
card in the app — «Written to the Base: <files> · <sha>», with «Undo» beside it: a new commit on top that puts those
files back, each only if neither the hub nor the clone changed it since (`base.revert`).

**What kept a file out is the session's to fix.** The Base's own check (`just check`: a Record without its Showcase
row, say), a file both the session and the hub edited (`base` writes both versions between conflict markers, and no
commit takes it until they are merged): the session is told, as a message from the platform (`REFUSED_ASK`,
`MERGE_ASK`, `by="platform"`), and fixes it in its next turn, whose own commit takes the fix. The owner's line says
«Not written: <why>. The session was asked to fix it.» The same problems again after the session was told
(`base_asks`) are the owner's: «…The session could not fix it: it needs you.» Rolling back is «Undo», or the evening's
list of commits.

The owner never commits by a button: the platform's commit runs after a turn, its «Undo» is reachable only through
the router's token (the bridge) and the face's login, never from a lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
import secrets
from typing import TYPE_CHECKING

from . import changes, episodes
from .kbcheck import OWNER
from .spoken import unmarked
from .courier import line
from .sessions import parsed

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.tobase")

# A Bash command that writes (`bash_writes`): a redirection (not of a descriptor, not to /dev/null), an in-place edit,
# a file command, a git command that rewrites the tree, a script's write call.
_WRITES = re.compile(r"(?<![0-9&<>=-])>{1,2}(?!\s*(?:/dev/null|&))|\b(?:sed|perl)\b[^|;&\n]*\s-[a-zA-Z]*i|"
                     r"\b(?:tee|mv|cp|rm|touch|truncate|patch|install|ln|unlink)\b|"
                     r"\bgit\s+(?:checkout|restore|apply|mv|rm|reset|stash)\b|"
                     r"\bwrite_(?:text|bytes)\b|\.write\(|\bopen\([^)]*['\"][wax]")


def bash_writes(command: str, path: str, root: str) -> bool:
    """The Bash command writes and names the Base file `path` (relative to the clone at `root`): whole — as it is, or
    under the clone — or, for a command that `cd`s first, its name as a word of its own and each of its folders. A heuristic, on the
    safe side: it only decides between sessions that each saw the file change."""
    if not _WRITES.search(command):
        return False
    if f"{root}/{path}" in command or re.search(rf"(?<![\w./-]){re.escape(path)}(?![\w-])", command):
        return True
    *folders, name = path.split("/")
    return bool(folders) and re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", command) is not None and \
        all(re.search(rf"(?<![\w.-]){re.escape(folder)}(?![\w-])", command) for folder in folders)


def _bash_commands(row: dict) -> list[str]:
    """The Bash commands an assistant line of the transcript calls."""
    content = (row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None
    if row.get("type") != "assistant" or not isinstance(content, list):
        return []
    return [b["input"]["command"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and
            b.get("name") == "Bash" and isinstance(b.get("input"), dict) and isinstance(b["input"].get("command"), str)]

KEY = "base/"                 # a commit's «Undo» button: base/<card id>
UNDO = "undo"
AUTO = "auto"                 # the platform's own commits running (`_commits`)
TTL_S = 7 * 86400             # «Undo» lives as long as an approval's buttons
NAMED = 3                     # files named in the line under a ping; past them «+N more»
SUBJECT_CHARS = 120
WHY_CHARS = 1500              # the turn's summary in a commit's body: why the files changed, not the transcript
TITLE_CHARS = 70
DIFF_LIMIT = 1024 * 1024      # «Open diff»
COUNT_LIMIT = 256 * 1024      # one file's diff read to count its lines
COUNTED = 60                  # files whose +/− are counted; the rest show none
PRUNE_S = 60.0                # the lists checked against the clone at least this often while any is not empty
GONE = "This commit is no longer here."
NO_BASE = "The Base is not connected."
RUNNING = "The session is running now — wait for its result."
UNAVAILABLE = "The Base's clone is unavailable now — try later."
# What the platform tells a session about the Base, as a message of its own (`by="platform"`): the work is the
# session's — merging a file both sides edited, fixing what the Base's check refused — never the owner's.
MERGE_ASK = ("Message from the platform, not from the owner. While you were editing the Base, its hub changed the same "
             "lines of {files}, and the two edits do not merge by themselves. Each of these files now holds both "
             "versions between conflict markers («<<<<<<< this session», «=======», «>>>>>>> the hub»); the hub's side "
             "is the newer word, often the owner's own edit from another device. Merge each into one text that keeps "
             "the facts of both sides, remove every marker, keep the Base's rules (a Record's row in its Space's "
             "Showcase, below the marker), and say in one line what you merged. The commit follows as usual.")
REFUSED_ASK = ("Message from the platform, not from the owner. Your Base files were not committed after your turn:\n"
               "{lines}\nFix it in the files — a new Record needs its row in its Space's Showcase, below the marker; "
               "a file with conflict markers needs merging — and say in one line what you fixed. The commit follows "
               "after your turn, as usual.")
ASKED = "The session was asked to fix it."
CONFLICT = "the hub changed the same lines meanwhile"
NEEDS_YOU = "The session could not fix it: it needs you."


def plural(n: int, one: str, many: str) -> str:
    """«1 file», «2 files»."""
    return f"{n} {one if n == 1 else many}"


def files_word(n: int) -> str:
    return plural(n, "file", "files")


def named(paths: list[str]) -> str:
    """«a.md, b.md, c.md +2 more»: the first NAMED paths of a commit."""
    more = f" +{len(paths) - NAMED} more" if len(paths) > NAMED else ""
    return ", ".join(paths[:NAMED]) + more


def counted(diff: str) -> tuple[int, int]:
    """Lines added and removed in a unified diff, its headers left out."""
    add = rem = 0
    for row in diff.splitlines():
        if row.startswith("+") and not row.startswith("+++ "):
            add += 1
        elif row.startswith("-") and not row.startswith("--- "):
            rem += 1
    return add, rem


def short(path: str, cap: int = 40) -> str:
    """A path for a button: its end kept, where the file's name is."""
    return path if len(path) <= cap else "…" + path[-(cap - 1):]


def subject_of(text: str) -> str:
    """The owner's words as a commit subject: one line, the voice mark off, cut."""
    text = line(text, 10_000)
    return line(unmarked(text), SUBJECT_CHARS)


def why(summary: dict | None, limit: int = WHY_CHARS) -> str:
    """A summary's text for a commit's body: control characters out, cut at a line under `limit`."""
    text = summary.get("text") if isinstance(summary, dict) else None
    if not isinstance(text, str):
        return ""
    text = re.sub(r"[^\S\n]+\n", "\n", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "\n…"


class ToBase:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        self._lock = asyncio.Lock()          # one mark, prune or open at a time: they read and write the same rows
        self._pruned = 0.0
        self._commits: dict[str, asyncio.Task] = {}  # the commits running now, by card
        self._pending: set[str] = set()      # sessions whose run ended: their own commit waits for an idle lane
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS base_runs (
                run TEXT PRIMARY KEY,             -- a run of a session on the Base's lane, watched
                session TEXT NOT NULL,
                before TEXT,                      -- the tree's snapshot at its last mark: path -> digest, JSON;
                                                  -- NULL: it failed, the file tools alone count for that window
                last_raw INTEGER NOT NULL DEFAULT 0  -- the raw line (`raw.id`) its last mark read up to
            );
            CREATE TABLE IF NOT EXISTS base_cards (
                id TEXT PRIMARY KEY,              -- 8 hex; its «Undo» is base/<id>
                session TEXT NOT NULL,
                state TEXT NOT NULL,              -- committing | committed | failed | undone (older rows: expired)
                files TEXT NOT NULL,              -- [{path, status, add, del, on, digest}], JSON
                subject TEXT NOT NULL DEFAULT '', -- unused
                fallback TEXT NOT NULL,           -- the commit's subject: `<Space>: <title>`
                ping INTEGER,                     -- the session's latest ping when it was opened
                result TEXT NOT NULL DEFAULT '{}', -- {sha, files, refused, said, undone}
                created REAL NOT NULL,
                at REAL NOT NULL,
                auto INTEGER NOT NULL DEFAULT 0   -- the platform's own commit after a turn: «Undo», not «Commit»
            );
            CREATE INDEX IF NOT EXISTS base_cards_session ON base_cards (session, state);
            CREATE TABLE IF NOT EXISTS base_dirty (
                path TEXT PRIMARY KEY,            -- the clone's uncommitted paths as it last said (`_dirty`)
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS base_asks (
                session TEXT NOT NULL,            -- told what kept its files out of the Base (`_ask`)
                digest TEXT NOT NULL,             -- of those problems: the same ones again are the owner's
                at REAL NOT NULL,
                PRIMARY KEY (session, digest)
            );
            DROP TABLE IF EXISTS base_pings;
            """)
        if "auto" not in {r[1] for r in self.store.db.execute("PRAGMA table_info(base_cards)")}:
            self.store.db.execute("ALTER TABLE base_cards ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
        # The owner's «To the Base» card is gone: one left open from before is closed, never pressed again.
        self.store.db.execute("UPDATE base_cards SET state = 'expired' WHERE state = 'shown'")
        self.store.db.commit()
        self._recovering: asyncio.Task | None = None
        # A session's `base` count (its uncommitted Base files) only while a Base is configured.
        self.store.base_shown = lambda: self.base is not None

    # --- where the Base is ------------------------------------------------------------------------------------------

    @property
    def base(self):
        """The Base's clone (`base.BaseRepo`), or None when the platform has none configured."""
        return getattr(self.platform, "base", None)

    def lane(self) -> str:
        """The Base's lane: `base.lane` of `platform.yaml`."""
        cfg = getattr(self.platform.cfg, "base", None)
        return getattr(cfg, "lane", None) or "private"

    def watched(self, session: dict | None) -> bool:
        """A session that can change the Base: on its lane, whatever its folder (`/work` holds the clone), not a side
        question (its runs only read)."""
        return self.base is not None and session is not None and not session.get("parent") and \
            session.get("lane") == self.lane()

    def _root(self) -> str:
        """The clone's root as a run sees it."""
        return posixpath.join("/work", str(self.base.tree_rel).strip("/"))

    def _rel(self, path: str) -> str | None:
        """A path a file tool named, relative to the clone's root; None for one outside it."""
        root = self._root()
        path = posixpath.normpath(path) if posixpath.isabs(path) else ""
        return path[len(root) + 1:] if path.startswith(root + "/") else None

    # --- who changed what ---------------------------------------------------------------------------------------------

    async def before_start(self, session: dict) -> bool:
        """Before a run on the Base's lane starts (and before the Base is refreshed for it): False while a commit is
        in flight — the run must not change what is being staged; it stays queued, and the next drain (a poll on)
        tries again, so neither the drain nor the Base's `busy` ever waits on the other. Otherwise every mark owed is
        taken, so no earlier window takes in what this run does."""
        if not self.watched(session):
            return True
        if self._commits:
            return False
        await self.settle()
        return not self._commits  # a run that just ended has its own commit now: this one waits for it

    async def run_started(self, session: dict, run: str) -> None:
        """A run of a session in the clone is about to start: its first window opens on the tree as it is now."""
        if not self.watched(session):
            return
        async with self._lock:
            try:
                before = json.dumps(await asyncio.to_thread(self.base.snapshot))
            except Exception as exc:  # the type only: a name in the tree may say anything
                log.error("session %s: the Base's snapshot failed (%s); its file tools alone count", session["id"],
                          type(exc).__name__)
                before = None
            last = self.store.db.execute("SELECT COALESCE(MAX(id), 0) FROM raw").fetchone()[0]
            self.store.db.execute("INSERT OR REPLACE INTO base_runs (run, session, before, last_raw) "
                                  "VALUES (?, ?, ?, ?)", (run, session["id"], before, last))
            self.store.db.commit()

    async def settle(self) -> None:
        """Every mark owed — a watched run's new `result` line, or its end — taken: the window's difference recorded
        for its session; then the lists checked against the clone and the pings brought up to date. Cheap when nothing
        is owed: a few rows read, and the clone asked once a minute while any list is not empty."""
        if self.base is None:
            return
        async with self._lock:
            self._pending |= await self._settle()
        await self._merges()
        self._auto()

    async def _settle(self) -> set[str]:
        """The marks owed, taken; the sessions one of whose runs ended (their own commit is owed next)."""
        db, now = self.store.db, self.platform.clock()
        owed, ended_now = [], set()
        for run, session, last_raw in db.execute("SELECT run, session, last_raw FROM base_runs").fetchall():
            ended = db.execute("SELECT ended FROM runs WHERE id = ?", (run,)).fetchone()
            ended = ended is None or ended[0] is not None
            # Only the lines since its last mark are looked at (a range of `raw.id`): a long session costs nothing.
            if ended or db.execute("SELECT 1 FROM raw WHERE id > ? AND run = ? AND json_extract(line, '$.type') = "
                                   "'result'", (last_raw, run)).fetchone():
                owed.append((run, session, ended))
        if not owed and not (now - self._pruned >= PRUNE_S and db.execute("SELECT 1 FROM base_files").fetchone()):
            return ended_now
        touched: set[str] = set()
        if owed:
            try:
                after = await asyncio.to_thread(self.base.snapshot)
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's snapshot failed (%s); the marks wait for the next poll", type(exc).__name__)
                return ended_now
            for run, session, ended in owed:
                if ended and db.execute("SELECT 1 FROM raw WHERE run = ? AND session = ?", (run, session)).fetchone() \
                        is None:
                    db.execute("DELETE FROM base_runs WHERE run = ?", (run,))  # never started: nothing of its own
                    continue
                touched.add(session)
                touched |= self._mark(run, session, after, dirty, ended, now)
                if ended:
                    ended_now.add(session)
            db.commit()
        else:
            try:
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's state was not read (%s)", type(exc).__name__)
                return ended_now
        self._prune(dirty)
        return ended_now

    def _mark(self, run: str, session: str, after: dict, dirty: dict[str, str], ended: bool, now: float) -> set[str]:
        """The window's difference recorded for its session; the other sessions that lost a `snapshot` claim to it."""
        db = self.store.db
        before_text, last_raw = db.execute("SELECT before, last_raw FROM base_runs WHERE run = ?", (run,)).fetchone()
        before = json.loads(before_text) if before_text is not None else None
        by_snapshot = set() if before is None else {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        scan, upto, commands = changes.Scan(), last_raw, []
        for raw_id, text in db.execute("SELECT id, line FROM raw WHERE id > ? AND run = ? ORDER BY id",
                                       (last_raw, run)):
            scan.line(row := parsed(text))
            commands += _bash_commands(row)
            upto = raw_id
        by_tools = {rel for c in scan.changes if (rel := self._rel(c.path)) is not None}
        root, lost = self._root(), set()
        for path in (by_snapshot | by_tools) & dirty.keys():
            if path in by_tools:
                via = "both" if path in by_snapshot else "tool"
            else:
                via = "bash" if any(bash_writes(c, path, root) for c in commands) else "snapshot"
            if via == "snapshot" and db.execute("SELECT 1 FROM base_files WHERE path = ? AND session != ? AND "
                                                "via != 'snapshot'", (path, session)).fetchone():
                continue  # another session wrote it: this one only saw it change
            row = db.execute("SELECT via FROM base_files WHERE session = ? AND path = ?", (session, path)).fetchone()
            if row is None:
                db.execute("INSERT INTO base_files (session, path, via, at) VALUES (?, ?, ?, ?)", (session, path, via, now))
            elif row[0] not in (via, "both") and via != "snapshot":
                db.execute("UPDATE base_files SET via = ? WHERE session = ? AND path = ?",
                           (via if row[0] == "snapshot" else "both", session, path))
            if via != "snapshot":
                for (other,) in db.execute("SELECT session FROM base_files WHERE path = ? AND session != ? AND "
                                           "via = 'snapshot'", (path, session)).fetchall():
                    db.execute("DELETE FROM base_files WHERE session = ? AND path = ?", (other, path))
                    lost.add(other)
        if ended:
            db.execute("DELETE FROM base_runs WHERE run = ?", (run,))
        else:
            db.execute("UPDATE base_runs SET before = ?, last_raw = ? WHERE run = ?", (json.dumps(after), upto, run))
        for each in {session} | lost:
            self.store._session_event(each)
        return lost

    async def _dirty(self) -> dict[str, str]:
        """The clone's uncommitted paths and their A/M/D, as it says now — kept (`base_dirty`), so the feed can name
        the files no session claims without asking the clone."""
        dirty = {c.path: c.status for c in await asyncio.to_thread(self.base.changed)}
        db = self.store.db
        db.execute("DELETE FROM base_dirty")
        db.executemany("INSERT INTO base_dirty (path, status) VALUES (?, ?)", sorted(dirty.items()))
        db.commit()
        return dirty

    def _prune(self, dirty: dict[str, str]) -> set[str]:
        """Every list loses what is no longer uncommitted — committed by any button, or refreshed to the hub's
        content. The sessions whose list changed."""
        db = self.store.db
        self._pruned = self.platform.clock()
        gone = [(s, p) for s, p in db.execute("SELECT session, path FROM base_files") if p not in dirty]
        db.executemany("DELETE FROM base_files WHERE session = ? AND path = ?", gone)
        sessions = {s for s, _ in gone}
        for session in sessions:
            self.store._session_event(session)
        db.commit()
        return sessions

    def paths(self, session: str) -> list[str]:
        return [p for (p,) in self.store.db.execute("SELECT path FROM base_files WHERE session = ? ORDER BY path",
                                                    (session,))]

    def count(self, session: str) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM base_files WHERE session = ?", (session,)).fetchone()[0]

    # --- the ping's button ----------------------------------------------------------------------------------------

    def _ping_button(self, seq: int, button: list | None, field: str = "base", **more) -> None:
        """The ping's summary note with `field` set (or taken off) — `undo`, the platform's own commit's «Undo» —
        and `more` (`base_line`: what the turn wrote to the Base), and a `ping` note that rewrites its message."""
        row = self.store.db.execute("SELECT session, data FROM notes WHERE seq = ? AND kind = 'summary'",
                                    (seq,)).fetchone()
        if row is None:
            return
        data = {**json.loads(row[1]), field: button, **more}
        self.store.db.execute("UPDATE notes SET data = ? WHERE seq = ?", (json.dumps(data, ensure_ascii=False), seq))
        self.store._note("ping", row[0], {**data, "seq": seq})

    # --- the card -------------------------------------------------------------------------------------------------

    CARD = ("id", "session", "state", "files", "subject", "fallback", "ping", "result", "created", "at", "auto")

    def card(self, card_id: str) -> dict | None:
        row = self.store.db.execute(f"SELECT {', '.join(self.CARD)} FROM base_cards WHERE id = ?",
                                    (card_id,)).fetchone()
        if row is None:
            return None
        card = dict(zip(self.CARD, row))
        card["files"], card["result"] = json.loads(card["files"]), json.loads(card["result"])
        return card

    def cards(self, session: str, states: tuple[str, ...] = ()) -> list[dict]:
        where = f" AND state IN ({', '.join('?' * len(states))})" if states else ""
        ids = self.store.db.execute(f"SELECT id FROM base_cards WHERE session = ?{where} ORDER BY created, rowid",
                                    (session, *states)).fetchall()
        return [self.card(card_id) for (card_id,) in ids]

    async def _files(self, paths: list[str], dirty: dict[str, str], snapshot: dict) -> list[dict]:
        """Each path with its A/M/D, its +/− lines (the first COUNTED files) and its digest as the card shows it."""
        out = []
        for n, path in enumerate(paths):
            add = rem = None
            if n < COUNTED:
                try:
                    add, rem = counted(await asyncio.to_thread(self.base.diff, [path], COUNT_LIMIT))
                except Exception as exc:
                    log.warning("the Base's diff of one file failed (%s)", type(exc).__name__)
            out.append({"path": path, "status": dirty.get(path, "M"), "add": add, "del": rem, "on": True,
                        "digest": snapshot.get(path)})
        return out

    def _new(self, session: dict, files: list[dict], now: float) -> dict:
        title = line((self.store.face_session(session["id"]) or {}).get("title") or session["track"], TITLE_CHARS)
        first = files[0]["path"]
        space = first.split("/", 1)[0] if "/" in first else ""
        latest = self.store.db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?",
                                       (session["id"],)).fetchone()[0]
        card = {"id": secrets.token_hex(4), "session": session["id"], "state": "committing", "files": files,
                "subject": "", "fallback": line(f"{space}: {title}" if space else title, SUBJECT_CHARS), "ping": latest,
                "result": {}, "created": now, "at": now, "auto": 1}
        self.store.db.execute(f"INSERT INTO base_cards ({', '.join(self.CARD)}) VALUES "
                              f"({', '.join('?' * len(self.CARD))})", self._row(card))
        return card

    @staticmethod
    def _row(card: dict) -> tuple:
        return (card["id"], card["session"], card["state"], json.dumps(card["files"], ensure_ascii=False),
                card["subject"], card["fallback"], card["ping"], json.dumps(card["result"], ensure_ascii=False),
                card["created"], card["at"], card["auto"])

    def _save(self, card: dict, now: float, states: tuple[str, ...]) -> bool:
        """The card written back while it is still in one of `states`; True when it was."""
        cursor = self.store.db.execute(
            f"UPDATE base_cards SET state = ?, files = ?, subject = ?, result = ?, at = ? WHERE id = ? AND state IN "
            f"({', '.join('?' * len(states))})",
            (card["state"], json.dumps(card["files"], ensure_ascii=False), card["subject"],
             json.dumps(card["result"], ensure_ascii=False), now, card["id"], *states))
        card["at"] = now
        return cursor.rowcount == 1

    def _show(self, card: dict) -> None:
        """The card's new state to the app (an event `base`)."""
        self.store._event("base", card["session"], card["id"], self.face(card))
        self.store.db.commit()

    def face(self, card: dict) -> dict:
        """The platform's own commit as the app shows it: one line, its files, its sha, «Undo» while it can."""
        result = card["result"]
        return {"id": card["id"], "session": card["session"], "state": card["state"],
                "files": [{"path": f["path"], "status": f["status"], "add": f["add"], "del": f["del"]}
                          for f in card["files"]],
                "subject": card["fallback"], "sha": result.get("sha"), "said": result.get("said") or "",
                "refused": result.get("refused") or [], "committed": result.get("files") or [],
                "created": card["created"], "at": card["at"], "undone": result.get("undone"),
                "undo": self._undoable(card) is None}

    def _finish(self, card: dict, state: str, result: dict, now: float, states: tuple[str, ...]) -> bool:
        card = {**card, "state": state, "result": result}
        if not self._save(card, now, states):
            return False
        self._show(card)
        return True

    # --- what the owner does: «Undo», in Telegram (`press`) and in the app (face.py) -----------------------------------

    async def press(self, bound: dict) -> dict:
        """«Undo» on a ping. A press on an older ping's «To the Base · N» says it is gone."""
        card = self.card(bound["approval"][len(KEY):])
        if card is None or bound["action"] != UNDO:
            return {"toast": GONE}
        toast, _ = await self.undo(card)
        return {"toast": toast}

    async def diff(self, card: dict) -> str:
        """The bounded diff of what the commit changed; of one that did not go, its files as the clone has them now."""
        try:
            if self.base is not None and card["result"].get("sha"):
                return await asyncio.to_thread(self.base.shown, card["result"]["sha"], DIFF_LIMIT)
            paths = [f["path"] for f in card["files"]]
            return await asyncio.to_thread(self.base.diff, paths, DIFF_LIMIT) if self.base is not None else ""
        except Exception as exc:
            return f"{UNAVAILABLE}\n" if isinstance(exc, OSError) else f"The diff failed: {type(exc).__name__}\n"

    # --- the platform's own commit after a turn, and its «Undo» ------------------------------------------------------

    def _lane_open(self) -> bool:
        """A run is open on the Base's lane, or one has ended and its last window is not marked yet: until then, whose
        a changed file is is not known."""
        return self.store.db.execute("SELECT 1 FROM runs r JOIN sessions s ON s.id = r.session WHERE r.ended IS NULL "
                                     "AND s.lane = ? UNION ALL SELECT 1 FROM base_runs LIMIT 1",
                                     (self.lane(),)).fetchone() is not None

    def _auto(self) -> None:
        """The sessions whose run ended get their own commits, one by one in the background — once no run is open on
        the lane, so every window is marked and a file two runs touched is known as both's (never committed by
        itself). A restart in between leaves their files to the button."""
        if self.base is None or not self._pending or AUTO in self._commits or self._lane_open():
            return
        self._commits[AUTO] = asyncio.create_task(self._autocommits())

    def _body(self, session_id: str, where: str) -> str:
        """A commit's body: the session's latest summary — what its turn did and why, so `git log` of a Record answers
        why it changed — and the line naming the session and that turn, with a link to the turn in the app."""
        summary = self.store.last_summary(session_id)
        turn = summary.get("turn") if isinstance(summary, dict) else None
        named = f"Retinue: session {session_id}" + (f", {where} {turn}." if isinstance(turn, int) else ".")
        origin = getattr(self.platform.cfg, "face_origin", "")
        if origin and isinstance(turn, int):
            named += f"\n{origin}/s/{session_id}?turn={turn}"
        text = why(summary)
        return f"{text}\n\n{named}" if text else named

    async def _autocommits(self) -> None:
        try:
            while self._pending and not self._lane_open():
                await self._autocommit(self._pending.pop())
        finally:
            self._commits.pop(AUTO, None)
        await self.settle_lists()
        await self.platform.drain()  # a run the commits held back goes now

    async def _autocommit(self, session_id: str) -> None:
        """Everything the session changed in the Base — added, edited, deleted, shared with another session — and the
        files no session claims, committed and pushed as the owner's own (his rules; what the platform never commits,
        `protect` and hidden paths, stays out). The line under the session's latest ping says what went where, or why
        nothing did and what came next. Runs on the lane wait for it (`before_start`)."""
        card = None
        try:
            async with self._lock:
                session = self.store.session(session_id)
                if session is None:
                    return
                dirty = await self._dirty()
                snapshot = await asyncio.to_thread(self.base.snapshot)
                self._prune(dirty)
                claimed = {p for (p,) in self.store.db.execute("SELECT DISTINCT path FROM base_files")}
                paths = [p for p in self.paths(session_id) if p in dirty] + \
                    [p for p in sorted(dirty) if p not in claimed]
                if not paths:
                    return
                card = self._new(session, await self._files(paths, dirty, snapshot), self.platform.clock())
                self.store.db.commit()
                self._commits[card["id"]] = self._commits[AUTO]  # `tick` sees it is running
            done = await self.base.commit(paths, card["fallback"], self._body(session_id, "after turn"),
                                          expect={f["path"]: f["digest"] for f in card["files"]}, session=session_id,
                                          role=OWNER)
            await self._committed(card, done)
        except Exception as exc:  # the type only: a path or a hook's line may say anything
            log.error("session %s: the platform's own commit failed: %s", session_id, type(exc).__name__)
            if card is not None:
                self.store.db.execute("DELETE FROM base_cards WHERE id = ? AND state = 'committing'", (card["id"],))
                self.store.db.commit()
        finally:
            if card is not None:
                self._commits.pop(card["id"], None)

    async def _committed(self, card: dict, done) -> None:
        """The commit's answer, said in one line under the session's latest ping and on its card in the app: what
        went where («Undo» beside it), and what did not, why, and that the session was asked to fix it — or, asked
        once about the same thing already, that it needs the owner. Nothing to say: the card is forgotten."""
        db = self.store.db
        went = [c.path for c in done.files] if done.sha else []
        problems = [*(f"{line(c, 200)}: {CONFLICT}" for c in done.conflicts or []),
                    *(line(r, 200) for r in done.refused or [])]
        if not went and not problems:
            db.execute("DELETE FROM base_cards WHERE id = ?", (card["id"],))
            db.commit()
            return
        said = f"Written to the Base: {named(went)} · {str(done.sha)[:7]}." if went else ""
        if problems:
            more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
            asked = await self._ask(card["session"], problems)
            said = f"{said} Not written: {problems[0]}{more}. {ASKED if asked else NEEDS_YOU}".strip()
        else:
            db.execute("DELETE FROM base_asks WHERE session = ?", (card["session"],))
        if went:
            card = {**card, "files": [f for f in card["files"] if f["path"] in went]}
        latest = db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?",
                            (card["session"],)).fetchone()[0]
        result = {**self._result(done), "ping": latest, "said": said}
        self._finish(card, "committed" if went else "failed", result, self.platform.clock(), ("committing",))
        if latest is not None:
            button = [f"Undo · {len(went)} in the Base",
                      self.store.add_button(KEY + card["id"], UNDO, card["id"], card["created"] + TTL_S)] if went else None
            self._ping_button(latest, button, "undo", auto=card["id"], base_line=said)
        db.commit()

    async def _ask(self, session_id: str, problems: list[str]) -> bool:
        """The session told what kept its files out, once for the same problems: True when told now; False when it
        was told before and they are still there — the owner's then."""
        digest = hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()
        db = self.store.db
        if db.execute("SELECT 1 FROM base_asks WHERE session = ? AND digest = ?", (session_id, digest)).fetchone():
            return False
        db.execute("INSERT INTO base_asks (session, digest, at) VALUES (?, ?, ?)",
                   (session_id, digest, self.platform.clock()))
        db.commit()
        return await self._tell(session_id, REFUSED_ASK.format(lines="\n".join(f"- {p}" for p in problems)))

    def _undoable(self, card: dict) -> str | None:
        """Why the card's «Undo» cannot be pressed now; None when it can."""
        if not card["result"].get("sha"):
            return GONE
        if card["state"] == "undone":
            return "Already undone."
        if card["state"] != "committed":
            return GONE
        if self.platform.clock() > card["created"] + TTL_S:
            return "Too old to undo here — undo it from the Mac."
        return None

    async def undo(self, card: dict) -> tuple[str, dict | None]:
        """«Undo» of the platform's own commit: its files put back as they were before it, in a new commit — each
        only if neither the hub nor the clone changed it since — while the session is not running."""
        if (why := self._undoable(card)) is not None:
            return why, None
        if self.base is None:
            return NO_BASE, None
        if any(r["session"] == card["session"] for r in self.store.open_runs()):
            return RUNNING, None
        if card["id"] in self._commits:
            return "Undoing — wait.", None
        task = asyncio.create_task(self._undo(card))
        self._commits[card["id"]] = task  # no run on the lane starts meanwhile
        return await asyncio.shield(task)  # a request cut off midway never cuts the undo off

    async def _undo(self, card: dict) -> tuple[str, dict | None]:
        try:
            try:
                done = await self.base.revert(card["result"]["sha"], [f["path"] for f in card["files"]],
                                              card["fallback"], card["session"])
            except Exception as exc:
                log.error("card %s: the undo failed: %s", card["id"], type(exc).__name__)
                return (UNAVAILABLE if isinstance(exc, OSError) else f"The undo failed ({type(exc).__name__})."), None
            return await self._undone(card, done)
        finally:
            self._commits.pop(card["id"], None)
            await self.platform.drain()

    async def _undone(self, card: dict, done) -> tuple[str, dict | None]:
        refused = [line(r, 200) for r in done.refused]
        if not done.sha:
            return "Nothing undone" + (f": {refused[0]}" if refused else "."), None
        said = f"Undone: {str(done.sha)[:7]} puts back {files_word(len(done.files))}."
        noted = self.store.db.execute("SELECT data FROM notes WHERE seq = ?", (card["result"].get("ping"),)).fetchone()
        turn = json.loads(noted[0]).get("turn") if noted else None  # the turn whose ping the card was pressed under
        if not isinstance(turn, int):  # a card with no ping of its own: the session's last summary
            turn = (self.store.last_summary(card["session"]) or {}).get("turn")
        if isinstance(turn, int):
            episodes.mark(self.store, card["session"], episodes.BASE_UNDO, self.platform.clock(), turn=turn)
        if refused:
            said += " Left as they are:\n" + "\n".join(f"• {r}" for r in refused)
        self._finish(card, "undone", {**card["result"], "undone": done.sha, "said": said, "refused": refused},
                     self.platform.clock(), ("committed",))
        if (seq := card["result"].get("ping")) is not None:
            self._ping_button(seq, None, "undo")
            self.store.db.commit()
        await self.settle_lists()
        return said.split("\n")[0], self.card(card["id"])

    @staticmethod
    def _result(done) -> dict:
        """The commit's answer (`base.Committed`) as the card says it."""
        files = [f"{c.status} {c.path}" for c in (done.files or [])]
        refused = [*(f"conflict: {line(c, 200)}" for c in done.conflicts or []),
                   *(line(r, 200) for r in done.refused or []),
                   *(f"not in the Base: {line(p, 200)}" for p in done.left_out or [])]
        if done.sha:
            said = f"Committed: {str(done.sha)[:7]} · {files_word(len(files))}."
        else:
            said = "Nothing committed."
        if refused:
            said += " Left out, still uncommitted:\n" + "\n".join(f"• {r}" for r in refused)
        return {"sha": done.sha, "files": files, "refused": refused, "said": said}

    async def settle_lists(self) -> None:
        """After a commit: every list checked against the clone now; the files it marked told to their sessions."""
        async with self._lock:
            try:
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's state was not read (%s)", type(exc).__name__)
                return
            self._prune(dirty)
        await self._merges()

    async def _merges(self) -> None:
        """The files the clone wrote with conflict markers (`base.take_marked`) go to the sessions that changed them,
        one message each with its files: a session that wrote the file is told, else one that saw it change. A file
        no session claims keeps its markers, and no commit takes it until someone merges it."""
        take = getattr(self.base, "take_marked", None)
        if take is None:
            return
        try:
            paths = await asyncio.to_thread(take)
        except Exception as exc:
            log.error("the Base's marked files were not read (%s)", type(exc).__name__)
            return
        told: dict[str, list[str]] = {}
        for path in paths:
            claims = self.store.db.execute("SELECT session, via FROM base_files WHERE path = ?", (path,)).fetchall()
            wrote = [s for s, via in claims if via != "snapshot"] or [s for s, _ in claims]
            if not wrote:
                log.warning("base: a file holds conflict markers and no session claims it")
            for session in wrote:
                told.setdefault(session, []).append(path)
        for session, files in told.items():
            await self._tell(session, MERGE_ASK.format(files=", ".join(files)))

    async def _tell(self, session_id: str, text: str) -> bool:
        """A message from the platform into the session, as a parent's goes: the session takes it up in its next run."""
        session = self.store.session(session_id)
        if session is None:
            return False
        try:
            await self.platform.write_into(f"base:{secrets.token_hex(8)}", session, text, by="platform")
        except Exception as exc:
            log.error("session %s: the platform's message was not written (%s)", session_id, type(exc).__name__)
            return False
        return True

    async def idle(self) -> None:
        """The commits running now, finished (tests, shutdown)."""
        while self._commits:
            await asyncio.gather(*self._commits.values(), return_exceptions=True)

    # --- the clock and a restart ------------------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        """A card left `committing` with no commit running is settled again (`recover`) — the Base may have been
        unreadable, or off, when the platform started."""
        stuck = [c for (c,) in self.store.db.execute("SELECT id FROM base_cards WHERE state = 'committing'")
                 if c not in self._commits]
        if stuck and self.base is not None and (self._recovering is None or self._recovering.done()):
            try:
                self._recovering = asyncio.get_running_loop().create_task(self.recover())
            except RuntimeError:  # no loop: called outside the platform's own clock
                pass

    async def recover(self) -> None:
        """A commit a restart cut: what of it the clone no longer has uncommitted went in; the rest goes with the
        session's next turn."""
        if self.base is None:
            return
        left = [c for (card_id,) in self.store.db.execute("SELECT id FROM base_cards WHERE state = 'committing'")
                if card_id not in self._commits and (c := self.card(card_id)) is not None]
        if not left:
            return
        try:
            dirty = await self._dirty()
        except Exception as exc:
            log.error("the commits a restart cut were not settled (%s)", type(exc).__name__)
            return
        now = self.platform.clock()
        for card in left:
            went = [f["path"] for f in card["files"] if f["path"] not in dirty]
            if went:
                self._finish(card, "committed", {"sha": None, "files": went,
                                                 "said": f"Written to the Base before the platform restarted: "
                                                         f"{named(went)}."}, now, ("committing",))
            else:
                self.store.db.execute("DELETE FROM base_cards WHERE id = ?", (card["id"],))
                self.store.db.commit()
        await self.settle_lists()

    # --- the app and the feed ---------------------------------------------------------------------------------------

    def face_cards(self, session: str) -> list[dict]:
        """A session's last few commits for its snapshot in the app."""
        return [self.face(c) for c in self.cards(session)[-5:]]
