"""Scheduler: what has to happen at a given moment — the owner's reminders, the morning summary, a turn that waits
for the subscription limit to reset, a price drop a watch found. One table in the router's SQLite and one loop
in the router (`Core.clock`); no framework.

A reminder keeps the owner's wall time and zone as they were said (`local`, `tz`) and the moment it fires (`due`,
UTC). The text is written when the reminder is set; when it fires, the assistant tells it in her own words, and
code sends the text as it is when she cannot. A repeating one (`data.repeat`) is the same row moved to its next
wall time when it has fired: cancelling it cancels the series, moving it moves the series.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import clock

REMINDER, SUMMARY, RETRY, PRICE, DIGEST, WEEKLY = "reminder", "summary", "retry", "price", "digest", "weekly"
ACTIVE, RUNNING, SENT, CANCELLED = "active", "running", "sent", "cancelled"
MAX_TEXT = 500
SUMMARY_AT = "09:00"  # the morning summary, the owner's wall time
WEEKLY_AT = (6, "18:00")  # the weekly review of his goals: Sunday (Monday is 0), his wall time
ID_NOTE = "The id is for cancelling and moving; do not tell it to the owner, say what and when."
REPEATS = {"daily": "every day", "weekdays": "on weekdays", "weekly": "every week", "monthly": "every month"}
_COLUMNS = "id, kind, text, local, tz, due, status, data"


@dataclass
class Job:
    id: int
    kind: str
    text: str
    local: str      # the owner's wall time: 2026-10-09T18:00
    tz: str
    due: float      # UTC epoch seconds
    status: str
    data: dict

    @property
    def key(self) -> str:
        """What is sent at most once: this row at this moment. A moved reminder has a new key."""
        return f"{self.id}:{self.due:.0f}"


class Scheduler:
    def __init__(self, db: sqlite3.Connection, tz: str = clock.DEFAULT_TZ) -> None:
        self.db, self.tz = db, tz
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                 -- reminder | summary | retry | price | digest | weekly
                text TEXT NOT NULL,                 -- a reminder's words, written in advance and sent as they are
                local TEXT NOT NULL,                -- the owner's wall time: 2026-10-09T18:00
                tz TEXT NOT NULL,                   -- IANA zone of `local`
                due REAL NOT NULL,                  -- when it fires, UTC epoch seconds
                status TEXT NOT NULL,               -- active | running | sent | cancelled
                data TEXT NOT NULL DEFAULT '{}',    -- retry: which owner messages wait; price: the alert
                created REAL NOT NULL,
                fired REAL                          -- when it was taken for sending
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, due);
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_summary_a_day ON jobs (local) WHERE kind = 'summary';
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_digest_a_day ON jobs (local) WHERE kind = 'digest';
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_weekly_a_week ON jobs (local) WHERE kind = 'weekly';
            """
        )
        # A summary or a retry the previous process was running when it stopped: run it again. Better a second
        # run than an answer the owner was promised and never gets.
        self.db.execute("UPDATE jobs SET status = ? WHERE status = ?", (ACTIVE, RUNNING))
        self.db.commit()

    @staticmethod
    def _job(row) -> Job:
        return Job(row[0], row[1], row[2], row[3], row[4], row[5], row[6], json.loads(row[7]))

    def line(self, job: Job, numbered: bool = True) -> str:
        """How a reminder is named: with its id as a service mark at the end for the tools, without it wherever it
        may reach the owner. A «#3» at the front reads as a label, and the model repeats it to the owner. The time
        is the owner's present zone: a one-off reminder set at home reads in Tokyo time once he is there."""
        repeat = REPEATS.get(job.data.get("repeat", ""), "")
        return (f"{clock.day(job.due, self.tz)} — {job.text}" + (f" ({repeat})" if repeat else "")
                + (f" [id {job.id}]" if numbered else ""))

    # --- the owner's reminders: what the assistant's tools do ------------------------------------

    def when(self, when: str, weekday: str, now: float) -> tuple[datetime | None, str]:
        """Check a moment the model named: it parses, the weekday the model believes in is the real one, it is
        still ahead. Returns (moment, "") or (None, the refusal the model reads)."""
        try:
            moment = clock.parse(when, self.tz)
        except ValueError as exc:
            return None, f"Not set: {exc}. The time is in {clock.label(self.tz)}."
        if weekday.strip():
            said = clock.weekday(weekday)
            if said is None:
                return None, f"Not set: «{weekday}» is not a day of the week."
            if said != moment.weekday():
                return None, (f"Not set: {moment:%Y-%m-%d} is a {clock.WEEKDAYS[moment.weekday()]}, "
                              f"not a {clock.WEEKDAYS[said]}. Check the date.")
        if moment.timestamp() <= now:
            return None, (f"Not set: {clock.day(moment.timestamp(), self.tz)} has already passed. "
                          f"It is now {clock.stamp(now, self.tz)}.")
        return moment, ""

    def add(self, text: str, when: str, weekday: str, now: float, repeat: str = "") -> tuple[bool, str]:
        text = " ".join(text.split())[:MAX_TEXT]
        repeat = repeat.strip().lower()
        if not text:
            return False, "Not set: the reminder has no text."
        if repeat and repeat not in REPEATS:
            return False, f"Not set: unknown repeat «{repeat}». Allowed: {', '.join(REPEATS)} or empty."
        moment, refusal = self.when(when, weekday, now)
        if moment is None:
            return False, refusal
        if repeat == "weekdays" and moment.weekday() >= 5:
            return False, "Not set: it repeats on weekdays, but the first time falls on a weekend. Start on a weekday."
        due = moment.timestamp()
        for job in self.reminders():
            if job.due == due and job.text.casefold() == text.casefold():
                return True, f"Already set: {self.line(job)}. {ID_NOTE}"
        data = {"repeat": repeat, "day": moment.day} if repeat else {}
        cursor = self.db.execute("INSERT INTO jobs (kind, text, local, tz, due, status, data, created)"
                                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (REMINDER, text, f"{moment:%Y-%m-%dT%H:%M}", self.tz, due, ACTIVE, json.dumps(data),
                                  now))
        self.db.commit()
        return True, f"Set: {self.line(self.get(cursor.lastrowid))}. {ID_NOTE}"

    def get(self, job_id: int) -> Job | None:
        row = self.db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def reminders(self, limit: int = 50) -> list[Job]:
        """Active reminders, the nearest first."""
        rows = self.db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE kind = ? AND status = ? ORDER BY due, id LIMIT ?",
                               (REMINDER, ACTIVE, limit)).fetchall()
        return [self._job(row) for row in rows]

    def listing(self) -> str:
        jobs = self.reminders()
        if not jobs:
            return "No active reminders."
        return "Active reminders, nearest first:\n" + "\n".join(self.line(job) for job in jobs) + f"\n{ID_NOTE}"

    def _active_reminder(self, job_id: int) -> Job | None:
        job = self.get(job_id)
        return job if job and job.kind == REMINDER and job.status == ACTIVE else None

    def cancel(self, job_id: int) -> tuple[bool, str]:
        job = self._active_reminder(job_id)
        if job is None:
            return False, f"No active reminder with id {job_id}. The list is list_reminders."
        self.db.execute("UPDATE jobs SET status = ? WHERE id = ? AND status = ?", (CANCELLED, job.id, ACTIVE))
        self.db.commit()
        return True, f"Cancelled: {self.line(job)}."

    def today(self, now: float) -> list[Job]:
        """Active reminders that fire before the owner's day is over."""
        midnight = clock.local(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return [job for job in self.reminders() if job.due < midnight.timestamp()]

    # --- the router's own jobs ---------------------------------------------------------------------

    def ensure_summary(self, now: float) -> None:
        """There is always a next morning summary in the table. One row per local day (a unique index), so the
        loop and a restart add nothing twice; a summary missed while the router was down stays due and goes late."""
        self._ensure_daily(SUMMARY, SUMMARY_AT, now)

    def ensure_digest(self, now: float, at: str) -> None:
        """The evening list of the assistant's commits to the knowledge base: the same one-a-day row as the summary."""
        self._ensure_daily(DIGEST, at, now)

    def ensure_weekly(self, now: float) -> None:
        """The weekly review of the owner's goals: the next Sunday 18:00, one row a week, like the summary."""
        weekday, at = WEEKLY_AT
        moment = clock.next_at(at, now, self.tz)
        moment += timedelta(days=(weekday - moment.weekday()) % 7)
        self._ensure_at(WEEKLY, moment, now)

    def _ensure_daily(self, kind: str, at: str, now: float) -> None:
        self._ensure_at(kind, clock.next_at(at, now, self.tz), now)

    def _ensure_at(self, kind: str, moment: datetime, now: float) -> None:
        local = f"{moment:%Y-%m-%dT%H:%M}"
        # Checked before the insert: a refused insert would still spend an id, and the loop asks every 30 s.
        self.db.execute("INSERT OR IGNORE INTO jobs (kind, text, local, tz, due, status, created)"
                        " SELECT ?, '', ?, ?, ?, ?, ? WHERE NOT EXISTS"
                        " (SELECT 1 FROM jobs WHERE kind = ? AND local = ?)",
                        (kind, local, self.tz, moment.timestamp(), ACTIVE, now, kind, local))
        self.db.commit()

    def rezone(self, tz: str, now: float) -> tuple[int, int]:
        """The owner is in another zone now. What was said as a wall time that recurs — a repeating reminder, the
        morning summary, the weekly review, the evening list — keeps its wall time and moves to the new zone: «every
        day at 9» is 9 where he wakes up. A one-off reminder, a retry and a price drop keep their instant: «call at
        18:00 at home» was a moment, and it is told in the new zone's clock. Returns (moved, kept).

        A repeating reminder whose wall time has already passed today in the new zone waits for its next one (sending
        it now would be late for a reason that is not the router's); the router's own job fires at once instead — the
        summary he has not had today is still worth having.

        A job firing right now (running) only changes its zone: its wall time is the one it fired for, and `done`
        reads the row back, so a series moves to the next wall time in the new zone."""
        moved = kept = 0
        rows = self.db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE status IN (?, ?)", (ACTIVE, RUNNING)).fetchall()
        for job in map(self._job, rows):
            follows = job.kind in (SUMMARY, WEEKLY, DIGEST) or (job.kind == REMINDER and job.data.get("repeat"))
            if not follows or job.tz == tz:
                kept += job.kind == REMINDER and job.tz != tz and job.status == ACTIVE
                continue
            if job.status == RUNNING:
                self.db.execute("UPDATE jobs SET tz = ? WHERE id = ? AND status = ?", (tz, job.id, RUNNING))
                moved += 1
                continue
            moment = datetime.strptime(job.local, "%Y-%m-%dT%H:%M").replace(tzinfo=clock.zone(tz))
            if moment.timestamp() <= now:
                if job.kind == REMINDER:
                    moment = following(Job(job.id, job.kind, job.text, job.local, tz, moment.timestamp(), job.status,
                                           job.data), now)
                else:
                    moment = clock.local(now, tz)
            self.db.execute("UPDATE jobs SET local = ?, tz = ?, due = ? WHERE id = ? AND status = ?",
                            (f"{moment:%Y-%m-%dT%H:%M}" if job.kind == REMINDER else job.local, tz,
                             moment.timestamp(), job.id, ACTIVE))
            moved += 1
        self.tz = tz
        self.db.commit()
        return moved, kept

    def add_retry(self, data: dict, due: float, now: float) -> int:
        """Run the same owner messages again when the subscription limit resets."""
        cursor = self.db.execute("INSERT INTO jobs (kind, text, local, tz, due, status, data, created)"
                                 " VALUES (?, '', ?, ?, ?, ?, ?, ?)",
                                 (RETRY, f"{clock.local(due, self.tz):%Y-%m-%dT%H:%M}", self.tz, due, ACTIVE,
                                  json.dumps(data), now))
        self.db.commit()
        return cursor.lastrowid

    def add_price(self, alert: dict, now: float) -> bool:
        """Keep a price drop a watch found (`travel.alert`), to be told at once. One row per alert: a second look
        after a restart adds nothing. travel-ops numbers its alerts anew on a new volume, so an alert is its
        number, its watch and the moment it was seen. Returns whether it was new."""
        key = [alert.get("alert_id"), alert.get("watch_id"), alert.get("seen_at")]
        if any(json.loads(data).get("key") == key for (data,) in
               self.db.execute("SELECT data FROM jobs WHERE kind = ?", (PRICE,))):
            return False
        self.db.execute("INSERT INTO jobs (kind, text, local, tz, due, status, data, created)"
                        " VALUES (?, '', ?, ?, ?, ?, ?, ?)",
                        (PRICE, f"{clock.local(now, self.tz):%Y-%m-%dT%H:%M}", self.tz, now, ACTIVE,
                         json.dumps({**alert, "key": key}, ensure_ascii=False), now))
        self.db.commit()
        return True

    # --- firing: what the router's loop does -----------------------------------------------------

    def due(self, now: float) -> list[Job]:
        rows = self.db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE status = ? AND due <= ? ORDER BY due, id",
                               (ACTIVE, now)).fetchall()
        return [self._job(row) for row in rows]

    def claim(self, job: Job, now: float) -> bool:
        """Take a due job, at most once: a compare-and-set on this row at this moment (`job.key`). Every job runs
        the model for seconds or minutes, so it is taken as running and becomes sent with `done`; a restart in
        between runs it again. The price is a second message if the router dies in the instant between sending
        and `done` — a duplicate rather than a loss."""
        cursor = self.db.execute("UPDATE jobs SET status = ?, fired = ? WHERE id = ? AND status = ? AND due = ?",
                                 (RUNNING, now, job.id, ACTIVE, job.due))
        self.db.commit()
        return cursor.rowcount == 1

    def postpone(self, job: Job, due: float) -> None:
        """A running job that could not finish: active again, due later. The next pass takes it."""
        self.db.execute("UPDATE jobs SET status = ?, due = ? WHERE id = ? AND status = ?", (ACTIVE, due, job.id, RUNNING))
        self.db.commit()

    def done(self, job: Job, now: float | None = None) -> None:
        """A job has run. A repeating reminder is not over: the same row, active again at its next wall time after
        both its own moment and now — the ones missed while the router was down are not sent in a burst."""
        after = max(job.due, time.time() if now is None else now)
        job = self.get(job.id) or job  # the row as it is now: a new zone may have come while it ran
        moment = following(job, after) if job.kind == REMINDER else None
        if moment is not None:
            self.db.execute("UPDATE jobs SET status = ?, local = ?, due = ? WHERE id = ? AND status = ?",
                            (ACTIVE, f"{moment:%Y-%m-%dT%H:%M}", moment.timestamp(), job.id, RUNNING))
        else:
            self.db.execute("UPDATE jobs SET status = ? WHERE id = ? AND status = ?", (SENT, job.id, RUNNING))
        self.db.commit()

    def fired_since(self, ts: float) -> int:
        """Reminders sent since `ts`: a number for the morning summary's health line. A repeating one is active
        again after it fires, so it is counted by when it fired, not by its status."""
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind = ? AND status != ? AND fired >= ?",
                               (REMINDER, CANCELLED, ts)).fetchone()[0]

    def move(self, job_id: int, when: str, weekday: str, now: float) -> tuple[bool, str]:
        job = self._active_reminder(job_id)
        if job is None:
            return False, f"No active reminder with id {job_id}. The list is list_reminders."
        moment, refusal = self.when(when, weekday, now)
        if moment is None:
            return False, refusal.replace("Not set:", "Not moved:", 1)
        if job.data.get("repeat") == "weekdays" and moment.weekday() >= 5:
            return False, "Not moved: it repeats on weekdays, but the new date is a weekend. Choose a weekday."
        data = {**job.data, "day": moment.day} if job.data.get("repeat") else job.data  # a series moves whole
        self.db.execute("UPDATE jobs SET local = ?, tz = ?, due = ?, data = ? WHERE id = ? AND status = ?",
                        (f"{moment:%Y-%m-%dT%H:%M}", self.tz, moment.timestamp(), json.dumps(data), job.id, ACTIVE))
        self.db.commit()
        return True, f"Moved: {self.line(self.get(job.id))}."


def following(job: Job, after: float) -> datetime | None:
    """The next wall time of a repeating reminder strictly after `after`, in its own zone: the same hour and minute;
    a monthly one on its day of the month, or the month's last day when the month is shorter (the 31st → 30 April
    → 31 May). None for a reminder that does not repeat."""
    repeat = job.data.get("repeat", "")
    if repeat not in REPEATS:
        return None
    moment = datetime.strptime(job.local, "%Y-%m-%dT%H:%M").replace(tzinfo=clock.zone(job.tz))
    anchor = int(job.data.get("day") or moment.day)
    while moment.timestamp() <= after:
        if repeat == "monthly":
            year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
            moment = moment.replace(year=year, month=month, day=min(anchor, calendar.monthrange(year, month)[1]))
            continue
        moment += timedelta(days=7 if repeat == "weekly" else 1)
        while repeat == "weekdays" and moment.weekday() >= 5:
            moment += timedelta(days=1)
    return moment
