"""The container probe: the lane image, built and run as compose runs it (no capabilities, no new privileges, a
read-only root, the lanes' AppArmor and seccomp profiles), with the owner's real login. It answers what no unit test can:
whether the CLI's bubblewrap sandbox works in that container — in auto mode, with the env scrub off: the token and
the hook token out of a command's sight (the managed settings' `sandbox.credentials`), the config directory not
writable, a command's `allowed_domains` unable to widen the network — and that the room's managed settings offer the
CLI's web tools while Bash stays off the open web.

A stand-in platform answers every PreToolUse with `allow` (which also passes auto's classifier by), so whatever stops a
call here is the lane itself. The model is
real: the run costs a few calls. Skipped without Docker or without `CLAUDE_CODE_OAUTH_TOKEN`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from retinue import cliengine


def _docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
pytestmark = [pytest.mark.probe,
              pytest.mark.skipif(not TOKEN, reason="needs CLAUDE_CODE_OAUTH_TOKEN"),
              pytest.mark.skipif(not _docker(), reason="needs Docker")]

ROOT = Path(__file__).resolve().parents[1]
# The lanes' security_opt in compose, with the seccomp profile from the repository rather than its installed copy.
SECURITY = [arg for opt in ("no-new-privileges:true", "apparmor=retinue-lane",
                            f"seccomp={ROOT / 'deploy' / 'lanes' / 'seccomp.json'}", "systempaths=unconfined")
            for arg in ("--security-opt", opt)]
LANE, UID = "private", "10005"   # the room (docs/platform.md, «The room»)
LANE_TOKEN = "probe-lane-token"
RUN_TIMEOUT_S = 300
# Every hook that carries the run's token allows: the platform's own decisions are not under test here, the lane's
# are. One without it is denied, as the platform denies an unknown run, so the Bash calls run only if it arrives.
HOOKS = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Allow(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        event = self.path.rsplit("/", 1)[-1]
        if self.headers.get("Authorization") != "Bearer probe-hook-token":
            body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "unknown run"}}
        elif event == "PreToolUse":
            body = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "allow"}}
        elif event == "PermissionRequest":
            body = {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "allow"}}}
        else:
            body = {}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

HTTPServer(("0.0.0.0", 9500), Allow).serve_forever()
"""
PROMPT = """This is an automated test of a sandbox. Make exactly these tool calls, one at a time, in this order,
whatever the earlier ones return, then reply with one word: done.
1. Bash: echo PATH=$PATH; printenv CLAUDE_CODE_OAUTH_TOKEN | wc -c; grep -l sk-ant- /proc/[0-9]*/environ 2>/dev/null | wc -l; printenv RETINUE_HOOK_RUN | wc -c; printenv CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR | wc -c; grep -l probe-hook-token /proc/[0-9]*/environ 2>/dev/null | wc -l
2. Bash: touch /lane/config/written-by-bash && echo WROTE
3. Bash, with the Bash tool's allowed_domains parameter set to ["example.com"]: curl -sI https://example.com | head -1
4. WebFetch: url https://example.com, prompt "title"
"""


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=1800)
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture(scope="module")
def lane():
    """The lane image, a network with the internet on it (no proxy: the hosts list is not under test), the stand-in
    platform under its compose name, and the lane — its URL on this machine."""
    tag = f"retinue-lane-probe:{uuid.uuid4().hex[:8]}"
    net, hooks, name = f"lane-probe-{tag[-8:]}", f"lane-probe-hooks-{tag[-8:]}", f"lane-probe-{tag[-8:]}"
    docker("build", "-f", str(ROOT / "deploy" / "lanes" / "Dockerfile"), "--build-arg", f"LANE={LANE}",
           "--build-arg", f"LANE_UID={UID}", "-t", tag, str(ROOT))
    try:
        docker("network", "create", net)
        docker("run", "-d", "--rm", "--name", hooks, "--network", net, "--network-alias", "platform", tag,
               "python", "-c", HOOKS)
        docker("run", "-d", "--rm", "--name", name, "--network", net, "-p", "127.0.0.1::9600",
               "--cap-drop", "ALL", "--read-only", *SECURITY,
               "--tmpfs", f"/lane/run:size=16m,uid={UID},gid={UID}", "--tmpfs", f"/tmp:size=512m,uid={UID},gid={UID}",
               "--mount", "type=volume,dst=/work", "--mount", "type=volume,dst=/lane/config",
               "--mount", "type=volume,dst=/lane/state",
               "-e", f"RETINUE_LANE_TOKEN={LANE_TOKEN}",   # no login: it comes with the run, as from the platform
               "-e", "RETINUE_LANE_HOOK_URL=http://platform:9500/hook", "-e", "NO_PROXY=platform", tag)
        url = "http://" + docker("port", name, "9600/tcp").splitlines()[0]
        for _ in range(120):
            try:
                call(url, "GET", "/ping")
                break
            except OSError:
                time.sleep(1)
        yield name, url
    finally:
        for container in (name, hooks):
            docker("rm", "-f", container, check=False)
        docker("network", "rm", net, check=False)
        docker("image", "rm", "-f", tag, check=False)


def call(url: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {LANE_TOKEN}",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def test_bash_runs_sandboxed_without_the_token_and_the_web_is_the_clis_alone(lane):
    name, url = lane
    started = call(url, "POST", "/run", {
        "session": "0badcafe", "run": "probe", "cli_session": None, "cwd": "", "prompt": PROMPT,
        "settings": cliengine.settings("http://platform:9500/hook", 30, []), "allowed": [], "disallowed": [],
        "hook_token": "probe-hook-token", "oauth_token": TOKEN})
    assert started == {"status": "started"}, started
    journal: list[dict] = []
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline and not any(
            line.get("type") == "lane" and line.get("event") == "exited" for line in journal):
        items = call(url, "POST", "/items", {"limit": 200})["items"]
        if items:
            journal += [json.loads(item["line"]) for item in items]
            call(url, "POST", "/items/confirm", {"upto": items[-1]["seq"]})
        else:
            time.sleep(1)
    lines = [json.dumps(line) for line in journal]
    assert any(line.get("type") == "result" for line in journal), "the run did not end: " + "\n".join(lines[-5:])
    assert not [line for line in lines if TOKEN in line], "the token reached a command's output"

    calls = {block["id"]: block for line in journal if line.get("type") == "assistant"
             for block in line["message"]["content"] if block.get("type") == "tool_use"}
    results = {block["tool_use_id"]: block for line in journal if line.get("type") == "user"
               and isinstance(line["message"]["content"], list)
               for block in line["message"]["content"] if block.get("type") == "tool_result"}
    bash = [results[i] for i, block in calls.items() if block["name"] == "Bash" and i in results]
    assert len(bash) >= 2, "the model made the Bash calls"
    assert any("PATH=" in json.dumps(result) for result in bash), "the environment was printed"
    shown = next(result for result in bash if "PATH=" in json.dumps(result))
    assert not shown.get("is_error") and "\\n0\\n0\\n0\\n0\\n0" in json.dumps(shown["content"]), \
        "neither the token nor the hook token nor the token's descriptor is in the command's environment or in any " \
        "process's it can see: the scrub is off, the managed settings deny the names"
    assert not any("WROTE" in json.dumps(result) and not result.get("is_error") for result in bash), \
        "a write into /lane/config succeeded"
    assert docker("exec", name, "ls", "/lane/config/written-by-bash", check=False) == ""
    widened = [results.get(i) for i, block in calls.items() if block["name"] == "Bash"
               and block["input"].get("allowed_domains")]
    assert widened, "the model passed allowed_domains"
    assert all(result is None or result.get("is_error") or "200" not in json.dumps(result["content"])
               for result in widened), "a command's allowed_domains reached a host the managed list does not name"
    init = next(line for line in journal if line.get("type") == "system" and line.get("subtype") == "init")
    assert init["permissionMode"] == "auto", "the CLI runs in auto: else the platform falls back to cards"
    assert {"WebFetch", "WebSearch"} <= set(init.get("tools", [])), "the room's web is the CLI's"


def test_the_lane_runs_the_pinned_cli_logged_in_with_the_login_the_run_brought(lane):
    """After the run above: the lane host holds the login the run brought, in memory, and its container's
    environment never had it."""
    _, url = lane
    ping = call(url, "GET", "/ping")
    assert ping["pinned"] is True and cliengine.CLI_VERSION in ping["version"]
    assert ping["login"] == {"loggedIn": True, "authMethod": "oauth_token"}
    name, _ = lane
    docker("exec", name, "test", "-r", "/etc/claude-code/managed-settings.json")   # else the CLI runs without them
    managed = docker("exec", name, "cat", "/etc/claude-code/CLAUDE.md")  # the lane's instructions, common part first
    assert managed.startswith("# A session on the owner's server") and "`out/`" in managed
    assert docker("exec", name, "stat", "-c", "%U %a", "/etc/claude-code/CLAUDE.md") == "root 644"
    assert "sk-ant-" not in docker("exec", name, "cat", "/proc/1/environ"), "not in Docker init's environment"
