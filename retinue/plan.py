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
  each with what «Make it a task» names it by.

The Router pushes the window whole each time it reads the calendar (`tasks.Tasks.calendar`), replacing the one
before; the platform never calls the Router. Its titles are the owner's calendar's words, for his screen alone: no
agent reads them here.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

from aiohttp import web

from .collections import Refused, Rejected
from .courier import line

if TYPE_CHECKING:
    from .platformd import Platform

TASKS = "tasks"
SCHEMA = 1
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
        return [web.get("/api/plan", self.face_plan)]

    def owner_routes(self) -> list:
        return [web.post("/owner/calendar", self.owner_calendar)]

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
            return {"rows": [], "rev": None, "space": None, "goals": [], "missing": True}
        rows: dict[str, dict] = {}
        after = None
        for _ in range(PAGES):
            answer = await store.query(TASKS, [{"field": "state", "op": "eq", "value": "open"}], after=after)
            rows.update((r["id"], r) for r in answer["rows"])
            if not (after := answer["next"]):
                break
        fields = schema["form"]["fields"]
        if fields.get("slot", {}).get("type") == "instant":
            span = [_iso(now - SLOTS_BEHIND_DAYS * 86400 - 86400), _iso(now + SLOTS_AHEAD_DAYS * 86400)]
            answer = await store.query(TASKS, [{"field": "slot", "op": "between", "value": span}])
            rows.update((r["id"], r) for r in answer["rows"])
        goal = fields.get("goal") or {}
        goals = [{"value": v, "label": (goal.get("labels") or {}).get(v, v)} for v in goal.get("values") or []] \
            if goal.get("type") == "enum" else []
        return {"rows": [{k: r[k] for k in ("id", "fields", "version", "created", "updated")} for r in rows.values()],
                "rev": schema["rev"], "space": schema["space"], "goals": goals, "missing": False}

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
                                  "reminders": reminders})
