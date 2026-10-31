"""travel-ops in the room's sessions: live prices of flights, stays, trains and buses, as the assistant has them.

**The platform's own MCP server (`subs`) offers them**, beside the sub-sessions' tools: a run may have that one server
only (`cliengine.mcp_config`), and the lanes never reach travel-ops — it has no login, and only the platform shares a
network with it (`travel-platform`). A session sees travel-ops' tools under the platform's name
(`mcp__retinue__search_flights`), each call seen by the gate first, as every call of that server is (`subs.call`).

**What goes out is the assistant's check** (`travel.exact`): closed types only, and the call goes to travel-ops exactly
as it was checked; anything else comes back refused with the reason. Only the tools those forms know are offered —
never `watch_alerts`, which is the router's.

**travel-ops' own list and rules** (`initialize`, `tools/list`) are asked once in a while and kept; a travel-ops that
does not answer costs a run its travel tools, not its turn, and the session is told why.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from . import travel

LIST_S = 600.0          # travel-ops' tools and rules, asked again after this long
DOWN_S = 60.0           # a travel-ops that did not answer: asked again after this long, not on every request
LIST_TIMEOUT = 5.0      # initialize and tools/list: a travel-ops that does not answer costs a run five seconds
CALL_TIMEOUT = 840.0    # a search asks the sites for minutes: the assistant's own limit (`engine.TRAVEL_TIMEOUT_MS`)
CONTENT = ("text", "image")  # what a session reads of an answer: the words, and the pictures of `stay_photos`
SAID = ("travel-ops: live prices of flights, stays, trains and buses, through this server's tools (search_trip, "
        "search_flights, …). Every argument is checked before it leaves: closed types only — airport codes, dates, "
        "numbers, a place name in Latin script — and a refusal says what to fix. A search takes minutes; for another "
        "view of a search already made, use its refine tool with the search_id instead of searching again.")
DOWN = "travel-ops is unavailable right now ({why}): its tools are missing from this run."


class LaneTravel:
    """travel-ops as the platform offers it to the room's sessions."""

    def __init__(self, url: str, http: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ops = travel.TravelOps(url, http)
        self.clock = clock
        self._offer: tuple[float, str, list[dict]] | None = None

    async def offer(self) -> tuple[str, list[dict]]:
        """What a run is told and the tools it is offered: travel-ops' own, those the forms check. Kept `LIST_S`; a
        travel-ops that did not answer is asked again after `DOWN_S`, so a run's start waits for it once at most."""
        if self._offer is not None and self.clock() - self._offer[0] < LIST_S:
            return self._offer[1], self._offer[2]
        try:
            init = await self.ops.ask("initialize", {"protocolVersion": travel.PROTOCOL, "capabilities": {},
                                                     "clientInfo": {"name": "retinue-platform", "version": "1"}},
                                      LIST_TIMEOUT)
            listed = await self.ops.ask("tools/list", {}, LIST_TIMEOUT)
        except travel.Unavailable as exc:
            self._offer = (self.clock() - LIST_S + DOWN_S, DOWN.format(why=str(exc).rstrip(".")), [])
            return self._offer[1], self._offer[2]
        tools = [t for t in listed.get("tools") or [] if isinstance(t, dict) and t.get("name") in travel.FORMS]
        rules = init.get("instructions")
        said = SAID + (f"\ntravel-ops' own rules: {rules}" if isinstance(rules, str) and rules.strip() else "")
        self._offer = (self.clock(), said, tools)
        return said, tools

    @staticmethod
    def has(name: str) -> bool:
        """A name the platform's server serves as travel-ops' tool: one the forms know, or one they refuse by name."""
        return name in travel.FORMS or name in travel.NOT_HERS

    async def call(self, name: str, arguments: dict) -> tuple[list[dict] | str, bool]:
        """travel-ops' answer to a call the gate let through: (content, is_error). Refused before it leaves when its
        form does not hold."""
        try:
            clean = travel.exact(name, arguments)
            result = await self.ops.result(name, clean, CALL_TIMEOUT)
        except (travel.Refused, travel.Unavailable) as exc:
            return str(exc), True
        content = [c for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") in CONTENT]
        return content or "travel-ops answered nothing.", False
