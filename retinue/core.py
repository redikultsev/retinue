"""Router core: one conversation per agent, shared by every channel the owner talks through.

A channel adapter (Matrix, Telegram, ...) turns its messenger's events into `Core.handle(...)` calls and
shows the core's replies. The core knows nothing about messengers; agents know nothing about channels.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_message_text, new_text_message
from a2a.types import Role, SendMessageRequest

from .bus import MAX_PARALLEL, MAX_TEXT, Denied, Turns, check
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
    async def draft(self, agent_id: str, text: str) -> None: ...  # partial reply; may ignore
    async def send(self, agent_id: str, text: str, files: list[AgentFile]) -> None: ...
    async def mirror(self, agent_id: str, origin: str, text: str) -> None: ...
    async def notice(self, agent_id: str, text: str) -> None: ...
    async def protocol(self, line: str) -> None: ...
    async def trace(self, agent_id: str, tree_id: str, text: str) -> None: ...  # agents talking, under agent_id


# Called with the agent's partial reply while it is being written.
OnProgress = Callable[[str], Awaitable[None]]
STATES = {3: "done", 4: "failed", 7: "rejected"}


TURN_KEY = "retinue/turn"  # message metadata: the turn id an agent passes back when it uses the bus


async def ask_agent(url: str, text: str, context_id: str, on_progress: OnProgress | None = None,
                    turn_id: str | None = None) -> tuple[str, str, list[AgentFile]]:
    """Send one owner message to an agent over A2A (streaming); return (status, answer text, attached files).

    WORKING status messages carry the partial reply; artifacts carry the final answer and files.
    """
    status, answer, status_text, files = "error", "", "", []
    http = httpx.AsyncClient(timeout=AGENT_TIMEOUT)
    client = await create_client(agent=url, client_config=ClientConfig(streaming=True, httpx_client=http))

    def take_artifact(artifact) -> None:
        nonlocal answer
        for part in artifact.parts:
            if part.raw:
                files.append(AgentFile(part.filename or "file", part.media_type or "application/octet-stream",
                                       part.raw))
            elif part.text:
                answer = part.text

    try:
        message = new_text_message(text, context_id=context_id, role=Role.ROLE_USER)
        if turn_id:
            message.metadata.update({TURN_KEY: turn_id})
        async for response in client.send_message(SendMessageRequest(message=message)):
            if response.HasField("status_update"):
                state = response.status_update.status
                message_text = get_message_text(state.message) if state.HasField("message") else ""
                if int(state.state) == 2:  # WORKING
                    if message_text and on_progress:
                        await on_progress(message_text)
                else:
                    status = STATES.get(int(state.state), status)
                    status_text = message_text or status_text
            elif response.HasField("artifact_update"):
                take_artifact(response.artifact_update.artifact)
            elif response.HasField("task"):  # a non-streaming agent answers with the whole task
                task = response.task
                status = STATES.get(int(task.status.state), status)
                if task.status.HasField("message"):
                    status_text = get_message_text(task.status.message)
                for artifact in task.artifacts:
                    take_artifact(artifact)
            elif response.HasField("message"):
                status, answer = "done", get_message_text(response.message)
    finally:
        await client.close()
        await http.aclose()
    return status, answer or status_text, files


class Core:
    def __init__(self, agents: list[RouterAgent], store: Store, owner: str, ask=ask_agent) -> None:
        self.agents = {a.id: a for a in agents}
        self.store = store
        self.owner = owner
        self.ask = ask
        self.channels: list[Channel] = []
        self.turns = Turns()
        self.bus_slots = asyncio.Semaphore(MAX_PARALLEL)

    async def start(self, channels: list[Channel]) -> None:
        for agent_id in self.agents:
            self.store.conversation(agent_id)
        for channel in channels:
            try:
                await channel.start(self)
            except Exception:  # e.g. Telegram unreachable: the other channels keep working
                log.exception("channel %s failed to start", channel.name)
                continue
            self.channels.append(channel)
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
        turn = self.turns.open_root(agent.id)
        status, answer, files = "error", "", []
        try:
            status, answer, files = await self.ask(agent.url, text, context_id,
                                                   lambda partial: self._each([origin], "draft", agent.id, partial),
                                                   turn.id)
        except Exception as exc:  # the owner sees the failure instead of silence
            log.exception("agent %s failed", agent.id)
            answer = f"Агент недоступен: {type(exc).__name__}"
        finally:
            self.turns.close(turn)
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

    async def bus_call(self, caller: RouterAgent, turn_id: str, target_id: str, text: str) -> tuple[bool, str]:
        """An agent asks another agent. The caller is authenticated by its bus token; the rest is checked here."""
        turn = self.turns.turns.get(turn_id)
        if turn is None or turn.agent_id != caller.id:
            return False, "Нет активного запроса: обращаться к агентам можно только во время ответа."
        target = self.agents.get(target_id)
        tree = turn.tree
        try:
            check(caller, target, turn)
        except Denied as exc:
            await self._trace(tree, f"⛔ **{caller.name} → {target.name if target else target_id}:** отказано — {exc}")
            self.store.log(conversation_id=f"tree-{tree.id}", source=caller.id, target=target_id, status="denied",
                           input_chars=len(text), output_chars=0, channel="bus")
            return False, f"Отказано: {exc}."
        tree.calls += 1
        text = text[:MAX_TEXT]
        await self._trace(tree, f"**{caller.name} → {target.name}:**\n\n{text}", target.id)
        child = self.turns.open_child(turn, target.id)
        status, answer = "error", ""
        try:
            async with self.bus_slots:
                # The target's own conversation: one agent, one memory, whoever asks.
                status, answer, files = await self.ask(target.url, f"[Вопрос от агента «{caller.name}»]\n\n{text}",
                                                       self.store.conversation(target.id), None, child.id)
            if files:
                answer += f"\n\n(файлы агента не переданы: {', '.join(f.name for f in files)})"
        except Exception as exc:
            log.exception("bus call %s -> %s failed", caller.id, target.id)
            answer = f"Агент недоступен: {type(exc).__name__}"
        finally:
            self.turns.close(child)
        tree.tainted |= target.trust_class == "web"
        tree.private |= target.trust_class == "private"
        await self._trace(tree, f"**{target.name} → {caller.name}** ({status}):\n\n{answer}", target.id)
        self.store.log(conversation_id=f"tree-{tree.id}", source=caller.id, target=target.id, status=status,
                       input_chars=len(text), output_chars=len(answer), channel="bus")
        await self._each(self.channels, "protocol",
                         f"bus: {caller.name} → {target.name}: {status}, {len(text)} → {len(answer)} знаков")
        return status == "done", answer

    async def _trace(self, tree, text: str, target_id: str | None = None) -> None:
        """Show agents talking in the owner's room and in the room of the agent being asked."""
        for agent_id in dict.fromkeys(a for a in (tree.root_agent, target_id) if a):
            await self._each(self.channels, "trace", agent_id, tree.id, text)

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

