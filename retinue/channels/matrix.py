"""Matrix channel: an appservice. Agents are its virtual users, one room per agent, plus a «Протокол» room.

The router holds the only Matrix token; agents never see Matrix. Matrix is the record channel: what the
owner writes through other channels is mirrored into the agent's room.
"""

from __future__ import annotations

import asyncio
import logging

from mautrix.appservice import AppService
from mautrix.appservice.state_store import FileASStateStore
from mautrix.types import EventType, FileInfo, MessageEvent, MessageType, RoomID, UserID

from ..config import RouterAgent, RouterConfig
from ..core import AgentFile, Core
from ..protocol import Store
from ..render import render

log = logging.getLogger("retinue.matrix")

PROTOCOL = "_protocol"
ORIGIN_LABELS = {"telegram": "📱 Telegram"}


class MatrixChannel:
    name = "matrix"
    is_record = True
    typing_refresh_s = 25

    def __init__(self, cfg: RouterConfig, store: Store, loop: asyncio.AbstractEventLoop) -> None:
        self.cfg = cfg
        self.store = store
        self.core: Core | None = None
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

    async def start(self, core: Core) -> None:
        self.core = core
        await self.az.start(host=self.cfg.listen_host, port=self.cfg.listen_port)
        await self.az.intent.ensure_registered()
        await self.az.intent.set_displayname("Retinue")
        for agent in self.cfg.agents:
            await self.ensure_agent_room(agent)
        await self.ensure_protocol_room()
        self.az.ready = True

    async def ensure_agent_room(self, agent: RouterAgent) -> None:
        intent = self.intent(agent.id)
        await intent.ensure_registered()
        await intent.set_displayname(agent.name)
        if self.store.room(agent.id):
            return
        room_id = await intent.create_room(
            name=agent.name, topic=agent.topic or None, invitees=[UserID(self.cfg.owner)], is_direct=False,
        )
        self.store.save_room(agent.id, room_id)
        log.info("created room %s for agent %s", room_id, agent.id)

    async def ensure_protocol_room(self) -> None:
        if self.store.room(PROTOCOL):
            return
        room_id = await self.az.intent.create_room(
            name="Протокол", topic="Все обмены через Роутер", invitees=[UserID(self.cfg.owner)],
        )
        self.store.save_room(PROTOCOL, room_id)

    def intent(self, agent_id: str):
        return self.az.intent.user(UserID(self.cfg.agent_mxid(agent_id)))

    # --- inbound -----------------------------------------------------------------------------

    async def on_event(self, event) -> None:
        if event.type != EventType.ROOM_MESSAGE or not isinstance(event, MessageEvent):
            return
        # Commands come only from the owner. Everything else is ignored, including other bots.
        if event.sender != self.cfg.owner or event.content.msgtype != MessageType.TEXT:
            return
        agent_id = self.store.agent_by_room(event.room_id)
        if agent_id and agent_id != PROTOCOL and self.core:
            await self.core.handle(self, agent_id, event.content.body)

    # --- outbound ----------------------------------------------------------------------------

    def _room(self, agent_id: str) -> RoomID | None:
        room_id = self.store.room(agent_id)
        return RoomID(room_id) if room_id else None

    async def typing(self, agent_id: str, active: bool) -> None:
        if room_id := self._room(agent_id):
            await self.intent(agent_id).set_typing(room_id, timeout=int(self.typing_refresh_s * 2000) if active else 0)

    async def send(self, agent_id: str, text: str, files: list[AgentFile]) -> None:
        room_id = self._room(agent_id)
        if room_id is None:
            return
        intent = self.intent(agent_id)
        body, html = render(text)
        # m.text, not m.notice: clients grey out notices, and the router never reacts to agents anyway.
        await intent.send_text(room_id, text=body, html=html, msgtype=MessageType.TEXT)
        for f in files:
            mxc = await intent.upload_media(f.data, mime_type=f.media_type, filename=f.name, size=len(f.data))
            await intent.send_file(room_id, mxc, info=FileInfo(mimetype=f.media_type, size=len(f.data)),
                                   file_name=f.name)

    async def mirror(self, agent_id: str, origin: str, text: str) -> None:
        # The router cannot post as the owner (and should not), so the owner's words arrive as a quote.
        if room_id := self._room(agent_id):
            label = ORIGIN_LABELS.get(origin, origin)
            body, html = render(f"**Владелец, {label}:**\n\n" + "\n".join(f"> {line}" for line in text.splitlines()))
            await self.intent(agent_id).send_text(room_id, text=body, html=html, msgtype=MessageType.NOTICE)

    async def notice(self, agent_id: str, text: str) -> None:
        if room_id := self._room(agent_id):
            body, html = render(text)
            await self.intent(agent_id).send_text(room_id, text=body, html=html, msgtype=MessageType.NOTICE)

    async def protocol(self, line: str) -> None:
        if room_id := self._room(PROTOCOL):
            await self.az.intent.send_text(room_id, text=line, msgtype=MessageType.NOTICE)
