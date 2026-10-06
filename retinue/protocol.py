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
            """
        )
        if "channel" not in {row[1] for row in self.db.execute("PRAGMA table_info(protocol)")}:
            self.db.execute("ALTER TABLE protocol ADD COLUMN channel TEXT")
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

    def conversation(self, agent_id: str) -> str:
        """The agent's current conversation id, created on first use."""
        row = self.db.execute("SELECT context_id FROM conversations WHERE agent_id = ?", (agent_id,)).fetchone()
        return row[0] if row else self.new_conversation(agent_id)

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

    def add_button(self, event_id: str, label: str, action: str, value: str, expires: float) -> str:
        """Register a button and return its id: the only thing that goes into the messenger's callback data."""
        button_id = secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO buttons VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (button_id, event_id, label, action, value, expires))
        self.db.commit()
        return button_id

    def use_button(self, button_id: str, now: float) -> tuple[str, str, str, str] | None:
        """Spend a button: (event_id, label, action, value), or None when it is unknown, expired or already
        used. Pressing one button spends the whole card."""
        row = self.db.execute("SELECT event_id, label, action, value FROM buttons"
                              " WHERE id = ? AND used IS NULL AND expires > ?", (button_id, now)).fetchone()
        if row is None:
            return None
        self.db.execute("UPDATE buttons SET used = ? WHERE event_id = ?", (now, row[0]))
        self.db.commit()
        return row
