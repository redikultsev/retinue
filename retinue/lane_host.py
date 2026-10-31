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
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import shutil
import stat
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from . import cliengine, instructions
from .cliengine import Refused
from .config import LaneHostConfig

log = logging.getLogger("retinue.lane_host")

LINE_CAP = 8 * 1024 * 1024  # a longer stdout line is journaled as its length only
STOP_GRACE_S = 10           # SIGINT, then SIGTERM after this long
KILL_GRACE_S = 5            # on the host's own shutdown: then SIGKILL after this long (compose: stop_grace_period)
PROBE_TIMEOUT_S = 15        # `claude --version`, `claude auth status`
SESSION_ID = re.compile(r"[0-9a-f]{8}")  # sessions.PlatformStore.new_session
# A run or an answer with the owner's attachments: the blocks and the files in base64, and the rest of the body.
BODY_MAX = cliengine.CONTENT_B64 + cliengine.INBOX_BYTES * 4 // 3 + 8 * 1024 * 1024


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
        """The lines up to `upto` given. A `stream_event` line (a message's text while it is written, many per message)
        is deleted once given: the platform keeps none of them, and the message's own line carries the text whole."""
        partial = self.db.execute("DELETE FROM lines WHERE seq <= ? AND given = 0 AND json_valid(line) AND "
                                  "json_extract(line, '$.type') = 'stream_event'", (upto,)).rowcount
        cursor = self.db.execute("UPDATE lines SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return partial + cursor.rowcount

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
    cwd: Path  # where the owner's files of a later message go (`save_inbox`)


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 2**20 else f"{n / 2**20:.1f} MB"


def save_inbox(cwd: Path, files: list[dict], date: str, work: Path | None = None) -> list[tuple[str, int]]:
    """The owner's files written into `<cwd>/inbox/<date>-<name>`, each as (path relative to `cwd`, size). Every digest
    is checked before anything is written. Nothing is followed and nothing but a regular file is opened: a session
    that made `inbox` or a file in it a link, a FIFO or a device gets a refusal or another name, never a write
    elsewhere nor a wait. A name taken by other bytes gets «-2», «-3»…; one holding these very bytes is the same file
    — a message sent again writes nothing twice. Blocking file work: called in a thread (`_saved`). Inside a git
    repository under `work`, `inbox/` goes into the repository's own `.git/info/exclude` (`exclude_inbox`)."""
    given = []
    for item in files:
        data = base64.b64decode(item["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise Refused(f"files: «{item['name']}» does not match its sha256")
        given.append((f"{date}-{item['name']}", data, item["sha256"]))
    try:
        top = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise Refused(f"inbox: the session's folder did not open ({type(exc).__name__})") from None
    try:
        try:
            os.mkdir(cliengine.INBOX_DIR, 0o755, dir_fd=top)
        except FileExistsError:
            pass
        try:
            inbox = os.open(cliengine.INBOX_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top)
        except OSError:
            raise Refused(f"{cliengine.INBOX_DIR}: not a folder (or a link) in the session's folder") from None
        try:
            saved = [(f"{cliengine.INBOX_DIR}/{_save_one(inbox, name, data, digest)}", len(data))
                     for name, data, digest in given]
        finally:
            os.close(inbox)
    except OSError as exc:
        raise Refused(f"inbox: a file was not written ({type(exc).__name__})") from None
    finally:
        os.close(top)
    if work is not None:
        try:
            exclude_inbox(cwd, work)
        except OSError as exc:  # a courtesy to the owner's `git status`: its failure stops nothing
            log.warning("inbox: .git/info/exclude not written (%s)", type(exc).__name__)
    return saved


def _regular(fd: int, size: int | None = None) -> bool:
    """`fd` is a regular file with one name (of `size` bytes, when given)."""
    st = os.fstat(fd)
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and (size is None or st.st_size == size)


def _save_one(inbox: int, name: str, data: bytes, digest: str) -> str:
    stem, dot, ext = name.rpartition(".") if "." in name[11:] else (name, "", "")
    for n in range(1, 100):
        candidate = name if n == 1 else f"{stem}-{n}{dot}{ext}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=inbox)
        except FileExistsError:
            try:  # never a wait on a FIFO, never a read of more than these bytes
                fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox)
            except OSError:
                continue  # a link, a folder, a FIFO with no writer, a socket: another name
            with os.fdopen(fd, "rb") as there:
                if _regular(there.fileno(), len(data)) and \
                        hashlib.sha256(there.read(len(data) + 1)).hexdigest() == digest:
                    return candidate
            continue
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        except OSError:
            os.unlink(candidate, dir_fd=inbox)
            raise
        return candidate
    raise Refused(f"inbox: no free name for «{name}»")


EXCLUDE_MAX = 1024 * 1024  # a longer .git/info/exclude is not read, and not written


def exclude_inbox(cwd: Path, work: Path) -> bool:
    """`/<path to cwd>/inbox/` appended to `.git/info/exclude` of the git repository `cwd` is in (the nearest
    folder up to `work` holding a real `.git` folder), once: the owner's files stay out of the repository's
    `git status`. Only that file is touched — never hooks or config — and nothing on the way is followed: a `.git`,
    `info` or `exclude` that is a link, or an `exclude` that is not a regular file of one name, is left alone. A
    `.git` file (a worktree, a submodule) is left alone too. True when the line was added."""
    root = cwd
    while not (root / ".git").is_dir() or (root / ".git").is_symlink():
        if root == work or root.parent == root:
            return False
        root = root.parent
    line = f"/{(cwd / cliengine.INBOX_DIR).relative_to(root).as_posix()}/"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        fds.append(os.open(".git", flags, dir_fd=fds[-1]))
        try:
            os.mkdir("info", 0o755, dir_fd=fds[-1])
        except FileExistsError:
            pass
        fds.append(os.open("info", flags, dir_fd=fds[-1]))
        fd = os.open("exclude", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=fds[-1])
        with os.fdopen(fd, "r+b") as exclude:
            if not _regular(exclude.fileno()) or os.fstat(exclude.fileno()).st_size > EXCLUDE_MAX:
                return False
            lines = exclude.read().decode("utf-8", "replace").splitlines()
            if line in lines:
                return False
            exclude.seek(0, os.SEEK_END)
            exclude.write(("\n" if lines and exclude.tell() and not _ends_newline(exclude) else "").encode()
                          + f"# the owner's files from Retinue\n{line}\n".encode())
        return True
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _ends_newline(f) -> bool:
    f.seek(-1, os.SEEK_END)
    last = f.read(1)
    f.seek(0, os.SEEK_END)
    return last == b"\n"


CLONE_TIMEOUT_S = 600   # a first clone of a big mirror: local, no network
CLONE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")   # config.REPO_NAME
AUTHOR = re.compile(r"([^<>\n]+) <([^<>@\s]+@[^<>\s]+)>")


def check_code(spec: object) -> dict | None:
    """The platform's `code` of a run: `{root, author, repos}` — names only; the mirrors' folder is this host's own."""
    if spec is None:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("root"), str) or not CLONE_NAME.fullmatch(spec["root"]) \
            or not isinstance(spec.get("repos"), list) or not all(isinstance(r, str) and CLONE_NAME.fullmatch(r)
                                                                  for r in spec["repos"]) \
            or not isinstance(spec.get("author"), str) or not AUTHOR.fullmatch(spec["author"]):
        raise Refused("code: not {root, author, repos}")
    return spec


def ensure_clone(work: Path, mirrors: Path, root: str, name: str, author: str) -> bool:
    """`<work>/<root>/<name>`: a clone of the platform's mirror `<mirrors>/<name>.git` (read-only here), made once —
    its `origin` the mirror, so `git pull` in the room needs no credential and reaches nothing but it. The owner's
    name for its commits, `out/` and `inbox/` in its `.git/info/exclude`. Made in a temporary folder beside it and
    renamed, so a half clone never stands under the name. Nothing on the way is followed: `<root>` that is a link is
    refused, a `<name>` that exists — whatever it is — is the session's and left alone. True when it made one."""
    mirror = mirrors / f"{name}.git"
    try:
        if not stat.S_ISDIR(os.lstat(mirror).st_mode):
            return False
    except FileNotFoundError:
        return False
    top = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(root, 0o775, dir_fd=top)
        except FileExistsError:
            pass
        try:
            here = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top)
        except OSError:
            raise Refused(f"/work/{root}: not a folder (or a link)") from None
    finally:
        os.close(top)
    try:
        try:
            os.lstat(name, dir_fd=here)
            return False
        except FileNotFoundError:
            pass
        tmp = f".{name}.retinue-{os.urandom(4).hex()}"
        pinned = Path("/proc/self/fd").is_dir()
        where = f"/proc/self/fd/{here}" if pinned else str(work / root)
        found = AUTHOR.fullmatch(author)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "LANG": "C.UTF-8",
               "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}
        git = ["git", "-c", f"safe.directory={mirror}", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
        try:
            for argv in ([*git, "clone", "--quiet", "--no-hardlinks", "--template=", "--", str(mirror), tmp],
                         [*git, "-C", tmp, "config", "user.name", found.group(1).strip()],
                         [*git, "-C", tmp, "config", "user.email", found.group(2)]):
                subprocess.run(argv, cwd=where, env=env, pass_fds=(here,) if pinned else (), check=True,
                               capture_output=True, timeout=CLONE_TIMEOUT_S, stdin=subprocess.DEVNULL)
            info = os.open(f"{tmp}/.git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
            try:
                try:
                    os.mkdir("info", 0o775, dir_fd=info)
                except FileExistsError:
                    pass
                with open(os.open("info/exclude", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o664,
                                  dir_fd=info), "a") as exclude:
                    exclude.write("# the session's files for the owner and his attachments (Retinue)\n/out/\n/inbox/\n")
            finally:
                os.close(info)
            try:
                os.rename(tmp, name, src_dir_fd=here, dst_dir_fd=here)
            except OSError:  # another run made it meanwhile, or the name is taken now: theirs stands
                shutil.rmtree(tmp, dir_fd=here, ignore_errors=True)
                return False
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            shutil.rmtree(tmp, dir_fd=here, ignore_errors=True)
            err = getattr(exc, "stderr", b"") or b""
            log.error("code %s: not cloned: %s %s", name, type(exc).__name__, err.decode("utf-8", "replace")[-300:])
            return False
    finally:
        os.close(here)


def with_saved(prompt: str | list[dict], saved: list[tuple[str, int]]) -> list[dict]:
    """The prompt with one more text block: where the owner's files now are. Written by code, from what was written."""
    blocks = [{"type": "text", "text": prompt}] if isinstance(prompt, str) else list(prompt)
    lines = "\n".join(f"- {path} ({_size(size)})" for path, size in saved)
    return [*blocks, {"type": "text", "text": f"The owner's files are saved in the session's folder:\n{lines}"}]


def token_pipe(token: str) -> int:
    """The read end of a pipe that holds `token` and nothing more: the CLI reads it once at start, and then it is
    empty — unlike an environment variable, which /proc/<pid>/environ shows for the process's whole life."""
    read, write = os.pipe()
    try:
        os.write(write, token.encode())  # far below a pipe's buffer: the write never blocks
    finally:
        os.close(write)
    return read


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
        self.closing = False  # the host is shutting down: no run starts
        # The subscription token, as the platform last sent it with a run: in memory only — never journaled, logged
        # or written — so nothing in this container holds it but this process and a pipe to its CLI.
        self.oauth_token = ""

    def _env(self, hook_token: str, token_fd: int) -> dict[str, str]:
        cfg = self.cfg
        return cliengine.check_env(cliengine.env(cfg.home, cfg.config_dir, cfg.proxy, cfg.no_proxy, hook_token,
                                                 token_fd))

    async def _spawn(self, argv: list[str], hook_token: str, **kwargs) -> asyncio.subprocess.Process:
        """The CLI with its token in a pipe of its own: the only descriptor it inherits, closed here once it has it."""
        token_fd = token_pipe(self.oauth_token)
        try:
            return await asyncio.create_subprocess_exec(*argv, env=self._env(hook_token, token_fd),
                                                        pass_fds=(token_fd,), **kwargs)
        finally:
            os.close(token_fd)

    async def _cli(self, *args: str) -> str:
        """The pinned CLI's answer to a question that starts no session; empty when it gave none."""
        try:
            proc = await self._spawn([cliengine.cli_path(), *args], "", stdin=asyncio.subprocess.DEVNULL,
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
        app = web.Application(middlewares=[self._signed], client_max_size=BODY_MAX)
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
            raise Refused(f"cwd: not a path inside {self.cfg.work}")
        work = Path(self.cfg.work).resolve()
        path = (work / cwd).resolve()
        if not path.is_relative_to(work) or not path.is_dir():
            raise Refused(f"cwd: no such folder in {self.cfg.work}")
        return path

    def _notes(self, cwd: Path) -> str:
        """The owner's memory index, then the repository's rules. A repository that breaks the reading never fails
        the run: it runs without them and is told so; a memory that cannot be read is left out, said in the log."""
        try:
            memory = instructions.owner_memory(cliengine.owner_memory_dir())
        except Exception as exc:  # noqa: BLE001 — the run goes on without it
            log.error("the owner's memory was not read: %s", type(exc).__name__)
            memory = ""
        try:
            rules = instructions.compose(cwd, Path(self.cfg.work))
        except Exception as exc:  # noqa: BLE001 — whatever a repository holds, the run goes on
            log.error("the repository's rules in %s were not read: %s", cwd, type(exc).__name__)
            rules = instructions.unread(exc)
        return "\n".join(part for part in (memory, rules) if part)

    async def _saved(self, cwd: Path, files: list[dict], date: str) -> list[tuple[str, int]]:
        """`save_inbox` off the event loop: the lane's journal and its other runs go on while files are written."""
        return await asyncio.to_thread(save_inbox, cwd, files, date, Path(self.cfg.work).resolve())

    def _argv(self, body: dict, settings: Path, notes: bool = False, skills: bool = False) -> list[str]:
        allowed, disallowed = body.get("allowed") or [], body.get("disallowed") or []
        if not all(isinstance(rules, list) and all(isinstance(r, str) for r in rules) for rules in (allowed, disallowed)):
            raise Refused("allowed/disallowed: not lists of rules")
        resume = body.get("cli_session")
        if resume is not None and not isinstance(resume, str):
            raise Refused("cli_session: not a session id")
        fork = body.get("fork", False)  # the first run of a fork: `cli_session` is its parent's, copied, not continued
        if type(fork) is not bool or (fork and (not resume or body.get("prompt") is None)):
            raise Refused("fork: a fork without a parent session or without a first message")
        subs = body.get("subs", False)  # the platform's sub-sessions server, at this lane's own hook address
        if type(subs) is not bool:
            raise Refused("subs: not true/false")
        return cliengine.check_argv(cliengine.argv(str(settings), allowed, disallowed, resume, instructions=notes,
                                                    skills=skills, fork=fork,
                                                    mcp=self.cfg.hook_url if subs else None), self.cfg.hook_url)

    async def run(self, request: web.Request) -> web.Response:
        body = await _body(request)
        session, run = body.get("session"), body.get("run")
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session) or \
                not isinstance(run, str) or not cliengine.RUN_ID.fullmatch(run):
            return _refused("session/run: wrong ids")
        async with self.starting:
            if session in self.live:
                return web.json_response({"status": "running"})
            if self.closing or len(self.live) >= self.cfg.max_running:  # busy: the platform keeps it queued
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
            raise Refused(f"CLI is not {cliengine.CLI_VERSION}: {self.version or 'no answer'}")
        settings = cliengine.check_settings(body.get("settings"), self.cfg.hook_url)
        prompt, hook_token = body.get("prompt"), body.get("hook_token")
        if prompt is not None:
            prompt = cliengine.check_content(prompt)
        if prompt is None and not body.get("cli_session"):  # '' is no session, as in the store and `cliengine.argv`
            raise Refused("a new session without a prompt")
        files = body.get("files")
        if files is not None:
            if prompt is None:
                raise Refused("files: without a prompt")
            files = cliengine.check_files(files, body.get("date"))
        if not isinstance(hook_token, str) or not hook_token:
            raise Refused("hook_token: missing")
        oauth_token = body.get("oauth_token")
        if not isinstance(oauth_token, str) or not oauth_token or "\n" in oauth_token:
            raise Refused("oauth_token: the platform did not pass the subscription login — no run starts without it")
        self.oauth_token = oauth_token
        if (code := check_code(body.get("code"))) is not None:  # the owner's repositories, cloned once each
            for name in code["repos"]:
                if await asyncio.to_thread(ensure_clone, Path(self.cfg.work), Path(self.cfg.mirrors), code["root"],
                                           name, code["author"]):
                    log.info("code %s: cloned into /work/%s", name, code["root"])
            parts = str(body.get("cwd") or "").split("/")
            if len(parts) >= 2 and parts[0] == code["root"] and not (Path(self.cfg.work) / code["root"] /
                                                                    parts[1]).is_dir():
                raise Refused(f"repository {parts[1]} is not cloned yet: the platform has not fetched its mirror")
        cwd = self._cwd(body.get("cwd"))
        path = Path(self.cfg.run_dir) / f"{run}.json"
        # The owner's memory, the repository's rules and his skills, read again for every run the platform asks them
        # for — the room's too; only the retired lane-web's host (RETINUE_LANE_WEB=1) refuses them.
        context = body.get("instructions") is True and not self.cfg.web
        notes = self._notes(cwd) if context else ""
        skills = context and instructions.usable_skills(cliengine.skills_dir()) is not None
        argv = self._argv(body, path, bool(notes), skills)
        self._env(hook_token, 0)  # refused here, before anything is written, not at the spawn
        notes_path = Path(cliengine.instructions_path(str(path)))
        try:
            for target, text in ((path, json.dumps(settings, ensure_ascii=False)), (notes_path, notes)):
                target.unlink(missing_ok=True)
                if text:
                    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444), "w",
                                   encoding="utf-8") as out:
                        out.write(text)
        except OSError as exc:
            path.unlink(missing_ok=True)
            notes_path.unlink(missing_ok=True)
            raise Refused(f"settings not written: {type(exc).__name__}") from None
        if files:  # last, after every check and the run's own files: a refused run leaves nothing in the inbox
            try:
                prompt = with_saved(prompt, await self._saved(cwd, files, body["date"]))
            except BaseException:
                path.unlink(missing_ok=True)
                notes_path.unlink(missing_ok=True)
                raise
        try:
            proc = await self._spawn(argv, hook_token, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                                     stdout=asyncio.subprocess.PIPE)
        except OSError as exc:
            path.unlink(missing_ok=True)
            notes_path.unlink(missing_ok=True)
            raise Refused(f"the CLI did not start: {type(exc).__name__}") from None
        if prompt is None:
            proc.stdin.close()  # a resume after a decision: the CLI settles the deferred call and goes on alone
        else:
            proc.stdin.write(cliengine.user_line(prompt))
        return Live(session, run, proc, path, cwd)

    async def _pump(self, live: Live) -> None:
        """Journal the process's stdout to its end; its input closes with its first `result`. However the pump ends,
        `exited` is journaled: a run whose end the platform never hears holds its session and its room for good."""
        exited = False
        try:
            async for line in lines(live.proc.stdout, LINE_CAP):
                if isinstance(line, int):
                    self.journal.event(live.session, live.run, "truncated", bytes=line)
                    continue
                text = line.decode("utf-8", "replace")
                if not text.strip():
                    continue
                self.journal.add(live.session, live.run, text)
                if (answer := cliengine.deny_control(text)) is not None and not live.proc.stdin.is_closing():
                    live.proc.stdin.write(answer)  # an ask no hook decided: denied, never left waiting
                if _is_result(text):
                    live.proc.stdin.close()
            code = await live.proc.wait()
            self.journal.event(live.session, live.run, "exited", code=code)
            exited = True
        finally:
            if not exited:  # cut: the host's shutdown, a stdout a grandchild still holds
                self.journal.event(live.session, live.run, "exited", code=live.proc.returncode)
            self.journal.ended(live.session)
            self.live.pop(live.session, None)
            live.settings.unlink(missing_ok=True)
            Path(cliengine.instructions_path(str(live.settings))).unlink(missing_ok=True)

    async def answer(self, request: web.Request) -> web.Response:
        body = await _body(request)
        session, prompt = body.get("session"), body.get("prompt")
        if not isinstance(session, str):
            return _refused("session: not an id")
        try:
            prompt = cliengine.check_content(prompt) if prompt is not None else None
            files = cliengine.check_files(body["files"], body.get("date")) if "files" in body else None
        except Refused as exc:
            return _refused(str(exc))
        if prompt is None:
            return _refused("prompt: not text")
        live = self.live.get(session)
        if live is None or live.proc.stdin.is_closing():
            return web.json_response({"status": "not_running"})
        if files:
            try:
                prompt = with_saved(prompt, await self._saved(live.cwd, files, body["date"]))
            except Refused as exc:
                return _refused(str(exc))
        try:
            live.proc.stdin.write(cliengine.user_line(prompt))
            await live.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return web.json_response({"status": "not_running"})
        return web.json_response({"status": "sent"})

    async def stop(self, request: web.Request) -> web.Response:
        session = (await _body(request)).get("session")
        if not isinstance(session, str):
            return _refused("session: not an id")
        live = self.live.get(session)
        if live is None:
            return web.json_response({"status": "not_running"})
        self._stop(live)
        return web.json_response({"status": "stopping"})

    def _stop(self, live: Live, **why) -> None:
        self.journal.event(live.session, live.run, "stopped", **why)
        self._spawn_later(self._interrupt(live.proc))

    async def shutdown(self) -> None:
        """The host's own stop (SIGTERM from Docker): no run starts any more, every live one is stopped as `/stop` stops
        it, and its end is waited for, so its `exited` is journaled now rather than `restarted` by the next host. A
        process that outlives both graces is killed; a pump a grandchild still holds open is cut, its `exited`
        journaled all the same. `by: shutdown` tells the platform the stop was not the owner's: the turn goes on."""
        async with self.starting:
            self.closing = True
            for live in list(self.live.values()):
                self._stop(live, by="shutdown")
        waiting = list(self.tasks)
        _, late = await asyncio.wait(waiting, timeout=STOP_GRACE_S + KILL_GRACE_S) if waiting else (set(), set())
        if late:
            for live in list(self.live.values()):
                try:
                    live.proc.kill()
                except ProcessLookupError:
                    pass
            _, late = await asyncio.wait(late, timeout=KILL_GRACE_S)
            for task in late:
                task.cancel()
            await asyncio.gather(*late, return_exceptions=True)

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
            return _refused("upto: not a number")
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
    # Group-writable files, inherited by every CLI and its tools: in the Base's clone (a setgid folder of the group
    # the lane shares with the platform) the platform must be able to put the hub's version over a file a session
    # wrote (docs/base.md). Elsewhere the group is the lane user's own.
    os.umask(0o002)

    async def run() -> None:
        host = LaneHost(cfg, Journal(f"{cfg.state}/journal.sqlite"))
        await host.start()
        runner = web.AppRunner(host.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("lane host on :%d, CLI %s, at most %d runs", cfg.listen_port, host.version or "?", cfg.max_running)
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("stopping %d runs", len(host.live))
        await host.shutdown()
        await runner.cleanup()

    asyncio.run(run())


if __name__ == "__main__":
    main()
