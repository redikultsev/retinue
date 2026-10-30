"""The platform (`retinue-platform`): the one process that holds the sessions. It answers the lane CLIs' hooks (the
gate), pulls every lane's journal by number into its SQLite, folds the lines into session states, approval cards and
summaries, starts runs on the lanes within their caps, and serves the router: the owner's new sessions, his messages
into them, his presses on their cards, and the notes it has for him, by number. With a face origin configured it also
serves the owner's app (`face.py`) on a second port: the same sessions, cards and decisions, by other doors.

Everything that matters is in SQLite, and every lane line is ingested once (`UNIQUE(lane, lane_seq)`), so the
platform can die anywhere: a new one pulls the lines it did not confirm, and a line it had already stored changes
nothing again — no card is sent twice. The module is not `platform.py`: that name shadows the standard library's.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hmac
import json
import logging
import posixpath
import secrets
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web

from . import (approvals, attachments, cliengine, courier, face, feed, inbox, outfiles, pings, questions, signals, subs,
               tobase, topush, transcript)
from .base import BaseRepo, under
from . import library as library_mod
from . import code as code_mod
from .code import CodeRepos
from .spheres import Spheres
from .config import PlatformConfig
from .gate import Gate, retire
from .mail import Unavailable
from .outbox import Neighbour
from .sessions import PlatformStore, parsed

log = logging.getLogger("retinue.platformd")

POLL_S = 1.0                 # a lane's journal is pulled this often
POLL_LIMIT = 200             # lines per pull: lane_host's own ceiling
BACKOFF_MAX_S = 30.0
TICK_S = 60.0                # the clock: idle sessions closed, old raw lines pruned, unpressed cards expired
BUTTON_TTL_S = 7 * 86400
IDLE_CLOSE_S = 7 * 86400
CLEANUP_DAYS = 30            # the CLI's own transcripts in the lane
STOP_TIMEOUT_S = 5.0         # a lane's answer to the shutdown's /stop: a hung lane must not outlast the stop grace
LABELS = {approvals.APPROVE: "Разрешить", approvals.REJECT: "Отказать", approvals.OPEN: "Открыть"}
STALE = "Карточка устарела — решение не принято."
# A session of a lane no longer in platform.yaml (lane-code and lane-web, retired into the room): it stays listed and
# readable, and nothing goes into it.
RETIRED = "Полоса выключена: начни новую сессию."
HOOK_EVENTS = "|".join(cliengine.EVENTS)
# The prompt of the one resume a run stopped by the platform's own shutdown gets once the platform is back.
CONTINUE = "Платформа перезапускалась посреди твоего хода. Продолжи с того места, где остановился."
UNKNOWN_RUN = "unknown run"
HOOK_BODY_MAX = 16 * 1024 * 1024  # a hook carries a Write's content; only the owner's own calls carry his files
LIMIT_SAID_AGAIN_S = 120  # the CLI repeats its wait every half minute; a reset this close to the one said is the same


class LaneClient(Neighbour):
    """A lane host: its journal (`items`, `confirm`) and its closed command set."""

    async def run(self, body: dict) -> dict:
        return await self._call("POST", "/run", body)

    async def answer(self, session: str, prompt: str, content: dict | None = None) -> dict:
        """The owner's message into a live run: its text, or with attachments its content (`inbox.Ready.lane`)."""
        return await self._call("POST", "/answer", {"session": session, **(content or {"prompt": prompt})})

    async def stop(self, session: str, timeout: float = 60) -> dict:
        return await self._call("POST", "/stop", {"session": session}, timeout)


class Platform:
    def __init__(self, cfg: PlatformConfig, store: PlatformStore, lanes: dict[str, LaneClient] | None = None,
                 clock: Callable[[], float] = time.time, base: BaseRepo | None = None) -> None:
        if not cfg.router_token:
            raise SystemExit("the platform needs RETINUE_PLATFORM_TOKEN: without it anyone on its network is the owner")
        self.cfg, self.store, self.clock = cfg, store, clock
        store.clock = clock
        self.on_delta: Callable[[str, str], None] | None = None  # a message's text while it is written: the face
        # The lanes runs go to: every configured lane but a retired one (`LaneRef.retired`), whose sessions stay
        # readable and whose open runs hold no room.
        self.live = {name for name, lane in cfg.lanes.items() if not lane.retired}
        self.lanes = lanes if lanes is not None else {
            name: LaneClient(lane.url, lane.token, f"линия {name}") for name, lane in cfg.lanes.items()
            if name in self.live}
        self.tracks = {track.name: track for track in cfg.tracks}
        self.gate = Gate(store, cfg.lanes, cfg.hook_secret, clock, cfg.skills_mount, cfg.owner_memory_mount)
        self.tasks: list[asyncio.Task] = []
        self._draining = asyncio.Lock()
        self._sending: set[str] = set()  # runs whose `/run` is awaiting its answer
        self.out = outfiles.OutFiles(store, cfg.out_store_mb, cfg.out_day_mb)  # what sessions hand over from `out/`
        self.feed = feed.Feed(store, cfg)  # «Ждут тебя»: the face's home and `/wait`, with the assistant's digest
        self.library = library_mod.Library(store, cfg)  # «Читать»: the hub's pages, as the router pushes them
        self._looking = asyncio.Lock()
        pings.prepare(store)
        self.spheres = Spheres(self)  # the owner's spheres, their main sessions, forks (`spheres.py`)
        self.subs = subs.Subs(self)  # sub-sessions a session starts, their reports, the sphere line (`subs.py`)
        # The owner's Base cloned into a lane (`base.py`): kept fresh here, committed by his button.
        self.base = base if base is not None or cfg.base is None else BaseRepo(cfg.base, _lane_view(cfg))
        if self.base is not None:
            self.base.busy = self._base_busy
        # «В Базу»: who changed what in the Base and the owner's commit card (`tobase.py`).
        self.tobase = tobase.ToBase(self)
        self.feed.tobase = self.tobase  # the feed's «В Базу» rows, nobody's files among them
        # The owner's repositories in the room (`code.py`) and «Запушить», their push by his button (`topush.py`).
        self.code = CodeRepos(cfg.code, cfg.lanes[cfg.code.lane].work_mount) if cfg.code is not None else None
        self.topush = topush.ToPush(self)
        self.feed.topush = self.topush  # the feed's «Запушить» rows
        self.gate.push_lanes = frozenset({cfg.code.lane}) if cfg.code is not None else frozenset()
        self._fetching: set[str] = set()  # repositories whose first mirror is being fetched

    # --- the HTTP surface ---------------------------------------------------------------------------------------

    def app(self) -> web.Application:
        # The owner's attachments in his message (`inbox.BODY_MAX`); a hook is held to HOOK_BODY_MAX (`_hook_body`).
        app = web.Application(middlewares=[self._signed], client_max_size=max(16 * 1024 * 1024, inbox.BODY_MAX))
        app.add_routes([web.post("/hook/mcp", self.mcp), web.get("/hook/mcp", self.mcp_stream),
                        web.post(f"/hook/mark/{{n:{cliengine.MARK_N}}}", self.mark),
                        web.post(f"/hook/{{event:{HOOK_EVENTS}}}", self.hook),
                        web.post("/owner/new", self.owner_new), web.post("/owner/input", self.owner_input),
                        web.post("/owner/press", self.owner_press), web.post("/owner/login", self.owner_login),
                        web.post("/owner/file", self.owner_file), web.post("/owner/sphere", self.owner_sphere),
                        web.post("/owner/spheres", self.owner_spheres),
                        web.post("/owner/waiting", self.owner_waiting), web.post("/owner/feed", self.owner_feed),
                        web.post("/owner/library", self.owner_library), web.post("/owner/stats", self.owner_stats),
                        web.post("/items", self.items),
                        web.post("/items/confirm", self.confirm)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        """A hook is signed with its run's own token, which the gate checks; everything else only with the
        router's. A lane's token opens nothing here."""
        if request.path.startswith("/hook/"):
            return await handler(request)
        if not hmac.compare_digest(_bearer(request), self.cfg.router_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def hook(self, request: web.Request) -> web.Response:
        if (body := await _hook_body(request)) is None:
            return web.json_response({"error": "hook: тело больше 16 МБ"}, status=413)
        return web.json_response(await self.gate.hook(request.match_info["event"], _bearer(request), body))

    async def mark(self, request: web.Request) -> web.Response:
        if (body := await _hook_body(request)) is None:
            return web.json_response({"error": "hook: тело больше 16 МБ"}, status=413)
        return web.json_response(await self.gate.hook(f"mark/{int(request.match_info['n'])}", _bearer(request), body))

    async def mcp(self, request: web.Request) -> web.Response:
        """The sub-sessions' MCP server (`subs`): a run's own hook token names it, as a hook's does."""
        run = self.gate.run_of(_bearer(request))
        if run is None:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32001,
                                                                              "message": UNKNOWN_RUN}}, status=401)
        if (body := await _hook_body(request)) is None:
            return web.json_response({"error": "mcp: тело больше 16 МБ"}, status=413)
        status, answer = await self.subs.mcp(run, body)
        return web.json_response(answer, status=status) if answer is not None else web.Response(status=status)

    async def mcp_stream(self, request: web.Request) -> web.Response:
        """No server-sent stream: every answer comes in its POST's response (MCP's streamable HTTP allows 405)."""
        return web.Response(status=405)

    async def items(self, request: web.Request) -> web.Response:
        limit = (await _body(request)).get("limit")
        return web.json_response({"items": self.store.look(min(limit if type(limit) is int and limit > 0 else 50,
                                                               200))})

    async def confirm(self, request: web.Request) -> web.Response:
        upto = (await _body(request)).get("upto")
        if type(upto) is not int:
            return web.json_response({"error": "upto: не номер"}, status=400)
        return web.json_response({"confirmed": self.store.confirm(upto)})

    # --- the owner, through the router --------------------------------------------------------------------------

    async def owner_new(self, request: web.Request) -> web.Response:
        body = await _body(request)
        input_id, name, text = body.get("input_id"), body.get("track"), body.get("text") or ""
        if not all(isinstance(v, str) for v in (input_id, name, text)) or not input_id:
            return web.json_response({"error": "input_id, track, text: не строки"}, status=400)
        return web.json_response(await self.new_session(input_id, name, text, attachments=body.get("attachments")))

    async def owner_sphere(self, request: web.Request) -> web.Response:
        """`/<sphere> [text]` in the owner's chat (`Spheres.command`)."""
        body = await _body(request)
        input_id, name, text = body.get("input_id"), body.get("sphere"), body.get("text") or ""
        if not all(isinstance(v, str) for v in (input_id, name, text)) or not input_id:
            return web.json_response({"error": "input_id, sphere, text: не строки"}, status=400)
        return web.json_response(await self.spheres.command(input_id, name, text))

    async def owner_spheres(self, request: web.Request) -> web.Response:
        """The spheres, for the router: which `/<name>` are the platform's, and the bot's menu."""
        return web.json_response({"spheres": self.spheres.listed()})

    async def owner_input(self, request: web.Request) -> web.Response:
        """The owner's reply into a session: text, attachments (`inbox.given`), or both."""
        body = await _body(request)
        input_id, session_id, text = body.get("input_id"), body.get("session"), body.get("text", "")
        attachments = body.get("attachments")
        if not all(isinstance(v, str) and v for v in (input_id, session_id)) or not isinstance(text, str) or \
                not (text or attachments):
            return web.json_response({"error": "input_id, session, text: не строки"}, status=400)
        # The card the owner replied to: an open question takes his text as its own answer. A reply carrying files
        # is never an answer: the bridge sends no `card` with it, and even if one comes, it goes in as a message with
        # its files.
        card = body.get("card")
        if isinstance(card, str) and self.store.input_known(input_id):
            return web.json_response({"say": "Это сообщение сессия уже получила."})  # the router sent it again
        if isinstance(card, str) and not attachments and (say := self.tobase.reply(card, session_id, text)) is not None:
            return web.json_response({"say": say})  # a reply to a «В Базу» card: the commit's subject
        if isinstance(card, str) and not attachments and (say := self.topush.reply(card, session_id, text)) is not None:
            return web.json_response({"say": say})  # a reply to a «Запушить» card: its branch or its PR's title
        if isinstance(card, str) and not attachments and \
                (say := await self.reply_answer(card, session_id, text, input_id)) is not None:
            return web.json_response({"say": say})
        return web.json_response({"say": await self.write(input_id, session_id, text, attachments)})

    async def owner_press(self, request: web.Request) -> web.Response:
        button = (await _body(request)).get("button")
        now = self.clock()
        bound = self.store.peek_button(button, now) if isinstance(button, str) else None
        if bound is not None and bound["approval"].startswith(tobase.KEY):  # «В Базу»: its card keeps its state
            return web.json_response(await self.tobase.press(bound))
        if bound is not None and bound["approval"].startswith(topush.KEY):  # «Запушить»: the same, its own card
            return web.json_response(await self.topush.press(bound))
        if bound is not None and (ping := pings.seq_of(bound["approval"])) is not None:  # a ping's reply
            if self.store.spend_button(button, now) is None:
                return web.json_response({"toast": pings.GONE})
            return web.json_response({"toast": await pings.press(self, bound["digest"], ping,
                                                                 pings.reply_of(bound["action"]))})
        approval = self.store.approval(bound["approval"]) if bound else None
        # A multi-select's tick stays live — two fast taps both count; every other button acts once.
        tick = approval is not None and approval["kind"] == "question" and questions.is_tick(approval, bound["action"])
        spent = bound if tick else self.store.spend_button(button, now) if isinstance(button, str) else None
        if spent is None:
            return web.json_response({"toast": "Кнопка больше не действует."})
        if approval is not None and approval["kind"] == "question":
            return web.json_response(await self.pick(approval, spent["action"], spent["digest"]))
        if spent["action"] == approvals.OPEN:
            if approval is None or not self._pressable(approval, spent["digest"]):
                return web.json_response({"toast": STALE})
            keep = [[LABELS[a], self.store.add_button(approval["id"], a, approval["digest"], now + BUTTON_TTL_S)]
                    for a in (approvals.APPROVE, approvals.REJECT)]
            # The bridge edits the card with these at once; the note keeps an earlier one from bringing «Открыть» back.
            self.store.add_note("update", approval["session"], {"view": approval["view"], "buttons": keep},
                                approval=approval["id"])
            return web.json_response({"toast": "Ввод целиком — файлом.", "view": approval["view"], "keep": keep,
                                      "document": {"name": f"{approval['id']}.txt",
                                                   "text": approvals.input_text(approval)}})
        toast, view = await self.decide(approval, spent["action"], spent["digest"], from_face=False)
        return web.json_response({"toast": toast, "view": view, "keep": []} if view else {"toast": toast})

    async def owner_login(self, request: web.Request) -> web.Response:
        """`/app` in the owner's chat: a one-time code for the face, and the link that carries it."""
        if not self.cfg.face_origin:
            return web.json_response({"error": "Приложение не настроено: у платформы нет RETINUE_FACE_ORIGIN."})
        code = face.new_code(self.store, self.clock())
        return web.json_response({"code": code, "url": f"{self.cfg.face_origin}/login#{code}"})

    async def owner_file(self, request: web.Request) -> web.Response:
        """A file a session handed over, for the router to send: its name, type and bytes."""
        file_id = (await _body(request)).get("id")
        if not isinstance(file_id, str):
            return web.json_response({"error": "id: не строка"}, status=400)
        if (found := self.out.content(file_id)) is None:
            return web.json_response({"error": "Файла больше нет: файлы хранятся 30 дней."})
        payload = await asyncio.to_thread(outfiles.router_payload, *found)  # 20 MB in base64: not on the loop
        return web.Response(body=payload, content_type="application/json")

    async def owner_waiting(self, request: web.Request) -> web.Response:
        """The assistant's digest from the router (`feed.clean`): what waits for the owner in her world, replacing the
        one before it whole."""
        if request.content_length is None or request.content_length > feed.DIGEST_BYTES:
            return web.json_response({"error": f"дайджест больше {feed.DIGEST_BYTES} байт"}, status=413)
        now = self.clock()
        try:
            body = await _body(request)
            items, bot = feed.clean(body, now)
            chips = feed.clean_chips(body)
        except feed.Refused as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self.feed.keep(items, bot, now, chips)
        return web.json_response({"ok": True, "items": len(items)})

    async def owner_library(self, request: web.Request) -> web.Response:
        """The hub's list of pages from the router (`library.clean`), replacing the one before it whole."""
        if request.content_length is None or request.content_length > library_mod.BYTES:
            return web.json_response({"error": f"список страниц больше {library_mod.BYTES} байт"}, status=413)
        try:
            pages, dropped = library_mod.clean(await _body(request), self.library.hub)
        except library_mod.Refused as exc:
            return web.json_response({"error": str(exc)}, status=400)
        for page in dropped:
            log.warning("library: page %s dropped: %s", page["id"], page["why"])
        self.library.keep(pages, self.clock())
        return web.json_response({"ok": True, "pages": len(pages), "dropped": dropped})

    async def owner_stats(self, request: web.Request) -> web.Response:
        """How the work went between `since` and `until` (`signals.tally`): the weekly review's «Как шла неделя»."""
        body = await _body(request)
        since, until = body.get("since"), body.get("until", self.clock())
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (since, until)) or since >= until:
            return web.json_response({"error": "since, until: числа, since раньше until"}, status=400)
        return web.json_response(signals.tally(self.store, float(since), float(until)))

    async def owner_feed(self, request: web.Request) -> web.Response:
        """`/wait` in the owner's chat: the queue's first rows, as the router words them."""
        return web.json_response(self.feed.telegram(self.clock()))

    # --- what the owner does, from Telegram or from the face ------------------------------------------------------

    async def new_session(self, input_id: str, name: str, text: str, parent: str | None = None,
                          prompt: str | None = None, attachments: object = None, sphere: str | None = None,
                          fork_of: str | None = None, main: bool = False) -> dict:
        """A session on track `name` — or a sphere's name: its track, in that sphere — started with `text` and the
        owner's attachments; the same `input_id` again names the same session. A side session (`parent`) is sent
        `prompt` instead: its title stays the owner's own question. A fork (`fork_of`) starts from that session's
        conversation (`Spheres.fork`)."""
        if name in self.spheres.by_name and name not in self.tracks:
            sphere, name = name, self.spheres.by_name[name].track
        track = self.tracks.get(name)
        if track is None:
            known = f"; сферы: {', '.join(self.spheres.by_name)}" if self.spheres.by_name else ""
            return {"ok": False, "say": f"Нет такого трека: {name}. Есть: {', '.join(self.tracks)}{known}"}
        if sphere is not None and sphere not in self.spheres.by_name:
            return {"ok": False, "say": f"Нет такой сферы: {sphere}."}
        session = self.store.input_session(input_id)
        cwd = self.cwd_for(track, sphere)
        if session is None and self._code_track(track) and parent is None and fork_of is None:
            found = self._code_cwd(text)  # `/new code <repo> [text]`: the session starts in that repository
            if isinstance(found, str):
                return {"ok": False, "say": found}
            cwd, text = found
        ready = None
        if session is None and attachments:
            ready = await self._ready(text, attachments)
            if isinstance(ready, str):
                return {"ok": False, "say": ready}
        now = self.clock()
        session = session or self.store.input_session(input_id)  # the same message again while its files were read
        if session is None:
            session = self.store.new_session(track.name, track.lane, track.kind, now, parent=parent, sphere=sphere,
                                             fork_of=fork_of, main=main, cwd=cwd)
            if not self.store.add_input(input_id, session, text, now, *((ready.meta, ready.thumbs) if ready else ())):
                self._set_state(session, "closed", now)  # lost the race to a copy of this message: that one is it
                self.store.set_main(session, False)
                session = self.store.input_session(input_id)
            elif ready is not None and ready.meta:
                self.store.enqueue(session, text, now, ready.lane(self._today(now)))
                await self.drain()
            elif text:
                self.store.enqueue(session, prompt or text, now)
                await self.drain()
            else:
                self._set_state(session, "idle", now)
        said = f"Сессия {session} · {track.name}. Чтобы писать в неё, отвечай на её сообщения."
        return {"ok": True, "session": session, "say": _with_refused(said, ready)}

    def _code_track(self, track) -> bool:
        return self.code is not None and bool(self.cfg.code.track) and track.name == self.cfg.code.track

    def _code_cwd(self, text: str) -> tuple[str, str] | str:
        """`/new code <repo> [text]`: (the repository's folder in the room, the rest of the text), or what to say."""
        word, _, rest = text.strip().partition(" ")
        names = ", ".join(self.code.repos) or "нет ни одного"
        if word not in self.code.repos:
            return f"Назови репозиторий: /new {self.cfg.code.track} <имя> [задача]. Есть: {names}."
        return f"{self.cfg.code.root}/{word}", rest.strip()

    def cwd_for(self, track, sphere: str | None) -> str:
        """The directory a new session runs in: its sphere's, when the sphere names one (`base/travel`), else its
        track's."""
        found = self.spheres.by_name.get(sphere) if sphere else None
        return found.cwd if found is not None and found.cwd and found.track == track.name else track.cwd

    @staticmethod
    def cwd_of(session: dict, track) -> str:
        """Where a session runs: the directory it was made with; its track's for a session older than that column."""
        return session["cwd"] if session.get("cwd") is not None else track.cwd

    async def _ready(self, text: str, attachments: object) -> inbox.Ready | str:
        """The owner's message with its attachments made ready (`inbox`), or what to tell him when nothing of it can
        go in."""
        try:
            ready = await inbox.prepare(text, inbox.given(attachments))
        except inbox.Refused as exc:
            return f"{inbox.NOT_PASSED}: {exc}."
        if ready.empty:
            return f"{inbox.NOT_PASSED}: {'; '.join(ready.refused) or 'пустое сообщение'}."
        return ready

    def _today(self, now: float) -> str:
        """The date an inbox file is named by: the owner's own day."""
        return datetime.fromtimestamp(now, ZoneInfo(self.cfg.owner_tz)).date().isoformat()

    async def side(self, input_id: str, parent_id: str, question: str) -> dict:
        """A side question about session `parent_id`: a new session on its track, never a message into it. Its first
        prompt is the question after an excerpt of the parent's last items, built by code from the projection
        (`transcript.side_prompt`); the parent is only read."""
        asked = self.store.session(parent_id)
        if asked is None:
            return {"ok": False, "say": "Нет такой сессии."}
        prompt = transcript.side_prompt(asked["id"], asked["track"], self.store.items(asked["id"]), question,
                                        self.store.first_input(asked["id"]))
        # Nested one level only: a question asked from a side session hangs under the same main session.
        return await self.new_session(input_id, asked["track"], question, parent=asked["parent"] or asked["id"],
                                      prompt=prompt, sphere=asked["sphere"])

    async def write(self, input_id: str, session_id: str, text: str, attachments: object = None) -> str:
        """The owner's message into a session, once per `input_id`: into its live run, or queued as the next one.
        Every card it has open goes stale. What to tell him. With attachments (`inbox`), a picture or a short PDF goes
        in as a block, any other file into the session's inbox; what cannot be read is said, and the rest goes in."""
        session = self.store.session(session_id)
        if session is None:
            return "Нет такой сессии."
        if session["lane"] not in self.live:
            return RETIRED
        if self.store.input_session(input_id) is not None:
            return "Это сообщение сессия уже получила."
        ready = await self._ready(text, attachments) if attachments else None
        if isinstance(ready, str):
            return ready
        content = ready.lane(self._today(self.clock())) if ready is not None and ready.meta else None
        said = await self._write(input_id, session, text, ready, content)
        return _with_refused(said, ready)

    async def write_into(self, input_id: str, session: dict, text: str, by: str) -> str:
        """A message from a parent session into its sub-session (`subs`): as the owner's would go, but it neither
        closes the owner's open buttons nor counts as his word."""
        return await self._write(input_id, session, text, None, None, by=by)

    async def _write(self, input_id: str, session: dict, text: str, ready: inbox.Ready | None,
                     content: dict | None, by: str | None = None) -> str:
        session_id = session["id"]
        now = self.clock()
        if not self.store.add_input(input_id, session_id, text, now, *((ready.meta, ready.thumbs) if ready else ()),
                                    by=by):
            return "Это сообщение сессия уже получила."
        if by is None:  # the owner's own word: his buttons answered, his family's exchanges counted again
            self.subs.owner_wrote(session)
            self.store.db.execute("UPDATE sessions SET owner_joined = 1 WHERE id = ?", (session_id,))
            self.store.db.commit()  # the turn's ping answers him: it rings and closes his buttons (`_summary`)
            pings.close_open(self.store, session_id, now, input_id, text)  # its last ping answered, by a reply or not
            for approval in self.store.approvals(session=session_id, kind=questions.KINDS, states=("pending", "shown")):
                if self.store.move(approval["id"], (approval["state"],), "stale", now) and approval["state"] == "shown":
                    retire(self.store, {**approval, "state": "stale"}, "Устарела: ты написал новое сообщение", now)
        if session["lane"] in self.lanes and any(r["session"] == session_id for r in self.store.open_runs()):
            try:
                if (await self.lanes[session["lane"]].answer(session_id, text, content)).get("status") == "sent":
                    return "Передал в сессию."
            except Unavailable as exc:
                log.warning("session %s: %s", session_id, exc)
        self.store.enqueue(session_id, text, now, content, by=by)
        self._set_state(session_id, "active", now)
        await self.drain()
        queued = any(q["session"] == session_id for q in self.store.queued())
        return "Принято — сессия ждёт места на линии." if queued else "Принято."

    async def decide(self, approval: dict | None, action: str, digest: str, *,
                     from_face: bool) -> tuple[str, dict | None]:
        """The owner's decision on a card, by a Telegram button or in the face — the same rules in both: the card
        still asks (`_pressable`), the decision is bound to the digest the owner saw, and of two decisions exactly one
        moves the card. The toast, and the card's view as it now reads; no view when nothing was decided. A decision
        made in the face rewrites the Telegram card too; one made by a button reaches the face as the approval's
        event, written by the move itself."""
        if action not in (approvals.APPROVE, approvals.REJECT) or approval is None or \
                not self._pressable(approval, digest):
            return STALE, None
        if self._retired(approval["session"]):
            return RETIRED, None
        now = self.clock()
        granted = action == approvals.APPROVE
        view = {**approvals.without_idle(approval["view"]),
                "status": "Разрешено — выполняю." if granted else "Отказано."}
        # Its card rewritten with the decision — from a Telegram press too, which edits the card at once: a note
        # journaled before it (an idle line with the buttons) may still be on its way, and the latest note wins.
        telegram = {"kind": "update", "session": approval["session"], "data": {"view": view, "buttons": []},
                    "approval": approval["id"]}
        if not self.store.move(approval["id"], ("shown",), "granted" if granted else "rejected", now, note=telegram,
                               view=view):
            return STALE, None
        self.store.enqueue(approval["session"], None, now)  # a bare resume: the CLI asks PreToolUse again
        self.store.mark_seen(approval["session"], now)
        self._set_state(approval["session"], "active", now)
        await self.drain()
        return ("Разрешено." if granted else "Отказано."), view

    async def stop(self, session_id: str) -> bool:
        """The session's running turn interrupted by its lane; the lane journals `stopped`, and the session goes idle
        when that line comes. False when no run of it was there to stop."""
        session = self.store.session(session_id)
        if session is None:
            return False
        signals.add(self.store, signals.OWNER_STOP, session_id, self.clock())
        stopped = await self._stop_family(session)  # a parent's sub-sessions stop with it, a run of its own or not
        if session["lane"] not in self.lanes:
            return stopped
        self.subs.stopped(session)
        if session.get("sub_of"):  # closed now, whether its run's end comes later or came already: open no longer
            self._set_state(session_id, "closed", self.clock())
            stopped = True
        try:
            return (await self.lanes[session["lane"]].stop(session_id)).get("status") == "stopping" or stopped
        except Unavailable as exc:
            log.warning("session %s not stopped: %s", session_id, exc)
            return False

    async def _stop_family(self, parent: dict) -> bool:
        """«Стоп» on a session stops its open sub-sessions too: their queued runs dropped, their running ones
        interrupted, their cards stale, each closed; nothing they say after it is a report. True when any was."""
        now, any_stopped = self.clock(), False
        for child in self.subs.family(parent["id"]):
            any_stopped = True
            self.subs.stopped(child)
            self.store.db.execute("DELETE FROM queue WHERE session = ? AND attempt IS NULL", (child["id"],))
            self.store.db.commit()
            for approval in self.store.approvals(session=child["id"], kind=questions.KINDS, states=("pending", "shown")):
                if self.store.move(approval["id"], (approval["state"],), "stale", now) and approval["state"] == "shown":
                    retire(self.store, {**approval, "state": "stale"}, "Устарела: Владелец остановил сессию", now)
            running = any(r["session"] == child["id"] for r in self.store.open_runs())
            if running and child["lane"] in self.lanes:
                try:
                    await self.lanes[child["lane"]].stop(child["id"])
                except Unavailable as exc:
                    log.warning("session %s not stopped: %s", child["id"], exc)
            self._set_state(child["id"], "closed", now)
        return any_stopped

    def _pressable(self, approval: dict, digest: str, kind: str = "approval") -> bool:
        """The card still asks what the session waits on: shown, its session waiting with this call as the last
        thing it said, nothing written by the owner since, and the button bound to this very call."""
        session = self.store.session(approval["session"])
        last = self.store.last_line(approval["session"])
        if approval["kind"] != kind or approval["state"] != "shown" or session is None or \
                session["state"] != "waiting" or last is None or digest != approval["digest"]:
            return False
        line, at = last
        deferred = transcript.deferred_of(line)
        wrote = self.store.last_input_at(approval["session"])
        return isinstance(deferred, dict) and deferred.get("id") == approval["tool_use_id"] and \
            (wrote is None or wrote <= at)

    # --- questions: AskUserQuestion, answered by a button or in the owner's words -----------------------------------

    def _show_question(self, session: dict, approval: dict, now: float) -> bool:
        """Each question of the call as a Telegram message of its own, with its options as buttons."""
        view = questions.open_view()
        if not self.store.move(approval["id"], ("pending",), "shown", now, view=view):
            return False
        shown = {**approval, "state": "shown", "view": view}
        for n in range(len(questions.of(approval["input"]))):
            card = questions.card(shown, n, session["id"], session["track"])
            self.store.add_note("card", session["id"], {"view": dataclasses.asdict(card),
                                                        "buttons": self._question_buttons(shown, n, now)},
                                approval=questions.key(approval["id"], n))
        return True

    def _question_buttons(self, approval: dict, n: int, now: float) -> list[list[str]]:
        """Question `n`'s buttons as they read now: a live button kept for its action, a missing one made."""
        live = dict(self.store.live_buttons(approval["id"], now))
        return [[words, live.get(action) or self.store.add_button(approval["id"], action, approval["digest"],
                                                                  now + BUTTON_TTL_S)]
                for words, action in questions.buttons(approval, n)]

    async def pick(self, approval: dict, action: str, digest: str) -> dict:
        """A press on a question's Telegram button: an option of a single choice answers it; a multi-select's option
        is ticked or unticked in the stored picks — its button stays live, the message relabelled — and «Готово»
        answers it with what is ticked.
        What the bridge does with the pressed message: the toast, its view and the buttons it keeps."""
        parsed = questions.parse_action(action)
        if parsed is None or not self._pressable(approval, digest, "question"):
            return {"toast": STALE}
        n, choice = parsed
        asked = questions.of(approval["input"])
        if n >= len(asked) or (choice != questions.DONE and choice >= len(asked[n]["options"])):
            return {"toast": STALE}
        q, now = asked[n], self.clock()
        if str(n) in approval["view"].get("answers", {}):
            return {"toast": "На этот вопрос уже есть ответ."}
        if not q["multiSelect"]:
            return await self._answer(approval, n, q["options"][choice]["label"], False, now)
        picks = list(approval["view"].get("picks", {}).get(str(n)) or [])
        if choice == questions.DONE:
            if picks:
                return await self._answer(approval, n, questions.joined(questions.in_order(q, picks)), False, now)
            toast = "Отметь хотя бы один вариант — или ответь на это сообщение своим текстом."
        else:
            label = q["options"][choice]["label"]
            picks = [p for p in picks if p != label] if label in picks else [*picks, label]
            toast = ""
        view = {**approval["view"], "picks": {**approval["view"].get("picks", {}), str(n): picks}}
        approval = {**approval, "view": view}
        keep = self._question_buttons(approval, n, now)  # the same ticks, relabelled; a spent «Готово» made anew
        card = dataclasses.asdict(questions.card(approval, n, approval["session"], self._track_of(approval)))
        note = {"kind": "update", "session": approval["session"], "data": {"view": card, "buttons": keep},
                "approval": questions.key(approval["id"], n)}
        if not self.store.move(approval["id"], ("shown",), "shown", now, note=note, view=view):
            return {"toast": STALE}
        return {"toast": toast, "view": card, "keep": keep}

    async def answer(self, approval: dict | None, n: object, labels: object, text: object, digest: str) -> str:
        """The face's answer to question `n` of a card: the options picked (`labels`), or the owner's own `text`.
        The same rules as a Telegram press; what to tell him."""
        if approval is None or not self._pressable(approval, digest, "question"):
            return STALE
        asked = questions.of(approval["input"])
        if type(n) is not int or not 0 <= n < len(asked):
            return "Нет такого вопроса."
        if str(n) in approval["view"].get("answers", {}):
            return "На этот вопрос уже есть ответ."
        q = asked[n]
        if isinstance(text, str) and text.strip():
            if len(text) > questions.OTHER_LIMIT:
                return f"Ответ длиннее {questions.OTHER_LIMIT} знаков."
            return (await self._answer(approval, n, text.strip(), True, self.clock()))["toast"]
        options = {o["label"] for o in q["options"]}
        if not isinstance(labels, list) or not labels or not all(isinstance(label, str) and label in options
                                                                 for label in labels):
            return "Выбери вариант из предложенных или напиши свой ответ."
        picked = questions.in_order(q, labels)
        if not q["multiSelect"] and len(picked) != 1:
            return "Здесь выбирают один вариант."
        return (await self._answer(approval, n, questions.joined(picked), False, self.clock()))["toast"]

    async def reply_answer(self, card: str, session_id: str, text: str, input_id: str = "") -> str | None:
        """The owner's reply to a question's Telegram message: while the question asks, his text is its answer
        («Свой ответ»), taken once per `input_id`. None when the message is no open question of this session — the
        reply is then a message into the session like any other."""
        found = questions.split_key(card)
        approval = self.store.approval(found[0]) if found else None
        if approval is None or approval["kind"] != "question" or approval["session"] != session_id or \
                not self._pressable(approval, approval["digest"], "question"):
            return None
        n = found[1]
        if n >= len(questions.of(approval["input"])) or not text.strip():
            return None
        if str(n) in approval["view"].get("answers", {}):
            return "На этот вопрос уже есть ответ. Чтобы написать в сессию, ответь на другое её сообщение."
        if len(text) > questions.OTHER_LIMIT:
            return f"Ответ длиннее {questions.OTHER_LIMIT} знаков — не принят."
        if input_id and not self.store.take_answer_input(input_id, approval["id"], self.clock()):
            return "Это сообщение сессия уже получила."
        return (await self._answer(approval, n, text.strip(), True, self.clock()))["toast"]

    async def _answer(self, approval: dict, n: int, value: str, other: bool, now: float) -> dict:
        """Question `n` answered, from wherever: its Telegram message rewritten with the answer and no buttons, the
        face told by the approval's event; once every question has its answer the call is `answered` and the session
        resumes — a bare `--resume`, whose PreToolUse the gate answers with the answers. Read and written with no
        await between: of two answers to the same question exactly one is taken."""
        if self._retired(approval["session"]):
            return {"toast": RETIRED}
        approval = self.store.approval(approval["id"]) or approval
        view = approval["view"]
        if approval["state"] != "shown" or str(n) in view.get("answers", {}):
            return {"toast": STALE}
        answers = {**view.get("answers", {}), str(n): {"value": value, "other": other}}
        view = {**view, "answers": answers, "picks": {k: v for k, v in view.get("picks", {}).items() if k != str(n)}}
        done = len(answers) >= len(questions.of(approval["input"]))
        approval = {**approval, "view": view}
        card = dataclasses.asdict(questions.card(approval, n, approval["session"], self._track_of(approval)))
        note = {"kind": "update", "session": approval["session"], "data": {"view": card, "buttons": []},
                "approval": questions.key(approval["id"], n)}
        self.store.void_buttons(approval["id"], questions.action(n, ""), now)
        if not self.store.move(approval["id"], ("shown",), "answered" if done else "shown", now, note=note,
                               view=view):
            return {"toast": STALE}
        if not done:
            left = questions.left(approval)
            return {"toast": f"Принято. Осталось ответить: {left}.", "view": card, "keep": []}
        self.store.enqueue(approval["session"], None, now)  # a bare resume: the gate hands the CLI the answers
        self.store.mark_seen(approval["session"], now)
        self._set_state(approval["session"], "active", now)
        await self.drain()
        return {"toast": "Ответ передан в сессию.", "view": card, "keep": []}

    def _retired(self, session_id: str) -> bool:
        """The session's lane is no longer configured: nothing of the owner's goes into it, a decision neither."""
        session = self.store.session(session_id)
        return session is not None and session["lane"] not in self.live

    def _track_of(self, approval: dict) -> str:
        session = self.store.session(approval["session"])
        return session["track"] if session else ""

    # --- runs on the lanes --------------------------------------------------------------------------------------

    async def drain(self) -> None:
        """Start the queued runs there is room for, oldest first: one run per session at a time, the lane's own cap
        and the platform's total. A run the lane could not take now stays queued; one whose lane or track is no longer
        configured is refused at once."""
        async with self._draining:
            running = [r for r in self.store.open_runs() if r["lane"] in self.live]  # the others no one settles
            sessions = {r["session"] for r in running}
            per_lane, total = Counter(r["lane"] for r in running), len(running)
            for queued in self.store.queued():
                session = self.store.session(queued["session"])
                if session is None:
                    continue
                if (why := self._unrunnable(session)) is not None:  # told now, room or not
                    self._refused(session, why, self.clock(), f"q{queued['seq']}")
                    self.store.dequeue(queued["seq"])
                    continue
                if session.get("sub_of") and session["state"] == "closed":  # stopped: a message to it reopens it first
                    self.store.dequeue(queued["seq"])
                    continue
                if total >= self.cfg.max_sessions or queued["session"] in sessions:
                    continue
                sessions.add(queued["session"])  # the session's later entries wait behind this one
                if queued.get("by") is not None and (total >= self.cfg.max_sessions - 1 or per_lane[session["lane"]] >=
                                                     self.cfg.lanes[session["lane"]].max_sessions - 1):
                    continue  # a sub-session's or a report's run leaves the last place to the owner's own message
                if per_lane[session["lane"]] >= self.cfg.lanes[session["lane"]].max_sessions:
                    continue
                status = await self._start(session, queued)
                if status not in ("unavailable", "busy", "running"):
                    self.store.dequeue(queued["seq"])
                if status in ("started", "unavailable"):  # an unknown answer's run holds its room
                    per_lane[session["lane"]] += 1
                    total += 1

    def _unrunnable(self, session: dict) -> str | None:
        if session["lane"] not in self.cfg.lanes:
            return f"линии {session['lane']} больше нет в настройках"
        if session["lane"] not in self.live:
            return f"полоса {session['lane']} выключена"
        if session["track"] not in self.tracks:
            return f"трека {session['track']} больше нет в настройках"
        return None

    async def _start(self, session: dict, queued: dict) -> str:
        if not await self.tobase.before_start(session):  # a «В Базу» commit in flight: the run waits its turn
            return "busy"
        now = self.clock()
        track, lane = self.tracks[session["track"]], self.cfg.lanes[session["lane"]]
        fork, why = self.spheres.fork_start(session)  # a fork's first run copies its parent's conversation
        if why is not None:  # said as the session's own summary: the next message tries again
            self._set_state(session["id"], "idle", now)
            self._summary(session, why, pings.FAILED)
            return "refused"
        marks = approvals.marks_for(track, lane, read_only=bool(session.get("parent")))
        if not await self._code_ready(session, track):  # the repository's first mirror is still being fetched
            return "busy"
        await self._freshen(session, track)
        token = secrets.token_urlsafe(32)
        run = self.store.add_run(session["id"], self.gate.token_hash(token), [dataclasses.asdict(m) for m in marks],
                                 now)
        pings.started(self.store, run, queued["seq"], by_owner=queued["prompt"] != CONTINUE)  # a quiet ping or not
        if queued["prompt"] is not None and queued["prompt"] != CONTINUE:  # a new turn: whose doing it is (`subs`)
            self.store.db.execute("UPDATE sessions SET turn_by = ?, owner_joined = 0 WHERE id = ?",
                                  (queued.get("by") or "owner", session["id"]))
            self.store.db.commit()
        body = {"session": session["id"], "run": run, "cli_session": session["cli_session"] or fork or None,
                **({"fork": True} if fork else {}),
                # The sub-sessions' MCP server (`subs`): the lane host builds its address from its own hook address.
                **({"subs": True} if subs.may_start(session) else {}),
                "cwd": self.cwd_of(session, track), "settings": cliengine.settings(lane.hook_url, CLEANUP_DAYS, [m.rule for m in marks]),
                # A card rule is neither: its call is the gate's to defer, and the owner's grant must be able to run it.
                "allowed": [m.rule for m in marks if m.kind == "allow"],
                "disallowed": [m.rule for m in marks if m.kind in ("deny", "never")], "prompt": queued["prompt"],
                # With attachments: the blocks instead of the bare text, and the files.
                **(self.store.queued_content(queued["seq"]) or {}),
                "hook_token": token,
                # The repository's rules, the owner's skills and his memory, in the room too (docs/instructions.md); lane-web's
                # host, retired, still refuses them by its own switch.
                "instructions": True,
                # The lane's login, sent with each run and stored nowhere — not in the store, not in a log line: the
                # lane host keeps it in memory and gives it to the CLI through a pipe.
                "oauth_token": self.cfg.oauth_token,
                # The owner's repositories in the room (docs/code.md): the lane host clones each mirror it lacks.
                **({"code": self._code_spec()} if self.code is not None and session["lane"] == self.cfg.code.lane
                   else {})}
        await self.tobase.run_started(session, run)  # the Base as it is before this run: its first window
        await self.topush.run_started(session)  # a session in a repository's folder works in it
        # Recorded before the send: the lane journals `started` or `refused` before it answers, and the poller may
        # pull that line while the answer is still on its way, or after it is lost (`_apply`, `poll`).
        self.store.attempt(queued["seq"], run)
        self._sending.add(run)
        try:
            answer = await self.lanes[session["lane"]].run(body)
        except Unavailable as exc:
            log.warning("session %s: %s", session["id"], exc)
            answer = None
        finally:
            self._sending.discard(run)
        if self.store.attempted(run) is None:  # the journal said first
            return "started" if any(r["run"] == run for r in self.store.open_runs()) else "settled"
        if answer is None:
            # The lane may have started it: the run stays open, holding its session and its room on the lane, until
            # the lane's journal says.
            return "unavailable"
        self.store.attempt(queued["seq"], None)
        status = str(answer.get("status") or "")
        if status == "started":
            self._set_state(session["id"], "active", now)
            return status
        self.store.end_run(run, now)
        if status in ("busy", "running"):
            return status
        return self._refused(session, str(answer.get("error") or status), now, run)

    def _code_spec(self) -> dict:
        """What the lane host needs to make the room's clones: their folder, the repositories whose mirror is there,
        the owner's name for their commits."""
        return {"root": self.cfg.code.root, "author": self.cfg.code.author,
                "repos": [name for name in self.code.repos if self.code.ready(name)]}

    async def _code_ready(self, session: dict, track) -> bool:
        """Before a run in a repository's folder: its mirror fetched, so `git pull` in the room brings the remote's
        latest. Never waits for a fetch already running, nor past FRESH_TIMEOUT_S; False only while the repository's
        first mirror is being made (the run stays queued; the next drain tries again)."""
        repo = self.topush.repo_of_cwd(self.cwd_of(session, track)) if session["lane"] == getattr(
            self.cfg.code, "lane", None) else None
        if repo is None or self.code.repos[repo].off:
            return True
        if not self.code.ready(repo):
            if repo in self.code.failed:
                return True  # the lane host says there is no mirror; the reason is in the log and on the card
            self._spawn_fetch(repo)
            return False
        try:
            await asyncio.to_thread(self.code.fetch, repo, code_mod.FRESH_TIMEOUT_S, False)
        except Exception as exc:  # git's words, the token never among them
            log.warning("code %s: not fetched before a run of %s: %s", repo, session["id"], str(exc)[:200])
        return True

    def _spawn_fetch(self, repo: str) -> None:
        """The repository's first mirror, fetched in the background (once at a time); the queue drained after it."""
        if repo in self._fetching:
            return
        self._fetching.add(repo)

        async def fetch() -> None:
            try:
                if await asyncio.to_thread(self.code.fetch, repo, code_mod.GIT_TIMEOUT_S, False):
                    log.info("code %s: mirror fetched", repo)
            except Exception as exc:
                log.warning("code %s: mirror fetch failed: %s", repo, str(exc)[:300])
            finally:
                self._fetching.discard(repo)
            await self.drain()
        self.tasks.append(asyncio.create_task(fetch()))

    async def _code_loop(self) -> None:
        """The mirrors kept fresh while idle: at start (the first makes them), then every `every_s`."""
        while True:
            for name, repo in self.code.repos.items():
                if repo.off:
                    continue
                try:
                    await asyncio.to_thread(self.code.fetch, name, code_mod.GIT_TIMEOUT_S, False)
                except Exception as exc:
                    log.warning("code %s: mirror fetch failed: %s", name, str(exc)[:300])
            await asyncio.sleep(self.cfg.code.every_s)

    def _in_base(self, session: dict, track) -> bool:
        return self.base is not None and session["lane"] == self.cfg.base.lane and \
            under(self.cwd_of(session, track), self.cfg.base.tree)

    def _base_busy(self) -> bool:
        """A run is open on the clone's lane — any of its sessions can write /work/base, the Base's own or not.
        Asked by `BaseRepo` from its worker thread under its lock, so through a connection of its own; a database
        that cannot be read says busy."""
        try:
            db = sqlite3.connect(f"file:{self.cfg.state_db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return True
        try:
            return db.execute("SELECT 1 FROM runs r JOIN sessions s ON s.id = r.session WHERE r.ended IS NULL "
                              "AND s.lane = ? LIMIT 1", (self.cfg.base.lane,)).fetchone() is not None
        except sqlite3.Error:
            return True
        finally:
            db.close()

    async def _freshen(self, session: dict, track) -> None:
        """Before a run in the clone: the hub's changes in, when no other run on the lane is open. Never waits for
        the clone (a commit pushing, the first clone): the run goes on the clone as it is, as it does when the
        refresh fails."""
        if not self._in_base(session, track):
            return
        try:
            await self.base.refresh(wait=False)
        except Exception as exc:  # the type only: a path may say anything
            log.warning("base: not refreshed before a run of %s: %s", session["id"], type(exc).__name__)

    async def _base_loop(self) -> None:
        """The clone kept fresh while idle: at start (the first clones it), then every `every_s` — a look at the
        hub's ref, nothing more, while the hub stands still; nothing at all while a run on the lane is open."""
        while True:
            try:
                await self.base.refresh()
            except Exception as exc:
                log.warning("base: refresh failed: %s: %s", type(exc).__name__, str(exc)[:300])
            await asyncio.sleep(self.cfg.base.every_s)

    def _refused(self, session: dict, why: str, now: float, run: str | None = None) -> str:
        self._set_state(session["id"], "idle", now)
        self._summary(session, f"Линия не запустила сессию: {why}", pings.FAILED)
        self._turn_over(session, f"Линия не запустила сессию: {why}", failed=True, run=run)
        return "refused"

    def _turn_over(self, session: dict, text: str, failed: bool = False, run: str | None = None) -> None:
        """A sub-session's turn is over, however it ended: it closes (a message opens it again), and its parent gets
        a report when the turn was the parent's doing (`subs`)."""
        if not session.get("sub_of"):
            return
        self.subs.turn_ended(session, text, failed, run)
        self._set_state(session["id"], "closed", self.clock())

    # --- the lanes' journals ------------------------------------------------------------------------------------

    async def poll(self, lane: str) -> int:
        """One pull of the lane's journal: every line ingested, then confirmed. How many lines came."""
        client = self.lanes[lane]
        attempts = [run for run in self.store.attempts(lane) if run not in self._sending]
        items = await client.items(POLL_LIMIT)
        for item in items:
            self.ingest(lane, item)
        if items:
            await client.confirm(items[-1]["seq"])
        try:
            await self.hand_over()  # a turn that ended in these lines, or one a crash left owed
        except Exception as exc:  # files are never worth the lane's journal or the queue: logged, and on
            log.error("the hand-over of files failed: %s", type(exc).__name__)
        try:
            await self.tobase.settle()  # the Base files these lines' runs changed, and the pings' «В Базу»
        except Exception as exc:
            log.error("the Base's marks failed: %s", type(exc).__name__)
        try:
            await self.topush.settle()  # the repositories these lines' sessions worked in, and «Запушить · N»
        except Exception as exc:
            log.error("the repositories' looks failed: %s", type(exc).__name__)
        if len(items) < POLL_LIMIT:
            # The lane journals a run it started, or refused, before it answers: a lost answer (not one still awaited,
            # left out above) whose run the whole journal since does not name never reached it; the entry is sent again.
            for run in attempts:
                if (seq := self.store.attempted(run)) is not None:
                    self.store.end_run(run, self.clock())
                    self.store.attempt(seq, None)
        self.subs.deliver()  # the reports of the turns these lines ended, to parents that are free
        await self.drain()
        return len(items)

    def ingest(self, lane: str, item: dict) -> None:
        session_id, run, line = item.get("session"), item.get("run"), item.get("line")
        if not all(isinstance(v, str) for v in (session_id, run, line)) or self.store.has_raw(lane, item["seq"]):
            return
        session = self.store.session(session_id)
        if session is None:
            log.warning("lane %s: line %d of an unknown session %r", lane, item["seq"], session_id)
            return
        now = self.clock()
        data = parsed(line)
        if transcript.is_partial(data):  # never stored: the text of a message still being written, for the face
            # Given again after a crash between storing and confirming, a line before the last one stored is a
            # replay: the message it was part of is already whole in the transcript.
            if (text := transcript.delta_of(data)) and self.on_delta is not None and \
                    item["seq"] > self.store.last_lane_seq(lane):
                self.on_delta(session_id, text)
            return
        # What a line does is done before it is stored: a crash in between does it again, and every effect is a
        # state move that happens once.
        self._apply(session, run, data, now)
        for n, (kind, call) in enumerate(signals.from_line(data)):  # keyed by the line: a replay adds none
            signals.add(self.store, kind, session_id, now, call=call, key=f"{lane}:{item['seq']}:{n}")
        if (turn := self.store.add_raw(lane, item["seq"], session_id, run, line, now)) is not None:
            self.store.reproject(session_id, turn)

    def _apply(self, session: dict, run: str, line: dict, now: float) -> None:
        kind = line.get("type")
        if (tokens := transcript.context_tokens(line)) is not None:
            self.store.set_context(session["id"], tokens)
        if (wait := transcript.usage_limit_wait(line)) is not None:
            self._limit(session, now + wait)
        if kind == "system" and line.get("subtype") == "init":
            self.store.update_session(session["id"], now, cli_session=str(line.get("session_id") or ""),
                                      cli_version=str(line.get("claude_code_version") or ""))
            if subs.may_start(session) and (said := subs.server_failed(line)) and \
                    self.store.get(f"subs.mcp_failed:{session['id']}") is None:  # said once per session
                self.store.set(f"subs.mcp_failed:{session['id']}", "1")
                self._summary(session, said, pings.FAILED, run)
            mode = str(line.get("permissionMode") or "") or "не назван"
            if self.store.set_run_mode(run, mode):  # once per session: the gate falls back for this run on its own
                self._summary(session, manual_notice(mode, str(line.get("model") or "")), pings.FAILED, run)
        elif kind == "result":
            self._result(session, run, line, now)
            self.gate.run_ended(run)  # a block the model went past is the owner's notice; the run's asks forgotten
        elif kind == "lane":
            event = line.get("event")
            if event in ("exited", "restarted", "refused"):
                self.gate.run_ended(run)
            if event == "stopped" and line.get("by") == "shutdown":  # the lane's own stop: an update, a restart
                self.store.interrupt(run, now)
            if event in ("exited", "restarted", "refused"):
                self.store.end_run(run, now)
            if event in ("started", "refused") and (seq := self.store.attempted(run)) is not None:
                self.store.dequeue(seq)
                if event == "started":
                    self._set_state(session["id"], "active", now)
                else:
                    self._refused(session, str(line.get("error") or event), now, run)
            current = self.store.session(session["id"])["state"]
            if current != "active":  # a waiting one keeps its card; a finished one has said so
                return
            if event in ("exited", "restarted") and self.store.resume_interrupted(run, now):
                self._continue(session, now)
            elif event in ("stopped", "restarted", "exited") and not self.store.interrupted(run):
                self._set_state(session["id"], "idle", now)

    def _result(self, session: dict, run: str, line: dict, now: float) -> None:
        stop = line.get("stop_reason")
        if stop == "tool_deferred":
            deferred = transcript.deferred_of(line)
            call_id = str(deferred.get("id") or "") if isinstance(deferred, dict) else ""
            for approval in self.store.approvals(run=run, kind=questions.KINDS, states=("pending",)):
                if approval["tool_use_id"] != call_id:  # parallel calls: the CLI keeps only the last one
                    self.store.move(approval["id"], ("pending",), "dropped", now)
            asked = self.store.approvals(session=session["id"], tool_use_id=call_id, kind=questions.KINDS)
            shown = [self._show(session, a, now) for a in asked if a["state"] == "pending"]
            if any(shown) or any(a["state"] == "shown" for a in asked):
                self._set_state(session["id"], "waiting", now)
                return
            self._set_state(session["id"], "idle", now)
            if not self.store.approvals(session=session["id"], tool_use_id=call_id, kind="courier"):
                # The gate deferred while it shut down: the call is asked again, or the owner's decision on it,
                # which the CLI has not heard yet, is given.
                self.store.enqueue(session["id"], None, now)
                pings.unattended(self.store, session["id"])
        elif stop == "tool_deferred_unavailable":
            for approval in self.store.approvals(session=session["id"], kind=questions.KINDS,
                                                 states=("granted", "answered")):
                self.store.move(approval["id"], (approval["state"],), "expired", now)
            self._set_state(session["id"], "idle", now)
            gone = "Отложенный вызов продолжить нельзя: этого инструмента у сессии больше нет. Напиши, что делать дальше."
            self._summary(session, gone, pings.FAILED, run)
            self._turn_over(session, gone, failed=True, run=run)
        elif self._failed(line) and self.store.interrupted(run):
            pass  # the shutdown's own interrupt: the run's end resumes the session (`_continue`)
        elif self._failed(line):
            why = str(line.get("result") or "") or str(line.get("subtype") or "") or "без описания"
            signals.add(self.store, signals.RUN_ERROR, session["id"], now, key=f"run:{run}")
            self._summary(session, f"Ход оборвался ошибкой: {why}", pings.FAILED, run)
            self._set_state(session["id"], "idle", now)
            self._owe_look(session)
            self._turn_over(session, f"Ход оборвался ошибкой: {why}", failed=True, run=run)
        else:
            text, written = self._sphere_line(session, str(line.get("result") or "") or "Ход закончен без текста.")
            self._summary(session, text, run=run, line_before=written)
            self._set_state(session["id"], "closed" if session["kind"] == "code" else "idle", now)
            self._owe_look(session)
            self._turn_over(session, text, run=run)

    def _sphere_line(self, session: dict, text: str) -> tuple[str, str]:
        """A session nobody placed names its own sphere by its last line (`subs.sphere_line`): set, never as main,
        and its sub-sessions follow. A sub-session follows its parent, a side question its question: neither is read.
        The text without the line, and the line as written (the face hides it)."""
        row = self.store.session(session["id"]) or session
        if not subs.may_start(row):
            return text, ""
        text, name, written = subs.sphere_line(text, frozenset(self.spheres.by_name))
        if name is not None and row.get("sphere") is None and row.get("sphere_by") is None:
            self.store.set_sphere(row["id"], name, by="session")
        return text, written

    def _owe_look(self, session: dict) -> None:
        """The turn is over: what it left in `out/` is the owner's, looked at after this poll (`hand_over`). A side
        question's session changes nothing, so it hands nothing over; its parent's files stay its parent's."""
        if not session.get("parent"):
            self.out.owe(session["id"], session["created"])

    async def hand_over(self) -> None:
        """Every look owed: `out/` of the session's directory read through the lane's mount, off the loop; its files
        attributed when another session ran in the same directory meanwhile, held to the storage caps, and recorded
        with their notes (`outfiles`). A look that breaks is said once and owed no more. One look at a time; a poll
        that finds one going leaves the rest to it or to the next poll."""
        if self._looking.locked():
            return
        async with self._looking:
            for owed in self.out.owed():
                session = self.store.session(owed["session"])
                track = self.tracks.get(session["track"]) if session else None
                lane = self.cfg.lanes.get(session["lane"]) if session else None
                if session is None or track is None or lane is None:
                    self.out.settle(owed["session"])
                    continue
                looked = self.clock()
                try:
                    await self._look(session, track, lane, owed, looked)
                except Exception as exc:  # the type only: a name or a line may say anything
                    log.error("session %s: out/ look failed: %s", session["id"], type(exc).__name__)
                    self.out.failed(session, looked, owed["gen"], f"ошибка платформы ({type(exc).__name__})")

    async def _look(self, session: dict, track, lane, owed: dict, looked: float) -> None:
        cwd = self.cwd_of(session, track)
        look = await asyncio.to_thread(outfiles.scan, lane.work_mount, cwd, owed["since"],
                                       self.out.known(session["lane"], cwd), looked)
        neighbours = [t.name for t in self.cfg.tracks if t.lane == track.lane and t.cwd == cwd]
        if look.found and (rivals := self.out.rivals(session["id"], neighbours, owed["since"], looked, track.lane,
                                                     cwd)):
            look = outfiles.attribute(look, cwd, self.out.lines(session["id"], owed["since"], looked),
                                      [self.out.lines(r, owed["since"], looked) for r in rivals])
        self.out.record(session, cwd, self.out.capped(look, looked), looked, owed["gen"])

    @staticmethod
    def _failed(line: dict) -> bool:
        return bool(line.get("is_error")) or str(line.get("subtype") or "").startswith("error")

    def _continue(self, session: dict, now: float) -> None:
        """A turn the platform's or the lane's own shutdown cut: the session goes on once, told why — unless the CLI
        never said which session it was, and a resume would start a new one without the task."""
        if not self.store.session(session["id"])["cli_session"]:
            self._set_state(session["id"], "idle", now)
            self._summary(session, "Ход прервался: платформа перезапускалась, а сессия ещё не началась. "
                                   "Напиши задачу ещё раз.", pings.FAILED)
            return
        if any(q["session"] == session["id"] for q in self.store.queued()):
            # The owner wrote while it was down: his message goes next, and nothing of ours after it to contradict it.
            self._summary(session, "Ход прервался: платформа перезапускалась. Сессия продолжит с твоего нового "
                                   "сообщения.", pings.FAILED)
            return
        self.store.enqueue(session["id"], CONTINUE, now)  # the poll that ingested this line drains next
        self._summary(session, "Ход прервался: платформа перезапускалась. Продолжаю с того места, где сессия "
                               "остановилась.", pings.FAILED)

    def _show(self, session: dict, approval: dict, now: float) -> bool:
        if approval["kind"] == "question":
            return self._show_question(session, approval, now)
        view, actions = approvals.card(approval, session["id"], session["track"])
        data = dataclasses.asdict(view)
        if not self.store.move(approval["id"], ("pending",), "shown", now, view=data):
            return False
        buttons = [[LABELS[a], self.store.add_button(approval["id"], a, approval["digest"], now + BUTTON_TTL_S)]
                   for a in actions]
        self.store.add_note("card", session["id"], {"view": data, "buttons": buttons}, approval=approval["id"])
        return True

    def _summary(self, session: dict, text: str, kind: str = pings.DONE, run: str | None = None, **extra) -> None:
        """A note for the owner, in Telegram and in the face — a ping (`pings`): its kind, how long the turn took
        (from `run`), the model's suggested replies; `turn` places it in the transcript, after that turn. A turn that
        was not the owner's doing — a sub-session's on its parent's word, a parent's on a report — pings without a
        sound when it is done, and leaves his open buttons as they are unless it brings its own (`subs`)."""
        row = self.store.session(session["id"]) or session
        side = f" · вопрос к {row['parent']}" if row.get("parent") else ""
        sub = f" · подсессия {row['sub_of']}" if row.get("sub_of") else ""
        head = f"Итог · {session['track']} · сессия {session['id']}{side}{sub}"
        machine = row.get("turn_by") in ("parent", "report") and not row.get("owner_joined")
        pings.add(self.store, row, head, text, kind, self.clock(), run=run, spheres=_sphere_titles(self.cfg),
                  quiet=machine, machine=machine, **extra)

    summary = _summary

    def sphere_titles(self) -> dict[str, str]:
        return _sphere_titles(self.cfg)

    def _limit(self, session: dict, until: float) -> None:
        """The run waits for the subscription's limit to reset (the CLI's retry watchdog): said once per wait."""
        said = self.store.last_summary(session["id"]) or {}
        if isinstance(said.get("limit_until"), (int, float)) and abs(said["limit_until"] - until) < LIMIT_SAID_AGAIN_S:
            return
        if until > float(self.store.get("subs.limit_until") or 0):  # no report is delivered until then (`subs`)
            self.store.set("subs.limit_until", str(until))
        at = datetime.fromtimestamp(until, ZoneInfo(self.cfg.owner_tz)).strftime("%H:%M")
        self._summary(session, f"Лимит подписки — жду сброса до {at}", pings.FAILED, limit_until=until)

    def _set_state(self, session: str, state: str, now: float) -> None:
        """`turn_started` is set only when a turn begins — active from idle, closed or new — never on a resume after a
        card (waiting -> active), which moves `at`: the face's «Работают» counts a turn from it."""
        fields = {}
        if state == "active" and (self.store.session(session) or {}).get("state") not in ("active", "waiting"):
            fields["turn_started"] = now
        self.store.update_session(session, now, state=state, idle_since=now if state == "idle" else None, **fields)

    set_state = _set_state

    # --- the clock ----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        for session in self.store.sessions_in("idle", idle_before=now - IDLE_CLOSE_S):
            self._set_state(session, "closed", now)
        self.store.prune(now)
        self.out.prune(now)
        for approval in self.store.approvals(kind=questions.KINDS, states=("shown",)):
            if approval["at"] <= now - BUTTON_TTL_S and self.store.move(approval["id"], ("shown",), "expired", now):
                retire(self.store, {**approval, "state": "expired"}, "Истекла: решения не было семь дней", now)
                if self.store.session(approval["session"])["state"] == "waiting":
                    self._set_state(approval["session"], "idle", now)
                    self._turn_over(self.store.session(approval["session"]), "Карточка истекла: решения не было семь "
                                                                             "дней, ход не закончен.", failed=True,
                                    run=f"expired:{approval['id']}")
        self._mark_idle(now)  # the cards still shown after the expiry
        self.subs.deliver()  # a report that waited for its siblings, or for a limit to pass: `_clock` drains
        self.tobase.tick(now)
        self.topush.tick(now)

    def _mark_idle(self, now: float) -> None:
        """A card whose session has been silent past the prompt cache says what pressing it costs: its view gets the
        line (`approvals.idle_line`), and the Telegram card is rewritten with the buttons it still has — once past
        the threshold, then as the hours change. A card too long for one message with the line is left as it is."""
        for approval in self.store.approvals(kind="approval", states=("shown",)):
            session, last = self.store.session(approval["session"]), self.store.last_line(approval["session"])
            if session is None or session["state"] != "waiting" or last is None:
                continue
            idle = now - last[1]
            line = approvals.idle_line(idle, session["context_tokens"])
            stamp = approvals.idle_stamp(idle)
            if line is None or approval["view"].get("idle") == stamp:
                continue
            view = approvals.with_idle(approval["view"], line, stamp)
            if len(bridge_view(view).plain()) > approvals.OPEN_LIMIT:
                continue
            buttons = [[LABELS[action], button] for action, button in self.store.live_buttons(approval["id"], now)
                       if action in LABELS]
            self.store.restyle(approval["id"], "shown", view, {"kind": "update", "session": approval["session"],
                                                               "data": {"view": view, "buttons": buttons},
                                                               "approval": approval["id"]})

    # --- the process --------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.retire_cards()
        self.tasks = [asyncio.create_task(self._poller(lane)) for lane in self.lanes]
        self.tasks.append(asyncio.create_task(self._clock()))
        self.tasks.append(asyncio.create_task(self.tobase.recover()))  # a «В Базу» card a restart cut
        if self.base is not None:
            self.tasks.append(asyncio.create_task(self._base_loop()))
        self.tasks.append(asyncio.create_task(self.topush.recover()))  # a «Запушить» card a restart cut
        if self.code is not None:
            self.tasks.append(asyncio.create_task(self._code_loop()))

    def retire_cards(self) -> None:
        """At start: every card still asking in a session of a lane that is gone or retired goes stale with a line
        that says why, and its session stops waiting — so it leaves «Решить» and /wait. Nothing could take its answer."""
        now = self.clock()
        for approval in self.store.approvals(kind=questions.KINDS, states=("pending", "shown")):
            session = self.store.session(approval["session"])
            if session is None or session["lane"] in self.live:
                continue
            if self.store.move(approval["id"], (approval["state"],), "stale", now) and approval["state"] == "shown":
                retire(self.store, {**approval, "state": "stale"}, "Устарела: полоса выключена", now)
            if session["state"] == "waiting":
                self._set_state(session["id"], "idle", now)

    async def _poller(self, lane: str) -> None:
        wait = POLL_S
        while True:
            try:
                full = await self.poll(lane) >= POLL_LIMIT
                wait = 0 if full else POLL_S
            except Unavailable as exc:
                wait = min(max(wait, POLL_S) * 2, BACKOFF_MAX_S)
                log.warning("%s (next try in %.0f s)", exc, wait)
            except Exception:
                log.exception("lane %s: the pull failed", lane)
                wait = POLL_S
            await asyncio.sleep(wait)

    async def _clock(self) -> None:
        while True:
            try:
                self.tick()
                await self.drain()
            except Exception:
                log.exception("the clock failed")
            await asyncio.sleep(TICK_S)

    async def shutdown(self) -> None:
        """Every PreToolUse waiting now, and every one after, defers; then every running session is stopped. A call
        deferred this way, granted or not, is resumed once the platform is back (`_result`); a run stopped in the
        middle of its turn is recorded, and its session resumed once when its end comes (`_continue`)."""
        self.gate.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        runs = [run for run in self.store.open_runs() if run["lane"] in self.lanes]
        answers = await asyncio.gather(*(self.lanes[run["lane"]].stop(run["session"], STOP_TIMEOUT_S)
                                         for run in runs), return_exceptions=True)
        for run, answer in zip(runs, answers):
            if isinstance(answer, Exception):
                log.warning("session %s not stopped: %s", run["session"], answer)
            elif answer.get("status") == "stopping":  # not_running: it ended on its own, its lines say how
                self.store.interrupt(run["run"], self.clock())


def _lane_view(cfg: PlatformConfig) -> str:
    """The clone as the lane's read-only mount shows it, when the platform writes it through another path: then each
    call checks the two are one folder. '' when they are the same path (compose mounts it there)."""
    view = posixpath.join(cfg.lanes[cfg.base.lane].work_mount, cfg.base.tree)
    return "" if posixpath.normpath(view) == posixpath.normpath(cfg.base.mount) else view


def _with_refused(said: str, ready: inbox.Ready | None) -> str:
    """What the platform says, and after it what of the attachments did not go in, and why."""
    if ready is None or not ready.refused:
        return said
    return f"{said}\n{inbox.NOT_PASSED}: {'; '.join(ready.refused)}."


def _sphere_titles(cfg: PlatformConfig) -> dict[str, str]:
    """A sphere's name → how a ping's header names it: its icon and title."""
    return {sphere.name: f"{sphere.icon} {sphere.title}".strip() for sphere in cfg.spheres}


def manual_notice(mode: str, model: str) -> str:
    """What the owner is told when a session's CLI did not start in auto. The CLI gives no reason; the likely ones
    are the model (auto needs a recent one) and a CLI or account where auto is off."""
    on = f" на модели {model}" if model else ""
    return (f"Режим auto в этой сессии выключен: CLI запустился в режиме {mode}{on} и причину не назвал — обычно это "
            "модель без auto или auto, выключенный для CLI или аккаунта. Пока так, каждый вызов вне правил сессии "
            "приходит тебе карточкой.")


def bridge_view(view: dict) -> courier.CardView:
    """A view as the router will render it into one Telegram message."""
    return courier.CardView(list(view.get("head", [])), str(view.get("body") or ""), str(view.get("status") or ""),
                            tuple(view.get("link") or ()))


def _bearer(request: web.Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip()


async def _hook_body(request: web.Request) -> dict | None:
    """A hook's body, read up to HOOK_BODY_MAX and no further — the app's own limit is the owner's larger one. None
    when it is longer: the CLI blocks the call (`onFailure: block`)."""
    if request.content_length is not None and request.content_length > HOOK_BODY_MAX:
        return None
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(1 << 16):
        size += len(chunk)
        if size > HOOK_BODY_MAX:
            return None
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(prog="retinue-platform")
    parser.add_argument("--rebuild-projection", action="store_true",
                        help="project every session's transcript again from its raw lines, then exit")
    args = parser.parse_args()
    # Its environment holds the lanes' login: a parse worker for the owner's pictures (`inbox`) must not read it.
    attachments.undumpable()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = PlatformConfig.load()
    store = PlatformStore(cfg.state_db)
    if args.rebuild_projection:
        store.rebuild_all()
        return

    async def run() -> None:
        platform = Platform(cfg, store)
        runner = web.AppRunner(platform.app(), access_log=None, shutdown_timeout=5)  # within the stop grace
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        owner_app = face.Face(platform) if cfg.face_origin else None
        face_runner = web.AppRunner(owner_app.app(), access_log=None, shutdown_timeout=2) if owner_app else None
        if face_runner:  # on its own address only: the lanes' networks reach the platform's other ones
            await face_runner.setup()
            await web.TCPSite(face_runner, cfg.face_host, cfg.face_port).start()
        platform.start()
        log.info("platform on :%d, lanes %s, tracks %s; face %s", cfg.listen_port, ", ".join(cfg.lanes),
                 ", ".join(platform.tracks), f"on {cfg.face_host}:{cfg.face_port} for {cfg.face_origin}" if owner_app else "off")
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        if owner_app:
            owner_app.close()  # the open streams end; the phones reconnect to the next platform
            await face_runner.cleanup()
        await platform.shutdown()
        await runner.cleanup()

    asyncio.run(run())


if __name__ == "__main__":
    main()
