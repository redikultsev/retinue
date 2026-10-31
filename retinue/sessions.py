"""The platform's own SQLite: its sessions and their runs, the runs queued for room on a lane, what the owner wrote
into them, every raw line the lanes journaled, the transcript projected from those lines, the approvals with the
buttons bound to them, and the notes the router takes by number (the journal pattern of `tgbusiness.State`).

A raw line is stored once — `UNIQUE(lane, lane_seq)` makes a lane's replay after a reconnect a no-op — and is
numbered into its session's turns as it arrives: a `result` closes a turn, and so does a new run of the session
(a process that ended without one). The number is the session's, not recomputed from the lines still there, so
pruning old raw lines never renumbers the turns whose projection stays.

The face (`face.py`) reads the same rows as a snapshot and then follows `events`: every method here that changes
what the face shows — a session row, a turn's items, an approval, a summary — writes an upsert of that whole object
in the same transaction as the change, so a client that applies the events in order after its snapshot converges on
the snapshot it would read now. The store also keeps the face's logins: one-time codes and cookies, both hashed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from . import changes, transcript
from .spoken import unmarked

RAW_KEEP_S = 90 * 86400
# A platform button id starts so, and the router routes on it; a core button id (token_urlsafe) never holds a dot.
BUTTON_PREFIX = "p."
EVENTS_KEEP_S = 7 * 86400   # a face away longer reloads its snapshot (410 on the stream)
FACE_CLOSED_S = 30 * 86400  # how long a closed session stays in the face's list
TITLE_LEN = 80
# What the face sees of an approval: everything but the run and the file hash the digest holds.
FACE_APPROVAL = ("id", "session", "tool_use_id", "kind", "state", "tool", "input", "cwd", "digest", "view", "created",
                 "at")


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


class PlatformStore:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.clock = clock  # an event's `at`; the platform gives its own
        self.listener: Callable[[], None] | None = None  # told of every event written: the face's open streams
        self.base_shown: Callable[[], bool] = lambda: True  # `tobase`: a session's `base` count only with a Base on
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,                -- 8 hex
                track TEXT NOT NULL,
                lane TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- code | private
                state TEXT NOT NULL,                -- active | waiting | idle | closed
                cli_session TEXT NOT NULL DEFAULT '',
                cli_version TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                at REAL NOT NULL,
                idle_since REAL,
                turn INTEGER NOT NULL DEFAULT 0,    -- the turn the next raw line goes into
                turn_run TEXT,                      -- the run whose lines the open turn holds; NULL: none yet
                pinned INTEGER NOT NULL DEFAULT 0,  -- the owner keeps it on top of the face's list
                parent TEXT,                        -- a side session: the session its question is about
                context_tokens INTEGER,             -- what the main conversation's last request carried
                sphere TEXT,                        -- the owner's sphere of life it belongs to (`config.Sphere`)
                main INTEGER NOT NULL DEFAULT 0,    -- the sphere's main session: at most one (`sessions_main`)
                fork_of TEXT,                       -- a fork: the session whose conversation it started from
                cwd TEXT,                           -- its track's directory when it started; NULL: from before
                seen_at REAL,                       -- when the owner last looked at it or wrote into it
                news_at REAL,                       -- when it last said something new: a summary, a card
                turn_started REAL,                  -- when its running turn began: idle or closed -> active
                sub_of TEXT,                        -- a sub-session: the session that started it (`subs`)
                title TEXT,                         -- a title given at its start (a sub-session's); NULL: its first input
                turn_by TEXT,                       -- who started its current turn: owner | parent | report (`subs`)
                sphere_by TEXT,                     -- who set its sphere last: owner | session; NULL: its start
                hops INTEGER NOT NULL DEFAULT 0,    -- a parent: child<->parent exchanges since the owner last wrote
                owner_hold TEXT,                    -- a sub-session the owner stopped or wrote into: stopped | wrote;
                                                    -- its parent may not write to it until he writes to the parent
                owner_joined INTEGER NOT NULL DEFAULT 0, -- the owner wrote into its current turn (its ping is his)
                stopped_run TEXT,                   -- the run of it the owner stopped: what it says is no report
                archived REAL                       -- when the owner archived it: out of the face's list until a turn
                                                    -- of it begins again
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                session TEXT NOT NULL,
                hook_token_hash TEXT NOT NULL UNIQUE,
                marks TEXT NOT NULL,                -- approvals.Mark of each marker hook n, JSON
                started REAL NOT NULL,
                ended REAL,
                mode TEXT                           -- the permission mode its init line named; NULL: not seen yet
            );
            CREATE TABLE IF NOT EXISTS interrupted (
                run TEXT PRIMARY KEY,               -- a run the platform itself stopped while it shut down
                at REAL NOT NULL,
                resumed REAL                        -- when its session was resumed for it: once
            );
            CREATE TABLE IF NOT EXISTS inputs (
                input_id TEXT PRIMARY KEY,          -- the router's: tg:<message id>
                session TEXT NOT NULL,
                text TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS input_thumbs (
                input_id TEXT NOT NULL,
                n INTEGER NOT NULL,                 -- the attachment's place in the input's `attachments`
                jpeg BLOB NOT NULL,                 -- inbox.thumbnail: what the face's transcript shows of a picture
                PRIMARY KEY (input_id, n)
            );
            CREATE TABLE IF NOT EXISTS answer_inputs (
                input_id TEXT PRIMARY KEY,          -- the router's tg:<message id> of a reply taken as a question's answer
                approval TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw (
                id INTEGER PRIMARY KEY,
                lane TEXT NOT NULL,
                lane_seq INTEGER NOT NULL,
                session TEXT NOT NULL,
                run TEXT NOT NULL,
                turn INTEGER NOT NULL,
                line TEXT NOT NULL,
                at REAL NOT NULL,
                UNIQUE (lane, lane_seq)
            );
            CREATE INDEX IF NOT EXISTS raw_session ON raw (session, turn);
            CREATE TABLE IF NOT EXISTS items (
                session TEXT NOT NULL,
                turn INTEGER NOT NULL,
                n INTEGER NOT NULL,
                kind TEXT NOT NULL,
                data TEXT NOT NULL,
                first_raw INTEGER NOT NULL,
                last_raw INTEGER NOT NULL,
                PRIMARY KEY (session, turn, n)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,                -- 8 hex
                session TEXT NOT NULL,
                run TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- approval | courier | question
                tool TEXT NOT NULL,
                input TEXT NOT NULL,
                cwd TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,                -- pending | shown | granted | executing | executed | rejected
                                                    -- | stale | dropped | expired; a question: answered, not granted
                view TEXT NOT NULL DEFAULT '{}',    -- the card as shown: courier.CardView
                created REAL NOT NULL,
                at REAL NOT NULL,
                why TEXT NOT NULL DEFAULT ''        -- why it came to the owner: the classifier's reason, a card rule
            );
            CREATE INDEX IF NOT EXISTS approvals_call ON approvals (tool_use_id);
            CREATE TABLE IF NOT EXISTS buttons (
                id TEXT PRIMARY KEY,                -- BUTTON_PREFIX + token_urlsafe(12)
                approval TEXT NOT NULL,             -- a ping's reply: ping/<seq> (pings)
                action TEXT NOT NULL,               -- approve | reject | open; a question's q<n>:<option> | q<n>:done;
                                                    -- a ping's reply:<n>
                digest TEXT NOT NULL,               -- a ping's reply: its session
                expires REAL NOT NULL,
                used REAL
            );
            CREATE TABLE IF NOT EXISTS notes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                 -- card | update | summary | ping (pings) | file | file_skipped (outfiles)
                session TEXT NOT NULL,
                approval TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL,
                given INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL,
                prompt TEXT,                        -- NULL: a resume after the owner's decision
                content TEXT,                       -- JSON, with attachments: inbox.Ready.lane — blocks, files
                at REAL NOT NULL,
                attempt TEXT,                       -- the run sent for it whose answer was lost: the lane's journal
                                                    -- tells whether it started
                by TEXT                             -- who it is from: owner (NULL) | parent | report (`subs`)
            );
            CREATE INDEX IF NOT EXISTS inputs_session ON inputs (session, at);
            CREATE INDEX IF NOT EXISTS approvals_session ON approvals (session, state);
            CREATE INDEX IF NOT EXISTS notes_session ON notes (session, kind);
            CREATE INDEX IF NOT EXISTS buttons_approval ON buttons (approval);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,                          -- session | item | items_reset | approval | summary | file
                session TEXT,
                key TEXT,                           -- the object the event replaces whole
                data TEXT,
                at REAL
            );
            CREATE TABLE IF NOT EXISTS login_codes (
                hash TEXT PRIMARY KEY,              -- SHA-256 of the code the bot gave the owner
                expires REAL NOT NULL,
                used REAL
            );
            CREATE TABLE IF NOT EXISTS logins (
                hash TEXT PRIMARY KEY,              -- SHA-256 of the cookie
                via TEXT NOT NULL,                  -- code | telegram
                created REAL NOT NULL,
                expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_failures (at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- `speech`: stt.pending
            CREATE TABLE IF NOT EXISTS base_files (
                session TEXT NOT NULL,              -- a session whose runs changed this Base file (`tobase`)
                path TEXT NOT NULL,                 -- relative to the clone's root; uncommitted while it is here
                via TEXT NOT NULL,                  -- snapshot | tool | both: what saw the change
                at REAL NOT NULL,
                PRIMARY KEY (session, path)
            );
            CREATE TABLE IF NOT EXISTS push_touched (
                session TEXT NOT NULL,              -- a session whose runs worked in this repository (`topush`)
                repo TEXT NOT NULL,
                at REAL NOT NULL,
                PRIMARY KEY (session, repo)
            );
            CREATE TABLE IF NOT EXISTS push_repos (
                repo TEXT PRIMARY KEY,              -- the room's clone of it as its last look found it (`topush`)
                branch TEXT NOT NULL,
                sha TEXT NOT NULL,
                n INTEGER NOT NULL,                 -- its commits no branch of the remote has: «Push · N»
                at REAL NOT NULL
            );
            """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        for column, kind in (("pinned", "INTEGER NOT NULL DEFAULT 0"), ("parent", "TEXT"),
                             ("context_tokens", "INTEGER"), ("sphere", "TEXT"), ("main", "INTEGER NOT NULL DEFAULT 0"),
                             ("fork_of", "TEXT"), ("seen_at", "REAL"), ("news_at", "REAL"), ("cwd", "TEXT"),
                             ("turn_started", "REAL"), ("sub_of", "TEXT"), ("title", "TEXT"), ("turn_by", "TEXT"),
                             ("sphere_by", "TEXT"), ("hops", "INTEGER NOT NULL DEFAULT 0"), ("owner_hold", "TEXT"),
                             ("owner_joined", "INTEGER NOT NULL DEFAULT 0"), ("stopped_run", "TEXT"),
                             ("archived", "REAL")):
            if column not in columns:  # a database from before the column
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {column} {kind}")
        for table, column, kind in (("runs", "mode", "TEXT"), ("approvals", "why", "TEXT NOT NULL DEFAULT ''"),
                                    ("queue", "content", "TEXT"), ("queue", "by", "TEXT"), ("inputs", "by", "TEXT"),
                                    ("inputs", "attachments", "TEXT NOT NULL DEFAULT '[]'"),
                                    ("inputs", "turn", "INTEGER"), ("inputs", "n", "INTEGER"),
                                    ("push_repos", "why", "TEXT NOT NULL DEFAULT ''")):
            if column not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        # One main session per sphere, held by SQLite itself: two `/travel` racing cannot both make one.
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_main ON sessions (sphere) WHERE main = 1")
        self.db.commit()

    # --- sessions and runs --------------------------------------------------------------------------------------

    SESSION = ("id", "track", "lane", "kind", "state", "cli_session", "cli_version", "created", "at", "idle_since",
               "turn", "parent", "context_tokens", "sphere", "main", "fork_of", "cwd", "sub_of", "title", "turn_by",
               "sphere_by", "hops", "owner_hold", "owner_joined", "stopped_run", "archived")

    def new_session(self, track: str, lane: str, kind: str, now: float, parent: str | None = None,
                    sphere: str | None = None, fork_of: str | None = None, main: bool = False,
                    cwd: str | None = None, sub_of: str | None = None, title: str | None = None) -> str:
        """`main`: made its sphere's main session in the same INSERT when the sphere has none (`sessions_main`), so a
        message that comes while its first run starts already finds it; otherwise an ordinary session of the sphere.
        `cwd`: the track's directory as it was configured then — a fork must run where its parent ran. `sub_of`:
        a sub-session's parent session, `title` the one it was given (`subs`)."""
        session = secrets.token_hex(4)
        self.db.execute("INSERT INTO sessions (id, track, lane, kind, state, created, at, parent, sphere, fork_of, "
                        "main, cwd, turn_started, sub_of, title, turn_by) SELECT ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, "
                        "? AND NOT EXISTS (SELECT 1 FROM sessions WHERE sphere = ? AND main = 1), ?, ?, ?, ?, ?",
                        (session, track, lane, kind, now, now, parent, sphere, fork_of, int(main and sphere is not None),
                         sphere, cwd, now, sub_of, title, "parent" if sub_of else "owner"))
        self._session_event(session)
        self.db.commit()
        return session

    # --- spheres: the owner's grouping, a main session per sphere, what he has not seen yet ----------------------

    def main_of(self, sphere: str) -> str | None:
        row = self.db.execute("SELECT id FROM sessions WHERE sphere = ? AND main = 1", (sphere,)).fetchone()
        return row[0] if row else None

    def claim_main(self, session: str) -> bool:
        """The session becomes its sphere's main one if the sphere has none: of two racing claims one wins."""
        cursor = self.db.execute("UPDATE sessions SET main = 1 WHERE id = ? AND sphere IS NOT NULL AND NOT EXISTS "
                                 "(SELECT 1 FROM sessions m WHERE m.sphere = sessions.sphere AND m.main = 1)",
                                 (session,))
        if cursor.rowcount == 1:
            self._session_event(session)
        self.db.commit()
        return cursor.rowcount == 1

    def set_main(self, session: str, main: bool) -> bool:
        """The owner's mark: made main, the sphere's previous main session is main no more (in one transaction);
        unmarked, the sphere has none. False for an unknown session or one without a sphere."""
        row = self.db.execute("SELECT sphere FROM sessions WHERE id = ?", (session,)).fetchone()
        if row is None or row[0] is None:
            return False
        with self.db:
            before = self.main_of(row[0])
            if main and before not in (None, session):
                self.db.execute("UPDATE sessions SET main = 0 WHERE id = ?", (before,))
                self._session_event(before)
            self.db.execute("UPDATE sessions SET main = ? WHERE id = ?", (int(main), session))
            self._session_event(session)
        return True

    def set_sphere(self, session: str, sphere: str | None, by: str = "owner") -> bool:
        """The owner moves the session to another sphere, or out of any: it is no sphere's main session there. `by`
        session: its own `Sphere:` line (`subs.sphere_line`), taken only while nobody placed it. Its sub-sessions
        follow it, but one the owner placed himself."""
        cursor = self.db.execute("UPDATE sessions SET sphere = ?, main = 0, sphere_by = ? WHERE id = ?",
                                 (sphere, by, session))
        if cursor.rowcount == 1:
            self._session_event(session)
            for (child,) in self.db.execute("SELECT id FROM sessions WHERE sub_of = ? AND sphere_by IS NOT 'owner'",
                                            (session,)).fetchall():
                self.db.execute("UPDATE sessions SET sphere = ?, main = 0 WHERE id = ?", (sphere, child))
                self._session_event(child)
        self.db.commit()
        return cursor.rowcount == 1

    def mark_seen(self, session: str, now: float) -> None:
        """The owner opened the session, wrote into it or decided its card: nothing in it is unread now. A session
        event only when that changes what the face shows."""
        cursor = self.db.execute("UPDATE sessions SET seen_at = ? WHERE id = ? AND news_at > COALESCE(seen_at, 0)",
                                 (now, session))
        if cursor.rowcount == 0:
            self.db.execute("UPDATE sessions SET seen_at = ? WHERE id = ?", (now, session))
        else:
            self._session_event(session)
        self.db.commit()

    def _news(self, session: str, at: float) -> None:
        """Something new for the owner in the session — a summary, a card shown — inside the caller's transaction;
        the session's event says it is unread."""
        self.db.execute("UPDATE sessions SET news_at = ? WHERE id = ?", (at, session))
        self._session_event(session)

    def session(self, session: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self.SESSION)} FROM sessions WHERE id = ?", (session,)).fetchone()
        return dict(zip(self.SESSION, row)) if row else None

    def update_session(self, session: str, now: float, **fields) -> None:
        sets = ", ".join(["at = ?", *(f"{name} = ?" for name in fields)])
        self.db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (now, *fields.values(), session))
        self._session_event(session)
        self.db.commit()

    def set_pinned(self, session: str, pinned: bool) -> bool:
        """The owner's pin in the face; its `at` stays, so pinning does not reorder the list by time."""
        cursor = self.db.execute("UPDATE sessions SET pinned = ? WHERE id = ?", (int(pinned), session))
        if cursor.rowcount == 1:
            self._session_event(session)
        self.db.commit()
        return cursor.rowcount == 1

    def archive(self, session: str, now: float) -> list[str]:
        """The owner's «Archive»: the session, its sub-sessions and side questions, theirs too, out of the face's list;
        each one's event says so. Its ids, the session first; empty when there is no such session."""
        rows = self.db.execute("""WITH RECURSIVE kin(id) AS (SELECT id FROM sessions WHERE id = ?
            UNION SELECT s.id FROM sessions s JOIN kin ON s.sub_of = kin.id OR s.parent = kin.id) SELECT id FROM kin""",
                               (session,)).fetchall()
        ids = sorted((row[0] for row in rows), key=lambda id_: id_ != session)
        for id_ in ids:
            self.db.execute("UPDATE sessions SET archived = COALESCE(archived, ?) WHERE id = ?", (now, id_))
            self._session_event(id_)
        self.db.commit()
        return ids

    def face_about(self, session: str) -> dict:
        """What the face's «Summary» says of a session besides its row — only what the platform was told: the directory
        it ran in, the context its main conversation's last request carried, and from its newest top-level `init` line
        the model, the CLI's version, the permission mode and the MCP servers with their status. A field nothing said
        is null ([] for the servers); an init line pruned with its turn after 90 days takes its fields along."""
        row = self.db.execute("SELECT cwd, context_tokens, cli_version FROM sessions WHERE id = ?", (session,)).fetchone()
        cwd, tokens, version = row if row else (None, None, "")
        init: dict = {}
        for (line,) in self.db.execute("SELECT line FROM raw WHERE session = ? AND instr(line, '\"init\"') > 0 "
                                       "ORDER BY id DESC", (session,)):
            data = parsed(line)
            if data.get("type") == "system" and data.get("subtype") == "init" and not data.get("parent_tool_use_id"):
                init = data
                break

        def text(key: str) -> str | None:
            return init[key] if isinstance(init.get(key), str) and init[key] else None

        servers = init.get("mcp_servers") if isinstance(init.get("mcp_servers"), list) else []
        return {"cwd": text("cwd") or cwd or None, "context_tokens": tokens, "model": text("model"),
                "cli_version": version or text("claude_code_version"), "permission_mode": text("permissionMode"),
                "mcp": [{"name": s["name"], "status": str(s.get("status") or "")} for s in servers
                        if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"]]}

    def set_context(self, session: str, tokens: int) -> None:
        """No event: the size is read when a card is rewritten, not shown on its own."""
        self.db.execute("UPDATE sessions SET context_tokens = ? WHERE id = ?", (tokens, session))
        self.db.commit()

    def add_run(self, session: str, hook_token_hash: str, marks: list[dict], now: float) -> str:
        run = secrets.token_hex(8)
        self.db.execute("INSERT INTO runs (id, session, hook_token_hash, marks, started) VALUES (?, ?, ?, ?, ?)",
                        (run, session, hook_token_hash, _json(marks), now))
        self.db.commit()
        return run

    def run_of(self, hook_token_hash: str) -> dict | None:
        """The run a hook request's token belongs to; None for a token no run was given."""
        row = self.db.execute("SELECT id, session, marks, ended, mode FROM runs WHERE hook_token_hash = ?",
                              (hook_token_hash,)).fetchone()
        return {"id": row[0], "session": row[1], "marks": json.loads(row[2]), "ended": row[3],
                "mode": row[4]} if row else None

    def set_run_mode(self, run: str, mode: str) -> bool:
        """The permission mode a run's init line named. True when it is the first run of its session to name a mode
        other than auto: the owner is told once per session that it runs without the classifier."""
        session = self.db.execute("SELECT session FROM runs WHERE id = ?", (run,)).fetchone()
        if session is None:
            return False
        before = self.db.execute("SELECT 1 FROM runs WHERE session = ? AND id != ? AND mode IS NOT NULL "
                                 "AND mode != 'auto'", (session[0], run)).fetchone()
        cursor = self.db.execute("UPDATE runs SET mode = ? WHERE id = ? AND mode IS NULL", (mode, run))
        self.db.commit()
        return cursor.rowcount == 1 and mode != "auto" and before is None

    def end_run(self, run: str, now: float) -> None:
        self.db.execute("UPDATE runs SET ended = ? WHERE id = ? AND ended IS NULL", (now, run))
        self.db.commit()

    def open_runs(self) -> list[dict]:
        """The runs a lane may still be running: started and not yet seen to end."""
        rows = self.db.execute("SELECT r.id, r.session, s.lane FROM runs r JOIN sessions s ON s.id = r.session "
                               "WHERE r.ended IS NULL ORDER BY r.started")
        return [{"run": run, "session": session, "lane": lane} for run, session, lane in rows]

    def interrupt(self, run: str, now: float) -> None:
        """`run` was stopped by the platform's own shutdown, not by the owner; a second stop changes nothing."""
        self.db.execute("INSERT OR IGNORE INTO interrupted (run, at) VALUES (?, ?)", (run, now))
        self.db.commit()

    def interrupted(self, run: str) -> bool:
        return self.db.execute("SELECT 1 FROM interrupted WHERE run = ?", (run,)).fetchone() is not None

    def resume_interrupted(self, run: str, now: float) -> bool:
        """True once for a run the shutdown stopped: whoever gets it resumes the session."""
        cursor = self.db.execute("UPDATE interrupted SET resumed = ? WHERE run = ? AND resumed IS NULL", (now, run))
        self.db.commit()
        return cursor.rowcount == 1

    def sessions_in(self, state: str, idle_before: float | None = None) -> list[str]:
        sql, args = "SELECT id FROM sessions WHERE state = ?", [state]
        if idle_before is not None:
            sql, args = sql + " AND idle_since < ?", [*args, idle_before]
        return [row[0] for row in self.db.execute(sql + " ORDER BY created", args)]

    def add_input(self, input_id: str, session: str, text: str, now: float, attachments: list[dict] | None = None,
                  thumbs: list[bytes | None] | None = None, by: str | None = None) -> bool:
        """True when the owner's message is new; the router sending it again after a timeout is not. It is placed
        in the transcript where it came: before the items its session had not yet written (`turn`, `n`)."""
        row = self.db.execute("SELECT turn FROM sessions WHERE id = ?", (session,)).fetchone()
        turn = row[0] if row else 0
        n = self.db.execute("SELECT COUNT(*) FROM items WHERE session = ? AND turn = ?", (session, turn)).fetchone()[0]
        cursor = self.db.execute("INSERT OR IGNORE INTO inputs (input_id, session, text, at, attachments, turn, n, by) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (input_id, session, text, now, _json(attachments or []), turn, n, by))
        if cursor.rowcount == 1:
            self.db.executemany("INSERT INTO input_thumbs VALUES (?, ?, ?)",
                                [(input_id, i, jpeg) for i, jpeg in enumerate(thumbs or []) if jpeg])
            if by is None:  # he wrote: he has seen it; a parent's brief or a report is not him looking
                self.db.execute("UPDATE sessions SET seen_at = ? WHERE id = ?", (now, session))
            self._session_event(session)  # its title is its first input
            self._event("input", session, f"{session}/{input_id}", self._face_input(input_id))
        self.db.commit()
        return cursor.rowcount == 1

    def input_session(self, input_id: str) -> str | None:
        row = self.db.execute("SELECT session FROM inputs WHERE input_id = ?", (input_id,)).fetchone()
        return row[0] if row else None

    def input_known(self, input_id: str) -> bool:
        """The owner's message was taken already: as a message into a session, or as a question's answer."""
        return self.input_session(input_id) is not None or self.db.execute(
            "SELECT 1 FROM answer_inputs WHERE input_id = ?", (input_id,)).fetchone() is not None

    def take_answer_input(self, input_id: str, approval: str, now: float) -> bool:
        """True once for a reply taken as a question's answer. Kept apart from `inputs`: an answer is not a message
        into the session — it names no session title and leaves the session's other questions pressable."""
        cursor = self.db.execute("INSERT OR IGNORE INTO answer_inputs VALUES (?, ?, ?)", (input_id, approval, now))
        self.db.commit()
        return cursor.rowcount == 1

    def peek_button(self, button: str, now: float) -> dict | None:
        """A live button's binding, not spent: a multi-select's tick, which may be pressed again and again."""
        row = self.db.execute("SELECT approval, action, digest FROM buttons WHERE id = ? AND used IS NULL AND expires > ?",
                              (button, now)).fetchone()
        return {"approval": row[0], "action": row[1], "digest": row[2]} if row else None

    def first_input(self, session: str) -> str:
        """Its title: the one it was given (a sub-session's), else the first thing written into it."""
        row = self.db.execute("SELECT COALESCE((SELECT title FROM sessions WHERE id = ?), (SELECT text FROM inputs "
                              "WHERE session = ? AND text != '' ORDER BY at, rowid LIMIT 1))", (session, session)).fetchone()
        return row[0] or "" if row else ""

    def last_input_at(self, session: str) -> float | None:
        """The owner's last message into the session: what a parent or a report wrote does not count."""
        return self.db.execute("SELECT MAX(at) FROM inputs WHERE session = ? AND by IS NULL", (session,)).fetchone()[0]

    # --- the runs waiting for room on their lane ----------------------------------------------------------------

    def enqueue(self, session: str, prompt: str | None, now: float, content: dict | None = None,
                by: str | None = None, commit: bool = True) -> None:
        """`content`: what goes to the lane instead of the bare `prompt` when the message has attachments. `by`: a
        parent's message or a report (`subs`); None is the owner's or the platform's own."""
        self.db.execute("INSERT INTO queue (session, prompt, content, at, by) VALUES (?, ?, ?, ?, ?)",
                        (session, prompt, _json(content) if content else None, now, by))
        if commit:
            self.db.commit()

    def queued(self) -> list[dict]:
        """The waiting runs, oldest first — without their attachments, which only the run being started reads
        (`queued_content`): every poll drains."""
        rows = self.db.execute("SELECT seq, session, prompt, by FROM queue ORDER BY seq")
        return [{"seq": seq, "session": session, "prompt": prompt, "by": by} for seq, session, prompt, by in rows]

    def queued_content(self, seq: int) -> dict | None:
        row = self.db.execute("SELECT content FROM queue WHERE seq = ?", (seq,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def dequeue(self, seq: int) -> None:
        self.db.execute("DELETE FROM queue WHERE seq = ?", (seq,))
        self.db.commit()

    def attempt(self, seq: int, run: str | None) -> None:
        self.db.execute("UPDATE queue SET attempt = ? WHERE seq = ?", (run, seq))
        self.db.commit()

    def attempted(self, run: str) -> int | None:
        """The queued entry `run` was sent for, while the lane's journal has not yet said what became of it."""
        row = self.db.execute("SELECT seq FROM queue WHERE attempt = ?", (run,)).fetchone()
        return row[0] if row else None

    def attempts(self, lane: str) -> list[str]:
        rows = self.db.execute("SELECT q.attempt FROM queue q JOIN sessions s ON s.id = q.session "
                               "WHERE q.attempt IS NOT NULL AND s.lane = ? ORDER BY q.seq", (lane,))
        return [row[0] for row in rows]

    # --- raw lines and the projection ---------------------------------------------------------------------------

    def add_raw(self, lane: str, lane_seq: int, session: str, run: str, line: str, now: float) -> int | None:
        """The line stored and numbered into its session's turn; that turn's number, or None for a line already
        stored (a replay)."""
        with self.db:
            if self.db.execute("SELECT 1 FROM raw WHERE lane = ? AND lane_seq = ?", (lane, lane_seq)).fetchone():
                return None
            turn, turn_run = self.db.execute("SELECT turn, turn_run FROM sessions WHERE id = ?", (session,)).fetchone()
            if turn_run is not None and turn_run != run:
                turn += 1
            self.db.execute("INSERT INTO raw (lane, lane_seq, session, run, turn, line, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (lane, lane_seq, session, run, turn, line, now))
            ended = transcript.ends_turn(parsed(line))
            self.db.execute("UPDATE sessions SET turn = ?, turn_run = ? WHERE id = ?",
                            (turn + 1 if ended else turn, None if ended else run, session))
        return turn

    def last_lane_seq(self, lane: str) -> int:
        """The lane's last journal number stored: a line before it that comes now is a replay."""
        return self.db.execute("SELECT MAX(lane_seq) FROM raw WHERE lane = ?", (lane,)).fetchone()[0] or 0

    def has_raw(self, lane: str, lane_seq: int) -> bool:
        return self.db.execute("SELECT 1 FROM raw WHERE lane = ? AND lane_seq = ?", (lane, lane_seq)).fetchone() \
            is not None

    def last_line(self, session: str) -> tuple[dict, float] | None:
        """The session's last CLI line and when it came; the lane's own events after it do not count."""
        row = self.db.execute("SELECT line, at FROM raw WHERE session = ? AND (NOT json_valid(line) OR "
                              "json_extract(line, '$.type') IS NOT 'lane') ORDER BY id DESC LIMIT 1",
                              (session,)).fetchone()
        return (parsed(row[0]), row[1]) if row else None

    def _turn_lines(self, session: str, turn: int) -> tuple[list[int], list[dict]]:
        rows = self.db.execute("SELECT id, line FROM raw WHERE session = ? AND turn = ? ORDER BY id",
                               (session, turn)).fetchall()
        return [row[0] for row in rows], [parsed(row[1]) for row in rows]

    def _resumed(self, session: str, turn: int) -> dict | None:
        """The call the session's last result before this turn deferred — what a resume finishes first. Read from
        the projection, which outlives the result's raw line; by result, not by the turn just before: a lane event
        after a result opens a turn of its own."""
        row = self.db.execute("SELECT data FROM items WHERE session = ? AND turn < ? AND kind = 'result' "
                              "ORDER BY turn DESC, n DESC LIMIT 1", (session, turn)).fetchone()
        return json.loads(row[0])["deferred"] if row else None

    def reproject(self, session: str, turn: int) -> None:
        """The turn's items projected again from its raw lines, after the turns before it; a turn whose lines were
        pruned keeps its items. The face is told only what changed: each item that is new or differs, and — when the
        turn now holds fewer — that the ones past the end are gone."""
        ids, lines = self._turn_lines(session, turn)
        if not lines:
            return
        resumed = self._resumed(session, turn)
        items = [(item.kind, _json(item.data), ids[item.first], ids[item.last])
                 for item in transcript.project(lines, resumed)]
        with self.db:
            old = {n: (kind, data) for n, kind, data in self.db.execute(
                "SELECT n, kind, data FROM items WHERE session = ? AND turn = ?", (session, turn))}
            self.db.execute("DELETE FROM items WHERE session = ? AND turn = ?", (session, turn))
            self.db.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [(session, turn, n, *item) for n, item in enumerate(items)])
            if len(items) < len(old):
                self._event("items_reset", session, f"{session}/{turn}",
                            {"session": session, "turn": turn, "count": len(items)})
            for n, (kind, data, _, _) in enumerate(items):
                if old.get(n) != (kind, data):
                    self._event("item", session, f"{session}/{turn}/{n}", _face_item(session, turn, n, kind, data))

    def rebuild(self, session: str) -> None:
        for (turn,) in self.db.execute("SELECT DISTINCT turn FROM raw WHERE session = ? ORDER BY turn",
                                       (session,)).fetchall():
            self.reproject(session, turn)

    def rebuild_all(self) -> None:
        """The whole projection again from the raw lines; a turn whose lines were pruned keeps its items."""
        for (session,) in self.db.execute("SELECT id FROM sessions").fetchall():
            self.rebuild(session)

    def items(self, session: str) -> list[dict]:
        rows = self.db.execute("SELECT turn, n, kind, data, first_raw, last_raw FROM items WHERE session = ? "
                               "ORDER BY turn, n", (session,))
        return [{"turn": turn, "n": n, "kind": kind, "data": json.loads(data), "first_raw": first, "last_raw": last}
                for turn, n, kind, data, first, last in rows]

    def changes_of(self, session: str) -> tuple[str, list[changes.Change]]:
        """What the session's edits changed (`changes.Change`, in the order they ran) and the directory it ran in, as
        its CLI said at start (empty when no such line is left): from the raw lines where they are kept, from the
        projection of a turn whose lines were pruned."""
        scan, cwd = changes.Scan(), ""
        raw: dict[int, list[dict]] = {}
        for turn, line in self.db.execute("SELECT turn, line FROM raw WHERE session = ? ORDER BY id", (session,)):
            raw.setdefault(turn, []).append(parsed(line))
        projected: dict[int, list[dict]] = {}
        for item in self.items(session):
            projected.setdefault(item["turn"], []).append(item)
        for turn in sorted(raw.keys() | projected.keys()):
            if turn not in raw:
                scan.changes += changes.from_items(projected[turn])
                continue
            for line in raw[turn]:
                if line.get("type") == "system" and line.get("subtype") == "init" and isinstance(line.get("cwd"), str) \
                        and not line.get("parent_tool_use_id"):
                    cwd = line["cwd"]
                scan.line(line)
        return cwd, scan.changes

    def prune(self, now: float) -> int:
        """Raw lines older than 90 days go, a whole turn at a time — a turn still being written keeps its lines.
        The projection stays. The face's events older than 7 days go too, by number, and the last number gone is
        kept: a stream asked to start before it is told to reload (`events_gone`). Spent codes and logins go."""
        cursor = self.db.execute(
            "DELETE FROM raw WHERE (session, turn) IN (SELECT session, turn FROM raw GROUP BY session, turn "
            "HAVING MAX(at) < ?) AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = raw.session "
            "AND s.turn = raw.turn)", (now - RAW_KEEP_S,))
        upto = self.db.execute("SELECT MAX(id) FROM events WHERE at < ?", (now - EVENTS_KEEP_S,)).fetchone()[0]
        if upto is not None:
            self.db.execute("DELETE FROM events WHERE id <= ?", (upto,))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('events_pruned', ?)", (upto,))
        self.db.execute("DELETE FROM login_codes WHERE expires < ?", (now,))
        self.db.execute("DELETE FROM logins WHERE expires < ?", (now,))
        self.db.commit()
        return cursor.rowcount

    # --- approvals and their buttons ----------------------------------------------------------------------------

    APPROVAL = ("id", "session", "run", "tool_use_id", "kind", "tool", "input", "cwd", "file_hash", "digest", "state",
                "view", "created", "at", "why")

    def add_approval(self, session: str, run: str, tool_use_id: str, kind: str, tool: str, tool_input: dict,
                     cwd: str, file_hash: str, digest: str, now: float, state: str = "pending",
                     view: dict | None = None, why: str = "") -> str:
        approval = secrets.token_hex(4)
        self.db.execute(f"INSERT INTO approvals ({', '.join(self.APPROVAL)}) VALUES ({', '.join('?' * 15)})",
                        (approval, session, run, tool_use_id, kind, tool, _json(tool_input), cwd, file_hash, digest,
                         state, _json(view or {}), now, now, why))
        if state != "pending" and view:  # a card shown at once: a classifier's block, a courier
            self._news(session, now)
        self._approval_event(approval)
        self.db.commit()
        return approval

    def approval(self, approval: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self.APPROVAL)} FROM approvals WHERE id = ?", (approval,)).fetchone()
        if not row:
            return None
        data = dict(zip(self.APPROVAL, row))
        data["input"], data["view"] = json.loads(data["input"]), json.loads(data["view"])
        return data

    def approvals(self, *, session: str | None = None, run: str | None = None, tool_use_id: str | None = None,
                  kind: str | tuple[str, ...] | None = None, states: tuple[str, ...] = ()) -> list[dict]:
        """The approvals that match every filter given, oldest first; `kind` one kind or any of several."""
        where, args = [], []
        for column, value in (("session", session), ("run", run), ("tool_use_id", tool_use_id)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if kind is not None:
            kinds = (kind,) if isinstance(kind, str) else kind
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        if states:
            where.append(f"state IN ({', '.join('?' * len(states))})")
            args += states
        ids = self.db.execute(f"SELECT id FROM approvals WHERE {' AND '.join(where) or '1'} ORDER BY created, rowid",
                              args).fetchall()
        return [self.approval(approval) for (approval,) in ids]

    def move(self, approval: str, states: tuple[str, ...], to: str, now: float, note: dict | None = None,
             **fields) -> bool:
        """`to` only from one of `states`; True when this call moved it — of two racing moves exactly one wins. With
        `note` (`add_note`'s arguments), that note is journaled in the same transaction when the move wins."""
        if "view" in fields:
            fields["view"] = _json(fields["view"])
        sets = ", ".join(["state = ?", "at = ?", *(f"{name} = ?" for name in fields)])
        cursor = self.db.execute(f"UPDATE approvals SET {sets} WHERE id = ? AND state IN ({', '.join('?' * len(states))})",
                                 (to, now, *fields.values(), approval, *states))
        if cursor.rowcount == 1:
            self._approval_event(approval)
            if "shown" in (*states, to):  # how many cards its session waits on; a card newly shown is news
                session = self.db.execute("SELECT session FROM approvals WHERE id = ?", (approval,)).fetchone()[0]
                if to == "shown" and "shown" not in states:
                    self.db.execute("UPDATE sessions SET news_at = ? WHERE id = ?", (now, session))
                self._session_event(session)
            if note is not None:
                self._note(**note)
        self.db.commit()
        return cursor.rowcount == 1

    def restyle(self, approval: str, state: str, view: dict, note: dict) -> bool:
        """The card's view rewritten while it stays `state` — its `at` kept, so neither its expiry nor what the
        face shows of its age moves — and `note` journaled with it; False when it has left that state."""
        cursor = self.db.execute("UPDATE approvals SET view = ? WHERE id = ? AND state = ?",
                                 (_json(view), approval, state))
        if cursor.rowcount == 1:
            self._approval_event(approval)
            self._note(**note)
        self.db.commit()
        return cursor.rowcount == 1

    def live_buttons(self, approval: str, now: float) -> list[tuple[str, str]]:
        """The card's buttons that still act, (action, id) in the order they were made: what a rewrite keeps."""
        rows = self.db.execute("SELECT action, id FROM buttons WHERE approval = ? AND used IS NULL AND expires > ? "
                               "ORDER BY rowid", (approval, now))
        return [(action, button) for action, button in rows]

    def add_button(self, approval: str, action: str, digest: str, expires: float) -> str:
        button = BUTTON_PREFIX + secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO buttons (id, approval, action, digest, expires) VALUES (?, ?, ?, ?, ?)",
                        (button, approval, action, digest, expires))
        self.db.commit()
        return button

    def void_buttons(self, approval: str, prefix: str, now: float) -> int:
        """Every live button of the card whose action starts with `prefix` spent at once: a question's message
        rewritten with new buttons, or answered elsewhere, leaves none of its old ones pressable."""
        cursor = self.db.execute("UPDATE buttons SET used = ? WHERE approval = ? AND used IS NULL AND substr(action, 1, ?) = ?",
                                 (now, approval, len(prefix), prefix))
        self.db.commit()
        return cursor.rowcount

    def spend_button(self, button: str, now: float) -> dict | None:
        """The button's binding, once: a second press, or one after it expired, gets None."""
        cursor = self.db.execute("UPDATE buttons SET used = ? WHERE id = ? AND used IS NULL AND expires > ?",
                                 (now, button, now))
        self.db.commit()
        if cursor.rowcount != 1:
            return None
        approval, action, digest = self.db.execute("SELECT approval, action, digest FROM buttons WHERE id = ?",
                                                   (button,)).fetchone()
        return {"approval": approval, "action": action, "digest": digest}

    # --- notes: what the router takes by number -----------------------------------------------------------------

    def add_note(self, kind: str, session: str, data: dict, approval: str = "") -> int:
        seq = self._note(kind, session, data, approval)
        self.db.commit()
        return seq

    def _note(self, kind: str, session: str, data: dict, approval: str = "") -> int:
        seq = self.db.execute("INSERT INTO notes (kind, session, approval, data) VALUES (?, ?, ?, ?)",
                              (kind, session, approval, _json(data))).lastrowid
        if kind == "summary":  # every summary its own object: the face keeps them all
            self._event("summary", session, f"{session}/{seq}", _face_summary(session, seq, data))
            at = data.get("at")
            self._news(session, at if isinstance(at, (int, float)) else self.clock())
        return seq

    def last_summary(self, session: str) -> dict | None:
        row = self.db.execute("SELECT data FROM notes WHERE kind = 'summary' AND session = ? ORDER BY seq DESC LIMIT 1",
                              (session,)).fetchone()
        return json.loads(row[0]) if row else None

    def look(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT seq, kind, session, approval, data FROM notes WHERE given = 0 ORDER BY seq "
                               "LIMIT ?", (limit,))
        return [{"seq": seq, "kind": kind, "session": session, "approval": approval, "data": json.loads(data)}
                for seq, kind, session, approval, data in rows]

    def confirm(self, upto: int) -> int:
        cursor = self.db.execute("UPDATE notes SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return cursor.rowcount


    # --- the face: its snapshot and its events -------------------------------------------------------------------

    def _event(self, kind: str, session: str, key: str, data: dict) -> None:
        """One upsert for the face, inside the caller's transaction: committed with the change it describes or not at
        all. The listener only wakes the streams; they read the rows after this commit, on the same thread."""
        self.db.execute("INSERT INTO events (kind, session, key, data, at) VALUES (?, ?, ?, ?, ?)",
                        (kind, session, key, _json(data), self.clock()))
        if self.listener is not None:
            self.listener()

    def _session_event(self, session: str) -> None:
        if (row := self.face_session(session)) is not None:
            self._event("session", session, session, row)

    def _approval_event(self, approval: str) -> None:
        """A card the owner was shown, in any place: a pending call no one has seen yet is not the face's either."""
        if (data := self.face_approval(approval)) is not None:
            self._event("approval", data["session"], approval, data)

    FACE_SESSION = """SELECT s.id, s.track, s.lane, s.kind, s.state, s.created, s.at, s.pinned, s.parent,
        s.sphere, s.main, s.fork_of, s.news_at IS NOT NULL AND s.news_at > COALESCE(s.seen_at, 0),
        COALESCE(s.title, (SELECT i.text FROM inputs i WHERE i.session = s.id AND i.text != '' ORDER BY i.at, i.rowid
            LIMIT 1)),
        (SELECT COUNT(*) FROM approvals a WHERE a.session = s.id AND a.kind IN ('approval', 'question')
            AND a.state = 'shown'),
        (SELECT COUNT(*) FROM base_files b WHERE b.session = s.id),
        (SELECT COALESCE(SUM(r.n), 0) FROM push_touched t JOIN push_repos r ON r.repo = t.repo WHERE t.session = s.id),
        (SELECT r.repo || ': ' || r.why FROM push_touched t JOIN push_repos r ON r.repo = t.repo
            WHERE t.session = s.id AND r.why != '' ORDER BY r.repo LIMIT 1),
        s.turn_started, s.sub_of,
        (SELECT COUNT(*) FROM sessions c WHERE c.sub_of = s.id AND c.state != 'closed'),
        s.state = 'active' AND EXISTS (SELECT 1 FROM queue q WHERE q.session = s.id)
            AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.session = s.id AND r.ended IS NULL),
        s.archived IS NOT NULL
        FROM sessions s"""

    @staticmethod
    def _face_session(row: tuple) -> dict:
        session, track, lane, kind, state, created, at, pinned, parent, sphere, main, fork_of, unread, first, \
            waiting, base, push, push_why, turn_started, sub_of, children, queued, archived = row
        return {"id": session, "track": track, "lane": lane, "kind": kind, "state": state, "created": created,
                "at": at, "title": unmarked(first or "")[:TITLE_LEN] or track, "waiting": waiting, "pinned": bool(pinned),
                "parent": parent, "sphere": sphere, "main": bool(main), "fork_of": fork_of, "unread": bool(unread),
                "base": base, "push": push, **({"push_why": push_why} if push_why else {}),
                **({"turn_started": turn_started} if turn_started is not None else {}),
                # `side`: a side question (its `parent`); `sub_of`: a sub-session's parent session, `children` its own
                # open ones, `queued`: waiting for room in the lane (`subs`).
                "side": parent is not None, "sub_of": sub_of, "children": children, "queued": bool(queued),
                **({"archived": True} if archived else {})}

    def face_session(self, session: str) -> dict | None:
        """A session as the face lists it: its title is the first thing the owner wrote into it, or its track,
        `waiting` the cards it waits on, and `base` its Base files not committed yet (`tobase`)."""
        row = self.db.execute(f"{self.FACE_SESSION} WHERE s.id = ?", (session,)).fetchone()
        return self._based(self._face_session(row)) if row else None

    def face_sessions(self, now: float) -> list[dict]:
        """Every session that is not closed, those closed in the last 30 days and every sphere's main session, however
        long closed (a message to it opens it again); the latest first. An archived one is none of them."""
        rows = self.db.execute(f"{self.FACE_SESSION} WHERE s.archived IS NULL "
                               "AND (s.state != 'closed' OR s.at >= ? OR s.main = 1) "
                               "ORDER BY s.at DESC",
                               (now - FACE_CLOSED_S,))
        return [self._based(self._face_session(row)) for row in rows]

    def _based(self, row: dict) -> dict:
        return row if self.base_shown() else {**row, "base": 0}

    def face_approval(self, approval: str) -> dict | None:
        """An approval as the face shows it, the input whole; None for one never shown as a card."""
        row = self.db.execute(f"SELECT {', '.join(FACE_APPROVAL)} FROM approvals WHERE id = ? AND state != 'pending' "
                              "AND view != '{}'", (approval,)).fetchone()
        if not row:
            return None
        data = dict(zip(FACE_APPROVAL, row))
        data["input"], data["view"] = json.loads(data["input"]), json.loads(data["view"])
        return data

    def face_approvals(self, session: str) -> list[dict]:
        ids = self.db.execute("SELECT id FROM approvals WHERE session = ? AND state != 'pending' AND view != '{}' "
                              "ORDER BY created, rowid", (session,)).fetchall()
        return [self.face_approval(approval) for (approval,) in ids]

    def face_items(self, session: str) -> list[dict]:
        rows = self.db.execute("SELECT turn, n, kind, data FROM items WHERE session = ? ORDER BY turn, n", (session,))
        return [_face_item(session, turn, n, kind, data) for turn, n, kind, data in rows]

    FACE_INPUT = "SELECT session, input_id, text, at, attachments, turn, n, by FROM inputs"

    def _face_input(self, input_id: str) -> dict:
        return _face_input(self.db.execute(f"{self.FACE_INPUT} WHERE input_id = ?", (input_id,)).fetchone())

    def face_inputs(self, session: str) -> list[dict]:
        """What the owner wrote into the session, oldest first: his text and what came with it, by name."""
        rows = self.db.execute(f"{self.FACE_INPUT} WHERE session = ? ORDER BY at, rowid", (session,))
        return [_face_input(row) for row in rows]

    def input_thumb(self, session: str, input_id: str, n: int) -> bytes | None:
        row = self.db.execute("SELECT t.jpeg FROM input_thumbs t JOIN inputs i ON i.input_id = t.input_id "
                              "WHERE i.session = ? AND t.input_id = ? AND t.n = ?", (session, input_id, n)).fetchone()
        return row[0] if row else None

    def face_summaries(self, session: str) -> list[dict]:
        rows = self.db.execute("SELECT seq, data FROM notes WHERE kind = 'summary' AND session = ? ORDER BY seq",
                               (session,))
        return [_face_summary(session, seq, json.loads(data)) for seq, data in rows]

    def cursor(self) -> int:
        """The number of the last event written: a snapshot read now is as of it."""
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
        return row[0] if row else 0

    def events_after(self, cursor: int, limit: int = 200) -> list[tuple[int, str, str]]:
        return self.db.execute("SELECT id, kind, data FROM events WHERE id > ? ORDER BY id LIMIT ?",
                               (cursor, limit)).fetchall()

    def events_gone(self, cursor: int) -> bool:
        """The events after `cursor` can no longer be given whole: some were pruned, or the cursor is from another
        database. Its client reloads the snapshot."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'events_pruned'").fetchone()
        return cursor < (row[0] if row else 0) or cursor > self.cursor()

    # --- the face's logins: one-time codes, cookies, failures -----------------------------------------------------

    def add_code(self, code_hash: str, expires: float) -> None:
        """A new code; the ones not used yet are void — only the last code the bot gave works."""
        self.db.execute("DELETE FROM login_codes WHERE used IS NULL")
        self.db.execute("INSERT INTO login_codes (hash, expires) VALUES (?, ?)", (code_hash, expires))
        self.db.commit()

    def use_code(self, code_hash: str, now: float) -> bool:
        """True once for a live code: of two racing uses exactly one wins."""
        cursor = self.db.execute("UPDATE login_codes SET used = ? WHERE hash = ? AND used IS NULL AND expires > ?",
                                 (now, code_hash, now))
        self.db.commit()
        return cursor.rowcount == 1

    def add_login(self, token_hash: str, via: str, now: float, expires: float) -> None:
        self.db.execute("INSERT INTO logins VALUES (?, ?, ?, ?)", (token_hash, via, now, expires))
        self.db.commit()

    def login_expires(self, token_hash: str, now: float) -> float | None:
        row = self.db.execute("SELECT expires FROM logins WHERE hash = ? AND expires > ?", (token_hash, now)).fetchone()
        return row[0] if row else None

    def extend_login(self, token_hash: str, expires: float) -> None:
        self.db.execute("UPDATE logins SET expires = ? WHERE hash = ?", (expires, token_hash))
        self.db.commit()

    def drop_login(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM logins WHERE hash = ?", (token_hash,))
        self.db.commit()

    def auth_failed(self, now: float, window: float, limit: int, block: float) -> None:
        """One more failed login; the `limit`-th within `window` blocks every login for `block` seconds."""
        self.db.execute("DELETE FROM auth_failures WHERE at < ?", (now - window,))
        self.db.execute("INSERT INTO auth_failures VALUES (?)", (now,))
        if self.db.execute("SELECT COUNT(*) FROM auth_failures").fetchone()[0] >= limit:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('auth_blocked_until', ?)", (now + block,))
            self.db.execute("DELETE FROM auth_failures")
        self.db.commit()

    def auth_blocked(self, now: float) -> bool:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'auth_blocked_until'").fetchone()
        return row is not None and now < row[0]

    # --- text values: what `speech.Scribe` keeps, as the router's `protocol.Store` does ----------------------------

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.db.commit()


def _face_item(session: str, turn: int, n: int, kind: str, data: str) -> dict:
    return {"session": session, "turn": turn, "n": n, "kind": kind, "data": json.loads(data)}


def _face_input(row: tuple) -> dict:
    """An owner's message as the face shows it; its event's key is `<session>/<input_id>`. It goes before the item
    (`turn`, `n`) — the first one the session had not written when it came (null on old ones: no place)."""
    session, input_id, text, at, attachments, turn, n, by = row
    return {"session": session, "input_id": input_id, "text": text, "at": at, "turn": turn, "n": n,
            "attachments": json.loads(attachments or "[]"), **({"by": by} if by else {})}


def _face_summary(session: str, seq: int, data: dict) -> dict:
    """A summary as the face shows it; its event's key is `<session>/<seq>`. `turn`: the turn it ends (it is said
    when that turn's last line comes). A ping's fields (`pings`): `kind`, `took_s`, and `line`, the sphere line exactly
    as the model wrote it at the end of its message (the face hides it there)."""
    return {"session": session, "seq": seq, "head": data.get("head", ""), "text": data.get("text", ""),
            "at": data.get("at"), "turn": data.get("turn"), "kind": data.get("kind"), "took_s": data.get("took_s"),
            "line": data.get("line")}


def parsed(line: str) -> dict:
    """A raw line as the projection reads it; one that is not a JSON object projects to nothing."""
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
