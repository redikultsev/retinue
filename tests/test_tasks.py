"""The owner's tasks (docs/collections.md#tasks): the collection as the terminal's files define it, the Router's
«Make it a task» and its reconciliation of tasks with the rows of «Waiting for you», the reminders in the digest, and
what «Today»'s Tasks, Day and Week read (`GET /api/plan`) — the busy window the Router pushes among it."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from retinue import collector, feed, plan, waiting
from retinue.collections import Rejected, Writer
from retinue.core import _closes
from retinue.mail import MailStore
from retinue.outbox import OutboxStore
from retinue.protocol import Store
from retinue.scheduler import Scheduler
from retinue.tasks import RULE, Tasks, occurrence
from test_collections_assistant import platform_of
from test_face import faced
from test_platformd import ROUTER, short_wait  # noqa: F401 — the fixture is autouse
from test_waiting import asking, put

DEPLOY = Path(__file__).parent.parent / "deploy" / "collections"
DEFINE = Writer("owner", "define")
SCREEN = Writer("owner", "screen")


async def define(world) -> None:
    """The collection as the server's terminal makes it, from the files beside the deploy."""
    spec = json.loads((DEPLOY / "tasks.json").read_text())
    store = world.platform.collections
    await store.define(DEFINE, "tasks", spec["space"], spec["form"], world.clock.now, views=spec["views"])
    await store.rights(DEFINE, "tasks", json.loads((DEPLOY / "tasks.rights.json").read_text()), world.clock.now)


def router(world, tmp_path) -> SimpleNamespace:
    """The Router's side, as `tasks.Tasks` and `waiting` reach it: its kv, the mail's asks, the reminders, and the
    platform's `/owner/*` through its own app."""
    store = Store(str(tmp_path / "router.sqlite"))
    return SimpleNamespace(store=store, tz="UTC", platform=platform_of(world), jobs=Scheduler(store.db, "UTC"),
                           mail=SimpleNamespace(store=MailStore(store.db), changed=None, collector=None),
                           outbox=SimpleNamespace(store=OutboxStore(store.db)), proactive=None)


async def tasks_of(world) -> list[dict]:
    return (await world.platform.collections.query("tasks"))["rows"]


def test_the_terminals_files_make_a_collection_the_router_and_the_assistant_write_as_their_rights_say(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        await define(world)
        store = world.platform.collections
        schema = await store.schema("tasks")
        assert schema["form"]["fields"]["slot"] == {"type": "instant", "index": True}
        added = await store.apply(Writer("assistant", "assistant"), "tasks", [
            {"op": "add", "fields": {"title": "Ответить Анне Петровой", "state": "open", "goal": "work"}}],
            world.clock.now)
        [task] = added["records"]
        with pytest.raises(Rejected):  # her rights give no slot: a plan is a rule's
            await store.apply(Writer("assistant", "assistant"), "tasks", [
                {"op": "set", "id": task["id"], "version": 1, "fields": {"slot": "2026-11-04T17:00:00Z"}}],
                world.clock.now)
        moved = await store.apply(Writer("code:plan", "code"), "tasks", [
            {"op": "set", "id": task["id"], "version": 1, "fields": {"slot": "2026-11-04T17:00Z"}}], world.clock.now)
        assert moved["records"][0]["fields"]["slot"] == "2026-11-04T17:00:00Z", "kept with its seconds, to sort"
        said = await world.owner("collections/apply", {"actor": "assistant", "via": "code", "foreign": [],
                                                       "call": "x", "arguments": {"collection": "tasks", "ops": []}})
        assert not said["ok"] and "a rule's own" in said["text"], "via code is never hers"
        seen = await world.owner("collections/query", {"actor": RULE, "via": "code", "foreign": [], "call": "q",
                                                       "arguments": {"collection": "tasks"}})
        assert seen["answer"]["rows"][0]["id"] == task["id"], "the Router's code reads rows, not words"
        hers = await world.owner("collections/query", {"actor": "assistant", "via": "assistant", "foreign": [],
                                                       "call": "q2", "arguments": {"collection": "tasks"}})
        assert "answer" not in hers, "her turn reads the framed words alone"
    faced(tmp_path, run)


def test_a_moment_is_kept_in_utc_and_matched_between_two(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        await define(world)
        store = world.platform.collections
        for bad in ("2026-11-04 17:00", "2026-11-04T17:00:00+01:00", "2026-13-04T17:00Z", "2026-11-04T25:00Z", 17):
            with pytest.raises(Rejected, match="a moment in UTC"):
                await store.apply(SCREEN, "tasks", [{"op": "add", "fields": {"title": "x", "state": "open",
                                                                             "slot": bad}}], world.clock.now)
        for n, slot in enumerate(("2026-11-04T09:00:00Z", "2026-11-04T23:30Z", "2026-11-05T00:15:00Z")):
            await store.apply(SCREEN, "tasks", [{"op": "add", "fields": {"title": f"t{n}", "state": "open",
                                                                         "slot": slot}}], world.clock.now)
        found = await store.query("tasks", [{"field": "slot", "op": "between",
                                             "value": ["2026-11-04T12:00Z", "2026-11-05T00:15:00Z"]}])
        assert [r["fields"]["title"] for r in found["rows"]] == ["t1", "t2"], "across midnight, by the moment"
        with pytest.raises(Rejected, match="two moments"):
            await store.query("tasks", [{"field": "slot", "op": "between", "value": ["2026-11-04", "2026-11-05"]}])
    faced(tmp_path, run)


def test_make_it_a_task_names_its_row_and_a_second_press_makes_none_more(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("pay", ts=world.clock.now - 3600, at=world.clock.now - 3600))
        [row] = waiting.digest(core, world.clock.now)
        side = Tasks(core)
        assert await side.press(row["t"], "", world.clock.now)
        assert await side.press(row["t"], "", world.clock.now + 120), "known — its task is there already"
        [task] = await tasks_of(world)
        assert task["fields"]["ask"] == row["id"] and task["fields"]["state"] == "open"
        assert task["fields"]["title"] == row["title"], "the row's own masked words"
        assert not await side.press("0" * 32, "", world.clock.now), "a token the Router never showed"
        [change] = await world.platform.collections.history("tasks", task["id"])
        assert (change["actor"], change["via"]) == (RULE, "code")
    faced(tmp_path, run)


def test_closing_either_closes_the_other_and_undo_on_either_stands(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("reply", ts=now - 3600, at=now - 3600))
        [row] = waiting.digest(core, now)
        side = Tasks(core)
        await side.press(row["t"], "", now)
        assert await side.reconcile(now) == 0, "both open: nothing to carry"
        [task] = await tasks_of(world)
        await world.platform.collections.apply(SCREEN, "tasks", [
            {"op": "set", "id": task["id"], "version": task["version"], "fields": {"state": "done"}}], now)
        assert await side.reconcile(now + 30) == 1
        assert core.mail.store.asks(now + 30) == [], "«Done» on the task closed the letter's row"
        assert await side.reconcile(now + 60) == 0, "and the next look leaves them be"

        assert waiting.press(core, row["t"], "open", now + 90), "«Undo» on the row in the app"
        assert await side.reconcile(now + 120) == 1
        [task] = await tasks_of(world)
        assert task["fields"]["state"] == "open", "the row opened again opens its task"

        assert waiting.press(core, row["t"], "done", now + 150)
        assert await side.reconcile(now + 180) == 1
        [task] = await tasks_of(world)
        assert task["fields"]["state"] == "done", "«Done» on the row closes the task"
        changes = await world.owner("collections/changes", {"since": 0, "exclude_actors": ["code:plan",
                                                                                           "code:calsync"]})
        assert {c["actor"] for c in changes["changes"]} == {RULE}, "the evening list names the rule's changes"
        assert (await world.post("/owner/collections/changes", {"since": 0, "exclude_actors": ["assistant"]}))[0] \
            == 400, "only rules are left out"
    faced(tmp_path, run)


def test_the_first_look_lets_what_is_closed_win(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("decide", ts=now - 3600, at=now - 3600),
            asking("pay", message_id="<m2@example.org>", ts=now - 3600, at=now - 3600))
        decide, pay = waiting.digest(core, now)
        store = world.platform.collections
        await store.apply(SCREEN, "tasks", [
            {"op": "add", "fields": {"title": "decide", "state": "done", "ask": decide["id"]}},
            {"op": "add", "fields": {"title": "pay", "state": "open", "ask": pay["id"]}}], now)
        waiting.press(core, pay["t"], "dismissed", now)
        assert await Tasks(core).reconcile(now + 30) == 2
        assert core.mail.store.asks(now + 30) == [], "the done task closed its row"
        assert {r["fields"]["title"]: r["fields"]["state"] for r in await tasks_of(world)} == \
            {"decide": "done", "pay": "done"}, "the row he closed closed its task"
    faced(tmp_path, run)


def test_a_reminder_is_in_the_digest_off_the_queue_and_becomes_a_task_it_outlives(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 2 * 86400))
        ok, _ = core.jobs.add("Позвонить Анне Петровой +7 999 123-45-67", when, "", now)
        assert ok
        core.jobs.add("Далёкое", time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 30 * 86400)), "", now)
        [reminder] = [r for r in waiting.digest(core, now) if r["kind"] == "reminder"]
        assert "‹number›" in reminder["title"] and reminder["id"].startswith("reminder:"), "masked as hers are"
        items, _ = feed.clean({"items": [reminder]}, now)
        world.platform.feed.keep(items, "", now)
        assert world.platform.feed.rows(now + 3 * 86400) == [], "a reminder is no queue's"
        assert await Tasks(core).press("", reminder["id"], now)
        [task] = await tasks_of(world)
        assert task["fields"]["ask"] == reminder["id"] and task["fields"]["due"] == reminder["when"]
        assert core.jobs.get(int(reminder["id"].split(":")[1])).status == "active", "the reminder stays"
        assert not await Tasks(core).press("", "reminder:999", now)
        with pytest.raises(feed.Refused):
            feed.clean({"items": [{**reminder, "id": "letter:1234"}]}, now)
    faced(tmp_path, run)


def test_today_reads_its_tasks_its_window_and_the_digests_asks_at_once(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await world.login()
        assert (await world.get("/api/plan"))["tasks"]["missing"], "no collection yet: said, not an error"
        await define(world)
        store = world.platform.collections
        slot = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))
        await store.apply(SCREEN, "tasks", [
            {"op": "add", "fields": {"title": "open", "state": "open"}},
            {"op": "add", "fields": {"title": "done today", "state": "done", "slot": slot}},
            {"op": "add", "fields": {"title": "done long ago", "state": "done"}}], now)
        event = {"start": now + 7200, "end": now + 9000, "title": "Созвон с Acme", "all_day": False, "busy": True}
        assert (await world.post("/owner/calendar", {"events": [event], "at": now}))[0] == 200
        status, said = await world.post("/owner/calendar", {"events": [{**event, "end": now}], "at": now})
        assert status == 400, "an end before its start refuses the whole window"
        got = await world.get("/api/plan")
        assert sorted(r["fields"]["title"] for r in got["tasks"]["rows"]) == ["done today", "open"]
        assert [g["value"] for g in got["tasks"]["goals"]] == ["work", "home", "health", "study"]
        assert got["calendar"]["events"] == [event] and not got["calendar"]["stale"]
        world.clock.now = now + plan.CALENDAR_STALE_S + 1
        assert (await world.get("/api/plan"))["calendar"]["stale"], "an old window is said to be old"
    faced(tmp_path, run)


def test_an_occurrence_keeps_him_busy_unless_free_declined_or_all_day():
    timed = {"status": "confirmed", "start": {"dateTime": "2026-11-04T10:00:00+01:00"},
             "end": {"dateTime": "2026-11-04T11:00:00+01:00"}, "summary": "Daily", "transparency": "opaque"}
    shown = occurrence(timed, "Europe/Berlin")
    assert shown["busy"] and shown["end"] - shown["start"] == 3600 and shown["title"] == "Daily"
    assert not occurrence({**timed, "transparency": "transparent"}, "UTC")["busy"]
    assert not occurrence({**timed, "answer": "declined"}, "UTC")["busy"]
    assert occurrence({**timed, "status": "cancelled"}, "UTC") is None
    whole = occurrence({"start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}, "summary": "Trip"}, "UTC")
    assert whole["all_day"] and not whole["busy"] and whole["end"] - whole["start"] == 86400


def test_an_event_keeps_what_the_busy_window_and_the_tasks_calendar_need():
    kept = collector.brief({"id": "e1", "start": {}, "end": {}, "transparency": "transparent",
                            "eventType": "focusTime", "extendedProperties": {"private": {"retinue_task": "01ABC"}}})
    assert (kept["transparency"], kept["eventType"], kept["private"]) == \
        ("transparent", "focusTime", {"retinue_task": "01ABC"})
    plain = collector.brief({"id": "e2", "start": {}, "end": {}})
    assert (plain["transparency"], plain["eventType"], plain["private"]) == ("opaque", "default", {})


def test_she_closes_a_task_only_by_an_add_or_set_that_says_so():
    assert _closes([{"op": "set", "id": "x", "version": 1, "fields": {"state": "done"}}])
    assert _closes([{"op": "add", "fields": {"title": "x", "state": "dropped"}}])
    assert not _closes([{"op": "set", "id": "x", "version": 1, "fields": {"title": "done"}}])
    assert not _closes("done") and not _closes([{"op": "delete", "id": "x", "version": 1}])
