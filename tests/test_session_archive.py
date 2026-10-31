"""«Archive» (`Platform.archive`, `PlatformStore.archive`): the owner takes a session out of the app's list — it and
its kin end where they stand, every device drops the row, and a message to it brings it back."""

from __future__ import annotations

from test_face import FaceWorld, faced, input_id, replayed
from test_platformd import deferred_session, short_wait  # noqa: F401 — the fixture is autouse
from test_subs import SubsWorld, subs_scenario


def test_an_archived_session_ends_leaves_every_list_and_comes_back_with_a_message(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, _ = await deferred_session(world)
        cursor = world.store.cursor()
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/archive", {})
        assert status == 200 and data["session"]["archived"] is True and data["session"]["state"] == "closed"
        assert [a["state"] for a in world.store.approvals(session=session)] == ["stale"], "its card asks nothing now"
        assert "session" in [kind for _, kind, _ in world.store.events_after(cursor)], "every device hears of it"
        assert session not in [s["id"] for s in (await world.get("/api/sessions"))["sessions"]]
        assert replayed(world.store)["sessions"][session]["archived"] is True
        assert (await world.call("POST", "/api/sessions/nope0000/archive", {}))[0] == 404

        await world.call("POST", f"/api/sessions/{session}/input", {"input_id": input_id(), "text": "ещё"})
        assert "archived" not in world.store.face_session(session), "a turn begins: back in the list"
        assert session in [s["id"] for s in (await world.get("/api/sessions"))["sessions"]]

    faced(tmp_path, body)


def test_archiving_a_parent_ends_and_hides_its_sub_sessions_too(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        kids = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(2)]
        assert await world.platform.archive(parent)
        assert [world.store.session(k)["state"] for k in kids] == ["closed"] * 2
        assert all(world.store.session(s)["archived"] for s in [parent, *kids])
        listed = {s["id"] for s in world.store.face_sessions(world.clock.now)}
        assert not listed & {parent, *kids}
        assert not await world.platform.archive("nope0000")

    subs_scenario(tmp_path, body)


def test_undo_brings_an_archived_session_and_its_kin_back_and_its_main_mark_if_free(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        status, made, _ = await world.call("POST", "/api/sessions", {"input_id": input_id(), "track": "notes",
                                                                     "text": "план"})
        session = made["session"]
        world.store.db.execute("UPDATE sessions SET sphere = 'travel', main = 1 WHERE id = ?", (session,))
        world.store.db.commit()
        assert (await world.call("POST", f"/api/sessions/{session}/archive", {}))[0] == 200
        assert world.store.session(session)["main"] == 0
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/unarchive", {"main": True})
        assert status == 200 and "archived" not in data["session"] and data["session"]["main"] is True
        assert session in [s["id"] for s in (await world.get("/api/sessions"))["sessions"]]
        assert world.store.session(session)["state"] == "closed", "its stopped turn stays stopped"
        assert world.store.unarchive(session) == [], "not archived: nothing to undo"
        assert (await world.call("POST", f"/api/sessions/{session}/unarchive", {"main": "yes"}))[0] == 400
        assert (await world.call("POST", "/api/sessions/nope0000/unarchive", {}))[0] == 404

    faced(tmp_path, body)


def test_undo_takes_back_only_the_kin_archived_with_it(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        early, late = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(2)]
        world.store.archive(early, world.clock.now)
        world.clock.now += 60
        assert await world.platform.archive(parent)
        assert set(world.store.unarchive(parent)) == {parent, late}
        assert world.store.session(early)["archived"], "archived on its own before: it stays archived"

    subs_scenario(tmp_path, body)


def test_the_archived_list_has_the_latest_first_and_kin_archived_with_a_session_under_it(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        alone, kid = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(2)]
        world.store.archive(alone, world.clock.now)
        world.clock.now += 60
        assert await world.platform.archive(parent)
        listed = world.store.face_archived()
        assert [s["id"] for s in listed] == [parent, alone], "its kid went with it: restoring the parent brings it"
        assert listed[0]["archived_at"] == world.clock.now and listed[0]["archived"] is True
        world.store.unarchive(parent)
        assert [s["id"] for s in world.store.face_archived()] == [alone]
        assert kid not in [s["id"] for s in world.store.face_archived()]

    subs_scenario(tmp_path, body)


def test_the_app_reads_the_archived_list(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        status, made, _ = await world.call("POST", "/api/sessions", {"input_id": input_id(), "track": "notes",
                                                                     "text": "план"})
        assert (await world.get("/api/archived"))["sessions"] == []
        await world.call("POST", f"/api/sessions/{made['session']}/archive", {})
        [row] = (await world.get("/api/archived"))["sessions"]
        assert row["id"] == made["session"] and row["archived_at"] == world.clock.now

    faced(tmp_path, body)
