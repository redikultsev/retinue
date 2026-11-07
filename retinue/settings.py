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

A repository is added the same way (`repo`): its address, and its account's token when that account has none yet;
root writes one line into `code: repos:` and the section itself when there is none, and the status lists what the
file holds, so a repository added shows before the restart that puts it in use.

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
from .config import REPO_NAME, REPO_URL

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
AUTHOR = re.compile(r"[^<>\n]+ <[^<>@\s]+@[^<>\s]+>")  # retinue/config.py's check of `code: author`
AUTHOR_NAME, AUTHOR_EMAIL = re.compile(r"[^<>\n]+"), re.compile(r"[^<>@\s]+@[^<>\s]+>")  # its halves: author_ok
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
OPS = ("key", "git", "project_key", "lane", "repo", "restart")
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
        in_use = {repo.name for repo in (self.cfg.code.repos if self.cfg.code else [])}
        added: dict[str, list[str]] = {}  # in the file, in use after the restart
        for entry in file_repos(s):
            if entry["name"] not in in_use:
                found = REPO_URL.fullmatch(entry["url"])
                added.setdefault(entry["token"], []).append(found.group(2) if found else entry["url"])
        git = [{"key": key, "repos": repos.get(key, []), "added": added.get(key, []), **state(git_status.get(key))}
               for key in sorted(set(repos) | set(added) | set(git_status)) if GIT_KEY.fullmatch(key)]
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
                "author_needed": self.author_needed(s),
                "done": {k: v for k, v in (s.get("done") or {}).items() if isinstance(v, dict)}}

    def author_needed(self, status: dict) -> bool:
        """The first repository makes the `code:` section, whose commits need an author unless base: names one."""
        return self.cfg.code is None and not file_repos(status) and not (self.cfg.base and self.cfg.base.author)

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
        if op == "repo":
            return self.repo_request(body)
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

    def repo_request(self, body: dict) -> dict:
        """A repository to add: the address as the file keeps it, a name and an address the platform does not have
        yet, the section's lane, track and author when the file has no `code:` yet, the token when one is given."""
        given = body.get("url")
        url = given.strip() if isinstance(given, str) else ""
        if url and "://" not in url:
            url = "https://" + url  # «github.com/you/repo», as GitHub's page shows it
        url, name, key = repo_of(url)
        status = self.status() or {}
        known = [(r.name, r.url) for r in (self.cfg.code.repos if self.cfg.code else [])]
        known += [(e["name"], e["url"]) for e in file_repos(status)]
        for other, address in known:
            if same_repo(address, url):
                raise Refused(f"This repository is here already, as {other}.")
            if other == name:
                raise Refused(f"A repository named {name} is here already: Settings names it after the address, so "
                              "add this one to platform.yaml by hand.")
        out: dict = {"url": url}
        if self.cfg.code is None and not file_repos(status):
            if "private" not in self.cfg.lanes:
                raise Refused("This server has no room lane «private» for repositories: add code: to platform.yaml "
                              "by hand.")
            out["lane"] = "private"
            out["track"] = "code" if any(t.name == "code" for t in self.cfg.tracks) else ""
            if self.author_needed(status):
                author = body.get("author")
                author = author.strip() if isinstance(author, str) else ""
                if not author_ok(author):
                    raise Refused("Your commits need your name and email: Your Name <you@example.com>.")
                out["author"] = author
        value = body.get("value")
        if isinstance(value, str) and value.strip():
            out["value"] = _value(value, GIT_VALUE.pattern)
        return out

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


def repo_of(url: str) -> tuple[str, str, str]:
    """A repository's address as platform.yaml keeps it (`.git`: GitLab answers 301 without it), its folder's name and
    its token's key — the account's: one token per resource owner. The host derives the same
    (deploy/settings/retinue-settings.py), and tests/test_settings_host.py holds them equal."""
    found = REPO_URL.fullmatch(url)
    if not found or ".." in url:
        raise Refused("Not a repository's address: https://github.com/<owner>/<repo> or "
                      "https://gitlab.com/<group>/<project>.")
    host, path = found.groups()
    name = re.sub(r"[^a-z0-9_-]+", "-", path.rsplit("/", 1)[-1].lower()).strip("-_")[:63]
    key = re.sub(r"[^a-z0-9_]+", "_", path.split("/", 1)[0].lower()).strip("_")[:40]
    if not REPO_NAME.fullmatch(name) or not GIT_KEY.fullmatch(key):
        raise Refused("This address gives no folder name or account key: add it to platform.yaml by hand.")
    return f"https://{host}/{path}.git", name, key


def author_ok(author: str) -> bool:
    """`AUTHOR`, checked in two linear halves: split at its one « <» (the name holds no «<»), so no pattern backtracks
    over a long value. Capped like a line of the file."""
    name, sep, email = author.rpartition(" <")
    return (bool(sep) and len(author) <= 256 and AUTHOR_NAME.fullmatch(name) is not None
            and AUTHOR_EMAIL.fullmatch(email) is not None)


def same_repo(a: str, b: str) -> bool:
    def bare(url: str) -> str:
        return url.lower().removeprefix("https://").rstrip("/").removesuffix(".git")
    return bare(a) == bare(b)


def file_repos(status: dict) -> list[dict]:
    """The repositories platform.yaml holds, as the host reported them: names, addresses and keys, never more."""
    out = []
    for entry in status.get("repos") or []:
        if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("name", "url", "token")):
            out.append({"name": entry["name"], "url": entry["url"], "token": entry["token"]})
    return out


def _error(status: int, error: str) -> web.Response:
    return web.json_response({"error": error}, status=status)
