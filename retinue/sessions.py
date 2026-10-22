"""The platform's own SQLite: its sessions and their runs, the runs queued for room on a lane, what the owner wrote
into them, every raw line the lanes journaled, the transcript projected from those lines, the approvals with the
buttons bound to them, and the notes the router takes by number (the journal pattern of `tgbusiness.State`).

A raw line is stored once — `UNIQUE(lane, lane_seq)` makes a lane's replay after a reconnect a no-op — and is
numbered into its session's turns as it arrives: a `result` closes a turn, and so does a new run of the session
(a process that ended without one). The number is the session's, not recomputed from the lines still there, so
pruning old raw lines never renumbers the turns whose projection stays.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from . import transcript

RAW_KEEP_S = 90 * 86400
# A platform button id starts so, and the router routes on it; a core button id (token_urlsafe) never holds a dot.
BUTTON_PREFIX = "p."


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


class PlatformStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
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
                turn_run TEXT                       -- the run whose lines the open turn holds; NULL: none yet
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                session TEXT NOT NULL,
                hook_token_hash TEXT NOT NULL UNIQUE,
                marks TEXT NOT NULL,                -- approvals.Mark of each marker hook n, JSON
                started REAL NOT NULL,
                ended REAL
            );
            CREATE TABLE IF NOT EXISTS inputs (
                input_id TEXT PRIMARY KEY,          -- the router's: tg:<message id>
                session TEXT NOT NULL,
                text TEXT NOT NULL,
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
                kind TEXT NOT NULL,                 -- approval | courier
                tool TEXT NOT NULL,
                input TEXT NOT NULL,
                cwd TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,                -- pending | shown | granted | executing | executed | rejected
                                                    -- | stale | dropped | expired
                view TEXT NOT NULL DEFAULT '{}',    -- the card as shown: courier.CardView
                created REAL NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS approvals_call ON approvals (tool_use_id);
            CREATE TABLE IF NOT EXISTS buttons (
                id TEXT PRIMARY KEY,                -- BUTTON_PREFIX + token_urlsafe(12)
                approval TEXT NOT NULL,
                action TEXT NOT NULL,               -- approve | reject | open
                digest TEXT NOT NULL,
                expires REAL NOT NULL,
                used REAL
            );
            CREATE TABLE IF NOT EXISTS notes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                 -- card | update | summary
                session TEXT NOT NULL,
                approval TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL,
                given INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL,
                prompt TEXT,                        -- NULL: a resume after the owner's decision
                at REAL NOT NULL,
                attempt TEXT                        -- the run sent for it whose answer was lost: the lane's journal
                                                    -- tells whether it started
            );
            """)
        self.db.commit()

    # --- sessions and runs --------------------------------------------------------------------------------------

    SESSION = ("id", "track", "lane", "kind", "state", "cli_session", "cli_version", "created", "at", "idle_since")

    def new_session(self, track: str, lane: str, kind: str, now: float) -> str:
        session = secrets.token_hex(4)
        self.db.execute("INSERT INTO sessions (id, track, lane, kind, state, created, at) "
                        "VALUES (?, ?, ?, ?, 'active', ?, ?)", (session, track, lane, kind, now, now))
        self.db.commit()
        return session

    def session(self, session: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self.SESSION)} FROM sessions WHERE id = ?", (session,)).fetchone()
        return dict(zip(self.SESSION, row)) if row else None

    def update_session(self, session: str, now: float, **fields) -> None:
        sets = ", ".join(["at = ?", *(f"{name} = ?" for name in fields)])
        self.db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (now, *fields.values(), session))
        self.db.commit()

    def add_run(self, session: str, hook_token_hash: str, marks: list[dict], now: float) -> str:
        run = secrets.token_hex(8)
        self.db.execute("INSERT INTO runs (id, session, hook_token_hash, marks, started) VALUES (?, ?, ?, ?, ?)",
                        (run, session, hook_token_hash, _json(marks), now))
        self.db.commit()
        return run

    def run_of(self, hook_token_hash: str) -> dict | None:
        """The run a hook request's token belongs to; None for a token no run was given."""
        row = self.db.execute("SELECT id, session, marks, ended FROM runs WHERE hook_token_hash = ?",
                              (hook_token_hash,)).fetchone()
        return {"id": row[0], "session": row[1], "marks": json.loads(row[2]), "ended": row[3]} if row else None

    def end_run(self, run: str, now: float) -> None:
        self.db.execute("UPDATE runs SET ended = ? WHERE id = ? AND ended IS NULL", (now, run))
        self.db.commit()

    def open_runs(self) -> list[dict]:
        """The runs a lane may still be running: started and not yet seen to end."""
        rows = self.db.execute("SELECT r.id, r.session, s.lane FROM runs r JOIN sessions s ON s.id = r.session "
                               "WHERE r.ended IS NULL ORDER BY r.started")
        return [{"run": run, "session": session, "lane": lane} for run, session, lane in rows]

    def sessions_in(self, state: str, idle_before: float | None = None) -> list[str]:
        sql, args = "SELECT id FROM sessions WHERE state = ?", [state]
        if idle_before is not None:
            sql, args = sql + " AND idle_since < ?", [*args, idle_before]
        return [row[0] for row in self.db.execute(sql + " ORDER BY created", args)]

    def add_input(self, input_id: str, session: str, text: str, now: float) -> bool:
        """True when the owner's message is new; the router sending it again after a timeout is not."""
        cursor = self.db.execute("INSERT OR IGNORE INTO inputs VALUES (?, ?, ?, ?)", (input_id, session, text, now))
        self.db.commit()
        return cursor.rowcount == 1

    def input_session(self, input_id: str) -> str | None:
        row = self.db.execute("SELECT session FROM inputs WHERE input_id = ?", (input_id,)).fetchone()
        return row[0] if row else None

    def last_input_at(self, session: str) -> float | None:
        return self.db.execute("SELECT MAX(at) FROM inputs WHERE session = ?", (session,)).fetchone()[0]

    # --- the runs waiting for room on their lane ----------------------------------------------------------------

    def enqueue(self, session: str, prompt: str | None, now: float) -> None:
        self.db.execute("INSERT INTO queue (session, prompt, at) VALUES (?, ?, ?)", (session, prompt, now))
        self.db.commit()

    def queued(self) -> list[dict]:
        rows = self.db.execute("SELECT seq, session, prompt FROM queue ORDER BY seq")
        return [{"seq": seq, "session": session, "prompt": prompt} for seq, session, prompt in rows]

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
        pruned keeps its items."""
        ids, lines = self._turn_lines(session, turn)
        if not lines:
            return
        resumed = self._resumed(session, turn)
        with self.db:
            self.db.execute("DELETE FROM items WHERE session = ? AND turn = ?", (session, turn))
            self.db.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [(session, turn, n, item.kind, _json(item.data), ids[item.first], ids[item.last])
                                 for n, item in enumerate(transcript.project(lines, resumed))])

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

    def prune(self, now: float) -> int:
        """Raw lines older than 90 days go, a whole turn at a time — a turn still being written keeps its lines.
        The projection stays."""
        cursor = self.db.execute(
            "DELETE FROM raw WHERE (session, turn) IN (SELECT session, turn FROM raw GROUP BY session, turn "
            "HAVING MAX(at) < ?) AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = raw.session "
            "AND s.turn = raw.turn)", (now - RAW_KEEP_S,))
        self.db.commit()
        return cursor.rowcount

    # --- approvals and their buttons ----------------------------------------------------------------------------

    APPROVAL = ("id", "session", "run", "tool_use_id", "kind", "tool", "input", "cwd", "file_hash", "digest", "state",
                "view", "created", "at")

    def add_approval(self, session: str, run: str, tool_use_id: str, kind: str, tool: str, tool_input: dict,
                     cwd: str, file_hash: str, digest: str, now: float, state: str = "pending",
                     view: dict | None = None) -> str:
        approval = secrets.token_hex(4)
        self.db.execute(f"INSERT INTO approvals ({', '.join(self.APPROVAL)}) VALUES ({', '.join('?' * 14)})",
                        (approval, session, run, tool_use_id, kind, tool, _json(tool_input), cwd, file_hash, digest,
                         state, _json(view or {}), now, now))
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
                  kind: str | None = None, states: tuple[str, ...] = ()) -> list[dict]:
        """The approvals that match every filter given, oldest first."""
        where, args = [], []
        for column, value in (("session", session), ("run", run), ("tool_use_id", tool_use_id), ("kind", kind)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if states:
            where.append(f"state IN ({', '.join('?' * len(states))})")
            args += states
        ids = self.db.execute(f"SELECT id FROM approvals WHERE {' AND '.join(where) or '1'} ORDER BY created, rowid",
                              args).fetchall()
        return [self.approval(approval) for (approval,) in ids]

    def move(self, approval: str, states: tuple[str, ...], to: str, now: float, **fields) -> bool:
        """`to` only from one of `states`; True when this call moved it — of two racing moves exactly one wins."""
        if "view" in fields:
            fields["view"] = _json(fields["view"])
        sets = ", ".join(["state = ?", "at = ?", *(f"{name} = ?" for name in fields)])
        cursor = self.db.execute(f"UPDATE approvals SET {sets} WHERE id = ? AND state IN ({', '.join('?' * len(states))})",
                                 (to, now, *fields.values(), approval, *states))
        self.db.commit()
        return cursor.rowcount == 1

    def add_button(self, approval: str, action: str, digest: str, expires: float) -> str:
        button = BUTTON_PREFIX + secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO buttons (id, approval, action, digest, expires) VALUES (?, ?, ?, ?, ?)",
                        (button, approval, action, digest, expires))
        self.db.commit()
        return button

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
        cursor = self.db.execute("INSERT INTO notes (kind, session, approval, data) VALUES (?, ?, ?, ?)",
                                 (kind, session, approval, _json(data)))
        self.db.commit()
        return cursor.lastrowid

    def look(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT seq, kind, session, approval, data FROM notes WHERE given = 0 ORDER BY seq "
                               "LIMIT ?", (limit,))
        return [{"seq": seq, "kind": kind, "session": session, "approval": approval, "data": json.loads(data)}
                for seq, kind, session, approval, data in rows]

    def confirm(self, upto: int) -> int:
        cursor = self.db.execute("UPDATE notes SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return cursor.rowcount


def parsed(line: str) -> dict:
    """A raw line as the projection reads it; one that is not a JSON object projects to nothing."""
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
