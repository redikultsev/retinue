"""The replay set (`retinue/evals.py`), run on the server: each scenario in a fresh container of the room's own lane
image, run as compose runs a lane (no capabilities, a read-only root, the lanes' AppArmor and seccomp profiles), with
the stack's login and a stand-in platform that allows every call. Prints a line per check and writes the whole result
to /srv/retinue/evals/<time>.json — never into a lane's view.

    sudo PYTHONPATH=/opt/retinue python3 /opt/retinue/deploy/evals/run.py [scenario ...] [--repeat N]

Standard library only, beside the platform's `cliengine` and `evals`. The login is read from the stack's env file and
given to the lane host with the run, as the platform does; it is never printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from retinue import cliengine, evals

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path("/srv/retinue/stack.env")
OUT = Path("/srv/retinue/evals")
LANE_TOKEN = "eval-lane-token"
HOOK_TOKEN = "eval-hook-token"
RUN_TIMEOUT_S = 900
SECURITY = [arg for opt in ("no-new-privileges:true", "apparmor=retinue-lane",
                            f"seccomp={ROOT / 'deploy' / 'lanes' / 'seccomp.json'}", "systempaths=unconfined")
            for arg in ("--security-opt", opt)]
HOOKS = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Allow(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        event = self.path.rsplit("/", 1)[-1]
        if self.headers.get("Authorization") != "Bearer %s":
            body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "unknown run"}}
        elif event == "PreToolUse":
            body = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "allow"}}
        elif event == "PermissionRequest":
            body = {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "deny",
                                                                                "message": "no one to ask"}}}
        else:
            body = {}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

HTTPServer(("0.0.0.0", 9500), Allow).serve_forever()
""" % HOOK_TOKEN
LEFT = """
import json, os, sys
out = {}
for path in json.load(sys.stdin):
    try:
        with open(os.path.join("/work", path), encoding="utf-8") as f:
            out[path] = f.read()
    except OSError:
        pass
print(json.dumps(out))
"""
SEED = """
import json, os, sys
for path, text in json.load(sys.stdin).items():
    target = os.path.join("/work", path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
"""


def docker(*args: str, check: bool = True, stdin: str | None = None) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=1800, input=stdin)
    if check and done.returncode != 0:
        raise SystemExit(f"docker {args[0]}: {done.stderr.strip()[:400]}")
    return done.stdout.strip()


def lane_image() -> str:
    """The room's image as deploy/compose.yml names it: the release in stack.env, or the one built here."""
    env = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if "=" in line)
    name, version = (env.get(key, "").strip().strip("'\"") for key in ("RETINUE_IMAGE", "RETINUE_VERSION"))
    return f"{name or 'retinue'}-lane-private:{version or 'local'}"


def login() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"no CLAUDE_CODE_OAUTH_TOKEN in {ENV_FILE}")


def call(url: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {LANE_TOKEN}",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def run_one(scenario: evals.Scenario, token: str, uid: str, image: str) -> evals.Run:
    tag = uuid.uuid4().hex[:8]
    net, hooks, name, volume = f"retinue-eval-{tag}", f"retinue-eval-hooks-{tag}", f"retinue-eval-{tag}", \
        f"retinue-eval-work-{tag}"
    try:
        docker("network", "create", net)
        docker("run", "-d", "--rm", "--name", hooks, "--network", net, "--network-alias", "platform", image,
               "python", "-c", HOOKS)
        docker("run", "-d", "--rm", "--name", name, "--network", net, "-p", "127.0.0.1::9600",
               "--cap-drop", "ALL", "--read-only", *SECURITY,
               "--tmpfs", f"/lane/run:size=16m,uid={uid},gid={uid}", "--tmpfs", f"/tmp:size=512m,uid={uid},gid={uid}",
               "--mount", f"type=volume,src={volume},dst=/work", "--mount", "type=volume,dst=/lane/config",
               "--mount", "type=volume,dst=/lane/state",
               "-e", f"RETINUE_LANE_TOKEN={LANE_TOKEN}", "-e", "RETINUE_LANE_HOOK_URL=http://platform:9500/hook",
               "-e", "NO_PROXY=platform", image)
        docker("exec", "-i", name, "python3", "-c", SEED, stdin=json.dumps(scenario.files))
        url = "http://" + docker("port", name, "9600/tcp").splitlines()[0]
        for _ in range(120):
            try:
                call(url, "GET", "/ping")
                break
            except OSError:
                time.sleep(1)
        started = call(url, "POST", "/run", {
            "session": tag, "run": "eval", "cli_session": None, "cwd": scenario.cwd, "prompt": scenario.prompt,
            "settings": cliengine.settings("http://platform:9500/hook", 30, []), "allowed": [], "disallowed": [],
            "hook_token": HOOK_TOKEN, "oauth_token": token})
        if started != {"status": "started"}:
            raise SystemExit(f"{scenario.name}: the lane did not start the run: {started}")
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
        left = json.loads(docker("exec", "-i", name, "python3", "-c", LEFT, stdin=json.dumps(list(scenario.files))))
        return evals.Run([line for line in journal if line.get("type") != "stream_event"], left)
    finally:
        for container in (name, hooks):
            docker("rm", "-f", container, check=False)
        docker("network", "rm", net, check=False)
        docker("volume", "rm", "-f", volume, check=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    chosen = [s for s in evals.SCENARIOS if not args.scenarios or s.name in args.scenarios]
    if not chosen:
        raise SystemExit("no such scenario; there are: " + ", ".join(s.name for s in evals.SCENARIOS))
    image = lane_image()
    token, uid = login(), docker("run", "--rm", "--entrypoint", "id", image, "-u")
    results = []
    for scenario in chosen:
        for n in range(args.repeat):
            run = run_one(scenario, token, uid, image)
            scored = {**evals.score(scenario, run), "repeat": n, "summary": run.summary[:2000]}
            if token in json.dumps(run.lines):
                scored["passed"], scored["leak"] = False, "the login appeared in the run's lines"
            results.append(scored)
            mark = "PASS" if scored["passed"] else "FAIL"
            print(f"{mark} {scenario.name} #{n + 1}: " + "; ".join(
                f"{'+' if c['held'] else '-'}{c['check']}{'' if c['hard'] else ' (soft)'}" for c in scored["checks"]),
                flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    commit = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (OUT / f"{stamp}.json").write_text(json.dumps({"commit": commit, "at": stamp, "results": results},
                                                  ensure_ascii=False, indent=1))
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} passed at {commit} — {OUT / (stamp + '.json')}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
