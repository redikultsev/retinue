"""The router's bus tools (`retinue/tools.py`): what every call goes through, once, for every tool — the caller's own
open turn, the grant, the part, the arguments — and what the engine offers is the registry itself."""

import asyncio

import pytest

from retinue.archive import Archive
from retinue.config import RouterAgent
from retinue.core import Core
from retinue.engine import bus_tools
from retinue.protocol import Store
from retinue.tools import NOT_AN_OBJECT, QUERY_CHARS, TOOLS, run

from test_core import FakeChannel

GRANTS = ("archive", "reminders", "attachments", "trips", "drafts", "collections")


def make(tmp_path, *granted):
    """A router with no optional part: no pages, no outbox, no zone, no platform, no plan."""
    she = RouterAgent(id="assistant", name="Assistant", url="a", trust_class="private",
                      **{grant: grant in granted for grant in GRANTS})
    other = RouterAgent(id="other", name="Other", url="o", trust_class="private", **dict.fromkeys(GRANTS, True))
    core = Core([she, other], Store(str(tmp_path / "r.sqlite")), "owner", archive=Archive(str(tmp_path / "a.sqlite")))
    channel = FakeChannel("matrix", True)
    asyncio.run(core.start([channel]))
    return core, she, channel


def quiet(core, channel) -> bool:
    """Nothing on record and nothing said where the protocol is shown."""
    rows = core.store.db.execute("SELECT * FROM protocol WHERE channel = 'bus'").fetchall()
    return rows == [] and not [e for e in channel.events if e[0] == "protocol"]


@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_answers_only_in_the_callers_own_open_turn(tmp_path, name):
    core, she, channel = make(tmp_path, *GRANTS)
    theirs, closed = core.turns.open_root("other"), core.turns.open_root("assistant")
    core.turns.close(closed)

    async def go():
        return [await run(core, she, turn_id, name, {}) for turn_id in ("", "gone", closed.id, theirs.id)]

    refusal = f"No active request: {TOOLS[name].during} only during an answer."
    assert [(a.ok, a.text) for a in asyncio.run(go())] == [(False, refusal)] * 4
    assert quiet(core, channel), "a call outside her turn does nothing and is not recorded"


@pytest.mark.parametrize("name", [name for name, tool in TOOLS.items() if tool.grant])
def test_a_tool_needs_its_grant(tmp_path, name):
    tool = TOOLS[name]
    core, she, channel = make(tmp_path, *(grant for grant in GRANTS if grant != tool.grant))
    turn = core.turns.open_root("assistant")
    said = asyncio.run(run(core, she, turn.id, name, {}))
    assert (said.ok, said.text) == (False, tool.denied)
    assert quiet(core, channel)


@pytest.mark.parametrize("name", [name for name, tool in TOOLS.items() if tool.wired])
def test_a_tool_whose_part_this_router_has_not_says_so(tmp_path, name):
    core, she, channel = make(tmp_path, *GRANTS)
    turn = core.turns.open_root("assistant")
    said = asyncio.run(run(core, she, turn.id, name, {}))
    assert (said.ok, said.text) == (False, TOOLS[name].off) and TOOLS[name].off
    assert quiet(core, channel)


@pytest.mark.parametrize("name", [name for name, tool in TOOLS.items() if tool.wired is None])
def test_arguments_are_an_object(tmp_path, name):
    core, she, channel = make(tmp_path, *GRANTS)
    turn = core.turns.open_root("assistant")
    said = asyncio.run(run(core, she, turn.id, name, ["x"]))
    assert (said.ok, said.text) == (False, NOT_AN_OBJECT) and quiet(core, channel)


def test_a_refusal_before_anything_was_done_is_not_recorded_and_an_answer_is(tmp_path):
    core, she, channel = make(tmp_path, "archive")
    turn = core.turns.open_root("assistant")

    async def go():
        return [await run(core, she, turn.id, "search_archive", {"query": "Энск", "scope": "everywhere"}),
                await run(core, she, turn.id, "search_archive", {"query": "  Энск "})]

    refused, found = asyncio.run(go())
    assert refused.ok is False and refused.text
    assert found.ok and "Энск" in found.text
    rows = core.store.db.execute("SELECT conversation_id, source, target, status, input_chars FROM protocol "
                                 "WHERE channel = 'bus'").fetchall()
    assert rows == [(f"tree-{turn.tree.id}", "assistant", "archive", "done", 4)], "the query as searched"


def test_the_engine_offers_each_tool_by_the_registrys_name_words_and_schema():
    offered = bus_tools("http://router:9100", "token", "turn-1")
    assert list(offered) == list(TOOLS)
    for name, tool in TOOLS.items():
        assert (offered[name].name, offered[name].description) == (name, tool.description)
        assert offered[name].input_schema is tool.schema
    hints = offered["collection_query"].annotations.model_dump(exclude_none=True, by_alias=True)
    assert hints == {"readOnlyHint": True, "maxResultSizeChars": QUERY_CHARS}, "a query's answer stays inline"
    assert offered["search_archive"].annotations is None
