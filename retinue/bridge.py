"""The router's side of the platform (`retinue-platform`): its notes become Telegram messages, and the owner's answers
to them go back to it.

The router stays the only Telegram poller; the platform has no bot token. What the platform has for the owner — an
approval or courier card, a card rewritten, a session's summary, a file a session handed over — it journals by
number, and the bridge pulls it like any neighbour's items: shown, then confirmed. A note that did not go out for a passing reason (Telegram busy, the
network) is not confirmed and is tried again on the next pull. A note that can never go out — Telegram refuses it, or
it is malformed — is said to the owner as such and confirmed past, so it does not hold back the notes after it. Every
message the bridge sends is mapped in the router's `sent` table under its own channel names
(`platform`: message → session, `platform-card`: message → approval), never under `telegram`: a reply to it goes to
its session, and nothing of the platform enters the assistant's archive or her context.

With the face's address (`face_url`), an approval card also offers «Открыть в приложении»: a Mini App button that opens
that card in the face, where the owner logs in by Telegram's own signature. `/app` gets him a one-time code for the
face and the link that carries it — the one link the bridge ever makes clickable, to the face's own host.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from .attachments import NO_SPEECH, Unreadable, Upload, prepare
from .clock import ago
from .config import BOT_COMMANDS, SPHERE_NAME
from .channels.telegram import TelegramChannel, TelegramError, rows_of
from . import inbox, pings
from .core import ALBUM_S, defuse
from .courier import CardView
from .mail import Unavailable
from .outbox import Neighbour
from .outfiles import ACTIVE, PHOTO_MAX, PHOTOS
from .spoken import SPOKEN_MARK
from .protocol import Store
from .render import TELEGRAM_LIMIT, _tg_escape, _tg_link, linkable, render_telegram, tg_card

log = logging.getLogger("retinue.bridge")

PUMP_S = 2.0
BACKOFF_MAX_S = 30.0         # the platform away: each try waits twice as long as the last, up to this
NOTES_PER_PULL = 50
SESSION, CARD = "platform", "platform-card"  # the router's `sent` channels: message → session, message → approval
# `/wait`'s line of a question: message → `<approval>/<n>`. Not CARD: a card's update rewrites every CARD message.
QUESTION = "platform-question"
PING = "platform-ping"       # and: a ping's message with its reply buttons → the ping's note number
UNAVAILABLE = "Платформа недоступна — попробуй позже."
# Telegram's answers to an edit that has nothing left to do: the card already reads so, or the owner deleted it.
SETTLED_EDITS = ("message is not modified", "message to edit not found")
NOT_SHOWN = "Сообщение платформы не показано: Telegram его не принял или оно испорчено. Подробности — в журнале Роутера."
# What a note can fail with for good: Telegram's 400 (too long, say) or data not shaped as a note of its kind.
NOT_SHOWABLE = (TelegramError, KeyError, TypeError, ValueError, AttributeError)
IN_APP = "Открыть в приложении"
OPEN_APP = "Открыть"         # `/app`'s button: the face as a Mini App
NOT_PASSED = "Не передано в сессию"
ECHO_CHARS = 3000            # what was heard, said back, escaped: room is left for the platform's answer
ALBUMS = "platform.albums"   # the router's kv: the albums being gathered or sent into sessions, as JSON
ALBUM_KEYS = ("message_id", "caption", "media_group_id", "photo", "document")  # what is kept of a part
FILES_TIMEOUT_S = 300        # the platform reads every picture and PDF of the message before it answers
WAITING_LOOK_S = 30.0        # the assistant's digest («Ждут тебя») is built this often…
WAITING_PUSH_S = 3600.0      # …pushed when it changed, and at least this often anyway
WAITING_TIMEOUT_S = 10.0     # a push waits no longer: it runs beside the notes' pump, never in its way
DECIDE_HINT = "Реши кнопками её карточки или в приложении."
# `/wait`: what each row's icon is, by where it came from.
ICONS = {"question": "❓", "approval": "⚠️", "courier": "📦", "session": "💬", "letter": "✉️", "chat": "💬",
         "draft": "📝", "deadline": "⏳", "base": "🗂", "push": "⬆️"}
SPHERES = "platform.spheres"  # the router's kv: the platform's spheres as it last named them, as JSON
SPHERES_EVERY_S = 600        # how often the pump asks the platform for them again (a restart of it may change them)


class PlatformClient(Neighbour):
    """The platform as the router reaches it: its notes journal (`items`, `confirm`) and the owner's calls."""

    async def owner(self, call: str, body: dict, timeout: float = 60) -> dict:
        return await self._call("POST", f"/owner/{call}", body, timeout)

    async def out_file(self, file_id: str) -> dict:
        """A file a session handed over: `name`, `mime` and `body` (its bytes), or `error`. Up to 20 MB in base64: the
        answer is parsed and decoded off the event loop."""
        try:
            response = await self.http.post(f"{self.url}/owner/file", json={"id": file_id}, timeout=120,
                                            headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            raise Unavailable(f"недоступно: {self.name} ({type(exc).__name__})") from None
        if response.status_code not in (200, 400):
            raise Unavailable(f"{self.name}: ответ HTTP {response.status_code}")
        return await asyncio.to_thread(_file_answer, response.content)


    async def waiting(self, body: dict) -> dict:
        """The assistant's digest (`/owner/waiting`), with a short wait. A digest too big for the platform (413) is
        its refusal, not silence: the same digest is not pushed again every look."""
        return await self._digest("/owner/waiting", body)

    async def library(self, body: dict) -> dict:
        """The hub's list of pages for «Читать» (`/owner/library`), the same way as the digest."""
        return await self._digest("/owner/library", body)

    async def _digest(self, path: str, body: dict) -> dict:
        try:
            response = await self.http.post(f"{self.url}{path}", json=body, timeout=WAITING_TIMEOUT_S,
                                            headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            raise Unavailable(f"недоступно: {self.name} ({type(exc).__name__})") from None
        if response.status_code == 413:
            return {"error": "дайджест больше, чем платформа принимает (413)"}
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code not in (200, 400) or not isinstance(data, dict):
            raise Unavailable(f"{self.name}: ответ HTTP {response.status_code}")
        return data


class PlatformBridge:
    def __init__(self, neighbour: PlatformClient, store: Store, face_url: str = "") -> None:
        self.neighbour, self.store, self.face_url = neighbour, store, face_url
        self.channel: TelegramChannel | None = None
        self.timers: dict[str, asyncio.Task] = {}  # an album's key -> the task that closes it (`files`)
        # What waits for the owner in the assistant's world (`waiting.digest`), given by the router once its core is up.
        self.waiting: Callable[[float], list[dict]] | None = None
        self.chips: Callable[[float], list[dict]] | None = None  # the hub's status chips, pushed with it
        self._looked, self._pushed, self._pushed_mark = 0.0, 0.0, ""
        self._pushing: asyncio.Task | None = None
        # The hub's pages for the face's «Читать» (`Lifehub.library`): None until the hub has read them.
        self.library: Callable[[], list[dict] | None] | None = None
        self._library_looked, self._library_pushed, self._library_mark = 0.0, 0.0, ""
        self.spheres_at = 0.0  # when the platform last named its spheres (`refresh_spheres`)

    def attach(self, channel: TelegramChannel) -> None:
        self.channel = channel

    # --- the platform's notes -------------------------------------------------------------------------------------

    async def pump(self) -> None:
        try:
            await self.recover()
        except Exception:
            log.exception("the albums left by the previous router were not taken up")
        if self.waiting is not None or self.library is not None:  # beside the notes: a hung push holds back no card
            self._pushing = asyncio.create_task(self.push_loop())
        wait = PUMP_S
        while True:
            try:
                if time.monotonic() - self.spheres_at > SPHERES_EVERY_S:
                    try:
                        await self.refresh_spheres()
                    except Exception as exc:  # the notes come first: a menu that did not go is tried again later
                        log.warning("the platform's spheres not taken: %s", exc)
                await self.deliver()
                wait = PUMP_S
            except Unavailable as exc:
                wait = min(max(wait, PUMP_S) * 2, BACKOFF_MAX_S)
                log.warning("%s (next try in %.0f s)", exc, wait)
            except Exception:
                log.exception("the platform's notes were not delivered")
                wait = PUMP_S
            await asyncio.sleep(wait)

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

    async def push_loop(self) -> None:
        while True:
            try:
                await self.push_waiting(time.time())
            except Unavailable as exc:
                log.warning("the assistant's digest not pushed: %s", exc)
            except Exception:
                log.exception("the assistant's digest not pushed")
            try:
                await self.push_library(time.time())
            except Unavailable as exc:
                log.warning("the hub's pages not pushed: %s", exc)
            except Exception:
                log.exception("the hub's pages not pushed")
            await asyncio.sleep(PUMP_S)

    async def push_waiting(self, now: float) -> bool:
        """The assistant's digest to the platform (`/owner/waiting`), replacing the one there whole: looked at every
        WAITING_LOOK_S, pushed when it changed and at least every WAITING_PUSH_S. The platform never asks for it. A
        digest it refuses is logged and not pushed again until it changes or the hour is up. True when pushed."""
        if self.waiting is None or now - self._looked < WAITING_LOOK_S:
            return False
        self._looked = now
        body = {"items": self.waiting(now), "bot": getattr(self.channel, "username", "") or ""}
        if self.chips is not None:
            body["chips"] = self.chips(now)
        mark = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if mark == self._pushed_mark and now - self._pushed < WAITING_PUSH_S:
            return False
        answer = await self.neighbour.waiting(body)  # Unavailable: the next look, 30 s on, tries again
        if answer.get("error"):
            log.warning("the platform refused the assistant's digest: %s", answer["error"])
        self._pushed, self._pushed_mark = now, mark
        return True

    async def push_library(self, now: float) -> bool:
        """The hub's pages to the platform (`/owner/library`), replacing the list there whole, by the digest's rules:
        looked at every WAITING_LOOK_S, pushed when it changed and at least every WAITING_PUSH_S, a refused list not
        pushed again until it changes or the hour is up. Nothing before the hub first read its pages. True when
        pushed."""
        if self.library is None or now - self._library_looked < WAITING_LOOK_S:
            return False
        self._library_looked = now
        pages = self.library()
        if pages is None:
            return False
        body = {"pages": pages}
        mark = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if mark == self._library_mark and now - self._library_pushed < WAITING_PUSH_S:
            return False
        answer = await self.neighbour.library(body)
        if answer.get("error"):
            log.warning("the platform refused the hub's pages: %s", answer["error"])
        for page in answer.get("dropped") or []:  # the rest went up: these did not, and the log says which and why
            if isinstance(page, dict):
                log.warning("the platform dropped the hub's page %s: %s", page.get("id"), page.get("why"))
        self._library_pushed, self._library_mark = now, mark
        return True

    async def _show(self, note: dict) -> None:
        kind, session, data = note.get("kind"), str(note.get("session") or ""), note.get("data") or {}
        if kind == "card":
            keyboard = self._keyboard(data["buttons"], str(note["approval"]))
            native_id = await self.channel._message(tg_card(view_of(data["view"])), keyboard=keyboard)
            if native_id:
                self.store.save_sent(SESSION, native_id, session)
                self.store.save_sent(CARD, native_id, str(note["approval"]))
        elif kind == "update":
            html = tg_card(view_of(data["view"]))
            # A card rewritten with the buttons it keeps keeps the face's button too; one without keeps none.
            keyboard = self._keyboard(data["buttons"], str(note["approval"]))
            for native_id in self.store.sent_native(CARD, str(note["approval"])):
                await self._edit(html, int(native_id), keyboard)
        elif kind == "summary" and "kind" in data:
            await self._ping(int(note["seq"]), session, data)
        elif kind == "ping":
            await self._ping_closed(data)
        elif kind in ("summary", "file_skipped"):  # a summary from before pings
            await self.say(f"{data['head']}\n\n{data['text']}", session)
        elif kind == "file":
            await self._file(session, str(data["id"]), data)
        else:
            log.warning("platform note %s of an unknown kind %r skipped", note.get("seq"), kind)

    async def _ping(self, seq: int, session: str, data: dict) -> None:
        """The end of a turn (`pings`): its header and text, split as any text is, each message answering into the
        session; the reply buttons on the last one, which is mapped to the ping so a press elsewhere rewrites it. A
        quick done turn goes out without a sound. Buttons Telegram refuses cost the buttons, never the text: the last
        part goes again without them (a reply to it still answers)."""
        parts = ping_parts(data)
        keyboard = ping_keyboard(data)
        silent = not pings.loud(data)
        for n, html in enumerate(parts):
            last = n == len(parts) - 1
            try:
                native_id = await self.channel._message(html, keyboard=keyboard if last else None, silent=silent)
            except TelegramError as exc:
                if not (last and keyboard and refused(exc)):
                    raise
                log.warning("ping %s: its buttons refused (%s); sent without them", seq, exc)
                native_id = await self.channel._message(html, silent=silent)
            if native_id:
                self.store.save_sent(SESSION, native_id, session)
                if last:
                    self.store.save_sent(PING, native_id, str(seq))

    async def _ping_closed(self, data: dict) -> None:
        """A ping's replies closed: its last message rewritten without its buttons, with «→ <reply>» when one went in;
        or its «В Базу · N» put on, relabelled or taken off (`tobase`), the replies kept while they are open."""
        html = ping_parts(data)[-1]
        for native_id in self.store.sent_native(PING, str(data["seq"])):
            await self._edit(html, int(native_id), ping_keyboard(data))

    async def _file(self, session: str, file_id: str, note: dict) -> None:
        """A file the session handed over (`outfiles`), sent as itself: a small picture as a photo, the rest as a
        document, under the session's last message, and mapped to the session so a reply to it goes there. No
        caption: the platform's words reach the owner only through `_message`. An HTML page or an SVG — what a
        browser runs — never goes as itself: opened in Telegram it could run its script and reach anywhere, which
        would make out/ a way out of a lane without the web. It comes as a message with «Открыть», the face's Mini
        App, where it opens sandboxed; without the face, as `<name>.txt`."""
        if str(note.get("mime") or "").split(";")[0] in ACTIVE and self.face_url:
            await self._page(session, file_id, str(note.get("path") or ""))
            return
        answer = await self.neighbour.out_file(file_id)
        if not isinstance(answer.get("body"), bytes):
            await self.say(str(answer.get("error") or "Файла нет."), session)
            return
        body = answer["body"]
        name, mime = str(answer.get("name") or "file"), str(answer.get("mime") or "application/octet-stream")
        if mime.split(";")[0] in ACTIVE:  # no face to open it in: as text, so Telegram never runs it
            name, mime = f"{name}.txt", "text/plain"
        params = {"chat_id": str(self.channel.cfg.owner_id)}
        if last := max((int(n) for n in self.store.sent_native(SESSION, session)), default=0):
            params["reply_parameters"] = json.dumps({"message_id": last, "allow_sending_without_reply": True})
        sent = None
        if mime in PHOTOS and len(body) <= PHOTO_MAX:
            try:
                sent = await self.channel.call("sendPhoto", files={"photo": (name, body, mime)}, **params)
            except TelegramError as exc:
                if not refused(exc):
                    raise
                log.warning("file %s as a photo refused (%s): sent as a document", file_id, exc)
        if sent is None:
            sent = await self.channel.call("sendDocument", files={"document": (name, body, mime.split(";")[0])},
                                           **params)
        if sent.get("message_id"):
            self.store.save_sent(SESSION, str(sent["message_id"]), session)

    async def _page(self, session: str, file_id: str, path: str) -> None:
        name = path.rsplit("/", 1)[-1] or "page"
        keyboard = {"inline_keyboard": [[{"text": "Открыть", "web_app": {"url": f"{self.face_url}/f/{file_id}"}}]]}
        native_id = await self.channel._message(
            f"Страница из сессии: <code>{_tg_escape(name)}</code>\nОткроется в приложении, в песочнице: без скриптов "
            "и без загрузок извне.", keyboard=keyboard)
        if native_id:
            self.store.save_sent(SESSION, native_id, session)

    async def _edit(self, html: str, native_id: int, keyboard: dict | None) -> None:
        try:
            await self.channel._message(html, edit=native_id, keyboard=keyboard)
        except TelegramError as exc:
            if not any(reason in str(exc) for reason in SETTLED_EDITS):
                raise

    async def say(self, text: str, session: str = "", heard: str = "") -> None:
        """Text to the owner in the platform's name; with `session`, each message of it answers into that session.
        `heard`: what the router made of his voice, put above the text verbatim — escaped and on one line, so nothing
        in it formats the message or passes for the platform's own line. One message while both fit."""
        parts = render_telegram(text)
        if heard:
            echo = _echo(heard)
            if len(echo) + 2 + len(parts[0]) <= TELEGRAM_LIMIT:
                parts[0] = f"{echo}\n\n{parts[0]}"
            else:
                parts.insert(0, echo)
        for html in parts:
            native_id = await self.channel._message(html)
            if session and native_id:
                self.store.save_sent(SESSION, native_id, session)

    # --- spheres: `/<sphere> [text]` is the platform's, and the bot's menu names them --------------------------------

    def spheres(self) -> dict[str, dict]:
        """The platform's spheres by name, as it last named them: known across a restart of the router while the
        platform is away."""
        try:
            listed = json.loads(self.store.get(SPHERES) or "[]")
        except ValueError:
            return {}
        return {s["name"]: s for s in listed if isinstance(s, dict) and isinstance(s.get("name"), str)}

    async def refresh_spheres(self) -> None:
        """The platform's spheres, asked again; a list that changed is kept and the bot's menu set anew. A name that
        is not a command's shape, or is one of the bot's own commands, is left out: the router's commands stay its."""
        answer = await self.neighbour.owner("spheres", {})
        self.spheres_at = time.monotonic()
        listed = [{"name": s["name"], "title": str(s.get("title") or s["name"])[:40], "icon": str(s.get("icon") or "")[:8]}
                  for s in answer.get("spheres") or [] if isinstance(s, dict) and isinstance(s.get("name"), str)
                  and SPHERE_NAME.fullmatch(s["name"]) and s["name"] not in BOT_COMMANDS]
        before = self.store.get(SPHERES)
        if listed == list(self.spheres().values()):
            return
        self.store.set(SPHERES, json.dumps(listed, ensure_ascii=False))
        if self.channel is not None:
            try:
                await self.channel.set_menu()
            except BaseException:
                self.store.set(SPHERES, before or "[]")  # not kept: the next ask, in SPHERES_EVERY_S, tries again
                raise

    def menu(self) -> list[dict]:
        """The spheres as the bot's commands: «/travel — ✈️ Путешествия»."""
        return [{"command": s["name"], "description": f"{s['icon']} {s['title']}".strip()}
                for s in self.spheres().values()]

    async def sphere(self, name: str, text: str, native_id: str) -> None:
        """`/<sphere> [text]`: into the sphere's main session, or how it is (`Spheres.command`); a reply to the answer
        goes to that session."""
        try:
            answer = await self.neighbour.owner("sphere", {"input_id": _input_id(native_id), "sphere": name,
                                                           "text": text})
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE)
            return
        session = answer.get("session")
        await self.say(_said(answer), session if isinstance(session, str) else "")

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

    async def input(self, session: str, text: str, native_id: str, heard: str = "", reply_to: str | None = None) -> None:
        """The owner's reply into its session, once per Telegram message (`input_id`). `heard`: what the router made
        of his voice, said back above the platform's answer, in the same message. `reply_to`: the message he replied
        to — a question's card takes his text as its answer while it asks (`Platform.reply_answer`). A reply carrying
        files must not pass `reply_to` as a card: it is never an answer, it goes in as a message with its files."""
        body = {"input_id": _input_id(native_id), "session": session, "text": text}
        if card := (self.store.sent_event(CARD, reply_to) or self.store.sent_event(QUESTION, reply_to)
                    if reply_to else None):
            body["card"] = card
        try:
            answer = await self.neighbour.owner("input", body)
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE, session, heard)  # what was heard is not lost; a reply to it tries again
            return
        await self.say(_said(answer), session, heard)

    async def spoken(self, session: str, upload: Upload, native_id: str, scribe, caption: str = "",
                     reply_to: str | None = None) -> None:
        """A voice note, an audio file or a video note in reply to a session's message: the assistant's own speech
        path (`attachments.prepare` — her size limits, her scribe) turns it into text, which goes in like a typed
        reply, marked as spoken so the agent reads a misheard word as such. The owner sees what was heard with the
        platform's answer. Speech that cannot be read is said with the reason, and nothing goes into the session.
        Awaited like the assistant's own voice path, with «печатает…» meanwhile."""
        typing = asyncio.create_task(self._keep_typing())
        try:
            heard = await prepare(upload, scribe)
        except Unreadable as exc:
            await self.say(f"{NOT_PASSED}: {exc}.", session)
            return
        except Exception as exc:  # a bug in reading costs this message, said aloud, never the channel
            log.exception("reading %s for session %s failed", upload.label(), session)
            await self.say(f"{NOT_PASSED}: {upload.label()}: ошибка ({type(exc).__name__}).", session)
            return
        finally:
            typing.cancel()
        if heard.text == NO_SPEECH:
            await self.say(f"{NOT_PASSED}: {heard.what}: речи не разобрать.", session)
            return
        # One line, marked whole: a line break in the transcript cannot start something that reads as not spoken.
        spoken = SPOKEN_MARK + defuse(" ".join(heard.text.split()))
        await self.input(session, f"{caption.strip()}\n{spoken}" if caption.strip() else spoken, native_id,
                         heard=heard.text, reply_to=reply_to)

    async def files(self, session: str, message: dict) -> None:
        """A photo or a file in reply to a session's message: its bytes go to the platform as they are, which makes them
        the session's (`inbox`: a picture or a short PDF as a block, any other file into the session's folder) — the
        router reads nothing of it. The parts of an album (`media_group_id`) are gathered first, as the core gathers
        the assistant's, and go in as one message under the first part's id, the captions joined. They are kept in
        the router's SQLite as they come (`ALBUMS`), so a restart neither drops them nor splits the album: what was
        kept is gathered again on start, and a part Telegram delivers again joins it by its group."""
        group, native_id = message.get("media_group_id"), str(message["message_id"])
        if not group:
            await self._files(session, [self.channel._upload(message)], native_id, message.get("caption") or "")
            return
        albums = self._albums()
        if any(native_id in (str(p["message_id"]) for p in a["parts"]) for a in albums.values() if a["group"] == group):
            return  # delivered again: it is in its album already
        key = next((k for k, a in albums.items() if a["group"] == group and not a["sending"]), None)
        if key is None:
            key = group if group not in albums else f"{group}/{native_id}"  # a part late for an album already sent
            albums[key] = {"group": group, "session": session, "parts": [], "sending": False}
        albums[key]["parts"].append({k: message[k] for k in ALBUM_KEYS if k in message})
        self._keep(albums)
        self._arm(key)

    def _albums(self) -> dict:
        return json.loads(self.store.get(ALBUMS) or "{}")

    def _keep(self, albums: dict) -> None:
        self.store.set(ALBUMS, json.dumps(albums, ensure_ascii=False))

    def _arm(self, key: str, wait: float | None = None) -> None:
        if (timer := self.timers.pop(key, None)) is not None:
            timer.cancel()
        self.timers[key] = asyncio.create_task(self._close_album(key, ALBUM_S if wait is None else wait))

    async def recover(self) -> None:
        """The albums a previous router left: one being sent goes again at once (the platform takes an input once), one
        being gathered waits for its parts Telegram delivers again."""
        for key, album in self._albums().items():
            self._arm(key, 0 if album["sending"] else None)

    async def _close_album(self, key: str, wait: float) -> None:
        await asyncio.sleep(wait)
        self.timers.pop(key, None)
        albums = self._albums()
        if (album := albums.get(key)) is None:
            return
        album["sending"] = True
        self._keep(albums)
        parts, session = album["parts"], album["session"]
        try:
            await self._files(session, [self.channel._upload(p) for p in parts], str(parts[0]["message_id"]),
                              "\n".join(c for p in parts if (c := (p.get("caption") or "")).strip()))
        except Exception:  # the update loop has moved on already: the owner hears of it instead of silence
            log.exception("album %s for session %s failed", key, session)
            await self.say(f"{NOT_PASSED}: альбом из {len(parts)} файлов не собран — ошибка на стороне Роутера. "
                           "Пришли его ещё раз.", session)
        finally:
            albums = self._albums()
            albums.pop(key, None)
            self._keep(albums)

    async def _files(self, session: str, uploads: list[Upload], native_id: str, caption: str) -> None:
        typing = asyncio.create_task(self._keep_typing())
        refused, given, total = [], [], 0
        try:
            for upload in uploads:
                if upload.refused:
                    refused.append(upload.refused)
                    continue
                if total + upload.size > inbox.MAX_TOTAL:  # the size Telegram announced: not downloaded for nothing
                    refused.append(f"{upload.label()}: вместе с остальными больше {inbox.MAX_TOTAL // 2**20} МБ")
                    continue
                try:
                    data = upload.data or await upload.fetch()
                except Exception as exc:  # the channel words its errors without addresses: they carry the token
                    refused.append(f"{upload.label()}: Telegram не отдал его ({exc})")
                    continue
                total += max(len(data), upload.size)
                given.append({"name": upload.name, "media_type": upload.media_type or
                              ("image/jpeg" if upload.kind == "photo" else ""),
                              "data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()})
            if not given and not caption.strip():
                await self.say(f"{NOT_PASSED}: {'; '.join(refused)}.", session)
                return
            try:
                answer = await self.neighbour.owner("input", {"input_id": _input_id(native_id), "session": session,
                                                              "text": caption, "attachments": given}, FILES_TIMEOUT_S)
            except Unavailable as exc:
                log.warning("%s", exc)
                await self.say(UNAVAILABLE, session)
                return
        finally:
            typing.cancel()
        said = _said(answer)
        await self.say(f"{said}\n{NOT_PASSED}: {'; '.join(refused)}." if refused else said, session)

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self.channel.typing("", True)
            except Exception as exc:  # «печатает…» is a courtesy: its failure stops nothing
                log.warning("typing: %s", exc)
            await asyncio.sleep(self.channel.typing_refresh_s)

    async def press(self, query_id: str, button: str, native_id: int | None) -> None:
        """A press on a platform card's button, answered as the platform decides: a toast, maybe the card rewritten
        with the buttons it keeps, maybe the whole input as a file («Открыть»)."""
        try:
            pressed = await self.neighbour.owner("press", {"button": button})
        except Unavailable as exc:
            log.warning("%s", exc)
            pressed = {"toast": UNAVAILABLE}
        # Telegram takes at most 200 characters in a toast: a longer one would fail the answer, and the press with it.
        toast = str(pressed.get("toast") or "")
        await self.channel.call("answerCallbackQuery", callback_query_id=query_id,
                                text=toast if len(toast) <= 200 else toast[:199] + "…")
        if isinstance(pressed.get("view"), dict) and native_id is not None:
            approval = self.store.sent_event(CARD, str(native_id)) or ""
            await self._edit(tg_card(view_of(pressed["view"])), native_id,
                             self._keyboard(pressed.get("keep") or [], approval))
        if isinstance(document := pressed.get("document"), dict):
            await self.channel.call("sendDocument", chat_id=str(self.channel.cfg.owner_id),
                                    files={"document": (document["name"], document["text"].encode(), "text/plain")})


    async def wait(self) -> None:
        """`/wait`: «Ждут тебя» as text — a head with the count, then one message per row, grouped by action. A reply to
        a session's row goes into its session like a reply to any of its messages; to a question's row it is the answer
        (mapped apart from its card, so the card's later rewrites never reach this line). A card to decide is not
        answered by a reply — that would only make it stale — so its line says to use the card's buttons or the app,
        and is mapped to nothing. The assistant's rows are one message of hers, on record in her conversation: a reply
        to it goes to her with that message quoted."""
        try:
            feed = await self.neighbour.owner("feed", {})
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE)
            return
        rows = [r for r in feed.get("rows") or [] if isinstance(r, dict)]
        if not rows:
            await self.channel._message("Ничего не ждёт тебя.")
            return
        count, more = int(feed.get("count") or len(rows)), int(feed.get("more") or 0)
        head = f"Ждут тебя: {count}. Ответь на строку — уйдёт в её сессию."
        if more:
            head += f"\nЕщё {more} — в приложении." if self.face_url else f"\nЕщё {more}."
        await self.channel._message(head)
        group, mine = "", []
        for row in rows:
            if not isinstance(row.get("session"), str) or not row["session"]:
                mine.append(row)
                continue
            text = _wait_line(row, time.time(), str(row.get("group") or "") if row.get("group") != group else "")
            group, session = str(row.get("group") or ""), row["session"]
            decide = row.get("action") == "decide"
            if decide:
                text += f"\n<i>{_tg_escape(str(row.get('hint') or DECIDE_HINT))}</i>"
            where = f"/a/{row['approval']}" if isinstance(row.get("approval"), str) else f"/s/{session}"
            keyboard = {"inline_keyboard": [[{"text": IN_APP, "web_app": {"url": f"{self.face_url}{where}"}}]]} \
                if self.face_url else None
            native_id = await self.channel._message(text, keyboard=keyboard)
            if native_id and not decide:
                self.store.save_sent(SESSION, native_id, session)
                if isinstance(row.get("card"), str):  # a question's line: a reply to it is its answer
                    self.store.save_sent(QUESTION, native_id, row["card"])
        if mine:
            await self._hers(mine)

    async def _hers(self, rows: list[dict]) -> None:
        """The assistant's rows as her conversation's own system message (`Core.tell_owner`): on record there, so a
        reply to it reaches her with what it listed — by who and what, the words she knows them by."""
        now, lines = time.time(), ["Ждут тебя от ассистентки:"]
        for row in rows:
            since = row.get("since")
            waited = f" · {ago(now - since)}" if isinstance(since, (int, float)) and since <= now else ""
            where = f" ({row['where']})" if row.get("where") else ""
            lines.append(f"- {row.get('title') or ''}{where}: {row.get('essence') or ''}{waited}")
        lines.append("Ответь на это сообщение — оно уйдёт ассистентке.")
        core = getattr(self.channel, "core", None)
        if core is not None:
            await core.tell_owner("\n".join(lines), origin=self.channel)
        else:  # no core (never in the router): said, mapped to nothing
            await self.channel._message(_tg_escape("\n".join(lines)))

    def _keyboard(self, buttons: list, approval: str) -> dict | None:
        """A card's buttons one per row; while it still has any, and the face is known, the face's own button last."""
        keyboard = rows_of(buttons)
        if keyboard and approval and self.face_url:  # a question's message is `<approval>/<n>`: the app shows the card
            base = approval.removeprefix("base/") if approval.startswith("base/") else None  # a «В Базу» card
            push = approval.removeprefix("push/") if approval.startswith("push/") else None  # a «Запушить» card
            url = f"{self.face_url}/b/{base}" if base else f"{self.face_url}/p/{push}" if push else \
                f"{self.face_url}/a/{approval.partition('/')[0]}"
            keyboard["inline_keyboard"].append([{"text": IN_APP, "web_app": {"url": url}}])
        return keyboard

    async def login(self) -> None:
        """`/app`: a one-time code for the face from the platform, said with the link that carries it. The link is
        clickable only on the face's own host, as the router's config names it — never as the platform says."""
        try:
            answer = await self.neighbour.owner("login", {})
        except Unavailable as exc:
            log.warning("%s", exc)
            await self.say(UNAVAILABLE)
            return
        code, url = answer.get("code"), answer.get("url")
        if not isinstance(code, str) or not isinstance(url, str) or not code:
            await self.say(_said(answer))
            return
        host = urlsplit(self.face_url).hostname if self.face_url else ""
        link = _tg_link(url, url) if host and linkable(url, (host,)) else f"<code>{_tg_escape(url)}</code>"
        # «Открыть»: the app as a Mini App, logged in by Telegram's signature — no code needed in Telegram itself.
        keyboard = {"inline_keyboard": [[{"text": OPEN_APP, "web_app": {"url": f"{self.face_url}/"}}]]} \
            if self.face_url else None
        await self.channel._message(f"Вход в приложение: {link}\n\nИли введи код: <code>{_tg_escape(code)}</code>\n"
                                    "Код действует 10 минут и один раз.", keyboard=keyboard)


def _file_answer(content: bytes) -> dict:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    if isinstance(data.get("data"), str):
        data["body"] = base64.b64decode(data.pop("data"))
    return data


def ping_parts(data: dict) -> list[str]:
    """A ping as Telegram messages: its header line (escaped, bold) above the text, in the first message while both
    fit; «→ <reply>» under the last once a reply went in. The same parts every time for the same data: the message a
    closed ping rewrites is the last of them."""
    head = f"<b>{_tg_escape(pings.header(data))}</b>"
    text = str(data.get("text") or "")
    parts = render_telegram(text) if text.strip() else [head]
    if text.strip():
        if len(head) + 2 + len(parts[0]) <= TELEGRAM_LIMIT:
            parts[0] = f"{head}\n\n{parts[0]}"
        else:
            parts.insert(0, head)
    if isinstance(answered := data.get("answered"), str) and answered:
        arrow = f"→ {_tg_escape(answered)}"
        if len(parts[-1]) + 2 + len(arrow) <= TELEGRAM_LIMIT:
            parts[-1] = f"{parts[-1]}\n\n{arrow}"
    return parts


def ping_keyboard(data: dict) -> dict | None:
    """A ping's buttons: its suggested replies while they are open, then «В Базу · N» and «Запушить · N» while it
    carries them."""
    buttons = list(data.get("buttons") or []) if data.get("open") else []
    if isinstance(base := data.get("base"), list) and len(base) == 2:
        buttons.append(base)
    buttons += [b for b in data.get("push") or [] if isinstance(b, list) and len(b) == 2]
    return rows_of(buttons)


def refused(exc: TelegramError) -> bool:
    """Telegram's 400: the same request would be refused again. A 429, a 5xx or the network may pass."""
    return exc.code == 400


def view_of(data: dict) -> CardView:
    """A card as the platform sent it, in JSON: the `link` pair comes back as a list."""
    return CardView(list(data["head"]), str(data.get("body") or ""), str(data.get("status") or ""),
                    tuple(data.get("link") or ()))


def _input_id(native_id: str) -> str:
    return f"tg:{native_id}"


def _echo(heard: str) -> str:
    """«🎙 «…»» as Telegram HTML: the transcript on one line, escaped, cut at ECHO_CHARS of its escaped length."""
    out, size = [], 0
    for char in " ".join(heard.split()):
        piece = _tg_escape(char)
        if size + len(piece) > ECHO_CHARS:
            out.append("…")
            break
        out.append(piece)
        size += len(piece)
    return f"🎙 «{''.join(out)}»"


def _wait_line(row: dict, now: float, group: str) -> str:
    """One row of `/wait` as Telegram HTML: its group's name over the first row of a group, then «<icon> <where> ·
    <title> · <how long>», its one line, and the session it belongs to. Every word escaped: titles and lines are the
    owner's, a model's or a stranger's."""
    since = row.get("since")
    waited = "" if not isinstance(since, (int, float)) else \
        f"через {ago(since - now)}" if since > now else ago(now - since)
    parts = [str(row.get(k) or "") for k in ("where", "title")] + [waited]
    text = f"{ICONS.get(str(row.get('source')), '•')} " + " · ".join(_tg_escape(p) for p in parts if p)
    if row.get("essence"):
        text += f"\n{_tg_escape(str(row['essence']))}"
    if isinstance(row.get("session"), str) and row["session"]:
        text += f"\n<code>сессия {_tg_escape(row['session'])}</code>"
    return f"<b>{_tg_escape(group)}</b>\n{text}" if group else text


def _said(answer: dict) -> str:
    return str(answer.get("say") or answer.get("error") or "Платформа ответила без текста.")
