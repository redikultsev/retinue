"""The face: the owner's app on his phone — a PWA, and the same page opened as a Telegram Mini App — served by the
platform on a port of its own (`face_port`), behind the face's nginx and Traefik, which add nothing secret: every rule
of who may do what is here.

Who: the owner, and only after a login. A login is a one-time code the bot gives him for `/app` (`new_code`, through
the router's `/owner/login`) or the Mini App's `initData`, signed by Telegram with a key derived from the bot token
(`RETINUE_WEBAPP_KEY`; the platform never holds the token itself). Either sets the cookie `__Host-retinue`: 32 random
bytes, kept only as their SHA-256, for 30 days from the last use. A request that changes anything is a POST with the
header `X-Retinue: 1` and, when the browser names an origin, the face's own — a page elsewhere can neither read the
answers nor send that header without asking first. Ten failed logins in ten minutes close every login for ten.

What: a snapshot (`/api/sessions`, `/api/sessions/{id}`) as of a cursor, then the store's `events` after it as
Server-Sent Events — every event an upsert of one whole object, so applying them in order converges. One stream
carries every session. A stream reads the rows itself at its own pace, so a slow phone holds back no one; the store
only wakes it. Each event's `data` is the bare object, never wrapped. A stream ends with its login (a logout, or the
login's 30 days, checked at every ping); when 16 are open, a new one closes the oldest — a phone that went away
without closing its connection never locks the owner out. The text of a message while it is written comes on the same stream as `delta`, without a number: it
is never stored, and the message's own item replaces it.

The owner's voice (`/api/speech`): what he dictated, as text for him to edit and send himself — or, in English, word
for word and sent at once by the app. One recording at a time, held in memory only; the transcript is deleted at
ElevenLabs (`speech.Scribe`) and never written to a log.

The owner's actions here are the platform's own (`Platform.new_session`, `side`, `write`, `decide`, `answer`, `stop`):
a card decided or a question answered in the face obeys exactly the rules of a Telegram press, and the Telegram card
is rewritten.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
from collections import deque
from pathlib import PurePosixPath
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote

from aiohttp import web

from . import changes, feed, inbox, signals, spaces, tobase, topush
from .cliengine import UUID4
from .sessions import PlatformStore
from .speech import Scribe, SpeechError
from .spoken import VERBATIM_MARK

if TYPE_CHECKING:
    from .config import PlatformConfig
    from .platformd import Platform

log = logging.getLogger("retinue.face")

SCHEMA = 1
STATS_WEEKS = 8           # the «Week» screen: this many seven-day spans, for its sparklines
WEEK_S = 7 * 86400
COOKIE = "__Host-retinue"
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L: typed by hand into an installed PWA
CODE_LEN = 10
CODE_TTL_S = 600
LOGIN_TTL_S = 30 * 86400
LOGIN_SLIDE_S = 86400       # a login is extended, and its cookie set again, at most once a day
INIT_DATA_TTL_S = 300       # a Mini App's initData older than this is not a login
INIT_DATA_SKEW_S = 60       # one from the future: a clock off by this much still passes
FAIL_WINDOW_S, FAIL_LIMIT, FAIL_BLOCK_S = 600, 10, 600
PING_S = 15.0
WRITE_TIMEOUT_S = 30.0      # a stream whose phone takes no bytes this long is closed; it reconnects
RETRY_MS = 3000             # what the browser waits before it reconnects a stream
MAX_STREAMS = 16           # open streams; one more closes the oldest
EVENTS_PER_READ = 200
DELTA_BACKLOG = 2000        # a stream's unsent text pieces; past this the oldest are dropped — the item comes anyway
INPUT_ID = re.compile("face:" + UUID4.pattern)
ANY_INPUT_ID = re.compile(r"(?:tg|face):[0-9a-f-]{1,64}")  # a thumbnail's input: the router's or the face's
ACTIONS = ("approve", "reject")
# A handed-over file served by itself: a document of no origin that loads nothing (the hub's /pages/ policy).
FILE_CSP = "default-src 'none'; sandbox"
# An HTML file in the face's frame: its own inline styles and data: images and fonts, nothing else, no script.
VIEW_CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "connect-src 'none'; form-action 'none'; base-uri 'none'; sandbox")
NO_FRAME = "'none'"
# A recording: 10 minutes at most in the app. 20 MB holds that at 256 kbit/s — above what Safari's AAC or Chrome's Opus
# take for a voice (the app asks for 64) — and one at a time keeps it a small share of the platform's memory.
SPEECH_MAX = 20 * 2**20
SPEECH_CHUNK = 64 * 1024
GONE_CHECK_S = 0.25         # how often a transcription in flight looks whether the app is still there
SPEECH_TYPES = {"audio/webm": "webm", "video/webm": "webm", "audio/mp4": "m4a", "video/mp4": "m4a",
                "audio/x-m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav"}
# dictation: Scribe drops fillers and false starts and expects the owner's terms; any language (he mixes ru and en).
# en: English, word for word — the «uh» and the slips are what the session's corrections work on.
SPEECH_MODES = {"dictate": {"no_verbatim": True}, "en": {"language": "en", "no_verbatim": False}}
CODE_FAILED = "The code did not work: it is wrong, expired or already used. Send the bot /app again."
TELEGRAM_FAILED = "Telegram did not confirm the login. Close the app and open it again from the chat with the bot."


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_code(store: PlatformStore, now: float) -> str:
    """A one-time code for `/app`: 10 characters, 10 minutes, kept only as its hash; the codes before it are void."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    store.add_code(_hash(code), now + CODE_TTL_S)
    return code


def normal_code(code: str) -> str:
    """A code as the owner may type it: any case, with spaces or dashes."""
    return re.sub(r"[\s-]", "", code).upper()


def init_data_ok(init_data: str, key_hex: str, owner: int, now: float) -> bool:
    """Telegram's check of a Mini App's `initData`: every field but `hash`, sorted, `key=value` per line, signed with
    HMAC-SHA256 under HMAC_SHA256("WebAppData", bot token) — given to the platform as `key_hex`. Then it must be
    fresh and the user the owner."""
    if not key_hex or not owner or not isinstance(init_data, str):
        return False
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    fields = dict(pairs)
    given = fields.pop("hash", "")
    if len(fields) + 1 != len(pairs) or not re.fullmatch(r"[0-9a-fA-F]{64}", given):  # a field twice, or no hash
        return False
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    expected = hmac.new(bytes.fromhex(key_hex), check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given.lower()):
        return False
    try:
        auth_date, user = int(fields.get("auth_date", "")), json.loads(fields.get("user", ""))
    except (ValueError, RecursionError):
        return False
    if not now - INIT_DATA_TTL_S <= auth_date <= now + INIT_DATA_SKEW_S:
        return False
    return isinstance(user, dict) and type(user.get("id")) is int and user["id"] == owner


@dataclass(eq=False)
class _Stream:
    """One open `/api/events`: the login it was opened with, woken when the store writes, and the text pieces given
    since, each after the event that was the last one when it came — so it goes out after that event and before the
    next."""
    login: str
    request: web.Request | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    deltas: deque = field(default_factory=lambda: deque(maxlen=DELTA_BACKLOG))
    closed: bool = False
    writing: bool = False

    def close(self) -> None:
        """Ended from outside: its loop stops and the response ends cleanly; a write stuck on a dead connection is
        cut now instead of after its timeout."""
        self.closed = True
        self.wake.set()
        if self.writing and self.request is not None and self.request.transport is not None:
            self.request.transport.close()


class Hub:
    """The open streams, oldest first. Writing to them is theirs; the hub only wakes them and hands out text pieces,
    so nothing the platform does ever waits on a phone."""

    def __init__(self, store: PlatformStore) -> None:
        self.store = store
        self.streams: dict[_Stream, None] = {}  # a dict for its order: the first is the oldest
        self.closing = False

    def open(self, stream: _Stream) -> None:
        """A new stream; at the cap the oldest is closed for it."""
        while len(self.streams) >= MAX_STREAMS:
            oldest = next(iter(self.streams))
            self.streams.pop(oldest)
            oldest.close()
        self.streams[stream] = None

    def drop(self, stream: _Stream) -> None:
        self.streams.pop(stream, None)

    def logout(self, login: str) -> None:
        for stream in [s for s in self.streams if s.login == login]:
            self.drop(stream)
            stream.close()

    def wake(self) -> None:
        for stream in self.streams:
            stream.wake.set()

    def delta(self, session: str, text: str) -> None:
        if not self.streams:
            return
        after = self.store.cursor()
        for stream in self.streams:
            stream.deltas.append((after, session, text))
            stream.wake.set()

    def close(self) -> None:
        self.closing = True
        self.wake()


def _frame(data: dict | str, kind: str, number: int | None = None) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return (f"id: {number}\n" if number is not None else "") + f"event: {kind}\ndata: {text}\n\n"


def frames(rows: list[tuple[int, str, str]], deltas: deque, cursor: int) -> tuple[str, int]:
    """The rows read after `cursor` as SSE, with every text piece given after an event placed right after it; the
    pieces of one session in a row go out as one. The text and the new cursor; pieces that belong after events not
    read yet stay queued."""
    out: list[str] = []
    pieces: list[list[str]] = []  # [session, text]

    def take(upto: int) -> None:
        while deltas and deltas[0][0] <= upto:
            _, session, text = deltas.popleft()
            if pieces and pieces[-1][0] == session:
                pieces[-1][1] += text
            else:
                pieces.append([session, text])
        out.extend(_frame({"session": session, "text": text}, "delta") for session, text in pieces)
        pieces.clear()

    for number, kind, data in rows:
        take(number - 1)
        out.append(_frame(data, kind, number))
        cursor = number
    take(cursor)
    return "".join(out), cursor


def general_track(cfg: PlatformConfig) -> str | None:
    """The track the app's «＋» starts a session on: a general one in the root of the Base, which finds its own context
    and sphere — the first track on the Base's lane whose directory is the Base itself (`base`, `./base`, `base/` are
    one); with no Base, the first private track. Never a repository's track (`code`), never one on a retired lane:
    None then, and the app says so instead of sending."""
    tracks = [t for t in cfg.tracks if t.kind != "code" and ((lane := cfg.lanes.get(t.lane)) is None or not lane.retired)]
    if cfg.base is not None:
        root = PurePosixPath(cfg.base.tree)
        for t in tracks:
            if t.lane == cfg.base.lane and PurePosixPath(t.cwd) == root:
                return t.name
    return tracks[0].name if tracks else None


def _error(status: int, error: str) -> web.Response:
    return web.json_response({"error": error}, status=status)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class Face:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store, self.cfg = platform, platform.store, platform.cfg
        self.hub = Hub(self.store)
        self.store.listener = self.hub.wake
        platform.on_delta = self.hub.delta
        self.spaces = spaces.Spaces(platform)   # the Base read in the app: `/api/spaces…`, behind the same guard
        # The owner's voice: through the platform's own proxy; transcripts still to delete in the platform's store.
        self.scribe = Scribe(self.cfg.stt_key, store=self.store, proxy=self.cfg.speech.proxy)
        self.hearing = False                    # one recording at a time
        self.finishing: set[asyncio.Task] = set()  # cancelled ones ElevenLabs already had, ending to be deleted
        self.sweeper: asyncio.Task | None = None

    def clock(self) -> float:
        return self.platform.clock()

    def close(self) -> None:
        self.hub.close()
        for task in (self.sweeper, *self.finishing):
            if task is not None:
                task.cancel()

    async def _sweep(self, app: web.Application) -> None:
        """At start, in the background: the transcripts an earlier run could not delete at ElevenLabs."""
        self.sweeper = asyncio.create_task(self.scribe.sweep())

    def app(self) -> web.Application:
        # Bodies up to the owner's attachments in one message; the guard turns away a request without a login before
        # its body is read.
        app = web.Application(middlewares=[self._guard], client_max_size=inbox.BODY_MAX)
        app.add_routes([
            web.post("/api/auth/code", self.auth_code), web.post("/api/auth/telegram", self.auth_telegram),
            web.post("/api/auth/logout", self.logout),
            web.get("/api/meta", self.meta), web.get("/api/sessions", self.sessions),
            web.post("/api/sessions", self.new), web.get("/api/sessions/{id}", self.session),
            web.post("/api/sessions/{id}/input", self.input), web.post("/api/sessions/{id}/stop", self.stop),
            web.post("/api/sessions/{id}/pin", self.pin), web.post("/api/sessions/{id}/side", self.side),
            web.post("/api/sessions/{id}/archive", self.archive),
            web.get("/api/sessions/{id}/diff", self.diff), web.get("/api/approvals/{id}", self.approval),
            web.get("/api/sessions/{id}/inputs/{input}/{n:\\d{1,2}}", self.thumb),
            web.post("/api/approvals/{id}/decide", self.decide), web.post("/api/approvals/{id}/answer", self.answer),
            web.get("/api/files/{id}", self.file), web.get("/api/files/{id}/view", self.view),
            web.get("/api/files/{id}/meta", self.file_meta), web.get("/api/feed", self.feed),
            web.get("/api/stats", self.stats),
            web.get("/api/events", self.events),
            # Spheres (`spheres.py`): a session's sphere, its sphere's main session, a fork, what is unread.
            web.post("/api/sessions/{id}/sphere", self.sphere), web.post("/api/sessions/{id}/main", self.main),
            web.post("/api/sessions/{id}/seen", self.seen), web.post("/api/sessions/{id}/fork", self.fork),
            # «To the Base» (`tobase.py`): the session's commit card, the same one its Telegram copies show.
            web.post("/api/sessions/{id}/base", self.base_open), web.get("/api/base/{id}", self.base_card),
            web.get("/api/base/{id}/diff", self.base_diff), web.post("/api/base/{id}/{action:toggle|subject|commit|cancel}",
                                                                     self.base_act),
            # «Push» (`topush.py`): a repository's push card, the same one its Telegram copies show.
            web.post("/api/sessions/{id}/push", self.push_open), web.get("/api/push/{id}", self.push_card),
            web.get("/api/push/{id}/diff", self.push_diff),
            web.post("/api/push/{id}/{action:target|title|send|cancel}", self.push_act),
            *self.spaces.routes(),
            # «Read» (`library.py`): the hub's pages, what was opened and finished.
            web.get("/api/library", self.library), web.post("/api/library/{id}/{action:open|done}", self.library_act),
            # «Waiting for you»: a press on the assistant's row, for the router to apply (`feed.PRESSES`).
            web.post("/api/waiting/press", self.waiting_press),
            # …and its sheet: what the row is about and where it is, asked of the router (`feed.Details`).
            web.post("/api/waiting/detail", self.waiting_detail),
            # The owner's voice (`speech.py`): a recording in, its text out.
            web.post("/api/speech", self.speech)])
        app.on_startup.append(self._sweep)
        return app

    # --- who may ---------------------------------------------------------------------------------------------------

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        return _private(await self._guarded(request, handler))

    async def _guarded(self, request: web.Request, handler) -> web.StreamResponse:
        """A POST carries `X-Retinue: 1` and no foreign origin; a login is refused while logins are closed; every
        other call needs a live cookie, which a use extends."""
        now = self.clock()
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if request.headers.get("X-Retinue") != "1" or (origin is not None and origin != self.cfg.face_origin):
                return _error(403, "The request is not from the app's page.")
        if request.path.startswith("/api/auth/"):
            if self.store.auth_blocked(now):
                return _error(429, "Too many failed logins — try again in ten minutes.")
            return await handler(request)
        token = request.cookies.get(COOKIE, "")
        expires = self.store.login_expires(_hash(token), now) if token else None
        if expires is None:
            return _error(401, "Log in first: send the bot /app.")
        response = await handler(request)
        if not response.prepared and expires - now < LOGIN_TTL_S - LOGIN_SLIDE_S:
            self.store.extend_login(_hash(token), now + LOGIN_TTL_S)
            _set_cookie(response, token)
        return response

    def _logged_in(self, via: str, now: float) -> web.Response:
        token = secrets.token_urlsafe(32)
        self.store.add_login(_hash(token), via, now, now + LOGIN_TTL_S)
        response = web.json_response({"ok": True})
        _set_cookie(response, token)
        return response

    def _failed(self, now: float, why: str) -> web.Response:
        self.store.auth_failed(now, FAIL_WINDOW_S, FAIL_LIMIT, FAIL_BLOCK_S)
        return _error(401, why)

    async def auth_code(self, request: web.Request) -> web.Response:
        now, code = self.clock(), (await _body(request)).get("code")
        if not isinstance(code, str) or not self.store.use_code(_hash(normal_code(code)), now):
            return self._failed(now, CODE_FAILED)
        return self._logged_in("code", now)

    async def auth_telegram(self, request: web.Request) -> web.Response:
        now, init_data = self.clock(), (await _body(request)).get("init_data")
        if not init_data_ok(init_data, self.cfg.webapp_key, self.cfg.owner_tg, now):
            return self._failed(now, TELEGRAM_FAILED)
        return self._logged_in("telegram", now)

    async def logout(self, request: web.Request) -> web.Response:
        if token := request.cookies.get(COOKIE, ""):
            self.store.drop_login(_hash(token))
            self.hub.logout(_hash(token))  # its open streams end with it
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
        return response

    # --- what the face reads ---------------------------------------------------------------------------------------

    async def meta(self, request: web.Request) -> web.Response:
        return web.json_response({"schema": SCHEMA, "owner_ok": True, "face_origin": self.cfg.face_origin,
                                  "tracks": [{"name": t.name, "lane": t.lane, "kind": t.kind}
                                             for t in self.cfg.tracks],
                                  "general": general_track(self.cfg),
                                  "spheres": self.platform.spheres.listed(), "speech": self._speech_off() is None})

    async def sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"schema": SCHEMA, "cursor": self.store.cursor(),
                                  "sessions": self.store.face_sessions(self.clock())})

    async def session(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        if (session := self.store.face_session(session_id)) is None:
            return _error(404, "No such session.")
        return web.json_response({"schema": SCHEMA, "cursor": self.store.cursor(), "session": session,
                                  "items": self.store.face_items(session_id),
                                  "approvals": self.store.face_approvals(session_id),
                                  "summaries": self.store.face_summaries(session_id),
                                  "inputs": self.store.face_inputs(session_id),
                                  "files": self.platform.out.face_files(session_id),
                                  "base_cards": self.platform.tobase.face_cards(session_id),
                                  "push_cards": self.platform.topush.face_cards(session_id),
                                  "about": self.store.face_about(session_id)})

    async def feed(self, request: web.Request) -> web.Response:
        """«Waiting for you»: one queue by action across the sessions and the assistant's world (`feed.Feed`). Built on
        each read; the app reads it again when a session, a card or a summary changes, and once a minute."""
        return web.json_response({"schema": SCHEMA, **self.platform.feed.face(self.clock())})

    async def stats(self, request: web.Request) -> web.Response:
        """The «Week» screen: the platform's tally (`signals.tally`) of the last STATS_WEEKS seven-day spans, the
        newest first, each ending where the next one starts — the screen compares and draws them, never counts."""
        now = self.clock()
        weeks = [signals.tally(self.store, now - (n + 1) * WEEK_S, now - n * WEEK_S) for n in range(STATS_WEEKS)]
        return web.json_response({"schema": SCHEMA, "now": now, "weeks": weeks})

    async def waiting_press(self, request: web.Request) -> web.Response:
        """«Done», «Not a task», «Tomorrow» or «Undo» (`open`) on a row of «From the assistant» `{t, action}`: only its
        shape is checked here — the router alone knows what the row's token names — and it goes to the router as a
        note (`waiting-press`), which the bridge pulls like any other."""
        body = await _body(request)
        token, action = body.get("t"), body.get("action")
        if not isinstance(token, str) or not feed.TOKEN.fullmatch(token) or action not in feed.PRESSES:
            return _error(400, "Need t — the row's token — and action: done, dismissed, later or open.")
        self.store.add_note("waiting-press", "", {"t": token, "action": action})
        return web.json_response({"ok": True})

    async def waiting_detail(self, request: web.Request) -> web.Response:
        """A row of «From the assistant» opened `{ref, t?}`: what it is about and where it is — her judgement, the
        sender, the deadline, the letter's thread or the chat — which only the router has. Asked of it through
        `feed.Details` (the row's id and token, nothing else) and answered when the router has taken it, within
        `feed.DETAIL_WAIT_S`; relayed, never kept. Only a row of the digest on record — a person's with its own token, or
        a draft that is not the private matter's: 410 when it is not there any more or the router no longer knows it,
        504 when the router is silent, 502 when its answer is off the contract."""
        body = await _body(request)
        ref, token = body.get("ref"), body.get("t")
        items, _, _ = self.platform.feed.digest()
        row = next((i for i in items if isinstance(ref, str) and i["id"] == ref), None)
        if not isinstance(ref, str) or not feed.DIGEST_ID.fullmatch(ref) or \
                (token is not None and (not isinstance(token, str) or not feed.TOKEN.fullmatch(token))):
            return _error(400, "Need ref — the row's id — and t, its token, on a person's row.")
        if row is None or (row["kind"] in ("letter", "chat") and (token is None or row.get("t") != token)) or \
                (row["kind"] == "draft" and (not feed.DRAFT_REF.fullmatch(ref) or row["who"] == feed.PRIVATE_WHO)) or \
                row["kind"] == "deadline":
            return _error(410, "This row is no longer in the list.")
        details = self.platform.details
        number = details.ask(ref, token)
        if number is None:
            return _error(429, "Too many rows asked at once — try again in a moment.")
        how, detail = await details.wait(number)
        if how == "ok":
            return web.json_response({"detail": detail})
        if how == "gone":
            return _error(410, "The assistant no longer has this row: the list is out of date.")
        return _error(504 if how == "late" else 502, "The assistant's side did not answer in time." if how == "late"
                      else "The assistant's side could not say — try again.")

    async def library(self, request: web.Request) -> web.Response:
        """«Read»: the hub's pages as the router last pushed them, with what the owner opened and finished."""
        return web.json_response({"schema": SCHEMA, **self.platform.library.face()})

    async def library_act(self, request: web.Request) -> web.Response:
        """`open`: he opened the page (it goes to «Continue»); `done {done}`: «Finished», or taken back."""
        page_id, action, body = request.match_info["id"], request.match_info["action"], await _body(request)
        if action == "done" and type(body.get("done")) is not bool:
            return _error(400, "done: true or false.")
        library = self.platform.library
        page = library.opened(page_id, self.clock()) if action == "open" else \
            library.finished(page_id, body["done"], self.clock())
        if page is None:
            return _error(404, "There is no such page in the library.")
        return web.json_response({"page": page})

    async def thumb(self, request: web.Request) -> web.Response:
        """A picture the owner sent into a session, as the small JPEG the platform made of its own re-encoding
        (`inbox.thumbnail`) — never the bytes he sent."""
        input_id = request.match_info["input"]
        jpeg = self.store.input_thumb(request.match_info["id"], input_id, int(request.match_info["n"])) \
            if ANY_INPUT_ID.fullmatch(input_id) else None
        if jpeg is None:
            return _error(404, "No such image.")
        return web.Response(body=jpeg, content_type="image/jpeg",
                            headers={"Content-Disposition": "inline", "Content-Security-Policy": "default-src 'none'"})

    async def approval(self, request: web.Request) -> web.Response:
        """One card, for the link a Telegram card's Mini App button opens (`/a/<id>`)."""
        if (approval := self.store.face_approval(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        return web.json_response({"approval": approval})

    async def diff(self, request: web.Request) -> web.Response:
        """What the session's own edits changed, as a unified diff — built from its transcript (`changes`), never by
        running anything in the lane. `cwd`: the directory the session ran in, or the track's as configured."""
        session_id = request.match_info["id"]
        if (session := self.store.session(session_id)) is None:
            return _error(404, "No such session.")
        cwd, found = self.store.changes_of(session_id)
        if not cwd and (track := self.platform.tracks.get(session["track"])) is not None:
            cwd = self.platform.cwd_of(session, track)
        return web.json_response(await asyncio.to_thread(changes.render, found, cwd))

    async def file(self, request: web.Request) -> web.StreamResponse:
        """A file a session handed over (`outfiles`), as a download: served as the type its extension is on the list
        with, never sniffed, and under the sandbox policy — opened by itself, even an SVG is a document of no origin,
        never the face's."""
        if (found := self.platform.out.content(request.match_info["id"])) is None:
            return _error(404, "No such file.")
        data, body = found
        return web.Response(body=body, headers={"Content-Type": data["mime"], "Content-Security-Policy": FILE_CSP,
                                                "Content-Disposition": _attachment(data["name"])})

    async def file_meta(self, request: web.Request) -> web.Response:
        """One handed-over file's row, for the page a Telegram message's «Open» opens (`/f/<id>`)."""
        if (found := self.platform.out.face_file(request.match_info["id"])) is None:
            return _error(404, "No such file.")
        return web.json_response({"file": found})

    async def view(self, request: web.Request) -> web.StreamResponse:
        """An HTML file a session handed over, for the face's frame: inline, under the hub's /pages/ rules — the
        `sandbox` policy gives it an opaque origin (no cookie, no API, no storage of the face's), no script runs, and
        nothing loads but its own inline styles and `data:` images. Only the face may frame it."""
        found = self.platform.out.content(request.match_info["id"])
        if found is None or not found[0]["mime"].startswith("text/html"):
            return _error(404, "No such page.")
        policy = f"{VIEW_CSP}; frame-ancestors {self.cfg.face_origin or NO_FRAME}"
        return web.Response(body=found[1], headers={"Content-Type": found[0]["mime"],
                                                    "Content-Security-Policy": policy})

    async def events(self, request: web.Request) -> web.StreamResponse:
        """The events after the client's cursor, then each as it is written; `: ping` when nothing came for 15 s.
        A cursor whose events were pruned gets 410: the client reloads its snapshot."""
        given = request.headers.get("Last-Event-ID") or request.query.get("since")
        if given is not None and not given.isdigit():
            return _error(400, "since: not a number")
        cursor = int(given) if given is not None else self.store.cursor()
        if self.store.events_gone(cursor):
            return _error(410, "The events from this point are already gone — load the sessions again.")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                               "X-Accel-Buffering": "no"})
        stream = _Stream(_hash(request.cookies.get(COOKIE, "")), request)
        self.hub.open(stream)
        try:
            await response.prepare(request)
            await self._send(response, f"retry: {RETRY_MS}\n\n")
            await self._pump(response, stream, cursor)
        except (ConnectionError, TimeoutError):
            pass  # the phone went away, or takes nothing, or the stream was closed: it reconnects with Last-Event-ID
        finally:
            self.hub.drop(stream)
        return response

    async def _pump(self, response: web.StreamResponse, stream: _Stream, cursor: int) -> None:
        while not (self.hub.closing or stream.closed):
            stream.wake.clear()
            if self.store.events_gone(cursor):  # pruned under a stream that lagged a week: it reconnects into a 410
                return
            rows = self.store.events_after(cursor, EVENTS_PER_READ)
            text, cursor = frames(rows, stream.deltas, cursor)
            if text:
                await self._send(response, text, stream)
            if len(rows) == EVENTS_PER_READ or stream.wake.is_set():
                continue
            try:
                await asyncio.wait_for(stream.wake.wait(), PING_S)
            except TimeoutError:
                if self.store.login_expires(stream.login, self.clock()) is None:  # logged out, or its 30 days are over
                    return
                await self._send(response, ": ping\n\n", stream)

    @staticmethod
    async def _send(response: web.StreamResponse, text: str, stream: _Stream | None = None) -> None:
        if stream is not None:
            stream.writing = True
        try:
            await asyncio.wait_for(response.write(text.encode()), WRITE_TIMEOUT_S)
        finally:
            if stream is not None:
                stream.writing = False

    # --- what the owner does -----------------------------------------------------------------------------------------

    async def new(self, request: web.Request) -> web.Response:
        """A new session: on `track`, in `sphere` (optional) — a sphere alone runs on its own track."""
        body = await _body(request)
        input_id, sphere, text = body.get("input_id"), body.get("sphere"), body.get("text") or ""
        known = self.platform.spheres.by_name.get(sphere) if isinstance(sphere, str) else None
        track = body.get("track") or (known.track if known else None)
        if not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id) or not isinstance(track, str) or \
                not isinstance(text, str) or not (sphere is None or isinstance(sphere, str)):
            return _error(400, "input_id (face:<uuid4>), track or sphere, text: invalid")
        answer = await self.platform.new_session(input_id, track, text, attachments=body.get("attachments"),
                                                 sphere=sphere)
        if not answer["ok"]:
            return _error(400, answer["say"])
        return web.json_response({"session": answer["session"], "say": answer["say"]})

    async def input(self, request: web.Request) -> web.Response:
        body, session_id = await _body(request), request.match_info["id"]
        input_id, text, attachments = body.get("input_id"), body.get("text", ""), body.get("attachments")
        if not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id) or not isinstance(text, str) or \
                not (text.strip() or attachments):
            return _error(400, "input_id (face:<uuid4>), text: invalid")
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        return web.json_response({"say": await self.platform.write(input_id, session_id, text, attachments)})

    async def stop(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        return web.json_response({"ok": await self.platform.stop(session_id)})

    async def pin(self, request: web.Request) -> web.Response:
        """The owner's pin: kept on the session's row, so every device shows it, and sent as the session's event."""
        body, session_id = await _body(request), request.match_info["id"]
        if type(body.get("pinned")) is not bool:
            return _error(400, "pinned: true | false")
        if not self.store.set_pinned(session_id, body["pinned"]):
            return _error(404, "No such session.")
        return web.json_response({"session": self.store.face_session(session_id)})

    async def archive(self, request: web.Request) -> web.Response:
        """The owner's «Archive»: the session and its kin ended and out of the list on every device (`archive`)."""
        session_id = request.match_info["id"]
        if not await self.platform.archive(session_id):
            return _error(404, "No such session.")
        return web.json_response({"session": self.store.face_session(session_id)})

    async def sphere(self, request: web.Request) -> web.Response:
        """The owner moves a session to a sphere, or out of any (`null`); it is no sphere's main session there."""
        body, session_id = await _body(request), request.match_info["id"]
        sphere = body.get("sphere", False)
        if not (sphere is None or isinstance(sphere, str) and sphere in self.platform.spheres.by_name):
            return _error(400, "sphere: a sphere name from the settings, or null")
        if not self.store.set_sphere(session_id, sphere):
            return _error(404, "No such session.")
        return web.json_response({"session": self.store.face_session(session_id)})

    async def main(self, request: web.Request) -> web.Response:
        """The owner's main session of a sphere: marked, the sphere's previous one is main no more."""
        body, session_id = await _body(request), request.match_info["id"]
        if type(body.get("main")) is not bool:
            return _error(400, "main: true | false")
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        if not self.store.set_main(session_id, body["main"]):
            return _error(400, "Choose the session's sphere first.")
        return web.json_response({"session": self.store.face_session(session_id)})

    async def seen(self, request: web.Request) -> web.Response:
        """The owner has the session open: nothing in it is unread."""
        session_id = request.match_info["id"]
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        self.store.mark_seen(session_id, self.clock())
        return web.json_response({"session": self.store.face_session(session_id)})

    async def fork(self, request: web.Request) -> web.Response:
        """«Fork»: a new session from a copy of this one's conversation, with the track's full rights
        (`Spheres.fork`); the owner's first message to it is `text`."""
        body, session_id = await _body(request), request.match_info["id"]
        input_id, text = body.get("input_id"), body.get("text")
        if not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id) or not isinstance(text, str) or \
                not text.strip():
            return _error(400, "input_id (face:<uuid4>), text: invalid")
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        answer = await self.platform.spheres.fork(input_id, session_id, text.strip())
        if not answer["ok"]:
            return _error(400, answer["say"])
        return web.json_response({"session": answer["session"], "say": answer["say"]})

    async def side(self, request: web.Request) -> web.Response:
        """A side question about a session: a session of its own on the same track, never a message into this one
        (`Platform.side`)."""
        body, session_id = await _body(request), request.match_info["id"]
        input_id, text = body.get("input_id"), body.get("text")
        if not isinstance(input_id, str) or not INPUT_ID.fullmatch(input_id) or not isinstance(text, str) or \
                not text.strip():
            return _error(400, "input_id (face:<uuid4>), text: invalid")
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        answer = await self.platform.side(input_id, session_id, text.strip())
        if not answer["ok"]:
            return _error(400, answer["say"])
        return web.json_response({"session": answer["session"], "say": answer["say"]})

    async def decide(self, request: web.Request) -> web.Response:
        body, approval_id = await _body(request), request.match_info["id"]
        action, digest = body.get("action"), body.get("digest")
        if action not in ACTIONS or not isinstance(digest, str):
            return _error(400, "action: approve | reject; digest: string")
        if (approval := self.store.approval(approval_id)) is None or self.store.face_approval(approval_id) is None:
            return _error(404, "No such card.")
        toast, _ = await self.platform.decide(approval, action, digest, from_face=True)
        return web.json_response({"toast": toast, "approval": self.store.face_approval(approval_id)})

    async def answer(self, request: web.Request) -> web.Response:
        """An answer to one question of a question card: the options picked, or the owner's own text
        (`Platform.answer`, the rules of a Telegram press); the Telegram message of that question is rewritten."""
        body, approval_id = await _body(request), request.match_info["id"]
        if not isinstance(body.get("digest"), str) or type(body.get("question")) is not int:
            return _error(400, "digest: string; question: number; labels: [strings] or text: string")
        if (approval := self.store.approval(approval_id)) is None or self.store.face_approval(approval_id) is None:
            return _error(404, "No such card.")
        toast = await self.platform.answer(approval, body["question"], body.get("labels"), body.get("text"),
                                           body["digest"])
        return web.json_response({"toast": toast, "approval": self.store.face_approval(approval_id)})

    # --- the owner's voice -------------------------------------------------------------------------------------------

    def _speech_off(self) -> str | None:
        """Why the voice cannot work here, or None."""
        if not self.scribe.key:
            return "Voice is not set up: no ElevenLabs key (the platform's ELEVENLABS_API_KEY)."
        if not self.cfg.speech.proxy:
            return "Voice is not set up: the platform has no way to reach ElevenLabs (speech.proxy or code.proxy)."
        return None

    async def _abandon(self, call: asyncio.Task, upload: _Upload) -> None:
        """The app went away mid-transcription. Still being sent: cancelled — never sent, never paid. Sent: ElevenLabs
        has it all and bills it, so the call ends in the background and its transcript is deleted there; the app's
        next recording need not wait for it."""
        if not upload.sent:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            log.info("speech: cancelled by the app before the recording was sent")
            return
        self.finishing.add(call)
        call.add_done_callback(self._finished)
        log.info("speech: cancelled by the app after the recording was sent; finishing to delete it")

    def _finished(self, call: asyncio.Task) -> None:
        self.finishing.discard(call)
        if not call.cancelled():
            call.exception()  # retrieved: a failure was logged by the Scribe already

    async def speech(self, request: web.Request) -> web.Response:
        """`?mode=dictate|en`, the body the recording's bytes as the browser made them (WebM/Opus, MP4/AAC…):
        `{text}`, and in English also `{message}`, the text as the session gets it. Nothing kept but the transcript's
        id at ElevenLabs until it is deleted there."""
        mode = request.query.get("mode", "dictate")
        if mode not in SPEECH_MODES:
            return _error(400, "mode: dictate | en")
        if (ext := SPEECH_TYPES.get(request.content_type)) is None:
            return _error(415, "The recording is in the wrong format: WebM, MP4, OGG, MP3 or WAV is needed.")
        if why := self._speech_off():
            return _error(503, why)
        if (request.content_length or 0) > SPEECH_MAX:
            return _error(413, "The recording is too long: 10 minutes at most.")
        if self.hearing:
            return _error(429, "Already transcribing another recording — wait a little.")
        self.hearing = True
        try:
            data = await _read_at_most(request, SPEECH_MAX)
            if data is None:
                return _error(413, "The recording is too long: 10 minutes at most.")
            if not data:
                return _error(400, "The recording is empty.")
            options = dict(SPEECH_MODES[mode])
            if mode == "dictate":
                options["keyterms"] = self.cfg.speech.keyterms
            if _gone(request):  # «Cancel» while it was uploading: nothing goes to ElevenLabs
                return _error(400, "Cancelled.")
            upload = _Upload(data)
            del data
            call = asyncio.create_task(self.scribe.transcribe(upload, f"voice.{ext}", request.content_type, **options))
            try:
                while not (await asyncio.wait({call}, timeout=GONE_CHECK_S))[0]:
                    # Gone while a cancelled one still ends in the background: this one is waited out instead.
                    if _gone(request) and not (upload.sent and self.finishing):
                        await self._abandon(call, upload)
                        return _error(400, "Cancelled.")
            except asyncio.CancelledError:  # a server that cancels the handler when the app goes away
                await self._abandon(call, upload)
                raise
            try:
                text = call.result()
            except SpeechError as exc:
                return _error(502, f"Could not transcribe: {exc}.")
            if not text:
                return _error(422, "Could not make out the speech — record again.")
            return web.json_response({"text": text, **({"message": VERBATIM_MARK + text} if mode == "en" else {})})
        finally:
            self.hearing = False

    # --- «To the Base» -----------------------------------------------------------------------------------------------

    async def base_open(self, request: web.Request) -> web.Response:
        """The session's menu «To the Base», the button under its summary or its feed row: its card, opened or brought
        up to date — the same card a press in Telegram opens."""
        session_id = request.match_info["id"]
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        toast, card = await self.platform.tobase.open(session_id, telegram=False)
        return web.json_response({"toast": toast, "card": card})

    async def base_card(self, request: web.Request) -> web.Response:
        """One card, for the link a Telegram copy's Mini App button opens (`/b/<id>`)."""
        if (card := self.platform.tobase.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        return web.json_response({"card": self.platform.tobase.face(card)})

    async def base_diff(self, request: web.Request) -> web.Response:
        """The card's files turned on, as a unified diff the app renders (`base.BaseRepo.diff`, bounded)."""
        tobase_ = self.platform.tobase
        if (card := tobase_.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        text = await tobase_.diff(card)
        return web.json_response({"diff": text, "truncated": len(text.encode()) >= tobase.DIFF_LIMIT})

    async def base_act(self, request: web.Request) -> web.Response:
        """A file turned on or off (`{path, on}`), the subject (`{subject}`), «Commit», «Cancel»: by the rules of
        a Telegram press, and every Telegram copy rewritten."""
        body, action, tobase_ = await _body(request), request.match_info["action"], self.platform.tobase
        if (card := tobase_.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        if action == "toggle":
            if not isinstance(body.get("path"), str) or type(body.get("on")) is not bool:
                return _error(400, "path: string; on: true | false")
            toast, _ = tobase_.toggle(card, body["path"], body["on"])
        elif action == "subject":
            if not isinstance(body.get("subject"), str):
                return _error(400, "subject: string")
            toast, _ = tobase_.set_subject(card, body["subject"])
        elif action == "commit":
            toast, _ = await tobase_.commit(card)
        else:
            toast, _ = tobase_.cancel(card)
        now = tobase_.card(card["id"])
        return web.json_response({"toast": toast, "card": tobase_.face(now) if now else None})


    # --- «Push» -----------------------------------------------------------------------------------------------------

    async def push_open(self, request: web.Request) -> web.Response:
        """The session's «Push» (`{repo?}`): its card, opened or brought up to date — the same card a press in
        Telegram opens."""
        session_id, body = request.match_info["id"], await _body(request)
        if self.store.session(session_id) is None:
            return _error(404, "No such session.")
        repo = body.get("repo")
        if repo is not None and not isinstance(repo, str):
            return _error(400, "repo: string")
        toast, card = await self.platform.topush.open(session_id, repo, telegram=False)
        return web.json_response({"toast": toast, "card": card})

    async def push_card(self, request: web.Request) -> web.Response:
        """One card, for the link a Telegram copy's Mini App button opens (`/p/<id>`)."""
        if (card := self.platform.topush.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        return web.json_response({"card": self.platform.topush.face(card)})

    async def push_diff(self, request: web.Request) -> web.Response:
        topush_ = self.platform.topush
        if (card := topush_.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        text = await topush_.diff(card)
        return web.json_response({"diff": text, "truncated": len(text.encode()) >= topush.DIFF_LIMIT})

    async def push_act(self, request: web.Request) -> web.Response:
        """The remote branch (`{target}`), the PR's title (`{title}`), «Push», «Cancel»: by the rules of a
        Telegram press, and every Telegram copy rewritten."""
        body, action, topush_ = await _body(request), request.match_info["action"], self.platform.topush
        if (card := topush_.card(request.match_info["id"])) is None:
            return _error(404, "No such card.")
        if action in ("target", "title"):
            if not isinstance(body.get(action), str):
                return _error(400, f"{action}: string")
            toast, _ = (topush_.set_target if action == "target" else topush_.set_title)(card, body[action])
        elif action == "send":
            toast, _ = await topush_.send(card)
        else:
            toast, _ = topush_.cancel(card)
        now = topush_.card(card["id"])
        return web.json_response({"toast": toast, "card": topush_.face(now) if now else None})


class _Upload(io.BytesIO):
    """The recording as httpx reads it into its request: `sent` once its last piece was taken — from then on
    ElevenLabs has it all, and cancelling would only lose the transcript's id, never the bill."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.sent = False

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        if not chunk:
            self.sent = True
        return chunk


def _gone(request: web.Request) -> bool:
    """The app went away — «Cancel», or the phone lost the connection. aiohttp goes on with the handler regardless;
    nginx closes its connection to us when the browser closes its own."""
    return request.transport is None or request.transport.is_closing()


async def _read_at_most(request: web.Request, limit: int) -> bytes | None:
    """The body, or None when it is longer than `limit` — read by pieces, so a body without a length stops there."""
    parts, size = [], 0
    while chunk := await request.content.read(SPEECH_CHUNK):
        size += len(chunk)
        if size > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _attachment(name: str) -> str:
    """`Content-Disposition` for a download: an ASCII stand-in for old clients, the name itself as RFC 5987."""
    plain = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in name) or "file"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def _set_cookie(response: web.StreamResponse, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=LOGIN_TTL_S, path="/", secure=True, httponly=True, samesite="Strict")


def _private(response: web.StreamResponse) -> web.StreamResponse:
    if not response.prepared:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response
