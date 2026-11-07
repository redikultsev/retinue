"""deploy/settings/retinue-settings.py, root's half of «Settings», run as the service runs it: a server's files under
a temporary RETINUE_ROOT, the repository's own set-git-token.sh, and a `docker` that writes down what it was asked.
A value is written where the stack reads it and nowhere else: not in the status, not in what the script prints."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from retinue import cliengine, settings

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "settings" / "retinue-settings.py"
CLAUDE = "sk-ant-oat01-invented-example-token-0123456789"
VOICE = "sk_invented_voice_key_0123456789"
BOT = "123456789:AAinventedExampleBotToken_0123456789ab"
GIT = "github_pat_invented_example_0123456789"
PLATFORM_YAML = """\
state_db: /platform/platform.sqlite
lanes:
  private:
    url: http://lane-private:9600
    hook_url: http://platform:9500/hook
    deny: []
    # model: "claude-opus-5-5[1m]"
# A track: ...
tracks:
  - {name: notes, lane: private, kind: private, cwd: base}
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the host's script runs on Linux (flock, O_NOFOLLOW)")


def host_module():
    spec = importlib.util.spec_from_file_location("retinue_settings_host", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Server:
    """A server's /srv/retinue in a temporary folder, and the script run on it."""

    def __init__(self, tmp: Path, profiles: str = "platform") -> None:
        self.root, self.calls = tmp / "srv", tmp / "docker-calls"
        (self.root / "settings" / "inbox").mkdir(parents=True)
        (self.root / "platform").mkdir()
        self.stack = self.root / "stack.env"
        self.stack.write_text(f"COMPOSE_PROFILES={profiles}\nCLAUDE_CODE_OAUTH_TOKEN={CLAUDE}\n"
                              f"TELEGRAM_BOT_TOKEN=\nELEVENLABS_API_KEY=\n")
        self.yaml = self.root / "platform" / "platform.yaml"
        self.yaml.write_text(PLATFORM_YAML)
        self.docker = tmp / "docker"
        self.docker.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{self.calls}"\n'
                               '[[ -z ${DOCKER_FAILS:-} ]] || { echo "Error: image not found" >&2; exit 1; }\n')
        self.docker.chmod(0o755)
        self.out = ""

    def drop(self, request: dict, rid: str | None = None) -> str:
        rid = rid or os.urandom(16).hex()
        (self.root / "settings" / "inbox" / f"{rid}.json").write_text(json.dumps({"id": rid, **request}))
        return rid

    def run(self, **env: str) -> dict:
        done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(self.root),
                                   "RETINUE_REPO": str(REPO), "DOCKER": str(self.docker), **env})
        assert done.returncode == 0, done.stderr
        self.out = done.stdout + done.stderr
        return self.status()

    def status(self) -> dict:
        return json.loads((self.root / "settings" / "status" / "status.json").read_text())

    def text_of_status(self) -> str:
        return (self.root / "settings" / "status" / "status.json").read_text()

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def inbox(self) -> list[str]:
        return sorted(p.name for p in (self.root / "settings" / "inbox").iterdir())


def env_of(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_a_key_is_written_where_the_stack_reads_it_and_nothing_tells_its_value(tmp_path):
    server = Server(tmp_path)
    rid = server.drop({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE})
    status = server.run()
    assert env_of(server.stack)["ELEVENLABS_API_KEY"] == VOICE
    assert env_of(server.stack)["CLAUDE_CODE_OAUTH_TOKEN"] == CLAUDE, "the other lines are left as they were"
    assert stat.S_IMODE(server.stack.stat().st_mode) == 0o600
    assert server.inbox() == [], "the request is deleted"
    assert status["done"][rid]["ok"] is True
    assert status["keys"]["ELEVENLABS_API_KEY"]["set"] is True and status["keys"]["ELEVENLABS_API_KEY"]["changed"]
    assert status["keys"]["TELEGRAM_BOT_TOKEN"]["set"] is False
    assert status["pending"] == ["platform", "router"], "who reads it waits for a restart"
    assert stat.S_IMODE((server.root / "settings" / "status" / "status.json").stat().st_mode) == 0o644
    for told in (server.text_of_status(), server.out):
        assert VOICE not in told and CLAUDE not in told
        assert hashlib.sha256(VOICE.encode()).hexdigest() not in told
    assert server.docker_calls() == [], "nothing restarts before the owner presses Restart"


def test_a_service_whose_part_is_off_is_not_waited_on(tmp_path):
    server = Server(tmp_path, profiles="")
    server.drop({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE})
    assert server.run()["pending"] == ["router"], "the platform is off here: it reads the key when it is turned on"


def test_a_new_bot_token_makes_the_apps_telegram_key_as_setup_does(tmp_path):
    server = Server(tmp_path, profiles="")
    with server.stack.open("a") as out:
        out.write("RETINUE_FACE_ORIGIN=https://app.example.com\n")
    server.drop({"op": "key", "name": "TELEGRAM_BOT_TOKEN", "value": BOT})
    status = server.run()
    written = env_of(server.stack)
    assert written["TELEGRAM_BOT_TOKEN"] == BOT
    assert written["RETINUE_WEBAPP_KEY"] == hmac.new(b"WebAppData", BOT.encode(), hashlib.sha256).hexdigest()
    assert status["pending"] == ["router"], "the platform is off: only the router waits"
    assert written["RETINUE_WEBAPP_KEY"] not in server.text_of_status()


@pytest.mark.parametrize("request_, why", [
    ({"op": "key", "name": "RETINUE_BUS_SECRET", "value": "x" * 40}, "Settings does not set this key"),
    ({"op": "key", "name": "ELEVENLABS_API_KEY", "value": "short"}, "does not look like the right key"),
    ({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE + "\nRETINUE_BUS_SECRET=x"},
     "does not look like the right key"),
    ({"op": "key", "name": "CLAUDE_CODE_OAUTH_TOKEN", "clear": True}, "cannot be removed"),
    ({"op": "git", "key": "../etc", "value": GIT}, "a-z, 0-9 and _"),
    ({"op": "project_key", "name": "booking", "value": "x" * 20}, "A-Z, 0-9 and _"),
    ({"op": "lane", "lane": "private", "model": "claude-haiku-5-5"}, "is not one of"),
    ({"op": "lane", "lane": "private", "effort": "ultra"}, "is not one of"),
    ({"op": "lane", "lane": "private"}, "Nothing to change"),
    ({"op": "lane", "lane": "room", "model": "claude-opus-5-5"}, "There is no lane room"),
    ({"op": "shell", "command": "id"}, "Not a request this script takes"),
])
def test_what_is_not_on_the_lists_is_refused_in_words_and_nothing_is_written(tmp_path, request_, why):
    server = Server(tmp_path)
    stack, yaml = server.stack.read_text(), server.yaml.read_text()
    rid = server.drop(request_)
    status = server.run()
    assert status["done"][rid]["ok"] is False and why in status["done"][rid]["why"]
    assert status["done"][rid]["why"][0].isupper(), "the app shows it as it is"
    assert server.stack.read_text() == stack and server.yaml.read_text() == yaml
    assert status["pending"] == [] and server.inbox() == []
    assert "x" * 20 not in server.text_of_status()


def test_an_optional_key_is_removed_and_says_not_set(tmp_path):
    server = Server(tmp_path)
    server.stack.write_text(server.stack.read_text().replace("ELEVENLABS_API_KEY=", f"ELEVENLABS_API_KEY={VOICE}"))
    rid = server.drop({"op": "key", "name": "ELEVENLABS_API_KEY", "clear": True})
    status = server.run()
    assert status["done"][rid]["ok"] and env_of(server.stack)["ELEVENLABS_API_KEY"] == ""
    assert status["keys"]["ELEVENLABS_API_KEY"]["set"] is False


def test_a_link_or_a_stray_file_in_the_inbox_is_never_followed_and_is_cleared(tmp_path):
    server = Server(tmp_path)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE}))
    rid = os.urandom(16).hex()
    (server.root / "settings" / "inbox" / f"{rid}.json").symlink_to(elsewhere)
    stray, fresh = server.root / "settings" / "inbox" / "old.txt", server.root / "settings" / "inbox" / ".a.tmp"
    stray.write_text("x")
    fresh.write_text("x")
    os.utime(stray, (time.time() - 3600, time.time() - 3600))
    status = server.run()
    assert env_of(server.stack)["ELEVENLABS_API_KEY"] == "", "a link is not read"
    assert rid not in status["done"] and elsewhere.exists()
    assert server.inbox() == [".a.tmp"], "the link and the old stray file are gone; one being written is left"


def test_a_git_token_goes_through_set_git_token_sh(tmp_path):
    server = Server(tmp_path)
    rid = server.drop({"op": "git", "key": "example_gitlab", "value": GIT})
    status = server.run()
    tokens = server.root / "code" / "tokens.env"
    assert env_of(tokens) == {"RETINUE_GIT_TOKEN_EXAMPLE_GITLAB": GIT}
    assert stat.S_IMODE(tokens.stat().st_mode) == 0o600
    assert status["done"][rid]["ok"] and status["git"]["example_gitlab"]["set"] is True
    assert status["pending"] == ["platform"]
    assert GIT not in server.text_of_status() and GIT not in server.out
    for bad_value in ("a" * 513, "has space", "a=b"):  # what set-git-token.sh refuses, refused before it
        bad = server.drop({"op": "git", "key": "example_gitlab", "value": bad_value})
        assert server.run()["done"][bad]["ok"] is False
    assert env_of(tokens) == {"RETINUE_GIT_TOKEN_EXAMPLE_GITLAB": GIT}


def test_a_projects_key_is_written_under_its_name(tmp_path):
    server = Server(tmp_path)
    value = "invented-booking-key-0123456789"
    rid = server.drop({"op": "project_key", "name": "BOOKING", "value": value})
    status = server.run()
    assert env_of(server.root / "egress" / "project-keys.env") == {"RETINUE_PROJECT_KEY_BOOKING": value}
    assert status["done"][rid]["ok"] and status["project_keys"]["BOOKING"]["set"] is True
    assert value not in server.text_of_status()


def test_a_lanes_model_and_effort_are_written_in_its_block_and_nothing_else_moves(tmp_path):
    server = Server(tmp_path)
    server.drop({"op": "lane", "lane": "private", "model": "claude-sonnet-5-5"}, rid="1" * 32)
    server.drop({"op": "lane", "lane": "private", "effort": "high"}, rid="2" * 32)
    status = server.run()
    want = PLATFORM_YAML.replace('    # model: "claude-opus-5-5[1m]"\n',
                                 '    model: "claude-sonnet-5-5"\n    effort: "high"\n')
    assert server.yaml.read_text() == want, \
        "the commented model is replaced in its place, the effort added after the lane's last field"
    assert status["lanes"] == {"private": {"model": "claude-sonnet-5-5", "effort": "high"}}
    assert status["pending"] == ["platform"]
    import yaml
    assert yaml.safe_load(server.yaml.read_text())["lanes"]["private"]["model"] == "claude-sonnet-5-5"


def test_a_lane_written_on_one_line_is_the_owners_to_edit(tmp_path):
    server = Server(tmp_path)
    server.yaml.write_text("lanes:\n  private: {url: http://lane-private:9600}\ntracks: []\n")
    rid = server.drop({"op": "lane", "lane": "private", "model": "claude-opus-5-5"})
    status = server.run()
    assert status["done"][rid]["ok"] is False and "one line" in status["done"][rid]["why"]


def test_a_restart_recreates_the_waiting_services_alone_after_the_writes(tmp_path):
    server = Server(tmp_path)
    server.drop({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE})
    restart = server.drop({"op": "restart"}, rid="0" * 32)  # first by name: still done after the write
    status = server.run()
    stack, compose = server.root / "stack.env", REPO / "deploy" / "compose.yml"
    assert server.docker_calls() == [
        f"compose -p retinue --env-file {stack} -f {compose} up -d --no-deps --force-recreate router platform"]
    assert status["done"][restart] == {"ok": True, "at": status["done"][restart]["at"],
                                       "services": ["router", "platform"]}
    assert status["pending"] == [] and status["restarted"]


def test_a_failed_restart_keeps_what_waits_and_says_why(tmp_path):
    server = Server(tmp_path)
    server.drop({"op": "key", "name": "ELEVENLABS_API_KEY", "value": VOICE})
    server.run()
    restart = server.drop({"op": "restart"})
    status = server.run(DOCKER_FAILS="1")
    assert status["done"][restart]["ok"] is False
    assert status["done"][restart]["why"] == "The restart failed: Error: image not found"
    assert status["pending"] == ["platform", "router"]


def test_a_restart_with_nothing_waiting_runs_nothing(tmp_path):
    server = Server(tmp_path)
    restart = server.drop({"op": "restart"})
    assert server.run()["done"][restart]["ok"] is True
    assert server.docker_calls() == []


def test_the_hosts_lists_are_the_platforms():
    """The script runs with the host's python3 and imports nothing of Retinue: its copies of the lists are kept
    equal to the platform's here."""
    host = host_module()
    assert host.MODELS == cliengine.MODELS and host.EFFORTS == cliengine.EFFORTS
    assert set(host.KEYS) == set(settings.BY_NAME)
    for name, spec in host.KEYS.items():
        assert spec["shape"] == settings.BY_NAME[name]["shape"], name
        assert spec["clear"] == settings.BY_NAME[name]["clear"], name
        assert all(s in host.SERVICES for s in spec["services"])
    assert host.GIT_KEY.pattern == settings.GIT_KEY.pattern
    assert host.PROJECT_NAME.pattern == settings.PROJECT_NAME.pattern
    assert host.PROJECT_VALUE.pattern == settings.PROJECT_VALUE.pattern
    git_script = (REPO / "deploy" / "code" / "set-git-token.sh").read_text()
    assert "^" + host.GIT_KEY.pattern + "$" in git_script, "set-git-token.sh checks its key the same way"
    assert set(host.SERVICES) == {"router", "assistant", "platform", "tg-business"}
    compose = (REPO / "deploy" / "compose.yml").read_text()
    for service, profile in host.SERVICES.items():
        assert re.search(rf"^  {re.escape(service)}:", compose, re.M), service
        if profile:
            block = compose.split(f"\n  {service}:\n", 1)[1].split("\n  ", 1)[0]
            assert block.startswith(f'    profiles: ["{profile}"]'), (service, profile)
