#!/usr/bin/env python3
"""Settings on the host: what the owner sets in the app's «Settings», written where the stack reads it, by root.
Runs as root from retinue-settings.service, with the host's python3 (stdlib only), whenever the platform drops a
request into <root>/settings/inbox (retinue-settings.path) and whenever one of the files it reports on changes.

The platform holds no write to the stack's environment, the git tokens or platform.yaml, and must never read a
secret back. So it only drops requests, one JSON file each, into a folder it can write and root reads; this script
takes each one out, deletes it before anything else, checks it against the closed lists below and writes:

- a key of the stack (`KEYS`): its line in stack.env, replaced as setup.sh's `put` does. A new bot token also makes
  a new RETINUE_WEBAPP_KEY when the face is on, as setup.sh derives it;
- a git token: piped into deploy/code/set-git-token.sh itself — Settings is that script's form, not a second store;
- a project's key, RETINUE_PROJECT_KEY_<NAME>: its line in egress/project-keys.env, the same way;
- a lane's `model` and `effort`: their lines in that lane's block of platform.yaml, values from the closed lists of
  retinue/cliengine.py (MODELS, EFFORTS; tests/test_settings_host.py keeps the copies equal);
- a restart: `docker compose up -d --force-recreate` of the services a change is waiting on (`SERVICES`), and of no
  other.

What it reports goes to <root>/settings/status/status.json, which the platform reads through a read-only mount:
whether each key is set, when Settings last changed it, the lanes' values in the file, which services wait for a
restart and how each request ended. Never a value, never a part of one, never a hash of one. Nothing it prints
holds a value either: journalctl sees names and lengths.

Paths come from the environment, so the same script runs in the tests with a fake `docker`.
"""

import fcntl
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("RETINUE_ROOT", "/srv/retinue"))
REPO = Path(os.environ.get("RETINUE_REPO", "/opt/retinue"))
DOCKER = os.environ.get("DOCKER", "docker")
PROJECT = os.environ.get("RETINUE_PROJECT", "retinue")
PLATFORM_UID = int(os.environ.get("RETINUE_PLATFORM_UID", "10007"))  # compose's `user: "10007:10007"`
INBOX = ROOT / "settings" / "inbox"
STATUS = ROOT / "settings" / "status" / "status.json"
LOCK = ROOT / "settings" / "status" / ".lock"
STACK = ROOT / "stack.env"
TOKENS = ROOT / "code" / "tokens.env"
PROJECT_KEYS = ROOT / "egress" / "project-keys.env"
PLATFORM_YAML = ROOT / "platform" / "platform.yaml"
GIT_SCRIPT = REPO / "deploy" / "code" / "set-git-token.sh"

SCHEMA = 1
REQUEST = re.compile(r"[0-9a-f]{32}\.json")    # the platform's file names (retinue/settings.py), nothing else is read
REQUEST_MAX = 16 * 1024
STRAY_S = 60                                   # a file that is not a request, left this long, is deleted
DONE_KEEP = 50
RESTART_TIMEOUT_S = 600

# The services a change can wait on, and the compose profile each runs under (None: always on). A restart names
# these and no other.
SERVICES = {"router": None, "assistant": None, "platform": "platform", "tg-business": "tgbusiness"}
# The stack's keys the owner sets: who reads each (docs/install.md), its shape, and whether it may be emptied (an
# optional part's). The keys setup.sh generates (the bus secret, the lanes' tokens) are not here: they are rotated by
# setup.sh, never typed.
VALUE = r"[A-Za-z0-9._~+/=:@%,-]"              # no space, quote, `$`, `#`, `\`: a line compose reads as it is
KEYS = {
    "CLAUDE_CODE_OAUTH_TOKEN": {"services": ["assistant", "platform"], "shape": VALUE + "{20,1024}", "clear": False},
    "TELEGRAM_BOT_TOKEN": {"services": ["router"], "shape": r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}", "clear": False},
    "TELEGRAM_BUSINESS_TOKEN": {"services": ["tg-business"], "shape": r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}",
                                "clear": True},
    "ELEVENLABS_API_KEY": {"services": ["router", "platform"], "shape": VALUE + "{16,256}", "clear": True},
}
WEBAPP_KEY = "RETINUE_WEBAPP_KEY"              # HMAC_SHA256(key="WebAppData", msg=<bot token>), setup.sh's
GIT_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")  # set-git-token.sh's own check of its argument
GIT_PREFIX = "RETINUE_GIT_TOKEN_"
PROJECT_NAME = re.compile(r"[A-Z0-9_]{1,40}")
PROJECT_PREFIX = "RETINUE_PROJECT_KEY_"
PROJECT_VALUE = re.compile(VALUE + "{8,4096}")
LANE = re.compile(r"[a-z][a-z0-9]{1,30}")      # retinue/config.py LANE_NAME
# retinue/cliengine.py MODELS and EFFORTS: what the lanes may run with, each probed on the pinned CLI.
MODELS = ("claude-opus-5-5[1m]", "claude-opus-5-5", "claude-sonnet-5-5[1m]", "claude-sonnet-5-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class Refusal(Exception):
    """A request this script will not apply; its words go to the status, never a value in them."""


def log(text: str) -> None:
    print(text, flush=True)


# --- the files -----------------------------------------------------------------------------------------------------


def env_values(path: Path) -> dict[str, str]:
    """`NAME=value` lines, the last one winning, quotes taken off as setup.sh does. Only for `set` and the webapp key:
    a value read here goes nowhere but to `bool()` or to HMAC."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[name] = value
    return out


def write_private(path: Path, text: str) -> None:
    """Replace a root-only file whole: a temporary file of mode 600 beside it, renamed over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def put_line(path: Path, name: str, value: str) -> None:
    """`name=value` replaces every line of that name, as setup.sh's `put` and set-git-token.sh do."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if not line.startswith(f"{name}=")]
    write_private(path, "\n".join([*kept, f"{name}={value}"]) + "\n")


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def content(line: str) -> bool:
    """A line YAML reads: not blank, not a comment."""
    return bool(line.strip()) and not line.lstrip().startswith("#")


def lane_block(lines: list[str], lane: str) -> tuple[int, int, int]:
    """Where `lanes: <lane>:` is in platform.yaml's lines: its header line, the end of its block (exclusive) and the
    indent of its fields. A lane written on one line (`private: {url: …}`) is the owner's to edit by hand."""
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"lanes:\s*(#.*)?", line)), None)
    if start is None:
        raise Refusal("platform.yaml has no lanes: section")
    end = next((i for i in range(start + 1, len(lines)) if content(lines[i]) and indent(lines[i]) == 0), len(lines))
    for i in range(start + 1, end):
        found = re.fullmatch(rf"( +){re.escape(lane)}:(.*)", lines[i])
        if not found:
            continue
        rest = found.group(2).strip()
        if rest and not rest.startswith("#"):
            raise Refusal(f"Lane {lane} is written on one line in platform.yaml: edit it there by hand")
        own = len(found.group(1))
        stop = next((j for j in range(i + 1, end) if content(lines[j]) and indent(lines[j]) <= own), end)
        field = next((indent(lines[j]) for j in range(i + 1, stop) if content(lines[j])), own + 2)
        return i, stop, field
    raise Refusal(f"There is no lane {lane} in platform.yaml")


def lane_values(text: str, lane: str) -> dict[str, str]:
    """The lane's `engine`, `model` and `effort` as the file has them; a field not written is left out."""
    lines = text.splitlines()
    head, stop, field = lane_block(lines, lane)
    out = {}
    for line in lines[head + 1:stop]:
        found = re.fullmatch(rf" {{{field}}}(engine|model|effort):\s*(.*?)\s*(#.*)?", line)
        if found:
            value = found.group(2)
            out[found.group(1)] = json.loads(value) if value.startswith('"') else value.strip("'")
    return out


def lane_names(text: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"lanes:\s*(#.*)?", line)), None)
    if start is None:
        return []
    names, own = [], None
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            depth = len(line) - len(line.lstrip(" "))
            if depth == 0:
                break
            own = depth if own is None else own
            if depth == own and (found := re.match(r" +([a-z][a-z0-9]{1,30}):", line)):
                names.append(found.group(1))
    return names


def set_lane(text: str, lane: str, fields: dict[str, str]) -> str:
    """platform.yaml with the lane's fields replaced, or added after its last line; nothing else is touched."""
    lines = text.splitlines()
    head, stop, field = lane_block(lines, lane)
    for name, value in fields.items():
        line = f"{' ' * field}{name}: {json.dumps(value)}"
        # its line, or the example's commented `# model: …` in its place
        at = next((j for j in range(head + 1, stop) if re.match(rf" {{{field}}}{name}:", lines[j])), None)
        if at is None:
            at = next((j for j in range(head + 1, stop) if re.match(rf" {{{field}}}#\s*{name}:", lines[j])), None)
        if at is not None:
            lines[at] = line
        else:
            # after its last field, never after the comments that open what follows
            last = max(j for j in range(head, stop) if content(lines[j]))
            lines.insert(last + 1, line)
            stop += 1
    return "\n".join(lines) + "\n"


# --- the status ----------------------------------------------------------------------------------------------------


def read_status() -> dict:
    try:
        status = json.loads(STATUS.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return status if isinstance(status, dict) and status.get("schema") == SCHEMA else {}


def profiles() -> list[str]:
    return [p for p in env_values(STACK).get("COMPOSE_PROFILES", "").split(",") if p]


def running(services: list[str]) -> list[str]:
    """The services of a change that run here: one whose profile is off reads the new value when it is turned on."""
    on = profiles()
    return [s for s in services if SERVICES[s] is None or SERVICES[s] in on]


def build_status(changed: dict, pending: set[str], done: dict, now: float,
                 restarted: float | None) -> dict:
    stack, tokens, project_keys = env_values(STACK), env_values(TOKENS), env_values(PROJECT_KEYS)
    keys = {name: {"set": bool(stack.get(name)), "changed": changed.get(name)} for name in KEYS}
    git = {name[len(GIT_PREFIX):].lower(): {"set": bool(value), "changed": changed.get(name)}
           for name, value in tokens.items() if name.startswith(GIT_PREFIX)}
    project = {name[len(PROJECT_PREFIX):]: {"set": bool(value), "changed": changed.get(name)}
               for name, value in project_keys.items() if name.startswith(PROJECT_PREFIX)}
    lanes = {}
    try:
        text = PLATFORM_YAML.read_text()
        for lane in lane_names(text):
            try:
                lanes[lane] = lane_values(text, lane)
            except Refusal:
                lanes[lane] = {}
    except FileNotFoundError:
        pass
    return {"schema": SCHEMA, "at": now, "profiles": profiles(), "keys": keys, "git": git, "project_keys": project,
            "lanes": lanes, "pending": sorted(pending), "restarted": restarted, "changed": changed,
            "done": dict(list(done.items())[-DONE_KEEP:])}


def write_status(status: dict) -> None:
    """The platform's to read alone (its uid, mode 600; mounted read-only, so it cannot write it): no value is in it,
    and no one else needs it."""
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_name(f".status.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(status, separators=(",", ":"), sort_keys=True))
    if os.geteuid() == 0:
        os.chown(tmp, PLATFORM_UID, PLATFORM_UID)
    os.replace(tmp, STATUS)


# --- the requests --------------------------------------------------------------------------------------------------


def take(path: Path) -> dict | None:
    """One request, read without following a link and deleted before it is looked at: a secret lies on no disk
    longer than this. Anything that is not a small regular JSON object is dropped unread."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        path.unlink(missing_ok=True)
        return None
    try:
        info = os.fstat(fd)
        data = os.read(fd, REQUEST_MAX + 1) if (info.st_mode & 0o170000) == 0o100000 else b""
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
    if not data or len(data) > REQUEST_MAX:
        return None
    try:
        request = json.loads(data)
    except ValueError:
        return None
    return request if isinstance(request, dict) else None


def value_of(request: dict, shape: str) -> str:
    value = request.get("value")
    if not isinstance(value, str) or not re.fullmatch(shape, value):
        raise Refusal("This does not look like the right key: check you copied all of it, with nothing around it")
    return value


def apply(request: dict, changed: dict, pending: set[str], now: float) -> None:
    op = request.get("op")
    if op == "key":
        name = request.get("name")
        if name not in KEYS:
            raise Refusal("Settings does not set this key")
        spec = KEYS[name]
        if request.get("clear") is True:
            if not spec["clear"]:
                raise Refusal("This key cannot be removed: Retinue needs it")
            put_line(STACK, name, "")
        else:
            put_line(STACK, name, value_of(request, spec["shape"]))
        changed[name] = now
        services = list(spec["services"])
        if name == "TELEGRAM_BOT_TOKEN" and env_values(STACK).get("RETINUE_FACE_ORIGIN"):
            token = env_values(STACK)[name].encode()
            put_line(STACK, WEBAPP_KEY, hmac.new(b"WebAppData", token, hashlib.sha256).hexdigest())
            services.append("platform")
        pending.update(running(services))
        log(f"set {name}")
    elif op == "git":
        key = request.get("key")
        if not isinstance(key, str) or not GIT_KEY.fullmatch(key):
            raise Refusal("An account's key is a-z, 0-9 and _, as the repository's token: in platform.yaml")
        value = value_of(request, r"[^\s=]{1,512}")
        done = subprocess.run(["bash", str(GIT_SCRIPT), key], input=value + "\n", text=True, capture_output=True,
                              env={**os.environ, "RETINUE_ROOT": str(ROOT)}, timeout=60)
        if done.returncode != 0:
            raise Refusal("The token was not written: " + (done.stderr.strip().splitlines() or ["no reason"])[-1])
        changed[GIT_PREFIX + key.upper()] = now
        pending.update(running(["platform"]))
        log(f"set {GIT_PREFIX}{key.upper()} through set-git-token.sh")
    elif op == "project_key":
        name = request.get("name")
        if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
            raise Refusal("A project key's name is A-Z, 0-9 and _")
        put_line(PROJECT_KEYS, PROJECT_PREFIX + name, value_of(request, PROJECT_VALUE.pattern))
        changed[PROJECT_PREFIX + name] = now
        pending.update(running(["platform"]))
        log(f"set {PROJECT_PREFIX}{name}")
    elif op == "lane":
        lane = request.get("lane")
        if not isinstance(lane, str) or not LANE.fullmatch(lane):
            raise Refusal("Not a lane's name")
        fields = {}
        for name, allowed in (("model", MODELS), ("effort", EFFORTS)):
            if name in request:
                if request[name] not in allowed:
                    raise Refusal(f"{name.capitalize()} {request[name]!r} is not one of {', '.join(allowed)}")
                fields[name] = request[name]
        if not fields:
            raise Refusal("Nothing to change")
        text = PLATFORM_YAML.read_text()
        new = set_lane(text, lane, fields)
        if new != text:
            tmp = PLATFORM_YAML.with_name(f".platform.yaml.{os.getpid()}.tmp")
            tmp.write_text(new)
            os.chmod(tmp, PLATFORM_YAML.stat().st_mode & 0o777)
            os.replace(tmp, PLATFORM_YAML)
            changed[f"lane:{lane}"] = now
            pending.update(running(["platform"]))
        log(f"lane {lane}: {', '.join(f'{k} {v}' for k, v in fields.items())}")
    else:
        raise Refusal("Not a request this script takes")


def restart(services: list[str]) -> None:
    command = [DOCKER, "compose", "-p", PROJECT, "--env-file", str(STACK), "-f", str(REPO / "deploy" / "compose.yml"),
               "up", "-d", "--no-deps", "--force-recreate", *services]
    done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=RESTART_TIMEOUT_S)
    if done.returncode != 0:
        tail = (done.stderr.strip().splitlines() or ["no reason given"])[-1]
        raise Refusal(f"The restart failed: {tail[:300]}")
    log(f"restarted {' '.join(services)}")


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_status()
        changed = dict(previous.get("changed") or {})
        pending = {s for s in previous.get("pending") or [] if s in SERVICES}
        done = dict(previous.get("done") or {})
        restarted = previous.get("restarted")
        now = time.time()
        requests = []
        for path in sorted(INBOX.iterdir()) if INBOX.is_dir() else []:
            if REQUEST.fullmatch(path.name):
                if (request := take(path)) is not None:
                    requests.append((path.stem, request))
            elif now - path.lstat().st_mtime > STRAY_S:
                path.unlink(missing_ok=True)
        restart_ids = []
        for rid, request in sorted(requests, key=lambda r: r[1].get("op") == "restart"):  # writes first
            if request.get("op") == "restart":
                restart_ids.append(rid)
                continue
            try:
                apply(request, changed, pending, now)
                done[rid] = {"ok": True, "at": now}
            except Refusal as refusal:
                done[rid] = {"ok": False, "at": now, "why": str(refusal)}
                log(f"refused a request: {refusal}")
            except (OSError, subprocess.SubprocessError) as error:
                done[rid] = {"ok": False, "at": now, "why": f"The server could not write it ({type(error).__name__})"}
                log(f"failed a request: {type(error).__name__}")
        if restart_ids:
            services = [s for s in SERVICES if s in pending]
            # Said first, so the app knows the restart is under way while the platform it talks to goes down.
            write_status(build_status(changed, pending, done, now, restarted) | {"restarting": services})
            try:
                if services:
                    restart(services)
                pending.clear()
                restarted = time.time()
                for rid in restart_ids:
                    done[rid] = {"ok": True, "at": restarted, "services": services}
            except (Refusal, OSError, subprocess.SubprocessError) as error:
                why = str(error) if isinstance(error, Refusal) else f"The restart failed ({type(error).__name__})"
                for rid in restart_ids:
                    done[rid] = {"ok": False, "at": time.time(), "why": why}
                log(why)
        write_status(build_status(changed, pending, done, time.time(), restarted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
