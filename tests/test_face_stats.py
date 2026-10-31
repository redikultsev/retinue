"""The «Week» screen's numbers: `GET /api/stats` answers the platform's tally (`signals.tally`) of the last eight
seven-day spans, newest first, behind the face's login like every other route."""

from __future__ import annotations

from retinue import face, signals

from test_face import FaceWorld, faced
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse


def test_the_week_screen_gets_eight_spans_newest_first_and_only_with_a_login(tmp_path):
    async def body(world: FaceWorld):
        assert (await world.call("GET", "/api/stats"))[0] == 401, "nothing without a login"
        await world.login()
        now = world.clock.now
        signals.add(world.store, signals.OWNER_STOP, "s1", now - 3600)
        signals.add(world.store, signals.OWNER_STOP, "s1", now - face.WEEK_S - 3600)
        signals.add(world.store, signals.COMPACTION, "s1", now - 3 * face.WEEK_S - 3600)
        answer = await world.get("/api/stats")
        weeks = answer["weeks"]
        assert len(weeks) == face.STATS_WEEKS and answer["now"] == now
        assert [w["owner"]["stops"] for w in weeks[:3]] == [1, 1, 0]
        assert [w["engine"]["compactions"] for w in weeks[:4]] == [0, 0, 0, 1]
        assert all(weeks[n]["since"] == weeks[n + 1]["until"] for n in range(len(weeks) - 1)), "no gap, no overlap"

    faced(tmp_path, body)
