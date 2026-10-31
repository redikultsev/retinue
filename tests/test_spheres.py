"""Spheres (`retinue/spheres.py`): a session's sphere, one main session per sphere held by SQLite, `/<sphere>` from
the router, forks that copy a parent's conversation, and what the owner has not seen yet — in the store, through the
router's calls and through the face, against the fake lanes of `test_platformd`."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from retinue import sessions
from retinue.config import Sphere
from test_face import FaceWorld
from test_platformd import (CALL, DAY, DEFERRED, World, decision, deferred_session, fixture, pre_tool_use,  # noqa: F401
                            short_wait)  # the fixture is autouse

SPHERES = [Sphere("travel", "Путешествия", "notes", "✈️"), Sphere("home", "Дом", "notes"),
           Sphere("work", "Код", "retinue")]
READS_CLI = json.loads(fixture("reads")[0])["session_id"]


class SphereWorld(FaceWorld):
    async def up(self) -> None:
        self.cfg.spheres = SPHERES
        await super().up()


def spheres_scenario(tmp_path, body):
    async def go():
        world = SphereWorld(tmp_path)
        await world.start()
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


async def finished(world: World, session: str, lane: str = "private") -> dict:
    """The session's last run ends its turn and its process: idle, with the CLI session of the fixture."""
    run = [r for r in world.lanes[lane].runs() if r["session"] == session][-1]
    world.lanes[lane].add(session, run["run"], *fixture("reads"))
    world.lanes[lane].exited(session, run["run"])
    await world.poll()
    return run


# --- the store ------------------------------------------------------------------------------------------------------

def test_a_sphere_has_at_most_one_main_session_and_sqlite_holds_it(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    a = store.new_session("notes", "private", "private", 1.0, sphere="travel")
    b = store.new_session("notes", "private", "private", 2.0, sphere="travel")
    loose = store.new_session("notes", "private", "private", 3.0)
    assert store.claim_main(a) and not store.claim_main(b), "the second claim loses"
    assert not store.claim_main(loose) and not store.set_main(loose, True), "no sphere, no main session"
    assert store.main_of("travel") == a
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE sessions SET main = 1 WHERE id = ?", (b,))
    store.db.rollback()
    assert store.set_main(b, True) and store.main_of("travel") == b, "marking one moves the mark"
    assert not store.face_session(a)["main"] and store.face_session(b)["main"]
    assert store.set_sphere(b, "home") and store.main_of("travel") is None and store.main_of("home") is None
    assert store.face_session(b)["sphere"] == "home"
    assert store.set_main(b, True) and store.set_main(b, False) and store.main_of("home") is None


def test_a_main_session_stays_listed_however_long_ago_it_closed(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    old = store.new_session("notes", "private", "private", 1.0, sphere="travel")
    gone = store.new_session("notes", "private", "private", 1.0, sphere="travel")
    for session in (old, gone):
        store.update_session(session, 1.0, state="closed")
    store.claim_main(old)
    assert [s["id"] for s in store.face_sessions(1.0 + 60 * 86400)] == [old]


def test_a_session_is_unread_after_a_summary_or_a_new_card_until_the_owner_looks(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    clock = [10.0]
    store.clock = lambda: clock[0]
    session = store.new_session("notes", "private", "private", 10.0)
    store.add_input("tg:1", session, "привет", 10.0)
    assert not store.face_session(session)["unread"], "his own message is nothing new"
    store.add_note("summary", session, {"head": "Result", "text": "готово", "at": 20.0})
    assert store.face_session(session)["unread"]
    rows = [json.loads(data) for kind, data in store.db.execute("SELECT kind, data FROM events") if kind == "session"]
    assert rows[-1]["unread"], "the face is told by the session's event"
    store.mark_seen(session, 30.0)
    assert not store.face_session(session)["unread"]
    approval = store.add_approval(session, "r1", "t1", "approval", "Bash", {"command": "x"}, "/work", "", "d", 40.0)
    assert not store.face_session(session)["unread"], "a pending call is no one's card yet"
    store.move(approval, ("pending",), "shown", 41.0, view={"head": ["x"]})
    assert store.face_session(session)["unread"]
    store.add_input("tg:2", session, "ответ", 50.0)
    assert not store.face_session(session)["unread"], "an answer in Telegram is a look"
    store.add_approval(session, "r1", "t2", "courier", "Bash", {"command": "git push"}, "/work", "", "", 60.0,
                       state="dropped", view={"head": ["Courier"]})
    assert store.face_session(session)["unread"], "a courier card is shown at once"


# --- the router's calls ---------------------------------------------------------------------------------------------

def test_a_sphere_command_writes_into_its_main_session_making_one_when_there_is_none(tmp_path):
    async def body(world: SphereWorld):
        assert (await world.owner("spheres", {}))["spheres"][0] == {"name": "travel", "title": "Путешествия",
                                                                   "icon": "✈️", "track": "notes"}
        empty = await world.owner("sphere", {"input_id": "tg:1", "sphere": "travel", "text": ""})
        assert "no main session" in empty["say"] and "session" not in empty
        first = await world.owner("sphere", {"input_id": "tg:2", "sphere": "travel", "text": "найди жильё в Зурбагане"})
        session = first["session"]
        assert first["say"].startswith(f"✈️ Путешествия · new main session {session}.")
        row = world.store.session(session)
        assert (row["track"], row["sphere"], row["main"]) == ("notes", "travel", 1)
        assert world.lanes["private"].runs()[-1]["prompt"] == "найди жильё в Зурбагане"
        await finished(world, session)
        status = await world.owner("sphere", {"input_id": "tg:3", "sphere": "travel", "text": ""})
        assert status["session"] == session
        assert status["say"].startswith(f"✈️ Путешествия · session {session} · ready\n")
        again = await world.owner("sphere", {"input_id": "tg:4", "sphere": "travel", "text": "а подешевле?"})
        assert again["session"] == session and again["say"] == f"✈️ Путешествия · session {session}: Accepted."
        assert world.lanes["private"].runs()[-1]["prompt"] == "а подешевле?"
        assert world.lanes["private"].runs()[-1]["cli_session"] == READS_CLI, "the main session goes on"
        retried = await world.owner("sphere", {"input_id": "tg:4", "sphere": "travel", "text": "а подешевле?"})
        assert retried["session"] == session and len(world.lanes["private"].runs()) == 2
        assert (await world.owner("sphere", {"input_id": "tg:5", "sphere": "mars", "text": "x"}))["ok"] is False
        status, _ = await world.post("/owner/sphere", {"input_id": "tg:6", "sphere": 1, "text": "x"})
        assert status == 400

    spheres_scenario(tmp_path, body)


def test_new_with_a_sphere_runs_on_its_track_and_a_side_question_keeps_the_sphere(tmp_path):
    async def body(world: SphereWorld):
        made = await world.owner("new", {"input_id": "tg:1", "track": "work", "text": "почини тест"})
        row = world.store.session(made["session"])
        assert (row["track"], row["sphere"], row["main"]) == ("retinue", "work", 0), "not main unless claimed"
        side = await world.platform.side("face:s1", made["session"], "зачем?")
        assert world.store.session(side["session"])["sphere"] == "work"
        wrong = await world.owner("new", {"input_id": "tg:2", "track": "nope", "text": "x"})
        assert wrong["say"] == "No such track: nope. There are: retinue, notes; spheres: travel, home, work"

    spheres_scenario(tmp_path, body)


# --- forks ----------------------------------------------------------------------------------------------------------

def test_a_fork_resumes_its_parent_with_fork_session_once_then_goes_on_as_itself(tmp_path):
    async def body(world: SphereWorld):
        parent = (await world.owner("sphere", {"input_id": "tg:1", "sphere": "travel", "text": "план поездки"}))[
            "session"]
        await finished(world, parent)
        answer = await world.platform.spheres.fork("face:f1", parent, "а если на поезде?")
        fork = answer["session"]
        row = world.store.session(fork)
        assert answer["ok"] and fork != parent
        assert (row["fork_of"], row["track"], row["lane"], row["sphere"], row["main"]) == \
            (parent, "notes", "private", "travel", 0)
        run = world.lanes["private"].runs()[-1]
        assert (run["session"], run["cli_session"], run["fork"], run["prompt"]) == \
            (fork, READS_CLI, True, "а если на поезде?")
        assert not {"Edit", "Bash"} & set(run["disallowed"]), "a fork has its track's full rights"
        assert world.store.face_session(fork)["fork_of"] == parent
        again = await world.platform.spheres.fork("face:f1", parent, "а если на поезде?")
        assert again["session"] == fork and len(world.lanes["private"].runs()) == 2, "a retried POST is one fork"

        world.lanes["private"].add(fork, run["run"], fixture("reads")[0].replace(READS_CLI, "f0f0f0f0-0000-4000-8000-"
                                                                                           "000000000000"))
        await world.poll()
        assert world.store.session(fork)["cli_session"] == "f0f0f0f0-0000-4000-8000-000000000000"
        world.lanes["private"].add(fork, run["run"], *fixture("reads")[1:])
        world.lanes["private"].exited(fork, run["run"])
        await world.poll()
        await world.platform.write("tg:9", fork, "дальше")
        later = world.lanes["private"].runs()[-1]
        assert later["cli_session"] == "f0f0f0f0-0000-4000-8000-000000000000" and "fork" not in later
        assert world.store.session(parent)["cli_session"] == READS_CLI, "the parent is its own still"

    spheres_scenario(tmp_path, body)


def test_a_fork_is_refused_while_its_parent_works_or_waits_on_a_card(tmp_path):
    async def body(world: SphereWorld):
        waiting, _, _ = await deferred_session(world)
        refused = await world.platform.spheres.fork("face:f1", waiting, "ветка")
        assert refused == {"ok": False, "say": "Not forked: the session is waiting for your decision on a card — fork "
                                               "it after that."}
        fresh = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "x"}))["session"]
        assert "has not made a single turn yet" in (await world.platform.spheres.fork("face:f2", fresh, "y"))["say"]
        world.lanes["private"].add(fresh, world.lanes["private"].runs()[-1]["run"], fixture("reads")[0])
        await world.poll()
        assert "is working now" in (await world.platform.spheres.fork("face:f3", fresh, "y"))["say"]
        side = (await world.platform.side("face:s1", waiting, "что там?"))["session"]
        assert "a side question is not forked" in (await world.platform.spheres.fork("face:f4", side, "y"))["say"]
        assert world.store.input_session("face:f1") is None, "nothing made for a refusal"

    spheres_scenario(tmp_path, body)


def test_a_fork_whose_parent_started_working_meanwhile_does_not_run_and_says_so(tmp_path):
    async def body(world: SphereWorld):
        world.cfg.lanes["private"].max_sessions = 1
        parent = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        await finished(world, parent)
        other = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "занимаю линию"}))["session"]
        fork = (await world.platform.spheres.fork("face:f1", parent, "ветка"))["session"]
        assert world.store.queued()[0]["session"] == fork, "it waits for room on the lane"
        await world.platform.write("tg:3", parent, "ещё")  # the parent's turn queued before the fork can start
        world.store.update_session(parent, world.clock(), state="active")
        await finished(world, other)
        runs = [r["session"] for r in world.lanes["private"].runs()]
        assert fork not in runs
        said = world.store.last_summary(fork)
        assert said["text"].startswith("The fork did not start: the session is working now")
        assert world.store.session(fork)["state"] == "idle"

    spheres_scenario(tmp_path, body)


# --- the face -------------------------------------------------------------------------------------------------------

def test_the_face_sets_a_sphere_marks_main_forks_and_clears_unread(tmp_path):
    async def body(world: SphereWorld):
        await world.login()
        meta = await world.get("/api/meta")
        assert [s["name"] for s in meta["spheres"]] == ["travel", "home", "work"]
        status, made, _ = await world.call("POST", "/api/sessions", {"input_id": "face:" + "1" * 8 + "-1111-4111-8111-"
                                                                     + "1" * 12, "sphere": "travel", "text": "план"})
        assert status == 200, made
        session = made["session"]
        assert world.store.session(session)["track"] == "notes" and world.store.session(session)["sphere"] == "travel"
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/main", {"main": True})
        assert status == 200 and data["session"]["main"] is True
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/sphere", {"sphere": "home"})
        assert status == 200 and (data["session"]["sphere"], data["session"]["main"]) == ("home", False)
        assert (await world.call("POST", f"/api/sessions/{session}/sphere", {"sphere": "mars"}))[0] == 400
        assert (await world.call("POST", f"/api/sessions/{session}/sphere", {"sphere": ["x"]}))[0] == 400
        assert (await world.call("POST", f"/api/sessions/{session}/main", {"main": "yes"}))[0] == 400
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/sphere", {"sphere": None})
        assert status == 200 and data["session"]["sphere"] is None
        assert (await world.call("POST", f"/api/sessions/{session}/main", {"main": True}))[0] == 400
        assert (await world.call("POST", "/api/sessions/nope0000/seen", {}))[0] == 404

        world.clock.now += 60  # the turn ends a minute after he wrote
        await finished(world, session)
        assert world.store.face_session(session)["unread"]
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/seen", {})
        assert status == 200 and data["session"]["unread"] is False

        fork_id = "face:" + "2" * 8 + "-2222-4222-8222-" + "2" * 12
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/fork", {"input_id": fork_id,
                                                                                     "text": " ветка "})
        assert status == 200, data
        assert world.store.session(data["session"])["fork_of"] == session
        assert world.lanes["private"].runs()[-1]["prompt"] == "ветка"
        assert (await world.call("POST", f"/api/sessions/{session}/fork", {"input_id": fork_id, "text": " "}))[0] \
            == 400
        status, data, _ = await world.call("POST", f"/api/sessions/{data['session']}/fork",
                                           {"input_id": "face:" + "3" * 8 + "-3333-4333-8333-" + "3" * 12,
                                            "text": "ещё"})
        assert status == 400 and data["error"].startswith("Not forked: the session has not made a single turn yet")

    spheres_scenario(tmp_path, body)


# --- a fork never re-runs its parent's deferred call (review of w7/spheres) ------------------------------------------

PARENT_CLI = "11111111-1111-4111-8111-111111111111"


def test_a_parent_whose_card_expired_still_ends_deferred_and_is_not_forked(tmp_path):
    """(A) Idle after its card expired, the parent's conversation still ends in the deferred call: a fork's resume
    would run it, and the owner never gave the fork that call."""
    async def body(world: SphereWorld):
        parent, _, _ = await deferred_session(world)
        world.clock.now += 7 * DAY + 1
        world.platform.tick()
        assert world.store.session(parent)["state"] == "idle"
        answer = await world.platform.spheres.fork("face:f1", parent, "ветка")
        assert answer["ok"] is False and "ends in a call not yet run" in answer["say"]
        assert world.store.input_session("face:f1") is None

    spheres_scenario(tmp_path, body)


def test_a_granted_call_whose_resume_the_lane_refused_is_not_forked_nor_granted_to_another_session(tmp_path):
    """(B) Granted, then the lane refused the bare resume: the parent idles with its approval `granted`. No fork; and
    a run of another session making a call with the same tool_use id is never «Владелец разрешил» by it."""
    async def body(world: SphereWorld):
        parent, _, card = await deferred_session(world)
        world.lanes["code"].answers["/run"] = "refused"
        await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        assert world.store.session(parent)["state"] == "idle"
        assert world.store.approval(card["approval"])["state"] == "granted"
        world.lanes["code"].answers["/run"] = "started"
        assert (await world.platform.spheres.fork("face:f1", parent, "ветка"))["ok"] is False

        other = (await world.owner("new", {"input_id": "tg:7", "track": "retinue", "text": "другое"}))["session"]
        run = [r for r in world.lanes["code"].runs() if r["session"] == other][-1]
        assert decision(await world.hook("PreToolUse", run, pre_tool_use())) != "allow"
        assert world.store.approval(card["approval"])["state"] == "granted", "the parent's grant stays the parent's"
        assert {a["session"] for a in world.store.approvals(tool_use_id=CALL["id"])} == {parent, other}

    spheres_scenario(tmp_path, body)


def test_a_parent_with_a_run_queued_or_moved_to_another_lane_or_folder_is_not_forked(tmp_path):
    """(C) A resume still owed — queued, its answer lost after a restart — is a run of the parent to come. And a
    track moved to another lane or folder no longer holds the parent's conversation."""
    async def body(world: SphereWorld):
        parent = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        await finished(world, parent)
        world.store.enqueue(parent, None, world.clock())
        assert "working" in (await world.platform.spheres.fork("face:f1", parent, "y"))["say"]
        world.store.dequeue(world.store.queued()[0]["seq"])
        notes = world.platform.tracks["notes"]
        notes.cwd = "elsewhere"
        assert "another lane or in another folder" in (await world.platform.spheres.fork("face:f2", parent, "y"))["say"]
        notes.cwd, notes.lane = "", "code"
        assert "another lane or in another folder" in (await world.platform.spheres.fork("face:f3", parent, "y"))["say"]
        notes.lane = "private"
        assert (await world.platform.spheres.fork("face:f4", parent, "y"))["ok"]

    spheres_scenario(tmp_path, body)


def test_a_new_main_session_is_main_from_its_insert_so_a_message_meanwhile_goes_into_it(tmp_path):
    async def body(world: SphereWorld):
        world.lanes["private"].delays["/run"] = 0.2  # the first run is still being started

        async def say(n: int, text: str) -> dict:
            return await world.owner("sphere", {"input_id": f"tg:{n}", "sphere": "travel", "text": text})

        first, second = await asyncio.gather(say(1, "найди жильё"), say(2, "и билеты"))
        assert first["session"] == second["session"]
        assert world.store.main_of("travel") == first["session"]
        assert world.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    spheres_scenario(tmp_path, body)


# --- the Base: a sphere's own directory in the clone, refreshed before a run (docs/base.md) --------------------------

class FakeBase:
    """BaseRepo's refresh as the platform sees it: nothing while its `busy` says a run on the lane is open."""
    tree_rel = "base"

    def __init__(self, busy) -> None:
        self.refreshes, self.busy, self.waits = 0, busy, []

    async def refresh(self, wait: bool = True):
        self.waits.append(wait)
        if not self.busy():
            self.refreshes += 1


class BaseWorld(FaceWorld):
    async def up(self) -> None:
        from retinue.config import BaseConfig, Track
        self.cfg.tracks.append(Track("base", "private", "private", "base"))
        self.cfg.spheres = [Sphere("trip", "Поездки", "base", "✈️", cwd="base/travel"), *SPHERES]
        self.cfg.base = BaseConfig(author="Владелец <owner@example.com>")
        await super().up()
        self.base = self.platform.base = FakeBase(self.platform._base_busy)


def test_a_sphere_in_the_base_runs_in_its_space_and_the_clone_is_refreshed_before(tmp_path):
    async def body(world: BaseWorld):
        first = (await world.owner("sphere", {"input_id": "tg:1", "sphere": "trip", "text": "план"}))["session"]
        run = world.lanes["private"].runs()[-1]
        assert run["cwd"] == "base/travel" and world.store.session(first)["cwd"] == "base/travel"
        assert world.base.refreshes == 1 and world.base.waits == [False], \
            "the hub's changes are in before the run starts, never waiting for a commit that holds the clone"
        assert world.platform.cwd_for(world.platform.tracks["notes"], "trip") == "", \
            "a sphere's directory only on the sphere's own track"
        await world.owner("new", {"input_id": "tg:2", "track": "base", "text": "ещё"})
        assert world.lanes["private"].runs()[-1]["cwd"] == "base" and world.base.refreshes == 1, \
            "another run is writing in the clone: no refresh under it"
        await finished(world, first)
        await world.owner("new", {"input_id": "tg:3", "track": "notes", "text": "вне Базы"})
        assert world.lanes["private"].runs()[-1]["cwd"] == "" and world.base.refreshes == 1
        fork = await world.platform.spheres.fork("face:f1", first, "а если иначе?")
        assert fork["ok"] and world.store.session(fork["session"])["cwd"] == "base/travel", \
            "a fork runs where its parent ran: the sphere's directory is not «moved»"

    async def go():
        world = BaseWorld(tmp_path)
        await world.start()
        try:
            await body(world)
        finally:
            await world.stop()
    asyncio.run(go())
