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
        added = await store.apply(Writer("assistant", "assistant"), "tasks", [
            {"op": "add", "fields": {"title": "Ответить Анне Петровой", "state": "open", "goal": "work"}}],
            world.clock.now)
        [task] = added["records"]
        with pytest.raises(Rejected):  # a task has no time of its own: its pieces are `blocks`, a rule's
            await store.apply(Writer("code:plan", "code"), "tasks", [
                {"op": "set", "id": task["id"], "version": 1, "fields": {"slot": "2026-11-04T17:00:00Z"}}],
                world.clock.now)
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


def test_a_task_is_filed_under_a_project_and_moved(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        store = world.platform.collections
        spec = json.loads((DEPLOY / "tasks.json").read_text())
        before = {**spec["form"], "fields": {k: v for k, v in spec["form"]["fields"].items() if k != "project"}}
        await store.define(DEFINE, "tasks", spec["space"], before, now)
        [old] = (await store.apply(SCREEN, "tasks", [{"op": "add", "fields": {
            "title": "Renew the passport", "state": "open", "goal": "home"}}], now))["records"]
        await define(world)  # the form grows by `project`: the task made before it is not rewritten
        [kept] = (await store.query("tasks"))["rows"]
        assert (kept["fields"], kept["version"]) == (old["fields"], 1), "it keeps its goal and has no project"

        said = await world.owner("collections/apply", {
            "actor": "assistant", "via": "assistant", "foreign": [], "call": "t1", "arguments": {
                "collection": "tasks", "ops": [{"op": "add", "fields": {
                    "title": "Send Acme the draft", "state": "open", "project": "work/acme"}}]}})
        assert said["ok"], said
        filed = await store.query("tasks", [{"field": "project", "op": "eq", "value": "work/acme"}])
        [task] = filed["rows"]
        assert task["fields"]["title"] == "Send Acme the draft", "her tool files it under the project"

        said = await world.owner("collections/apply", {
            "actor": "assistant", "via": "assistant", "foreign": [], "call": "t2", "arguments": {
                "collection": "tasks", "ops": [{"op": "set", "id": task["id"], "version": task["version"],
                                                "fields": {"project": "work/acme/site"}}]}})
        assert said["ok"], said
        [moved] = (await store.query("tasks", [{"field": "project", "op": "eq", "value": "work/acme/site"}]))["rows"]
        assert moved["id"] == task["id"] and moved["version"] == 2, "moved, the same task"
        assert not (await store.query("tasks", [{"field": "project", "op": "eq", "value": "work/acme"}]))["rows"]

        session = Writer("session:s1", "session")
        await store.apply(session, "tasks", [{"op": "set", "id": old["id"], "version": 1,
                                              "fields": {"project": "home/papers"}}], now)
        [moved] = (await store.query("tasks", [{"field": "project", "op": "eq", "value": "home/papers"}]))["rows"]
        assert moved["fields"]["goal"] == "home", "moved by a session: its goal stays"
        with pytest.raises(Rejected):  # the Router's rule takes in rows; it never files or moves a task
            await store.apply(Writer(RULE, "code"), "tasks", [{"op": "set", "id": old["id"], "version": 2,
                                                               "fields": {"project": "work"}}], now)
    faced(tmp_path, run)


def test_a_moment_is_kept_in_utc_and_matched_between_two(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        await define(world)
        store = world.platform.collections
        spec = json.loads((DEPLOY / "blocks.json").read_text())
        await store.define(DEFINE, "blocks", spec["space"], spec["form"], world.clock.now, views=spec["views"])
        [task] = (await store.apply(SCREEN, "tasks", [{"op": "add", "fields": {"title": "x", "state": "open"}}],
                                    world.clock.now))["records"]
        piece = {"task": task["id"], "minutes": 30, "state": "planned"}
        for bad in ("2026-11-04 17:00", "2026-11-04T17:00:00+01:00", "2026-13-04T17:00Z", "2026-11-04T25:00Z", 17):
            with pytest.raises(Rejected, match="a moment in UTC"):
                await store.apply(SCREEN, "blocks", [{"op": "add", "fields": {**piece, "start": bad}}], world.clock.now)
        for start in ("2026-11-04T09:00:00Z", "2026-11-04T23:30Z", "2026-11-05T00:15:00Z"):
            await store.apply(SCREEN, "blocks", [{"op": "add", "fields": {**piece, "start": start}}], world.clock.now)
        found = await store.query("blocks", [{"field": "start", "op": "between",
                                              "value": ["2026-11-04T12:00Z", "2026-11-05T00:15:00Z"]}])
        assert [r["fields"]["start"] for r in found["rows"]] == ["2026-11-04T23:30:00Z", "2026-11-05T00:15:00Z"], \
            "kept with its seconds, matched across midnight by the moment"
        with pytest.raises(Rejected, match="two moments"):
            await store.query("blocks", [{"field": "start", "op": "between", "value": ["2026-11-04", "2026-11-05"]}])
    faced(tmp_path, run)


def test_a_row_s_task_names_its_row_and_a_second_plan_makes_none_more(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("pay", ts=world.clock.now - 3600, at=world.clock.now - 3600))
        [row] = waiting.digest(core, world.clock.now)
        side = Tasks(core)
        made = await side.make(row["t"], "", world.clock.now)
        assert made and await side.make(row["t"], "", world.clock.now + 120) == made, "its task is there already"
        [task] = await tasks_of(world)
        assert task["id"] == made
        assert task["fields"]["ask"] == row["id"] and task["fields"]["state"] == "open"
        assert task["fields"]["title"] == row["title"], "the row's own masked words"
        assert await side.make("0" * 32, "", world.clock.now) is None, "a token the Router never showed"
        [change] = await world.platform.collections.history("tasks", task["id"])
        assert (change["actor"], change["via"]) == (RULE, "code")
    faced(tmp_path, run)


def test_a_row_s_task_carries_the_letter_s_deadline_a_day_as_due_a_time_as_no_later_than(tmp_path):
    from datetime import datetime, timezone

    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        ahead = datetime.fromtimestamp(now + 5 * 86400, timezone.utc)
        day, moment = ahead.strftime("%Y-%m-%d"), ahead.strftime("%Y-%m-%dT18:00")
        letters = [("<d1@example.org>", "anna@example.org", day), ("<d2@example.org>", "boris@example.org", moment),
                   ("<d3@example.org>", "vera@example.org", "next Friday")]
        for message_id, sender, due in letters:
            item = asking("pay", sender=sender, message_id=message_id, ts=now - 3600, at=now - 3600)
            item["fields"]["card"] = {**item["fields"]["card"], "due_now": due}
            put(core.mail.store, item)
        side = Tasks(core)
        for row in waiting.digest(core, now):
            assert await side.make(row["t"], "", now)
        fields = sorted((t["fields"] for t in await tasks_of(world)), key=lambda f: (f.get("due") or "~", f.get("by") or ""))
        assert len(fields) == 3
        assert fields[0]["due"] == day and "by" not in fields[0], "a day: due"
        assert fields[1]["due"] == day and fields[1]["by"] == f"{day}T18:00:00Z", "a time too: by, in his zone (UTC here)"
        assert "due" not in fields[2] and "by" not in fields[2], "a deadline she could not date stays words"
    faced(tmp_path, run)


def test_a_deadline_s_time_is_his_zone_s_and_the_task_made_before_it_was_dated_takes_it(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        core.tz = "Asia/Bangkok"  # seven hours ahead of UTC
        put(core.mail.store, asking("pay", ts=now - 3600, at=now - 3600))
        [row] = waiting.digest(core, now)
        side = Tasks(core)
        made = await side.make(row["t"], "", now)  # «Plan» before she dated the letter
        [item] = core.mail.store.asks(now)
        core.mail.store.set(item["seq"], now, card={**item["card"], "due_now": "2030-11-15T18:00"})
        assert await side.make(row["t"], "", now + 60) == made, "the same task"
        [task] = await tasks_of(world)
        assert (task["fields"]["due"], task["fields"]["by"]) == ("2030-11-15", "2030-11-15T11:00:00Z")
    faced(tmp_path, run)


def test_her_due_is_a_real_date_or_nothing():
    from retinue.mail import decisions, due_of
    assert due_of("2026-11-15") == "2026-11-15" and due_of(" 2026-11-15T09:30 ") == "2026-11-15T09:30"
    for off in ("2026-02-30", "15 Nov", "2026-11-15T25:00", "", None, "2026-11-15T09:30:00"):
        assert due_of(off) == "", off
    [one] = decisions(json.dumps({"items": [{"n": 1, "needs": "pay", "deadline": "by Friday", "due": "2026-11-13",
                                            "cost": "high", "new": True, "to_assistant": False, "text": "Pay"}]})).values()
    assert one.due == "2026-11-13"


def test_closing_either_closes_the_other_and_undo_on_either_stands(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("reply", ts=now - 3600, at=now - 3600))
        [row] = waiting.digest(core, now)
        side = Tasks(core)
        await side.make(row["t"], "", now)
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

        assert waiting.press(core, row["t"], "open", now + 190) and await side.reconcile(now + 200) == 1
        assert waiting.press(core, row["t"], "dismissed", now + 210)
        assert await side.reconcile(now + 240) == 1
        [task] = await tasks_of(world)
        assert task["fields"]["state"] == "dropped", "«Not a task» on the row drops the task"
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
            {"decide": "done", "pay": "dropped"}, "the row he said is not a task dropped its task"
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
        assert await Tasks(core).make("", reminder["id"], now)
        [task] = await tasks_of(world)
        assert task["fields"]["ask"] == reminder["id"] and task["fields"]["due"] == reminder["when"]
        assert core.jobs.get(int(reminder["id"].split(":")[1])).status == "active", "the reminder stays"
        assert await Tasks(core).make("", "reminder:999", now) is None
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
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        made = await store.apply(SCREEN, "tasks", [
            {"op": "add", "fields": {"title": "open", "state": "open"}},
            {"op": "add", "fields": {"title": "done today", "state": "done"}},
            {"op": "add", "fields": {"title": "done long ago", "state": "done"}}], now)
        spec = json.loads((DEPLOY / "blocks.json").read_text())
        await store.define(DEFINE, "blocks", spec["space"], spec["form"], now, views=spec["views"])
        await store.apply(SCREEN, "blocks", [{"op": "add", "fields": {
            "task": made["records"][1]["id"], "start": start, "minutes": 30, "state": "passed"}}], now)
        event = {"start": now + 7200, "end": now + 9000, "title": "Созвон с Acme", "all_day": False, "busy": True}
        assert (await world.post("/owner/calendar", {"events": [event], "at": now}))[0] == 200
        status, said = await world.post("/owner/calendar", {"events": [{**event, "end": now}], "at": now})
        assert status == 400, "an end before its start refuses the whole window"
        got = await world.get("/api/plan")
        assert sorted(r["fields"]["title"] for r in got["tasks"]["rows"]) == ["done today", "open"], \
            "the open ones, and a closed one whose passed piece is on its day"
        assert [b["fields"]["state"] for b in got["tasks"]["blocks"]] == ["passed"]
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


def test_a_task_dropped_says_not_a_task_to_its_row_and_a_row_that_ran_out_leaves_its_task(tmp_path):
    async def run(world):
        now = world.clock.now = time.time()
        await define(world)
        core = router(world, tmp_path)
        put(core.mail.store, asking("reply", ts=now - 3600, at=now - 3600))
        [row] = waiting.digest(core, now)
        side = Tasks(core)
        await side.make(row["t"], "", now)
        await side.reconcile(now)
        [task] = await tasks_of(world)
        await world.platform.collections.apply(SCREEN, "tasks", [
            {"op": "set", "id": task["id"], "version": task["version"], "fields": {"state": "dropped"}}], now)
        assert await side.reconcile(now + 30) == 1
        [found] = waiting.rows(core, now + 30).values()
        assert (found["state"], found["how"]) == ("closed", "dismissed"), "dropped is «Not a task» on the row"

        waiting.press(core, row["t"], "open", now + 60)
        await side.reconcile(now + 90)
        [task] = await tasks_of(world)
        assert task["fields"]["state"] == "open"
        later = now + 10 * 86400  # every ask under the row ran out unmarked
        assert waiting.rows(core, later)[row["id"]]["state"] == "expired"
        assert await side.reconcile(later) == 0
        [task] = await tasks_of(world)
        assert task["fields"]["state"] == "open", "a row that ran out is no word of his: the task stays"
    faced(tmp_path, run)
