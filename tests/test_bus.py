"""Bus rules: grants, loops, depth, trust classes, taint — decided by the router, not the model."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from retinue.archive import ASSISTANT, OWNER, Archive
from retinue.bus import BusServer, bus_token
from retinue.config import RouterAgent
from retinue.core import Core
from retinue.protocol import Store

from test_core import FakeChannel

AGENTS = [
    RouterAgent(id="concierge", name="Главная", url="c", trust_class="none", can_call=["*"]),
    RouterAgent(id="travel", name="Путешествия", url="t", trust_class="web", can_call=["study", "work"]),
    RouterAgent(id="study", name="Учёба", url="s", trust_class="web", can_call=["travel"]),
    RouterAgent(id="work", name="Работа", url="k", trust_class="private", can_call=["travel"]),
]


def make(tmp_path):
    asked = []

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None):
        asked.append((url, text, context_id, turn_id))
        return "done", f"{url}: ok", []

    core = Core(AGENTS, Store(str(tmp_path / "r.sqlite")), "@owner:x", ask=fake_ask)
    matrix = FakeChannel("matrix", True)
    asyncio.run(core.start([matrix]))
    return core, matrix, asked


def by_id(agent_id):
    return next(a for a in AGENTS if a.id == agent_id)


def test_rules(tmp_path):
    core, matrix, asked = make(tmp_path)

    async def run():
        root = core.turns.open_root("concierge")
        ok, text = await core.bus_call(by_id("concierge"), root.id, "travel", "AMS→LIS 10.05")
        assert ok and text == "t: ok"
        child_turn = asked[-1][3]
        assert asked[-1][2] == core.store.conversation("travel"), "the target answers in its own conversation"
        assert asked[-1][1].startswith("[Question from agent «Главная»]")

        # web agent's answer taints the tree: the chain can no longer reach an agent with the base
        ok, text = await core.bus_call(by_id("concierge"), root.id, "work", "что с отпуском?")
        assert not ok and "an agent with the web" in text

        # the callee's turn is only valid while it runs, and only for that agent
        ok, text = await core.bus_call(by_id("travel"), child_turn, "study", "x")
        assert not ok and "No active request" in text
        ok, text = await core.bus_call(by_id("study"), root.id, "travel", "x")
        assert not ok and "No active request" in text

        # depth and loops
        t1 = core.turns.open_child(root, "travel")
        t2 = core.turns.open_child(t1, "study")
        ok, text = await core.bus_call(by_id("study"), t2.id, "travel", "x")
        assert not ok and "a loop" in text
        ok, text = await core.bus_call(by_id("travel"), t1.id, "concierge", "x")
        assert not ok and "may not call" in text

        # classes: web -> private and private -> web are refused as free text
        fresh = core.turns.open_root("travel")
        ok, text = await core.bus_call(by_id("travel"), fresh.id, "work", "x")
        assert not ok and "with the web does not write" in text
        private = core.turns.open_root("work")
        ok, text = await core.bus_call(by_id("work"), private.id, "travel", "x")
        assert not ok and "a typed skill" in text

    asyncio.run(run())
    traces = [e[2] for e in matrix.events if e[0] == "trace"]
    assert traces[0].startswith("**Главная → Путешествия:**") and "AMS→LIS" in traces[0]
    assert any(t.startswith("⛔") for t in traces)


def test_private_answer_keeps_chain_off_the_web(tmp_path):
    core, _, _ = make(tmp_path)

    async def run():
        root = core.turns.open_root("concierge")
        ok, _ = await core.bus_call(by_id("concierge"), root.id, "work", "что с отпуском?")
        assert ok
        ok, text = await core.bus_call(by_id("concierge"), root.id, "travel", "билеты на эти даты")
        assert not ok and "an agent with the Base" in text

    asyncio.run(run())


def test_depth_limit(tmp_path):
    core, _, _ = make(tmp_path)

    async def run():
        root = core.turns.open_root("concierge")
        t1 = core.turns.open_child(root, "study")
        t2 = core.turns.open_child(t1, "travel")
        ok, text = await core.bus_call(by_id("travel"), t2.id, "work", "x")
        assert not ok and "deeper than" in text

    asyncio.run(run())


def test_archive_search_through_the_bus(tmp_path):
    agents = [RouterAgent(id="assistant", name="Assistant", url="a", trust_class="private", archive=True),
              RouterAgent(id="travel", name="Путешествия", url="t", trust_class="web")]
    archive = Archive(str(tmp_path / "archive.sqlite"))
    archive.append(ASSISTANT, "По Энску советую площадь и музей.", conversation_id="old", channel="telegram", ts=100.0)
    archive.append(ASSISTANT, "В Энске съезди ещё на озеро.", conversation_id="now", channel="telegram", ts=150.0)
    question, _ = archive.append(OWNER, "Что ты советовала по Энску?", conversation_id="now", channel="telegram", ts=200.0)

    async def run():
        core = Core(agents, Store(str(tmp_path / "r.sqlite")), "owner", archive=archive)
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        core.inbound[turn.tree.id] = question.id
        mine = {"Authorization": f"Bearer {bus_token('secret', 'assistant')}"}
        async with TestClient(TestServer(BusServer(core, "secret", 0).app)) as http:
            found = await (await http.post("/archive/search", json={"turn": turn.id, "query": "Энск"}, headers=mine)).json()
            assert found["ok"] and "площадь и музей" in found["text"] and "1970-01-01 00:01 UTC, Thursday · Assistant" in found["text"]
            assert "Что ты советовала" not in found["text"], "the question being answered is not a result"
            hits = found["text"].split("\n\n")[1:]
            where = {("площадь" in h, "озеро" in h): h.splitlines()[0] for h in hits}
            assert where[(True, False)].endswith("· past conversation]") and where[(False, True)].endswith("· this conversation]"), \
                "a hit says which conversation it is from: after /new the old one is not «this one»"

            empty = await (await http.post("/archive/search", json={"turn": turn.id, "query": "зарплата"}, headers=mine)).json()
            assert empty["ok"] and "Nothing found" in empty["text"] and "3 events" in empty["text"]
            assert "this one and past ones" in empty["text"]
            assert "mail and calendar are not connected" in empty["text"] and "files on the Mac" in empty["text"], \
                "an empty result names what is not collected"

            nobody = await http.post("/archive/search", json={"turn": turn.id, "query": "Энск"},
                                     headers={"Authorization": "Bearer wrong"})
            assert nobody.status == 401
            stale = await (await http.post("/archive/search", json={"turn": "closed", "query": "Энск"}, headers=mine)).json()
            assert not stale["ok"] and "No active request" in stale["text"]

            other = core.turns.open_root("travel")
            theirs = {"Authorization": f"Bearer {bus_token('secret', 'travel')}"}
            denied = await (await http.post("/archive/search", json={"turn": other.id, "query": "Энск"}, headers=theirs)).json()
            assert not denied["ok"] and "not been granted" in denied["text"] and "площадь" not in denied["text"]
        return core

    core = asyncio.run(run())
    rows = core.store.db.execute("SELECT source, target, status FROM protocol WHERE channel = 'bus'").fetchall()
    assert rows == [("assistant", "archive", "done")] * 2, "every search is in the protocol"


def test_reminders_through_the_bus(tmp_path):
    agents = [RouterAgent(id="assistant", name="Assistant", url="a", trust_class="private", reminders=True),
              RouterAgent(id="travel", name="Путешествия", url="t", trust_class="web")]

    async def run():
        core = Core(agents, Store(str(tmp_path / "r.sqlite")), "owner")
        await core.start([FakeChannel("telegram", False)])
        mine = {"Authorization": f"Bearer {bus_token('secret', 'assistant')}"}
        theirs = {"Authorization": f"Bearer {bus_token('secret', 'travel')}"}
        turn, other = core.turns.open_root("assistant"), core.turns.open_root("travel")
        async with TestClient(TestServer(BusServer(core, "secret", 0).app)) as http:
            async def post(action, headers=mine, **body):
                response = await http.post(f"/reminders/{action}", json=body, headers=headers)
                return response.status, await response.json() if response.status == 200 else None

            assert await post("list", turn=turn.id) == (200, {"ok": True, "text": "No active reminders."})
            status, past = await post("add", turn=turn.id, text="позвонить", when="2020-01-01T10:00", weekday="")
            assert not past["ok"] and "has already passed" in past["text"]
            status, denied = await post("add", theirs, turn=other.id, text="x", when="2030-01-01T10:00", weekday="")
            assert not denied["ok"] and "not been granted" in denied["text"], "a grant, like the archive"
            status, odd = await post("delete", turn=turn.id)
            assert not odd["ok"] and "No such reminder action" in odd["text"]
            assert (await post("list", {"Authorization": "Bearer wrong"}, turn=turn.id))[0] == 401
        return core

    core = asyncio.run(run())
    rows = core.store.db.execute("SELECT source, target, status FROM protocol WHERE channel = 'bus'").fetchall()
    assert rows == [("assistant", "reminders/list", "done"), ("assistant", "reminders/add", "rejected")]


def test_the_zone_through_the_bus(tmp_path):
    from retinue.proactive import Proactive

    agent = RouterAgent(id="assistant", name="Assistant", url="a", trust_class="private", reminders=True)

    async def run():
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner", proactive=Proactive())
        await core.start([FakeChannel("telegram", False)])
        mine = {"Authorization": f"Bearer {bus_token('secret', 'assistant')}"}
        turn = core.turns.open_root("assistant")
        async with TestClient(TestServer(BusServer(core, "secret", 0).app)) as http:
            response = await http.post("/zone", json={"turn": turn.id, "zone": "Asia/Manila"}, headers=mine)
            said = await response.json()
        assert said["ok"] and said["text"].startswith("The zone is now Asia/Manila") and core.tz == "Asia/Manila"
        return core

    core = asyncio.run(run())
    assert core.store.db.execute("SELECT target, status FROM protocol WHERE channel = 'bus'").fetchall() == [
        ("zone", "done")]


def test_every_guard_decision_on_travel_ops_is_on_record(tmp_path):
    """The assistant calls travel-ops herself; the engine's guard tells the router what it let through and what
    it refused, so the protocol shows every search and every refusal."""
    agents = [RouterAgent(id="assistant", name="Assistant", url="a", trust_class="private")]

    async def run():
        core = Core(agents, Store(str(tmp_path / "r.sqlite")), "owner")
        matrix = FakeChannel("matrix", True)
        await core.start([matrix])
        mine = {"Authorization": f"Bearer {bus_token('secret', 'assistant')}"}
        turn = core.turns.open_root("assistant")
        async with TestClient(TestServer(BusServer(core, "secret", 0).app)) as http:
            async def post(headers=mine, **body):
                response = await http.post("/travel/log", json=body, headers=headers)
                return response.status, await response.json() if response.status == 200 else None

            allowed = await post(turn=turn.id, tool="search_trip", decision="allow", chars=93)
            denied = await post(turn=turn.id, tool="search_trip", decision="deny", chars=140,
                                reason="place: название латиницей, до пяти слов")
            odd = await post(turn=turn.id, tool="search_trip; DROP TABLE protocol", decision="allow", chars="x")
            core.turns.close(turn)
            late = await post(turn=turn.id, tool="search_trip", decision="allow", chars=1)
            stranger = await post({"Authorization": "Bearer wrong"}, turn=turn.id, tool="x", decision="allow")
        return core, matrix, allowed, denied, odd, late, stranger

    core, matrix, allowed, denied, odd, late, stranger = asyncio.run(run())
    assert allowed == denied == odd == (200, {"ok": True, "text": ""})
    assert late == (200, {"ok": False, "text": "No active request."}) and stranger[0] == 401
    rows = core.store.db.execute("SELECT source, target, status, input_chars FROM protocol WHERE channel = 'bus'")
    assert rows.fetchall() == [("assistant", "travel/search_trip", "allowed", 93),
                               ("assistant", "travel/search_trip", "denied", 140),
                               ("assistant", "travel/search_tripDROPTABLEprotocol", "allowed", 0)]
    assert ("protocol", "travel: Assistant → search_trip: denied — place: название латиницей, до пяти слов") \
        in matrix.events, "the reason is shown where the protocol is shown"
