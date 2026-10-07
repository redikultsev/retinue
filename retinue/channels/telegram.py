"""Telegram channel: one bot, one private chat with the owner, one stream. No topics, no drafts.

Bot API over plain HTTP (long polling), no framework. Only the owner's user id is served. Everything the owner
reads leaves through `_message`: sendMessage or editMessageText with the link preview switched off. No other
method carries text, and no call ever has a `url` field.

Files the owner sends are downloaded here (getFile, up to 20 MB) and handed to the core as they are; reading them
is the core's work. A file URL carries the bot token: it is never logged and never put into an error text.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..attachments import MAX_DOWNLOAD, Upload, too_big
from ..config import TelegramConfig
from ..core import AgentFile, Core
from ..protocol import Store
from ..render import render_telegram, tg_plain

log = logging.getLogger("retinue.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
NO_PREVIEW = {"is_disabled": True}
SLASH_COMMANDS = {"/new": "!new", "/compact": "!compact", "/check": "!check"}
# Files a message may carry; the core reads them. A GIF (animation) also carries `document`: it is checked first.
MEDIA = ("photo", "document", "voice", "audio", "video", "video_note")
# What is still refused aloud: the value finishes the phrase «Пока не умею принимать …».
UNSUPPORTED = {"animation": "гифки", "poll": "опросы", "dice": "кубики", "game": "игры", "story": "истории"}
LOST = "Сообщение не обработано: ошибка на стороне Роутера. Повтори его, пожалуйста."
POISON = ("Сообщение с файлом не обработано: пока Роутер его разбирал, он перезапустился. Пришли файл ещё раз — "
          "лучше в другом формате.")
START = ("Пиши сюда — ответит ассистентка. Команды: /new — новый разговор, /compact — сжать разговор, "
         "/check — проверка канала, /help — справка.")


class TelegramError(Exception):
    pass


class TelegramChannel:
    name = "telegram"
    is_record = False
    typing_refresh_s = 4.0  # a chat action lasts 5 s

    def __init__(self, cfg: TelegramConfig, store: Store, link_hosts: list[str] | tuple = ()) -> None:
        self.cfg = cfg
        self.store = store
        self.core: Core | None = None
        self.link_hosts = tuple(link_hosts)  # travel-ops' sites: a link there is clickable (render.linkable)
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
            {"command": "compact", "description": "сжать разговор"},
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
        the offset moves on. A message with a file is marked before it is touched: if the process dies on it (a file
        that swells the router), the second delivery is refused aloud instead of killing the router again.
        """
        message = update.get("message") or {}
        if any(key in message for key in MEDIA):
            if self.store.get("telegram.attempt") == str(update["update_id"]):
                log.warning("update %s: the previous process died on it; refused", update["update_id"])
                try:
                    await self.core.tell_owner(POISON, origin=self)
                except Exception:
                    log.exception("could not report the refused update %s", update["update_id"])
                self.store.set("telegram.offset", str(update["update_id"] + 1))
                return
            self.store.set("telegram.attempt", str(update["update_id"]))
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
        text = (message.get("text") or "").strip() or self._as_text(message)
        forwarded_from = self._forwarded_from(message)
        reply = message.get("reply_to_message") or {}
        reply_to = str(reply["message_id"]) if "message_id" in reply else None
        if not text:
            if "animation" not in message and any(key in message for key in MEDIA):
                await self.core.receive(self, message.get("caption") or "", [self._upload(message)],
                                        native_id=native_id, reply_to=reply_to, forwarded_from=forwarded_from,
                                        group=message.get("media_group_id"))
                return
            what = next((name for key, name in UNSUPPORTED.items() if key in message), "такие сообщения")
            await self.core.unsupported(self, what, native_id=native_id, caption=message.get("caption") or "")
            return
        command = text.split()[0].split("@")[0].lower()
        if not forwarded_from:  # somebody else's text is data: «/new» inside it is not a command
            if command in ("/start", "/help"):  # the core's help names «!» commands; Telegram uses «/»
                await self.core.tell_owner(START, origin=self)
                return
            if command in SLASH_COMMANDS:
                text = SLASH_COMMANDS[command] + text[len(text.split()[0]):]
        # One chat, one assistant: no address, the core picks the default agent.
        await self.core.handle(self, None, text, native_id=native_id, forwarded_from=forwarded_from,
                               reply_to=reply_to)

    @staticmethod
    def _as_text(message: dict) -> str:
        """A sticker, a place or a contact is said in words: the model needs no file for them."""
        if sticker := message.get("sticker"):
            return f"[стикер {sticker.get('emoji') or ''}]".replace(" ]", "]")
        if venue := message.get("venue"):
            place = venue.get("location") or {}
            return (f"[место: {venue.get('title', '')}, {venue.get('address', '')} "
                    f"({place.get('latitude')}, {place.get('longitude')})]")
        if place := message.get("location"):
            return f"[геопозиция: {place.get('latitude')}, {place.get('longitude')}]"
        if contact := message.get("contact"):
            name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")]))
            return f"[контакт: {name}, {contact.get('phone_number', '')}]"
        return ""

    def _upload(self, message: dict) -> Upload:
        """The file of a message as the core takes it: not downloaded yet — the core fetches it when it reads it,
        so an album is gathered without waiting for downloads. Too big for the Bot API: the reason instead of a
        download, said to the owner by the core."""
        kind = next(key for key in MEDIA if key in message)
        item = message[kind][-1] if kind == "photo" else message[kind]  # a photo comes in sizes, the largest last
        upload = Upload(kind, name=item.get("file_name", ""), media_type=item.get("mime_type", ""),
                        duration=int(item.get("duration") or 0), size=int(item.get("file_size") or 0))
        if upload.size > MAX_DOWNLOAD:
            upload.refused = too_big(upload)
            return upload

        async def fetch() -> bytes:
            try:
                return await self.download(item["file_id"])
            except TelegramError:
                raise
            except Exception as exc:  # worded by its type only: a Bot API address carries the token
                raise TelegramError(type(exc).__name__) from None

        upload.fetch = fetch
        return upload

    async def download(self, file_id: str) -> bytes:
        path = (await self.call("getFile", file_id=file_id))["file_path"]
        try:
            response = await self.http.get(f"{API}/file/bot{self.cfg.bot_token}/{path}")
        except httpx.HTTPError as exc:
            raise TelegramError(f"file: {type(exc).__name__}") from None  # its text may hold the URL, and the token
        if response.status_code != 200:  # not raise_for_status(): its message carries the URL
            raise TelegramError(f"file: {response.status_code}")
        return response.content

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
        for html in render_telegram(text, self.link_hosts):
            await self._message(html, ref=ref)
        for f in files:
            await self.call("sendDocument", files={"document": (f.name, f.data, f.media_type)},
                            chat_id=str(self.cfg.owner_id))

    async def mirror(self, agent_id: str, origin: str, text: str) -> None:
        pass  # Telegram is not the record of other channels

    async def notice(self, agent_id: str, text: str, buttons: list[tuple[str, str]] | None = None,
                     ref: str | None = None) -> None:
        parts = render_telegram(text, self.link_hosts)
        # callback_data is the button id and nothing else: Telegram allows 64 bytes, and the meaning stays with us.
        keyboard = {"inline_keyboard": [[{"text": label, "callback_data": button_id} for label, button_id in buttons]]} \
            if buttons else None
        for i, html in enumerate(parts):
            await self._message(html, ref=ref, keyboard=keyboard if i == len(parts) - 1 else None)

    async def protocol(self, line: str) -> None:
        pass  # the protocol lives in the Store

    async def trace(self, agent_id: str, tree_id: str, text: str) -> None:
        pass  # agents' conversation is not shown here
