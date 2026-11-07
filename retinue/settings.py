"""«Settings» in the app (docs/face.md «Settings»): the keys and tokens the stack reads, the lane's model and effort,
each set or replaced from the app and none ever read back.

The platform cannot write what it is asked to change — the stack's environment is root's, the git tokens and the
projects' keys root's files, platform.yaml mounted read-only — and it must not: a key typed in the app goes to the
server and is never shown again. So a change is a request, one JSON file dropped into `settings_inbox`, which root's
`retinue-settings.service` on the host takes out, deletes, checks against its own closed lists and writes
(deploy/settings/retinue-settings.py); what it did comes back in `settings_status`, a file with whether each key is
set, when Settings changed it, the lanes' values, the services waiting for a restart and how each request ended —
never a value. A restart is a request too, pressed by the owner.

The owner's computers (`/mcp/device`) are listed here too — name, scope, when last seen — and one is taken back here
at once: that is the platform's own file (`Collections.revoke_device`), not root's. Making one stays the server's
terminal's (`retinue-platform collections device add`): its token is shown once, and the app never holds one.

What a key is for and where to get it is said here, in the owner's words; which names exist, what they look like and
who reads them is the host script's, and the copies here are kept equal to it by tests/test_settings.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import cliengine
from .collections import DEVICE, Rejected
from .collections import Refused as StoreRefused

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.settings")

SCHEMA = 1
VALUE = r"[A-Za-z0-9._~+/=:@%,-]"
TELEGRAM = r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}"
GIT_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
GIT_VALUE = re.compile(r"[^\s=]{1,512}")
PROJECT_NAME = re.compile(r"[A-Z0-9_]{1,40}")
PROJECT_VALUE = re.compile(VALUE + "{8,4096}")
BAD_VALUE = "This does not look like the right key: check you copied all of it, with nothing around it."

# The stack's keys, in the order the screen shows them. `group`: its section; `on`: the compose profile its part runs
# under (None: always), so a key of a part that is off says so; `clear`: an optional part's, which may be removed.
KEYS = [
    {"name": "CLAUDE_CODE_OAUTH_TOKEN", "group": "keys", "title": "Claude subscription", "shape": VALUE + "{20,1024}",
     "what": "Runs the assistant and every session on your Claude plan",
     "where": "On a computer with Claude Code, run claude setup-token and paste what it prints. Not an API key: "
              "one would bill the API instead of your plan.",
     "hint": "sk-ant-oat01-…", "on": None, "clear": False},
    {"name": "ELEVENLABS_API_KEY", "group": "keys", "title": "ElevenLabs", "shape": VALUE + "{16,256}",
     "what": "Turns voice notes and dictation into text. Without it, voice is refused",
     "where": "elevenlabs.io → Developers → API keys: a key with speech to text only and a credit limit.",
     "hint": "sk_…", "on": None, "clear": True},
    {"name": "TELEGRAM_BOT_TOKEN", "group": "channels", "title": "Telegram bot", "shape": TELEGRAM,
     "what": "Your chat with Retinue and its cards in Telegram, and the app's sign-in from Telegram",
     "where": "@BotFather → /mybots → your bot → API Token. Revoking it there gives a new one.",
     "hint": "123456789:AA…", "on": None, "clear": False},
    {"name": "TELEGRAM_BUSINESS_TOKEN", "group": "channels", "title": "Telegram Business", "shape": TELEGRAM,
     "what": "Reads the chats you chose and sends the replies you approve",
     "where": "@BotFather: a second bot of its own with Business Mode on, never the main bot's token.",
     "hint": "123456789:AA…", "on": "tgbusiness", "clear": True},
]
BY_NAME = {key["name"]: key for key in KEYS}
OPS = ("key", "git", "project_key", "lane", "restart")
# What a model is called on screen: its name and its context.
MODEL_LABELS = {"claude-opus-5-5[1m]": "Opus 5.5 · 1M context", "claude-opus-5-5": "Opus 5.5",
                "claude-sonnet-5-5[1m]": "Sonnet 5.5 · 1M context", "claude-sonnet-5-5": "Sonnet 5.5"}
EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"}
ENGINE_LABELS = {"claude-code": "Claude Code"}


class Settings:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    @property
    def cfg(self):
        return self.platform.cfg  # the platform's own, as it is now

    def routes(self) -> list:
        return [web.get("/api/settings", self.get),
                web.post("/api/settings/{op:" + "|".join(OPS) + "}", self.post),
                web.post("/api/settings/device/revoke", self.revoke)]

    # --- what the screen shows -------------------------------------------------------------------------------------

    def status(self) -> dict | None:
        try:
            status = json.loads(Path(self.cfg.settings_status).read_text())
        except (OSError, ValueError):
            return None
        return status if isinstance(status, dict) and status.get("schema") == SCHEMA else None

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response({**self.view(self.status()), "devices": await self.devices()})

    async def devices(self) -> list[dict] | None:
        """The owner's computers, as made: what each may do and when it was last seen — its last request since the
        platform started, else its last change — never a token or its hash. None: the store could not be read."""
        try:
            made = await self.platform.collections.devices()
        except StoreRefused:
            return None
        computers = self.platform.computers
        return [{"name": d["name"], "scope": d["scope"], "created": d["created"], "revoked": d["revoked"],
                 "seen": max((t for t in (computers.seen(d["name"]), d["written"]) if t is not None), default=None)}
                for d in made]

    def view(self, status: dict | None) -> dict:
        s = status or {}
        profiles = set(s.get("profiles") or [])

        def state(entry: object) -> dict:
            entry = entry if isinstance(entry, dict) else {}
            return {"set": entry.get("set") is True, "changed": entry.get("changed")}

        keys = [{"name": k["name"], "group": k["group"], "title": k["title"], "what": k["what"], "where": k["where"],
                 "hint": k["hint"], "clear": k["clear"], "on": k["on"] is None or k["on"] in profiles,
                 **state((s.get("keys") or {}).get(k["name"]))} for k in KEYS]
        git_status = s.get("git") or {}
        repos: dict[str, list[str]] = {}
        for repo in (self.cfg.code.repos if self.cfg.code else []):
            repos.setdefault(repo.token, []).append(repo.path)
        git = [{"key": key, "repos": repos.get(key, []), **state(git_status.get(key))}
               for key in sorted(set(repos) | set(git_status)) if GIT_KEY.fullmatch(key)]
        project_status = s.get("project_keys") or {}
        projects = self.platform.projects.projects if getattr(self.platform, "projects", None) else {}
        wanted = {p.key_env.removeprefix("RETINUE_PROJECT_KEY_"): p.path or "." for p in projects.values() if p.key_env}
        project_keys = [{"name": name, "project": wanted.get(name, ""), **state(project_status.get(name))}
                        for name in sorted(set(wanted) | set(project_status)) if PROJECT_NAME.fullmatch(name)]
        file_lanes = s.get("lanes") or {}
        lanes = []
        for lane in self.cfg.lanes.values():
            if lane.retired:
                continue
            written = file_lanes.get(lane.name) if isinstance(file_lanes.get(lane.name), dict) else {}
            lanes.append({"name": lane.name, "engine": lane.engine, "engine_label": ENGINE_LABELS.get(lane.engine, lane.engine),
                          "model": written.get("model", lane.model), "effort": written.get("effort", lane.effort),
                          "running": {"model": lane.model, "effort": lane.effort}})
        return {"schema": SCHEMA, "ready": status is not None, "at": s.get("at"),
                "pending": [p for p in s.get("pending") or [] if isinstance(p, str)],
                "restarting": [p for p in s.get("restarting") or [] if isinstance(p, str)],
                "restarted": s.get("restarted"),
                "lanes": lanes,
                "models": [{"id": m, "label": MODEL_LABELS.get(m, m)} for m in cliengine.MODELS],
                "efforts": [{"id": e, "label": EFFORT_LABELS.get(e, e)} for e in cliengine.EFFORTS],
                "keys": keys, "git": git, "project_keys": project_keys,
                "done": {k: v for k, v in (s.get("done") or {}).items() if isinstance(v, dict)}}

    # --- what the owner changes ------------------------------------------------------------------------------------

    async def post(self, request: web.Request) -> web.Response:
        op = next((known for known in OPS if known == request.match_info["op"]), None)  # one of ours, not the URL's
        if op is None:
            return _error(404, "Not found.")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(400, "Not a request.")
        try:
            req = self.request_of(op, body)
        except Refused as refusal:
            return _error(400, str(refusal))
        if self.status() is None:
            return _error(503, "The server's settings service is not installed yet: run deploy/setup.sh once more "
                               "on the server.")
        rid = secrets.token_hex(16)
        try:
            self.drop(rid, {"id": rid, "op": op, **req})
        except OSError as error:
            log.error("a settings request was not written: %s", type(error).__name__)
            return _error(503, "The server could not take the change. Try again in a minute.")
        log.info("settings: %s request %s", op, rid)  # never what it carries
        return web.json_response({"id": rid}, status=202)

    async def revoke(self, request: web.Request) -> web.Response:
        """A device taken back at once: its token opens nothing at its next call, its sessions leave the list. One
        taken back already is done as well."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not DEVICE.fullmatch(name):
            return _error(400, "Not a device's name.")
        try:
            await self.platform.collections.revoke_device(name, self.platform.clock())
        except Rejected as refusal:
            return _error(400, refusal.reason[:1].upper() + refusal.reason[1:] + ".")
        except StoreRefused:
            return _error(503, "The server could not take the device back. Try again in a minute.")
        self.platform.computers.forget(name)
        log.info("settings: a device taken back")  # never which
        return web.json_response({"revoked": True})

    def request_of(self, op: str, body: dict) -> dict:
        """The request as the host takes it, checked here first so the owner hears at once what is wrong; the host
        checks it again, by its own lists."""
        value = body.get("value")
        clear = body.get("clear") is True
        if op == "key":
            key = BY_NAME.get(body.get("name"))  # type: ignore[arg-type]
            if key is None:
                raise Refused("Settings does not set this key.")
            if clear:
                if not key["clear"]:
                    raise Refused("This key cannot be removed: Retinue needs it.")
                return {"name": key["name"], "clear": True}
            return {"name": key["name"], "value": _value(value, key["shape"])}
        if op == "git":
            key = body.get("key")
            if not isinstance(key, str) or not GIT_KEY.fullmatch(key):
                raise Refused("An account's key is a-z, 0-9 and _, as its token: in platform.yaml.")
            return {"key": key, "value": _value(value, GIT_VALUE.pattern)}
        if op == "project_key":
            name = body.get("name")
            if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
                raise Refused("A project key's name is A-Z, 0-9 and _.")
            return {"name": name, "value": _value(value, PROJECT_VALUE.pattern)}
        if op == "lane":
            lane = self.cfg.lanes.get(body.get("lane"))  # type: ignore[arg-type]
            if lane is None or lane.retired:
                raise Refused("There is no such lane.")
            out: dict = {"lane": lane.name}
            for name, allowed in (("model", cliengine.MODELS), ("effort", cliengine.EFFORTS)):
                if name in body:
                    if body[name] not in allowed:
                        raise Refused(f"{name.capitalize()} is one of {', '.join(allowed)}.")
                    out[name] = body[name]
            if len(out) == 1:
                raise Refused("Nothing to change.")
            return out
        return {}  # restart: nothing else in it

    def drop(self, rid: str, req: dict) -> None:
        """One file, its name the request's id, written beside under a dot name and renamed: the host never reads a
        half-written one. Mode 600 in a folder of mode 700, both the platform's: no one else reads it before root."""
        inbox = Path(self.cfg.settings_inbox)
        tmp = inbox / f".{rid}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(req, out, separators=(",", ":"))
            os.replace(tmp, inbox / f"{rid}.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


class Refused(ValueError):
    pass


def _value(value: object, shape: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(shape, value.strip()):
        raise Refused(BAD_VALUE)
    return value.strip()  # a copied key often brings a newline or a space along


def _error(status: int, error: str) -> web.Response:
    return web.json_response({"error": error}, status=status)
