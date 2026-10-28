"""Telegram channel: one bot, one private chat with the owner, one stream. No topics, no drafts.

Bot API over plain HTTP (long polling), no framework. Only the owner's user id is served. Everything the owner
reads leaves through `_message`: sendMessage or editMessageText with the link preview switched off. No other
method carries text, and no call ever has a `url` field — but for the Mini App buttons of the platform's messages (a
card's, a handed-over page's), whose address is the face's own, from the router's config (`bridge.PlatformBridge`).

Files the owner sends are downloaded here (getFile, up to 20 MB) and handed to the core as they are; reading them
is the core's work. A file URL carries the bot token: it is never logged and never put into an error text. One
exception: an HTML file captioned «в хаб <Пространство>» goes to the life hub's code, which puts it in the base as
a page (`Lifehub.upload`); the model never sees it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

from ..attachments import MAX_DOWNLOAD, Upload, too_big
from ..config import TelegramConfig
from ..core import AgentFile, Core
from ..protocol import Store
from ..render import render_telegram, tg_card, tg_plain
from ..sessions import BUTTON_PREFIX

if TYPE_CHECKING:
    from ..bridge import PlatformBridge

log = logging.getLogger("retinue.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
NO_PREVIEW = {"is_disabled": True}
SLASH_COMMANDS = {"/new": "!new", "/compact": "!compact", "/check": "!check", "/mail": "!mail",
                  "/initiative": "!initiative"}
# Files a message may carry; the core reads them. A GIF (animation) also carries `document`: it is checked first.
MEDIA = ("photo", "document", "voice", "audio", "video", "video_note")
# What is still refused aloud: the value finishes the phrase «Пока не умею принимать …».
UNSUPPORTED = {"animation": "гифки", "poll": "опросы", "dice": "кубики", "game": "игры", "story": "истории"}
LOST = "Сообщение не обработано: ошибка на стороне Роутера. Повтори его, пожалуйста."
POISON = ("Сообщение с файлом не обработано: пока Роутер его разбирал, он перезапустился. Пришли файл ещё раз — "
          "лучше в другом формате.")
NOT_FOR_SESSION = "В сессии принимаю текст, голосовые, фото и файлы."
SPEECH_KINDS = ("voice", "audio", "video_note")  # what a session takes besides text: its transcript (`bridge.spoken`)
FILE_KINDS = ("photo", "document")  # and these as they are: the platform makes them the session's (`bridge.files`)
# A file with this caption is a page for the life hub: code puts it in the base (`Lifehub.upload`), not the model.
TO_HUB = re.compile(r"\s*в\s+хаб\s+(\S+)\s*", re.I)
NO_PAGES = "Хаб не подключён: страницы публиковать некуда (docs/lifehub.md)."
# Any trace of a forward, his own included: a forwarded file is never his page, whoever's name it hides behind.
FORWARDED = ("forward_origin", "forward_date", "forward_from", "forward_from_chat", "forward_sender_name",
             "is_automatic_forward")
# The bot's menu; the platform's spheres follow it (`set_menu`).
MENU = ({"command": "new", "description": "новый разговор"},
        {"command": "compact", "description": "сжать разговор"},
        {"command": "check", "description": "проверка канала"},
        {"command": "mail", "description": "почта: ящики и правила"},
        {"command": "initiative", "description": "как часто ассистентка пишет первой"},
        {"command": "help", "description": "справка"})
WAIT = {"command": "wait", "description": "что ждёт тебя"}  # «Ждут тебя» as text (`bridge.wait`), with the platform
APP_BUTTON = "Открыть"        # the menu button beside the input: the face as a Mini App (`set_app_button`)
START = ("Пиши сюда — ответит ассистентка. Команды: /new — новый разговор, /compact — сжать разговор, "
         "/check — проверка канала, /mail — почта: ящики и твои правила, /initiative — как часто ассистентка "
         "пишет первой: реже, обычно, чаще, выкл; тихие часы, /help — справка.")


class TelegramError(Exception):
    def __init__(self, text: str, code: int | None = None) -> None:
        super().__init__(text)
        self.code = code  # Telegram's error_code, or the HTTP status of an answer that is not the Bot API's JSON


def forwarded(message: dict) -> bool:
    """Any trace of a forward, the owner's own forward included."""
    return any(key in message for key in FORWARDED)

BUTTONS_PER_ROW = 4  # Telegram takes at most 8 in a row; four labels like «Откатить 10» still read


def keyboard_of(buttons) -> dict | None:
    """(label, button id) pairs as an inline keyboard, in rows. callback_data is the button id and nothing else:
    Telegram allows 64 bytes, and the meaning stays with the router."""
    keys = [{"text": label, "callback_data": button_id} for label, button_id in buttons]
    return {"inline_keyboard": [keys[i:i + BUTTONS_PER_ROW] for i in range(0, len(keys), BUTTONS_PER_ROW)]} \
        if keys else None


def rows_of(buttons) -> dict | None:
    """A courier's card: one button per row — «Отправить» never sits next to «Поправить» under a thumb."""
    return {"inline_keyboard": [[{"text": label, "callback_data": button_id}] for label, button_id in buttons]} \
        if buttons else None


class TelegramChannel:
    name = "telegram"
    is_record = False
    typing_refresh_s = 4.0  # a chat action lasts 5 s
    username = ""           # the bot's own, from getMe: the face's «Открыть в Telegram» (`bridge.push_waiting`)

    def __init__(self, cfg: TelegramConfig, store: Store, link_hosts: list[str] | tuple = (),
                 bridge: PlatformBridge | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.core: Core | None = None
        self.link_hosts = tuple(link_hosts)  # travel-ops' sites: a link there is clickable (render.linkable)
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)
        self.bridge = bridge  # the platform's sessions, when the router has one
        if bridge:
            bridge.attach(self)

    # --- Bot API -----------------------------------------------------------------------------

    async def call(self, method: str, files: dict | None = None, **params) -> dict:
        url = f"{API}/bot{self.cfg.bot_token}/{method}"
        params = {k: v for k, v in params.items() if v is not None}
        response = await (self.http.post(url, data=params, files=files) if files else self.http.post(url, json=params))
        try:
            data = response.json()
        except ValueError:  # a gateway's HTML page; worded without the URL, which carries the token
            raise TelegramError(f"{method}: {response.status_code} not the Bot API's answer",
                                response.status_code) from None
        if not data.get("ok"):
            code = data.get("error_code")
            raise TelegramError(f"{method}: {code} {data.get('description')}", code if isinstance(code, int) else None)
        return data["result"]

    # --- setup -------------------------------------------------------------------------------

    async def start(self, core: Core) -> None:
        self.core = core
        me = await self.call("getMe")
        self.username = str(me.get("username") or "")
        await self.set_menu()
        await self.set_app_button()
        asyncio.create_task(self.poll())
        log.info("telegram @%s ready", me.get("username"))

    async def set_menu(self) -> None:
        """The bot's commands, and after them the platform's spheres (`/travel`) as the router last heard of them."""
        platform = [WAIT, *self.bridge.menu()] if self.bridge else []
        await self.call("setMyCommands", commands=[*MENU, *platform])

    async def set_app_button(self) -> None:
        """The menu button beside the owner's chat input opens the face as a Mini App (`face_url`, the router's own);
        with no face it is Telegram's default again. Set for his chat only, never as every private chat's default. Set at every start — the same call twice changes nothing — and a
        refusal is logged, never a reason not to start."""
        face_url = self.bridge.face_url if self.bridge else ""
        button = {"type": "web_app", "text": APP_BUTTON, "web_app": {"url": f"{face_url}/"}} if face_url else \
            {"type": "default"}
        try:
            await self.call("setChatMenuButton", chat_id=self.cfg.owner_id, menu_button=button)  # his chat only
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("the bot's menu button not set: %s", exc)

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
        that swells the router), the second delivery is refused aloud instead of killing the router again — all but the
        owner's voice or file to a session (`_for_session`).
        """
        message = update.get("message") or {}
        if any(key in message for key in MEDIA) and not self._for_session(message):
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

    def _for_session(self, message: dict) -> bool:
        """The owner's voice, photo or file in reply to a session's message. It gets no mark: nothing parses it in the
        router (the bytes go to the scribe or to the platform as they are), and a second delivery is to be read again
        and input under the same `input_id`, which the platform takes once — not refused with «пришли ещё раз»."""
        reply = (message.get("reply_to_message") or {}).get("message_id")
        return bool(self.bridge and reply is not None and not forwarded(message) and "animation" not in message
                    and not TO_HUB.fullmatch(message.get("caption") or "")  # a page for the hub is read here
                    and any(key in message for key in (*SPEECH_KINDS, *FILE_KINDS))
                    and self.bridge.session_of(str(reply)))

    async def on_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
        else:
            await self.on_message(update.get("message"))

    async def on_callback(self, query: dict) -> None:
        """A button was pressed. Checked here: who pressed and in which chat. The callback data is only a button
        id; what the button means, whether it is still alive and whether it was used already, the core reads
        from its own tables; a platform card's button (`BUTTON_PREFIX`), the platform from its own."""
        message = query.get("message") or {}
        if not self.core or (query.get("from") or {}).get("id") != self.cfg.owner_id \
                or (message.get("chat") or {}).get("id") != self.cfg.owner_id:
            return
        data = str(query.get("data") or "")
        if self.bridge and data.startswith(BUTTON_PREFIX):
            await self.bridge.press(query["id"], data, message.get("message_id"))
            return
        pressed = await self.core.press(self, data)
        await self.call("answerCallbackQuery", callback_query_id=query["id"], text=pressed.toast)
        if pressed.view is not None and "message_id" in message:
            await self._message(tg_card(pressed.view), edit=message["message_id"], keyboard=rows_of(pressed.keep))
        elif pressed.card and "message_id" in message:
            # The card is rewritten: the choice stays visible; the buttons are gone, or those still alive stay.
            await self._message(render_telegram(pressed.card)[0], edit=message["message_id"],
                                keyboard=keyboard_of(pressed.keep))

    async def on_platform_message(self, message: dict, text: str, native_id: str, reply_to: str | None) -> bool:
        """The owner's own message that belongs to the platform: a reply to a session's message, `/new <track|sphere>
        [text]`, `/<sphere> [text]`, `/wait` («Ждут тебя» as text), or `/app` (a login to the face) when the router knows
        the face. A spoken reply goes in as its transcript; a photo or a file goes as it is, an album as one message;
        anything else is refused. True when it was the platform's; everything else, bare `/new` too, stays the
        assistant's."""
        if session := self.bridge.session_of(reply_to):
            if message.get("text"):
                await self.bridge.input(session, text, native_id, reply_to=reply_to)
            elif any(key in message for key in SPEECH_KINDS):
                await self.bridge.spoken(session, self._upload(message), native_id, self.core.scribe,
                                         message.get("caption") or "", reply_to=reply_to)
            elif any(key in message for key in FILE_KINDS) and "animation" not in message:
                # Never a question's answer, even in reply to its card: a message with its files (`bridge.files`).
                await self.bridge.files(session, message)
            else:
                await self.bridge.say(NOT_FOR_SESSION, session)
            return True
        words = text.split(maxsplit=2)
        if words and words[0].split("@")[0].lower() == "/app" and message.get("text") and self.bridge.face_url:
            await self.bridge.login()
            return True
        if words and words[0].split("@")[0].lower() == "/wait" and message.get("text"):
            await self.bridge.wait()
            return True
        if len(words) > 1 and words[0].split("@")[0].lower() == "/new":  # a track's name or a sphere's
            await self.bridge.new(words[1], words[2] if len(words) > 2 else "", native_id)
            return True
        name = words[0].split("@")[0].lower().removeprefix("/") if words and words[0].startswith("/") else ""
        if name and message.get("text") and name in self.bridge.spheres():  # `/travel найди жильё…`
            await self.bridge.sphere(name, text.split(maxsplit=1)[1] if len(words) > 1 else "", native_id)
            return True
        return False

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
        if not forwarded(message) and "document" in message and "animation" not in message \
                and (to_hub := TO_HUB.fullmatch(message.get("caption") or "")):
            if self.core.lifehub:
                await self.core.lifehub.upload(self, to_hub.group(1).lower(), self._upload(message), native_id)
            else:
                await self.core.tell_owner(NO_PAGES, origin=self)
            return
        # Only words the owner typed or said here are his to a session: any trace of a forward, his own one too, is
        # data for the assistant.
        if self.bridge and not forwarded(message) and await self.on_platform_message(message, text, native_id, reply_to):
            return
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
        if not forwarded(message):  # any forward is data, his own too: «/new» inside it is not a command
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
                       edit: int | None = None, silent: bool = False) -> str | None:
        """The one way text reaches the owner: a new message, or with `edit` a rewrite of an old one; its message id
        comes back. If Telegram refuses the markup, the same text goes out with no markup except <code> around
        addresses — on that path too the preview is off and nothing is a link. `silent`: a new message without a
        sound (a quick ping, `pings.loud`)."""
        method = "editMessageText" if edit else "sendMessage"
        params = dict(chat_id=self.cfg.owner_id, message_id=edit, parse_mode="HTML", link_preview_options=NO_PREVIEW,
                      reply_markup=keyboard, disable_notification=True if silent and not edit else None)
        try:
            sent = await self.call(method, text=html, **params)
        except TelegramError as exc:
            if "parse" not in str(exc):
                raise
            sent = await self.call(method, text=tg_plain(html), **params)
        native_id = str(sent["message_id"]) if sent.get("message_id") else None
        if ref and native_id:
            self.store.save_sent(self.name, native_id, ref)  # so a reply to it can be traced back
        return native_id

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
        keyboard = keyboard_of(buttons or [])
        for i, html in enumerate(parts):
            await self._message(html, ref=ref, keyboard=keyboard if i == len(parts) - 1 else None)

    async def card(self, agent_id: str, view, buttons: list[tuple[str, str]] | None = None, ref: str | None = None,
                   edit: bool = False) -> None:
        """A courier's card: shown, or with `edit` rewritten in every message that showed it."""
        html, keyboard = tg_card(view), rows_of(buttons or [])
        if not edit:
            await self._message(html, ref=ref, keyboard=keyboard)
            return
        for native_id in self.store.sent_native(self.name, ref or ""):
            await self._message(html, edit=int(native_id), keyboard=keyboard)

    async def protocol(self, line: str) -> None:
        pass  # the protocol lives in the Store

    async def trace(self, agent_id: str, tree_id: str, text: str) -> None:
        pass  # agents' conversation is not shown here
