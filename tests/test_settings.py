"""«Settings» on the platform (retinue/settings.py): `GET /api/settings` says what is set and never a value; a change
is checked, then dropped as one file of mode 600 into the inbox root's service takes it from, and the answer is its
id; without the host's status nothing is taken. Behind the face's login and its POST header like every route."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from retinue import cliengine, settings

from test_face import ORIGIN, FaceWorld, faced
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse

VOICE = "sk_invented_voice_key_0123456789"
NOW = 1_800_000_000


def status(**over) -> dict:
    return {"schema": 1, "at": NOW, "profiles": ["platform"],
            "keys": {"CLAUDE_CODE_OAUTH_TOKEN": {"set": True, "changed": None},
                     "ELEVENLABS_API_KEY": {"set": False, "changed": None}},
            "git": {"example_gitlab": {"set": True, "changed": NOW - 60}},
            "project_keys": {}, "lanes": {"code": {"model": "claude-sonnet-5-5"}},
            "pending": ["platform"], "restarted": None, "changed": {},
            "done": {"a" * 32: {"ok": False, "at": NOW, "why": "The token was not written: not a token"}}, **over}


def wire(world: FaceWorld, written: dict | None) -> tuple:
    inbox, status_file = world.tmp / "settings" / "inbox", world.tmp / "settings" / "status" / "status.json"
    inbox.mkdir(parents=True)
    status_file.parent.mkdir(parents=True)
    if written is not None:
        status_file.write_text(json.dumps(written))
    world.cfg.settings_inbox, world.cfg.settings_status = str(inbox), str(status_file)
    return inbox, status_file


def requests_in(inbox) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(inbox.iterdir())]


def test_settings_need_a_login_and_a_post_its_header(tmp_path):
    async def body(world: FaceWorld):
        inbox, _ = wire(world, status())
        assert (await world.call("GET", "/api/settings"))[0] == 401
        assert (await world.call("POST", "/api/settings/key", {"name": "ELEVENLABS_API_KEY", "value": VOICE}))[0] == 401
        await world.login()
        assert (await world.call("POST", "/api/settings/key", {"name": "ELEVENLABS_API_KEY", "value": VOICE},
                                 headers={"Origin": ORIGIN}))[0] == 403
        assert list(inbox.iterdir()) == []

    faced(tmp_path, body)


def test_the_view_says_what_is_set_and_how_the_lanes_run_and_never_a_value(tmp_path):
    async def body(world: FaceWorld):
        wire(world, status())
        await world.login()
        view = await world.get("/api/settings")
        assert view["ready"] is True and view["pending"] == ["platform"]
        assert [k["name"] for k in view["keys"]] == [k["name"] for k in settings.KEYS]
        keys = {k["name"]: k for k in view["keys"]}
        assert keys["CLAUDE_CODE_OAUTH_TOKEN"]["set"] is True and keys["ELEVENLABS_API_KEY"]["set"] is False
        assert keys["TELEGRAM_BOT_TOKEN"]["set"] is False, "a key the status does not name is not set"
        assert keys["TELEGRAM_BUSINESS_TOKEN"]["on"] is False, "its profile is off on this server"
        assert all({"what", "where", "hint", "title"} <= set(k) and "shape" not in k and "value" not in k
                   for k in view["keys"])
        assert view["git"] == [{"key": "example_gitlab", "repos": [], "set": True, "changed": NOW - 60}]
        lanes = {lane["name"]: lane for lane in view["lanes"]}
        assert lanes["code"]["model"] == "claude-sonnet-5-5", "what the file says now"
        assert lanes["code"]["running"] == {"model": cliengine.MODEL, "effort": cliengine.EFFORT}, \
            "what it runs with until the restart"
        assert lanes["private"]["model"] == cliengine.MODEL and lanes["code"]["effort"] == cliengine.EFFORT
        assert [m["id"] for m in view["models"]] == list(cliengine.MODELS)
        assert [e["id"] for e in view["efforts"]] == list(cliengine.EFFORTS)
        assert view["done"]["a" * 32]["why"].startswith("The token was not written")

    faced(tmp_path, body)


def test_a_key_is_dropped_once_for_root_and_never_answered_back(tmp_path):
    async def body(world: FaceWorld):
        inbox, _ = wire(world, status())
        await world.login()
        code, answer, _ = await world.call("POST", "/api/settings/key",
                                           {"name": "ELEVENLABS_API_KEY", "value": f" {VOICE}\n"})
        assert code == 202 and set(answer) == {"id"} and len(answer["id"]) == 32
        [path] = list(inbox.iterdir())
        assert path.name == f"{answer['id']}.json", "no half-written file left beside it"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 or sys.platform == "win32"
        assert json.loads(path.read_text()) == {"id": answer["id"], "op": "key", "name": "ELEVENLABS_API_KEY",
                                                "value": VOICE}, "a copied newline or space is taken off"
        assert VOICE not in json.dumps(await world.get("/api/settings"))
        assert (await world.call("POST", "/api/settings/lane", {"lane": "code", "model": "claude-opus-5-5"}))[0] == 202
        assert (await world.call("POST", "/api/settings/restart", {}))[0] == 202
        assert [(r["op"], r.get("model")) for r in requests_in(inbox) if r["op"] != "key"] in (
            [("lane", "claude-opus-5-5"), ("restart", None)], [("restart", None), ("lane", "claude-opus-5-5")])

    faced(tmp_path, body)


@pytest.mark.parametrize("op, body, why", [
    ("key", {"name": "RETINUE_BUS_SECRET", "value": "x" * 40}, "Settings does not set this key."),
    ("key", {"name": "ELEVENLABS_API_KEY", "value": "short"}, settings.BAD_VALUE),
    ("key", {"name": "ELEVENLABS_API_KEY", "value": VOICE + "\nRETINUE_BUS_SECRET=x"}, settings.BAD_VALUE),
    ("key", {"name": "TELEGRAM_BOT_TOKEN", "value": "not-a-bot-token-at-all-0123456789"}, settings.BAD_VALUE),
    ("key", {"name": "CLAUDE_CODE_OAUTH_TOKEN", "clear": True}, "This key cannot be removed: Retinue needs it."),
    ("git", {"key": "../x", "value": "github_pat_x"}, "a-z, 0-9 and _"),
    ("git", {"key": "you", "value": "has space"}, settings.BAD_VALUE),
    ("project_key", {"name": "booking", "value": "x" * 20}, "A-Z, 0-9 and _"),
    ("lane", {"lane": "nowhere", "model": "claude-opus-5-5"}, "There is no such lane."),
    ("lane", {"lane": "code", "model": "claude-haiku-5-5"}, "Model is one of claude-opus-5-5[1m]"),
    ("lane", {"lane": "code", "effort": "ultra"}, "Effort is one of low, medium, high, xhigh, max."),
    ("lane", {"lane": "code"}, "Nothing to change."),
])
def test_what_settings_does_not_take_is_refused_in_words_and_nothing_is_dropped(tmp_path, op, body, why):
    async def go(world: FaceWorld):
        inbox, _ = wire(world, status())
        await world.login()
        code, answer, _ = await world.call("POST", f"/api/settings/{op}", body)
        assert code == 400 and why in answer["error"]
        assert list(inbox.iterdir()) == []

    faced(tmp_path, go)


def test_without_the_hosts_service_nothing_is_taken_and_the_view_says_so(tmp_path):
    async def body(world: FaceWorld):
        inbox, status_file = wire(world, None)
        await world.login()
        view = await world.get("/api/settings")
        assert view["ready"] is False and all(k["set"] is False for k in view["keys"])
        code, answer, _ = await world.call("POST", "/api/settings/key", {"name": "ELEVENLABS_API_KEY", "value": VOICE})
        assert code == 503 and "setup.sh" in answer["error"] and list(inbox.iterdir()) == []
        status_file.write_text(json.dumps(status(schema=2)))
        assert (await world.get("/api/settings"))["ready"] is False, "a status of another schema is not read"

    faced(tmp_path, body)


def test_a_request_file_is_written_beside_and_renamed(tmp_path, monkeypatch):
    """The host never reads a half-written request: it takes `<id>.json` alone, and the platform writes a dot name
    first. A failed write leaves nothing behind."""
    async def body(world: FaceWorld):
        inbox, _ = wire(world, status())
        await world.login()
        real = os.replace

        def failing(src, dst):
            raise OSError("disk full")
        monkeypatch.setattr(settings.os, "replace", failing)
        code, _, _ = await world.call("POST", "/api/settings/key", {"name": "ELEVENLABS_API_KEY", "value": VOICE})
        assert code == 503 and list(inbox.iterdir()) == []
        monkeypatch.setattr(settings.os, "replace", real)

    faced(tmp_path, body)
