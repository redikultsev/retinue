"""The owner's tasks as the Router keeps them in step (docs/collections.md#tasks): one record of the platform's
collection `tasks` per thing to do, written by whoever took it in — the owner on his screen, the assistant from a
conversation, a session — and three things only the Router can do, because only it holds what they join.

- **«Make it a task»** on a row of «Waiting for you» — a person's ask or a reminder — is a press in the app
  (`waiting-press`, action `task`): the Router adds the task as the rule `code:waiting`, its title the row's own masked
  words, its `ask` the row's digest id (`letter:<hmac>`, `chat:<hmac>`, `reminder:<id>`). A row that has an open task
  already gets none more.
- **Reconciliation**: every look at the digest the Router reads the tasks that name a person's row and compares each
  pair with how it last left them (its kv, SYNCED_KEY). Whichever side moved since carries the other: a task closed —
  on the screen, by her on his word, by a session — closes its row as his «Done» would; a row closed — by his press,
  an answer code saw, a draft that went — closes its task; either opened again opens the other. Acting on what moved,
  never on what merely differs, lets «Undo» on either side stand. A reminder's task is its own: the reminder fires
  as set, and closing the task leaves it.
- **The busy window**: the calendar's occurrences of today and the next CALENDAR_DAYS, read live from the collector
  every CALENDAR_S and pushed to the platform (`POST /owner/calendar`) for the face's day and week — with their
  titles, which go to the screen alone.

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
from .courier import line
from .mail import Unavailable

log = logging.getLogger(__name__)

TASKS = "tasks"
RULE = "code:waiting"         # the Router's writer of tasks: a press, the reconciliation
SYNCED_KEY = "tasks.synced"   # the Router's kv: each task that names a row, as last left — [task, row], JSON
TITLE_CHARS = 200             # a task's title, as the form takes it
QUERY_PAGES = 5               # 200 rows a page: the tasks that name a row, at most this many pages read a look
CALL_S = 15                   # a call to the platform waits no longer: it runs beside the digest's push
CALENDAR_S = 600              # the busy window is read this often — the collector's own poll
CALENDAR_DAYS = 14            # today and this many days after it
CALENDAR_EVENTS = 400         # occurrences pushed at most
EVENT_TITLE_CHARS = 100
REMINDER = re.compile(r"reminder:(\d{1,12})")
PERSON = re.compile(r"(?:letter|chat):[0-9a-f]{16}")
OPEN, CLOSED = "open", "closed"


class Tasks:
    """The Router's side of the tasks, with its core: what a press, a look and the calendar's clock ask of it."""

    def __init__(self, core) -> None:
        self.core = core
        self._calendar_at = 0.0
        self._missing = False  # the collection or the rule's rights are not there: said once, then quiet

    # --- the platform ---------------------------------------------------------------------------------------------

    async def _call(self, name: str, arguments: dict, call: str) -> dict:
        body = {"actor": RULE, "via": "code", "foreign": [], "call": call, "arguments": arguments}
        return await self.core.platform(f"collections/{name}", body, CALL_S)

    async def _named(self) -> list[dict] | None:
        """The tasks that name a row (`ask` set), every state; None when they cannot be read — no collection yet, no
        rights for the rule, the platform away."""
        rows: list[dict] = []
        after = None
        for page in range(QUERY_PAGES):
            arguments = {"collection": TASKS, "filter": [{"field": "ask", "op": "not_empty"}],
                         "fields": ["title", "state", "ask"], **({"after": after} if after else {})}
            data = await self._call("query", arguments, f"named/{page}/{after or ''}")
            answer = data.get("answer") if data.get("ok") else None
            if not isinstance(answer, dict):
                if not self._missing:
                    log.warning("tasks not read: %s", line(str(data.get("text") or "no answer"), 300))
                    self._missing = True
                return None
            rows += [r for r in answer.get("rows") or [] if isinstance(r, dict)]
            after = answer.get("next")
            if not after:
                break
        self._missing = False
        return rows

    async def _apply(self, ops: list[dict], idem: str) -> bool:
        data = await self._call("apply", {"collection": TASKS, "ops": ops, "idem": idem}, idem)
        if not data.get("ok"):
            log.warning("tasks not written: %s", line(str(data.get("text") or "no answer"), 300))
        return bool(data.get("ok"))

    # --- «Make it a task» -----------------------------------------------------------------------------------------

    async def press(self, token: str, ref: str, now: float) -> bool:
        """«Make it a task» on a row in the app: a person's row by its token, a reminder by its id. True when the row
        is known — its task made now or there already —, False when it is not (an old list, a reminder gone)."""
        from .waiting import _rows, rows

        if self.core.platform is None:
            return False
        if match := REMINDER.fullmatch(ref):
            job = self.core.jobs.get(int(match[1]))
            if job is None or job.kind != "reminder" or job.status != "active":
                return False
            from .waiting import said
            due = clock.local(job.due, self.core.tz).date().isoformat()
            return await self._make(f"reminder:{job.id}", said(job.text), due, now)
        seqs = set(_rows(self.core, now).get(token) or ()) if token else set()
        if not seqs:
            return False
        found = next(((row_id, row) for row_id, row in rows(self.core, now).items() if seqs & set(row["seqs"])), None)
        if found is None:
            return False
        return await self._make(found[0], found[1]["title"], None, now)

    async def _make(self, ask: str, title: str, due: str | None, now: float) -> bool:
        named = await self._named()
        if named is None:
            return False
        if any(r["fields"].get("ask") == ask and r["fields"].get("state") == OPEN for r in named):
            return True
        fields = {"title": line(title, TITLE_CHARS) or "Task", "state": OPEN, "ask": ask,
                  **({"due": due} if due else {})}
        return await self._apply([{"op": "add", "fields": fields}], f"task:{ask}:{int(now // 60)}")

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
            task = OPEN if state == OPEN else CLOSED
            side = OPEN if row["state"] in ("open", "later") else CLOSED
            was_task, was_row = synced.get(record["id"], (None, None))
            if was_task is None and task != side:
                # The first look at the pair: what is closed wins — a task made from a row the owner then answered,
                # a row whose task he closed before the Router saw it.
                was_task, was_row = (OPEN, side) if task == CLOSED else (task, OPEN)
            if task != was_task and task != side:
                # The task moved, and the row is not where the task is now.
                if task == CLOSED:
                    self.core.mail.store.mark(row["seqs"], "done", now)
                    side, changed = CLOSED, changed + 1
                elif self.core.mail.store.mark(row["seqs"], "open", now):
                    side, changed = (OPEN if rows(self.core, now)[ask]["state"] in ("open", "later") else CLOSED,
                                     changed + 1)
            elif side != was_row and side != task:
                # The row moved and the task is not where the row is now.
                ops = [{"op": "set", "id": record["id"], "version": record["version"],
                        "fields": {"state": "done" if side == CLOSED else OPEN}}]
                if await self._apply(ops, f"sync:{record['id']}:{record['version']}"):
                    task, changed = side, changed + 1
            kept[record["id"]] = [task, side]
        if kept != synced:
            self.core.store.set(SYNCED_KEY, json.dumps(kept))
        if changed and getattr(self.core.mail, "changed", None):
            self.core.mail.changed()  # a row closed here leaves the app's queue now, not at the next look
        return changed

    def _synced(self) -> dict[str, tuple[str, str]]:
        try:
            known = json.loads(self.core.store.get(SYNCED_KEY) or "{}")
        except ValueError:
            return {}
        return {k: tuple(v) for k, v in known.items() if isinstance(v, list) and len(v) == 2} \
            if isinstance(known, dict) else {}

    # --- the busy window ------------------------------------------------------------------------------------------

    async def calendar(self, now: float) -> bool:
        """The calendar of today and the next CALENDAR_DAYS to the platform, every CALENDAR_S. True when pushed; an
        unread calendar is not pushed, and the platform shows the last one with its age."""
        if self.core.platform is None or not self.core.mail or self.core.mail.collector is None or \
                now - self._calendar_at < CALENDAR_S:
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
        answer = await self.core.platform("calendar", {"events": shown, "at": now, "failed": len(failed)}, CALL_S)
        if answer.get("error"):
            log.warning("the platform refused the busy window: %s", answer["error"])
            return False
        return True

    async def cycle(self, now: float) -> None:
        """What the digest's look does beside the push: the reconciliation, and the busy window when it is due."""
        try:
            await self.reconcile(now)
        except Unavailable as exc:  # the platform restarting: the next look, 30 s on, tries again
            log.warning("the tasks were not reconciled: %s", exc)
        except Exception:
            log.exception("the tasks were not reconciled")
        try:
            await self.calendar(now)
        except Unavailable as exc:
            self._calendar_at = 0.0  # not pushed: tried again at the next look, not in ten minutes
            log.warning("the busy window was not pushed: %s", exc)
        except Exception:
            log.exception("the busy window was not pushed")


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
    busy = not all_day and event.get("transparency") != "transparent" and event.get("answer") != "declined"
    return {"start": begins, "end": max(ends, begins), "title": line(str(event.get("summary") or ""), EVENT_TITLE_CHARS)
            or "Busy", "all_day": all_day, "busy": busy}


# --- the owner's reminders made into tasks, once, from the router's container -----------------------------------

def main(argv: list[str] | None = None) -> int:
    """`python -m retinue.tasks from-reminders <id>… [--title <id>=<words>]…`: each of these reminders gets a task
    that names it (`ask: reminder:<id>`, its day as `due`), as «Make it a task» would make it — the reminder stays and
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
