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

from retinue.mirror import Mirror
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
    router.set("plan.activities", "[]")  # no meals unless a test sets them: the times below are the tasks' own
    core = SimpleNamespace(platform=platform_of(world), store=router, tz=H.zone, tell_owner=tell_owner)
    events = [{"start": msk(10, "19:00"), "end": msk(10, "20:00"), "title": "Lesson", "all_day": False, "busy": True}]
    mirror = Mirror(platform_of(world))  # the plan goes up to the platform's own app
    return Planner(core, SimpleNamespace(events=events, events_at=1e12), H, mirror), told  # a busy window always fresh


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
        assert "10 Nov" in said, said
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
        assert said
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
        assert await blocks(world, "passed") == [], "over, but the evening is not: decided when the next window opens"
        planner.sign(msk(10, "20:20"))
        await planner.cycle(msk(11, "10:05"))
        passed = await blocks(world, "passed")
        assert [b["fields"]["task"] for b in passed] == [read["id"]], "he was here that day: counted"
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
        await planner.plan(msk(10, "18:00"))
        assert await blocks(world) == []
        planner.core.store.set("plan.plans", json.dumps({"2026-11-10": {"id": "p0123456789", "at": 1.0,
                                                                         "blocks": {"01ABC": 3}, "left": []}}))
        planner._migrate()
        moved = planner.plans()["p0123456789"]
        assert moved["windows"] == ["2026-11-10"] and moved["blocks"]["01ABC"]["version"] == 3
    faced(tmp_path, run)


def test_a_block_the_owner_stretched_is_the_task_s_length_and_the_next_day_lays_none_of_it(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        homework = await add(world, title="Homework for the lesson", minutes=30)
        await planner.plan(msk(10, "09:00"))
        [laid] = await blocks(world)
        await world.platform.collections.apply(SCREEN, "blocks", [{"op": "set", "id": laid["id"], "version": laid["version"],
                                                                   "fields": {"minutes": 120, "locked": True}}],
                                               world.clock.now)
        said = await planner.plan(msk(10, "09:05"), datetime(2026, 11, 11).date())
        assert "11 Nov" in said, "his two hours are the task's time: nothing left to lay"
        assert [(b["fields"]["task"], b["fields"]["minutes"]) for b in await blocks(world)] == [(homework["id"], 120)]
    faced(tmp_path, run)


def test_a_week_is_laid_window_after_window_under_one_plan_and_one_undo_takes_it_all_back(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        await add(world, title="Write the report", minutes=600)
        await add(world, title="Pay the bill", minutes=15)
        said = await planner.press({"action": "plan", "days": 7, "at": msk(10, "09:00")}, msk(10, "09:00"))
        assert "10 Nov" in said, said
        laid = await blocks(world)
        assert len({b["fields"]["plan"] for b in laid}) == 1
        assert sum(b["fields"]["minutes"] for b in laid) == 615, "each window saw what the one before laid: no doubles"
        assert len({hm(b["fields"]["start"]) + b["fields"]["start"][:10] for b in laid}) == len(laid)
        [entry] = world.platform.plan.plan_state()["recent"]
        assert entry["by"] == "owner" and not entry["undone"]
        assert await planner.undo(laid[0]["fields"]["plan"], msk(10, "09:01")) == f"Taken back: {len(laid)} blocks."
        assert await blocks(world) == []
    faced(tmp_path, run)


def test_her_moves_on_his_word_go_under_one_plan_of_her_turn_and_one_undo_takes_them_and_her_estimate_back(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, told = await setup(world, tmp_path)
        homework = await add(world, title="English homework", minutes=60)
        await planner.plan(msk(10, "17:00"))
        [laid] = await blocks(world)
        assert hm(laid["fields"]["start"]) == "17:00"
        ok, said = await planner.move("t1", homework["id"], "2026-11-11", "evening", "", False, msk(10, "16:55"))
        assert ok, said
        [moved] = await blocks(world)
        assert moved["id"] == laid["id"] and moved["fields"]["start"] == iso(msk(11, "18:00")), "the first free evening time"
        assert moved["fields"]["plan"] != laid["fields"]["plan"], "under her turn's plan"
        ok, said = await planner.estimate("t1", homework["id"], 120, "", msk(10, "16:55"))
        assert ok, said
        task = await world.platform.collections.get("tasks", homework["id"])
        assert task["fields"]["minutes"] == 120
        [change] = [c for c in await world.platform.collections.history("tasks", homework["id"]) if c["actor"] != "owner"]
        assert (change["actor"], change["via"]) == ("assistant", "assistant"), "her write, not the rule's"
        await planner.turn_done("t1")
        assert told == ["Plan changed: English homework: Tue 10 Nov 17:00 → Wed 11 Nov 18:00; "
                        "English homework: 60 → 120 min."]
        assert planner.turn_plans == {}, "a turn's plan ends with the turn"
        recent = world.platform.plan.plan_state()["recent"]
        hers = next(p for p in recent if p["by"] == "assistant")
        assert hers["id"] == moved["fields"]["plan"], "the grid lights her plan"
        assert await planner.undo(hers["id"], msk(10, "17:10")) == "Taken back: 1 block, 1 estimate."
        [back] = await blocks(world)
        assert back["fields"]["start"] == laid["fields"]["start"] and back["fields"]["plan"] == laid["fields"]["plan"]
        assert (await world.platform.collections.get("tasks", homework["id"]))["fields"]["minutes"] == 60
    faced(tmp_path, run)


def test_her_move_keeps_off_meetings_and_his_own_pieces_and_lays_a_new_piece_where_none_is(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, told = await setup(world, tmp_path)
        report = await add(world, title="Write the report", minutes=60)
        bank = await add(world, title="Call the bank", minutes=30)
        await planner.plan(msk(10, "17:00"), only=report["id"])
        ok, said = await planner.move("t2", report["id"], "2026-11-10", "", "19:30", False, msk(10, "16:55"))
        assert not ok and "is taken" in said, "the lesson is there"
        [laid] = await blocks(world)
        await world.platform.collections.apply(SCREEN, "blocks", [
            {"op": "set", "id": laid["id"], "version": laid["version"], "fields": {"locked": True}}], world.clock.now)
        ok, said = await planner.move("t2", report["id"], "2026-11-10", "", "21:00", False, msk(10, "16:55"))
        assert not ok
        ok, _ = await planner.move("t2", report["id"], "2026-11-10", "", "21:00", True, msk(10, "16:55"))
        assert ok, "he named that piece"
        ok, _ = await planner.move("t2", bank["id"], "2026-11-10", "", "00:30", False, msk(10, "17:05"))
        assert ok
        got = {b["fields"]["task"]: (b["fields"]["start"], b["fields"]["minutes"]) for b in await blocks(world)}
        assert got == {report["id"]: (iso(msk(10, "21:00")), 60), bank["id"]: (iso(msk(11, "00:30")), 30)}, \
            "00:30 is the window's own, after midnight; the bank had no piece: a new one"
        ok, said = await planner.move("t2", bank["id"], "2026-11-10", "noon", "", False, msk(10, "17:05"))
        assert not ok and "part" in said
        ok, said = await planner.move("t2", "01AAAAAAAAAAAAAAAAAAAAAAAA", "2026-11-10", "evening", "", False,
                                      msk(10, "17:05"))
        assert not ok
        await planner.turn_done("t2")
        assert len(told) == 1 and told[0].count("→") == 1 and "Call the bank: Wed 11 Nov 00:30, 30 min" in told[0]
    faced(tmp_path, run)


def test_a_task_with_no_later_than_is_laid_before_it_and_what_does_not_fit_is_said_short(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        homework = await add(world, title="English homework", minutes=120, by=iso(msk(10, "19:00")))
        said = await planner.plan(msk(10, "17:00"))
        [laid] = await blocks(world)
        assert (hm(laid["fields"]["start"]), laid["fields"]["minutes"]) == ("17:00", 90), "one piece, before the lesson"
        assert laid["fields"]["why"] == "due today", "its day is the day of its «no later than»"
        assert "English homework" in said and "30" in said, said
        [day] = world.platform.plan.plan_state()["plans"].values()
        assert day["short"] == [{"task": homework["id"], "title": "English homework", "minutes": 30}]
        ok, text = await planner.estimate("t3", homework["id"], None, "", msk(10, "17:05"), "2026-11-12T18:00")
        assert ok and "12 Nov 18:00" in text, text
        task = await world.platform.collections.get("tasks", homework["id"])
        assert task["fields"]["by"] == iso(msk(12, "18:00"))
        assert not (await planner.estimate("t3", homework["id"], None, "", msk(10, "17:05"), "Thursday"))[0]
    faced(tmp_path, run)


def test_plan_on_a_person_s_row_makes_its_task_and_lays_it_in_one_press(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        made: list[tuple] = []

        async def make(token, ref, now):
            made.append((token, ref))
            return (await add(world, title="reply: is a transfer needed", ask="letter:0123456789abcdef"))["id"] \
                if token == "a" * 32 else None
        planner.tasks.make = make
        said = await planner.press({"action": "place", "day": "2026-11-11", "t": "a" * 32, "at": msk(10, "17:00")},
                                   msk(10, "17:00"))
        assert "11 Nov" in said, said
        [laid] = await blocks(world)
        assert hm(laid["fields"]["start"]) == "10:00" and laid["fields"]["start"].startswith("2026-11-11")
        said = await planner.press({"action": "place", "day": "2026-11-11", "t": "b" * 32, "at": msk(10, "17:00")},
                                   msk(10, "17:00"))
        assert said
        assert made == [("a" * 32, ""), ("b" * 32, "")]
    faced(tmp_path, run)


def test_the_app_and_its_mock_press_what_the_platform_takes():
    """The face's contract (face/src/lib/contract.ts `PRESS_FIELDS`) names the presses the app makes and the mock
    answers: the same as `PRESSES` here, in the same order — a press added on one side only fails here."""
    import re
    from retinue.plan import PRESSES
    source = (Path(__file__).parent.parent / "face/src/lib/contract.ts").read_text()
    body = re.search(r"export const PRESS_FIELDS\b[^=]*= \{(.*?)\n\};", source, re.S)
    assert body, "PRESS_FIELDS not found in contract.ts"
    assert tuple(re.findall(r"^\s+(\w+): \[", body.group(1), re.M)) == PRESSES


def test_a_place_press_names_a_task_a_person_s_row_or_a_reminder_and_nothing_else():
    from retinue.plan import Refusal, clean_press
    assert clean_press({"action": "place", "day": "2026-11-11", "task": "01AAAAAAAAAAAAAAAAAAAAAAAA", "t": "a" * 32}) == \
        {"action": "place", "day": "2026-11-11", "task": "01AAAAAAAAAAAAAAAAAAAAAAAA"}, "a task wins: its row is not read"
    assert clean_press({"action": "place", "day": "2026-11-11", "t": "a" * 32}) == \
        {"action": "place", "day": "2026-11-11", "t": "a" * 32}
    assert clean_press({"action": "place", "day": "2026-11-11", "ref": "reminder:7"})["ref"] == "reminder:7"
    for bad in ({"action": "place", "day": "2026-11-11", "t": "nope"}, {"action": "place", "day": "2026-11-11"},
                {"action": "place", "day": "2026-11-11", "ref": "letter:1"}):
        try:
            clean_press(bad)
        except Refusal:
            continue
        raise AssertionError(bad)


def test_a_piece_of_a_day_with_no_sign_of_him_is_not_counted_and_one_he_laid_himself_always_is(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, told = await setup(world, tmp_path)
        homework = await add(world, title="English homework", minutes=60)
        report = await add(world, title="Write the report", minutes=30)
        await planner.plan(msk(10, "17:00"))
        mine = next(b for b in await blocks(world) if b["fields"]["task"] == report["id"])
        await world.platform.collections.apply(SCREEN, "blocks", [
            {"op": "set", "id": mine["id"], "version": mine["version"], "fields": {"locked": True}}], world.clock.now)
        planner.sign(msk(9, "12:00"))  # the day before: not this window's
        await planner.cycle(msk(11, "10:05"))
        assert [b["fields"]["task"] for b in await blocks(world, "passed")] == [report["id"]], "his own piece counts"
        [skipped] = await blocks(world, "skipped")
        assert skipped["fields"]["task"] == homework["id"] and skipped["fields"]["why"]
        assert len(told) == 1 and "English homework (Tue 10 Nov 17:30)" in told[0]
        said = await planner.plan(msk(11, "10:05"))
        assert "English homework" in said, "its hour is laid again"
    faced(tmp_path, run)


def test_a_record_he_changed_on_his_screen_that_day_is_a_sign_too(tmp_path):
    async def run(world):
        world.clock.now = msk(10, "18:00")
        planner, _ = await setup(world, tmp_path)
        await add(world, title="English homework", minutes=60)  # added on his screen at 18:00: he was here
        await planner.plan(msk(10, "17:00"))
        await planner.cycle(msk(11, "10:05"))
        assert len(await blocks(world, "passed")) == 1
        assert await planner._present(msk(11, "10:00"), msk(12, "10:00")) is False
    faced(tmp_path, run)


def test_the_window_he_sets_in_the_app_is_kept_across_a_restart_and_the_next_plan_lays_into_it(tmp_path):
    from retinue.plan import Refusal, clean_press

    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        await add(world, title="Pay the bill", minutes=30)
        said = await planner.press({"action": "hours", "opens": "20:00", "closes": "02:00", "zone": "Europe/Berlin",
                                    "at": msk(10, "17:00")}, msk(10, "17:00"))
        assert "Europe/Berlin" in said and "20:00–02:00" in said
        assert world.platform.plan.plan_state()["window"] == {"zone": "Europe/Berlin", "opens": "20:00", "closes": "02:00"}
        again = Planner(planner.core, planner.tasks, Hours("UTC"))  # the Router restarted: his window, not the default
        assert again.hours == Hours("Europe/Berlin", wall(20, 0), wall(2, 0))
        await again.plan(msk(10, "17:00"))
        [block] = await blocks(world)
        assert "20:00" <= hm(block["fields"]["start"]) < "23:00", "inside his evening window, not at 17:00"
        refused = await again.press({"action": "hours", "opens": "09:00", "closes": "17:00", "zone": "Mars/Olympus",
                                     "at": msk(10, "17:01")}, msk(10, "17:01"))
        assert refused.startswith("Not kept") and again.hours.opens == wall(20, 0)
    faced(tmp_path, run)
    assert clean_press({"action": "hours", "opens": "10:00", "closes": "01:00", "zone": "America/Argentina/Salta",
                        "day": "2026-11-11"}) == {"action": "hours", "opens": "10:00", "closes": "01:00",
                                                  "zone": "America/Argentina/Salta"}
    for bad in ({"action": "hours", "opens": "10:00", "closes": "01:00"},
                {"action": "hours", "opens": "10", "closes": "01:00", "zone": "Europe/Berlin"},
                {"action": "hours", "opens": "10:00", "closes": "01:00", "zone": "../../etc/passwd"}):
        try:
            clean_press(bad)
        except Refusal:
            continue
        raise AssertionError(bad)


def test_a_meal_keeps_its_place_from_every_task_and_lunch_said_now_frees_it_and_moves_what_it_crosses(tmp_path):
    from retinue.plan_layout import Activity

    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        assert await planner.press(
            {"action": "activities", "activities": [{"title": "Lunch", "minutes": 45, "from": "13:00", "to": "16:00"},
                                                    {"title": "Rest", "minutes": 60}], "at": msk(10, "09:00")},
            msk(10, "09:00"))
        assert planner.activities == (Activity("Lunch", 45, wall(13, 0), wall(16, 0)), Activity("Rest", 60))
        for n in range(4):
            await add(world, title=f"Task {n}", minutes=90)
        await planner.plan(msk(10, "12:00"))
        spans = [(moment(b["fields"]["start"]), moment(b["fields"]["start"]) + b["fields"]["minutes"] * 60)
                 for b in await blocks(world)]
        assert spans and all(b <= msk(10, "13:00") or a >= msk(10, "13:45") for a, b in spans), "lunch kept free"
        state = world.platform.plan.plan_state()
        assert {"title": "Lunch", "from": msk(10, "13:00"), "to": msk(10, "13:45")} in state["meals"]
        assert state["activities"][1] == {"title": "Rest", "minutes": 60}

        # At 12:30 he goes for lunch: a hold «Lunch»; the meal's place that day is let go; what it crosses moves.
        said = await planner.press({"action": "now", "activity": "lunch", "at": msk(10, "12:30")}, msk(10, "12:30"))
        assert said.startswith("Lunch till 13:15")
        assert not [m for m in world.platform.plan.plan_state()["meals"] if m["from"] == msk(10, "13:00")]
        later = [b for b in await blocks(world) if moment(b["fields"]["start"]) >= msk(10, "12:30")]
        assert all(moment(b["fields"]["start"]) >= msk(10, "13:15") for b in later)

        # «Rest» from 13:00 cuts the lunch running then; × ends the rest and keeps tomorrow's airport.
        await planner.press({"action": "now", "activity": "Rest", "at": msk(10, "13:00")}, msk(10, "13:00"))
        ok, text = await planner.hold_said("Travel", 90, "2026-11-11T19:00", msk(10, "13:00"))
        assert ok and text.startswith("Travel Wed 11 Nov 19:00–20:30")
        assert [(h["label"], h["to"]) for h in planner.held(msk(10, "13:00"))] == [
            ("Lunch", msk(10, "13:00")), ("Rest", msk(10, "14:00")), ("Travel", msk(11, "20:30"))], \
            "the rest cut lunch at 13:00; the lunch is kept, eaten"
        assert not [m for m in planner.meals(msk(10, "15:00")) if m["title"] == "Lunch" and msk(10, "00:00") <= m["from"] < msk(11, "00:00")], \
            "lunch eaten 12:30–13:00 stays eaten when the hold is over: no place comes back after the rest"
        assert await planner.free(msk(10, "13:10"))
        assert await planner.free(msk(10, "13:11"))
        assert [h["label"] for h in planner.holds(msk(10, "13:10"))] == ["Travel"]
        assert (await planner.hold_said("Travel", 30, "2026-11-01T10:00", msk(10, "13:10")))[0] is False
        assert (await planner.hold_said("Travel", 0, "", msk(10, "13:10")))[0] is False
        assert (await planner.hold_said("Travel", 30, "2026-11-11", msk(10, "13:10")))[0] is False, "a date is no time"
        ok, text = await planner.hold_said("Dinner", 45, "2026-11-11T18:50", msk(10, "13:10"))
        assert ok, "a hold starting inside the new one is named, not lost quietly"
        assert (await planner.press({"action": "activities", "activities": [{"title": "Lunch", "minutes": 45},
                                                                             {"title": "lunch", "minutes": 30}],
                                     "at": msk(10, "13:10")}, msk(10, "13:10"))).startswith("Not kept")
    faced(tmp_path, run)


def test_not_working_today_moves_the_rest_of_the_day_on_keeps_his_own_and_one_undo_takes_it_all_back(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        for n in range(3):
            await add(world, title=f"Task {n}", minutes=60)
        await planner.plan(msk(10, "13:50"))
        laid = await blocks(world)
        assert len(laid) == 3 and all(moment(b["fields"]["start"]) >= msk(10, "14:00") for b in laid)
        his = laid[-1]
        await world.platform.collections.apply(SCREEN, "blocks", [
            {"op": "set", "id": his["id"], "version": his["version"], "fields": {"locked": True}}], world.clock.now)
        before = {b["id"]: b["fields"]["start"] for b in await blocks(world)}

        said = await planner.press({"action": "off", "at": msk(10, "14:00")}, msk(10, "14:00"))
        assert "01:00" in said
        after = {b["id"]: b for b in await blocks(world)}
        moved = [b for i, b in after.items() if i != his["id"]]
        assert all(msk(11, "10:00") <= moment(b["fields"]["start"]) < msk(12, "01:00") for b in moved), "on to Wed"
        assert after[his["id"]]["fields"]["start"] == his["fields"]["start"], "his own stays"
        [hold] = planner.holds(msk(10, "14:00"))
        assert (hold["label"], hold["from"], hold["to"]) == ("Not working", msk(10, "14:00"), msk(11, "01:00"))
        assert world.platform.plan.plan_state()["recent"][0]["by"] == "owner", "«Undo plan» in the app is this one"

        plan_id = world.platform.plan.plan_state()["recent"][0]["id"]
        assert (await planner.undo(plan_id, msk(10, "14:05"))).startswith("Taken back: 2 blocks")
        assert {b["id"]: b["fields"]["start"] for b in await blocks(world)} == before
        assert planner.holds(msk(10, "14:05")) == [], "the time is his again"
    faced(tmp_path, run)


def test_not_working_today_cuts_the_running_block_keeps_his_holds_names_what_has_no_room_and_undo_puts_all_back(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, _ = await setup(world, tmp_path)
        await add(world, title="Report", minutes=60, due="2026-11-10")
        await add(world, title="Bill", minutes=30)
        await planner.plan(msk(10, "13:50"))
        before = {b["id"]: (b["fields"]["start"], b["fields"]["minutes"]) for b in await blocks(world)}
        await planner.hold("Travel", msk(10, "18:00"), msk(10, "18:30"), msk(10, "14:20"))
        week = [{"start": msk(d, "09:00"), "end": msk(d + 1, "02:00"), "title": "Away", "all_day": False, "busy": True}
                for d in range(11, 18)]
        planner.tasks.events = planner.tasks.events + week  # no room in the next seven windows
        said = await planner.press({"action": "off", "at": msk(10, "14:20")}, msk(10, "14:20"))
        assert said, said
        assert "01:00" in await planner.press({"action": "off", "at": msk(10, "14:21")}, msk(10, "14:21")), \
            "a second press is the first"
        [running] = await blocks(world)
        assert running["fields"]["minutes"] == 20, "the report begun at 14:00 is cut at 14:20"
        assert [h["label"] for h in planner.holds(msk(10, "14:21"))] == ["Not working", "Travel"], "his hold stays"

        off = next(p for p in planner.plans().values() if p.get("off"))
        assert (await planner.undo(off["id"], msk(10, "14:25"))).startswith("Taken back: 2 blocks")
        assert {b["id"]: (b["fields"]["start"], b["fields"]["minutes"]) for b in await blocks(world)} == before
        assert [h["label"] for h in planner.holds(msk(10, "14:25"))] == ["Travel"]
    faced(tmp_path, run)
