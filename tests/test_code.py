"""The owner's repositories in the room (`code.py`, docs/code.md), against real git: a remote served over smart HTTP
by `git http-backend` behind Basic auth (the token's only way in), the mirror, the room's clone made as the lane host
makes it, and the room's repository as hostile input — a config that tries to redirect the push, hooks and an
fsmonitor that try to run, alternates, links and a `.git` file that try to bring other objects in."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from retinue import code
from retinue.code import CodeRepos, Refused
from retinue.config import CodeConfig, CodeRepo, PlatformConfig
from retinue.lane_host import Refused as LaneRefused
from retinue.lane_host import check_code, ensure_clone

TOKEN = "github_pat_11TESTTOKEN0123456789abcdefXYZ"
AUTHOR = "Владелец <o@example.com>"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout


def commit(repo: Path, path: str, text: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).strip()


class GitServer:
    """`git http-backend` over HTTP, as GitHub's smart HTTP looks to git: every request needs Basic auth with the
    token (a 401 with a challenge otherwise); every request's path, query and headers are kept."""

    def __init__(self, root: Path, token: str = TOKEN) -> None:
        self.root, self.token, self.seen = root, token, []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                server.seen.append({"path": self.path, "headers": dict(self.headers)})
                given = self.headers.get("Authorization", "")
                user_pass = base64.b64decode(given[6:]).decode() if given.startswith("Basic ") else ""
                if user_pass.partition(":")[2] != server.token:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": str(server.root), "GIT_HTTP_EXPORT_ALL": "1",
                       "PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": self.command,
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                       "REMOTE_USER": "owner", "REMOTE_ADDR": "127.0.0.1", "GIT_CONFIG_NOSYSTEM": "1",
                       "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                       "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
                out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
                head, _, payload = out.partition(b"\r\n\r\n")
                status = 200
                headers = []
                for row in head.decode().split("\r\n"):
                    name, _, value = row.partition(": ")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                    elif name:
                        headers.append((name, value))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Remote:
    """A bare remote `o/r.git` with `main` (two commits), served by GitServer."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "remotes"
        self.bare = self.root / "o" / "r.git"
        git("init", "-q", "--bare", "-b", "main", str(self.bare))
        git("config", "http.receivepack", "true", cwd=self.bare)
        seed = tmp / "seed"
        git("clone", "-q", str(self.bare), str(seed))
        self.first = commit(seed, "README.md", "hello\n", "init")
        commit(seed, ".gitignore", "*.log\nbuild/\n", "ignore")
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.server = GitServer(self.root)
        self.url = f"{self.server.base}/o/r.git"

    def sha(self, branch: str) -> str | None:
        done = subprocess.run(["git", f"--git-dir={self.bare}", "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], capture_output=True, text=True)
        return done.stdout.strip() or None


@pytest.fixture
def remote(tmp_path):
    made = Remote(tmp_path)
    yield made
    made.server.close()


def repos(tmp: Path, url: str, **repo) -> CodeRepos:
    cfg = CodeConfig(repos=[CodeRepo("r", url, "owner", secret=TOKEN, **repo)], mirrors=str(tmp / "mirrors"),
                     outbox=str(tmp / "platform" / "code"), author=AUTHOR)
    (tmp / "work").mkdir(exist_ok=True)
    return CodeRepos(cfg, str(tmp / "work"))


def room(tmp: Path, c: CodeRepos, name: str = "r") -> Path:
    """The room's clone, made as the lane host makes it."""
    assert ensure_clone(tmp / "work", tmp / "mirrors", "code", name, AUTHOR)
    return tmp / "work" / "code" / name


def committed(repo: Path, path: str, text: str, message: str) -> str:
    """A commit as the session makes one: with the clone's own identity."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).strip()


# --- the mirror and the token -------------------------------------------------------------------------------------

def test_the_mirror_is_fetched_with_the_token_and_the_token_is_nowhere_but_the_request(tmp_path, remote, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(code.subprocess, "run", spy)
    c = repos(tmp_path, remote.url)
    assert c.fetch("r") and c.ready("r") and c.default_branch("r") == "main"
    assert git(f"--git-dir={c.mirror('r')}", "rev-parse", "refs/heads/main").strip() == remote.sha("main")
    assert git(f"--git-dir={c.mirror('r')}", "symbolic-ref", "HEAD").strip() == "refs/heads/main"

    # The remote saw the token in a Basic header only; never in a path or a query.
    authed = [s for s in remote.server.seen if s["headers"].get("Authorization")]
    assert authed and all(TOKEN not in s["path"] for s in remote.server.seen)
    # Never in an argument, an environment or a file of the platform's.
    for argv, env in calls:
        assert all(TOKEN not in a for a in argv) and all(TOKEN not in v for v in env.values())
    for path in [*tmp_path.joinpath("mirrors").rglob("*"), *tmp_path.joinpath("platform").rglob("*")]:
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path

    # A wrong token: refused with git's words, and the token is not among them.
    c.repos["r"].secret = "github_pat_WRONG"
    with pytest.raises(code.Unavailable) as refused:
        c.fetch("r")
    assert "github_pat_WRONG" not in str(refused.value)
    assert c.failed["r"]


def test_a_repository_without_its_token_is_off_and_the_config_names_the_variable(tmp_path, monkeypatch):
    path = tmp_path / "platform.yaml"
    path.write_text(
        "lanes:\n  private: {url: 'http://l:9600', hook_url: 'http://p/hook', work_mount: /lanes/private}\n"
        "tracks:\n  - {name: code, lane: private, kind: private, cwd: ''}\n"
        "code:\n  author: 'A B <a@b.c>'\n  proxy: 'http://platform-egress:3128'\n  repos:\n"
        "    - {name: retinue, url: 'https://github.com/you/retinue.git', token: you}\n"
        "    - {name: example-app, url: 'https://gitlab.com/example-group/example-app', token: example_gitlab}\n")
    for name in ("RETINUE_LANE_TOKEN_PRIVATE", "RETINUE_PLATFORM_TOKEN", "RETINUE_HOOK_SECRET",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RETINUE_GIT_TOKEN_YOU", TOKEN)
    monkeypatch.delenv("RETINUE_GIT_TOKEN_EXAMPLE_GITLAB", raising=False)
    cfg = PlatformConfig.load(path)
    retinue, app = cfg.code.repos
    assert (retinue.secret, retinue.off, retinue.path, retinue.host) == (TOKEN, "", "you/retinue", "github.com")
    assert app.off == "нет токена RETINUE_GIT_TOKEN_EXAMPLE_GITLAB" and app.path == "example-group/example-app"
    assert TOKEN not in repr(cfg.code) or retinue.secret == TOKEN  # held, and only in the field the push reads

    for bad in ("http://github.com/a/b", "https://github.com.evil.example/a/b", "https://evil.example/a/b",
                "https://github.com/a/../b", "git@github.com:a/b.git", "https://user:pw@github.com/a/b"):
        path.write_text(path.read_text().replace("https://github.com/you/retinue.git", bad, 1))
        with pytest.raises(SystemExit):
            PlatformConfig.load(path)
        path.write_text(path.read_text().replace(bad, "https://github.com/you/retinue.git", 1))


# --- the room's clone ---------------------------------------------------------------------------------------------

def test_the_lane_host_clones_the_mirror_once_with_the_owners_name_and_its_origin_the_mirror(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    assert git("remote", "get-url", "origin", cwd=clone).strip() == str(tmp_path / "mirrors" / "r.git")
    assert git("config", "user.email", cwd=clone).strip() == "o@example.com"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "main"
    assert "/out/" in (clone / ".git" / "info" / "exclude").read_text()
    assert not list((clone / ".git" / "hooks").glob("*")) if (clone / ".git" / "hooks").exists() else True
    # Made once: what stands under the name is the session's.
    (clone / "mine.txt").write_text("x")
    assert not ensure_clone(tmp_path / "work", tmp_path / "mirrors", "code", "r", AUTHOR)
    assert (clone / "mine.txt").exists()
    # `git pull` in the room needs no credential: it reads the mirror.
    commit_on_remote(tmp_path, remote, "NEWS.md", "news\n", "news")
    c.fetch("r")
    git("pull", "-q", "--ff-only", cwd=clone)
    assert (clone / "NEWS.md").exists()
    # No mirror: nothing made; a folder of clones that is a link: refused.
    assert not ensure_clone(tmp_path / "work", tmp_path / "mirrors", "code", "other", AUTHOR)
    (tmp_path / "work2").mkdir()
    (tmp_path / "work2" / "code").symlink_to(tmp_path / "work" / "code")
    with pytest.raises(LaneRefused):
        ensure_clone(tmp_path / "work2", tmp_path / "mirrors", "code", "r", AUTHOR)
    with pytest.raises(LaneRefused):
        check_code({"root": "../x", "repos": ["r"], "author": AUTHOR})
    with pytest.raises(LaneRefused):
        check_code({"root": "code", "repos": ["../../etc"], "author": AUTHOR})


def commit_on_remote(tmp: Path, remote: Remote, path: str, text: str, message: str) -> str:
    other = tmp / f"other-{len(list(tmp.glob('other-*')))}"
    git("clone", "-q", str(remote.bare), str(other))
    sha = commit(other, path, text, message)
    git("push", "-q", "origin", "HEAD:main", cwd=other)
    return sha


# --- a look and a push --------------------------------------------------------------------------------------------

def test_a_look_names_the_commits_the_files_and_what_is_not_committed(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "fix-login", cwd=clone)
    one = committed(clone, "src/login.py", "print('a')\n", "Fix login")
    two = committed(clone, "src/util.py", "x = 1\n", "Add util")
    (clone / "README.md").write_text("changed, not committed\n")
    (clone / "new.txt").write_text("untracked")
    (clone / "debug.log").write_text("ignored")
    (clone / "build").mkdir()
    (clone / "build" / "out.bin").write_text("ignored folder")
    (clone / "notes").mkdir()
    for n in range(5):
        (clone / "notes" / f"{n}.md").write_text("an untracked folder, named once")
    (clone / "out").mkdir()
    (clone / "out" / "report.pdf").write_text("for the owner")

    look = c.look("r", "refs/retinue/cards/a1")
    assert (look.branch, look.sha, look.base, look.ahead, look.unpushed) == ("fix-login", two, "main", 2, 2)
    assert [x["sha"] for x in look.commits] == [two, one] and look.commits[0]["subject"] == "Add util"
    assert {f["path"]: (f["status"], f["add"], f["del"]) for f in look.files} == {
        "src/login.py": ("A", 1, 0), "src/util.py": ("A", 1, 0)}
    assert look.dirty == ["README.md", "new.txt", "notes/"] and not look.partial and not look.workflows
    assert "+print('a')" in c.diff("r", look.sha, 100_000)

    pushed = c.push("r", "refs/retinue/cards/a1", two, "retinue/s1-fix-login", title="Fix login", body="b")
    assert pushed.ok and remote.sha("retinue/s1-fix-login") == two and remote.sha("main") != two
    assert c.remote_sha("r", "retinue/s1-fix-login") == two
    c.fetch("r")
    assert c.count("r", "refs/retinue/heads/r")[2] == 0, "every commit is on a branch of the remote now"


def test_never_the_default_branch_never_forced_never_a_tag(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "feature", cwd=clone)
    sha = committed(clone, "a.txt", "a\n", "A")
    git("tag", "v9", cwd=clone)
    c.look("r", "refs/retinue/cards/x")
    for branch in ("main", "MAIN", "master", "HEAD", "-f", "a..b", "refs/heads/x", "x.lock", "a b", "@{1}"):
        done = c.push("r", "refs/retinue/cards/x", sha, branch)
        assert not done.ok and done.said.startswith("Не пушу"), branch
    assert remote.sha("main") != sha

    # The remote's branch moved elsewhere: a push is refused, never forced over it.
    other = commit_on_remote(tmp_path, remote, "b.txt", "b\n", "B")
    git(f"--git-dir={remote.bare}", "update-ref", "refs/heads/taken", other)
    done = c.push("r", "refs/retinue/cards/x", sha, "taken")
    assert not done.ok and "уже другая" in done.said and remote.sha("taken") == other
    assert not subprocess.run(["git", f"--git-dir={remote.bare}", "rev-parse", "--verify", "--quiet", "refs/tags/v9"],
                              capture_output=True).stdout


# --- the room's repository is hostile -----------------------------------------------------------------------------

def test_a_hostile_config_and_hooks_cannot_redirect_the_push_nor_run_anything(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    evil = tmp_path / "evil.git"
    git("init", "-q", "--bare", str(evil))
    git("checkout", "-qb", "feature", cwd=clone)
    sha = committed(clone, "a.txt", "a\n", "A")
    # Then the session makes its repository hostile: hooks, and a config that points everything elsewhere.
    marker = tmp_path / "ran"
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("pre-push", "post-checkout", "reference-transaction", "pre-auto-gc", "post-update", "pre-receive",
                 "update", "proc-receive", "push-to-checkout", "fsmonitor-watchman"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}-{hook}\n")
        (hooks / hook).chmod(0o755)
    with (clone / ".git" / "config").open("a") as config:
        config.write(f"""
[url "{evil}"]
\tinsteadOf = {remote.url}
\tpushInsteadOf = {remote.url}
\tinsteadOf = {remote.server.base}
[remote "origin"]
\tpushurl = {evil}
[core]
\thooksPath = {hooks}
\tfsmonitor = touch {marker}-fsmonitor
\tsshCommand = touch {marker}-ssh
\talternateRefsCommand = touch {marker}-altrefs
[uploadpack]
\tpackObjectsHook = touch {marker}-packhook
[credential]
\thelper = !touch {marker}-credential
[include]
\tpath = {tmp_path / 'included'}
[extensions]
\tpartialClone = evil
[remote "evil"]
\turl = ext::sh -c touch% {marker}-ext
\tpromisor = true
""")
    (tmp_path / "included").write_text(f"[core]\n\tfsmonitor = touch {marker}-included\n")

    look = c.look("r", "refs/retinue/cards/h")
    assert look.sha == sha
    done = c.push("r", "refs/retinue/cards/h", sha, "retinue/h")
    assert done.ok and remote.sha("retinue/h") == sha, "the push went to platform.yaml's URL"
    assert git(f"--git-dir={evil}", "for-each-ref").strip() == "", "nothing reached the insteadOf/pushurl remote"
    assert sorted(p.name for p in tmp_path.glob("ran*")) == [], "no hook, fsmonitor, helper or command of the room ran"


@pytest.mark.parametrize("layout", ["alternates", "http-alternates", "commondir", "gitfile", "objects-link",
                                    "pack-link", "git-link", "repo-link", "detached", "ref-link", "refs-link"])
def test_a_room_repository_that_could_bring_other_objects_in_is_refused(tmp_path, remote, layout):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "feature", cwd=clone)
    committed(clone, "a.txt", "a\n", "A")
    dot = clone / ".git"
    elsewhere = tmp_path / "platform-secret.git"
    git("init", "-q", "--bare", str(elsewhere))
    if layout == "alternates":
        (dot / "objects" / "info" / "alternates").write_text(f"{elsewhere}/objects\n")
    elif layout == "http-alternates":
        (dot / "objects" / "info" / "http-alternates").write_text("https://evil.example/objects\n")
    elif layout == "commondir":
        (dot / "commondir").write_text(str(elsewhere))
    elif layout == "gitfile":
        os.rename(dot, tmp_path / "moved.git")
        (clone / ".git").write_text(f"gitdir: {tmp_path / 'moved.git'}\n")
    elif layout == "objects-link":
        os.rename(dot / "objects", tmp_path / "objects")
        (dot / "objects").symlink_to(tmp_path / "objects")
    elif layout == "pack-link":
        (dot / "objects" / "pack" / "extra").symlink_to(elsewhere / "objects")
    elif layout == "git-link":
        os.rename(dot, tmp_path / "moved.git")
        (clone / ".git").symlink_to(tmp_path / "moved.git")
    elif layout == "repo-link":
        os.rename(clone, tmp_path / "moved")
        clone.symlink_to(tmp_path / "moved")
    elif layout == "detached":
        git("checkout", "-q", "--detach", cwd=clone)
    elif layout == "ref-link":
        (dot / "refs" / "heads" / "x").symlink_to("/etc/passwd")
    elif layout == "refs-link":
        git("pack-refs", "--all", cwd=clone)
        os.rename(dot / "refs", tmp_path / "refs")
        (dot / "refs").symlink_to(tmp_path / "refs")
    with pytest.raises(Refused):
        c.look("r", "refs/retinue/cards/z")
    outbox = tmp_path / "platform" / "code" / "r.git"
    assert not (outbox / "HEAD").exists() or not git(f"--git-dir={outbox}", "for-each-ref", "refs/retinue/cards/z")


def test_workflows_are_named_and_a_big_tree_is_bounded(tmp_path, remote, monkeypatch):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "ci", cwd=clone)
    committed(clone, ".github/workflows/ci.yml", "on: push\n", "CI")
    for n in range(3):
        (clone / f"big{n}.txt").write_text("x" * 100)
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "big", cwd=clone)
    for n in range(3):
        (clone / f"big{n}.txt").write_text("y" * 100)
    monkeypatch.setattr(code, "MAX_HASHED", 150)
    look = c.look("r", "refs/retinue/cards/w")
    assert look.workflows == [".github/workflows/ci.yml"]
    assert look.partial and len([p for p in look.dirty if p.startswith("big")]) == 1
    monkeypatch.setattr(code, "MAX_ROOM_ENTRIES", 3)
    with pytest.raises(Refused):
        c.look("r", "refs/retinue/cards/w")


# --- the pull request ---------------------------------------------------------------------------------------------

def test_a_pull_request_is_opened_or_the_open_one_found_with_the_token_in_its_header_only(tmp_path, remote):
    async def go():
        seen = []

        async def pulls(request: web.Request) -> web.Response:
            seen.append((request.method, request.path, dict(request.query), request.headers.get("Authorization"),
                         await request.json() if request.method == "POST" else None))
            if request.method == "POST":
                if len([s for s in seen if s[0] == "POST"]) == 1:
                    return web.json_response({"html_url": "https://github.com/o/r/pull/7"}, status=201)
                return web.json_response({"message": "Validation Failed",
                                          "errors": [{"message": "A pull request already exists for o:b."}]},
                                         status=422)
            return web.json_response([{"html_url": "https://github.com/o/r/pull/7"}])

        app = web.Application()
        app.add_routes([web.post("/repos/o/r/pulls", pulls), web.get("/repos/o/r/pulls", pulls)])
        server = TestServer(app)
        await server.start_server()
        try:
            c = repos(tmp_path, "https://github.com/o/r.git", api=str(server.make_url("")).rstrip("/"))
            c._defaults["r"] = "main"
            first = await c.open_pr("r", "retinue/b", "Fix", "body")
            again = await c.open_pr("r", "retinue/b", "Fix", "body")
        finally:
            await server.close()
        assert first == again == ("https://github.com/o/r/pull/7", "")
        assert seen[0][3] == f"Bearer {TOKEN}" and seen[0][4] == {"title": "Fix", "body": "body", "head": "retinue/b",
                                                                  "base": "main"}
        assert seen[-1][2] == {"head": "o:retinue/b", "state": "open"}
        assert all(TOKEN not in json.dumps([s[1], s[2], s[4]]) for s in seen)

    import asyncio
    asyncio.run(go())


def test_the_token_script_reads_it_without_echo_and_prints_only_its_name_and_length(tmp_path):
    script = Path(__file__).resolve().parents[1] / "deploy" / "code" / "set-git-token.sh"
    env = {"PATH": os.environ["PATH"], "RETINUE_ROOT": str(tmp_path)}
    for token in ("github_pat_first", TOKEN):
        done = subprocess.run(["bash", str(script), "you"], input=token + "\n", capture_output=True, text=True,
                              env=env)
        assert done.returncode == 0 and token not in done.stdout + done.stderr
        assert "RETINUE_GIT_TOKEN_YOU" in done.stdout and f"({len(token)} знаков)" in done.stdout
    tokens = tmp_path / "code" / "tokens.env"
    assert tokens.read_text() == f"RETINUE_GIT_TOKEN_YOU={TOKEN}\n", "one line, replaced"
    assert oct(tokens.stat().st_mode & 0o777) == "0o600"
    for bad in ("", "two words", "a=b"):
        assert subprocess.run(["bash", str(script), "you"], input=bad + "\n", capture_output=True, text=True,
                              env=env).returncode == 1
    assert subprocess.run(["bash", str(script), "../x"], input="t\n", capture_output=True, text=True, env=env).returncode == 2
    assert tokens.read_text() == f"RETINUE_GIT_TOKEN_YOU={TOKEN}\n"


# --- after the review: everything that leaves is on the card, bounded, through the proxy -------------------------

class RecordingProxy:
    """An HTTP proxy that forwards plain-HTTP requests (absolute URIs) and keeps what passed: the platform's git must
    reach the remote only through it."""

    def __init__(self) -> None:
        import http.client
        import urllib.parse

        self.seen: list[tuple[str, str]] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _forward(self) -> None:
                proxy.seen.append((self.command, self.path))
                url = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                conn = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "host")}
                conn.request(self.command, url.path + (f"?{url.query}" if url.query else ""), body=body,
                             headers=headers)
                res = conn.getresponse()
                data = res.read()
                self.send_response(res.status)
                for k, v in res.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _forward

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_every_remote_call_goes_through_the_platforms_proxy(tmp_path, remote):
    proxy = RecordingProxy()
    try:
        c = repos(tmp_path, remote.url)
        c.cfg.proxy = proxy.url
        assert c.fetch("r")
        clone = room(tmp_path, c)
        git("checkout", "-qb", "p", cwd=clone)
        sha = committed(clone, "p.txt", "p\n", "P")
        c.look("r", "refs/retinue/cards/p")
        assert c.push("r", "refs/retinue/cards/p", sha, "retinue/p").ok and c.remote_sha("r", "retinue/p") == sha
    finally:
        proxy.close()
    assert {path.split("?")[0].rsplit("/", 1)[-1] for _, path in proxy.seen} >= {"refs", "git-upload-pack",
                                                                                 "git-receive-pack"}
    assert all(path.startswith(remote.server.base) for _, path in proxy.seen)
    assert "proxy = " + proxy.url in (tmp_path / "platform" / "code" / "platform.gitconfig").read_text()


def test_every_commit_that_leaves_is_shown_whole_even_when_the_net_diff_hides_it(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "sneaky", cwd=clone)
    added = committed(clone, "secret.txt", "the Base's words\n", "Add notes")
    (clone / "secret.txt").unlink()
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "Revert notes\n\nThe body says more.\n@someone Closes #1", cwd=clone)
    empty = git("commit", "-q", "--allow-empty", "-m", "Empty\n\nonly a message carries this", cwd=clone)
    del empty
    look = c.look("r", "refs/retinue/cards/s")
    assert look.files == [], "the net diff is empty…"
    assert [x["subject"] for x in look.commits] == ["Empty", "Revert notes", "Add notes"], "…but every commit is shown"
    assert look.commits[2]["sha"] == added and look.commits[2]["files"] == [{"path": "secret.txt", "status": "A"}]
    assert look.commits[1]["files"] == [{"path": "secret.txt", "status": "D"}]
    assert "only a message carries this" in look.commits[0]["message"] and look.unpushed == 3
    assert look.objects >= 5 and look.bytes > 0
    text = c.diff("r", look.sha, 100_000)
    assert "the Base's words" in text and "only a message carries this" in text, "the diff is commit by commit"


def test_a_new_root_or_an_unrelated_history_merged_in_is_refused(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-q", "--orphan", "other", cwd=clone)
    git("rm", "-rqf", ".", cwd=clone)
    committed(clone, "elsewhere.txt", "another history\n", "Orphan")
    git("checkout", "-qb", "merged", "main", cwd=clone)
    git("merge", "-q", "-s", "ours", "--allow-unrelated-histories", "-m", "Merge", "other", cwd=clone)
    with pytest.raises(Refused, match="корневой"):
        c.look("r", "refs/retinue/cards/o")
    # A merge of the remote's own branch is ordinary: shown, with both parents.
    git("checkout", "-qb", "ok", "main", cwd=clone)
    committed(clone, "x.txt", "x\n", "X")
    commit_on_remote(tmp_path, remote, "y.txt", "y\n", "Y")
    c.fetch("r")
    git("fetch", "-q", "origin", cwd=clone)
    git("merge", "-q", "-m", "Merge main", "origin/main", cwd=clone)
    look = c.look("r", "refs/retinue/cards/m")
    assert [len(x["parents"]) for x in look.commits] == [2, 1]


def test_an_object_no_shown_change_accounts_for_is_refused(tmp_path, remote, monkeypatch):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "f", cwd=clone)
    committed(clone, "f.txt", "f\n", "F")
    real = CodeRepos._git

    def blind(self, *args, **kwargs):  # a change the card would not show: its objects are not accounted
        done = real(self, *args, **kwargs)
        if "diff-tree" in args:
            done.stdout = ""
        return done

    monkeypatch.setattr(CodeRepos, "_git", blind)
    with pytest.raises(Refused, match="объектов, которых нет"):
        c.look("r", "refs/retinue/cards/f")


def test_the_rooms_objects_are_copied_checked_never_read_in_place(tmp_path, remote, monkeypatch):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "g", cwd=clone)
    sha = committed(clone, "g.txt", "g\n", "G")
    store = tmp_path / "platform" / "code" / "r.room"
    c.look("r", "refs/retinue/cards/g")
    assert not [p for p in store.rglob("*") if p.is_symlink()], "nothing in the copy points into the room"
    assert (store / "objects" / sha[:2] / sha[2:]).is_file()
    assert (store / "objects" / "info" / "alternates").read_text().strip() == str(tmp_path / "mirrors" / "r.git" /
                                                                                  "objects")
    assert not list((store / "objects" / "pack").glob("pack-*")), "the clone's own pack is all in the mirror"
    # After `git gc` the new commit lives in a pack of the room's: copied, once.
    git("gc", "-q", "--prune=now", cwd=clone)
    c.look("r", "refs/retinue/cards/g")
    assert len(list((store / "objects" / "pack").glob("pack-*.pack"))) == 1
    monkeypatch.setattr(code, "MAX_PACK_COPY", 10)
    for p in (store / "objects" / "pack").glob("pack-*"):
        p.unlink()
    (store / "packs.json").unlink()
    with pytest.raises(Refused, match="больше"):
        c.look("r", "refs/retinue/cards/g")


def test_a_loose_object_whose_bytes_do_not_match_its_name_is_refused(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "h", cwd=clone)
    sha = committed(clone, "h.txt", "h\n", "H")
    loose = clone / ".git" / "objects" / sha[:2] / sha[2:]
    other = git("hash-object", "-w", "--stdin", cwd=clone)  # any other object's bytes under the commit's name
    del other
    data = next(p for p in (clone / ".git" / "objects").glob("??/*") if p.name != sha[2:]).read_bytes()
    loose.chmod(0o644)
    loose.write_bytes(data)
    with pytest.raises(Refused, match="не сходится с именем"):
        c.look("r", "refs/retinue/cards/h")


def test_the_sessions_ignore_files_cannot_stall_a_look(tmp_path, remote):
    import time as time_

    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "i", cwd=clone)
    committed(clone, ".gitignore", "*a" * 15 + "*b\n" + "*" * 400 + "x\n" + "a*" * 30 + "\n" + "deep/**/**/*a*a*a*a*c\n",
              "ignore")
    (clone / ("a" * 200)).write_text("x")
    (clone / "deep").mkdir()
    (clone / "deep" / ("a" * 200)).write_text("x")
    started = time_.monotonic()
    look = c.look("r", "refs/retinue/cards/i")
    assert time_.monotonic() - started < 10
    assert look.partial, "a pattern with too many stars is not used, and the look says so"


def test_gits_output_is_bounded_and_a_card_shows_names_as_they_are(tmp_path, remote):
    from retinue.topush import inert, shown

    c = repos(tmp_path, remote.url)
    c.fetch("r")
    with pytest.raises(code.Unavailable, match="больше"):
        c._git(f"--git-dir={c.mirror('r')}", "cat-file", "-p", "HEAD", cap=10)
    assert shown("evil\nline‮exe.txt", 200) == "evil⟦U+000A⟧line⟦U+202E⟧exe.txt"
    assert shown("a" * 500, 50).endswith("…") and len(shown("a" * 500, 50)) == 50
    assert inert("Fix `x` @owner Closes #1 ![i](https://e.x/p.png)") == \
        "Fix 'x' @owner Closes #1 ![i](https://e.x/p.png)", "inside a code span: no mention, no link, no picture"


def test_the_token_leaves_the_platforms_environment_once_read(tmp_path, monkeypatch):
    path = tmp_path / "platform.yaml"
    path.write_text(
        "lanes:\n  private: {url: 'http://l:9600', hook_url: 'http://p/hook', work_mount: /lanes/private}\n"
        "tracks:\n  - {name: code, lane: private, kind: private, cwd: ''}\n"
        "code:\n  author: 'A B <a@b.c>'\n  proxy: 'http://platform-egress:3128'\n  repos:\n"
        "    - {name: a, url: 'https://github.com/o/a.git', token: o}\n"
        "    - {name: b, url: 'https://github.com/o/b.git', token: o}\n")
    for name in ("RETINUE_LANE_TOKEN_PRIVATE", "RETINUE_PLATFORM_TOKEN", "RETINUE_HOOK_SECRET",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RETINUE_GIT_TOKEN_O", TOKEN)
    cfg = PlatformConfig.load(path)
    assert [r.secret for r in cfg.code.repos] == [TOKEN, TOKEN], "one owner's token serves both of its repositories"
    assert "RETINUE_GIT_TOKEN_O" not in os.environ


# --- the second review: raw commits, streamed packs, the helper's host ------------------------------------------

def crafted(clone: Path, extra: bytes, message: bytes = b"Crafted\n") -> str:
    """A commit object written by hand on top of HEAD, with `extra` header lines; the branch moved to it."""
    head = git("rev-parse", "HEAD", cwd=clone).strip()
    tree = git("rev-parse", "HEAD^{tree}", cwd=clone).strip()
    raw = (f"tree {tree}\nparent {head}\nauthor A <a@e.x> 1700000000 +0000\ncommitter A <a@e.x> 1700000000 +0000\n"
           .encode() + extra + b"\n" + message)
    sha = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=clone, input=raw,
                         capture_output=True, check=True).stdout.decode().strip()
    git("update-ref", "HEAD", sha, cwd=clone)
    return sha


@pytest.mark.parametrize("extra", [b"x-foo hidden data\n", b"gpgsig -----BEGIN PGP SIGNATURE-----\n abc\n -----END\n",
                                   b"mergetag object 0000000000000000000000000000000000000000\n",
                                   b"encoding ISO-8859-1\n"])
def test_a_commit_header_the_card_cannot_show_is_refused(tmp_path, remote, extra):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "hdr", cwd=clone)
    committed(clone, "a.txt", "a\n", "A")
    crafted(clone, extra)
    with pytest.raises(Refused, match="заголовок|кодировка"):
        c.look("r", "refs/retinue/cards/x")


def test_messages_are_read_from_the_object_whole_and_a_separator_in_one_spoofs_nothing(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "msg", cwd=clone)
    committed(clone, "a.txt", "a\n", "A")
    sha = crafted(clone, b"encoding UTF-8\n", "Safe\x1e\x1fsha: fake\n\nтайный хвост⁦\n".encode())
    look = c.look("r", "refs/retinue/cards/m")
    assert look.commits[0]["sha"] == sha and look.commits[0]["message"] == "Safe\x1e\x1fsha: fake\n\nтайный хвост⁦"
    assert look.commits[0]["author"].startswith("A <a@e.x>, 2023-11-14 22:13")
    text = c.diff("r", look.sha, 100_000)
    assert "Safe⟦U+001E⟧⟦U+001F⟧sha: fake" in text and "тайный хвост⟦U+2066⟧" in text and "Author: A <a@e.x>" in text
    crafted(clone, b"", b"x" * (code.MAX_MESSAGE + 10))
    with pytest.raises(Refused, match="сообщение больше"):
        c.look("r", "refs/retinue/cards/m")


def test_a_pack_copy_is_retried_after_a_crash_once_and_refused_for_good_only_for_the_rooms_fault(tmp_path, remote,
                                                                                                monkeypatch):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "p", cwd=clone)
    committed(clone, "p.txt", "p\n", "P")
    git("gc", "-q", "--prune=now", cwd=clone)
    store = tmp_path / "platform" / "code" / "r.room"
    state = store / "packs.json"
    real = code._copy

    def broken(error):
        def copy(parent, name, into, size):
            if name.endswith(".pack"):
                raise error
            return real(parent, name, into, size)
        return copy

    def ours() -> dict:
        return {k: v for k, v in json.loads(state.read_text()).items() if v["state"] != "mirror"}

    # The platform's own trouble (the disk, git): nothing kept, the next look copies it.
    monkeypatch.setattr(code, "_copy", broken(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        c.look("r", "refs/retinue/cards/p")
    assert ours() == {} and not [p for p in (store / "objects" / "pack").iterdir() if p.name.startswith(".")]
    monkeypatch.setattr(code, "_copy", real)
    assert c.look("r", "refs/retinue/cards/p").commits[0]["subject"] == "P"
    [(stem, verdict)] = ours().items()
    assert verdict["state"] == "copied"

    # A crash in the middle (`copying` left): tried once more after the restart, then refused with the reason.
    for tries, ok in ((1, True), (2, False)):
        state.write_text(json.dumps({**json.loads(state.read_text()), stem: {**verdict, "state": "copying",
                                                                             "tries": tries}}))
        if ok:
            assert c.look("r", "refs/retinue/cards/p").commits[0]["subject"] == "P"
        else:
            with pytest.raises(Refused, match="не скопировался 2 раза подряд"):
                c.look("r", "refs/retinue/cards/p")

    # The room's fault (the pack moved while read): refused for good, with its reason.
    state.write_text(json.dumps({k: v for k, v in json.loads(state.read_text()).items() if k != stem}))
    monkeypatch.setattr(code, "_copy", broken(Refused(".git/objects/pack/pack-…: изменился, пока читался")))
    with pytest.raises(Refused, match="изменился"):
        c.look("r", "refs/retinue/cards/p")
    monkeypatch.setattr(code, "_copy", real)
    assert ours()[stem]["state"] == "failed"
    with pytest.raises(Refused, match="изменился, пока читался"):
        c.look("r", "refs/retinue/cards/p")


def test_a_second_author_and_committer_after_the_first_pair_is_refused(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "two", cwd=clone)
    committed(clone, "a.txt", "a\n", "A")
    head = git("rev-parse", "HEAD", cwd=clone).strip()
    tree = git("rev-parse", "HEAD^{tree}", cwd=clone).strip()
    raw = (f"tree {tree}\nparent {head}\nauthor SECRET the Base's words <s@e.x> 1700000000 +0000\n"
           f"committer SECRET more of them <s@e.x> 1700000000 +0000\nauthor Benign <b@e.x> 1700000000 +0000\n"
           f"committer Benign <b@e.x> 1700000000 +0000\n\nLooks fine\n").encode()
    sha = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=clone, input=raw,
                         capture_output=True, check=True).stdout.decode().strip()
    assert subprocess.run(["git", "fsck", "--strict"], cwd=clone, capture_output=True).returncode == 0, \
        "git's own check lets it through"
    git("update-ref", "HEAD", sha, cwd=clone)
    with pytest.raises(Refused, match="повторён"):
        c.look("r", "refs/retinue/cards/two")
    # The order itself: a parent after the author, two trees, no author, two encodings — refused the same way.
    from retinue.code import _commit
    one = "a" * 40
    for bad in (f"tree {one}\nauthor A <a@e.x> 1 +0000\nparent {one}\ncommitter A <a@e.x> 1 +0000\n\nm",
                f"tree {one}\ntree {one}\nauthor A <a@e.x> 1 +0000\ncommitter A <a@e.x> 1 +0000\n\nm",
                f"tree {one}\ncommitter A <a@e.x> 1 +0000\n\nm",
                f"tree {one}\nauthor A <a@e.x> 1 +0000\ncommitter A <a@e.x> 1 +0000\nencoding UTF-8\n"
                f"encoding UTF-8\n\nm"):
        with pytest.raises(Refused):
            _commit(one, bad.encode())
    good = _commit(one, f"tree {one}\nparent {one}\nauthor A <a@e.x> 1 +0000\ncommitter B <b@e.x> 1 +0000\n"
                        f"encoding utf-8\n\nm\n".encode())
    assert (good.parents, good.author[:9], good.committer[:9], good.message) == ([one], "A <a@e.x>", "B <b@e.x>", "m")


def test_a_folder_of_many_entries_stops_at_the_deadline(tmp_path, remote, monkeypatch):
    c = repos(tmp_path, remote.url)
    c.fetch("r")
    clone = room(tmp_path, c)
    git("checkout", "-qb", "d", cwd=clone)
    committed(clone, "keep/a.txt", "a\n", "A")
    for n in range(300):
        (clone / "keep" / f"x{n}.log").write_text("ignored")
    monkeypatch.setattr(code, "MAX_FOLDER_ENTRIES", 50)
    assert c.look("r", "refs/retinue/cards/d").partial


def test_the_helper_answers_the_configured_host_alone(tmp_path, remote):
    c = repos(tmp_path, remote.url)
    repo = c.repos["r"]
    host = remote.url.split("://")[1].split("/")[0]
    asks = {"same": f"protocol=http\nhost={host}\n\n", "other": "protocol=http\nhost=evil.example\n\n",
            "proxy": "protocol=http\nhost=platform-egress:3128\n\n", "https": f"protocol=https\nhost={host}\n\n"}
    got = {k: c._git("credential", "fill", stdin=v.encode(), auth=repo, check=False, timeout=20).stdout
           for k, v in asks.items()}
    assert f"password={TOKEN}" in got["same"]
    assert all(TOKEN not in got[k] for k in ("other", "proxy", "https"))



def test_the_helper_reads_a_pipe_numbered_past_nine(tmp_path, remote):
    # Debian's /bin/sh is dash, and dash takes no fd past 9 in `<&$n`: the helper reads the pipe as /dev/fd/<n>.
    c = repos(tmp_path, remote.url)
    host = remote.url.split("://")[1].split("/")[0]
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(12)]
    try:
        got = c._git("credential", "fill", stdin=f"protocol=http\nhost={host}\n\n".encode(), auth=c.repos["r"],
                     check=False, timeout=20).stdout
    finally:
        for fd in held:
            os.close(fd)
    assert f"password={TOKEN}" in got
    assert "<&$" not in c._config.read_text()

def test_a_code_section_with_repositories_needs_the_proxy(tmp_path, monkeypatch):
    path = tmp_path / "platform.yaml"
    path.write_text(
        "lanes:\n  private: {url: 'http://l:9600', hook_url: 'http://p/hook', work_mount: /lanes/private}\n"
        "tracks:\n  - {name: code, lane: private, kind: private, cwd: ''}\n"
        "code:\n  author: 'A B <a@b.c>'\n  repos:\n    - {name: a, url: 'https://github.com/o/a.git', token: o}\n")
    for name in ("RETINUE_LANE_TOKEN_PRIVATE", "RETINUE_PLATFORM_TOKEN", "RETINUE_HOOK_SECRET",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "x")
    with pytest.raises(SystemExit, match="proxy is required"):
        PlatformConfig.load(path)


def test_spaces_and_combining_marks_are_shown_as_codes():
    from retinue.code import shown

    assert shown("a b ćd e", 100) == "a⟦U+00A0⟧b⟦U+2003⟧c⟦U+0301⟧d e"


def test_an_author_longer_than_the_card_shows_is_refused():
    from retinue.code import MAX_IDENTITY, _commit
    one = "a" * 40
    long = "A" + "x" * MAX_IDENTITY
    for who in ("author", "committer"):
        lines = {"author": "A <a@e.x> 1 +0000", "committer": "A <a@e.x> 1 +0000"}
        lines[who] = f"{long} <a@e.x> 1 +0000"
        raw = f"tree {one}\nauthor {lines['author']}\ncommitter {lines['committer']}\n\nm".encode()
        with pytest.raises(Refused, match="длиннее"):
            _commit("b" * 40, raw)
    assert _commit("b" * 40, f"tree {one}\nauthor A <a@e.x> 1 +0000\ncommitter A <a@e.x> 1 +0000\n\nm".encode())
