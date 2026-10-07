"""Scheduler: what has to happen at a given moment — the owner's reminders, the morning summary, a turn that waits
for the subscription limit to reset, a price drop a watch found. One table in the router's SQLite and one loop
in the router (`Core.clock`); no framework.

A reminder keeps the owner's wall time and zone as they were said (`local`, `tz`) and the moment it fires (`due`,
UTC). The text is written when the reminder is set; when it fires, the assistant tells it in her own words, and
code sends the text as it is when she cannot.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import clock

REMINDER, SUMMARY, RETRY, PRICE = "reminder", "summary", "retry", "price"
ACTIVE, RUNNING, SENT, CANCELLED = "active", "running", "sent", "cancelled"
MAX_TEXT = 500
SUMMARY_AT = "09:00"  # the morning summary, the owner's wall time
ID_NOTE = "id — для отмены и переноса; Владельцу его не называй, говори, о чём и когда."
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
                kind TEXT NOT NULL,                 -- reminder | summary | retry | price
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
        may reach the owner. A «#3» at the front reads as a label, and the model repeats it to the owner."""
        return f"{clock.day(job.due, job.tz)} — {job.text}" + (f" [id {job.id}]" if numbered else "")

    # --- the owner's reminders: what the assistant's tools do ------------------------------------

    def when(self, when: str, weekday: str, now: float) -> tuple[datetime | None, str]:
        """Check a moment the model named: it parses, the weekday the model believes in is the real one, it is
        still ahead. Returns (moment, "") or (None, the refusal the model reads)."""
        try:
            moment = clock.parse(when, self.tz)
        except ValueError as exc:
            return None, f"Не поставила: {exc}. Время — по {clock.label(self.tz)}."
        if weekday.strip():
            said = clock.weekday(weekday)
            if said is None:
                return None, f"Не поставила: «{weekday}» — не день недели."
            if said != moment.weekday():
                return None, (f"Не поставила: {moment:%Y-%m-%d} — {clock.WEEKDAYS[moment.weekday()]}, "
                              f"а не {clock.WEEKDAYS[said]}. Проверь дату.")
        if moment.timestamp() <= now:
            return None, (f"Не поставила: {clock.day(moment.timestamp(), self.tz)} уже прошло. "
                          f"Сейчас {clock.stamp(now, self.tz)}.")
        return moment, ""

    def add(self, text: str, when: str, weekday: str, now: float) -> tuple[bool, str]:
        text = " ".join(text.split())[:MAX_TEXT]
        if not text:
            return False, "Не поставила: нет текста напоминания."
        moment, refusal = self.when(when, weekday, now)
        if moment is None:
            return False, refusal
        due = moment.timestamp()
        for job in self.reminders():
            if job.due == due and job.text.casefold() == text.casefold():
                return True, f"Уже стоит: {self.line(job)}. {ID_NOTE}"
        cursor = self.db.execute("INSERT INTO jobs (kind, text, local, tz, due, status, created)"
                                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (REMINDER, text, f"{moment:%Y-%m-%dT%H:%M}", self.tz, due, ACTIVE, now))
        self.db.commit()
        return True, f"Поставила: {self.line(self.get(cursor.lastrowid))}. {ID_NOTE}"

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
            return "Активных напоминаний нет."
        return "Активные напоминания, ближайшие первыми:\n" + "\n".join(self.line(job) for job in jobs) + f"\n{ID_NOTE}"

    def _active_reminder(self, job_id: int) -> Job | None:
        job = self.get(job_id)
        return job if job and job.kind == REMINDER and job.status == ACTIVE else None

    def cancel(self, job_id: int) -> tuple[bool, str]:
        job = self._active_reminder(job_id)
        if job is None:
            return False, f"Нет активного напоминания с id {job_id}. Список — list_reminders."
        self.db.execute("UPDATE jobs SET status = ? WHERE id = ? AND status = ?", (CANCELLED, job.id, ACTIVE))
        self.db.commit()
        return True, f"Отменила: {self.line(job)}."

    def today(self, now: float) -> list[Job]:
        """Active reminders that fire before the owner's day is over."""
        midnight = clock.local(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return [job for job in self.reminders() if job.due < midnight.timestamp()]

    # --- the router's own jobs ---------------------------------------------------------------------

    def ensure_summary(self, now: float) -> None:
        """There is always a next morning summary in the table. One row per local day (a unique index), so the
        loop and a restart add nothing twice; a summary missed while the router was down stays due and goes late."""
        moment = clock.next_at(SUMMARY_AT, now, self.tz)
        local = f"{moment:%Y-%m-%dT%H:%M}"
        # Checked before the insert: a refused insert would still spend an id, and the loop asks every 30 s.
        self.db.execute("INSERT OR IGNORE INTO jobs (kind, text, local, tz, due, status, created)"
                        " SELECT ?, '', ?, ?, ?, ?, ? WHERE NOT EXISTS"
                        " (SELECT 1 FROM jobs WHERE kind = ? AND local = ?)",
                        (SUMMARY, local, self.tz, moment.timestamp(), ACTIVE, now, SUMMARY, local))
        self.db.commit()

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

    def done(self, job: Job) -> None:
        self.db.execute("UPDATE jobs SET status = ? WHERE id = ? AND status = ?", (SENT, job.id, RUNNING))
        self.db.commit()

    def fired_since(self, ts: float) -> int:
        """Reminders sent since `ts`: a number for the morning summary's health line."""
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind = ? AND status = ? AND fired >= ?",
                               (REMINDER, SENT, ts)).fetchone()[0]

    def move(self, job_id: int, when: str, weekday: str, now: float) -> tuple[bool, str]:
        job = self._active_reminder(job_id)
        if job is None:
            return False, f"Нет активного напоминания с id {job_id}. Список — list_reminders."
        moment, refusal = self.when(when, weekday, now)
        if moment is None:
            return False, refusal.replace("Не поставила", "Не перенесла")
        self.db.execute("UPDATE jobs SET local = ?, tz = ?, due = ? WHERE id = ? AND status = ?",
                        (f"{moment:%Y-%m-%dT%H:%M}", self.tz, moment.timestamp(), job.id, ACTIVE))
        self.db.commit()
        return True, f"Перенесла: {self.line(self.get(job.id))}."
