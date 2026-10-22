"""The router's side of the platform (`retinue-platform`): its notes become Telegram messages, and the owner's answers
to them go back to it.

The router stays the only Telegram poller; the platform has no bot token. What the platform has for the owner — an
approval or courier card, a card rewritten, a session's summary — it journals by number, and the bridge pulls it like
any neighbour's items: shown, then confirmed. A note that did not go out for a passing reason (Telegram busy, the
network) is not confirmed and is tried again on the next pull. A note that can never go out — Telegram refuses it, or
it is malformed — is said to the owner as such and confirmed past, so it does not hold back the notes after it. Every
message the bridge sends is mapped in the router's `sent` table under its own channel names
(`platform`: message → session, `platform-card`: message → approval), never under `telegram`: a reply to it goes to
its session, and nothing of the platform enters the assistant's archive or her context.
"""

from __future__ import annotations

import asyncio
import logging

from .channels.telegram import TelegramChannel, TelegramError, rows_of
from .courier import CardView
from .mail import Unavailable
from .outbox import Neighbour
from .protocol import Store
from .render import render_telegram, tg_card

log = logging.getLogger("retinue.bridge")

PUMP_S = 2.0
NOTES_PER_PULL = 50
SESSION, CARD = "platform", "platform-card"  # the router's `sent` channels: message → session, message → approval
UNAVAILABLE = "Платформа недоступна — попробуй позже."
# Telegram's answers to an edit that has nothing left to do: the card already reads so, or the owner deleted it.
SETTLED_EDITS = ("message is not modified", "message to edit not found")
NOT_SHOWN = "Сообщение платформы не показано: Telegram его не принял или оно испорчено. Подробности — в журнале Роутера."
# What a note can fail with for good: Telegram's 400 (too long, say) or data not shaped as a note of its kind.
NOT_SHOWABLE = (TelegramError, KeyError, TypeError, ValueError, AttributeError)


class PlatformClient(Neighbour):
    """The platform as the router reaches it: its notes journal (`items`, `confirm`) and the owner's calls."""

    async def owner(self, call: str, body: dict) -> dict:
        return await self._call("POST", f"/owner/{call}", body)


class PlatformBridge:
    def __init__(self, neighbour: PlatformClient, store: Store) -> None:
        self.neighbour, self.store = neighbour, store
        self.channel: TelegramChannel | None = None

    def attach(self, channel: TelegramChannel) -> None:
        self.channel = channel

    # --- the platform's notes -------------------------------------------------------------------------------------

    async def pump(self) -> None:
        while True:
            try:
                await self.deliver()
            except Unavailable as exc:
                log.warning("%s", exc)
            except Exception:
                log.exception("the platform's notes were not delivered")
            await asyncio.sleep(PUMP_S)

    async def deliver(self) -> int:
        """One pull: every note shown in order, then confirmed up to the last one done with. The first that fails for a
        passing reason stops the pull; it and those after it come again next time. How many notes were done with."""
        done = []
        try:
            for note in await self.neighbour.items(NOTES_PER_PULL):
                try:
                    await self._show(note)
                except NOT_SHOWABLE as exc:
                    if isinstance(exc, TelegramError) and not refused(exc):
                        raise
                    log.exception("platform note %s can never be shown; skipped", note["seq"])
                    await self.say(f"{NOT_SHOWN} ({type(exc).__name__}: {exc})", str(note.get("session") or ""))
                done.append(note["seq"])
        finally:
            if done:
                try:
                    await self.neighbour.confirm(done[-1])
                except Exception:
                    log.exception("platform notes up to %s were not confirmed", done[-1])
        return len(done)

    async def _show(self, note: dict) -> None:
        kind, session, data = note.get("kind"), str(note.get("session") or ""), note.get("data") or {}
        if kind == "card":
            native_id = await self.channel._message(tg_card(view_of(data["view"])), keyboard=rows_of(data["buttons"]))
            if native_id:
                self.store.save_sent(SESSION, native_id, session)
                self.store.save_sent(CARD, native_id, str(note["approval"]))
        elif kind == "update":
            html, keyboard = tg_card(view_of(data["view"])), rows_of(data["buttons"])
            for native_id in self.store.sent_native(CARD, str(note["approval"])):
                await self._edit(html, int(native_id), keyboard)
        elif kind == "summary":
            await self.say(f"{data['head']}\n\n{data['text']}", session)
        else:
            log.warning("platform note %s of an unknown kind %r skipped", note.get("seq"), kind)

    async def _edit(self, html: str, native_id: int, keyboard: dict | None) -> None:
        try:
            await self.channel._message(html, edit=native_id, keyboard=keyboard)
        except TelegramError as exc:
            if not any(reason in str(exc) for reason in SETTLED_EDITS):
                raise

    async def say(self, text: str, session: str = "") -> None:
        """Text to the owner in the platform's name; with `session`, each message of it answers into that session."""
        for html in render_telegram(text):
            native_id = await self.channel._message(html)
            if session and native_id:
                self.store.save_sent(SESSION, native_id, session)

    # --- the owner's side -----------------------------------------------------------------------------------------

    def session_of(self, reply_to: str | None) -> str | None:
        return self.store.sent_event(SESSION, reply_to) if reply_to else None

    async def new(self, track: str, text: str, native_id: str) -> None:
        try:
            answer = await self.neighbour.owner("new", {"input_id": _input_id(native_id), "track": track,
                                                        "text": text})
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE)
            return
        session = answer.get("session") if answer.get("ok") else None
        await self.say(_said(answer), session if isinstance(session, str) else "")

    async def input(self, session: str, text: str, native_id: str) -> None:
        try:
            answer = await self.neighbour.owner("input", {"input_id": _input_id(native_id), "session": session,
                                                          "text": text})
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE)
            return
        await self.say(_said(answer), session)

    async def press(self, query_id: str, button: str, native_id: int | None) -> None:
        """A press on a platform card's button, answered as the platform decides: a toast, maybe the card rewritten
        with the buttons it keeps, maybe the whole input as a file («Открыть»)."""
        try:
            pressed = await self.neighbour.owner("press", {"button": button})
        except Unavailable as exc:
            log.warning("%s", exc)
            pressed = {"toast": UNAVAILABLE}
        await self.channel.call("answerCallbackQuery", callback_query_id=query_id, text=str(pressed.get("toast") or ""))
        if isinstance(pressed.get("view"), dict) and native_id is not None:
            await self._edit(tg_card(view_of(pressed["view"])), native_id, rows_of(pressed.get("keep") or []))
        if isinstance(document := pressed.get("document"), dict):
            await self.channel.call("sendDocument", chat_id=str(self.channel.cfg.owner_id),
                                    files={"document": (document["name"], document["text"].encode(), "text/plain")})


def refused(exc: TelegramError) -> bool:
    """Telegram's 400: the same request would be refused again. A 429, a 5xx or the network may pass."""
    return exc.code == 400


def view_of(data: dict) -> CardView:
    """A card as the platform sent it, in JSON: the `link` pair comes back as a list."""
    return CardView(list(data["head"]), str(data.get("body") or ""), str(data.get("status") or ""),
                    tuple(data.get("link") or ()))


def _input_id(native_id: str) -> str:
    return f"tg:{native_id}"


def _said(answer: dict) -> str:
    return str(answer.get("say") or answer.get("error") or "Платформа ответила без текста.")
