"""The owner's tasks as the Router keeps them in step (docs/collections.md#tasks): one record of the platform's
collection `tasks` per thing to do, written by whoever took it in — the owner on his screen, the assistant from a
conversation, a session — and three things only the Router can do, because only it holds what they join.

- **«Plan»** on a person's row or a reminder in the app's Tasks is a plan press (`place` with the row's `t` or `ref`):
  the Router adds its task as the rule `code:waiting` — its title the row's own masked words, a reminder's day its
  due, its `ask` the row's digest id (`letter:<hmac>`, `chat:<hmac>`, `reminder:<id>`) — and the plan
  lays it on the day. A row that has an open task already gets none more: that one is laid.
- **Reconciliation**: every look at the digest the Router reads the tasks that name a person's row and compares each
  pair with how it last left them (its kv, SYNCED_KEY). Whichever side moved since carries the other: a task done —
  on the screen, by her on his word, by a session — closes its row as his «Done» would, a task dropped as «Not a task»
  would; a row closed — by his press, an answer code saw, a draft that went — closes its task, done, or dropped when
  he said «Not a task»; a row that ran out leaves its task as it is; either opened again opens the other. Acting on what moved,
  never on what merely differs, lets «Undo» on either side stand. A reminder's task is its own: the reminder fires
  as set, and closing the task leaves it.
- **The busy window**: the calendar's occurrences of today and the next CALENDAR_DAYS, read live from the collector
  every CALENDAR_S and pushed to the platform (`POST /owner/calendar`) for the face's day and week — with their
  titles, which go to the screen alone.

A task's `project` (its path in the Base, `work/acme`) is set and moved by the owner, the assistant, a session or the
owner's computer — never by the Router: its rule's rights do not open the field.

The Router reads and writes the collection through the platform's `/owner/collections/*` as `code:waiting` via
`code`: the rule's rights are the owner's to give (`collections rights tasks …`), and without them nothing here
writes. The platform never calls the Router.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as wall, timedelta

from . import clock
from .coderules import WAITING, Records, mark
from .courier import line
from .mirror import Mirror

log = logging.getLogger(__name__)

TASKS = "tasks"
SYNCED_KEY = "tasks.synced"   # the Router's kv: each task that names a row, as last left — [task, row], JSON
TITLE_CHARS = 200             # a task's title, as the form takes it
CALENDAR_S = 600              # the busy window is read this often — the collector's own poll
CALENDAR_DAYS = 14            # today and this many days after it
CALENDAR_EVENTS = 400         # occurrences pushed at most
EVENT_TITLE_CHARS = 100
REMINDER = re.compile(r"reminder:(\d{1,12})")
PERSON = re.compile(r"(?:letter|chat):[0-9a-f]{16}")
OPEN, DONE, DROPPED = "open", "done", "dropped"


class Tasks:
    """The Router's side of the tasks, with its core: what a press, a look and the calendar's clock ask of it."""

    def __init__(self, core, mirror: Mirror | None = None) -> None:
        self.core = core
        self._tasks = Records(core.platform, WAITING, TASKS)  # the Router's writer of tasks: a press, the reconciliation
        self._calendar_at = 0.0
        self.events_at: float | None = None  # when the busy window was last read; none yet — a plan would land on meetings
        self.events: list[dict] = []  # the busy window as last read: what the plan lays around (`planner`)
        self._window: dict | None = None  # and as the platform takes it: `{events, at, failed}`
        self._push = mirror.snapshot("calendar", lambda now: self._window, "the busy window", every_s=0) \
            if mirror else None

    async def _named(self) -> list[dict] | None:
        """The tasks that name a row (`ask` set), every state; None when they cannot be read — no collection yet, no
        rights for the rule, the platform away."""
        return await self._tasks.query([{"field": "ask", "op": "not_empty"}], ["title", "state", "ask", "due", "by"])

    # --- «Plan» on a row ------------------------------------------------------------------------------------------

    async def make(self, token: str, ref: str, now: float) -> str | None:
        """«Plan» on a row in the app (`planner.Planner`'s `place`): its task — a person's row by its token, a reminder
        by its id —, made now or the open one there already; None when the row is not known (an old list, a reminder
        gone) or the task could not be written."""
        from .waiting import _rows, rows

        if self.core.platform is None:
            return None
        if match := REMINDER.fullmatch(ref):
            job = self.core.jobs.get(int(match[1]))
            if job is None or job.kind != "reminder" or job.status != "active":
                return None
            from .waiting import said
            due = clock.local(job.due, self.core.tz).date().isoformat()
            return await self._make(f"reminder:{job.id}", said(job.text), due, now)
        seqs = set(_rows(self.core, now).get(token) or ()) if token else set()
        if not seqs:
            return None
        found = next(((row_id, row) for row_id, row in rows(self.core, now).items() if seqs & set(row["seqs"])), None)
        if found is None:
            return None
        due = str(found[1].get("due") or "")  # her reading of the letter's deadline: a day, or a day and a time
        return await self._make(found[0], found[1]["title"], due[:10] or None, now, self._by(due))

    def _by(self, due: str) -> str | None:
        """A deadline with a time of day, his local, as the task's «no later than»."""
        if "T" not in due:
            return None
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            at = datetime.strptime(due, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(self.core.tz)).timestamp()
        except (ValueError, KeyError):
            return None
        return datetime.fromtimestamp(at, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")  # past too: «missed its deadline»

    async def _make(self, ask: str, title: str, due: str | None, now: float, by: str | None = None) -> str | None:
        named = await self._named()
        if named is None:
            return None
        if mine := next((r for r in named if r["fields"].get("ask") == ask and r["fields"].get("state") == OPEN), None):
            # Made before the letter was dated, or a later letter brought an earlier deadline: the task takes it.
            had = mine["fields"]
            change = {**({"due": due} if due and (not had.get("due") or due < had["due"]) else {}),
                      **({"by": by} if by and (not had.get("by") or by < had["by"]) else {})}
            if change:  # refused: logged, and the task is laid as it is
                await self._tasks.apply([{"op": "set", "id": mine["id"], "version": mine["version"], "fields": change}],
                                        f"task-due:{mine['id']}:{mark(change, 12)}")
            return mine["id"]
        fields = {"title": line(title, TITLE_CHARS) or "Task", "state": OPEN, "ask": ask,
                  **({"due": due} if due else {}), **({"by": by} if by else {})}
        answer = await self._tasks.apply([{"op": "add", "fields": fields}], f"task:{ask}:{int(now // 60)}")
        records = (answer or {}).get("records") or []
        return records[0].get("id") if records and isinstance(records[0], dict) else None

    # --- the reconciliation ---------------------------------------------------------------------------------------

    async def reconcile(self, now: float) -> int:
        """One look: each task that names a person's row against its row, whichever moved since the last look
        carrying the other. How many things were changed — rows marked, tasks set."""
        from .waiting import rows

        if self.core.platform is None or not self.core.mail:
            return 0
        named = await self._named()
        if named is None:
            return 0
        known = rows(self.core, now)
        synced = self._synced()
        kept, changed = {}, 0
        for record in named:
            ask, state = record["fields"].get("ask"), record["fields"].get("state")
            row = known.get(ask) if isinstance(ask, str) and PERSON.fullmatch(ask) else None
            if row is None:  # a reminder's, or a row older than the asks the store still reads: nothing to join
                continue
            task = state if state in (DONE, DROPPED) else OPEN
            was_task, was_row = synced.get(record["id"], (None, None))
            # A row that ran out moved nowhere: the owner did nothing to it, so it carries nothing to its task.
            side = _side(row) or was_row or task
            if was_task is None and task != side:
                # The first look at the pair: what is closed wins — a task made from a row the owner then answered,
                # a row whose task he closed before the Router saw it.
                was_task, was_row = (OPEN, side) if task != OPEN else (task, OPEN)
            if task != was_task and task != side:
                # The task moved, and the row is not where the task is now.
                if task != OPEN:
                    self.core.mail.store.mark(row["seqs"], "dismissed" if task == DROPPED else "done", now)
                    side, changed = task, changed + 1
                elif self.core.mail.store.mark(row["seqs"], "open", now):
                    side, changed = _side(rows(self.core, now)[ask]) or OPEN, changed + 1
            elif side != was_row and side != task:
                # The row moved and the task is not where the row is now.
                ops = [{"op": "set", "id": record["id"], "version": record["version"], "fields": {"state": side}}]
                if await self._tasks.apply(ops, f"sync:{record['id']}:{record['version']}") is not None:
                    task, changed = side, changed + 1
            kept[record["id"]] = [task, side]
        if kept != synced:
            self.core.store.set(SYNCED_KEY, json.dumps(kept))
        if changed and getattr(self.core.mail, "changed", None):
            self.core.mail.changed()  # a row closed here leaves the app's queue now, not at the next look
        return changed

    def _synced(self) -> dict[str, tuple[str, str]]:
        """How each pair was last left; `closed` of an older look is «Done»'s."""
        try:
            known = json.loads(self.core.store.get(SYNCED_KEY) or "{}")
        except ValueError:
            return {}
        return {k: tuple(DONE if x == "closed" else x for x in v) for k, v in known.items()
                if isinstance(v, list) and len(v) == 2} if isinstance(known, dict) else {}

    # --- the busy window ------------------------------------------------------------------------------------------

    async def calendar(self, now: float) -> bool:
        """The calendar of today and the next CALENDAR_DAYS, read every CALENDAR_S and pushed to the platform. True
        when pushed; an unread calendar is not pushed, and the platform shows the last one with its age. The platform
        away: read and pushed again at the next look, not in ten minutes."""
        if self.core.platform is None or not self.core.mail or self.core.mail.collector is None:
            self.events_at = now  # no calendar to read: none keeps him busy, and that is known
            return False
        if now - self._calendar_at < CALENDAR_S:
            return False
        self._calendar_at = now
        start = clock.local(now, self.core.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=CALENDAR_DAYS + 1)
        try:
            events, failed = await self.core.mail.collector.agenda(start.timestamp(), end.timestamp())
        except Exception as exc:  # noqa: BLE001 — the collector away: the window keeps its age on the platform
            log.warning("the calendar not read for the busy window: %s", exc)
            return False
        shown = [e for e in (occurrence(event, self.core.tz) for event in events) if e][:CALENDAR_EVENTS]
        self.events, self.events_at = shown, now
        self._window = {"events": shown, "at": now, "failed": len(failed)}
        if self._push is None:
            return False
        if not await self._push(now):
            self._calendar_at = 0.0
            return False
        return True


def _side(row: dict) -> str | None:
    """Where a person's row stands for its task: open (or waiting out «Tomorrow»), done, dropped («Not a task»); None
    when it ran out — no move of the owner's."""
    if row["state"] in ("open", "later"):
        return OPEN
    if row["state"] == "closed":
        return DROPPED if row.get("how") == "dismissed" else DONE
    return None


def occurrence(event: dict, tz: str) -> dict | None:
    """An occurrence as the face's day shows it: its moments, its title, whether it is the whole day, and whether it
    keeps the owner busy — not when it is free time (`transparent`), declined, cancelled or the whole day."""
    if event.get("status") == "cancelled":
        return None
    start, end = event.get("start") or {}, event.get("end") or {}
    all_day = bool(start.get("date")) and not start.get("dateTime")
    try:
        if all_day:
            zone = clock.zone(tz)
            begins = datetime.combine(datetime.fromisoformat(start["date"]).date(), wall(), zone).timestamp()
            ends = datetime.combine(datetime.fromisoformat(end.get("date") or start["date"]).date(), wall(),
                                    zone).timestamp()
        else:
            begins = datetime.fromisoformat(str(start["dateTime"]).replace("Z", "+00:00")).timestamp()
            ends = datetime.fromisoformat(str(end.get("dateTime") or start["dateTime"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    # His own, or an invitation he accepted or may go to: an invitation he never answered, or declined, keeps no one
    # busy — someone else's «busy» does not empty his day.
    busy = not all_day and event.get("transparency") != "transparent" and \
        event.get("answer") not in ("declined", "needsAction")
    return {"start": begins, "end": max(ends, begins), "title": line(str(event.get("summary") or ""), EVENT_TITLE_CHARS)
            or "Busy", "all_day": all_day, "busy": busy}


# --- the owner's reminders made into tasks, once, from the router's container -----------------------------------

def main(argv: list[str] | None = None) -> int:
    """`python -m retinue.tasks from-reminders <id>… [--title <id>=<words>]…`: each of these reminders gets a task
    that names it (`ask: reminder:<id>`, its day as `due`), as «Plan» on it would make it — the reminder stays and
    fires as set. Run inside the router's container once, when the tasks collection is first set up: the owner's
    reminders that were tasks all along. A reminder that has its task already gets none more."""
    import argparse
    import asyncio
    import time
    from types import SimpleNamespace

    from .bridge import PlatformClient
    from .config import RouterConfig
    from .proactive import ZONE_KEY
    from .protocol import Store
    from .scheduler import Scheduler

    parser = argparse.ArgumentParser(prog="python -m retinue.tasks", description="the owner's reminders as tasks")
    parser.add_argument("action", choices=["from-reminders"])
    parser.add_argument("ids", nargs="+", type=int, help="the reminders' ids, as list_reminders names them")
    parser.add_argument("--title", action="append", default=[], metavar="ID=WORDS",
                        help="a task's title instead of the reminder's own words")
    parser.add_argument("--config", default="/config/router.yaml")
    args = parser.parse_args(argv)
    titles = dict(t.split("=", 1) for t in args.title if "=" in t)
    cfg = RouterConfig.load(args.config)
    if not cfg.platform_url:
        raise SystemExit("no platform in the router's config: there are no tasks to make")
    store = Store(cfg.state_db)
    tz = store.get(ZONE_KEY) or cfg.owner_tz
    client = PlatformClient(cfg.platform_url, cfg.platform_token, "the platform")
    core = SimpleNamespace(platform=client.owner, jobs=Scheduler(store.db, tz), tz=tz, store=store, mail=None)
    tasks = Tasks(core)

    async def run() -> int:
        failed = 0
        for job_id in args.ids:
            job = core.jobs.get(job_id)
            if job is None or job.kind != "reminder" or job.status != "active":
                print(f"{job_id}: no active reminder")
                failed += 1
                continue
            title = titles.get(str(job_id)) or " ".join(job.text.split())
            due = clock.local(job.due, tz).date().isoformat()
            made = await tasks._make(f"reminder:{job_id}", title, due, time.time())
            print(f"{job_id}: {'a task, due ' + due if made else 'not made (see the log)'}")
            failed += not made
        return failed

    return 1 if asyncio.run(run()) else 0


if __name__ == "__main__":
    raise SystemExit(main())


