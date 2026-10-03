"""Router: the only process that holds the Matrix token.

Each agent appears in Matrix as a virtual user of this appservice and gets its own room.
The owner writes in an agent's room; the router forwards the text to that agent over A2A
and posts the answer back as the agent. Agents never touch Matrix themselves.

Pilot scope: owner <-> agent only. Agent <-> agent requests, labels and budgets come next.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_message_text, get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest
from mautrix.appservice import AppService
from mautrix.appservice.state_store import FileASStateStore
from mautrix.types import EventType, MessageEvent, MessageType, RoomID, UserID

from .config import RouterAgent, RouterConfig
from .protocol import Store
from .render import render

log = logging.getLogger("retinue.router")
PROTOCOL = "_protocol"
MAX_INPUT_CHARS = 20_000
# An agent turn with web search takes minutes; the A2A client default timeout is seconds.
AGENT_TIMEOUT = httpx.Timeout(900, connect=10)
TYPING_REFRESH_S = 25


async def ask_agent(url: str, text: str, context_id: str) -> tuple[str, str]:
    """Send one owner message to an agent over A2A; return (status, answer text)."""
    status, parts = "error", []
    http = httpx.AsyncClient(timeout=AGENT_TIMEOUT)
    client = await create_client(agent=url, client_config=ClientConfig(streaming=False, httpx_client=http))
    try:
        request = SendMessageRequest(message=new_text_message(text, context_id=context_id, role=Role.ROLE_USER))
        async for response in client.send_message(request):
            chunk = get_stream_response_text(response)
            if response.HasField("task"):
                task = response.task
                status = {3: "done", 4: "failed", 7: "rejected"}.get(int(task.status.state), status)
                if not chunk and task.status.HasField("message"):
                    chunk = get_message_text(task.status.message)
            if chunk:
                parts.append(chunk)
    finally:
        await client.close()
        await http.aclose()
    return status, parts[-1] if parts else ""


class Router:
    def __init__(self, cfg: RouterConfig, loop: asyncio.AbstractEventLoop) -> None:
        self.cfg = cfg
        self.store = Store(cfg.state_db)
        self.agents = {a.id: a for a in cfg.agents}
        self.az = AppService(
            server=cfg.homeserver,
            domain=cfg.server_name,
            as_token=cfg.as_token,
            hs_token=cfg.hs_token,
            bot_localpart=cfg.bot_localpart,
            id=cfg.appservice_id,
            state_store=FileASStateStore(path=f"{cfg.state_db}.mx-state.json", binary=False),
            loop=loop,
        )
        self.az.matrix_event_handler(self.on_event)

    # --- setup -------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.az.start(host=self.cfg.listen_host, port=self.cfg.listen_port)
        await self.az.intent.ensure_registered()
        await self.az.intent.set_displayname("Retinue")
        for agent in self.cfg.agents:
            await self.ensure_agent_room(agent)
        await self.ensure_protocol_room()
        self.az.ready = True
        log.info("router ready: %d agents", len(self.cfg.agents))

    async def ensure_agent_room(self, agent: RouterAgent) -> None:
        intent = self.az.intent.user(UserID(self.cfg.agent_mxid(agent.id)))
        await intent.ensure_registered()
        await intent.set_displayname(agent.name)
        if self.store.room(agent.id):
            return
        room_id = await intent.create_room(
            name=agent.name, topic=agent.topic or None, invitees=[UserID(self.cfg.owner)], is_direct=False,
        )
        self.store.save_room(agent.id, room_id, f"room-{uuid.uuid4()}")
        log.info("created room %s for agent %s", room_id, agent.id)

    async def ensure_protocol_room(self) -> None:
        if self.store.room(PROTOCOL):
            return
        room_id = await self.az.intent.create_room(
            name="Протокол", topic="Все обмены через Роутер", invitees=[UserID(self.cfg.owner)],
        )
        self.store.save_room(PROTOCOL, room_id, PROTOCOL)

    # --- events ------------------------------------------------------------------------------

    async def on_event(self, event) -> None:
        if event.type != EventType.ROOM_MESSAGE or not isinstance(event, MessageEvent):
            return
        # Commands come only from the owner. Everything else is ignored, including other bots.
        if event.sender != self.cfg.owner:
            return
        if event.content.msgtype not in (MessageType.TEXT,):
            return
        found = self.store.agent_by_room(event.room_id)
        if not found or found[0] == PROTOCOL:
            return
        agent_id, context_id = found
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        asyncio.create_task(self.forward(agent, event.room_id, context_id, event.content.body))

    async def forward(self, agent: RouterAgent, room_id: RoomID, context_id: str, text: str) -> None:
        intent = self.az.intent.user(UserID(self.cfg.agent_mxid(agent.id)))
        text = text[:MAX_INPUT_CHARS]
        status, answer = "error", ""
        typing = asyncio.create_task(self.keep_typing(intent, room_id))
        try:
            status, answer = await ask_agent(agent.url, text, context_id)
        except Exception as exc:  # the owner sees the failure instead of silence
            log.exception("agent %s failed", agent.id)
            answer = f"Агент недоступен: {type(exc).__name__}"
        finally:
            typing.cancel()
            await intent.set_typing(room_id, timeout=0)
        body, html = render(answer)
        # m.text, not m.notice: clients grey out notices, and the router never reacts to agents anyway.
        await intent.send_text(room_id, text=body, html=html, msgtype=MessageType.TEXT)
        self.store.log(conversation_id=context_id, source=self.cfg.owner, target=agent.id, status=status,
                       input_chars=len(text), output_chars=len(answer))
        await self.post_protocol(f"{self.cfg.owner} → {agent.name}: {status}, {len(text)} → {len(answer)} знаков")

    @staticmethod
    async def keep_typing(intent, room_id: RoomID) -> None:
        """Clients drop a typing notice after its timeout, so refresh it while the agent works."""
        while True:
            await intent.set_typing(room_id, timeout=TYPING_REFRESH_S * 2 * 1000)
            await asyncio.sleep(TYPING_REFRESH_S)

    async def post_protocol(self, line: str) -> None:
        found = self.store.room(PROTOCOL)
        if found:
            await self.az.intent.send_text(RoomID(found[0]), text=line, msgtype=MessageType.NOTICE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retinue router")
    parser.add_argument("--config", default="/config/router.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The homeserver sends hs_token in the query string; the access log would write it to disk.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    router = Router(RouterConfig.load(args.config), loop)
    loop.run_until_complete(router.start())
    loop.run_forever()


if __name__ == "__main__":
    main()
