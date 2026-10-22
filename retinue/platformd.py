"""The platform (`retinue-platform`): the one process that holds the sessions. It answers the lane CLIs' hooks (the
gate), pulls every lane's journal by number into its SQLite, folds the lines into session states, approval cards and
summaries, starts runs on the lanes within their caps, and serves the router: the owner's new sessions, his messages
into them, his presses on their cards, and the notes it has for him, by number.

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
import secrets
import signal
import time
from collections import Counter
from collections.abc import Callable

from aiohttp import web

from . import approvals, cliengine, transcript
from .config import PlatformConfig
from .gate import Gate, retire
from .mail import Unavailable
from .outbox import Neighbour
from .sessions import PlatformStore

log = logging.getLogger("retinue.platformd")

POLL_S = 1.0                 # a lane's journal is pulled this often
POLL_LIMIT = 200             # lines per pull: lane_host's own ceiling
BACKOFF_MAX_S = 30.0
TICK_S = 60.0                # the clock: idle sessions closed, old raw lines pruned, unpressed cards expired
BUTTON_TTL_S = 7 * 86400
IDLE_CLOSE_S = 7 * 86400
CLEANUP_DAYS = 30            # the CLI's own transcripts in the lane
LABELS = {approvals.APPROVE: "Разрешить", approvals.REJECT: "Отказать", approvals.OPEN: "Открыть"}
HOOK_EVENTS = "|".join(cliengine.EVENTS)


class LaneClient(Neighbour):
    """A lane host: its journal (`items`, `confirm`) and its closed command set."""

    async def run(self, body: dict) -> dict:
        return await self._call("POST", "/run", body)

    async def answer(self, session: str, prompt: str) -> dict:
        return await self._call("POST", "/answer", {"session": session, "prompt": prompt})

    async def stop(self, session: str) -> dict:
        return await self._call("POST", "/stop", {"session": session})


class Platform:
    def __init__(self, cfg: PlatformConfig, store: PlatformStore, lanes: dict[str, LaneClient] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        if not cfg.router_token:
            raise SystemExit("the platform needs RETINUE_PLATFORM_TOKEN: without it anyone on its network is the owner")
        self.cfg, self.store, self.clock = cfg, store, clock
        self.lanes = lanes if lanes is not None else {
            name: LaneClient(lane.url, lane.token, f"линия {name}") for name, lane in cfg.lanes.items()}
        self.tracks = {track.name: track for track in cfg.tracks}
        self.gate = Gate(store, cfg.lanes, cfg.hook_secret, clock)
        self.tasks: list[asyncio.Task] = []
        self._draining = asyncio.Lock()
        self._sending: set[str] = set()  # runs whose `/run` is awaiting its answer

    # --- the HTTP surface ---------------------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=16 * 1024 * 1024)  # a Write's content
        app.add_routes([web.post(r"/hook/mark/{n:\d{1,4}}", self.mark), web.post(f"/hook/{{event:{HOOK_EVENTS}}}",
                                                                                    self.hook),
                        web.post("/owner/new", self.owner_new), web.post("/owner/input", self.owner_input),
                        web.post("/owner/press", self.owner_press), web.post("/items", self.items),
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
        return web.json_response(await self.gate.hook(request.match_info["event"], _bearer(request),
                                                      await _body(request)))

    async def mark(self, request: web.Request) -> web.Response:
        return web.json_response(await self.gate.hook(f"mark/{int(request.match_info['n'])}", _bearer(request),
                                                      await _body(request)))

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
        track = self.tracks.get(name)
        if track is None:
            return web.json_response({"ok": False, "say": f"Нет такого трека: {name}. Есть: {', '.join(self.tracks)}"})
        now = self.clock()
        session = self.store.input_session(input_id)
        if session is None:
            session = self.store.new_session(track.name, track.lane, track.kind, now)
            self.store.add_input(input_id, session, text, now)
            if text:
                self.store.enqueue(session, text, now)
                await self.drain()
            else:
                self._set_state(session, "idle", now)
        return web.json_response({"ok": True, "session": session,
                                  "say": f"Сессия {session} · {track.name}. Чтобы писать в неё, отвечай на её "
                                         "сообщения."})

    async def owner_input(self, request: web.Request) -> web.Response:
        body = await _body(request)
        input_id, session_id, text = body.get("input_id"), body.get("session"), body.get("text")
        if not all(isinstance(v, str) and v for v in (input_id, session_id, text)):
            return web.json_response({"error": "input_id, session, text: не строки"}, status=400)
        session = self.store.session(session_id)
        if session is None:
            return web.json_response({"say": "Нет такой сессии."})
        now = self.clock()
        if not self.store.add_input(input_id, session_id, text, now):
            return web.json_response({"say": "Это сообщение сессия уже получила."})
        for approval in self.store.approvals(session=session_id, kind="approval", states=("pending", "shown")):
            if self.store.move(approval["id"], (approval["state"],), "stale", now) and approval["state"] == "shown":
                retire(self.store, {**approval, "state": "stale"}, "Устарела: ты написал новое сообщение", now)
        if any(r["session"] == session_id for r in self.store.open_runs()):
            try:
                if (await self.lanes[session["lane"]].answer(session_id, text)).get("status") == "sent":
                    return web.json_response({"say": "Передал в сессию."})
            except Unavailable as exc:
                log.warning("session %s: %s", session_id, exc)
        self.store.enqueue(session_id, text, now)
        self._set_state(session_id, "active", now)
        await self.drain()
        queued = any(q["session"] == session_id for q in self.store.queued())
        return web.json_response({"say": "Принято — сессия ждёт места на линии." if queued else "Принято."})

    async def owner_press(self, request: web.Request) -> web.Response:
        button = (await _body(request)).get("button")
        now = self.clock()
        spent = self.store.spend_button(button, now) if isinstance(button, str) else None
        if spent is None:
            return web.json_response({"toast": "Кнопка больше не действует."})
        approval = self.store.approval(spent["approval"])
        if approval is None or not self._pressable(approval, spent["digest"]):
            return web.json_response({"toast": "Карточка устарела — решение не принято."})
        action = spent["action"]
        if action == approvals.OPEN:
            keep = [[LABELS[a], self.store.add_button(approval["id"], a, approval["digest"], now + BUTTON_TTL_S)]
                    for a in (approvals.APPROVE, approvals.REJECT)]
            return web.json_response({"toast": "Ввод целиком — файлом.", "view": approval["view"], "keep": keep,
                                      "document": {"name": f"{approval['id']}.txt",
                                                   "text": approvals.input_text(approval)}})
        granted = action == approvals.APPROVE
        view = {**approval["view"], "status": "Разрешено — выполняю." if granted else "Отказано."}
        if not self.store.move(approval["id"], ("shown",), "granted" if granted else "rejected", now, view=view):
            return web.json_response({"toast": "Карточка устарела — решение не принято."})
        self.store.enqueue(approval["session"], None, now)  # a bare resume: the CLI asks PreToolUse again
        self._set_state(approval["session"], "active", now)
        await self.drain()
        return web.json_response({"toast": "Разрешено." if granted else "Отказано.", "view": view, "keep": []})

    def _pressable(self, approval: dict, digest: str) -> bool:
        """The card still asks what the session waits on: shown, its session waiting with this call as the last
        thing it said, nothing written by the owner since, and the button bound to this very call."""
        session = self.store.session(approval["session"])
        last = self.store.last_line(approval["session"])
        if approval["kind"] != "approval" or approval["state"] != "shown" or session is None or \
                session["state"] != "waiting" or last is None or digest != approval["digest"]:
            return False
        line, at = last
        deferred = transcript.deferred_of(line)
        wrote = self.store.last_input_at(approval["session"])
        return isinstance(deferred, dict) and deferred.get("id") == approval["tool_use_id"] and \
            (wrote is None or wrote <= at)

    # --- runs on the lanes --------------------------------------------------------------------------------------

    async def drain(self) -> None:
        """Start the queued runs there is room for, oldest first: one run per session at a time, the lane's own cap
        and the platform's total. A run the lane could not take now stays queued."""
        async with self._draining:
            running = self.store.open_runs()
            sessions = {r["session"] for r in running}
            per_lane, total = Counter(r["lane"] for r in running), len(running)
            for queued in self.store.queued():
                if total >= self.cfg.max_sessions:
                    break
                session = self.store.session(queued["session"])
                if session is None or queued["session"] in sessions:
                    continue
                sessions.add(queued["session"])  # the session's later entries wait behind this one
                if per_lane[session["lane"]] >= self.cfg.lanes[session["lane"]].max_sessions:
                    continue
                status = await self._start(session, queued)
                if status not in ("unavailable", "busy", "running"):
                    self.store.dequeue(queued["seq"])
                if status in ("started", "unavailable"):  # an unknown answer's run holds its room
                    per_lane[session["lane"]] += 1
                    total += 1

    async def _start(self, session: dict, queued: dict) -> str:
        now = self.clock()
        track, lane = self.tracks.get(session["track"]), self.cfg.lanes[session["lane"]]
        if track is None:
            return self._refused(session, f"трека {session['track']} больше нет в настройках", now)
        marks = approvals.marks_for(track, lane)
        token = secrets.token_urlsafe(32)
        run = self.store.add_run(session["id"], self.gate.token_hash(token), [dataclasses.asdict(m) for m in marks],
                                 now)
        body = {"session": session["id"], "run": run, "cli_session": session["cli_session"] or None,
                "cwd": track.cwd, "settings": cliengine.settings(lane.hook_url, CLEANUP_DAYS, [m.rule for m in marks]),
                "allowed": [m.rule for m in marks if m.kind == "allow"],
                "disallowed": [m.rule for m in marks if m.kind != "allow"], "prompt": queued["prompt"],
                "hook_token": token}
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
        return self._refused(session, str(answer.get("error") or status), now)

    def _refused(self, session: dict, why: str, now: float) -> str:
        self._set_state(session["id"], "idle", now)
        self._summary(session, f"Линия не запустила сессию: {why}")
        return "refused"

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
        if len(items) < POLL_LIMIT:
            # The lane journals a run it started, or refused, before it answers: a lost answer (not one still awaited,
            # left out above) whose run the whole journal since does not name never reached it; the entry is sent again.
            for run in attempts:
                if (seq := self.store.attempted(run)) is not None:
                    self.store.end_run(run, self.clock())
                    self.store.attempt(seq, None)
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
        # What a line does is done before it is stored: a crash in between does it again, and every effect is a
        # state move that happens once.
        self._apply(session, run, _parsed(line), now)
        if (turn := self.store.add_raw(lane, item["seq"], session_id, run, line, now)) is not None:
            self.store.reproject(session_id, turn)

    def _apply(self, session: dict, run: str, line: dict, now: float) -> None:
        kind = line.get("type")
        if kind == "system" and line.get("subtype") == "init":
            self.store.update_session(session["id"], now, cli_session=str(line.get("session_id") or ""),
                                      cli_version=str(line.get("claude_code_version") or ""))
        elif kind == "result":
            self._result(session, run, line, now)
        elif kind == "lane":
            event = line.get("event")
            if event in ("exited", "restarted", "refused"):
                self.store.end_run(run, now)
            if event in ("started", "refused") and (seq := self.store.attempted(run)) is not None:
                self.store.dequeue(seq)
                if event == "started":
                    self._set_state(session["id"], "active", now)
                else:
                    self._refused(session, str(line.get("error") or event), now)
            current = self.store.session(session["id"])["state"]
            if (event in ("stopped", "restarted") and current != "closed") or (event == "exited" and
                                                                                current == "active"):
                self._set_state(session["id"], "idle", now)

    def _result(self, session: dict, run: str, line: dict, now: float) -> None:
        stop = line.get("stop_reason")
        if stop == "tool_deferred":
            deferred = transcript.deferred_of(line)
            call_id = str(deferred.get("id") or "") if isinstance(deferred, dict) else ""
            for approval in self.store.approvals(run=run, kind="approval", states=("pending",)):
                if approval["tool_use_id"] != call_id:  # parallel calls: the CLI keeps only the last one
                    self.store.move(approval["id"], ("pending",), "dropped", now)
            asked = self.store.approvals(tool_use_id=call_id, kind="approval")
            shown = [self._show(session, a, now) for a in asked if a["state"] == "pending"]
            if any(shown) or any(a["state"] == "shown" for a in asked):
                self._set_state(session["id"], "waiting", now)
                return
            self._set_state(session["id"], "idle", now)
            if not self.store.approvals(tool_use_id=call_id, kind="courier"):
                # The gate deferred while it shut down: the call is asked again, or the owner's decision on it,
                # which the CLI has not heard yet, is given.
                self.store.enqueue(session["id"], None, now)
        elif stop == "tool_deferred_unavailable":
            for approval in self.store.approvals(session=session["id"], kind="approval", states=("granted",)):
                self.store.move(approval["id"], ("granted",), "expired", now)
            self._set_state(session["id"], "idle", now)
            self._summary(session, "Отложенный вызов продолжить нельзя: этого инструмента у сессии больше нет. "
                                   "Напиши, что делать дальше.")
        elif line.get("is_error") or str(line.get("subtype") or "").startswith("error"):
            why = str(line.get("result") or "") or str(line.get("subtype") or "") or "без описания"
            self._summary(session, f"Ход оборвался ошибкой: {why}")
            self._set_state(session["id"], "idle", now)
        else:
            self._summary(session, str(line.get("result") or "") or "Ход закончен без текста.")
            self._set_state(session["id"], "closed" if session["kind"] == "code" else "idle", now)

    def _show(self, session: dict, approval: dict, now: float) -> bool:
        view, actions = approvals.card(approval, session["id"], session["track"])
        data = dataclasses.asdict(view)
        if not self.store.move(approval["id"], ("pending",), "shown", now, view=data):
            return False
        buttons = [[LABELS[a], self.store.add_button(approval["id"], a, approval["digest"], now + BUTTON_TTL_S)]
                   for a in actions]
        self.store.add_note("card", session["id"], {"view": data, "buttons": buttons}, approval=approval["id"])
        return True

    def _summary(self, session: dict, text: str) -> None:
        self.store.add_note("summary", session["id"], {"head": f"Итог · {session['track']} · сессия {session['id']}",
                                                       "text": text})

    def _set_state(self, session: str, state: str, now: float) -> None:
        self.store.update_session(session, now, state=state, idle_since=now if state == "idle" else None)

    # --- the clock ----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        for session in self.store.sessions_in("idle", idle_before=now - IDLE_CLOSE_S):
            self._set_state(session, "closed", now)
        self.store.prune(now)
        for approval in self.store.approvals(kind="approval", states=("shown",)):
            if approval["at"] <= now - BUTTON_TTL_S and self.store.move(approval["id"], ("shown",), "expired", now):
                retire(self.store, {**approval, "state": "expired"}, "Истекла: решения не было семь дней", now)
                if self.store.session(approval["session"])["state"] == "waiting":
                    self._set_state(approval["session"], "idle", now)

    # --- the process --------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._poller(lane)) for lane in self.lanes]
        self.tasks.append(asyncio.create_task(self._clock()))

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
            except Exception:
                log.exception("the clock failed")
            await asyncio.sleep(TICK_S)

    async def shutdown(self) -> None:
        """Every PreToolUse waiting now, and every one after, defers; then every running session is stopped. A call
        deferred this way, granted or not, is resumed once the platform is back (`_result`)."""
        self.gate.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for run in self.store.open_runs():
            try:
                await self.lanes[run["lane"]].stop(run["session"])
            except Unavailable as exc:
                log.warning("session %s not stopped: %s", run["session"], exc)


def _bearer(request: web.Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip()


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _parsed(line: str) -> dict:
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(prog="retinue-platform")
    parser.add_argument("--rebuild-projection", action="store_true",
                        help="project every session's transcript again from its raw lines, then exit")
    args = parser.parse_args()
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
        runner = web.AppRunner(platform.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        platform.start()
        log.info("platform on :%d, lanes %s, tracks %s", cfg.listen_port, ", ".join(cfg.lanes),
                 ", ".join(platform.tracks))
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        await platform.shutdown()
        await runner.cleanup()

    asyncio.run(run())


if __name__ == "__main__":
    main()
