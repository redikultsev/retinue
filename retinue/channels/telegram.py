"""Telegram channel: one bot, one private chat with the owner, one stream. No topics, no drafts.

Bot API over plain HTTP (long polling), no framework. Only the owner's user id is served. Everything the owner
reads leaves through `_message`: sendMessage or editMessageText with the link preview switched off. No other
method carries text, and no call ever has a `url` field.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import TelegramConfig
from ..core import AgentFile, Core
from ..protocol import Store
from ..render import render_telegram, tg_plain

log = logging.getLogger("retinue.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
NO_PREVIEW = {"is_disabled": True}
SLASH_COMMANDS = {"/new": "!new", "/check": "!check", "/help": "!help"}
# What a message may carry instead of text. None of it is handled yet; each is refused aloud: the value
# finishes the phrase «Пока не умею принимать …».
UNSUPPORTED = {"photo": "фото", "document": "файлы", "voice": "голосовые", "audio": "аудио", "video": "видео",
               "video_note": "видеосообщения", "sticker": "стикеры", "animation": "гифки", "contact": "контакты",
               "location": "геопозицию", "venue": "места", "poll": "опросы"}
LOST = "Сообщение не обработано: ошибка на стороне Роутера. Повтори его, пожалуйста."
START = "Пиши сюда — ответит ассистентка. Команды: /new — новый разговор, /check — проверка канала, /help — справка."


class TelegramError(Exception):
    pass


class TelegramChannel:
    name = "telegram"
    is_record = False
    typing_refresh_s = 4.0  # a chat action lasts 5 s

    def __init__(self, cfg: TelegramConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.core: Core | None = None
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
        await self.call("setMyCommands", commands=[
            {"command": "new", "description": "новый разговор"},
            {"command": "check", "description": "проверка канала"},
            {"command": "help", "description": "справка"},
        ])
        asyncio.create_task(self.poll())
        log.info("telegram @%s ready", me.get("username"))

    # --- inbound -----------------------------------------------------------------------------

    async def poll(self) -> None:
        while True:
            try:
                updates = await self.call("getUpdates", offset=int(self.store.get("telegram.offset") or 0),
                                          timeout=POLL_TIMEOUT_S, allowed_updates=["message", "callback_query"])
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                await self.consume(update)

    async def consume(self, update: dict) -> None:
        """Handle one update, then move the offset past it.

        The offset is saved after the handler, not before: if the router dies while handling, Telegram delivers
        the update again after the restart, and the core recognises a message it has already answered. A handler
        that fails does not replay forever either: the owner is told that the message was not processed, and
        the offset moves on.
        """
        try:
            await self.on_update(update)
        except Exception:
            log.exception("update %s failed", update.get("update_id"))
            try:
                await self.core.tell_owner(LOST, origin=self)
            except Exception:
                log.exception("could not report the failed update %s", update.get("update_id"))
        self.store.set("telegram.offset", str(update["update_id"] + 1))

    async def on_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
        else:
            await self.on_message(update.get("message"))

    async def on_callback(self, query: dict) -> None:
        """A button was pressed. Checked here: who pressed and in which chat. The callback data is only a button
        id; what the button means, whether it is still alive and whether it was used already, the core reads
        from its own tables."""
        message = query.get("message") or {}
        if not self.core or (query.get("from") or {}).get("id") != self.cfg.owner_id \
                or (message.get("chat") or {}).get("id") != self.cfg.owner_id:
            return
        pressed = await self.core.press(self, str(query.get("data") or ""))
        await self.call("answerCallbackQuery", callback_query_id=query["id"], text=pressed.toast)
        if pressed.card and "message_id" in message:
            # The card is rewritten without a keyboard: the buttons are gone, the choice stays visible.
            await self._message(render_telegram(pressed.card)[0], edit=message["message_id"])

    async def on_message(self, message: dict | None) -> None:
        if not message or not self.core:
            return
        sender = message.get("from") or {}
        # Commands come only from the owner, in the private chat with the bot. Everything else is ignored.
        if sender.get("id") != self.cfg.owner_id or message["chat"]["id"] != self.cfg.owner_id or sender.get("is_bot"):
            return
        native_id = str(message["message_id"])
        text = (message.get("text") or "").strip()
        if not text:
            what = next((name for key, name in UNSUPPORTED.items() if key in message), "такие сообщения")
            await self.core.unsupported(self, what, native_id=native_id, caption=message.get("caption") or "")
            return
        forwarded_from = self._forwarded_from(message)
        command = text.split()[0].split("@")[0].lower()
        if not forwarded_from:  # somebody else's text is data: «/new» inside it is not a command
            if command == "/start":
                await self.core.tell_owner(START, origin=self)
                return
            if command in SLASH_COMMANDS:
                text = SLASH_COMMANDS[command] + text[len(text.split()[0]):]
        reply = message.get("reply_to_message") or {}
        # One chat, one assistant: no address, the core picks the default agent.
        await self.core.handle(self, None, text, native_id=native_id, forwarded_from=forwarded_from,
                               reply_to=str(reply["message_id"]) if "message_id" in reply else None)

    def _forwarded_from(self, message: dict) -> str | None:
        """Who wrote a forwarded message, or None when the text is the owner's own."""
        origin = message.get("forward_origin")
        if not origin:
            return None
        user = origin.get("sender_user") or {}
        if user.get("id") == self.cfg.owner_id:
            return None  # the owner forwarded his own words
        name = (" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username")
                or origin.get("sender_user_name")
                or (origin.get("sender_chat") or {}).get("title") or (origin.get("chat") or {}).get("title"))
        return name or "неизвестный отправитель"

    # --- outbound ----------------------------------------------------------------------------

    async def _message(self, html: str, *, ref: str | None = None, keyboard: dict | None = None,
                       edit: int | None = None) -> None:
        """The one way text reaches the owner: a new message, or with `edit` a rewrite of an old one. If
        Telegram refuses the markup, the same text goes out with no markup except <code> around addresses —
        on that path too the preview is off and nothing is a link."""
        method = "editMessageText" if edit else "sendMessage"
        params = dict(chat_id=self.cfg.owner_id, message_id=edit, parse_mode="HTML", link_preview_options=NO_PREVIEW,
                      reply_markup=keyboard)
        try:
            sent = await self.call(method, text=html, **params)
        except TelegramError as exc:
            if "parse" not in str(exc):
                raise
            sent = await self.call(method, text=tg_plain(html), **params)
        if ref and sent.get("message_id"):
            self.store.save_sent(self.name, str(sent["message_id"]), ref)  # so a reply to it can be traced back

    async def typing(self, agent_id: str, active: bool) -> None:
        if active:  # a chat action expires by itself
            await self.call("sendChatAction", chat_id=self.cfg.owner_id, action="typing")

    async def draft(self, agent_id: str, text: str) -> None:
        pass  # no streaming: what sendMessageDraft does with link previews is not established

    async def send(self, agent_id: str, text: str, files: list[AgentFile], ref: str | None = None) -> None:
        for html in render_telegram(text):
            await self._message(html, ref=ref)
        for f in files:
            await self.call("sendDocument", files={"document": (f.name, f.data, f.media_type)},
                            chat_id=str(self.cfg.owner_id))

    async def mirror(self, agent_id: str, origin: str, text: str) -> None:
        pass  # Telegram is not the record of other channels

    async def notice(self, agent_id: str, text: str, buttons: list[tuple[str, str]] | None = None,
                     ref: str | None = None) -> None:
        parts = render_telegram(text)
        # callback_data is the button id and nothing else: Telegram allows 64 bytes, and the meaning stays with us.
        keyboard = {"inline_keyboard": [[{"text": label, "callback_data": button_id} for label, button_id in buttons]]} \
            if buttons else None
        for i, html in enumerate(parts):
            await self._message(html, ref=ref, keyboard=keyboard if i == len(parts) - 1 else None)

    async def protocol(self, line: str) -> None:
        pass  # the protocol lives in the Store

    async def trace(self, agent_id: str, tree_id: str, text: str) -> None:
        pass  # agents' conversation is not shown here
