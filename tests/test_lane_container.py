"""The container probe: the lane image, built and run as compose runs it (no capabilities, no new privileges, a
read-only root, Docker's default seccomp profile), with the owner's real login. It answers what no unit test can:
whether the CLI's bubblewrap sandbox works in that container — the env scrub, the token out of a command's sight,
the config directory not writable — and that the managed settings deny the web.

A stand-in platform answers every hook with `allow`, so whatever stops a call here is the lane itself. The model is
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
LANE, UID = "code", "10006"
LANE_TOKEN = "probe-lane-token"
RUN_TIMEOUT_S = 300
# Every hook allows: the platform's own decisions are not under test here, the lane's are.
HOOKS = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Allow(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        event = self.path.rsplit("/", 1)[-1]
        if event == "PreToolUse":
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
1. Bash: env; for f in /proc/[0-9]*/environ; do tr '\\0' '\\n' < "$f"; done 2>/dev/null
2. Bash: touch /lane/config/written-by-bash && echo WROTE
3. WebFetch: url https://example.com, prompt "title"
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
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only",
               "--tmpfs", f"/lane/run:size=16m,uid={UID},gid={UID}", "--tmpfs", f"/tmp:size=512m,uid={UID},gid={UID}",
               "--mount", "type=volume,dst=/work", "--mount", "type=volume,dst=/lane/config",
               "--mount", "type=volume,dst=/lane/state",
               "-e", "CLAUDE_CODE_OAUTH_TOKEN", "-e", f"RETINUE_LANE_TOKEN={LANE_TOKEN}",
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


def test_the_lane_runs_the_pinned_cli_logged_in(lane):
    _, url = lane
    ping = call(url, "GET", "/ping")
    assert ping["pinned"] is True and cliengine.CLI_VERSION in ping["version"]
    assert ping["login"] == {"loggedIn": True, "authMethod": "oauth_token"}


def test_bash_runs_sandboxed_without_the_token_and_the_web_is_denied(lane):
    name, url = lane
    started = call(url, "POST", "/run", {
        "session": "0badcafe", "run": "probe", "cli_session": None, "cwd": "", "prompt": PROMPT,
        "settings": cliengine.settings("http://platform:9500/hook", 30, []), "allowed": [], "disallowed": [],
        "hook_token": "probe-hook-token"})
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
    assert not any("WROTE" in json.dumps(result) and not result.get("is_error") for result in bash), \
        "a write into /lane/config succeeded"
    assert docker("exec", name, "ls", "/lane/config/written-by-bash", check=False) == ""
    fetched = [results.get(i) for i, block in calls.items() if block["name"] == "WebFetch"]
    assert all(result is None or result.get("is_error") for result in fetched), "WebFetch ran"
    init = next(line for line in journal if line.get("type") == "system" and line.get("subtype") == "init")
    assert fetched or "WebFetch" not in init.get("tools", []), "WebFetch is neither offered nor callable"
