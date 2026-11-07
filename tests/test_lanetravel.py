"""travel-ops in the room's sessions (`retinue/lanetravel.py`): the platform's own MCP server offers travel-ops' tools
beside its sub-sessions', and forwards a call only when the gate saw it and its arguments pass the assistant's forms —
against the fake lanes of `test_platformd` and a fake travel-ops."""

from __future__ import annotations

import json

import httpx

from retinue import lanetravel, subs
from test_platformd import decision, short_wait  # noqa: F401 — the fixture is autouse
from test_subs import SubsWorld, subs_scenario

URL = "http://travel-ops:8765/mcp"
SAID = "Search live prices with seen_at timestamps."
LISTED = [{"name": name, "description": f"{name} of travel-ops", "inputSchema": {"type": "object"}}
          for name in ("search_flights", "refine_flights", "stay_photos", "watch_alerts", "book_now")]
FLIGHTS = {"origin": "AMS", "destination": "LIS", "depart": "2027-03-13"}
OWN = [t["name"] for t in subs.TOOLS]


class FakeTravelOps:
    """travel-ops' `mcp --http` as the platform reaches it: one JSON-RPC message per POST."""

    def __init__(self) -> None:
        self.up = True
        self.calls: list[tuple[str, dict]] = []
        self.asked: list[str] = []

    def reply(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused")
        body = json.loads(request.content)
        self.asked.append(body["method"])
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "instructions": SAID,
                      "serverInfo": {"name": "travel-ops", "version": "1"}}
        elif body["method"] == "tools/list":
            result = {"tools": LISTED}
        else:
            name, arguments = body["params"]["name"], body["params"]["arguments"]
            self.calls.append((name, arguments))
            result = {"content": [{"type": "text", "text": json.dumps({"search_id": "abcd1234", "cards": []})},
                                  *([{"type": "image", "data": "aGk=", "mimeType": "image/jpeg"}]
                                    if name == "stay_photos" else [])],
                      "structuredContent": {"search_id": "abcd1234"}, "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def desk(self) -> lanetravel.LaneTravel:
        return lanetravel.LaneTravel(URL, httpx.AsyncClient(transport=httpx.MockTransport(self.reply)))


class TravelWorld(SubsWorld):
    ops = FakeTravelOps()

    async def up(self) -> None:
        await super().up()
        self.platform.travel = self.ops.desk()

    async def listed(self, session: str) -> tuple[str, list[str]]:
        run = self.run_of(session)
        _, init = await self.mcp(run, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-11-25"}})
        _, tools = await self.mcp(run, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return init["result"]["instructions"], [t["name"] for t in tools["result"]["tools"]]

    async def travel(self, session: str, name: str, arguments: dict, call: str, **how) -> tuple[str, bool]:
        return await self.tool(session, name, arguments, call, **how)


def travel_scenario(tmp_path, body):
    TravelWorld.ops = FakeTravelOps()
    return subs_scenario(tmp_path, body, TravelWorld)


def test_a_session_is_offered_the_travel_ops_tools_that_have_a_form_beside_its_own(tmp_path):
    async def body(world: TravelWorld):
        parent = await world.parent()
        said, names = await world.listed(parent)
        assert names[:len(OWN)] == OWN, "the sub-sessions' tools first, as before"
        assert names[len(OWN):] == ["search_flights", "refine_flights", "stay_photos"], \
            "only tools the assistant's forms check: not the router's watch_alerts, not one nobody checked"
        assert SAID in said and "travel-ops" in said, "travel-ops' own rules reach the session"
        await world.listed(parent)
        assert world.ops.asked.count("tools/list") == 1, "travel-ops' list is kept a while, not asked every run"
    travel_scenario(tmp_path, body)


def test_a_sub_session_gets_the_server_for_travel_ops_alone(tmp_path):
    async def body(world: TravelWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "flights", "toolu_start")
        await world.platform.drain()
        assert world.run_of(child).get("subs") is True, "the server is on its run: travel-ops is there"
        said, names = await world.listed(child)
        assert names == ["search_flights", "refine_flights", "stay_photos"], "no sub-sessions' tools at depth 1"
        assert "Sub-sessions" not in said and SAID in said
        answer, error = await world.travel(child, "search_flights", FLIGHTS, "toolu_child")
        assert not error and "abcd1234" in answer and world.ops.calls == [("search_flights", FLIGHTS)]
    travel_scenario(tmp_path, body)


def test_a_call_goes_to_travel_ops_only_as_the_gate_saw_it_and_the_form_allows(tmp_path):
    async def body(world: TravelWorld):
        parent = await world.parent()
        answer, error = await world.travel(parent, "search_flights", FLIGHTS, "toolu_1")
        assert not error and json.loads(answer)["search_id"] == "abcd1234"
        assert world.ops.calls == [("search_flights", FLIGHTS)]
        answer, error = await world.travel(parent, "search_flights", FLIGHTS, "toolu_2", pre=False)
        assert error and answer == subs.UNSEEN, "a call the gate never saw is not run"
        leaky = dict(FLIGHTS, destination="LIS", note="my passport 0000")
        answer, error = await world.travel(parent, "search_flights", leaky, "toolu_3")
        assert error, "the assistant's closed forms, with the reason"
        answer, error = await world.travel(parent, "search_flights", dict(FLIGHTS, origin="ams"), "toolu_4")
        assert error and "'AMS'" in answer, "travel-ops gets exactly what was checked"
        answer, error = await world.travel(parent, "watch_alerts", {}, "toolu_5")
        assert error and "router" in answer
        assert world.ops.calls == [("search_flights", FLIGHTS)], "nothing refused ever reached travel-ops"
    travel_scenario(tmp_path, body)


def test_a_subagent_may_search_but_still_not_start_a_sub_session(tmp_path):
    async def body(world: TravelWorld):
        parent = await world.parent()
        answer, error = await world.travel(parent, "search_flights", FLIGHTS, "toolu_1", agent="agent-1")
        assert not error and world.ops.calls == [("search_flights", FLIGHTS)]
        answer, error = await world.tool(parent, subs.START, {"title": "x", "brief": "y"}, "toolu_2", agent="agent-1")
        assert error and answer == subs.NO_DEPTH
    travel_scenario(tmp_path, body)


def test_pictures_come_through_and_a_travel_ops_that_is_down_costs_its_tools_only(tmp_path):
    async def body(world: TravelWorld):
        parent = await world.parent()
        run = world.run_of(parent)
        assert await world.travel(parent, "refine_flights", {"search_id": "abcd1234"}, "toolu_0")
        await world.hook("PreToolUse", run, {
            "hook_event_name": "PreToolUse", "tool_name": subs.PREFIX + "stay_photos",
            "tool_input": {"search_id": "abcd1234", "stays": ["Harbor View"]}, "tool_use_id": "toolu_1",
            "cwd": "/work", "permission_mode": "auto"})
        _, data = await world.mcp(run, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
            "name": "stay_photos", "arguments": {"search_id": "abcd1234", "stays": ["Harbor View"]},
            "_meta": {"claudecode/toolUseId": "toolu_1"}}})
        assert [c["type"] for c in data["result"]["content"]] == ["text", "image"]
        world.ops.up = False
        world.platform.travel = world.ops.desk()  # a new process: nothing kept
        said, names = await world.listed(parent)
        assert names == OWN and "unavailable" in said, "its tools are gone for this run; the session is told why"
        answer, error = await world.travel(parent, "search_flights", FLIGHTS, "toolu_2")
        assert error and answer
    travel_scenario(tmp_path, body)


def test_without_travel_ops_nothing_changes(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        assert world.platform.travel is None
        _, data = await world.mcp(world.run_of(parent), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in data["result"]["tools"]] == OWN
        child = await world.start_child(parent, "flights", "toolu_start")
        await world.platform.drain()
        assert "subs" not in world.run_of(child), "a sub-session's run has no server to talk to"
    subs_scenario(tmp_path, body)


def test_a_search_may_take_its_minutes():
    """A search asks the sites for minutes: the call waits as long as the assistant's does, listing does not."""
    from retinue.engine import TRAVEL_TIMEOUT_MS

    from retinue import cliengine

    assert lanetravel.CALL_TIMEOUT == TRAVEL_TIMEOUT_MS / 1000 == cliengine.MCP_CALL_MS / 1000
    assert json.loads(cliengine.mcp_config("http://platform:9500/hook"))["mcpServers"]["retinue"]["timeout"] \
        == TRAVEL_TIMEOUT_MS, "the CLI waits for a search as long as the platform does"
    assert lanetravel.LIST_TIMEOUT <= 5 and lanetravel.DOWN_S >= 60, "a dead travel-ops costs a run seconds, once"
