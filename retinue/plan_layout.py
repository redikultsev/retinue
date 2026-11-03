"""The day's layout (docs/collections.md#blocks): which open tasks go into the free time of one window, and when — a
pure function of the window, the moment, what keeps the owner busy and the tasks, so the Router's plan, its re-layout
and the tests all run the same code.

- **The window** is one day of the owner's, his `Hours`: from when to when, in which zone — set by him in the app
  (`planner.HOURS_KEY`), fixed whatever zone he travels to, since his work and his sleep keep that clock. One that
  closes after midnight belongs to the day it opened on. The window a plan is for is the one he is in while at least an
  hour of it is left, else the next one.
- **«No later than»** (`by`, a moment): no piece of a task ends after it while it is ahead; once it has passed the
  task is overdue — first in the order, laid anywhere — rather than gone.
- **Busy** is what the caller says keeps him busy — his meetings (each with BUFFER_S after it), his holds, the blocks
  already standing — plus everything before the next quarter hour.
- **The order** is code's: what is overdue or due on the window's day first, then by due date, then the shorter, then
  the older.
- **A task goes in pieces** of CHUNK_MIN_S to CHUNK_MAX_S, one piece a window; what it still needs is its estimate
  less what its passed pieces counted — for a task with a due date never less than half of it until it is done, so
  time counted for pieces he did not work cannot quietly empty it — and less what its pieces planned in other windows
  hold, so a task planned on Monday is not laid again on Tuesday.
- **Pauses** keep themselves: a piece may not make a run of tasks longer than RUN_S, the run counted by time — pieces
  less than GAP_S apart are one run, those already standing among them — so after 90 minutes the next piece starts 15
  later, and a short task cannot slip into the pause.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LEFT_MIN_S = 3600          # a window with less than this left is over: the plan is for the next one
STEP_S = 15 * 60           # starts fall on the quarter hour
BUFFER_S = 10 * 60         # after a meeting
RUN_S, PAUSE_S = 90 * 60, 15 * 60
GAP_S = 10 * 60            # pieces closer than this are one run
CHUNK_MIN_S, CHUNK_MAX_S = 30 * 60, 90 * 60
HOURS_MIN_M = 120          # a window shorter than this is refused: an hour of it must be left for a plan of today
DEFAULT_S = 30 * 60        # a task with no estimate
KEEP_PIECE_S = 30 * 60     # a task whose time is all counted, not done: planned on in pieces of this


@dataclass(frozen=True)
class Hours:
    """The owner's day for plans: it opens at `opens` and closes at `closes` — the next day when that is earlier — in
    `zone`."""
    zone: str = "UTC"
    opens: time = time(9, 0)
    closes: time = time(23, 0)

    @classmethod
    def of(cls, value: object) -> Hours | None:
        """`{zone, opens: "10:00", closes: "01:00"}`; None when it is not of that shape or shorter than HOURS_MIN_M."""
        if not isinstance(value, dict):
            return None
        try:
            opens, closes = (time.fromisoformat(str(value[k])) for k in ("opens", "closes"))
            zone = str(value["zone"])
            if not 0 < len(zone) < 64:
                return None
            ZoneInfo(zone)
        except (KeyError, ValueError, TypeError):
            return None
        span = (closes.hour * 60 + closes.minute - opens.hour * 60 - opens.minute) % (24 * 60)
        return None if span < HOURS_MIN_M or opens.second or closes.second else cls(zone, opens, closes)

    def shape(self) -> dict:
        return {"zone": self.zone, "opens": self.opens.strftime("%H:%M"), "closes": self.closes.strftime("%H:%M")}


HOURS = Hours()


@dataclass(frozen=True)
class Window:
    day: date              # the day its 10:00 is on, in WINDOW_ZONE: what the screen and «today» go by
    start: float
    end: float


@dataclass(frozen=True)
class Task:
    id: str
    minutes: int | None    # the estimate; none — DEFAULT_S
    done: int = 0          # minutes its passed pieces counted
    due: date | None = None
    created: float = 0.0
    planned: int = 0       # minutes its pieces still planned in other windows hold
    locked: int = 0        # minutes of its pieces ahead the owner put or stretched himself, in any window
    owned: bool = False    # every piece of it ahead is the owner's own: he laid its time, the plan lays none
    by: float | None = None  # «no later than»: no piece of it ends after this moment, while it is ahead


@dataclass
class Layout:
    window: Window
    placed: list[tuple[str, float, int]] = field(default_factory=list)   # (task, start, minutes)
    left: list[tuple[str, str]] = field(default_factory=list)            # (task, why)


def window_of(day: date, hours: Hours = HOURS) -> Window:
    z = ZoneInfo(hours.zone)
    closes_on = day + timedelta(days=1) if hours.closes <= hours.opens else day
    return Window(day, datetime.combine(day, hours.opens, z).timestamp(),
                  datetime.combine(closes_on, hours.closes, z).timestamp())


def window_at(now: float, hours: Hours = HOURS) -> Window:
    """The window a plan made now is for: the one he is in while an hour of it is left, else the next."""
    local = datetime.fromtimestamp(now, ZoneInfo(hours.zone))
    past_midnight = hours.closes <= hours.opens and local.time() < hours.closes
    day = local.date() - timedelta(days=1) if past_midnight else local.date()  # 00:30 is yesterday's evening
    current = window_of(day, hours)
    return current if now < current.end - LEFT_MIN_S else window_of(day + timedelta(days=1), hours)


def need_s(task: Task) -> int:
    """What a task still needs, in seconds: its estimate — or what its passed pieces and the owner's own pieces ahead
    come to, when that is more (a block he stretched to two hours says the task takes two)."""
    estimate = max((task.minutes or 0) * 60 or DEFAULT_S, (task.done + task.locked) * 60)
    counted = task.done * 60
    if task.due is not None:
        counted = min(counted, estimate // 2)
    left = estimate - counted
    return left if left > 0 else KEEP_PIECE_S


def left_s(task: Task) -> int:
    """What is left to lay in this window: what it needs less what other windows already hold; 0 — nothing, and
    nothing for a task whose pieces ahead are all the owner's own."""
    return 0 if task.owned else max(0, need_s(task) - task.planned * 60)


def piece_s(need: int) -> int:
    """The next piece of what is left: at most CHUNK_MAX_S; a tail under CHUNK_MIN_S is shared with the piece before
    it, both on the quarter hour."""
    if need <= CHUNK_MAX_S:
        return max(STEP_S, math.ceil(need / STEP_S) * STEP_S)
    if need - CHUNK_MAX_S < CHUNK_MIN_S:
        return math.ceil(need / 2 / STEP_S) * STEP_S
    return CHUNK_MAX_S


def ordered(tasks: list[Task], window: Window) -> list[Task]:
    """Overdue and due on the window's day first, then by due date, then the shorter, then the older."""
    def key(t: Task):
        urgent = t.due is not None and t.due <= window.day
        return (0 if urgent else 1, t.due or date.max, need_s(t), t.created, t.id)
    return sorted(tasks, key=key)


def _free(at: float, length: int, busy: list[tuple[float, float]]) -> bool:
    return all(at + length <= a or at >= b for a, b in busy)


def _run(at: float, length: int, pieces: list[tuple[float, float]]) -> int:
    """The run of tasks a piece at `at` would be part of: itself and every piece reached through gaps under GAP_S."""
    lo, hi, total = at, at + length, length
    grew = True
    seen: set[tuple[float, float]] = set()
    while grew:
        grew = False
        for a, b in pieces:
            if (a, b) not in seen and a < hi + GAP_S and b > lo - GAP_S:
                seen.add((a, b))
                lo, hi, total, grew = min(lo, a), max(hi, b), total + int(b - a), True
    return total


def first_free(start: float, end: float, length: int, taken: list[tuple[float, float]],
               pieces: list[tuple[float, float]]) -> float | None:
    """The first quarter hour from `start` where a piece of `length` is free of `taken`, ends by `end` and keeps the
    run of `pieces` it joins under RUN_S; None — there is none."""
    at = math.ceil(start / STEP_S) * STEP_S
    while at + length <= end:
        if _free(at, length, taken) and _run(at, length, pieces) <= RUN_S:
            return at
        at += STEP_S
    return None


def free(at: float, length: int, taken: list[tuple[float, float]]) -> bool:
    """Whether a piece of `length` at `at` crosses nothing in `taken`."""
    return _free(at, length, taken)


def lay(window: Window, now: float, busy: list[tuple[float, float]], pieces: list[tuple[float, float]],
        tasks: list[Task]) -> Layout:
    """The tasks laid into the window's free time, in code's order: each its next piece at the first quarter hour
    that is free, inside the window, and keeps the run under RUN_S. `busy` is the caller's (meetings with their
    buffer, holds); `pieces` the task blocks already standing in the window, which are busy too and count in runs."""
    out = Layout(window)
    start = max(window.start, math.ceil(now / STEP_S) * STEP_S)
    taken = list(busy) + list(pieces)
    standing = list(pieces)
    for task in ordered(tasks, window):
        if not (need := left_s(task)):
            continue
        length = piece_s(need)
        end = window.end if task.by is None or task.by <= now else min(window.end, task.by)
        at = first_free(start, end, length, taken, standing)
        if at is None:
            out.left.append((task.id, f"no free {length // 60} min before the window ends" if end == window.end
                             else f"no free {length // 60} min before its deadline"))
            continue
        out.placed.append((task.id, at, length // 60))
        taken.append((at, at + length))
        standing.append((at, at + length))
    return out


def meeting_busy(events: list[dict], start: float, end: float) -> list[tuple[float, float]]:
    """What the window's events keep busy: each busy occurrence that is not the whole day, BUFFER_S longer."""
    return [(e["start"], e["end"] + BUFFER_S) for e in events
            if e.get("busy") and not e.get("all_day") and e["end"] > start and e["start"] < end]
