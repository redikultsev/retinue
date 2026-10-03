"""Telegram channel: one bot, the owner's private chat split into topics — one topic per agent.

Bot API over plain HTTP (long polling), no framework: the adapter needs a handful of methods, including recent
ones (topics in private chats, message drafts) that frameworks add late. Only the owner's user id is served.
If the bot has no topic mode, the chat falls back to one stream: `@agent text` picks the agent, and the last
picked agent gets the following messages.
"""

from __future__ import annotations

import asyncio
import logging
import re
import zlib

import httpx

from ..config import RouterAgent, TelegramConfig
from ..core import AgentFile, Core
from ..protocol import Store
from ..render import render_telegram

log = logging.getLogger("retinue.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]
SLASH_COMMANDS = {"/new": "!new", "/compact": "!compact", "/help": "!help"}


class TelegramError(Exception):
    pass


class TelegramChannel:
    name = "telegram"
    is_record = False

    def __init__(self, cfg: TelegramConfig, agents: list[RouterAgent], store: Store) -> None:
        self.cfg = cfg
        self.agents = {a.id: a for a in agents}
        self.store = store
        self.core: Core | None = None
        self.topics = False
        self.typing_refresh_s = 4.0
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)

    # --- Bot API -----------------------------------------------------------------------------

    async def call(self, method: str, files: dict | None = None, **params) -> dict:
        url = f"{API}/bot{self.cfg.bot_token}/{method}"
        params = {k: v for k, v in params.items() if v is not None}
        response = await (self.http.post(url, data=params, files=files) if files else self.http.post(url, json=params))
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    # --- setup -------------------------------------------------------------------------------

    async def start(self, core: Core) -> None:
        self.core = core
        me = await self.call("getMe")
        self.topics = bool(me.get("has_topics_enabled"))
        # A draft preview lives 30 s; a chat action 5 s.
        self.typing_refresh_s = 20.0 if self.topics else 4.0
        await self.call("setMyCommands", commands=[
            {"command": "new", "description": "новый разговор с агентом этой темы"},
            {"command": "compact", "description": "сжать контекст"},
            {"command": "help", "description": "справка"},
        ])
        if self.topics:
            for i, agent in enumerate(self.agents.values()):
                await self.ensure_topic(agent, TOPIC_COLORS[i % len(TOPIC_COLORS)])
        else:
            log.warning("@%s has no topic mode in private chats: enable it in @BotFather; using one stream",
                        me.get("username"))
        asyncio.create_task(self.poll())
        log.info("telegram @%s ready, topics: %s", me.get("username"), self.topics)

    async def ensure_topic(self, agent: RouterAgent, color: int) -> None:
        if self.store.place(self.name, agent.id):
            return
        topic = await self.call("createForumTopic", chat_id=self.cfg.owner_id, name=agent.name, icon_color=color)
        self.store.save_place(self.name, agent.id, str(topic["message_thread_id"]))
        log.info("created topic %s for agent %s", topic["message_thread_id"], agent.id)

    # --- inbound -----------------------------------------------------------------------------

    async def poll(self) -> None:
        offset = int(self.store.get("telegram.offset") or 0)
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT_S,
                                          allowed_updates=["message"])
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                # Saved before handling: a message that crashes the handler must not replay forever.
                self.store.set("telegram.offset", str(offset))
                try:
                    await self.on_message(update.get("message"))
                except Exception:
                    log.exception("update %s failed", update["update_id"])

    async def on_message(self, message: dict | None) -> None:
        if not message or not self.core:
            return
        sender = message.get("from") or {}
        # Commands come only from the owner, in the private chat with the bot. Everything else is ignored.
        if sender.get("id") != self.cfg.owner_id or message["chat"]["id"] != self.cfg.owner_id or sender.get("is_bot"):
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        command = text.split()[0].split("@")[0].lower()
        if command == "/start":
            await self.send_html(None, "Пиши в тему агента. В общем чате — <code>@агент текст</code>.")
            return
        text = SLASH_COMMANDS.get(command, command) + text[len(text.split()[0]):] if command in SLASH_COMMANDS else text
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None
        agent_id = self.store.agent_by_place(self.name, str(thread)) if thread else None
        if agent_id is None:
            agent_id, text = self.addressed(text)
        if agent_id is None:
            names = ", ".join(f"@{a.id}" for a in self.agents.values())
            await self.send_html(None, f"Кому? Напиши в тему агента или начни с имени: {names}.")
            return
        if not self.topics:
            self.store.set("telegram.last_agent", agent_id)
        await self.core.handle(self, agent_id, text)

    def addressed(self, text: str) -> tuple[str | None, str]:
        """`@travel текст` or `@путешествия текст` picks the agent; without topics the last one is kept."""
        if match := re.match(r"@(\S+)\s*(.*)", text, re.S):
            key = match.group(1).lower()
            for agent in self.agents.values():
                if key in (agent.id.lower(), agent.name.lower()):
                    return agent.id, match.group(2)
        if not self.topics:
            return self.store.get("telegram.last_agent"), text
        return None, text

    # --- outbound ----------------------------------------------------------------------------

    def _thread(self, agent_id: str) -> int | None:
        place = self.store.place(self.name, agent_id) if self.topics else None
        return int(place) if place else None

    async def send_html(self, agent_id: str | None, html: str) -> None:
        thread = self._thread(agent_id) if agent_id else None
        try:
            await self.call("sendMessage", chat_id=self.cfg.owner_id, message_thread_id=thread, text=html,
                            parse_mode="HTML", link_preview_options={"is_disabled": True})
        except TelegramError as exc:
            if "parse" not in str(exc):
                raise
            plain = re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            await self.call("sendMessage", chat_id=self.cfg.owner_id, message_thread_id=thread, text=plain)

    async def typing(self, agent_id: str, active: bool) -> None:
        if not active:
            return  # the final message replaces the draft; a chat action expires by itself
        if self.topics:
            # An empty draft shows Telegram's own «Thinking…» placeholder in the agent's topic.
            await self.call("sendMessageDraft", chat_id=self.cfg.owner_id, message_thread_id=self._thread(agent_id),
                            draft_id=zlib.crc32(agent_id.encode()) or 1, text="")
        else:
            await self.call("sendChatAction", chat_id=self.cfg.owner_id, action="typing")

    async def send(self, agent_id: str, text: str, files: list[AgentFile]) -> None:
        prefix = "" if self.topics else f"<b>{self.agents[agent_id].name}</b>\n\n"
        for i, html in enumerate(render_telegram(text)):
            await self.send_html(agent_id, (prefix if i == 0 else "") + html)
        for f in files:
            await self.call("sendDocument", files={"document": (f.name, f.data, f.media_type)},
                            chat_id=str(self.cfg.owner_id), message_thread_id=self._thread(agent_id))

    async def mirror(self, agent_id: str, origin: str, text: str) -> None:
        pass  # Telegram is the quick window, not the record

    async def notice(self, agent_id: str, text: str) -> None:
        prefix = "" if self.topics else f"<b>{self.agents[agent_id].name}</b>: "
        await self.send_html(agent_id, prefix + "<i>" + "\n\n".join(render_telegram(text)) + "</i>")

    async def protocol(self, line: str) -> None:
        pass  # the protocol lives in Matrix and in the Store
