"""The Router's mirror on the platform (`mirror`): a snapshot goes up whole when it changed and at least hourly, a
refusal is logged and not hammered, the platform away is tried again; a press goes to its kind's handler, in the
notes' order or beside them; the look runs its steps in order, each on its own, and a poke brings it forward."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from retinue import mirror as mirror_module
from retinue.bridge import SHORT_S, PlatformClient
from retinue.mail import Unavailable
from retinue.mirror import FAILED_PRESS, PUSH_S, Look, Mirror


class Platform:
    def __init__(self):
        self.calls, self.answers, self.down = [], {}, False

    async def post(self, call, body):
        if self.down:
            raise Unavailable("unavailable: the platform (ConnectError)")
        self.calls.append((call, json.loads(json.dumps(body))))
        return self.answers.get(call, {"ok": True})


def test_a_snapshot_goes_whole_when_it_changes_and_hourly_and_a_refusal_or_the_platform_away_is_logged(caplog):
    caplog.set_level(logging.INFO)
    platform = Platform()
    shown: list | None = None
    digest = Mirror(platform.post).snapshot("waiting", lambda now: None if shown is None else {"items": list(shown)},
                                            "the assistant's digest")

    async def run():
        nonlocal shown
        assert not await digest(1000.0), "nothing to show yet: not an empty one"
        shown = ["chat:1"]
        assert await digest(1010.0)
        assert not await digest(1100.0), "the same is not pushed again"
        shown.append("chat:2")
        assert await digest(1200.0), "changed"
        assert await digest(1200.0 + PUSH_S), "an hour on, changed or not"
        platform.answers["waiting"] = {"error": "items[2]: wrong fields"}
        shown.append("chat:3")
        assert await digest(9000.0)
        assert not await digest(9100.0), "a refused one is not hammered"
        platform.answers["waiting"] = {"ok": True, "dropped": [{"id": "chat:3", "why": "not a sphere name"}]}
        shown.append("chat:4")
        assert await digest(9200.0)
        platform.down = True
        shown.append("chat:5")
        assert not await digest(9300.0), "the platform away: said, raised to nobody"
        platform.down = False
        assert await digest(9301.0), "and the same goes at the next publish"

    asyncio.run(run())
    assert [body for _, body in platform.calls] == [
        {"items": ["chat:1"]}, {"items": ["chat:1", "chat:2"]}, {"items": ["chat:1", "chat:2"]},
        {"items": ["chat:1", "chat:2", "chat:3"]}, {"items": ["chat:1", "chat:2", "chat:3", "chat:4"]},
        {"items": ["chat:1", "chat:2", "chat:3", "chat:4", "chat:5"]}]
    assert {call for call, _ in platform.calls} == {"waiting"}
    assert "the platform refused the assistant's digest: items[2]: wrong fields" in caplog.text
    assert "the platform dropped chat:3 from the assistant's digest: not a sphere name" in caplog.text
    assert "the assistant's digest not pushed: unavailable: the platform" in caplog.text


def test_a_snapshot_asked_for_after_each_change_goes_every_time_and_one_that_cannot_be_built_breaks_nothing(caplog):
    platform = Platform()
    mirror = Mirror(platform.post)
    plan = mirror.snapshot("plan", lambda now: {"plans": {}}, "the plan", every_s=0)
    broken = mirror.snapshot("library", lambda now: {"pages": 1 / 0}, "the pages' list")

    async def run():
        assert await plan(1.0) and await plan(1.0), "every time it is asked"
        assert not await broken(1.0)

    asyncio.run(run())
    assert platform.calls == [("plan", {"plans": {}})] * 2
    assert "the pages' list not pushed" in caplog.text and "ZeroDivisionError" in caplog.text


def test_a_press_goes_to_its_kinds_handler_in_the_notes_order_or_beside_them_and_a_failure_is_said(caplog):
    mirror = Mirror(Platform().post)
    pressed, release = [], asyncio.Event()

    async def waiting(data, now):
        pressed.append(("waiting", data["t"], now))
        return None if data["t"] == "known" else "The list is out of date."

    async def plan(data, now):
        await release.wait()
        pressed.append(("plan", data["action"], now))
        raise RuntimeError("the collections are locked")

    mirror.on("waiting-press", waiting)
    mirror.on("plan-press", plan, beside=True)

    async def run():
        assert mirror.takes("waiting-press") and not mirror.takes("card")
        assert await mirror.press("waiting-press", {"t": "known"}, 5.0) is None, "applied: nothing to say"
        assert await mirror.press("waiting-press", {"t": "old"}, 6.0) == "The list is out of date."
        assert await mirror.press("waiting-press", "битая", 7.0) == FAILED_PRESS, "malformed: told, no details"
        assert await mirror.press("plan-press", {"action": "plan"}, 8.0) is None, "beside the notes: started"
        assert pressed[-1][0] == "waiting", "and not waited for"
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert pressed == [("waiting", "known", 5.0), ("waiting", "old", 6.0), ("plan", "plan", 8.0)]
    assert caplog.text.count("was not applied") == 2, "the fault and the one beside: logged"


def test_the_look_runs_its_steps_in_order_each_on_its_own_and_a_poke_brings_it_forward(caplog):
    ran: list[str] = []
    started: list[asyncio.Task] = []

    async def reconcile(now):
        ran.append("tasks")
        raise Unavailable("unavailable: the platform (ConnectError)")

    def calendar(now):
        ran.append("calendar")
        raise RuntimeError("a bug")

    def google(now):  # work of its own: the look does not wait for it
        ran.append("google")
        task = asyncio.create_task(asyncio.sleep(3600))
        started.append(task)
        return task

    async def digest(now):
        ran.append("digest")

    look = Look([("the tasks' reconciliation", reconcile), ("the busy window", calendar),
                 ("the calendar «Retinue»", google), ("the assistant's digest", digest)])

    async def run():
        assert await look.once(1000.0)
        assert not await look.once(1000.0 + mirror_module.LOOK_S - 1), "every 30 s"
        look.poke()
        assert await look.once(1001.0), "poked: at once"
        assert await look.once(1001.0 + mirror_module.LOOK_S)
        for task in started:
            task.cancel()

    asyncio.run(run())
    assert ran == ["tasks", "calendar", "google", "digest"] * 3
    assert "the tasks' reconciliation not done: unavailable" in caplog.text and "the busy window failed" in caplog.text


def test_a_snapshot_goes_to_the_platforms_own_address_with_a_short_wait_and_a_413_is_its_refusal():
    seen = []

    def answer(request):
        seen.append((request.url.path, json.loads(request.content), request.extensions["timeout"]["read"]))
        if request.url.path == "/owner/waiting":
            return httpx.Response(413, text="<html>too big</html>")
        return httpx.Response(200, json={"ok": True, "pages": 0})

    client = PlatformClient("http://platform:9500", "t", "the platform",
                            httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    assert asyncio.run(client.publish("library", {"pages": []})) == {"ok": True, "pages": 0}
    assert "413" in asyncio.run(client.publish("waiting", {"items": []}))["error"]
    assert seen == [("/owner/library", {"pages": []}, SHORT_S), ("/owner/waiting", {"items": []}, SHORT_S)]
