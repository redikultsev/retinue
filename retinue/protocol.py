"""Protocol: append-only log of every exchange the router carries, plus the router's own state.

Field names follow the OpenTelemetry GenAI conventions where they exist, so a trace
viewer (Phoenix, Datasette) can read it without translation.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
import uuid
from pathlib import Path


WINDOWS = ("five_hour", "seven_day")
WINDOW_COLUMNS = [(f"{w}_{part}", kind) for w in WINDOWS for part, kind in (("utilization", "REAL"),
                                                                           ("resets_at", "INTEGER"))]


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS protocol (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                conversation_id TEXT NOT NULL,      -- gen_ai.conversation.id (A2A context id)
                source TEXT NOT NULL,               -- who sent: owner mxid or agent id
                target TEXT NOT NULL,               -- who received
                status TEXT NOT NULL,               -- done | failed | rejected | error
                input_chars INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                num_turns INTEGER,
                cost_usd REAL,                      -- reported by the engine; on a subscription it is a usage gauge
                duration_ms INTEGER
            );
            CREATE TRIGGER IF NOT EXISTS protocol_append_only_u BEFORE UPDATE ON protocol
                BEGIN SELECT RAISE(ABORT, 'protocol is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS protocol_append_only_d BEFORE DELETE ON protocol
                BEGIN SELECT RAISE(ABORT, 'protocol is append-only'); END;
            CREATE TABLE IF NOT EXISTS rooms (      -- Matrix adapter: agent room per agent
                agent_id TEXT PRIMARY KEY,          -- '_protocol' for the protocol room
                room_id TEXT NOT NULL,
                context_id TEXT NOT NULL            -- legacy: conversations moved to `conversations`
            );
            CREATE TABLE IF NOT EXISTS places (     -- where an agent lives in a channel: Telegram topic id, ...
                channel TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                place_id TEXT NOT NULL,
                PRIMARY KEY (channel, agent_id)
            );
            CREATE TABLE IF NOT EXISTS kv (         -- small adapter state, e.g. the Telegram update offset
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (  -- one current conversation per agent, for every channel
                agent_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL            -- A2A context id
            );
            CREATE TABLE IF NOT EXISTS sent (       -- a message the router sent: messenger id -> archive event id
                channel TEXT NOT NULL,
                native_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                PRIMARY KEY (channel, native_id)
            );
            CREATE TABLE IF NOT EXISTS buttons (    -- one row per button; the messenger carries only `id`
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,             -- archive id of the card the button belongs to
                label TEXT NOT NULL,
                action TEXT NOT NULL,               -- what the router does when it is pressed
                value TEXT NOT NULL,
                expires REAL NOT NULL,
                used REAL                           -- when the card was spent; NULL while it is live
            );
            CREATE TABLE IF NOT EXISTS runs (       -- one row per model run: what the status page will count
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                agent_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- conversation | retry | summary | compact
                status TEXT NOT NULL,               -- done | failed | rejected | error | limit
                num_turns INTEGER,
                duration_ms INTEGER,
                cost_usd REAL,                      -- the CLI's estimate; on a subscription a gauge, not a bill
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                rate_status TEXT,                   -- the subscription limit as the CLI last reported it in this run
                rate_type TEXT,                     -- five_hour | seven_day | seven_day_opus | ...
                rate_utilization REAL,              -- share of the window used, 0..1
                rate_resets_at INTEGER              -- unix seconds
            );
            """
        )
        if "channel" not in {row[1] for row in self.db.execute("PRAGMA table_info(protocol)")}:
            self.db.execute("ALTER TABLE protocol ADD COLUMN channel TEXT")
        # Both windows of the subscription limit, as the CLI reports them on every event (engine.rate_of).
        runs = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        for column, kind in WINDOW_COLUMNS:
            if column not in runs:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
        if "alone" not in {row[1] for row in self.db.execute("PRAGMA table_info(buttons)")}:
            # 1: pressing it spends this button only, the card's others stay alive (the evening list's «Откатить»)
            self.db.execute("ALTER TABLE buttons ADD COLUMN alone INTEGER NOT NULL DEFAULT 0")
        # Rooms used to own the conversation; keep those conversations when upgrading.
        self.db.execute("INSERT OR IGNORE INTO conversations SELECT agent_id, context_id FROM rooms"
                        " WHERE agent_id != '_protocol' AND context_id != ''")
        self.db.commit()

    def log(self, *, conversation_id: str, source: str, target: str, status: str, input_chars: int,
            output_chars: int, num_turns: int | None = None, cost_usd: float | None = None,
            duration_ms: int | None = None, channel: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO protocol (ts, conversation_id, source, target, status, input_chars, output_chars,"
            " num_turns, cost_usd, duration_ms, channel) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), conversation_id, source, target, status, input_chars, output_chars,
             num_turns, cost_usd, duration_ms, channel),
        )
        self.db.commit()

    def run(self, *, agent_id: str, conversation_id: str, kind: str, status: str, meta: dict,
            ts: float | None = None) -> None:
        """Account for one model run from what the host reported (`Reply.meta`). Numbers come through A2A as
        floats; counts are stored as integers."""
        usage, rate = meta.get("usage") or {}, meta.get("rate_limit") or {}
        windows = rate.get("windows") or {}

        def whole(value) -> int | None:
            return None if value is None else int(value)

        def window(name: str, part: str):
            value = (windows.get(name) or {}).get(part)
            return whole(value) if part == "resets_at" else value

        self.db.execute(
            "INSERT INTO runs (ts, agent_id, conversation_id, kind, status, num_turns, duration_ms, cost_usd,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, rate_status, rate_type,"
            " rate_utilization, rate_resets_at, " + ", ".join(c for c, _ in WINDOW_COLUMNS) + ")"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time() if ts is None else ts, agent_id, conversation_id, kind, status,
             whole(meta.get("num_turns")), whole(meta.get("duration_ms")), meta.get("cost_usd"),
             whole(usage.get("input_tokens")), whole(usage.get("output_tokens")),
             whole(usage.get("cache_read_tokens")), whole(usage.get("cache_write_tokens")),
             rate.get("status"), rate.get("rate_limit_type"), rate.get("utilization"), whole(rate.get("resets_at")),
             *(window(w, part) for w in WINDOWS for part in ("utilization", "resets_at"))),
        )
        self.db.commit()

    def windows(self) -> dict[str, dict]:
        """Each window of the subscription limit as last reported, with when: {"five_hour": {"utilization": 0.23,
        "resets_at": …, "seen": …}}. A window never reported is absent."""
        known = {}
        for name in WINDOWS:
            row = self.db.execute(f"SELECT {name}_utilization, {name}_resets_at, ts FROM runs WHERE"
                                  f" {name}_utilization IS NOT NULL ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
            if row:
                known[name] = dict(zip(("utilization", "resets_at", "seen"), row))
        return known

    def runs_since(self, ts: float) -> dict[str, int]:
        """How many runs ended how, since `ts`: {"done": 12, "limit": 1, ...}."""
        return dict(self.db.execute("SELECT status, COUNT(*) FROM runs WHERE ts >= ? GROUP BY status", (ts,)))

    def last_rate_limit(self) -> dict | None:
        """The subscription limit as the CLI last reported it, in any run; None until it has."""
        row = self.db.execute("SELECT rate_status, rate_type, rate_utilization, rate_resets_at, ts FROM runs"
                              " WHERE rate_status IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(zip(("status", "type", "utilization", "resets_at", "seen"), row)) if row else None

    def tokens(self, since: float) -> list[dict]:
        """Model runs since `since` by kind: how many, the four kinds of tokens, the CLI's cost estimate — the largest
        first. Cache reads are most of it: every turn reads the session again."""
        rows = self.db.execute("SELECT kind, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),"
                               " SUM(cache_write_tokens), SUM(cost_usd) FROM runs WHERE ts >= ? GROUP BY kind",
                               (since,)).fetchall()
        out = [{"kind": kind, "runs": runs, "input": a or 0, "output": b or 0, "cache_read": c or 0,
                "cache_write": d or 0, "cost_usd": round(cost or 0.0, 2)} for kind, runs, a, b, c, d, cost in rows]
        return sorted(out, key=lambda r: -(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]))

    def run_times(self, since: float) -> list[tuple[float, str]]:
        """(when, status) of every model run since `since`."""
        return [tuple(row) for row in self.db.execute("SELECT ts, status FROM runs WHERE ts >= ? ORDER BY ts",
                                                      (since,))]

    def conversation(self, agent_id: str) -> str:
        """The agent's current conversation id, created on first use."""
        row = self.db.execute("SELECT context_id FROM conversations WHERE agent_id = ?", (agent_id,)).fetchone()
        return row[0] if row else self.new_conversation(agent_id)

    def belongs(self, conversation_id: str, agent_id: str) -> bool:
        """Is this the agent's conversation: its current one, or one it has been asked in before (the protocol)."""
        return conversation_id == self.conversation(agent_id) or self.db.execute(
            "SELECT 1 FROM protocol WHERE conversation_id = ? AND target = ? LIMIT 1",
            (conversation_id, agent_id)).fetchone() is not None

    def new_conversation(self, agent_id: str) -> str:
        context_id = f"conv-{uuid.uuid4()}"
        self.db.execute("INSERT OR REPLACE INTO conversations VALUES (?, ?)", (agent_id, context_id))
        self.db.commit()
        return context_id

    def room(self, agent_id: str) -> str | None:
        row = self.db.execute("SELECT room_id FROM rooms WHERE agent_id = ?", (agent_id,)).fetchone()
        return row[0] if row else None

    def agent_by_room(self, room_id: str) -> str | None:
        row = self.db.execute("SELECT agent_id FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        return row[0] if row else None

    def save_room(self, agent_id: str, room_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO rooms VALUES (?, ?, '')", (agent_id, room_id))
        self.db.commit()

    def place(self, channel: str, agent_id: str) -> str | None:
        row = self.db.execute("SELECT place_id FROM places WHERE channel = ? AND agent_id = ?",
                              (channel, agent_id)).fetchone()
        return row[0] if row else None

    def agent_by_place(self, channel: str, place_id: str) -> str | None:
        row = self.db.execute("SELECT agent_id FROM places WHERE channel = ? AND place_id = ?",
                              (channel, place_id)).fetchone()
        return row[0] if row else None

    def save_place(self, channel: str, agent_id: str, place_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO places VALUES (?, ?, ?)", (channel, agent_id, place_id))
        self.db.commit()

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.db.commit()

    def save_sent(self, channel: str, native_id: str, event_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?, ?)", (channel, native_id, event_id))
        self.db.commit()

    def sent_event(self, channel: str, native_id: str) -> str | None:
        row = self.db.execute("SELECT event_id FROM sent WHERE channel = ? AND native_id = ?",
                              (channel, native_id)).fetchone()
        return row[0] if row else None

    def add_button(self, event_id: str, label: str, action: str, value: str, expires: float,
                   alone: bool = False) -> str:
        """Register a button and return its id: the only thing that goes into the messenger's callback data."""
        button_id = secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO buttons (id, event_id, label, action, value, expires, used, alone)"
                        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)", (button_id, event_id, label, action, value, expires,
                                                               int(alone)))
        self.db.commit()
        return button_id

    def use_button(self, button_id: str, now: float) -> tuple[str, str, str, str, bool] | None:
        """Spend a button: (event_id, label, action, value, alone), or None when it is unknown, expired or already
        used. Pressing one button spends the whole card, unless it is a button `alone`: then only itself."""
        row = self.db.execute("SELECT event_id, label, action, value, alone FROM buttons"
                              " WHERE id = ? AND used IS NULL AND expires > ?", (button_id, now)).fetchone()
        if row is None:
            return None
        if row[4]:
            self.db.execute("UPDATE buttons SET used = ? WHERE id = ?", (now, button_id))
        else:
            self.db.execute("UPDATE buttons SET used = ? WHERE event_id = ?", (now, row[0]))
        self.db.commit()
        return (*row[:4], bool(row[4]))

    def card_buttons(self, event_id: str, now: float) -> tuple[list[tuple[str, str]], list[str]]:
        """The card's buttons still alive, (label, id) in their order, and the labels already pressed."""
        rows = self.db.execute("SELECT label, id, used, expires FROM buttons WHERE event_id = ? ORDER BY rowid",
                               (event_id,)).fetchall()
        return ([(label, i) for label, i, used, expires in rows if used is None and expires > now],
                [label for label, i, used, expires in sorted((r for r in rows if r[2]), key=lambda r: r[2])])
