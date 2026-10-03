"""Router core: one conversation per agent, shared by every channel the owner talks through.

A channel adapter (Matrix, Telegram, ...) turns its messenger's events into `Core.handle(...)` calls and
shows the core's replies. The core knows nothing about messengers; agents know nothing about channels.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_message_text, get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest

from .config import RouterAgent
from .protocol import Store

log = logging.getLogger("retinue.core")

MAX_INPUT_CHARS = 20_000
# An agent turn with web search takes minutes; the A2A client default timeout is seconds.
AGENT_TIMEOUT = httpx.Timeout(900, connect=10)
HELP = "Команды: `!new` — новый разговор, `!compact` — сжать контекст, `!help` — эта справка."


@dataclass
class AgentFile:
    name: str
    media_type: str
    data: bytes


class Channel(Protocol):
    """What the core needs from a messenger. Adapters check that a message comes from the owner."""

    name: str
    is_record: bool          # keeps the full record: messages sent through other channels are mirrored here
    typing_refresh_s: float  # how often the core repeats `typing` while an agent works

    async def start(self, core: Core) -> None: ...
    async def typing(self, agent_id: str, active: bool) -> None: ...
    async def send(self, agent_id: str, text: str, files: list[AgentFile]) -> None: ...
    async def mirror(self, agent_id: str, origin: str, text: str) -> None: ...
    async def notice(self, agent_id: str, text: str) -> None: ...
    async def protocol(self, line: str) -> None: ...


async def ask_agent(url: str, text: str, context_id: str) -> tuple[str, str, list[AgentFile]]:
    """Send one owner message to an agent over A2A; return (status, answer text, attached files)."""
    status, parts, files = "error", [], []
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
                files = [AgentFile(p.filename or "file", p.media_type or "application/octet-stream", p.raw)
                         for a in task.artifacts for p in a.parts if p.raw]
            if chunk:
                parts.append(chunk)
    finally:
        await client.close()
        await http.aclose()
    return status, parts[-1] if parts else "", files


class Core:
    def __init__(self, agents: list[RouterAgent], store: Store, owner: str, ask=ask_agent) -> None:
        self.agents = {a.id: a for a in agents}
        self.store = store
        self.owner = owner
        self.ask = ask
        self.channels: list[Channel] = []

    async def start(self, channels: list[Channel]) -> None:
        self.channels = channels
        for agent_id in self.agents:
            self.store.conversation(agent_id)
        for channel in channels:
            await channel.start(self)
        log.info("router ready: %d agents, channels: %s", len(self.agents), ", ".join(c.name for c in channels))

    async def handle(self, origin: Channel, agent_id: str, text: str) -> None:
        """Entry point for an owner message that an adapter has already authenticated."""
        agent = self.agents.get(agent_id)
        text = text.strip()
        if agent is None or not text:
            return
        if text.startswith("!"):
            await self.command(origin, agent, text)
            return
        asyncio.create_task(self.forward(origin, agent, text))

    async def command(self, origin: Channel, agent: RouterAgent, text: str) -> None:
        """Room commands, handled by the core itself; the agent never sees them."""
        name = text.split()[0].lower()
        if name == "!new":
            context_id = self.store.new_conversation(agent.id)
            self.store.log(conversation_id=context_id, source=self.owner, target=agent.id, status="new",
                           input_chars=0, output_chars=0, channel=origin.name)
            await self._each(self._audience(origin), "notice", agent.id,
                             "Новый разговор. Прошлый контекст агент больше не видит.")
        elif name == "!compact":
            # Claude Code compacts the session on the /compact slash command and keeps the same session id.
            asyncio.create_task(self.forward(origin, agent, "/compact", shown=text, done_text="Контекст сжат."))
        else:
            await origin.notice(agent.id, HELP)

    async def forward(self, origin: Channel, agent: RouterAgent, text: str, *, shown: str | None = None,
                      done_text: str = "") -> None:
        text = text[:MAX_INPUT_CHARS]
        context_id = self.store.conversation(agent.id)
        records = [c for c in self.channels if c is not origin and c.is_record]
        await self._each(records, "mirror", agent.id, origin.name, shown or text)
        typing = asyncio.create_task(self._keep_typing(origin, agent.id))
        status, answer, files = "error", "", []
        try:
            status, answer, files = await self.ask(agent.url, text, context_id)
        except Exception as exc:  # the owner sees the failure instead of silence
            log.exception("agent %s failed", agent.id)
            answer = f"Агент недоступен: {type(exc).__name__}"
        finally:
            typing.cancel()
            await self._each([origin], "typing", agent.id, False)
        if status == "done" and not answer.strip() and done_text:
            answer = done_text
        await self._each([origin, *records], "send", agent.id, answer, files)
        self.store.log(conversation_id=context_id, source=self.owner, target=agent.id, status=status,
                       input_chars=len(text), output_chars=len(answer), channel=origin.name)
        line = (f"{origin.name}: {self.owner} → {agent.name}: {status}, {len(text)} → {len(answer)} знаков"
                + (f", файлов: {len(files)}" if files else ""))
        await self._each(self.channels, "protocol", line)

    def _audience(self, origin: Channel) -> list[Channel]:
        return [origin, *(c for c in self.channels if c is not origin and c.is_record)]

    async def _keep_typing(self, channel: Channel, agent_id: str) -> None:
        while True:
            await self._each([channel], "typing", agent_id, True)
            await asyncio.sleep(channel.typing_refresh_s)

    @staticmethod
    async def _each(channels: list[Channel], method: str, *args) -> None:
        # One broken channel must not stop the reply from reaching the others.
        for channel in channels:
            try:
                await getattr(channel, method)(*args)
            except Exception:
                log.exception("channel %s: %s failed", channel.name, method)

