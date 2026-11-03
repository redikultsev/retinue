"""The owner's day plan, the Router's side (docs/collections.md#blocks): a press — «Plan my day» in the app, `/plan` in
Telegram, «Plan» on one task — lays the open tasks into the window's free time (`plan_layout`) as records of the
collection `blocks`, written as the rule `code:plan` in one apply, all or none. No plan is made without a press.

- **What keeps him busy** is the calendar window the Router last read (`tasks.Tasks.events`: his busy occurrences,
  each with a buffer after), his holds («Busy 1 h»), and the blocks standing in the window.
- **A plan is kept by its id**, not by its day: `{id, by, at, windows, blocks: {id: {version, before, day, end}}, left}`
  — so a second press, «Plan» on one task or a plan of several days never loses the blocks of the one before. A block's
  plan is its field `plan`; every write of this module keeps the version that plan knows.
- **«Undo»** takes back one plan: each of its blocks still at the version this module wrote goes back to what it was
  before (`before`), or is `skipped` when the plan added it; one the owner moved, resized or closed since is his, and is
  named instead.
- **«Busy»** is a hold from the moment of the press; the plan's blocks it crosses are laid again after it.
- **Each look** (the digest's, every 30 s): a block whose time is over is `passed` — its minutes count toward its task;
  the future blocks of a task that is no longer open are `skipped`, so «Done» frees their time. Whenever the calendar,
  the holds, the blocks or the tasks changed since the last look — the owner's own moves included — the plan's blocks
  are laid again where something crosses them, in every window a plan holds: moved (`start`), not taken off and added
  again; a task's plan pieces beyond what it needs are taken off. The look counts as seen only once that went through.
- **No plan on a stale calendar**: a busy window older than CALENDAR_FRESH_S, or none read since the Router started,
  refuses the press — the plan would land on meetings.
- **The state** — the plans with what was left out, the holds — lives in the Router's kv and goes to the platform after
  each change (`POST /owner/plan`), shown by window day: each day's latest plan not taken back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import date, datetime, timedelta, timezone

from . import plan_layout as L
from .courier import line

log = logging.getLogger(__name__)

TASKS, BLOCKS = "tasks", "blocks"
RULE = "code:plan"
PLANS_KEY = "plan.plans"    # the Router's kv: by plan id — {id, by, at, windows, blocks: {id: {version, before, day, end}},
                            # left: {day: [{task, title, why}]}, pending?, failed?, undone?}
HOLDS_KEY = "plan.holds"    # the owner's holds: [{from, to, label}]
PLANS_KEPT_S = 14 * 86400   # a plan with no block ahead is kept this long after it was made
PLANS_MAX = 120             # and never more plans than this, the oldest going first
RECENT = 20                 # the plans of the last day the grid is told about, at most
CALENDAR_FRESH_S = 30 * 60  # a busy window older than this lays no plan
HOLD_MAX_S = 24 * 3600      # a hold from a button is at most this long
CALL_S = 15
PAGE = 200
BUSY_MINUTES = (30, 60, 120)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(round(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def moment(value: object) -> float | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _hm(ts: float, tz: str) -> str:
    from . import clock
    return clock.local(ts, tz).strftime("%H:%M")


class Planner:
    def __init__(self, core, tasks, hours: L.Hours = L.HOURS) -> None:
        self.core, self.tasks, self.hours = core, tasks, hours
        self.lock = asyncio.Lock()  # one writer of plans: a press, a look, an «Undo» never interleave
        self._seen = ""             # the busy window's mark at the last look: a change lays the plan again
        self.said: dict = {}        # what the last press came to, for the app: {at, text}
        self._migrate()

    # --- the platform ---------------------------------------------------------------------------------------------

    async def _call(self, name: str, arguments: dict, call: str) -> dict:
        body = {"actor": RULE, "via": "code", "foreign": [], "call": call, "arguments": arguments}
        return await self.core.platform(f"collections/{name}", body, CALL_S)

    async def _rows(self, collection: str, filter: list[dict], fields: list[str] | None = None) -> list[dict] | None:
        rows, after = [], None
        for page in range(5):
            arguments = {"collection": collection, "filter": filter, **({"fields": fields} if fields else {}),
                         **({"after": after} if after else {})}
            data = await self._call("query", arguments, f"q/{collection}/{page}/{secrets.token_hex(4)}")
            answer = data.get("answer") if data.get("ok") else None
            if not isinstance(answer, dict):
                log.warning("%s not read: %s", collection, line(str(data.get("text") or "no answer"), 300))
                return None
            rows += [r for r in answer.get("rows") or [] if isinstance(r, dict)]
            if not (after := answer.get("next")):
                return rows
        return rows

    async def _apply(self, ops: list[dict], idem: str) -> dict | None:
        if not ops:
            return {"records": [], "change_ids": []}
        data = await self._call("apply", {"collection": BLOCKS, "ops": ops, "idem": idem}, idem)
        if not data.get("ok"):
            log.warning("blocks not written: %s", line(str(data.get("text") or "no answer"), 300))
            return None
        return data.get("answer") or {"records": [], "change_ids": []}

    # --- the state ------------------------------------------------------------------------------------------------

    def _load(self, key: str, empty):
        try:
            value = json.loads(self.core.store.get(key) or "null")
        except ValueError:
            value = None
        return value if isinstance(value, type(empty)) else empty

    def plans(self) -> dict:
        return self._load(PLANS_KEY, {})

    def holds(self, now: float) -> list[dict]:
        return [h for h in self._load(HOLDS_KEY, []) if isinstance(h, dict) and float(h.get("to") or 0) > now]

    def _keep_plans(self, plans: dict, now: float | None = None) -> None:
        """The plans worth keeping: those with a block ahead, else made in the last PLANS_KEPT_S; the newest PLANS_MAX."""
        now = now if now is not None else _now()
        kept = [p for p in plans.values() if isinstance(p, dict) and p.get("id") and (
            any(float(b.get("end") or 0) > now for b in (p.get("blocks") or {}).values())
            or float(p.get("at") or 0) > now - PLANS_KEPT_S)]
        kept = sorted(kept, key=lambda p: float(p.get("at") or 0))[-PLANS_MAX:]
        self.core.store.set(PLANS_KEY, json.dumps({p["id"]: p for p in kept}))

    def _migrate(self) -> None:
        """Once, at the first start of this version: the plans kept by window day become plans kept by id."""
        plans = self._load(PLANS_KEY, {})
        if not plans or not any(_day(k) for k in plans):
            return
        out = {}
        for day, p in plans.items():
            if not (_day(day) and isinstance(p, dict) and p.get("id")):
                continue
            out[p["id"]] = {"id": p["id"], "by": "owner", "at": float(p.get("at") or 0), "windows": [day],
                            "blocks": {b: {"version": v, "before": None, "day": day, "end": 0}
                                       for b, v in (p.get("blocks") or {}).items() if isinstance(v, int)},
                            "left": {day: p.get("left") or []},
                            **({k: p[k] for k in ("failed", "undone") if p.get(k)})}
        self.core.store.set(PLANS_KEY, json.dumps(out))
        log.info("plans kept by id now: %d moved over", len(out))

    def latest(self, day: str, plans: dict | None = None) -> dict | None:
        """The day's latest plan not taken back."""
        live = [p for p in (plans if plans is not None else self.plans()).values()
                if isinstance(p, dict) and day in (p.get("windows") or []) and not p.get("undone")]
        return max(live, key=lambda p: float(p.get("at") or 0), default=None)

    def _by_day(self, plans: dict) -> dict:
        """What the app's Day shows of the plans: by window day, that day's latest plan not taken back."""
        days = sorted({d for p in plans.values() if isinstance(p, dict) for d in p.get("windows") or []})[-31:]
        out = {}
        for day in days:
            p = self.latest(day, plans)
            if p is None:
                continue
            out[day] = {"id": p["id"], "at": p.get("at", 0), "left": (p.get("left") or {}).get(day, []),
                        "blocks": {b: r.get("version") for b, r in (p.get("blocks") or {}).items() if r.get("day") == day},
                        "failed": bool(p.get("failed")), "undone": False}
        return out

    @staticmethod
    def _recent(plans: dict, now: float) -> list[dict]:
        """The plans of the last day, newest first, for the grid: who asked for each, and whether it was taken back —
        its blocks carry its id in their field `plan`."""
        recent = [p for p in plans.values() if isinstance(p, dict) and float(p.get("at") or 0) > now - 86400]
        return [{"id": p["id"], "by": p.get("by") or "owner", "at": float(p.get("at") or 0),
                 "undone": bool(p.get("undone")), "failed": bool(p.get("failed"))}
                for p in sorted(recent, key=lambda p: -float(p.get("at") or 0))[:RECENT]]

    @staticmethod
    def _keep(plans: dict, record: dict, before: dict | None = None) -> None:
        """A block this module just wrote, into the plan it belongs to (its field `plan`): its version and span — so
        «Undo» knows it as written; `before` only when this write is the plan's own move of someone else's block."""
        f = record.get("fields") or {}
        entry = plans.get(str(f.get("plan") or ""))
        start = moment(f.get("start"))
        if not isinstance(entry, dict) or start is None:
            return
        known = (entry.setdefault("blocks", {})).get(record["id"]) or {}
        entry["blocks"][record["id"]] = {"version": record.get("version"),
                                         "before": known.get("before") if known else before,
                                         "day": known.get("day") or "",
                                         "end": start + int(f.get("minutes") or 0) * 60}

    async def push(self, now: float) -> None:
        """The plans, the holds and what the last press came to, to the platform, whole, for the app's Day."""
        if self.core.platform is None:
            return
        plans = self.plans()
        body = {"at": now, "plans": self._by_day(plans), "recent": self._recent(plans, now), "holds": self.holds(now),
                "said": self.said,
                "window": {"zone": self.hours.zone, "opens": self.hours.opens.strftime("%H:%M"),
                           "closes": self.hours.closes.strftime("%H:%M")}}
        try:
            answer = await self.core.platform("plan", body, CALL_S)
            if answer.get("error"):
                log.warning("the platform refused the plan: %s", answer["error"])
        except Exception as exc:  # noqa: BLE001 — the platform away: the next change pushes again
            log.warning("the plan not pushed: %s", exc)

    # --- what the window holds ------------------------------------------------------------------------------------

    async def _world(self, window: L.Window, now: float):
        """The open tasks, the window's blocks, the minutes each task's passed pieces counted and the minutes its
        pieces planned in other windows hold. None: unread."""
        tasks = await self._rows(TASKS, [{"field": "state", "op": "eq", "value": "open"}],
                                 ["title", "minutes", "due"])
        blocks = await self._rows(BLOCKS, [{"field": "state", "op": "in", "value": ["planned", "passed"]},
                                           {"field": "start", "op": "between",
                                            "value": [iso(window.start - 86400), iso(window.end)]}])
        if tasks is None or blocks is None:
            return None
        open_ids = {t["id"] for t in tasks}
        standing = [b for b in blocks if b["fields"].get("state") == "planned" and b["fields"].get("task") in open_ids
                    and (s := moment(b["fields"].get("start"))) is not None
                    and s + int(b["fields"].get("minutes") or 0) * 60 > window.start and s < window.end]
        done: dict[str, int] = {}
        elsewhere: dict[str, int] = {}
        locked: dict[str, int] = {}
        loose: set[str] = set()
        here = {b["id"] for b in standing}
        ids = [t["id"] for t in tasks][:PAGE]
        if ids:
            pieces = await self._rows(BLOCKS, [{"field": "state", "op": "in", "value": ["passed", "planned"]},
                                               {"field": "task", "op": "in", "value": ids}],
                                      ["task", "minutes", "state", "start", "locked"])
            for b in pieces or []:
                f = b["fields"]
                into = done if f.get("state") == "passed" else elsewhere if b["id"] not in here else None
                if into is not None:
                    into[f["task"]] = into.get(f["task"], 0) + int(f.get("minutes") or 0)
                if f.get("state") == "planned" and self._span(b)[1] > now:
                    if f.get("locked"):
                        locked[f["task"]] = locked.get(f["task"], 0) + int(f.get("minutes") or 0)
                    else:
                        loose.add(f["task"])
        owned = {t for t in locked if t not in loose}
        return tasks, standing, (done, elsewhere, locked, owned)

    def _stale(self, now: float) -> bool:
        """No busy window read since the Router started, or one older than CALENDAR_FRESH_S."""
        at = getattr(self.tasks, "events_at", None)
        return at is None or now - float(at) > CALENDAR_FRESH_S

    def _busy(self, window: L.Window, now: float) -> list[tuple[float, float]]:
        events = getattr(self.tasks, "events", None) or []
        busy = L.meeting_busy(events, window.start, window.end)
        busy += [(float(h["from"]), float(h["to"])) for h in self.holds(now)]
        return busy

    @staticmethod
    def _span(block: dict) -> tuple[float, float]:
        start = moment(block["fields"].get("start")) or 0.0
        return start, start + int(block["fields"].get("minutes") or 0) * 60

    @staticmethod
    def _task(row: dict, counted: tuple) -> L.Task:
        f = row["fields"]
        done, elsewhere, locked, owned = counted
        return L.Task(row["id"], f.get("minutes") if isinstance(f.get("minutes"), int) else None,
                      done.get(row["id"], 0), _day(f.get("due")) if f.get("due") else None, float(row.get("created") or 0),
                      elsewhere.get(row["id"], 0), locked.get(row["id"], 0), row["id"] in owned)

    @staticmethod
    def _why(task: L.Task, window: L.Window) -> str:
        if task.due is None:
            return ""
        if task.due < window.day:
            return "overdue"
        return "due today" if task.due == window.day else f"due {task.due:%a %d %b}"

    # --- presses --------------------------------------------------------------------------------------------------

    async def plan(self, now: float, day: date | None = None, only: str | None = None, days: int = 1) -> str:
        """«Plan my day» (or one task's «Plan» to a day), «Plan my week» (`days` = 7 windows in a row from the day's):
        each window's free time filled in code's order, one window's write after another so the next sees what the
        one before laid, all under one plan id — one «Undo» for the lot. Nothing standing is taken off. What it says
        back: what went where, what was left out."""
        async with self.lock:
            if self._stale(now):
                return "Not planned: the calendar has not been read in the last half hour. Try again in a minute."
            first = L.window_of(day, self.hours) if day else L.window_at(now, self.hours)
            plan_id = f"p{secrets.token_hex(5)}"
            plans = self.plans()
            # The intent first: a plan cut short by a failure is known, and a second press is a plan of its own.
            plans[plan_id] = {"id": plan_id, "by": "owner", "at": now, "windows": [], "blocks": {}, "pending": True,
                              "left": {}}
            self._keep_plans(plans, now)
            said: list[str] = []
            failed = False
            for n in range(max(1, min(days, 7))):
                window = first if n == 0 else L.window_of(first.day + timedelta(days=n), self.hours)
                world = await self._world(window, now)
                if world is None:
                    failed = True
                    break
                tasks, standing, counted = world
                have = {b["fields"]["task"] for b in standing}
                candidates = [self._task(t, counted) for t in tasks
                              if t["id"] not in have and (only is None or t["id"] == only)]
                if only is not None and not candidates:
                    self._drop_plan(plan_id, now)
                    return "It has a piece in that day already." if only in have else "No such open task."
                layout = L.lay(window, now, self._busy(window, now), [self._span(b) for b in standing], candidates)
                by_id = {t.id: t for t in candidates}
                titles = {t["id"]: str(t["fields"].get("title") or "") for t in tasks}
                key = window.day.isoformat()
                ops = [{"op": "add", "fields": {"task": task, "start": iso(start), "minutes": minutes,
                                                "state": "planned", "plan": plan_id,
                                                **({"why": w} if (w := self._why(by_id[task], window)) else {})}}
                       for task, start, minutes in layout.placed]
                answer = await self._apply(ops, f"plan:{plan_id}:{key}") if ops else {"records": []}
                plans = self.plans()
                entry = plans.get(plan_id) or {}
                if answer is None:
                    failed = True
                    break
                entry.setdefault("windows", []).append(key)
                entry.setdefault("left", {})[key] = [{"task": t, "title": line(titles.get(t, ""), 120), "why": why}
                                                     for t, why in layout.left]
                for record in answer.get("records") or []:
                    if isinstance(record, dict):
                        self._keep(plans, record)
                        entry["blocks"][record["id"]]["day"] = key
                self._keep_plans(plans, now)
                if days == 1:
                    said.append(self._said(window, layout, titles))
                elif layout.placed:
                    total = sum(m for _, _, m in layout.placed)
                    said.append(f"{window.day:%a} {len(layout.placed)} ({total // 60} h {total % 60:02d})")
            plans = self.plans()
            entry = plans.get(plan_id) or {}
            entry["pending"] = False
            if failed:
                entry["failed"] = True
            self._keep_plans(plans, now)
        await self.push(now)
        if failed and not entry.get("blocks"):
            return "Not planned: the blocks could not be written. Try again."
        if days == 1:
            return said[0] if said else "Not planned: try again."
        head = f"Week planned from {first.day:%a %d %b}: " + (", ".join(said) if said else "no open task fits its free time")
        return head + "." + (" Cut short — try again for the rest." if failed else "")

    def _drop_plan(self, plan_id: str, now: float) -> None:
        plans = self.plans()
        plans.pop(plan_id, None)
        self._keep_plans(plans, now)

    def _said(self, window: L.Window, layout: L.Layout, titles: dict[str, str]) -> str:
        tz = self.core.tz
        if not layout.placed:
            head = f"Nothing planned for {window.day:%a %d %b}: no open task fits its free time."
        else:
            total = sum(m for _, _, m in layout.placed)
            parts = ", ".join(f"{_hm(s, tz)} {line(titles.get(t, ''), 40)}" for t, s, _ in sorted(layout.placed, key=lambda p: p[1]))
            head = (f"Plan for {window.day:%a %d %b}: {len(layout.placed)} "
                    f"{'task' if len(layout.placed) == 1 else 'tasks'}, {total // 60} h {total % 60:02d} — {parts}.")
        if layout.left:
            head += " Left out: " + ", ".join(line(titles.get(t, ""), 40) for t, _ in layout.left[:5]) + "."
        return head

    async def undo(self, plan_id: str, now: float) -> str:
        """«Undo» of one plan: its blocks still as this module last wrote them go back to what they were before it —
        taken off when it added them; the rest are the owner's and are named."""
        async with self.lock:
            plans = self.plans()
            entry = plans.get(plan_id)
            if not isinstance(entry, dict) or entry.get("undone"):
                return "That plan is not known any more: nothing taken back."
            written = entry.get("blocks") or {}
            rows = await self._rows(BLOCKS, [{"field": "state", "op": "eq", "value": "planned"}])
            if rows is None:
                return "Not taken back: the blocks could not be read. Try again."
            ops, kept = [], 0
            for b in rows:
                known = written.get(b["id"])
                if known is None:
                    continue
                if b["version"] != known.get("version"):
                    kept += 1
                    continue
                before = known.get("before")
                fields = {"state": "skipped"} if not before else \
                    {k: v for k, v in before.items() if k in ("start", "minutes", "state")}
                ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": fields})
            answer = await self._apply(ops, f"undo:{plan_id}:{len(ops)}")
            if answer is None:
                return "Not taken back: the blocks could not be written. Try again."
            plans = self.plans()
            for record in answer.get("records") or []:
                if isinstance(record, dict) and (record.get("fields") or {}).get("plan") != plan_id:
                    self._keep(plans, record)  # someone else's block put back: its own plan knows the version
            plans[plan_id] = {**plans.get(plan_id, entry), "undone": now, "blocks": {}}
            self._keep_plans(plans, now)
        await self.push(now)
        said = f"Taken back: {len(ops)} {'block' if len(ops) == 1 else 'blocks'}."
        return said + (f" {kept} you changed since stay." if kept else "")

    async def busy(self, minutes: int, now: float, label: str = "Busy") -> str:
        """«Busy» from now: a hold, and the plan's blocks it crosses laid again after it."""
        minutes = max(5, min(int(minutes), HOLD_MAX_S // 60))
        holds = self.holds(now) + [{"from": now, "to": now + minutes * 60, "label": line(label, 40)}]
        self.core.store.set(HOLDS_KEY, json.dumps(holds))
        moved = await self.relay(now)
        return f"Busy till {_hm(now + minutes * 60, self.core.tz)}." + (f" {moved}" if moved else "")

    async def free(self, now: float) -> str:
        """The holds taken off: the time is free again — and stays free: nothing is pulled into it."""
        self.core.store.set(HOLDS_KEY, "[]")
        await self.push(now)
        return "Busy taken off."

    async def remove(self, block_id: str, now: float) -> str:
        """× on a block: it is skipped; nothing else moves."""
        async with self.lock:
            rows = await self._rows(BLOCKS, [{"field": "state", "op": "eq", "value": "planned"}])
            block = next((b for b in rows or [] if b["id"] == block_id), None)
            if block is None:
                return "That block is not planned any more."
            ok = await self._apply([{"op": "set", "id": block_id, "version": block["version"],
                                     "fields": {"state": "skipped"}}], f"remove:{block_id}:{block['version']}")
        await self.push(now)
        return "Taken off the day." if ok is not None else "Not taken off: try again."

    # --- each look ------------------------------------------------------------------------------------------------

    async def cycle(self, now: float) -> None:
        """Pieces whose time is over passed, closed tasks' future pieces skipped; then, when anything changed since the
        last look — the calendar, a hold, a block, a task — the plans laid again where something crosses them. The
        look is seen only once that went through: a refused write is tried again at the next one."""
        if self.core.platform is None:
            return
        try:
            snapshot = await self._settle(now)
            if snapshot is None:
                return
            mark = hashlib.sha256(json.dumps([getattr(self.tasks, "events", None) or [], self.holds(now), snapshot],
                                             sort_keys=True).encode()).hexdigest()
            if mark != self._seen and await self._relay(now) is not None:
                self._seen = mark
        except Exception:
            log.exception("the plan's look failed")

    async def _settle(self, now: float) -> list | None:
        """The pieces whose time is over passed, the future pieces of closed tasks skipped. What the look saw of the
        blocks and the tasks after it, for the mark; None when they could not be read."""
        async with self.lock:
            rows = await self._rows(BLOCKS, [{"field": "state", "op": "eq", "value": "planned"}])
            tasks = await self._rows(TASKS, [{"field": "state", "op": "eq", "value": "open"}],
                                     ["title", "minutes", "due"])
            if rows is None or tasks is None:
                return None
            open_ids = {t["id"] for t in tasks}
            ops = []
            for b in rows:
                start, end = self._span(b)
                if end <= now:
                    ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "passed"}})
                elif b["fields"].get("task") not in open_ids and start > now:
                    ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}})
            if ops and await self._apply(ops, f"settle:{_hash(ops)}") is not None:
                changed = {o["id"] for o in ops}
                rows = [b for b in rows if b["id"] not in changed]
            return [sorted((b["id"], b["version"]) for b in rows), sorted((t["id"], t["version"]) for t in tasks)]

    async def relay(self, now: float) -> str:
        """The plans laid again where something now crosses them (`_relay`): what moved, said."""
        return await self._relay(now) or ""

    async def _relay(self, now: float) -> str | None:
        """Every window from today's on that a plan holds a block in, laid again where something now crosses the
        plans' blocks: only those still as this module wrote them, not started and not the owner's — crossed by the
        calendar, a hold, a block of the owner's or another of the plan's — moved in code's order, and a task's plan
        pieces beyond what it needs taken off. What could not be laid again is taken off and said. None: not done."""
        async with self.lock:
            plans = self.plans()
            mine = {b: (p["id"], r) for p in plans.values() if isinstance(p, dict) and not p.get("undone")
                    for b, r in (p.get("blocks") or {}).items() if float(r.get("end") or 0) > now}
            if not mine:
                return ""
            today = L.window_at(now, self.hours)
            days = sorted({r.get("day") for _, r in mine.values() if r.get("day")} | {today.day.isoformat()})
            ops, placed_left, urgent, titles = [], [], [], {}
            for day in days:
                d = _day(day)
                if d is None or d < today.day:
                    continue
                window = L.window_of(d, self.hours)
                world = await self._world(window, now)
                if world is None:
                    return None
                tasks, standing, done = world
                titles.update({t["id"]: str(t["fields"].get("title") or "") for t in tasks})
                busy = self._busy(window, now)
                ours = sorted((b for b in standing if b["id"] in mine and mine[b["id"]][1].get("version") == b["version"]
                               and not b["fields"].get("locked") and self._span(b)[0] > now), key=self._span)
                others = [b for b in standing if b not in ours]
                crossed, kept = [], []
                for b in ours:
                    a, z = self._span(b)
                    if any(x < z and a < y for x, y in busy + [self._span(o) for o in others] + kept):
                        crossed.append(b)
                    else:
                        kept.append((a, z))
                # A task's plan pieces beyond what it needs (the owner laid its time himself): the last ones go.
                by_id = {t["id"]: t for t in tasks}
                extra = []
                for b in reversed(ours):
                    t = by_id.get(b["fields"]["task"])
                    if t is None or b in crossed:
                        continue
                    task = self._task(t, done)
                    if L.need_s(task) - task.planned * 60 - sum(int(o["fields"].get("minutes") or 0) * 60
                                                                 for o in standing if o["fields"]["task"] == t["id"]
                                                                 and o is not b and o not in extra) > 0:
                        continue  # still needed
                    extra.append(b)
                if not crossed and not extra:
                    continue
                keep = [self._span(b) for b in standing if b not in crossed and b not in extra]
                again = [self._task(by_id[b["fields"]["task"]], done) for b in crossed if b["fields"]["task"] in by_id]
                layout = L.lay(window, now, busy, keep, again)
                spots = {t: (s, m) for t, s, m in layout.placed}
                for b in crossed:
                    spot = spots.pop(b["fields"]["task"], None)
                    if spot is None:
                        ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}})
                    else:
                        ops.append({"op": "set", "id": b["id"], "version": b["version"],
                                    "fields": {"start": iso(spot[0]), "minutes": spot[1]}})
                ops += [{"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}}
                        for b in extra]
                placed_left += [(day, mine[b["id"]][0], t, why) for t, why in layout.left
                                for b in crossed if b["fields"]["task"] == t]
                urgent += [t for t, _ in layout.left if (x := by_id.get(t)) and (due := _day(x["fields"].get("due")))
                           and due <= window.day and window.day == today.day]
            if not ops:
                return ""
            answer = await self._apply(ops, f"relay:{_hash(ops)}")
            if answer is None:
                return None
            plans = self.plans()
            for record in answer.get("records") or []:
                if isinstance(record, dict):
                    self._keep(plans, record)
            for day, plan_id, t, why in placed_left:
                left = (plans.get(plan_id) or {}).setdefault("left", {}).setdefault(day, [])
                if not any(x.get("task") == t for x in left):
                    left.append({"task": t, "title": line(titles.get(t, ""), 120), "why": why})
            self._keep_plans(plans, now)
        await self.push(now)
        if urgent:
            await self.core.tell_owner("Did not fit today any more: "
                                       + ", ".join(line(titles.get(t, ""), 40) for t in urgent) + ".")
        moved = sum(1 for o in ops if "start" in o["fields"])
        return f"Moved {moved} {'block' if moved == 1 else 'blocks'}." if moved else ""

    # --- the app's presses ----------------------------------------------------------------------------------------

    async def told(self, now: float, day: date | None = None) -> None:
        """`/plan` in Telegram, or «Plan my day» from the morning: the plan, and its line with «Undo» and «Busy 1 h»
        — each a button of its own, so one press leaves the other alive."""
        from .core import Button

        text = await self.plan(now, day)
        window = L.window_of(day, self.hours) if day else L.window_at(now, self.hours)
        plan_id = (self.latest(window.day.isoformat()) or {}).get("id")
        buttons = [Button("Undo", "plan-undo", plan_id, alone=True)] if plan_id else []
        buttons.append(Button("Busy 1 h", "plan-busy", "60", alone=True))
        await self.core.tell_owner(text, buttons=buttons)

    async def undo_pressed(self, plan_id: str) -> str:
        return await self._noted(await self.undo(plan_id, _now()))

    async def busy_pressed(self, minutes: str) -> str:
        return await self._noted(await self.busy(int(minutes or 60), _now()))

    async def _noted(self, text: str) -> str:
        self.said = {"at": _now(), "text": text}
        await self.push(_now())
        return text

    async def press(self, data: dict, now: float) -> str:
        """A press in the app (`plan-press`): plan, plan one task to a day, undo, busy, free, remove. Its moment
        is the press's own; one older than PRESS_MAX_S is dropped and said."""
        at = float(data.get("at") or now)
        if now - at > PRESS_MAX_S:
            text = "A press from the app came too late — the Router was away. Press again."
            self.said = {"at": now, "text": text}
            await self.push(now)
            return text
        text = await self._pressed(data, now, at)
        day = (str(data.get("day") or "") or L.window_at(now, self.hours).day.isoformat()) \
            if data.get("action") in ("plan", "place") else ""
        self.said = {"at": now, "text": text, **({"day": day} if day else {})}
        await self.push(now)
        return text

    async def _pressed(self, data: dict, now: float, at: float) -> str:
        action = data.get("action")
        if action == "plan":
            return await self.plan(now, _day(data.get("day")) if data.get("day") else None,
                                   days=7 if data.get("days") == 7 else 1)
        if action == "place":
            return await self.plan(now, _day(data.get("day")), only=str(data.get("task") or ""))
        if action == "undo":
            return await self.undo(str(data.get("plan") or ""), now)
        if action == "busy":
            return await self.busy(int(data.get("minutes") or 60), at)
        if action == "free":
            return await self.free(now)
        if action == "remove":
            return await self.remove(str(data.get("block") or ""), now)
        return "No such press."


PRESS_MAX_S = 600


def _now() -> float:
    import time
    return time.time()


def _hash(ops: list[dict]) -> str:
    """An idem of a write by what it writes: two different writes in one look never share one."""
    return hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest()[:16]
