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
    return Planner(core, SimpleNamespace(events=events), H), told


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
