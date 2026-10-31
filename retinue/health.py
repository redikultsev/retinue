"""The stack's containers, told to the owner: one message when something goes bad, one when it is well again.

The host looks at Docker every two minutes (deploy/health/retinue-health.py) and writes health.json; the router reads
it on its clock. Counted by code, never written by the model. What was said and to whom is in the router's SQLite, so
a router that restarts does not say it again — and a router that crashed tells of its own restart once it is back.

- A container that is not running, or whose healthcheck says unhealthy, is bad after two checks in a row: a deploy
  that recreates containers is caught between two states for one check at most. It is well again after two good
  checks in a row, so a healthcheck that flaps is not told as recovered at every good one.
- A restart by Docker (its count grew on the same container) is said at once; a new container is a deploy, not a
  restart. Restarted and running again: one line, and nothing to recover from.
- At most one notice per container an hour (`NOTICE_S`): an OOM loop or a flapping check is counted for the morning
  summary, not sent every few minutes. Its recovery then goes unsaid too.
- Stopped with exit code 0 is the operator's own stop: told once, «stopped manually», never counted as a failure.
- A health.json older than ten minutes is itself bad: the host's timer has stopped, and nothing else would say so.
- In the owner's quiet hours (the assistant's, `proactive.py`) a notice is held and the held ones go as one message
  when they end. What is told at once anyway: an outage — Docker not answering or showing no container, a stale
  file, the router or the platform down.

The router down is the one thing this cannot tell: the host holds no bot token by design. The owner learns it from
the morning summary that does not come.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time

from . import clock

log = logging.getLogger("retinue.health")

BAD_CHECKS = 2          # in a row, before a container that is down or unhealthy is told
GOOD_CHECKS = 2         # in a row, before one told as bad is told as well again
NOTICE_S = 3600         # one notice per container at most this often; the rest are counted
STALE_S = 10 * 60       # a health.json older than this: the host's timer stopped
EVENTS_DAYS = 30        # how long what went bad is kept for the summary's count
STALE = "#status"       # the row of the file itself
DOCKER = "docker"       # the row of Docker itself: not answering, or no container at all
CRITICAL = {STALE, DOCKER, "router", "platform"}  # an outage: told at once, quiet hours or not
MANUAL = "stopped manually"
MANUAL_RU = "остановлен вручную"  # rows kept in SQLite before the English: still an operator's stop


class Watch:
    def __init__(self, path: str, db: sqlite3.Connection) -> None:
        self.path = path
        self.db = db
        self.core = None
        self.tasks: set[asyncio.Task] = set()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_services (
                name TEXT PRIMARY KEY,              -- the compose service; STALE, DOCKER for the file and Docker
                container TEXT NOT NULL DEFAULT '', -- its id at the last check: another one is a deploy, not a restart
                restarts INTEGER NOT NULL DEFAULT 0,  -- Docker's restart count at the last check
                failing INTEGER NOT NULL DEFAULT 0, -- checks in a row it was down or unhealthy
                good INTEGER NOT NULL DEFAULT 0,    -- checks in a row it was well, while `bad`
                bad TEXT,                           -- what is wrong with it; NULL: nothing is
                silent INTEGER NOT NULL DEFAULT 0,  -- `bad` was not told (a notice within the hour): nor is its end
                since REAL,                         -- when it went bad
                told REAL NOT NULL DEFAULT 0        -- the last notice about it: one an hour at most
            );
            CREATE TABLE IF NOT EXISTS health_checks (id INTEGER PRIMARY KEY CHECK (id = 1), checked REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS health_events (ts REAL NOT NULL, name TEXT NOT NULL, what TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health_held (ts REAL NOT NULL, line TEXT NOT NULL);  -- quiet hours' notices
            """)
        db.commit()

    def attach(self, core) -> None:
        self.core = core

    # --- the file ----------------------------------------------------------------------------------------------------

    def read(self) -> dict | None:
        """health.json; None when there is none (the host has not been set up yet). An unreadable one counts by its
        file's time: still being unreadable ten minutes on is the same as a timer that stopped."""
        try:
            with open(self.path) as file:
                report = json.load(file)
            if not isinstance(report, dict) or not isinstance(report.get("checked"), (int, float)):
                raise ValueError(report)
            return report
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            try:
                return {"checked": 0.0, "written": os.stat(self.path).st_mtime, "unreadable": True}
            except OSError:
                return None

    # --- the clock -------------------------------------------------------------------------------------------------

    def step(self, now: float | None = None) -> None:
        """Called by the router's clock: whatever is new to say, said in a task of its own."""
        now = time.time() if now is None else now
        try:
            said = self.check(now)
        except Exception:
            log.exception("health: the check failed")
            return
        if said and self.core:
            task = asyncio.create_task(self.core.tell_owner(said))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def check(self, now: float) -> str:
        """What to tell the owner now, "" for nothing. Each health.json is counted once."""
        report = self.read()
        if report is None:
            return self._say(now, [], [])
        written = report.get("written", report["checked"])
        stale = self._row(STALE)
        if now - written > STALE_S:
            bad = []
            if not stale["bad"]:
                why = ("status unreadable" if report.get("unreadable")
                       else f"last one {clock.ago(now - written)} ago")
                self._mark(STALE, f"the container watch is silent: {why}", now, silent=False)
                bad.append((STALE, f"- the container watch is silent ({why}): the retinue-health timer on the host is "
                                   "not running, and nobody is left to report a container going down"))
            return self._say(now, bad, [])
        recovered = []
        if stale["bad"]:
            self._clear(STALE)
            recovered.append((STALE, "the container watch"))
        last = self.db.execute("SELECT checked FROM health_checks WHERE id = 1").fetchone()
        if report.get("unreadable") or (last and report["checked"] <= last[0]):
            return self._say(now, [], recovered)
        self.db.execute("INSERT INTO health_checks (id, checked) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET "
                        "checked = excluded.checked", (report["checked"],))
        containers = list(report.get("containers") or [])
        outage = bool(report.get("error")) or not containers
        if report.get("error"):  # Docker did not answer: as one container that is down
            containers = [{"name": DOCKER, "state": "error", "error": str(report["error"])}]
        elif not containers:     # not even the router's own: the project's label, or the whole stack, is gone
            containers = [{"name": DOCKER, "state": "empty"}]
        bad, seen = [], set()
        for container in containers:
            name = str(container.get("name") or "?")
            seen.add(name)
            said, back = self._judge(name, container, now)
            bad += [(name, line) for line in said]
            recovered += [(name, name) for name in back]
        # Docker silent or empty: every container's row is kept, with the counts it had
        gone = [] if outage else self.db.execute("SELECT name, bad, silent FROM health_services WHERE name != ?",
                                                 (STALE,)).fetchall()
        for name, was, silent in gone:
            if name not in seen:  # taken away (a profile turned off), or Docker answers again: nothing left to watch
                self.db.execute("DELETE FROM health_services WHERE name = ?", (name,))
                if was and not silent:
                    recovered.append((name, name if name == DOCKER else f"{name} (the container is gone)"))
        self.db.execute("DELETE FROM health_events WHERE ts < ?", (now - EVENTS_DAYS * 86400,))
        self.db.commit()
        return self._say(now, bad, recovered)

    def _judge(self, name: str, container: dict, now: float) -> tuple[list[str], list[str]]:
        """One container at one check: the lines that it went bad, the names that it is well again."""
        row = self._row(name)
        cid, restarts = str(container.get("id") or ""), int(container.get("restarts") or 0)
        restarted = row["container"] == cid and restarts > row["restarts"]
        wrong = _wrong(container)
        failing = row["failing"] + 1 if wrong else 0
        good = row["good"] + 1 if row["bad"] and not wrong and not restarted else 0
        self.db.execute("UPDATE health_services SET container = ?, restarts = ?, failing = ?, good = ? WHERE name = ?",
                        (cid, restarts, failing, good, name))
        if row["bad"]:
            if good >= GOOD_CHECKS:
                self._clear(name)
                return [], [] if row["silent"] else [name]
            return [], []
        recent = now - row["told"] < NOTICE_S  # told about it within the hour: counted, not sent
        if restarted and not wrong:
            self._event(name, "restarted", now)
            if recent:
                return [], []
            self.db.execute("UPDATE health_services SET told = ? WHERE name = ?", (now, name))
            return [f"- {name}: restarted by Docker ({restarts} in the container's life), running now"], []
        if wrong and (restarted or failing >= BAD_CHECKS):
            what = f"{wrong}, restarted by Docker ({restarts} in the container's life)" if restarted else wrong
            self._mark(name, what, now, silent=recent)
            return [] if recent else [f"- {name}: {what}"], []
        return [], []

    # --- what is said, and when ------------------------------------------------------------------------------------

    def _say(self, now: float, bad: list[tuple[str, str]], recovered: list[tuple[str, str]]) -> str:
        """The message for now. In quiet hours what is not an outage waits in `health_held`; out of them, what waited
        goes first, as one message with the times."""
        quiet = self._quiet(now)
        held = [line for name, line in bad if quiet and name not in CRITICAL]
        held += [f"- OK again: {said}" for name, said in recovered if quiet and name not in CRITICAL]
        self.db.executemany("INSERT INTO health_held (ts, line) VALUES (?, ?)", [(now, line) for line in held])
        bad = [line for name, line in bad if not quiet or name in CRITICAL]
        recovered = [said for name, said in recovered if not quiet or name in CRITICAL]
        parts = []
        if not quiet:
            waited = self.db.execute("SELECT ts, line FROM health_held ORDER BY rowid").fetchall()
            if waited:
                self.db.execute("DELETE FROM health_held")
                tz = self.core.tz if self.core else clock.DEFAULT_TZ
                parts.append("\n".join(["Server during quiet hours:", *(f"{line[:2]}{clock.local(ts, tz):%H:%M} {line[2:]}"
                                                                  for ts, line in waited)]))
        self.db.commit()
        if bad:
            parts.append("\n".join(["Server: not OK.", *bad]))
        if recovered:
            parts.append(f"Server: OK again — {', '.join(recovered)}.")
        return "\n\n".join(parts)

    def _quiet(self, now: float) -> bool:
        """The owner's quiet hours, the assistant's own (`/initiative quiet`): no core — none."""
        if not self.core:
            return False
        from .proactive import DEFAULT_QUIET, quiet_at

        hours = self.core.proactive.quiet() if getattr(self.core, "proactive", None) else DEFAULT_QUIET
        return quiet_at(clock.local(now, self.core.tz).hour, hours)

    # --- the morning summary ----------------------------------------------------------------------------------------

    def health(self, now: float) -> str:
        """The containers' part of the summary's health line: how many are well, which are not, what failed in a day.
        A container its operator stopped is named apart, not as a failure."""
        report = self.read()
        if report is None:
            return "container watch not set up"
        if now - report.get("written", report["checked"]) > STALE_S:
            return "container watch silent"
        if report.get("unreadable"):
            return "container status unreadable"
        if report.get("error"):
            return f"Docker did not answer the watch: {str(report['error']).rstrip('. ')}"
        names = sorted(str(c.get("name") or "?") for c in report.get("containers") or [])
        if not names:
            return "containers: none visible"
        rows = self.db.execute("SELECT name, bad FROM health_services WHERE bad IS NOT NULL AND name NOT IN (?, ?)",
                               (STALE, DOCKER)).fetchall()
        stopped = sorted(name for name, bad in rows if _manual(bad) and name in names)
        bad = {name: said for name, said in rows if not _manual(said) and name in names}
        total = len(names) - len(stopped)
        text = (f"containers: {total - len(bad)} of {total} OK, not OK — "
                + ", ".join(f"{name} ({bad[name]})" for name in sorted(bad))) if bad else \
            f"containers: all {total} OK"
        if stopped:
            text += f", {MANUAL} — {', '.join(stopped)}"
        failed = [name for name, in self.db.execute("SELECT name FROM health_events WHERE ts >= ? AND name != ? "
                                                    "ORDER BY ts", (now - 86400, STALE)).fetchall()]
        if failed:
            text += f", failures in a day — {len(failed)} ({', '.join(dict.fromkeys(failed))})"
        return text

    # --- rows -------------------------------------------------------------------------------------------------------

    def _row(self, name: str) -> dict:
        if self.db.execute("INSERT OR IGNORE INTO health_services (name) VALUES (?)", (name,)).rowcount:
            self.db.commit()
        row = self.db.execute("SELECT container, restarts, failing, good, bad, silent, told FROM health_services "
                              "WHERE name = ?", (name,)).fetchone()
        return dict(zip(("container", "restarts", "failing", "good", "bad", "silent", "told"), row))

    def _mark(self, name: str, what: str, now: float, silent: bool) -> None:
        """Bad from now on; told unless `silent`. An operator's stop is not a failure: not counted."""
        self.db.execute("UPDATE health_services SET bad = ?, since = ?, silent = ?, good = 0, "
                        "told = CASE WHEN ? THEN told ELSE ? END WHERE name = ?",
                        (what, now, int(silent), int(silent), now, name))
        if not _manual(what):
            self._event(name, what, now)
        self.db.commit()

    def _clear(self, name: str) -> None:
        self.db.execute("UPDATE health_services SET bad = NULL, since = NULL, silent = 0, good = 0 WHERE name = ?",
                        (name,))
        self.db.commit()

    def _event(self, name: str, what: str, now: float) -> None:
        self.db.execute("INSERT INTO health_events (ts, name, what) VALUES (?, ?, ?)", (now, name, what))


def _manual(bad: str) -> bool:
    """An operator's stop, in today's words or in those of a row kept before them."""
    return bad.startswith((MANUAL, MANUAL_RU))


def _wrong(container: dict) -> str:
    """What is wrong with a container at this check, in the owner's words; "" when nothing is."""
    state, health = str(container.get("state") or ""), str(container.get("health") or "")
    if state == "error":
        return f"Docker did not answer: {container.get('error') or 'no reason recorded'}"
    if state == "empty":
        return "Docker shows no retinue container"
    if state == "running":
        return "not responding (healthcheck: unhealthy)" if health == "unhealthy" else ""
    memory = ", out of memory" if container.get("oom") else ""
    if state == "restarting":
        return f"restarting{memory}"
    if state in ("exited", "dead"):
        code = container.get("exit_code", "?")
        if state == "exited" and code == 0 and not memory:
            return f"{MANUAL} (code 0)"
        return f"stopped, code {code}{memory}"
    return {"paused": "paused", "created": "created, not started"}.get(state, f"state {state or 'unknown'}")
