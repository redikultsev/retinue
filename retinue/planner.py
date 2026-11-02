"""The owner's day plan, the Router's side (docs/collections.md#blocks): a press — «Plan my day» in the app, `/plan` in
Telegram, «Plan» on one task — lays the open tasks into the window's free time (`plan_layout`) as records of the
collection `blocks`, written as the rule `code:plan` in one apply, all or none. No plan is made without a press.

- **What keeps him busy** is the calendar window the Router last read (`tasks.Tasks.events`: his busy occurrences,
  each with a buffer after), his holds («Busy 1 h»), and the blocks standing in the window.
- **«Undo»** takes back one plan: each of its blocks still `planned` at the version this module wrote is `skipped`;
  one the owner moved, resized or closed since is his, and is named instead.
- **«Busy»** is a hold from the moment of the press; the plan's blocks it crosses are laid again after it.
- **Each look** (the digest's, every 30 s): a block whose time is over is `passed` — its minutes count toward its task;
  the future blocks of a task that is no longer open are `skipped`, so «Done» frees their time; a calendar that moved,
  a hold, a block of the owner's laid over one of the plan's lays again only what they cross.
- **The state** — the plans by window with what was left out, the holds — lives in the Router's kv and goes to the
  platform whole after each change (`POST /owner/plan`), for the app's Day.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import date, datetime, timezone

from . import plan_layout as L
from .courier import line

log = logging.getLogger(__name__)

TASKS, BLOCKS = "tasks", "blocks"
RULE = "code:plan"
PLANS_KEY = "plan.plans"    # the Router's kv: by window day, its last plan — {id, at, blocks: {id: version}, left}
HOLDS_KEY = "plan.holds"    # the owner's holds: [{from, to, label}]
PLANS_KEPT = 14             # windows kept
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

    def _keep_plans(self, plans: dict) -> None:
        kept = dict(sorted(plans.items())[-PLANS_KEPT:])
        self.core.store.set(PLANS_KEY, json.dumps(kept))

    async def push(self, now: float) -> None:
        """The plans, the holds and what the last press came to, to the platform, whole, for the app's Day."""
        if self.core.platform is None:
            return
        body = {"at": now, "plans": self.plans(), "holds": self.holds(now), "said": self.said,
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
        """The open tasks, the window's blocks, the minutes each candidate's passed pieces counted. None: unread."""
        tasks = await self._rows(TASKS, [{"field": "state", "op": "eq", "value": "open"}],
                                 ["title", "minutes", "due"])
        blocks = await self._rows(BLOCKS, [{"field": "start", "op": "between",
                                            "value": [iso(window.start - 86400), iso(window.end)]}])
        if tasks is None or blocks is None:
            return None
        open_ids = {t["id"] for t in tasks}
        standing = [b for b in blocks if b["fields"].get("state") == "planned" and b["fields"].get("task") in open_ids
                    and (s := moment(b["fields"].get("start"))) is not None
                    and s + int(b["fields"].get("minutes") or 0) * 60 > window.start and s < window.end]
        done: dict[str, int] = {}
        ids = [t["id"] for t in tasks][:PAGE]
        if ids:
            passed = await self._rows(BLOCKS, [{"field": "state", "op": "eq", "value": "passed"},
                                               {"field": "task", "op": "in", "value": ids}], ["task", "minutes"])
            for b in passed or []:
                done[b["fields"]["task"]] = done.get(b["fields"]["task"], 0) + int(b["fields"].get("minutes") or 0)
        return tasks, standing, done

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
    def _task(row: dict, done: dict[str, int]) -> L.Task:
        f = row["fields"]
        return L.Task(row["id"], f.get("minutes") if isinstance(f.get("minutes"), int) else None,
                      done.get(row["id"], 0), _day(f.get("due")) if f.get("due") else None, float(row.get("created") or 0))

    @staticmethod
    def _why(task: L.Task, window: L.Window) -> str:
        if task.due is None:
            return ""
        if task.due < window.day:
            return "overdue"
        return "due today" if task.due == window.day else f"due {task.due:%a %d %b}"

    # --- presses --------------------------------------------------------------------------------------------------

    async def plan(self, now: float, day: date | None = None, only: str | None = None) -> str:
        """«Plan my day» (or one task's «Plan» to a day): the window's free time filled, in code's order, written in
        one apply. What it says back: what went where, what was left out."""
        async with self.lock:
            window = L.window_of(day, self.hours) if day else L.window_at(now, self.hours)
            world = await self._world(window, now)
            if world is None:
                return "Not planned: the tasks could not be read. Try again in a minute."
            tasks, standing, done = world
            have = {b["fields"]["task"] for b in standing}
            candidates = [self._task(t, done) for t in tasks if t["id"] not in have and (only is None or t["id"] == only)]
            if only is not None and not candidates:
                return "It has a piece in that day already." if only in have else "No such open task."
            layout = L.lay(window, now, self._busy(window, now),
                           [self._span(b) for b in standing], candidates)
            by_id = {t.id: t for t in candidates}
            titles = {t["id"]: str(t["fields"].get("title") or "") for t in tasks}
            plan_id = f"p{secrets.token_hex(5)}"
            ops = [{"op": "add", "fields": {"task": task, "start": iso(start), "minutes": minutes, "state": "planned",
                                            "plan": plan_id, **({"why": w} if (w := self._why(by_id[task], window))
                                                                else {})}}
                   for task, start, minutes in layout.placed]
            plans = self.plans()
            key = window.day.isoformat()
            # The intent first: a plan cut short by a failure is known, and a second press is the same plan's again.
            plans[key] = {"id": plan_id, "at": now, "blocks": {}, "pending": True,
                          "left": [{"task": t, "title": line(titles.get(t, ""), 120), "why": why} for t, why in layout.left]}
            self._keep_plans(plans)
            answer = await self._apply(ops, f"plan:{plan_id}")
            if answer is None:
                plans[key] = {**plans[key], "pending": False, "failed": True}
                self._keep_plans(plans)
                await self.push(now)
                return "Not planned: the blocks could not be written. Try again."
            written = {r["id"]: r["version"] for r in answer.get("records") or [] if isinstance(r, dict)}
            plans[key] = {**plans[key], "blocks": {**(plans.get(key, {}).get("blocks") or {}), **written},
                          "pending": False}
            self._keep_plans(plans)
        await self.push(now)
        return self._said(window, layout, titles)

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
        """«Undo» of one plan: its blocks still as this module wrote them are skipped; the rest are the owner's."""
        async with self.lock:
            plans = self.plans()
            key = next((k for k, p in plans.items() if isinstance(p, dict) and p.get("id") == plan_id), None)
            if key is None:
                return "That plan is not known any more: nothing taken back."
            written = plans[key].get("blocks") or {}
            rows = await self._rows(BLOCKS, [{"field": "plan", "op": "eq", "value": plan_id}])
            if rows is None:
                return "Not taken back: the blocks could not be read. Try again."
            ops, kept = [], 0
            for b in rows:
                if b["fields"].get("state") == "planned" and written.get(b["id"]) == b["version"]:
                    ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}})
                elif b["fields"].get("state") == "planned":
                    kept += 1
            if await self._apply(ops, f"undo:{plan_id}:{len(ops)}") is None:
                return "Not taken back: the blocks could not be written. Try again."
            plans[key] = {**plans[key], "undone": now, "blocks": {}}
            self._keep_plans(plans)
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
        """Pieces whose time is over passed, closed tasks' future pieces skipped, today's plan laid again where the
        calendar, a hold or a block of the owner's now crosses it."""
        if self.core.platform is None:
            return
        try:
            changed = await self._settle(now)
            mark = hashlib.sha256(json.dumps([getattr(self.tasks, "events", None) or [], self.holds(now)],
                                             sort_keys=True).encode()).hexdigest()
            if mark != self._seen or changed:
                self._seen = mark
                await self.relay(now)
        except Exception:
            log.exception("the plan's look failed")

    async def _settle(self, now: float) -> bool:
        async with self.lock:
            rows = await self._rows(BLOCKS, [{"field": "state", "op": "eq", "value": "planned"}])
            if not rows:
                return False
            open_ids = {t["id"] for t in await self._rows(TASKS, [{"field": "state", "op": "eq", "value": "open"}],
                                                           ["title"]) or []}
            ops = []
            for b in rows:
                start, end = self._span(b)
                if end <= now:
                    ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "passed"}})
                elif b["fields"].get("task") not in open_ids and start > now:
                    ops.append({"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}})
            if ops:
                await self._apply(ops, f"settle:{int(now // 30)}")
            return bool(ops)

    async def relay(self, now: float) -> str:
        """Today's plan laid again where something now crosses it: only its blocks still as this module wrote them,
        not started, crossed by the calendar, a hold or a block of the owner's — taken off and laid again in code's
        order. What could not be laid again is said."""
        async with self.lock:
            window = L.window_at(now, self.hours)
            plans = self.plans()
            mine = (plans.get(window.day.isoformat()) or {}).get("blocks") or {}
            if not mine:
                return ""
            world = await self._world(window, now)
            if world is None:
                return ""
            tasks, standing, done = world
            busy = self._busy(window, now)
            ours = [b for b in standing if mine.get(b["id"]) == b["version"] and self._span(b)[0] > now]
            others = [b for b in standing if b not in ours]
            crossed = [b for b in ours if any(a < self._span(b)[1] and self._span(b)[0] < z
                                              for a, z in busy + [self._span(o) for o in others])]
            if not crossed:
                return ""
            keep = [self._span(b) for b in standing if b not in crossed]
            by_id = {t["id"]: t for t in tasks}
            again = [self._task(by_id[b["fields"]["task"]], done) for b in crossed if b["fields"]["task"] in by_id]
            layout = L.lay(window, now, busy, keep, again)
            ops = [{"op": "set", "id": b["id"], "version": b["version"], "fields": {"state": "skipped"}} for b in crossed]
            plan_id = (plans.get(window.day.isoformat()) or {}).get("id") or f"p{secrets.token_hex(5)}"
            ops += [{"op": "add", "fields": {"task": t, "start": iso(s), "minutes": m, "state": "planned",
                                             "plan": plan_id}} for t, s, m in layout.placed]
            answer = await self._apply(ops, f"relay:{plan_id}:{int(now // 30)}")
            if answer is None:
                return ""
            records = answer.get("records") or []
            entry = plans[window.day.isoformat()]
            blocks = {k: v for k, v in (entry.get("blocks") or {}).items() if k not in {b["id"] for b in crossed}}
            blocks.update({r["id"]: r["version"] for r in records[len(crossed):] if isinstance(r, dict)})
            titles = {t["id"]: str(t["fields"].get("title") or "") for t in tasks}
            left = [{"task": t, "title": line(titles.get(t, ""), 120), "why": why} for t, why in layout.left]
            plans[window.day.isoformat()] = {**entry, "blocks": blocks, "left": (entry.get("left") or []) + left}
            self._keep_plans(plans)
        await self.push(now)
        urgent = [x for x in layout.left if (t := self._task(by_id[x[0]], done)).due and t.due <= window.day]
        if urgent:
            await self.core.tell_owner("Did not fit today any more: "
                                       + ", ".join(line(titles.get(t, ""), 40) for t, _ in urgent) + ".")
        moved = len(layout.placed)
        return f"Moved {moved} {'block' if moved == 1 else 'blocks'}." if moved else ""

    # --- the app's presses ----------------------------------------------------------------------------------------

    async def told(self, now: float, day: date | None = None) -> None:
        """`/plan` in Telegram, or «Plan my day» from the morning: the plan, and its line with «Undo» and «Busy 1 h»
        — each a button of its own, so one press leaves the other alive."""
        from .core import Button

        text = await self.plan(now, day)
        window = L.window_of(day, self.hours) if day else L.window_at(now, self.hours)
        plan_id = (self.plans().get(window.day.isoformat()) or {}).get("id")
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
        self.said = {"at": now, "text": text}
        await self.push(now)
        return text

    async def _pressed(self, data: dict, now: float, at: float) -> str:
        action = data.get("action")
        if action == "plan":
            return await self.plan(now, _day(data.get("day")) if data.get("day") else None)
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
