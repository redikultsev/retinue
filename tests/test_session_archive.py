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
