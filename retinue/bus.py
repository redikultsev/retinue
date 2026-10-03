"""Agent bus: agents ask each other only through the router, and the router decides in code.

An agent gets a `turn` id with every request the router sends it. To ask another agent it calls the bus with
that turn id and its own bus token. The router knows which owner message the turn belongs to (the tree), how
deep the chain already is and which agents' answers the chain has seen, and applies the rules below. The model
never decides whether a call is allowed.

Rules (architecture.md §15):
- the caller may call the target (`can_call` grant);
- no loops (the target is not already in the chain), depth at most MAX_DEPTH, MAX_CALLS per owner message,
  at most MAX_PARALLEL bus calls at once;
- trust classes: free text never goes from an agent with the base to one with the web, nor back; once a
  chain has read a web agent's answer it cannot reach an agent with the base. Typed skills between the
  classes come later, with schemas.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass, field

from aiohttp import web

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


def check(caller: RouterAgent, target: RouterAgent | None, turn: Turn) -> None:
    """Raise Denied with a reason the caller (and the owner) can read."""
    if target is None:
        raise Denied("такого агента нет")
    if target.id not in caller.can_call and "*" not in caller.can_call:
        raise Denied(f"агенту {caller.name} не разрешено обращаться к агенту {target.name}")
    if target.id in turn.chain:
        raise Denied("петля: этот агент уже участвует в цепочке")
    if turn.depth + 1 > MAX_DEPTH:
        raise Denied(f"цепочка глубже {MAX_DEPTH} не допускается")
    if turn.tree.calls >= MAX_CALLS:
        raise Denied(f"на одно сообщение Владельца не больше {MAX_CALLS} обращений")
    if caller.trust_class == "web" and target.trust_class == "private":
        raise Denied("агент с вебом не пишет свободным текстом агенту с базой")
    if caller.trust_class == "private" and target.trust_class == "web":
        raise Denied("агент с базой не пишет свободным текстом агенту с вебом: нужен типизированный навык")
    if turn.tree.tainted and target.trust_class == "private":
        raise Denied("в цепочке уже был ответ агента с вебом, к агенту с базой она не пойдёт")


class BusServer:
    """HTTP endpoint on the agents network: GET /agents, POST /call. Authenticated by per-agent token."""

    def __init__(self, core, secret: str, port: int) -> None:
        self.core = core
        self.port = port
        self.tokens = {bus_token(secret, a): a for a in core.agents}
        self.app = web.Application()
        self.app.add_routes([web.get("/agents", self.list_agents), web.post("/call", self.call)])

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

    async def list_agents(self, request: web.Request) -> web.Response:
        caller = self.caller(request)
        allowed = [a for a in self.core.agents.values()
                   if a.id != caller.id and (a.id in caller.can_call or "*" in caller.can_call)]
        return web.json_response([{"id": a.id, "name": a.name, "description": a.description,
                                   "trust_class": a.trust_class} for a in allowed])

    async def call(self, request: web.Request) -> web.Response:
        caller = self.caller(request)
        body = await request.json()
        ok, text = await self.core.bus_call(caller, str(body.get("turn", "")), str(body.get("agent", "")),
                                            str(body.get("text", "")))
        return web.json_response({"ok": ok, "text": text})
