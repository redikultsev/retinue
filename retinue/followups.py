"""Follow-ups: a session that waits on the world — an answer to a letter, a price, a check that runs — asks the
platform to bring it back to the task later (`follow_up`), instead of promising the owner it will look again, which
nothing would make happen: a session cannot wake itself (the CLI's schedulers are denied, `approvals.BASE_DENY`).

On the platform's own server (`subs`): `follow_up(after_hours, why)` and `cancel_follow_up(id)`, for a session the
owner started. When it is due and the session is free — idle or closed, no run open, nothing queued, no limit being
waited out — the platform writes the session a message of its own and queues it as a turn (`by="follow_up"`), as a
report wakes a parent. The message is the platform's words with the follow-up's id and when it was set, never `why`:
that is the model's text, maybe repeating a page it read, and it is not to come back as an instruction. `why` is for
the owner: the call's arguments in the transcript show it. The turn rings when done, as a turn nobody watched.

A turn a follow-up started is no one's word: it cannot set another follow-up, and a sub-session it asks for is the
owner's card, as from a report's turn (`MACHINE_TURNS`). Limits: `OPEN_MAX` open per session, `ALL_MAX` in all,
from `MIN_HOURS` to `MAX_HOURS` ahead — past that the CLI's transcript may be gone (`cleanupPeriodDays`). An archived
session's follow-up, and one whose session's lane or track is gone, is dropped when it comes due; the owner's «Stop»
of the session drops its open ones and a fired one still queued.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import pings

if TYPE_CHECKING:
    from .platformd import Platform

FOLLOW, CANCEL = "follow_up", "cancel_follow_up"
OPEN_MAX = 3
ALL_MAX = 15
MIN_HOURS, MAX_HOURS = 1, 21 * 24
WHY_CHARS = 300
# Turns no one's word started — a report, a follow-up, the router's schedule writing to the voice (`voice.py`), a
# reaction to an event (`events.py`): they widen nothing (`subs`, the gate).
MACHINE_TURNS = ("report", "follow_up", "schedule", "event")

TOOLS = [
    {"name": FOLLOW,
     "description": "Come back to this task later: the platform writes to this session when it is due, and you go on "
                    "from where you stopped. Use it when the task waits on the world — a reply, a price, a build, a "
                    "date — instead of telling the owner you will check again. Say in `why` what to look at then; the "
                    "owner sees it.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["after_hours", "why"],
                     "properties": {"after_hours": {"type": "number", "minimum": MIN_HOURS, "maximum": MAX_HOURS,
                                                    "description": f"hours from now, {MIN_HOURS} to {MAX_HOURS}"},
                                    "why": {"type": "string", "description": "what to check then, one line"}}}},
    {"name": CANCEL,
     "description": "Cancel a follow-up this session set: when the task is done or no longer waits.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                     "properties": {"id": {"type": "string"}}}},
]

FROM_MACHINE = "This turn was started by a follow-up or a report, not by the owner: it cannot set a follow-up."
DUE = ("The follow-up {id} you set on {set} is due. Go back to the task: look at what changed, finish what you can, "
       "and say in your summary what still waits and when to look again — a follow-up cannot be set from this turn.")


class FollowUps:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS follow_ups (
                id TEXT PRIMARY KEY,
                call TEXT NOT NULL UNIQUE,          -- the tool call that set it: a call made again sets none more
                session TEXT NOT NULL,
                due REAL NOT NULL,
                why TEXT NOT NULL,
                made REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'open', -- open | fired | cancelled | dropped
                ended REAL
            );
            CREATE INDEX IF NOT EXISTS follow_ups_due ON follow_ups (state, due);
            """)
        self.store.db.commit()

    def _when(self, at: float) -> str:
        return datetime.fromtimestamp(at, ZoneInfo(self.platform.cfg.owner_tz)).strftime("%Y-%m-%d %H:%M")

    def open_for(self, session: str) -> list[dict]:
        rows = self.store.db.execute("SELECT id, due, why FROM follow_ups WHERE session = ? AND state = 'open' "
                                     "ORDER BY due", (session,))
        return [{"id": i, "due": due, "why": why} for i, due, why in rows]

    def make(self, session: dict, call_id: str, arguments: dict) -> tuple[str, bool]:
        hours, why = arguments.get("after_hours"), arguments.get("why")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not MIN_HOURS <= hours <= MAX_HOURS:
            return f"after_hours: a number from {MIN_HOURS} to {MAX_HOURS}.", True
        if not isinstance(why, str) or not why.strip():
            return "why: one line, what to check then.", True
        if (self.store.session(session["id"]) or session).get("turn_by") in MACHINE_TURNS:
            return FROM_MACHINE, True
        db, now = self.store.db, self.platform.clock()
        known = db.execute("SELECT id, due FROM follow_ups WHERE call = ?", (call_id,)).fetchone()
        if known is not None:
            return f"Follow-up {known[0]} is set for {self._when(known[1])}.", False
        if len(self.open_for(session["id"])) >= OPEN_MAX:
            return (f"This session has {OPEN_MAX} open follow-ups already — cancel one ({CANCEL}) or say in the "
                    "summary what to check."), True
        if db.execute("SELECT COUNT(*) FROM follow_ups WHERE state = 'open'").fetchone()[0] >= ALL_MAX:
            return "The platform holds as many follow-ups as it takes — say in the summary what to check.", True
        follow, due = "f" + secrets.token_hex(3), now + float(hours) * 3600
        db.execute("INSERT INTO follow_ups (id, call, session, due, why, made) VALUES (?, ?, ?, ?, ?, ?)",
                   (follow, call_id, session["id"], due, " ".join(why.split())[:WHY_CHARS], now))
        db.commit()
        return f"Follow-up {follow} is set for {self._when(due)} ({self.platform.cfg.owner_tz}).", False

    def cancel(self, session: dict, arguments: dict) -> tuple[str, bool]:
        follow = arguments.get("id")
        cursor = self.store.db.execute("UPDATE follow_ups SET state = 'cancelled', ended = ? WHERE id = ? AND "
                                       "session = ? AND state = 'open'", (self.platform.clock(), follow, session["id"]))
        self.store.db.commit()
        return (f"Follow-up {follow} cancelled.", False) if cursor.rowcount else (f"No open follow-up {follow}.", True)

    def stopped(self, session: str) -> None:
        """The owner stopped the session: what it meant to come back to goes, a fired one not yet started too."""
        db = self.store.db
        with db:
            db.execute("UPDATE follow_ups SET state = 'dropped', ended = ? WHERE session = ? AND state = 'open'",
                       (self.platform.clock(), session))
            db.execute("DELETE FROM queue WHERE session = ? AND by = 'follow_up' AND attempt IS NULL", (session,))

    def tick(self) -> list[str]:
        """The follow-ups due whose sessions are free: each one's turn queued. The sessions that got one."""
        db, now = self.store.db, self.platform.clock()
        if self.platform.subs.limit_on(now):
            return []
        busy = {r["session"] for r in self.store.open_runs()} | {q["session"] for q in self.store.queued()}
        given = []
        for follow, session_id, made in db.execute("SELECT id, session, made FROM follow_ups WHERE state = 'open' "
                                                   "AND due <= ? ORDER BY due", (now,)).fetchall():
            session = self.store.session(session_id)
            if session is None or session.get("archived") or session["lane"] not in self.platform.live or \
                    session["track"] not in self.platform.tracks:  # nowhere it could ever run
                db.execute("UPDATE follow_ups SET state = 'dropped', ended = ? WHERE id = ?", (now, follow))
                db.commit()
                continue
            if session_id in busy or session_id in given or session["state"] not in ("idle", "closed"):
                continue  # the next tick tries again
            text = DUE.format(id=follow, set=self._when(made))
            with db:  # fired and queued together; the input's own commit comes after
                db.execute("UPDATE follow_ups SET state = 'fired', ended = ? WHERE id = ?", (now, follow))
                self.store.enqueue(session_id, text, now, by="follow_up", commit=False)
            self.store.add_input(f"follow:{follow}", session_id, text, now, by="follow_up")
            pings.unattended(self.store, session_id)  # nobody watched it start: its end rings
            self.platform.life.queued(session_id, now)
            given.append(session_id)
        return given

