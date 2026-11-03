"""The owner's tasks, his day and his week on the face (docs/face.md#tasks-day-week): one read for the three tabs of
«Today» (`GET /api/plan`), and the busy window the Router pushes (`POST /owner/calendar`).

What the tabs show is put together here on each read, never kept beside its sources:

- **the tasks** — the open ones of the collection `tasks`, and every one, open or not, whose slot falls between
  yesterday and the window's end (a done task stays on its day) — as the records are, with the collection's `rev` for
  the face to know when to read again; its writes go through the collections' own route (`POST /api/c/tasks`), as
  any screen's do;
- **the calendar** — the occurrences of today and the next fourteen days as the Router last pushed them, with when
  that was: a busy window older than CALENDAR_STALE_S is shown as such, never as the day being free;
- **the asks and the reminders** — the person's rows and the reminders of the assistant's digest on record (`feed`),
  each with what «Plan» names it by.

The Router pushes the window whole each time it reads the calendar (`tasks.Tasks.calendar`), replacing the one
before; the platform never calls the Router. Its titles are the owner's calendar's words, for his screen alone: no
agent reads them here.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING

from aiohttp import web

from .collections import Refused, Rejected
from .courier import line

if TYPE_CHECKING:
    from .platformd import Platform

TASKS, BLOCKS = "tasks", "blocks"
SCHEMA = 2
CALENDAR_BYTES = 128 * 1024  # a window as the Router pushes it, at most
CALENDAR_EVENTS = 400
CALENDAR_STALE_S = 30 * 60   # a window older than this is said to be old
TITLE_CHARS = 100
SPAN_S = 40 * 86400          # an occurrence lasts no longer than this; a window reaches no further than this ahead
SLOTS_BEHIND_DAYS = 1        # the tasks with a slot from this many days back are read beside the open ones
SLOTS_AHEAD_DAYS = 15
PAGES = 5                    # 200 records a page: the open tasks read at most this many pages


class Refusal(ValueError):
    """A window not shaped as the contract says: refused whole, the one on record stays."""


PLAN_KEY = "plan.state"      # the platform's kv: the Router's last push of the day plan
PLAN_BYTES = 64 * 1024
PRESSES = ("plan", "place", "undo", "busy", "free", "remove", "hours")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
PLAN_ID = re.compile(r"p[0-9a-f]{10}")
TOKEN = re.compile(r"[0-9a-f]{32}")       # a person's row's `t` (`feed.TOKEN`)
REMINDER = re.compile(r"reminder:\d{1,12}")  # a reminder of the digest (`feed.REMINDER_ID`)
HM = re.compile(r"[0-2]\d:[0-5]\d")
ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9_+-]{1,30}){0,2}")  # an IANA name: the Router checks it is one


def clean_press(body: object) -> dict:
    """A press as the app may make it: its action, and only the fields that action takes, each of its shape."""
    if not isinstance(body, dict) or body.get("action") not in PRESSES:
        raise Refusal(f"action: one of {', '.join(PRESSES)}")
    action, out = body["action"], {"action": body["action"]}
    shapes = {"day": DAY, "task": ID, "block": ID, "plan": PLAN_ID, "t": TOKEN, "ref": REMINDER, "opens": HM, "closes": HM,
              "zone": ZONE}
    takes = {"plan": ("day", "days"), "place": ("day", "task"), "undo": ("plan",), "remove": ("block",), "busy": ("minutes",),
             "free": (), "hours": ("opens", "closes", "zone")}[action]
    if action == "place" and body.get("task") is None:
        # «Plan» on a person's row or a reminder: the Router makes its task and lays it — one press, no «make» first.
        takes = ("day", "t") if body.get("t") is not None else ("day", "ref")
    for key in takes:
        value = body.get(key)
        if key == "minutes":
            if value not in (30, 60, 120):
                raise Refusal("minutes: 30, 60 or 120")
        elif key == "days":
            if value is None:
                continue
            if value not in (1, 7):
                raise Refusal("days: 1 or 7")
        elif value is None and key == "day" and action == "plan":
            continue
        elif not isinstance(value, str) or not shapes[key].fullmatch(value):
            raise Refusal(f"{key}: not of its shape")
        out[key] = value
    return out


def clean_plan(body: object, now: float) -> dict:
    """The Router's push, checked for its shape: plans by window day, holds, what the last press said."""
    if not isinstance(body, dict) or type(body.get("at")) not in (int, float):
        raise Refusal("at: when it was made")
    plans, holds, said = body.get("plans") or {}, body.get("holds") or [], body.get("said") or {}
    if not isinstance(plans, dict) or len(plans) > 31 or not all(isinstance(k, str) and DAY.fullmatch(k) for k in plans):
        raise Refusal("plans: by window day")
    kept = {}
    for day, entry in plans.items():
        if not isinstance(entry, dict):
            raise Refusal(f"plans.{day}: an object")
        left = [{"task": str(x.get("task") or "")[:26], "title": line(str(x.get("title") or ""), 120),
                 "why": line(str(x.get("why") or ""), 120)} for x in entry.get("left") or [] if isinstance(x, dict)][:50]
        blocks = entry.get("blocks") or {}
        short = [{"task": str(x.get("task") or "")[:26], "title": line(str(x.get("title") or ""), 120),
                  "minutes": int(x["minutes"])} for x in entry.get("short") or []
                 if isinstance(x, dict) and type(x.get("minutes")) is int and 0 < x["minutes"] <= 100_000][:50]
        kept[day] = {"id": str(entry.get("id") or "")[:12], "at": float(entry.get("at") or 0), "left": left, "short": short,
                     "blocks": sorted(k for k in blocks if isinstance(k, str) and ID.fullmatch(k))[:200],
                     "undone": bool(entry.get("undone")), "failed": bool(entry.get("failed"))}
    recent = [{"id": str(p["id"]), "by": p.get("by") if p.get("by") in ("owner", "assistant") else "owner",
               "at": float(p.get("at") or 0), "undone": bool(p.get("undone")), "failed": bool(p.get("failed"))}
              for p in (body.get("recent") or [])[:20]
              if isinstance(p, dict) and isinstance(p.get("id"), str) and PLAN_ID.fullmatch(p["id"])
              and type(p.get("at")) in (int, float)] if isinstance(body.get("recent"), list) else []
    if not isinstance(holds, list) or len(holds) > 20:
        raise Refusal("holds: a list")
    held = [{"from": float(h["from"]), "to": float(h["to"]), "label": line(str(h.get("label") or "Busy"), 40)}
            for h in holds if isinstance(h, dict) and all(type(h.get(k)) in (int, float) for k in ("from", "to"))]
    told = {"at": float(said["at"]), "text": line(str(said.get("text") or ""), 500),
            **({"day": said["day"]} if isinstance(said.get("day"), str) and len(said["day"]) == 10 else {})} \
        if isinstance(said, dict) and type(said.get("at")) in (int, float) else {}
    window = body.get("window") or {}
    shaped = isinstance(window, dict) and isinstance(window.get("zone"), str) and len(window["zone"]) < 64 and \
        all(isinstance(window.get(k), str) and HM.fullmatch(window[k]) for k in ("opens", "closes"))
    return {"at": float(body["at"]), "plans": kept, "recent": recent, "holds": held, "said": told,
            **({"window": {k: window[k] for k in ("zone", "opens", "closes")}} if shaped else {})}


def clean_calendar(body: object, now: float) -> tuple[list[dict], float, int]:
    """The Router's window, checked: `{events: [{start, end, title, all_day, busy}], at, failed}`. One event off the
    contract refuses the whole push."""
    if not isinstance(body, dict) or not isinstance(body.get("events"), list) or len(body["events"]) > CALENDAR_EVENTS:
        raise Refusal(f"events: a list of at most {CALENDAR_EVENTS}")
    at, failed = body.get("at"), body.get("failed", 0)
    if type(at) not in (int, float) or not math.isfinite(at) or not 0 < at <= now + 60:
        raise Refusal("at: when the calendar was read")
    if type(failed) is not int or not 0 <= failed <= 100:
        raise Refusal("failed: how many accounts were not read")
    events = []
    for n, event in enumerate(body["events"]):
        if not isinstance(event, dict) or set(event) != {"start", "end", "title", "all_day", "busy"}:
            raise Refusal(f"events[{n}]: start, end, title, all_day, busy")
        start, end = event["start"], event["end"]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)) or \
                not now - SPAN_S < start <= end <= start + SPAN_S or start > now + SPAN_S:
            raise Refusal(f"events[{n}]: start and end, two moments, the end not before the start")
        if not isinstance(event["title"], str) or type(event["all_day"]) is not bool or type(event["busy"]) is not bool:
            raise Refusal(f"events[{n}]: title a string, all_day and busy true or false")
        events.append({"start": float(start), "end": float(end), "title": line(event["title"], TITLE_CHARS) or "Busy",
                       "all_day": event["all_day"], "busy": event["busy"]})
    return sorted(events, key=lambda e: (e["start"], e["end"])), float(at), failed


def _iso(moment: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(moment, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Plan:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        db = platform.store.db
        db.execute("CREATE TABLE IF NOT EXISTS owner_calendar (id INTEGER PRIMARY KEY CHECK (id = 1), "
                   "events TEXT NOT NULL, at REAL NOT NULL, failed INTEGER NOT NULL DEFAULT 0)")
        db.commit()

    def routes(self) -> list:
        return [web.get("/api/plan", self.face_plan), web.post("/api/plan/press", self.face_press)]

    def owner_routes(self) -> list:
        return [web.post("/owner/calendar", self.owner_calendar), web.post("/owner/plan", self.owner_plan)]

    # --- the Router's plan ----------------------------------------------------------------------------------------

    async def owner_plan(self, request: web.Request) -> web.Response:
        """The day plan's state from the Router (`planner.Planner.push`): the plans by window, the holds, what the last
        press came to — kept whole, shown on Day beside the blocks themselves."""
        if request.content_length is None or request.content_length > PLAN_BYTES:
            return web.json_response({"error": f"a plan over {PLAN_BYTES} bytes"}, status=413)
        try:
            body = clean_plan(await request.json(), self.platform.clock())
        except (ValueError, Refusal) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        store = self.platform.store
        store.set(PLAN_KEY, json.dumps(body, ensure_ascii=False))
        store._event("plan", "", "plan", {"at": body["at"]})  # an open «Today» reads its day again
        store.db.commit()
        return web.json_response({"ok": True})

    def plan_state(self) -> dict:
        try:
            state = json.loads(self.platform.store.get(PLAN_KEY) or "{}")
        except ValueError:
            state = {}
        return {"at": state.get("at"), "plans": state.get("plans") or {}, "recent": state.get("recent") or [],
                "holds": state.get("holds") or [],
                "said": state.get("said") or {}, "window": state.get("window") or {"zone": self.platform.views.zone(),
                                                                                     "opens": "09:00", "closes": "23:00"}}

    async def face_press(self, request: web.Request) -> web.Response:
        """A press on the plan in the app `{action, day?, task?, plan?, block?, minutes?}`: only its shape is checked
        here, stamped with its moment, and it goes to the Router as a note (`plan-press`)."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            note = clean_press(body)
        except Refusal as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self.platform.store.add_note("plan-press", "", {**note, "at": self.platform.clock()})
        return web.json_response({"ok": True})

    # --- the Router's window --------------------------------------------------------------------------------------

    async def owner_calendar(self, request: web.Request) -> web.Response:
        if request.content_length is None or request.content_length > CALENDAR_BYTES:
            return web.json_response({"error": f"a window over {CALENDAR_BYTES} bytes"}, status=413)
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            events, at, failed = clean_calendar(body, self.platform.clock())
        except Refusal as exc:
            return web.json_response({"error": str(exc)}, status=400)
        store = self.platform.store
        store.db.execute("INSERT OR REPLACE INTO owner_calendar (id, events, at, failed) VALUES (1, ?, ?, ?)",
                         (json.dumps(events, ensure_ascii=False), at, failed))
        store.db.commit()  # an open «Today» reads it at its next look: the window moves every ten minutes at most
        return web.json_response({"ok": True, "events": len(events)})

    def calendar(self) -> dict:
        row = self.platform.store.db.execute("SELECT events, at, failed FROM owner_calendar WHERE id = 1").fetchone()
        if row is None:
            return {"events": [], "at": None, "failed": 0, "stale": True}
        return {"events": json.loads(row[0]), "at": row[1], "failed": row[2],
                "stale": self.platform.clock() - row[1] > CALENDAR_STALE_S}

    # --- the face -------------------------------------------------------------------------------------------------

    async def tasks(self) -> dict:
        """The open tasks, and every task whose slot falls in the window: `{rows, rev, goals, missing}` — `missing`
        when there is no collection `tasks` yet."""
        store, now = self.platform.collections, self.platform.clock()
        schema = await store.schema(TASKS)
        if schema is None:
            return {"rows": [], "rev": None, "space": None, "goals": [], "missing": True, "blocks": []}
        rows: dict[str, dict] = {}
        after = None
        for _ in range(PAGES):
            answer = await store.query(TASKS, [{"field": "state", "op": "eq", "value": "open"}], after=after)
            rows.update((r["id"], r) for r in answer["rows"])
            if not (after := answer["next"]):
                break
        fields = schema["form"]["fields"]
        blocks: list[dict] = []
        if await store.schema(BLOCKS) is not None:
            span = [_iso(now - SLOTS_BEHIND_DAYS * 86400 - 86400), _iso(now + SLOTS_AHEAD_DAYS * 86400)]
            after = None
            for _ in range(PAGES):
                answer = await store.query(BLOCKS, [{"field": "start", "op": "between", "value": span},
                                                    {"field": "state", "op": "in", "value": ["planned", "passed"]}],
                                           after=after)
                blocks += [{k: r[k] for k in ("id", "fields", "version")} for r in answer["rows"]]
                if not (after := answer["next"]):
                    break
            missing = {b["fields"].get("task") for b in blocks} - set(rows)
            for task_id in missing:  # a passed piece of a task closed since: its title still shows on its day
                if isinstance(task_id, str) and (record := await store.get(TASKS, task_id)) and not record["deleted"]:
                    rows[task_id] = record
        goal = fields.get("goal") or {}
        goals = [{"value": v, "label": (goal.get("labels") or {}).get(v, v)} for v in goal.get("values") or []] \
            if goal.get("type") == "enum" else []
        return {"rows": [{k: r[k] for k in ("id", "fields", "version", "created", "updated")} for r in rows.values()],
                "rev": schema["rev"], "space": schema["space"], "goals": goals, "missing": False, "blocks": blocks}

    async def face_plan(self, request: web.Request) -> web.Response:
        """`GET /api/plan`: what the tabs Tasks, Day and Week of «Today» show, read at once."""
        try:
            tasks = await self.tasks()
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except Rejected as exc:
            return web.json_response({"error": exc.reason}, status=400)
        items, _, _ = self.platform.feed.digest()
        asks = [{"id": i["id"], "kind": i["kind"], "who": i["who"], "title": i["title"], "since": i["since"],
                 **({"t": i["t"]} if i.get("t") else {})} for i in items if i["kind"] in ("letter", "chat")]
        reminders = [{"id": i["id"], "title": i["title"], "at": i["since"], "when": i.get("when"),
                      "repeat": i.get("repeat")} for i in items if i["kind"] == "reminder"]
        return web.json_response({"schema": SCHEMA, "now": self.platform.clock(), "tz": self.platform.views.zone(),
                                  "tasks": tasks, "calendar": self.calendar(), "asks": asks,
                                  "reminders": reminders, "plan": self.plan_state()})
