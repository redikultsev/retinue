"""A lane's host: the only process in a lane container that the platform talks to, and the only one that starts the
pinned CLI. It knows a closed set of commands — start a run, answer a live one, stop one, say how it is — and keeps
a journal: every stdout line of every CLI process, whole and in order, with the host's own events between them. The
platform pulls the journal by number and confirms it (the pattern of `tgbusiness.State`), so a line the platform
missed while it was away is simply given again.

Nothing the platform sends is run as it came: a run's settings, argv and environment are built and checked by
`cliengine`, its settings may point hooks only at this lane's hook address, its directory lies under /work, and no
run starts unless `claude --version` said the pinned version when the host came up.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from . import cliengine
from .cliengine import Refused
from .config import LaneHostConfig

log = logging.getLogger("retinue.lane_host")

LINE_CAP = 8 * 1024 * 1024  # a longer stdout line is journaled as its length only
STOP_GRACE_S = 10           # SIGINT, then SIGTERM after this long
PROBE_TIMEOUT_S = 15        # `claude --version`, `claude auth status`
SESSION_ID = re.compile(r"[0-9a-f]{8}")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # it names the run's settings file: cliengine.SETTINGS_PATH


class Journal:
    """The lane's own SQLite: the lines the platform has not confirmed, and the CLI processes running now — what is
    left of them after the host died is what it journals as `restarted` when it comes up again."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS lines (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL,
                run TEXT NOT NULL,
                line TEXT NOT NULL,                 -- a CLI stdout line as it came, or a {"type": "lane"} event
                given INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS running (session TEXT PRIMARY KEY, run TEXT NOT NULL, pid INTEGER NOT NULL);
            """)
        self.db.commit()

    def add(self, session: str, run: str, line: str) -> None:
        self.db.execute("INSERT INTO lines (session, run, line) VALUES (?, ?, ?)", (session, run, line))
        self.db.commit()

    def event(self, session: str, run: str, event: str, **fields) -> None:
        self.add(session, run, json.dumps({"type": "lane", "event": event, **fields}, ensure_ascii=False))

    def look(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT seq, session, run, line FROM lines WHERE given = 0 ORDER BY seq LIMIT ?",
                               (limit,))
        return [{"seq": seq, "session": session, "run": run, "line": line} for seq, session, run, line in rows]

    def confirm(self, upto: int) -> int:
        cursor = self.db.execute("UPDATE lines SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return cursor.rowcount

    def started(self, session: str, run: str, pid: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO running VALUES (?, ?, ?)", (session, run, pid))
        self.db.commit()

    def ended(self, session: str) -> None:
        self.db.execute("DELETE FROM running WHERE session = ?", (session,))
        self.db.commit()

    def recover(self) -> int:
        """The runs a previous host left running, each journaled as `restarted`, and forgotten."""
        left = self.db.execute("SELECT session, run, pid FROM running ORDER BY session").fetchall()
        for session, run, pid in left:
            self.event(session, run, "restarted", pid=pid)
        self.db.execute("DELETE FROM running")
        self.db.commit()
        return len(left)


async def lines(stream: asyncio.StreamReader, cap: int) -> AsyncIterator[bytes | int]:
    """Each line of `stream` without its newline — or, for a line longer than `cap`, its length in bytes: such a line
    is never held whole."""
    buf, over = bytearray(), 0
    while chunk := await stream.read(1 << 16):
        buf += chunk
        start = 0
        while (end := buf.find(b"\n", start)) >= 0:
            size = over + end - start
            yield size if size > cap else bytes(buf[start:end])
            over, start = 0, end + 1
        del buf[:start]
        if len(buf) > cap:
            over += len(buf)
            buf.clear()
    if over or buf:
        size = over + len(buf)
        yield size if size > cap else bytes(buf)


def _is_result(line: str) -> bool:
    try:
        return json.loads(line).get("type") == "result"
    except (ValueError, AttributeError):
        return False


@dataclass
class Live:
    session: str
    run: str
    proc: asyncio.subprocess.Process
    settings: Path


class LaneHost:
    def __init__(self, cfg: LaneHostConfig, journal: Journal) -> None:
        if not cfg.token:
            raise SystemExit("the lane host needs RETINUE_LANE_TOKEN: without it anyone on its network starts a run")
        self.cfg, self.journal = cfg, journal
        self.live: dict[str, Live] = {}
        self.version = ""
        self.pinned = False
        self.starting = asyncio.Lock()  # the cap and the one-process-per-session rule hold across a spawn's awaits
        self.tasks: set[asyncio.Task] = set()

    def _env(self, hook_token: str) -> dict[str, str]:
        cfg = self.cfg
        return cliengine.check_env(cliengine.env(cfg.oauth_token, cfg.home, cfg.config_dir, cfg.proxy, cfg.no_proxy,
                                                 hook_token))

    async def _cli(self, *args: str) -> str:
        """The pinned CLI's answer to a question that starts no session; empty when it gave none."""
        try:
            proc = await asyncio.create_subprocess_exec(
                cliengine.cli_path(), *args, env=self._env(""), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            log.warning("the CLI did not start: %s", exc)
            return ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ""
        return out.decode("utf-8", "replace").strip()

    async def start(self) -> None:
        if left := self.journal.recover():
            log.warning("%d runs were left running by the previous host", left)
        self.version = await self._cli("--version")
        self.pinned = cliengine.version_ok(self.version)
        if not self.pinned:
            log.error("the CLI says %r, not %s: no run starts", self.version, cliengine.CLI_VERSION)

    def _spawn_later(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=1024 * 1024)
        app.add_routes([web.post("/run", self.run), web.post("/answer", self.answer), web.post("/stop", self.stop),
                        web.get("/ping", self.ping), web.post("/items", self.items),
                        web.post("/items/confirm", self.confirm)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, self.cfg.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _cwd(self, cwd: object) -> Path:
        if not isinstance(cwd, str) or cwd.startswith("/") or "\x00" in cwd or ".." in Path(cwd).parts:
            raise Refused(f"cwd: не путь внутри {self.cfg.work}")
        work = Path(self.cfg.work).resolve()
        path = (work / cwd).resolve()
        if not path.is_relative_to(work) or not path.is_dir():
            raise Refused(f"cwd: нет такой папки в {self.cfg.work}")
        return path

    def _argv(self, body: dict, settings: Path) -> list[str]:
        allowed, disallowed = body.get("allowed") or [], body.get("disallowed") or []
        if not all(isinstance(rules, list) and all(isinstance(r, str) for r in rules) for rules in (allowed, disallowed)):
            raise Refused("allowed/disallowed: не списки правил")
        resume = body.get("cli_session")
        if resume is not None and not isinstance(resume, str):
            raise Refused("cli_session: не id сессии")
        return cliengine.check_argv(cliengine.argv(str(settings), allowed, disallowed, resume))

    async def run(self, request: web.Request) -> web.Response:
        body = await _body(request)
        session, run = body.get("session"), body.get("run")
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session) or \
                not isinstance(run, str) or not RUN_ID.fullmatch(run):
            return _refused("session/run: не те id")
        async with self.starting:
            if session in self.live:
                return web.json_response({"status": "running"})
            if len(self.live) >= self.cfg.max_running:
                return web.json_response({"status": "busy"})
            try:
                live = await self._start(session, run, body)
            except Refused as exc:
                self.journal.event(session, run, "refused", error=str(exc))
                return _refused(str(exc))
            self.live[session] = live
        self.journal.started(session, run, live.proc.pid)
        self.journal.event(session, run, "started", pid=live.proc.pid)
        self._spawn_later(self._pump(live))
        return web.json_response({"status": "started"})

    async def _start(self, session: str, run: str, body: dict) -> Live:
        if not self.pinned:
            raise Refused(f"CLI не {cliengine.CLI_VERSION}: {self.version or 'не ответил'}")
        settings = cliengine.check_settings(body.get("settings"), self.cfg.hook_url)
        prompt, hook_token = body.get("prompt"), body.get("hook_token")
        if prompt is not None and not isinstance(prompt, str):
            raise Refused("prompt: не текст")
        if prompt is None and body.get("cli_session") is None:
            raise Refused("новая сессия без запроса")
        if not isinstance(hook_token, str) or not hook_token:
            raise Refused("hook_token: нет")
        cwd = self._cwd(body.get("cwd"))
        path = Path(self.cfg.run_dir) / f"{run}.json"
        argv, env = self._argv(body, path), self._env(hook_token)
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w") as out:
            json.dump(settings, out, ensure_ascii=False)
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE)
        except OSError as exc:
            path.unlink(missing_ok=True)
            raise Refused(f"CLI не запустился: {type(exc).__name__}") from None
        if prompt is None:
            proc.stdin.close()  # a resume after a decision: the CLI settles the deferred call and goes on alone
        else:
            proc.stdin.write(cliengine.user_line(prompt))
        return Live(session, run, proc, path)

    async def _pump(self, live: Live) -> None:
        """Journal the process's stdout to its end; its input closes with its first `result`."""
        try:
            async for line in lines(live.proc.stdout, LINE_CAP):
                if isinstance(line, int):
                    self.journal.event(live.session, live.run, "truncated", bytes=line)
                    continue
                text = line.decode("utf-8", "replace")
                if not text.strip():
                    continue
                self.journal.add(live.session, live.run, text)
                if _is_result(text):
                    live.proc.stdin.close()
            code = await live.proc.wait()
            self.journal.event(live.session, live.run, "exited", code=code)
        finally:
            self.journal.ended(live.session)
            self.live.pop(live.session, None)
            live.settings.unlink(missing_ok=True)

    def _live(self, body: dict) -> Live | None:
        session = body.get("session")
        return self.live.get(session) if isinstance(session, str) else None

    async def answer(self, request: web.Request) -> web.Response:
        body = await _body(request)
        live, prompt = self._live(body), body.get("prompt")
        if not isinstance(prompt, str):
            return _refused("prompt: не текст")
        if live is None or live.proc.stdin.is_closing():
            return web.json_response({"status": "not_running"})
        try:
            live.proc.stdin.write(cliengine.user_line(prompt))
            await live.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return web.json_response({"status": "not_running"})
        return web.json_response({"status": "sent"})

    async def stop(self, request: web.Request) -> web.Response:
        live = self._live(await _body(request))
        if live is None:
            return web.json_response({"status": "not_running"})
        self.journal.event(live.session, live.run, "stopped")
        self._spawn_later(self._interrupt(live.proc))
        return web.json_response({"status": "stopping"})

    @staticmethod
    async def _interrupt(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
            except TimeoutError:
                proc.terminate()
        except ProcessLookupError:
            pass

    async def ping(self, request: web.Request) -> web.Response:
        try:
            status = json.loads(await self._cli("auth", "status", "--json"))
        except ValueError:
            status = {}
        if not isinstance(status, dict):
            status = {}
        return web.json_response({
            "version": self.version, "pinned": self.pinned,
            "login": {"loggedIn": status.get("loggedIn") is True, "authMethod": str(status.get("authMethod") or "")},
            "running": sorted(self.live), "max": self.cfg.max_running})

    async def items(self, request: web.Request) -> web.Response:
        limit = (await _body(request)).get("limit")
        return web.json_response({"items": self.journal.look(min(limit if type(limit) is int and limit > 0 else 50,
                                                                 200))})

    async def confirm(self, request: web.Request) -> web.Response:
        upto = (await _body(request)).get("upto")
        if type(upto) is not int:
            return _refused("upto: не номер")
        return web.json_response({"confirmed": self.journal.confirm(upto)})


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _refused(error: str) -> web.Response:
    return web.json_response({"status": "refused", "error": error}, status=400)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = LaneHostConfig.load()

    async def run() -> None:
        host = LaneHost(cfg, Journal(f"{cfg.state}/journal.sqlite"))
        await host.start()
        runner = web.AppRunner(host.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("lane host on :%d, CLI %s, at most %d runs", cfg.listen_port, host.version or "?", cfg.max_running)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
