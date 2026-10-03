"""Bus rules: grants, loops, depth, trust classes, taint — decided by the router, not the model."""

import asyncio

from retinue.config import RouterAgent
from retinue.core import Core
from retinue.protocol import Store

from test_core import FakeChannel

AGENTS = [
    RouterAgent(id="concierge", name="Главная", url="c", trust_class="none", can_call=["*"]),
    RouterAgent(id="travel", name="Путешествия", url="t", trust_class="web", can_call=["study", "career"]),
    RouterAgent(id="study", name="Учёба", url="s", trust_class="web", can_call=["travel"]),
    RouterAgent(id="career", name="Карьера", url="k", trust_class="private", can_call=["travel"]),
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
        ok, text = await core.bus_call(by_id("concierge"), root.id, "travel", "BEG→EVN 10.11")
        assert ok and text == "t: ok"
        child_turn = asked[-1][3]
        assert asked[-1][2] == f"bus-{root.tree.id}-travel", "bus calls do not touch the owner's conversation"

        # web agent's answer taints the tree: the chain can no longer reach an agent with the base
        ok, text = await core.bus_call(by_id("concierge"), root.id, "career", "что с отпуском?")
        assert not ok and "агента с вебом" in text

        # the callee's turn is only valid while it runs, and only for that agent
        ok, text = await core.bus_call(by_id("travel"), child_turn, "study", "x")
        assert not ok and "Нет активного запроса" in text
        ok, text = await core.bus_call(by_id("study"), root.id, "travel", "x")
        assert not ok and "Нет активного запроса" in text

        # depth and loops
        t1 = core.turns.open_child(root, "travel")
        t2 = core.turns.open_child(t1, "study")
        ok, text = await core.bus_call(by_id("study"), t2.id, "travel", "x")
        assert not ok and "петля" in text
        ok, text = await core.bus_call(by_id("travel"), t1.id, "concierge", "x")
        assert not ok and "не разрешено" in text

        # classes: web -> private and private -> web are refused as free text
        fresh = core.turns.open_root("travel")
        ok, text = await core.bus_call(by_id("travel"), fresh.id, "career", "x")
        assert not ok and "с вебом не пишет" in text
        private = core.turns.open_root("career")
        ok, text = await core.bus_call(by_id("career"), private.id, "travel", "x")
        assert not ok and "типизированный навык" in text

    asyncio.run(run())
    traces = [e[2] for e in matrix.events if e[0] == "trace"]
    assert traces[0].startswith("**Главная → Путешествия:**") and "BEG→EVN" in traces[0]
    assert any(t.startswith("⛔") for t in traces)


def test_depth_limit(tmp_path):
    core, _, _ = make(tmp_path)

    async def run():
        root = core.turns.open_root("concierge")
        t1 = core.turns.open_child(root, "study")
        t2 = core.turns.open_child(t1, "travel")
        ok, text = await core.bus_call(by_id("travel"), t2.id, "career", "x")
        assert not ok and "глубже" in text

    asyncio.run(run())
