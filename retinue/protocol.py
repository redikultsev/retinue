"""Protocol: append-only log of every exchange the router carries, plus the router's own state.

Field names follow the OpenTelemetry GenAI conventions where they exist, so a trace
viewer (Phoenix, Datasette) can read it without translation.
"""

from __future__ import annotations

import sqlite3
import time
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
            CREATE TABLE IF NOT EXISTS rooms (
                agent_id TEXT PRIMARY KEY,          -- '_protocol' for the protocol room
                room_id TEXT NOT NULL,
                context_id TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def log(self, *, conversation_id: str, source: str, target: str, status: str, input_chars: int,
            output_chars: int, num_turns: int | None = None, cost_usd: float | None = None,
            duration_ms: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO protocol (ts, conversation_id, source, target, status, input_chars, output_chars,"
            " num_turns, cost_usd, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), conversation_id, source, target, status, input_chars, output_chars,
             num_turns, cost_usd, duration_ms),
        )
        self.db.commit()

    def room(self, agent_id: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT room_id, context_id FROM rooms WHERE agent_id = ?", (agent_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def agent_by_room(self, room_id: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT agent_id, context_id FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def save_room(self, agent_id: str, room_id: str, context_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO rooms VALUES (?, ?, ?)", (agent_id, room_id, context_id))
        self.db.commit()
