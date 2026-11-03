"""Agent bus: agents ask each other only through the router, and the router decides in code.

An agent gets a `turn` id with every request the router sends it. To ask another agent it calls the bus with
that turn id and its own bus token. The router knows which owner message the turn belongs to (the tree), how
deep the chain already is and which agents' answers the chain has seen, and applies the rules below. The model
never decides whether a call is allowed.

Rules:
- the caller may call the target (`can_call` grant);
- no loops (the target is not already in the chain), depth at most MAX_DEPTH, MAX_CALLS per owner message,
  at most MAX_PARALLEL bus calls at once;
- trust classes: free text never goes from an agent with the base to one with the web, nor back. A chain
  that has read a web agent's answer cannot reach an agent with the base, and a chain that has read an
  answer of an agent with the base cannot reach the web. Typed skills between the classes come later.

A bus call runs in the target's own conversation (one agent, one memory), so the owner sees it in the
target's room as well. That is why the second taint rule matters: an agent's memory travels with its answers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass, field

from aiohttp import web

from . import tools
from .config import RouterAgent

log = logging.getLogger("retinue.bus")

MAX_DEPTH = 2
MAX_CALLS = 6
MAX_PARALLEL = 3
MAX_TEXT = 8_000


def bus_token(secret: str, agent_id: str) -> str:
    """Per-agent bus token, derived so that setup only has to keep one secret."""
    return hmac.new(secret.encode(), f"retinue-bus:{agent_id}".encode(), hashlib.sha256).hexdigest()


@dataclass
class Tree:
    """Everything that follows from one owner message."""
    id: str
    root_agent: str
    calls: int = 0
    tainted: bool = False  # the chain has read text written by an agent with the web
    private: bool = False  # the chain has read text written by an agent with the base
    foreign: set[str] = field(default_factory=set)  # someone else's text the turn read: forwarded, attachment, travel
    # The exchanges whose new messages started this run (`archive.exchange_of`): a chat, a mail thread. Empty for the
    # owner's own turn. «This conversation» for the search and the files is these; the rest is somebody else's.
    exchanges: set[str] = field(default_factory=set)
    asked: set[str] = field(default_factory=set)  # the archive ids of the new messages the run answers
    owner: bool = False  # the run answers the owner's own messages (`Core.turn`): his word may move his plan
    # What the run read, as code saw it: each search find, each file fetched, each read in the base, the other items
    # of a judgement — {id, exchange, source, closed}. A draft's card names what lies outside its exchange.
    reads: list[dict] = field(default_factory=list)


@dataclass
class Turn:
    id: str
    agent_id: str
    tree: Tree
    chain: tuple[str, ...] = field(default_factory=tuple)

    @property
    def depth(self) -> int:
        return len(self.chain) - 1


class Denied(Exception):
    pass


class Turns:
    def __init__(self) -> None:
        self.turns: dict[str, Turn] = {}

    def open_root(self, agent_id: str) -> Turn:
        return self._open(agent_id, Tree(id=uuid.uuid4().hex[:12], root_agent=agent_id), (agent_id,))

    def open_child(self, parent: Turn, agent_id: str) -> Turn:
        return self._open(agent_id, parent.tree, (*parent.chain, agent_id))

    def _open(self, agent_id: str, tree: Tree, chain: tuple[str, ...]) -> Turn:
        turn = Turn(id=uuid.uuid4().hex, agent_id=agent_id, tree=tree, chain=chain)
        self.turns[turn.id] = turn
        return turn

    def close(self, turn: Turn) -> None:
        self.turns.pop(turn.id, None)

    def of(self, agent_id: str, turn_id: str) -> Turn | None:
        """The agent's own open turn by its id, or None: everything an agent asks the router for is bound to one."""
        turn = self.turns.get(turn_id)
        return turn if turn is not None and turn.agent_id == agent_id else None


def check(caller: RouterAgent, target: RouterAgent | None, turn: Turn) -> None:
    """Raise Denied with a reason the caller (and the owner) can read."""
    if target is None:
        raise Denied("no such agent")
    if target.id not in caller.can_call and "*" not in caller.can_call:
        raise Denied(f"agent {caller.name} may not call agent {target.name}")
    if target.id in turn.chain:
        raise Denied("a loop: this agent is already in the chain")
    if turn.depth + 1 > MAX_DEPTH:
        raise Denied(f"a chain deeper than {MAX_DEPTH} is not allowed")
    if turn.tree.calls >= MAX_CALLS:
        raise Denied(f"at most {MAX_CALLS} calls per message from the owner")
    if caller.trust_class == "web" and target.trust_class == "private":
        raise Denied("an agent with the web does not write free text to an agent with the Base")
    if caller.trust_class == "private" and target.trust_class == "web":
        raise Denied("an agent with the Base does not write free text to an agent with the web: a typed skill is needed")
    if turn.tree.tainted and target.trust_class == "private":
        raise Denied("the chain already has an answer from an agent with the web; it will not go to an agent with the Base")
    if turn.tree.private and target.trust_class == "web":
        raise Denied("the chain already has an answer from an agent with the Base; it will not go to an agent with the web")


class BusServer:
    """HTTP endpoint on the agents network: POST /tools/<name> for every tool of `tools.TOOLS`, and the engine's guards'
    records, POST /travel/log and POST /kb/log. Authenticated by per-agent token."""

    def __init__(self, core, secret: str, port: int) -> None:
        self.core = core
        self.port = port
        self.tokens = {bus_token(secret, a): a for a in core.agents}
        self.app = web.Application()
        self.app.add_routes([web.post("/tools/{name}", self.tool),
                             web.post("/travel/log", self.travel_log),
                             web.post("/kb/log", self.kb_log)])

    async def start(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.port).start()
        log.info("bus on :%d", self.port)

    def caller(self, request: web.Request) -> RouterAgent:
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        for known, agent_id in self.tokens.items():
            if hmac.compare_digest(known, token):
                return self.core.agents[agent_id]
        raise web.HTTPUnauthorized()

    async def tool(self, request: web.Request) -> web.Response:
        caller = self.caller(request)
        name = request.match_info["name"]
        if name not in tools.TOOLS:
            raise web.HTTPNotFound()
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        answer = await tools.run(self.core, caller, str(body.get("turn") or ""), name, body.get("arguments"))
        said: dict = {"ok": answer.ok, "text": answer.text}
        if answer.images:
            said["images"] = [{"mimeType": media_type, "data": base64.b64encode(data).decode()}
                              for media_type, data in answer.images]
        return web.json_response(said)

    async def kb_log(self, request: web.Request) -> web.Response:
        caller = self.caller(request)
        body = await request.json()
        ok = await self.core.kb_log(caller, str(body.get("turn", "")), str(body.get("tool", "")),
                                    str(body.get("decision", "")), str(body.get("path", "")), str(body.get("reason", "")))
        return web.json_response({"ok": ok, "text": "" if ok else "No active request."})

    async def travel_log(self, request: web.Request) -> web.Response:
        caller = self.caller(request)
        body = await request.json()
        ok = await self.core.travel_log(caller, str(body.get("turn", "")), str(body.get("tool", "")),
                                        str(body.get("decision", "")), body.get("chars"), str(body.get("reason", "")))
        return web.json_response({"ok": ok, "text": "" if ok else "No active request."})
