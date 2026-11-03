"""The plan's blocks in the calendar «Retinue», both ways (`gcal`), against the platform's own collections and the
calendar writer in front of a fake Google: a plan goes there, its echo is nothing, his move or deletion in Google
comes back to the block as his, his drag on the grid goes there, a closed task's blocks leave."""

from __future__ import annotations

import time

from aiohttp.test_utils import TestServer

from retinue import calwrite, google
from retinue.gcal import Gcal, Writer
from retinue.planner import iso, moment
from test_calwrite import FakeCalendar
from test_face import faced
from test_planner import SCREEN, add, blocks, hm, msk, setup
from test_platformd import ROUTER, short_wait  # noqa: F401 — the fixture is autouse


async def wired(world, tmp_path):
    planner, told = await setup(world, tmp_path)
    fake = FakeCalendar()
    account = google.Google(google.Key("owner@example.org", "calwrite", "rt", "cid", "cs"), fake.client())
    server = TestServer(calwrite.Service(tmp_path / "calendar.json", lambda: [account], "tok").app())
    await server.start_server()
    gcal = Gcal(planner.core, planner, Writer(str(server.make_url("")), "tok", "the calendar writer"),
                "https://app.example.com/today?tab=plan")
    return planner, gcal, fake, server, told


def events(fake, calendar=None) -> dict[str, dict]:
    if calendar is None:
        (calendar,) = fake.events
    return {calwrite.block_of(e["id"]): e for e in fake.events[calendar].values()}


async def sync(gcal, now):
    gcal._at = 0.0
    await gcal.cycle(now)  # the task it started


def test_a_plan_goes_to_google_and_comes_back_only_as_his_own_moves(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, gcal, fake, server, _ = await wired(world, tmp_path)
        try:
            bill = await add(world, title="Pay the bill", minutes=15)
            talk = await add(world, title="Record the talk", minutes=90, due="2026-11-13")
            await planner.plan(msk(10, "18:00"))
            now = msk(10, "18:00")
            await sync(gcal, now)
            laid = {b["id"]: b for b in await blocks(world)}
            there = events(fake)
            assert set(there) == set(laid) and {e["summary"] for e in there.values()} == {"Pay the bill",
                                                                                         "Record the talk"}
            assert all(e["transparency"] == "transparent" for e in there.values()), "a plan's block keeps no time busy"
            writes = len([c for c in fake.calls if c[0] in ("POST", "PUT", "DELETE")])
            await sync(gcal, now + 60)
            assert len([c for c in fake.calls if c[0] in ("POST", "PUT", "DELETE")]) == writes, "its echo: nothing"

            # He moves the talk an hour later and makes it shorter in Google.
            talk_block = next(b for b in laid.values() if b["fields"]["task"] == talk["id"])
            start = moment(talk_block["fields"]["start"]) + 3600
            fake.touch(there[talk_block["id"]], start={"dateTime": iso(start), "timeZone": "Europe/Berlin"},
                       end={"dateTime": iso(start + 45 * 60), "timeZone": "Europe/Berlin"})
            # And deletes the bill.
            bill_block = next(b for b in laid.values() if b["fields"]["task"] == bill["id"])
            fake.touch(there[bill_block["id"]], status="cancelled")
            await sync(gcal, now + 120)
            moved = next(b for b in await blocks(world) if b["id"] == talk_block["id"])
            assert hm(moved["fields"]["start"]) == hm(iso(start)) and moved["fields"]["minutes"] == 45
            assert moved["fields"]["locked"] is True, "his move in Google is his, as a drag on the grid"
            history = await world.platform.collections.history("blocks", moved["id"])
            assert [c["actor"] for c in history].count("code:calsync") == 1
            skipped = next(b for b in await blocks(world, "skipped") if b["id"] == bill_block["id"])
            assert skipped["fields"]["why"] == "removed in Google"
            assert planner._load("plan.signs", []), "a move in Google is a sign that he was here"
            assert events(fake)[talk_block["id"]]["transparency"] == "opaque", "locked now: busy in Google too"

            # He drags it on the grid: Google follows.
            later = start + 1800
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": moved["id"], "version": moved["version"], "fields": {"start": iso(later)}}],
                world.clock.now)
            await sync(gcal, now + 180)
            assert events(fake)[moved["id"]]["start"]["dateTime"] == iso(later)

            # The task is done: its block is skipped by the plan's look, and its event leaves.
            await world.platform.collections.apply(SCREEN, "tasks", [
                {"op": "set", "id": talk["id"], "version": talk["version"], "fields": {"state": "done"}}],
                world.clock.now)
            await planner.cycle(now + 200)
            await sync(gcal, now + 240)
            assert events(fake)[moved["id"]]["status"] == "cancelled"
        finally:
            await server.close()
    faced(tmp_path, run)


def test_a_deleted_calendar_is_said_once_and_made_again_on_his_press(tmp_path):
    async def run(world):
        world.clock.now = time.time()
        planner, gcal, fake, server, told = await wired(world, tmp_path)
        try:
            await add(world, title="Pay the bill", minutes=15)
            await planner.plan(msk(10, "18:00"))
            now = msk(10, "18:00")
            await sync(gcal, now)
            fake.calendars.clear()
            fake.events.clear()
            await sync(gcal, now + 60)
            await sync(gcal, now + 120)
            assert told.count("The calendar «Retinue» was deleted in Google: the plan no longer goes there.") == 1
            assert "made again" in await gcal.again_pressed()
            await sync(gcal, now + 180)
            assert len(fake.calendars) == 1 and len(events(fake)) == 1, "every block written into the new one"
        finally:
            await server.close()
    faced(tmp_path, run)


def test_lost_state_takes_nothing_off_and_a_block_back_in_the_plan_gets_its_event_back(tmp_path):
    """The writer's state lost: the calendar the Router knows is taken back, none taken for deleted, no second one.
    A block taken off and put back (Undo) has its own event back, not «removed in Google». A block Google refuses
    stops no other."""
    async def run(world):
        world.clock.now = time.time()
        planner, gcal, fake, server, _ = await wired(world, tmp_path)
        try:
            await add(world, title="Pay the bill", minutes=15)
            await add(world, title="Read the contract", minutes=30)
            await planner.plan(msk(10, "18:00"))
            now = msk(10, "18:00")
            await sync(gcal, now)
            (tmp_path / "calendar.json").unlink()
            await sync(gcal, now + 60)
            await sync(gcal, now + 120)
            assert len(fake.calendars) == 1 and len(await blocks(world)) == 2, "nothing skipped, no second calendar"
            assert len(events(fake)) == 2

            one, other = await blocks(world)
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": one["id"], "version": one["version"], "fields": {"state": "skipped"}}],
                world.clock.now)
            await sync(gcal, now + 180)
            calendar = list(fake.events)[-1]
            assert events(fake, calendar)[one["id"]]["status"] == "cancelled"
            [back] = [b for b in await blocks(world, "skipped") if b["id"] == one["id"]]
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": one["id"], "version": back["version"], "fields": {"state": "planned"}}],
                world.clock.now)
            await sync(gcal, now + 240)
            assert events(fake, calendar)[one["id"]]["status"] == "confirmed"
            assert {b["id"] for b in await blocks(world)} == {one["id"], other["id"]}, "not «removed in Google»"

            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": one["id"], "version": back["version"] + 1, "fields": {"minutes": 2000}}],
                world.clock.now)
            later = moment(other["fields"]["start"]) + 600
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": other["id"], "version": other["version"], "fields": {"start": iso(later)}}],
                world.clock.now)
            await sync(gcal, now + 300)
            assert events(fake, calendar)[other["id"]]["start"]["dateTime"] == iso(later), "the refused one stops nothing"
        finally:
            await server.close()
    faced(tmp_path, run)


def test_a_lost_answer_neither_locks_a_block_nor_stalls_the_sync(tmp_path):
    """The Router's own write whose answer was lost comes back from Google as a change: its time is the block's, so
    it is no move of his — no lock, no sign. His move carried, then the Router gone before it kept that: the same
    changes read again are the same writes, and the sync goes on."""
    from retinue.gcal import KEY

    async def run(world):
        world.clock.now = time.time()
        planner, gcal, fake, server, _ = await wired(world, tmp_path)
        try:
            await add(world, title="Pay the bill", minutes=15)
            await planner.plan(msk(10, "18:00"))
            now = msk(10, "18:00")
            await sync(gcal, now)
            [block] = await blocks(world)
            later = moment(block["fields"]["start"]) + 900
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": block["id"], "version": block["version"], "fields": {"start": iso(later)}}],
                world.clock.now)
            known = gcal._state()["events"][block["id"]]
            await gcal.writer.put({"block": block["id"], **{k: known[k] for k in ("title", "minutes", "busy")},
                                   "start": iso(later), "zone": "Europe/Berlin", "etag": known["etag"]})
            await sync(gcal, now + 60)  # the Router never saw that answer
            [after] = await blocks(world)
            assert not after["fields"].get("locked") and not planner._load("plan.signs", [])

            there = events(fake)[block["id"]]
            moved = later + 3600
            fake.touch(there, start={"dateTime": iso(moved)}, end={"dateTime": iso(moved + 900)})
            before = planner.core.store.get(KEY)
            await sync(gcal, now + 120)
            planner.core.store.set(KEY, before)  # the Router died before it kept the look
            await sync(gcal, now + 180)
            [carried] = await blocks(world)
            assert carried["fields"]["locked"] is True and hm(carried["fields"]["start"]) == hm(iso(moved))
            again = moved + 1800
            await world.platform.collections.apply(SCREEN, "blocks", [
                {"op": "set", "id": carried["id"], "version": carried["version"], "fields": {"start": iso(again)}}],
                world.clock.now)
            await sync(gcal, now + 240)
            assert events(fake)[block["id"]]["start"]["dateTime"] == iso(again), "not stalled"
        finally:
            await server.close()
    faced(tmp_path, run)
