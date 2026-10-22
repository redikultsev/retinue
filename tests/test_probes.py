"""Probes: the pinned lane CLI, unmodified, against a local fake Messages API. They pin the CLI behaviour the platform
rests on — defer, resume, rules, marker hooks, settings that do not load — so a CLI bump that changes it fails here.

Run them with the pinned binary (`bash deploy/lanes/fetch-cli.sh <dir>`, then `RETINUE_LANE_CLI=<dir>/claude
pytest -m probe`); without it they are skipped. No credential is used except by the login probe, which needs
`CLAUDE_CODE_OAUTH_TOKEN`. The CLI's environment is `cliengine.env` with two test-only substitutions that never go
through `check_env`: the fake API's key and address instead of the token, and the env scrub off — it needs
bubblewrap, which the lane image has and a developer's machine may not.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from fakeapi import FakeApi
from retinue import cliengine, lane_host
from retinue.config import LaneHostConfig, LaneRef, PlatformConfig, Track
from retinue.lane_host import LaneHost
from retinue.platformd import Platform
from retinue.sessions import PlatformStore


def _pinned() -> bool:
    path = os.environ.get("RETINUE_LANE_CLI")
    if not path or not os.access(path, os.X_OK):
        return False
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return cliengine.version_ok(done.stdout)


pytestmark = [pytest.mark.probe,
              pytest.mark.skipif(not _pinned(), reason=f"RETINUE_LANE_CLI is not the pinned {cliengine.CLI_VERSION}")]

HOOK_TOKEN = "probe-hook-token"
TIMEOUT_S = 90


@dataclass
class Run:
    rc: int
    lines: list[dict]
    stderr: str

    @property
    def result(self) -> dict:
        return next(line for line in self.lines if line.get("type") == "result")

    @property
    def init(self) -> dict:
        return next(line for line in self.lines if line.get("type") == "system" and line.get("subtype") == "init")

    def tool_use_ids(self) -> list[str]:
        return [block["id"] for line in self.lines if line.get("type") == "assistant"
                for block in line["message"]["content"] if block.get("type") == "tool_use"]


class Lane:
    """One working directory, home and config directory, run again and again like a lane runs one session."""

    def __init__(self, root: Path, api: FakeApi) -> None:
        self.api = api
        self.home, self.cwd, self.config = root / "home", root / "work", root / "home" / "config"
        for path in (self.home, self.cwd, self.config):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = root / "settings.json"

    def env(self) -> dict[str, str]:
        env = cliengine.env("", str(self.home), str(self.config), "", "", HOOK_TOKEN)
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "NO_PROXY"):
            del env[key]
        return {**env, cliengine.SCRUB: "0", "ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": self.api.url}

    def run(self, prompt: str | None, *, resume: str | None = None, allowed=(), disallowed=(), marks=(),
            hook_url: str | None = None) -> Run:
        self.settings.write_text(json.dumps(cliengine.settings(hook_url or self.api.hook_url, 30, marks)))
        argv = cliengine.argv(str(self.settings), list(allowed), list(disallowed), resume)
        done = subprocess.run(argv, cwd=self.cwd, env=self.env(), capture_output=True, timeout=TIMEOUT_S,
                              input=cliengine.user_line(prompt) if prompt is not None else b"")
        lines = [json.loads(line) for line in done.stdout.decode().splitlines() if line.startswith("{")]
        return Run(done.returncode, lines, done.stderr.decode())


@pytest.fixture
def api():
    with FakeApi() as fake:
        yield fake


@pytest.fixture
def lane(tmp_path, api):
    return Lane(tmp_path, api)


def bash(command: str) -> tuple[str, dict]:
    return ("Bash", {"command": command, "description": "probe"})


def test_the_argv_cliengine_builds_is_the_one_the_pinned_cli_runs(lane, api):
    """The init line names the pinned version; the hook token reaches the hook through `$RETINUE_HOOK_TOKEN`."""
    api.tools, api.mode = [bash("true")], "defer"
    run = lane.run("go")
    assert run.rc == 0, run.stderr
    assert run.init["claude_code_version"] == cliengine.CLI_VERSION
    assert run.init["permissionMode"] == "default", "manual is the CLI's alias of default"
    assert cliengine.UUID4.fullmatch(run.result["session_id"]), "a session id --resume accepts in check_argv"
    assert {h.authorization for h in api.hooks} == {f"Bearer {HOOK_TOKEN}"}


def test_a_deferred_call_runs_exactly_once_on_resume_and_never_again(lane, api):
    marker = lane.cwd.parent / "ran"
    api.tools, api.mode = [bash(f"echo ran >> {marker}")], "defer"
    first = lane.run("go")
    assert first.rc == 0, first.stderr
    assert first.result["stop_reason"] == "tool_deferred"
    deferred = first.result["deferred_tool_use"]
    assert deferred["name"] == "Bash" and deferred["id"] == first.tool_use_ids()[-1]
    assert not marker.exists()

    api.mode, api.hooks = "allow", []
    session = first.result["session_id"]
    resumed = lane.run(None, resume=session)
    assert [h.tool_use_id for h in api.events("PreToolUse")] == [deferred["id"]], "the same call is asked again"
    assert resumed.result["stop_reason"] != "tool_deferred"
    assert marker.read_text() == "ran\n"

    lane.run(None, resume=session)
    assert marker.read_text() == "ran\n", "a further resume does not run it again"


def test_a_call_is_blocked_when_the_hook_endpoint_is_down_even_under_an_allow_rule(lane, api):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    api.tools = [bash("touch marker")]
    run = lane.run("go", allowed=["Bash(touch:*)"], hook_url=f"http://127.0.0.1:{closed}/hook")
    assert run.tool_use_ids() and not (lane.cwd / "marker").exists(), run.lines


def test_settings_the_agent_writes_have_no_effect_on_the_next_run(lane, api):
    evil = {"permissions": {"allow": ["Bash(touch:*)"], "defaultMode": "bypassPermissions"},
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f"touch {lane.cwd.parent / 'evil-hook'}"}]}]}}
    for path in (lane.cwd / ".claude" / "settings.json", lane.cwd / ".claude" / "settings.local.json",
                 lane.home / ".claude" / "settings.json", lane.config / "settings.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evil))
    api.tools, api.mode = [bash("touch marker")], "none"
    run = lane.run("go")
    assert run.tool_use_ids() and not (lane.cwd / "marker").exists() and not (lane.cwd.parent / "evil-hook").exists()
    assert run.init["permissionMode"] == "default"
    assert api.events("PermissionRequest"), "no allow rule loaded: the CLI asked, and the ask was denied"


def test_parallel_calls_under_defer_only_the_last_survives(lane, api):
    """Both calls are deferred, `deferred_tool_use` names the last, and on resume only it runs: the earlier one is
    gone from the conversation. The platform records the other approvals of such a run as dropped."""
    api.tools, api.mode = [bash("touch one"), bash("touch two")], "defer"
    first = lane.run("go")
    ids = first.tool_use_ids()
    assert len(ids) == 2 and first.result["deferred_tool_use"]["id"] == ids[1]
    api.mode = "allow"
    lane.run(None, resume=first.result["session_id"])
    assert sorted(p.name for p in lane.cwd.iterdir()) == ["two"]


@pytest.mark.parametrize("command, ran, deferred, asked", [
    ("touch marker", True, False, False),                  # allow-rule marker: the CLI's rule runs it
    ("mkdir marker", False, True, False),                  # no marker: deferred to the owner
    ("rm -f victim", False, False, False),                 # deny-rule marker: the CLI's rule denies it
    ("touch marker && mkdir other", False, False, True),   # marker fired, the CLI still asks: PermissionRequest
])
def test_marker_hooks_let_the_clis_rules_decide_and_defer_the_rest(lane, api, command, ran, deferred, asked):
    (lane.cwd / "victim").touch()
    api.tools, api.mode = [bash(command)], "marker"
    rules = ["Bash(touch:*)", "Bash(rm:*)"]
    run = lane.run("go", allowed=rules[:1], disallowed=rules[1:], marks=rules)
    assert (lane.cwd / "marker").exists() == ran
    assert (lane.cwd / "victim").exists() and not (lane.cwd / "other").exists()
    assert (run.result["stop_reason"] == "tool_deferred") == deferred
    assert bool(api.events("PermissionRequest")) == asked
    if not ran and not deferred:
        assert [d["tool_name"] for d in run.result["permission_denials"]] == ["Bash"]


def test_a_deferred_call_whose_tool_is_gone_at_resume_ends_unavailable(lane, api):
    api.tools, api.mode = [bash("touch marker")], "defer"
    first = lane.run("go")
    api.mode, api.hooks = "allow", []
    resumed = lane.run(None, resume=first.result["session_id"], disallowed=["Bash"])
    assert resumed.result["stop_reason"] == "tool_deferred_unavailable" and resumed.result["is_error"] is True
    assert not api.events("PreToolUse") and not (lane.cwd / "marker").exists()


@pytest.mark.skipif(not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), reason="needs the owner's CLAUDE_CODE_OAUTH_TOKEN")
def test_the_lane_cli_is_logged_in_with_the_owners_token(tmp_path):
    env = cliengine.env(os.environ["CLAUDE_CODE_OAUTH_TOKEN"], str(tmp_path), str(tmp_path / "config"), "", "",
                        HOOK_TOKEN)
    del env["HTTPS_PROXY"], env["NO_PROXY"]
    done = subprocess.run([cliengine.cli_path(), "auth", "status", "--json"], env={**env, cliengine.SCRUB: "0"},
                          capture_output=True, text=True, timeout=60)
    status = json.loads(done.stdout)
    assert status["loggedIn"] is True and status["authMethod"] == "oauth_token"


# --- the platform end to end: the pinned CLI under a real lane host, its hooks answered by the platform's gate ------


class Stack:
    """A lane host and a platform in this process, the CLI between them; either can be torn down and built again
    from its own files. The platform listens on one port throughout, so the hook address in a run's settings stays
    the same across a rebuild — and nothing answers there while it is down."""

    def __init__(self, root: Path, api: FakeApi) -> None:
        self.root, self.api = root, api
        (root / "work" / "repo").mkdir(parents=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        hook_url = f"http://127.0.0.1:{self.port}/hook"
        self.lane_cfg = LaneHostConfig(token="lane-token", hook_url=hook_url, max_running=2, work=str(root / "work"),
                                       home=str(root / "home"), config_dir=str(root / "home" / "config"),
                                       state=str(root / "state"), run_dir=str(root / "run"))
        (root / "run").mkdir()
        self.cfg = PlatformConfig(
            lanes={"code": LaneRef("code", "", hook_url, str(root / "work"), token="lane-token")},
            tracks=[Track("retinue", "code", "code", "repo")], state_db=str(root / "platform.sqlite"),
            router_token="router-token", hook_secret="hook-secret")
        self.host = self.lane_server = self.platform = self.runner = None

    def env(self, host: LaneHost, hook_token: str) -> dict[str, str]:
        env = cliengine.env("", self.lane_cfg.home, self.lane_cfg.config_dir, "", "", hook_token)
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "NO_PROXY"):
            del env[key]
        return {**env, cliengine.SCRUB: "0", "ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": self.api.url}

    async def lane_up(self) -> None:
        self.host = LaneHost(self.lane_cfg, lane_host.Journal(f"{self.lane_cfg.state}/journal.sqlite"))
        await self.host.start()
        assert self.host.pinned
        self.lane_server = TestServer(self.host.app())
        await self.lane_server.start_server()
        self.cfg.lanes["code"].url = str(self.lane_server.make_url("")).rstrip("/")

    async def lane_down(self) -> None:
        await self.lane_server.close()
        self.host.journal.db.close()

    def platform_object(self) -> None:
        self.store = PlatformStore(self.cfg.state_db)
        self.platform = Platform(self.cfg, self.store)

    async def platform_listening(self) -> None:
        self.runner = web.AppRunner(self.platform.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def platform_down(self) -> None:
        await self.runner.cleanup()
        self.store.db.close()

    async def owner(self, path: str, body: dict) -> dict:
        async with ClientSession() as http:
            async with http.post(f"http://127.0.0.1:{self.port}/owner/{path}", json=body,
                                 headers={"Authorization": "Bearer router-token"}) as response:
                return await response.json()

    async def settled(self) -> None:
        """The lane's CLI processes have all ended and the platform has taken every line."""
        for _ in range(TIMEOUT_S * 10):
            if not self.host.live:
                break
            await asyncio.sleep(0.1)
        assert not self.host.live, "the CLI did not end"
        while await self.platform.poll("code"):
            pass

    def notes(self, kind: str) -> list[dict]:
        return [n for n in self.store.look(200) if n["kind"] == kind]

    def results(self) -> list[dict]:
        return [line for line in (json.loads(row[0]) for row in self.store.db.execute("SELECT line FROM raw"))
                if line.get("type") == "result"]


@pytest.fixture
def stack(tmp_path, api, monkeypatch):
    stack = Stack(tmp_path, api)
    monkeypatch.setattr(LaneHost, "_env", lambda host, hook_token: stack.env(host, hook_token))
    monkeypatch.setattr(cliengine, "SETTINGS_PATH",
                        re.compile(re.escape(stack.lane_cfg.run_dir) + "/" + cliengine.RUN_ID.pattern + r"\.json"))
    return stack


def test_e4_a_deferred_call_granted_after_both_processes_restarted_runs_exactly_once(stack, api):
    marker = stack.root / "ran"
    api.tools = [bash(f"echo ran >> {marker}")]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        session = (await stack.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "go"}))["session"]
        await stack.settled()
        [card] = stack.notes("card")
        assert stack.store.session(session)["state"] == "waiting" and not marker.exists()

        await stack.platform_down()
        await stack.lane_down()
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        pressed = await stack.owner("press", {"button": card["data"]["buttons"][0][1]})
        assert pressed["toast"] == "Разрешено."
        await stack.settled()
        assert marker.read_text() == "ran\n"
        assert stack.store.approval(card["approval"])["state"] == "executed"
        assert [n["data"]["text"] for n in stack.notes("summary")] == ["done"]
        assert stack.store.session(session)["state"] == "closed"
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_e6_a_push_is_a_courier_card_never_a_grant_and_nothing_runs(stack, api):
    api.tools = [bash("git push origin main")]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        session = (await stack.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "go"}))["session"]
        await stack.settled()
        [card] = stack.notes("card")
        assert card["data"]["view"]["body"] == "git push origin main" and card["data"]["buttons"] == []
        assert stack.store.approvals(session=session, states=("pending", "shown", "granted", "executing",
                                                              "executed")) == []
        [result] = stack.results()
        assert result["stop_reason"] != "tool_deferred"
        assert [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_e12_with_the_platform_away_the_call_is_blocked_and_the_journal_waits_for_it(stack, api):
    api.tools = [bash("touch marker")]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        session = stack.store.new_session("retinue", "code", "code", 0.0)
        stack.store.enqueue(session, "go", 0.0)
        await stack.platform.drain()
        for _ in range(TIMEOUT_S * 10):
            if not stack.host.live:
                break
            await asyncio.sleep(0.1)
        assert not (stack.root / "work" / "repo" / "marker").exists(), "no hook answer: blocked"
        journal = [i["line"] for i in stack.host.journal.look(200)]

        await stack.platform_listening()
        await stack.settled()
        stored = [row[0] for row in stack.store.db.execute("SELECT line FROM raw ORDER BY lane_seq")]
        assert stored == journal and stack.host.journal.look(200) == []
        assert any(i["kind"] == "tool" for i in stack.store.items(session))
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())
