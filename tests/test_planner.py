"""The day plan, the Router's side (`planner`): a press lays the open tasks into the window's free time as `blocks`
written by `code:plan` in one apply; «Undo» takes back only what is still as it wrote it; «Busy» lays again what it
crosses; each look marks what passed and frees what a closed task held — against the platform's own collections."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from retinue.collections import Writer
from datetime import time as wall
from zoneinfo import ZoneInfo

from retinue.plan_layout import Hours
from retinue.planner import Planner, iso, moment
from retinue.protocol import Store
from test_collections_assistant import platform_of
from test_face import faced
from test_platformd import ROUTER, short_wait  # noqa: F401 — the fixture is autouse

DEPLOY = Path(__file__).parent.parent / "deploy" / "collections"
DEFINE, SCREEN = Writer("owner", "define"), Writer("owner", "screen")
H = Hours("Europe/Berlin", wall(10, 0), wall(1, 0))


def zone():
    return ZoneInfo(H.zone)


def msk(day: int, hm: str) -> float:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=zone()).timestamp()


async def setup(world, tmp_path):
    store = world.platform.collections
    for name in ("tasks", "blocks"):
        spec = json.loads((DEPLOY / f"{name}.json").read_text())
        await store.define(DEFINE, name, spec["space"], spec["form"], world.clock.now, views=spec["views"])
        await store.rights(DEFINE, name, json.loads((DEPLOY / f"{name}.rights.json").read_text()), world.clock.now)
    told: list[str] = []

    async def tell_owner(text, **_):
        told.append(text)
    router = Store(str(tmp_path / "router.sqlite"))
    core = SimpleNamespace(platform=platform_of(world), store=router, tz=H.zone, tell_owner=tell_owner)
    events = [{"start": msk(10, "19:00"), "end": msk(10, "20:00"), "title": "Lesson", "all_day": False, "busy": True}]
    return Planner(core, SimpleNamespace(events=events, events_at=1e12), H), told  # a busy window always fresh


async def add(world, **fields) -> dict:
    answer = await world.platform.collections.apply(SCREEN, "tasks", [{"op": "add", "fields": {"state": "open", **fields}}],
                                                    world.clock.now)
    return answer["records"][0]


async def blocks(world, state="planned") -> list[dict]:
    rows = (await world.platform.collections.query("blocks", [{"field": "state", "op": "eq", "value": state}]))["rows"]
    return sorted(rows, key=lambda b: b["fields"]["start"])


def hm(value: str) -> str:
    return datetime.fromtimestamp(moment(value), zone()).strftime("%H:%M")


def test_a_press_lays_the_window_in_one_apply_and_undo_takes_back_only_what_is_still_as_written(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        late = await add(world, title="Pay the invoice", minutes=15, due="2026-11-09")
        talk = await add(world, title="Record the talk", minutes=180, due="2026-11-13")
        said = await planner.plan(msk(10, "18:00"))
        assert "Plan for Tue 10 Nov: 2 tasks" in said, said
        laid = await blocks(world)
        assert [(b["fields"]["task"], hm(b["fields"]["start"]), b["fields"]["minutes"]) for b in laid] == \
            [(late["id"], "18:00", 15), (talk["id"], "20:15", 90)], "around the lesson and its ten minutes after"
        assert laid[0]["fields"]["why"] == "overdue" and len({b["fields"]["plan"] for b in laid}) == 1
        [change] = await world.platform.collections.history("blocks", laid[0]["id"])
        assert (change["actor"], change["via"]) == ("code:plan", "code")
        state = world.platform.plan.plan_state()
        assert state["plans"]["2026-11-10"]["id"] == laid[0]["fields"]["plan"], "the state pushed to the platform"

        moved = await world.platform.collections.apply(SCREEN, "blocks", [
            {"op": "set", "id": laid[1]["id"], "version": 1, "fields": {"start": iso(msk(10, "22:00")), "locked": True}}],
            world.clock.now)
        assert moved["records"][0]["version"] == 2
        said = await planner.undo(laid[0]["fields"]["plan"], msk(10, "18:05"))
        assert said == "Taken back: 1 block. 1 you changed since stay."
        assert [b["id"] for b in await blocks(world)] == [laid[1]["id"]], "his move stands"
        assert "not known" in await planner.undo("p0000000000", msk(10, "18:05"))
    faced(tmp_path, run)


def test_a_task_planned_on_one_day_is_laid_on_the_next_only_for_what_is_left(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        bill = await add(world, title="Pay the bill", minutes=15)
        talk = await add(world, title="Record the talk", minutes=180, due="2026-11-13")
        await planner.plan(msk(10, "18:00"))
        from datetime import date
        said = await planner.plan(msk(10, "18:00"), date(2026, 11, 11))
        wed = [b for b in await blocks(world) if b["fields"]["start"] >= iso(msk(11, "10:00"))]
        assert [(b["fields"]["task"], b["fields"]["minutes"]) for b in wed] == [(talk["id"], 90)], \
            "the bill is Tuesday's already; of the talk's three hours Tuesday holds ninety, Wednesday the rest"
        assert bill["id"] not in {b["fields"]["task"] for b in wed} and "1 task" in said
        third = await planner.plan(msk(10, "18:00"), date(2026, 11, 12))
        assert third.startswith("Nothing planned"), "nothing is left to lay: no task twice"
    faced(tmp_path, run)


def test_busy_lays_again_what_it_crosses_and_each_look_counts_what_passed_and_frees_what_is_closed(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, told = await setup(world, tmp_path)
        bank = await add(world, title="Call the bank", minutes=30)
        read = await add(world, title="Read the contract", minutes=60, due="2026-11-10")
        await planner.plan(msk(10, "20:00"))
        before = {b["fields"]["task"]: hm(b["fields"]["start"]) for b in await blocks(world)}
        assert before == {read["id"]: "20:15", bank["id"]: "21:15"}
        said = await planner.busy(60, msk(10, "20:20"))
        assert said.startswith("Busy till 21:20") and "Moved" in said
        after = {b["fields"]["task"]: hm(b["fields"]["start"]) for b in await blocks(world)}
        assert after == {read["id"]: "20:15", bank["id"]: "21:30"}, "a piece already begun stays; the one it crosses moves"
        assert not told, "nothing urgent fell out: no line in Telegram"

        await world.platform.collections.apply(SCREEN, "tasks", [{"op": "set", "id": bank["id"], "version": 1,
                                                                  "fields": {"state": "done"}}], world.clock.now)
        await planner.cycle(msk(10, "20:25"))
        skipped = {b["fields"]["task"] for b in await blocks(world, "skipped")}
        assert bank["id"] in skipped, "a done task's piece ahead frees its time"
        assert await blocks(world, "passed") == []
        await planner.cycle(msk(10, "23:59"))
        passed = await blocks(world, "passed")
        assert [b["fields"]["task"] for b in passed] == [read["id"]], "its time is over: counted"
    faced(tmp_path, run)


def test_a_press_from_the_app_older_than_ten_minutes_is_dropped_and_said(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        said = await planner.press({"action": "busy", "minutes": 60, "at": msk(10, "18:00")}, msk(10, "18:30"))
        assert "too late" in said and planner.holds(msk(10, "18:30")) == []
        assert world.platform.plan.plan_state()["said"]["text"] == said
    faced(tmp_path, run)


def test_a_second_plan_of_the_same_day_keeps_the_first_one_s_blocks_its_own(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        bill = await add(world, title="Pay the bill", minutes=30)
        await planner.plan(msk(10, "18:00"))
        first = (await blocks(world))[0]["fields"]["plan"]
        later = await add(world, title="Call the school", minutes=30)
        await planner.press({"action": "place", "day": "2026-11-10", "task": later["id"], "at": msk(10, "18:01")},
                            msk(10, "18:01"))
        assert {b["fields"]["task"] for b in await blocks(world)} == {bill["id"], later["id"]}
        assert "Taken back: 1 block." == await planner.undo(first, msk(10, "18:02")), "the first plan is still known"
        assert [b["fields"]["task"] for b in await blocks(world)] == [later["id"]], "the second stands"
        day = world.platform.plan.plan_state()["plans"]["2026-11-10"]
        assert day["id"] != first and not day["undone"], "the day shows its latest plan not taken back"
    faced(tmp_path, run)


def test_a_block_the_owner_lays_over_the_plan_moves_the_plan_s_piece_at_the_next_look(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        read = await add(world, title="Read the contract", minutes=60)
        mine = await add(world, title="Walk", minutes=60)
        await planner.plan(msk(10, "17:55"))
        [laid] = [b for b in await blocks(world) if b["fields"]["task"] == read["id"]]
        assert hm(laid["fields"]["start"]) == "18:00"
        await world.platform.collections.apply(SCREEN, "blocks", [{"op": "add", "fields": {
            "task": mine["id"], "start": iso(msk(10, "18:00")), "minutes": 60, "state": "planned", "locked": True}}],
            world.clock.now)
        await planner.cycle(msk(10, "17:56"))
        [moved] = [b for b in await blocks(world) if b["fields"]["task"] == read["id"]]
        assert moved["id"] == laid["id"] and hm(moved["fields"]["start"]) == "20:15", \
            "moved, not taken off and added again: after his walk and the lesson with its ten minutes"
        assert "Taken back: 1 block." == await planner.undo(laid["fields"]["plan"], msk(10, "17:57")), \
            "the move kept the version its plan knows"
    faced(tmp_path, run)


def test_a_task_whose_time_the_owner_laid_himself_loses_the_plan_s_extra_piece(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        essay = await add(world, title="Write the essay", minutes=120)
        await planner.plan(msk(10, "09:00"))
        assert [b["fields"]["minutes"] for b in await blocks(world)] == [90]
        await world.platform.collections.apply(SCREEN, "blocks", [{"op": "add", "fields": {
            "task": essay["id"], "start": iso(msk(10, "21:00")), "minutes": 120, "state": "planned", "locked": True}}],
            world.clock.now)
        await planner.cycle(msk(10, "09:01"))
        assert [(b["fields"]["minutes"], bool(b["fields"].get("locked"))) for b in await blocks(world)] == [(120, True)], \
            "his two hours are all of it: the plan's ninety minutes go"
    faced(tmp_path, run)


def test_no_plan_on_a_calendar_not_read_lately_and_old_plans_by_day_are_moved_over(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        await add(world, title="Pay the bill", minutes=30)
        planner.tasks.events_at = msk(10, "17:00")
        said = await planner.plan(msk(10, "18:00"))
        assert said.startswith("Not planned: the calendar") and await blocks(world) == []
        planner.core.store.set("plan.plans", json.dumps({"2026-11-10": {"id": "p0123456789", "at": 1.0,
                                                                         "blocks": {"01ABC": 3}, "left": []}}))
        planner._migrate()
        moved = planner.plans()["p0123456789"]
        assert moved["windows"] == ["2026-11-10"] and moved["blocks"]["01ABC"]["version"] == 3
    faced(tmp_path, run)
