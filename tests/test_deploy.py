"""What gets deployed: deploy/compose.yml and the files deploy/setup.sh writes. No Docker and no root needed."""

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from retinue import cliengine, lane_host
from retinue.bus import bus_token
from retinue.config import AgentConfig, PlatformConfig, RouterConfig

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())
SERVICES = COMPOSE["services"]


def setup(tmp_path, **env):
    """Run deploy/setup.sh into a temporary folder; return (folder, the stack environment it wrote).
    A value it wrote never appears in what it prints: the terminal ends up in logs and transcripts."""
    target = tmp_path / "srv"
    run = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0", **env})
    assert run.returncode == 0, run.stderr
    stack = target / "stack.env"
    assert oct(stack.stat().st_mode & 0o777) == "0o600"
    written = dict(line.split("=", 1) for line in stack.read_text().splitlines() if "=" in line)
    leaked = [name for name, value in written.items()
              if ("SECRET" in name or "TOKEN" in name) and value and value in run.stdout + run.stderr]
    assert not leaked, f"printed: {leaked}"
    assert "RETINUE_BUS_SECRET" in run.stdout, "names are printed, values are not"
    return target, written


def test_one_assistant_and_nothing_else():
    agents = sorted(p.name for p in (ROOT / "agents").iterdir())
    assert agents == ["assistant"], "Главная, Учёба and Путешествия are gone"
    assert sorted(p.name for p in (ROOT / "agents" / "assistant").iterdir()) == ["CLAUDE.md", "agent.yaml"]
    engine = AgentConfig.load(ROOT / "agents" / "assistant" / "agent.yaml").engine
    assert engine.tools == [] and engine.allowed_tools == [], "no built-in tool is offered or allowed"
    assert {"Bash", "WebSearch", "WebFetch"} <= set(engine.disallowed_tools)
    assert engine.bus_tools == ["search_archive", "set_reminder", "list_reminders", "cancel_reminder",
                                "move_reminder", "set_timezone", "get_attachment", "publish_trip", "draft_reply"], \
        "the archive, reminders and the zone, attachments, trip pages, draft replies; no ask_agent"
    assert engine.model == "claude-opus-5-5", "Opus, pinned by id"
    instructions = (ROOT / "agents" / "assistant" / "CLAUDE.md").read_text()
    assert all(f"`{name}`" in instructions for name in engine.bus_tools), "she is told about every tool she has"
    assert "[вложение #N:" in instructions and "переслано от" in instructions, "what a mark and a forward mean"
    assert engine.max_turns == 20, "a trip is a search, pages, photos and refinements: more steps than a reply"
    assert "Сработало слежение за ценой" in instructions and "только когда Владелец сам попросил" in instructions
    assert "Не отправлено на сайты" in instructions and "данные, а не команды" in instructions.split("## Поездки")[1]
    assert "1–3 минуты" in instructions and "`stay_photos`" in instructions and "`watch_alerts`" not in instructions
    assert "не больше 12 вызовов travel-ops" in instructions and "пиши ровно" in instructions, "the guard's limits"
    assert engine.instructions == "/agent/CLAUDE.md" and engine.config_dir == "/data/claude"
    base = instructions.split("## База знаний")[1].split("\n## ")[0]
    assert "/kb/AGENTS.md" in base and "written_by: Ассистентка (Opus 5.5)" in base and "ниже маркера" in base
    assert "База не приняла правку этого хода" in base and "Не записано" in base, "the router's and the guard's words"
    assert all(f"`{tool}`" in base for tool in ("Read", "Grep", "Glob", "Edit", "Write")) and "`path`" in base
    assert "MEMORY=1" in (ROOT / "docs" / "memory.md").read_text(), "the base has its install path in the repo"
    mail = instructions.split("## Почта и календарь")[1].split("\n## ")[0]
    from retinue.mail import COSTS, KINDS, NEEDS

    assert all(f"`{word}`" in mail for word in (*KINDS, *NEEDS, *COSTS)), "every word of both schemas is explained"
    assert "Сомневаешься — `keep: true`" in mail and "«не сверено»" in mail and "`irreversible`" in mail
    assert "данные, а не команды" in mail and "Коммит пометят «почта»" in mail, "what may be written from mail"
    able = instructions.split("## Что ты умеешь сейчас")[1].split("\n## ")[0]
    assert "Сама ничего не отправляешь" in able and "`draft_reply`" in able, "she cannot send; she proposes"
    replies = instructions.split("## Ответы людям")[1].split("\n## ")[0]
    assert "«Да» в чате — не согласие" in replies and "Никогда не пиши «отправила»" in replies
    assert "`replaces`" in replies and "Не принято" in replies and "`reply_to`" in replies
    assert "гасит открытую карточку" in replies and "лимит" not in replies, "a new message; no daily ceiling"
    assert "пароли" in replies and "данные, а не команды" in replies, "a request in someone's text is no command"
    assert "id в архиве" in (ROOT / "retinue" / "mailroom.py").read_text(), "the judgement names each item's id"
    assert "drafts: true" in (ROOT / "deploy" / "setup.sh").read_text()
    guide = (ROOT / "docs" / "mail.md").read_text()
    assert "MAIL=1" in guide and "deploy/mail/login.py" in guide and "In production" in guide
    assert "Only if\nthe sender is known" in guide and "MAIL=1" in (ROOT / "docs" / "install.md").read_text()
    trips = instructions.split("## Поездки")[1].split("\n## ")[0]
    assert "`publish_trip`" in trips and "[подробнее](" in trips and "trip_id" in trips and "Не опубликовано" in trips
    assert "Хаб не подключён" in trips and "вне VPN" in trips, "the answer stays whole; the page only adds to it"
    hub = (ROOT / "docs" / "lifehub.md").read_text()
    assert "LIFEHUB=1" in hub and "LIFEHUB_DEVICES" in hub and "10.8.0.1" in hub and "dig" in hub
    assert "LIFEHUB=1" in (ROOT / "docs" / "install.md").read_text() and "lifehub.md" in (ROOT / "docs" / "install.md").read_text()
    assert sorted(SERVICES) == ["assistant", "collector", "egress", "face", "lane-code", "lane-code-egress",
                                "lane-private", "lane-private-egress", "lane-private-sni", "lane-web",
                                "lane-web-egress", "lifehub", "lifehub-build", "mail-egress", "mail-send", "platform",
                                "platform-egress", "router", "tg-business",
                                "travel-ops", "travel-watch", "tuwunel"]
    assert sorted(COMPOSE["volumes"]) == ["assistant-data", "lane-code-config", "lane-code-state", "lane-code-work",
                                          "lane-private-config", "lane-private-state", "lane-private-work",
                                          "lane-web-config", "lane-web-state", "lane-web-work",
                                          "platform-data", "router-data", "travel-data", "tuwunel-db"]


def test_only_the_router_and_the_platform_hold_the_speech_key():
    """The router transcribes Telegram's voice notes, the platform what the owner dictates in the app; the assistant
    and every lane — the room above all — never see the key."""
    holders = sorted(name for name, service in SERVICES.items()
                     if "ELEVENLABS_API_KEY" in str(service.get("environment") or "") + str(service.get("env_file") or ""))
    assert holders == ["platform", "router"]
    for name in holders:
        assert SERVICES[name]["environment"]["ELEVENLABS_API_KEY"] == "${ELEVENLABS_API_KEY:-}"


def test_assistant_container_cannot_write_its_settings_and_keeps_only_its_session():
    assistant = SERVICES["assistant"]
    assert assistant["volumes"] == ["/srv/retinue/agents/assistant:/agent:ro", "assistant-data:/data",
                                    "/srv/retinue/memory/tree:/kb", "/srv/retinue/memory/policy.json:/memory-policy.json:ro"], \
        "settings read-only; the session transcript on its own volume; the base's files, not its repository"
    assert "tmpfs" not in assistant
    assert "CLAUDE_CONFIG_DIR" not in assistant["environment"] and "ANTHROPIC_API_KEY" not in assistant["environment"]
    router_mounts = " ".join(SERVICES["router"]["volumes"])
    assert "router-data:/data" in router_mounts and "router-data" not in str(assistant), \
        "the archive file is the router's alone"


def test_agents_network_is_closed_and_the_proxy_is_the_only_way_out():
    assert COMPOSE["networks"]["agents"] == {"internal": True}
    assert SERVICES["assistant"]["networks"] == ["agents", "travel-assistant"], "two internal networks and no other"
    assert "ports" not in SERVICES["assistant"] and "network_mode" not in SERVICES["assistant"]
    assert SERVICES["assistant"]["environment"]["HTTPS_PROXY"] == "http://egress:3128"
    assert set(SERVICES["egress"]["networks"]) == {"agents", "outside"} and "ports" not in SERVICES["egress"]
    assert all(v.endswith(":ro") for v in SERVICES["egress"]["volumes"])
    assert {"agents", "outside"} <= set(SERVICES["router"]["networks"]), "the router reaches agents and Telegram"
    conf = (ROOT / "deploy" / "egress" / "squid.conf").read_text()
    rules = [line.split("#")[0].strip() for line in conf.splitlines() if line.startswith("http_access")]
    assert rules == ["http_access deny !CONNECT", "http_access deny CONNECT !tls_port",
                     "http_access allow CONNECT allowed", "http_access deny all"], "one allow rule, deny last"
    assert (ROOT / "deploy" / "egress" / "allowed-hosts.txt").read_text().split() == ["api.anthropic.com"]


def test_setup_keeps_the_owners_list_of_hosts(tmp_path):
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    hosts = target / "egress" / "allowed-hosts.txt"
    assert hosts.read_text().split() == ["api.anthropic.com"] and (target / "egress" / "squid.conf").is_file()
    hosts.write_text("api.anthropic.com\nfound-by-the-probe.example\n")
    setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert "found-by-the-probe.example" in hosts.read_text(), "a second run does not overwrite it"


def test_tuwunel_is_off_by_default():
    assert SERVICES["tuwunel"]["profiles"] == ["matrix"]
    assert "tuwunel" not in str(SERVICES["router"].get("depends_on", "")), "the router starts without a homeserver"


def test_setup_without_matrix(tmp_path, monkeypatch):
    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert not (target / "tuwunel").exists() and "COMPOSE_PROFILES" not in printed and "RETINUE_AS_TOKEN" not in printed
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("RETINUE_AS_TOKEN", raising=False)
    cfg = RouterConfig.load(target / "router.yaml")
    assert cfg.matrix is None and cfg.telegram.owner_id == 42 and cfg.default_agent == "assistant"
    assert [(a.id, a.archive, a.reminders, a.attachments, a.trips, a.can_call) for a in cfg.agents] == [
        ("assistant", True, True, True, True, [])], "trip pages: the router refuses them while there is no hub"
    assert cfg.owner_tz == "UTC", "until the owner names his zone"
    assert (target / "agents" / "assistant" / "CLAUDE.md").read_text() == (ROOT / "agents" / "assistant" / "CLAUDE.md").read_text()
    assert "${RETINUE_BUS_TOKEN_ASSISTANT:-}" in (ROOT / "deploy" / "compose.yml").read_text()
    for agent in cfg.agents:
        assert printed[f"RETINUE_BUS_TOKEN_{agent.id.upper()}"] == bus_token(printed["RETINUE_BUS_SECRET"], agent.id)
        assert AgentConfig.load(target / "agents" / agent.id / "agent.yaml").public_url == agent.url
    assert printed["TELEGRAM_BOT_TOKEN"] == "" and printed["CLAUDE_CODE_OAUTH_TOKEN"] == "", "left for the owner"
    assert printed["ELEVENLABS_API_KEY"] == "", "pasted by the owner; without it voice is refused aloud"
    stack = target / "stack.env"
    stack.write_text(stack.read_text().replace("TELEGRAM_BOT_TOKEN=\n", "TELEGRAM_BOT_TOKEN=123:owner-pasted\n"))
    again, printed_again = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert printed_again["RETINUE_BUS_SECRET"] == printed["RETINUE_BUS_SECRET"], "a second run keeps the secrets"
    assert printed_again["TELEGRAM_BOT_TOKEN"] == "123:owner-pasted", "and what the owner filled in"
    assert oct((target / "secrets.env").stat().st_mode & 0o777) == "0o600"
    _, rotated = setup(tmp_path, TELEGRAM_OWNER_ID="42", ROTATE_BUS_SECRET="1")
    assert rotated["RETINUE_BUS_SECRET"] != printed["RETINUE_BUS_SECRET"], "a leaked secret is replaced"
    assert rotated["RETINUE_BUS_TOKEN_ASSISTANT"] == bus_token(rotated["RETINUE_BUS_SECRET"], "assistant")
    assert rotated["TELEGRAM_BOT_TOKEN"] == "123:owner-pasted"
    setup(tmp_path, TELEGRAM_OWNER_ID="42", OWNER_TZ="Asia/Tokyo")
    setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert RouterConfig.load(target / "router.yaml").owner_tz == "Asia/Tokyo", "a run without OWNER_TZ keeps the zone"


def test_setup_with_matrix(tmp_path, monkeypatch):
    target, printed = setup(tmp_path, MATRIX_SERVER_NAME="matrix.example.com", MATRIX_OWNER="alice")
    assert printed["COMPOSE_PROFILES"] == "matrix" and printed["MATRIX_SERVER_NAME"] == "matrix.example.com"
    monkeypatch.setenv("RETINUE_AS_TOKEN", printed["RETINUE_AS_TOKEN"])
    monkeypatch.setenv("RETINUE_HS_TOKEN", printed["RETINUE_HS_TOKEN"])
    cfg = RouterConfig.load(target / "router.yaml")
    assert cfg.telegram is None and cfg.matrix.owner == "@alice:matrix.example.com"
    registration = yaml.safe_load((target / "tuwunel" / "appservices" / "retinue.yaml").read_text())
    assert registration["as_token"] == printed["RETINUE_AS_TOKEN"]
    assert registration["namespaces"]["users"][0]["regex"] == r"^@agent_[a-z0-9_]+:matrix\.example\.com$"


def test_setup_refuses_to_run_without_a_channel(tmp_path):
    run = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(tmp_path / "srv"), "RETINUE_HOST_SETUP": "0"})
    assert run.returncode == 1 and "no channel" in run.stderr and not (tmp_path / "srv").exists()

def test_backup_is_off_until_asked_and_then_stays_on(tmp_path):
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert not (target / "backup.env").exists(), "no backup unless the owner asks for it"
    assert (target / "status").is_dir() and oct((target / "status").stat().st_mode & 0o777) == "0o755", \
        "the router mounts it read-only either way"
    run = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                              "TELEGRAM_OWNER_ID": "42", "BACKUP": "1"})
    assert run.returncode == 0, run.stderr
    backup_env = target / "backup.env"
    assert oct(backup_env.stat().st_mode & 0o777) == "0o600"
    names = ["RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"]
    assert [line.split("=")[0] for line in backup_env.read_text().splitlines()] == names
    assert all(f"Fill in {backup_env}: {name}" in run.stdout for name in names)
    filled = {name: f"owner-value-{i}" for i, name in enumerate(names)}
    backup_env.write_text("".join(f"{k}={v}\n" for k, v in filled.items()))
    again = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                "TELEGRAM_OWNER_ID": "42"})
    assert again.returncode == 0, again.stderr
    assert backup_env.read_text() == "".join(f"{k}={v}\n" for k, v in filled.items()), \
        "a later run without BACKUP=1 keeps it, and what the owner filled in"
    assert not any(value in again.stdout + again.stderr for value in filled.values()), "names only, never values"
    assert "Backup: on" in again.stdout


def test_backup_units_run_the_script_nightly_and_catch_up():
    timer = (ROOT / "deploy" / "backup" / "retinue-backup.timer").read_text()
    assert "OnCalendar=*-*-* 03:30 UTC" in timer and "Persistent=true" in timer
    # setup.sh installs it at 03:30 in the owner's zone: the same sed, run on the template.
    script = (ROOT / "deploy" / "setup.sh").read_text()
    [expression] = re.findall(r'-e "(s\|\^OnCalendar=[^"]*)"', script)
    done = subprocess.run(["sed", "-e", expression.replace("$OWNER_TZ", "Asia/Tokyo")], input=timer,
                          capture_output=True, text=True, check=True)
    assert "OnCalendar=*-*-* 03:30 Asia/Tokyo\n" in done.stdout and "03:30 UTC" not in done.stdout
    service = (ROOT / "deploy" / "backup" / "retinue-backup.service").read_text()
    assert "Type=oneshot" in service and "EnvironmentFile=/srv/retinue/backup.env" in service
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/retinue/retinue-backup.py" in service
    assert "/usr/local/lib/retinue/retinue-backup.py" in script and "RESTIC_VERSION=0.19.1" in script
    assert "sha256sum -c" in script, "the restic binary is checked against a pinned hash"


def test_health_units_look_every_two_minutes_and_may_write_only_the_status_folder(tmp_path, monkeypatch):
    timer = (ROOT / "deploy" / "health" / "retinue-health.timer").read_text()
    assert "OnUnitActiveSec=2min" in timer and "OnBootSec=" in timer and "WantedBy=timers.target" in timer
    service = (ROOT / "deploy" / "health" / "retinue-health.service").read_text()
    assert "Type=oneshot" in service and "ExecStart=/usr/bin/python3 /usr/local/lib/retinue/retinue-health.py" in service
    assert "ProtectSystem=strict" in service and "ReadWritePaths=/srv/retinue/status" in service
    assert "TimeoutStartSec=150" in service, "one inspect per container, up to 20 s each when Docker is slow"
    assert "EnvironmentFile" not in service, "no secret: the host sends nothing itself, the router does"
    script = (ROOT / "deploy" / "setup.sh").read_text()
    block = script.split("# Health:")[1].split("\nfi\n")[0]
    assert "retinue-health.py" in block and "enable --now retinue-health.timer" in block
    assert "BACKUP" not in block, "always on, not behind a switch"
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert RouterConfig.load(target / "router.yaml").health_status == "/status/health.json"
    assert "Watch(cfg.health_status, store.db)" in (ROOT / "retinue" / "router.py").read_text()
    assert "retinue-health" in (ROOT / "docs" / "health.md").read_text()


def test_router_reads_the_backup_status_read_only(tmp_path, monkeypatch):
    assert "/srv/retinue/status:/status:ro" in SERVICES["router"]["volumes"], "the host writes it, the router cannot"
    assert "status" not in str(SERVICES["assistant"]["volumes"])
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert RouterConfig.load(target / "router.yaml").backup_status == "/status/backup.json"
    assert "backup_status=cfg.backup_status" in (ROOT / "retinue" / "router.py").read_text()


def test_install_names_every_key_the_owner_pastes():
    import re

    asked = re.findall(r"^\[?.*fill (\w+) ", (ROOT / "deploy" / "setup.sh").read_text(), re.M)
    guides = "".join(page.read_text() for page in (ROOT / "docs").glob("*.md"))
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "ELEVENLABS_API_KEY"} <= set(asked)
    assert [name for name in asked if name not in guides] == [], "the guides say where each one comes from"
    assert "tests/e2e/multimodal.py" in (ROOT / "docs" / "install.md").read_text(), "the probe runs before files"
    assert "TRAVEL=1" in (ROOT / "docs" / "install.md").read_text() and "profile.yml" in guides, "how travel is on"


def test_travel_ops_is_a_neighbour_of_the_assistant_and_the_only_one_with_a_way_out():
    """The assistant reaches travel-ops' MCP on the internal network `travel`; travel-ops reaches the sites through
    its own network; the assistant still has no route out but the model API."""
    members = {net: sorted(name for name, service in SERVICES.items() if net in service.get("networks", []))
               for net in COMPOSE["networks"]}
    # Each link to travel-ops is a bridge of its own with exactly two members: the owner's traffic between the
    # router and the assistant (A2A, the bus) never crosses a bridge travel-ops is on.
    assert members["travel-assistant"] == ["assistant", "travel-ops"]
    assert members["travel-router"] == ["router", "travel-ops"]
    assert members["travel-out"] == ["travel-ops", "travel-watch"]
    assert members["agents"] == ["assistant", "egress", "router"] and "travel" not in COMPOSE["networks"]
    for net in ("travel-assistant", "travel-router"):
        assert COMPOSE["networks"][net] == {"internal": True}, f"{net}: no route out"
    assert "internal" not in (COMPOSE["networks"]["travel-out"] or {}), "travel-ops' way to the sites"
    search, watch = SERVICES["travel-ops"], SERVICES["travel-watch"]
    environment = SERVICES["assistant"]["environment"]
    assert environment["RETINUE_TRAVEL_URL"] == "${RETINUE_TRAVEL_URL:-}" and "travel-ops" in environment["NO_PROXY"]
    assert search["build"]["context"] == "${TRAVEL_OPS_GIT:-}", \
        "built from the owner's own checkout at a pinned commit (stack.env), no address of it in the repo"
    assert search["image"] == watch["image"] == "travel-ops:local" and "build" not in watch
    assert watch["command"] == ["travelops", "watch", "run", "--every-minutes", "30"]
    mounts = ["travel-data:/data", "/srv/retinue/travel/profile.yml:/app/profile.yml:ro"]
    assert search["volumes"] == watch["volumes"] == mounts, "the profile read-only, the data on its own volume"
    assert "travel-data" not in str(SERVICES["router"]) + str(SERVICES["assistant"])
    for service in (search, watch):
        assert service["profiles"] == ["travel"], "off until the owner turns it on: the image is about 5 GB"
        assert "ports" not in service and "environment" not in service, "no published port, no key"
        assert service["shm_size"] == "1gb" and service["mem_limit"] and service["cpus"] and service["pids_limit"]
        # Browsers that open strangers' pages: no capabilities, no way to gain any, a filesystem they cannot change
        # but their scratch space and the data volume.
        assert service["cap_drop"] == ["ALL"] and service["security_opt"] == ["no-new-privileges:true"]
        assert service["read_only"] is True and any(t.startswith("/tmp:") for t in service["tmpfs"])
        # Where Chrome and Camoufox write at start (docker diff of a run on the server), as the travel user.
        scratch = {t.split(":")[0]: t for t in service["tmpfs"]}
        for path in ("/home/travel/.local", "/home/travel/.config", "/home/travel/.cache/google-chrome",
                     "/home/travel/.cache/fontconfig", "/home/travel/.cache/camoufox/fontconfig",
                     "/home/travel/Downloads", "/home/travel/camoufox"):
            assert path in scratch and "uid=10001" in scratch[path], path
        assert "/home/travel/.cache" not in scratch, "Camoufox's Firefox is installed there: a tmpfs would hide it"


def test_the_travel_data_is_backed_up_when_travel_is_on(tmp_path):
    def run(target, **env):
        done = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   "TELEGRAM_OWNER_ID": "42", **env})
        assert done.returncode == 0, done.stderr
        return (target / "backup.env").read_text()

    plain = run(tmp_path / "plain", BACKUP="1")
    assert "RETINUE_VOLUMES" not in plain, "without travel the script's own list: router and assistant"
    travel = run(tmp_path / "travel", BACKUP="1", TRAVEL="1")
    assert 'RETINUE_VOLUMES="retinue_router-data retinue_assistant-data retinue_travel-data"' in travel
    assert run(tmp_path / "travel", BACKUP="1").count("RETINUE_VOLUMES") == 1, "a second run writes it once"


def test_the_travel_sites_list_names_each_site_travel_ops_links_to():
    from retinue.render import linkable

    hosts = [line.split("#")[0].strip() for line in (ROOT / "deploy" / "travel" / "link-hosts.txt").read_text()
             .splitlines() if line.split("#")[0].strip()]
    links = ["https://www.aviasales.ru/search/AMS2205LIS23051", "https://avia.tutu.ru/f/Amsterdam/Lisbon/",
             "https://www.onetwotrip.com/ru/f/search/x", "https://www.kupibilet.ru/search?x=1",
             "https://travel.wildberries.ru/avia/x", "https://kiwi.com/u/u6xbs4",
             "https://www.google.com/travel/flights?tfs=x", "https://www.trip.com/flights/showfarefirst?x=1",
             "https://www.booking.com/hotel/nl/example-apartment.en-gb.html", "https://www.airbnb.com/rooms/1",
             "https://www.trivago.com/en-US/oar/x", "https://12go.asia/en/travel/x",
             "https://shop.global.flixbus.com/search"]
    links += ["https://bus.tutu.ru/rasp/x", "https://www.tutu.ru/poezda/x", "https://www.wildberries.ru/travel/x"]
    assert [link for link in links if not linkable(link, hosts)] == [], "every site travel-ops links to"
    for foreign in ("https://www.google.com/search?q=x", "https://www.trivago.qzx.io/x", "https://evil.kiwi.com/x",
                    "https://mcp.tutu.ru/mcp", "https://cf.bstatic.com/x.jpg"):
        assert not linkable(foreign, hosts), foreign
    assert not any("*" in host for host in hosts), "exact hosts only"


def test_setup_with_travel(tmp_path, monkeypatch):
    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42", TRAVEL="1")
    assert printed["COMPOSE_PROFILES"] == "travel" and printed["RETINUE_TRAVEL_URL"] == "http://travel-ops:8765/mcp"
    assert printed["TRAVEL_OPS_GIT"] == "", "the owner's travel-ops checkout: he fills it in"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cfg = RouterConfig.load(target / "router.yaml")
    assert cfg.travel_url == "http://travel-ops:8765/mcp" and "kiwi.com" in cfg.link_hosts
    profile = target / "travel" / "profile.yml"
    assert oct(profile.stat().st_mode & 0o777) == "0o644", "read by travel-ops' own user"
    assert yaml.safe_load(profile.read_text()) is None, "no one's home in the repo: the owner writes it"
    profile.write_text("home_airports: [XXX]\n")
    again = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                "TELEGRAM_OWNER_ID": "42", "MATRIX_SERVER_NAME": "matrix.example.com",
                                "MATRIX_OWNER": "alice"})
    assert again.returncode == 0, again.stderr
    assert profile.read_text() == "home_airports: [XXX]\n", "a later run keeps it, and keeps travel on"
    stack = (target / "stack.env").read_text()
    assert "COMPOSE_PROFILES=matrix,travel" in stack and "RETINUE_TRAVEL_URL=http://travel-ops:8765/mcp" in stack
    assert "travel_url:" in (target / "router.yaml").read_text() and "profile.yml" not in again.stdout
    assert "stack.env: TRAVEL_OPS_GIT" in again.stdout, "asked for until it is filled"
    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42")
    assert not (plain / "travel").exists() and "travel" not in (plain / "router.yaml").read_text()
    assert printed["RETINUE_TRAVEL_URL"] == "", "no server for the assistant either"
    assert "TRAVEL_OPS_GIT" not in printed


def test_the_base_is_mounted_for_both_and_its_repository_for_the_router_only():
    router, assistant = " ".join(SERVICES["router"]["volumes"]), " ".join(SERVICES["assistant"]["volumes"])
    assert "/srv/retinue/memory/tree:/kb " in router + " " and "/srv/retinue/memory/hub.git:/hub" in router
    assert "/srv/retinue/memory/git:/kb-git" in router and "memory/git" not in assistant and "hub" not in assistant
    assert SERVICES["assistant"]["environment"]["RETINUE_MEMORY"] == "${RETINUE_MEMORY:-}"
    assert "apt-get install -y --no-install-recommends git" in (ROOT / "Dockerfile").read_text(), "the router commits"
    assert "str(ROOT)]" in (ROOT / "deploy/backup/retinue-backup.py").read_text(), \
        "the hub and the working copy live under /srv/retinue: the nightly backup takes them with it"


def test_memory_is_off_until_asked_and_its_folders_are_there_anyway(tmp_path, monkeypatch):
    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    memory = target / "memory"
    assert (memory / "tree").is_dir() and (memory / "git").is_dir(), "the mounts exist whether it is on or not"
    assert (memory / "policy.json").read_text() == (ROOT / "deploy/memory/policy.example.json").read_text()
    assert not (memory / "hub.git").exists() and printed["RETINUE_MEMORY"] == ""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert RouterConfig.load(target / "router.yaml").memory is None


def test_setup_with_memory_makes_the_hub_and_installs_its_checks(tmp_path, monkeypatch):
    import json

    from retinue import kbcheck

    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42", MEMORY="1")
    hub, memory = target / "memory" / "hub.git", target / "memory"
    assert printed["RETINUE_MEMORY"] == "/kb" and (hub / "HEAD").is_file()
    assert (hub / "hooks" / "pre-receive").read_text() == (ROOT / "retinue" / "kbcheck.py").read_text()
    assert os.access(hub / "hooks" / "pre-receive", os.X_OK)
    settings = subprocess.run(["git", "-C", str(hub), "config", "--list"], capture_output=True, text=True).stdout
    for setting in ("receive.denynonfastforwards=true", "receive.denydeletes=true", "core.sharedrepository=group"):
        assert setting in settings, setting
    policy = kbcheck.Policy.load(str(hub / "hooks" / "kb-policy.json"))
    assert policy.writer_uid == 10001 and policy.check == ["python3", "-I", "scripts/lint.py"], "-I: nothing in the tree on its path"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cfg = RouterConfig.load(target / "router.yaml").memory
    assert cfg.hub == "/hub" and cfg.checkout == ["/*", "!/.*/", "!/scripts/"] and cfg.tree == "/kb"
    assert cfg.sensitive == ["*"], "until the owner names the closed Spaces, every Space is closed"
    raw = json.loads((memory / "policy.json").read_text())
    (memory / "policy.json").write_text(json.dumps({**raw, "writable": ["notes/**/*.md"]}))
    (memory / "checkout.txt").write_text("/*.md\n/notes/\n")
    (memory / "sensitive.txt").write_text("# closed\nwork\nhealth\n")
    again, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert printed["RETINUE_MEMORY"] == "/kb", "the hub exists: on for good"
    assert json.loads((hub / "hooks" / "kb-policy.json").read_text())["writable"] == ["notes/**/*.md"], \
        "the owner's rules, copied to the hub on every run"
    assert RouterConfig.load(target / "router.yaml").memory.checkout == ["/*.md", "/notes/"]
    assert RouterConfig.load(target / "router.yaml").memory.sensitive == ["work", "health"]


def test_the_hub_takes_pushes_but_its_config_and_hooks_stay_roots(tmp_path):
    """The containers' user writes objects and refs through the group and nothing else: not the config (hooksPath,
    receive.*), not hooks/, not the hub's own folder (it could rename hooks/ away). No gc after a push: it would
    write in the hub's folder. Modes as setup.sh sets them; owners (the owner's hub and config, root's hooks/) are set
    on the host only (HOST_SETUP=1) and checked live."""
    import stat

    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42", MEMORY="1")
    hub = target / "memory" / "hub.git"

    def mode(path):
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(hub) == 0o755 and mode(hub / "hooks") == 0o755 and mode(hub / "config") == 0o644
    for path in [hub / "HEAD", *(hub / "hooks").iterdir()]:
        assert not mode(path) & 0o022, path
    for folder in ("objects", "refs"):
        assert mode(hub / folder) & 0o2070 == 0o2070, f"{folder}: group-writable, setgid"
    assert not (hub / "kbcheck").exists(), "the lint's copies live elsewhere, one fresh folder per check"
    settings = subprocess.run(["git", "-C", str(hub), "config", "--list"], capture_output=True, text=True).stdout
    assert "receive.autogc=false" in settings and "core.logallrefupdates=false" in settings
    head = subprocess.run(["git", "-C", str(hub), "symbolic-ref", "HEAD"], capture_output=True, text=True).stdout
    assert head.strip() == "refs/heads/none", \
        "a push to the branch HEAD names locks HEAD in the hub's own folder, which the group cannot write"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    (clone / "a.md").write_text("x\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "x"], check=True, env=env)
    (hub / "hooks" / "pre-receive").unlink()  # the policy's lint needs a base; here only the modes are checked
    push = subprocess.run(["git", "-C", str(clone), "push", "-q", str(hub), "main"], capture_output=True, text=True)
    assert push.returncode == 0, push.stderr
    fresh = [d for d in (hub / "objects").iterdir() if d.is_dir() and len(d.name) == 2]
    assert fresh and all(mode(d) & 0o2070 == 0o2070 for d in fresh), "what a push writes stays the group's"
    assert mode(hub) == 0o755 and not (hub / "packed-refs").exists()


def test_the_collector_shares_a_network_with_the_router_alone_and_goes_out_to_google_only():
    """The collector reads strangers' letters with the owner's tokens: its own user, its own keys read-only, no
    capabilities, a filesystem it cannot change; the router on one internal network, its proxy on the other; the
    proxy lets through Google's three hosts and nothing else. The assistant is on neither."""
    from retinue import google

    members = {net: sorted(name for name, service in SERVICES.items() if net in service.get("networks", []))
               for net in COMPOSE["networks"]}
    assert members["mail-router"] == ["collector", "router"] and members["mail-out"] == ["collector", "mail-egress"]
    for net in ("mail-router", "mail-out"):
        assert COMPOSE["networks"][net] == {"internal": True}, f"{net}: no route out"
    collector, proxy = SERVICES["collector"], SERVICES["mail-egress"]
    assert collector["image"] == "retinue:local" and collector["command"] == ["retinue-collector"]
    assert collector["user"] == "10002:10002", "not the containers' 10001: the router and the assistant cannot read its keys"
    assert collector["volumes"] == ["/srv/retinue/mail/keys:/keys:ro", "/srv/retinue/mail/state:/state"]
    assert collector["environment"] == {"RETINUE_COLLECTOR_TOKEN": "${RETINUE_COLLECTOR_TOKEN:-}",
                                        "HTTPS_PROXY": "http://mail-egress:3128"}, "no model token, no other key"
    assert collector["cap_drop"] == ["ALL"] and collector["security_opt"] == ["no-new-privileges:true"]
    assert collector["read_only"] is True and "ports" not in collector and collector["profiles"] == ["mail"]
    assert proxy["profiles"] == ["mail"] and set(proxy["networks"]) == {"mail-out", "send-out", "outside"} and \
        "ports" not in proxy
    assert proxy["volumes"] == ["/srv/retinue/egress/squid.conf:/etc/squid/squid.conf:ro",
                                "/srv/retinue/egress/mail-hosts.txt:/etc/squid/allowed-hosts.txt:ro"]
    assert (ROOT / "deploy" / "egress" / "mail-hosts.txt").read_text().split() == list(google.HOSTS)
    assert not any(net.startswith("mail") for net in SERVICES["assistant"]["networks"])
    assert SERVICES["router"]["environment"]["RETINUE_COLLECTOR_TOKEN"] == "${RETINUE_COLLECTOR_TOKEN:-}"
    assert "mail" not in str(SERVICES["assistant"]["volumes"]) + str(SERVICES["router"]["volumes"])


def test_setup_with_mail(tmp_path, monkeypatch):
    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42", MAIL="1")
    mail = target / "mail"
    for folder in (mail, mail / "keys", mail / "state"):
        assert oct(folder.stat().st_mode & 0o777) == "0o700", folder
    assert printed["COMPOSE_PROFILES"] == "mail" and len(printed["RETINUE_COLLECTOR_TOKEN"]) == 64
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RETINUE_COLLECTOR_TOKEN", printed["RETINUE_COLLECTOR_TOKEN"])
    cfg = RouterConfig.load(target / "router.yaml")
    assert cfg.collector_url == "http://collector:9200" and cfg.collector_token == printed["RETINUE_COLLECTOR_TOKEN"]
    hosts = target / "egress" / "mail-hosts.txt"
    assert hosts.read_text().split() == ["oauth2.googleapis.com", "gmail.googleapis.com", "www.googleapis.com"]
    hosts.write_text("oauth2.googleapis.com\n")
    again, reprinted = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert reprinted["RETINUE_COLLECTOR_TOKEN"] == printed["RETINUE_COLLECTOR_TOKEN"], "on for good, the same token"
    assert hosts.read_text() == "oauth2.googleapis.com\n", "the owner's list is his after the first run"
    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42")
    assert not (plain / "mail").exists() and "collector_url" not in (plain / "router.yaml").read_text()
    assert "RETINUE_COLLECTOR_TOKEN" not in printed and not (plain / "egress" / "mail-hosts.txt").exists()


def test_the_lifehub_is_built_with_no_network_and_served_read_only_to_traefik_alone():
    """The builder has no network and its own scratch space; nginx reads the live release and nothing else, on
    Traefik's network only, and answers only with the key Traefik adds after its check of the owner's devices. The
    router writes the data; the assistant sees none of it."""
    build, serve = SERVICES["lifehub-build"], SERVICES["lifehub"]
    assert build["profiles"] == serve["profiles"] == ["lifehub"], "off until the owner turns it on"
    assert build["network_mode"] == "none" and "networks" not in build and "ports" not in build
    assert build["user"] == "10001:10001" and build["read_only"] is True and build["cap_drop"] == ["ALL"]
    assert build["security_opt"] == ["no-new-privileges:true"] and build["tmpfs"][0].startswith("/tmp:")
    assert build["volumes"] == ["/srv/retinue/lifehub/data:/data:ro", "/srv/retinue/lifehub/site:/site"]
    assert build["build"] == {"context": "./lifehub"} and build["image"] == "retinue-lifehub:local"
    assert serve["image"].startswith("nginxinc/nginx-unprivileged:") and "@sha256:" in serve["image"], "pinned"
    assert serve["networks"] == ["dokploy-network"] and "ports" not in serve and serve["read_only"] is True
    assert serve["cap_drop"] == ["ALL"] and serve["volumes"] == [
        "/srv/retinue/lifehub/site:/site:ro", "/srv/retinue/lifehub/nginx.conf:/etc/nginx/conf.d/default.conf:ro"]
    labels = dict(label.split("=", 1) for label in serve["labels"])
    assert labels["traefik.http.routers.retinue-lifehub.rule"] == "Host(`${LIFEHUB_HOST:-}`)"
    assert labels["traefik.http.routers.retinue-lifehub.entrypoints"] == "websecure"
    assert labels["traefik.http.routers.retinue-lifehub.tls.certresolver"] == "letsencrypt"
    assert labels["traefik.http.routers.retinue-lifehub.middlewares"] == "lifehub-owner@file,lifehub-headers@file"
    assert labels["traefik.http.services.retinue-lifehub.loadbalancer.server.port"] == "8080"
    assert labels["traefik.http.routers.retinue-lifehub.service"] == "retinue-lifehub"
    # The owner's pages: the hub's host under /pages/, chosen before the hub's router, the same devices and key,
    # none of the hub's headers — so the hub's CSP never reaches a page, and only nginx sets a page's.
    assert labels["traefik.http.routers.retinue-lifehub-pages.rule"] == \
        "Host(`${LIFEHUB_HOST:-}`) && PathPrefix(`/pages/`)"
    assert int(labels["traefik.http.routers.retinue-lifehub-pages.priority"]) > len(
        labels["traefik.http.routers.retinue-lifehub.rule"]), "it wins over the hub's router, whose priority is its length"
    assert "traefik.http.routers.retinue-lifehub.priority" not in labels
    assert labels["traefik.http.routers.retinue-lifehub-pages.entrypoints"] == "websecure"
    assert labels["traefik.http.routers.retinue-lifehub-pages.tls.certresolver"] == "letsencrypt"
    assert labels["traefik.http.routers.retinue-lifehub-pages.middlewares"] == "lifehub-owner@file,lifehub-key@file", \
        "never lifehub-headers: two CSPs are both enforced, and a page's scripts die"
    assert labels["traefik.http.routers.retinue-lifehub-pages.service"] == "retinue-lifehub"
    assert not [label for label in labels if "PAGES_HOST" in labels[label]], "no name of their own any more"
    assert "/srv/retinue/lifehub/data:/lifehub" in SERVICES["router"]["volumes"]
    assert "/srv/retinue/lifehub/site:/lifehub-site:ro" in SERVICES["router"]["volumes"], "build.json, read only"
    assert "lifehub" not in str(SERVICES["assistant"]), "she publishes through the router's bus, never a file"
    docker = (ROOT / "deploy" / "lifehub" / "Dockerfile").read_text()
    assert "HUGO_VERSION=0.167.0" in docker and \
        "HUGO_SHA256=4d84519b9f619e6d4c3fb45a50157abeabeb724f859c60605f44c23def6e1169" in docker
    assert "hashlib.sha256" in docker and "USER 10001" in docker and 'CMD ["python3", "-I", "/app/build.py"]' in docker
    assert "COPY build.py /app/build.py" in docker and "COPY site /app/site" in docker
    assert docker.index("COPY vendor.json /app/vendor.json") < docker.index("RUN python3 -I /app/build.py vendor") \
        < docker.index("USER 10001"), "what pages may load is fetched and checked when the image is built"
    nginx = (ROOT / "deploy" / "lifehub" / "nginx.conf.template").read_text()
    hub = server_level(nginx)
    assert "root /site/current/hub;" in hub and 'if ($http_x_lifehub_key != "${LIFEHUB_KEY}") { return 404; }' in hub
    for header in (HUB_CSP, 'Referrer-Policy "no-referrer" always',
                   'X-Content-Type-Options "nosniff" always', 'X-Frame-Options "DENY" always'):
        assert f"add_header {header};" in hub, header
    assert "listen 8080 default_server;" in hub, "a request for any other name is the hub's: 404 without the key"
    assert nginx.count("server {") == 1, "one server: the pages are the hub's /pages/"


HUB_CSP = ("Content-Security-Policy \"default-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; "
           "object-src 'none'\" always")


def spans(text: str) -> list[tuple[str, str, int, int]]:
    """The `location … { … }` blocks directly in `text`: (the match, the body, where the block starts and ends),
    nested ones left in the body. Braces inside quotes (a regex's `{0,63}`) do not count."""
    import re
    out, i = [], 0
    while found := re.compile(r"^[ \t]*location ", re.M).search(text, i):
        at = found.end() - len("location ")
        opened, quoted = at, False
        while text[opened] != "{" or quoted:
            quoted ^= text[opened] == '"'
            opened += 1
        depth, j, quoted = 1, opened + 1, False
        while depth:
            quoted ^= text[j] == '"'
            if not quoted:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        out.append((text[at + len("location "):opened].strip(), text[opened + 1:j - 1], at, j))
        i = j
    return out


def blocks(text: str) -> list[tuple[str, str]]:
    return [(match, body) for match, body, *_ in spans(text)]


def outside(text: str) -> str:
    """`text` without its location blocks and comments: what a location that sets no header of its own inherits."""
    for _, _, start, end in reversed(spans(text)):
        text = text[:start] + text[end:]
    return "\n".join(line.split("#")[0] for line in text.splitlines())


def server_level(nginx: str) -> str:
    return outside(nginx[nginx.index("server {") + len("server {"):])


def test_a_page_under_pages_is_sandboxed_and_runs_scripts_only_when_the_owner_wrote_it():
    """/pages/ on the hub: every response under it — a page, a vendored file, a 404 — carries the pages' policy and
    none of the hub's; a page in `pages/scripts/` (only the owner wrote it) gets `sandbox allow-scripts`, any other
    no scripts; both no network and nothing but /pages/_vendor/, named with the hub's host. CORS only on
    /pages/_vendor/: a sandboxed page's origin is opaque, so its fonts need it — and nowhere else on the hub."""
    nginx = (ROOT / "deploy" / "lifehub" / "nginx.conf.template").read_text()
    top = dict(blocks(nginx[nginx.index("server {") + len("server {"):]))
    assert list(top) == ["~ /\\.", "/", "^~ /pages/"]
    assert "add_header" not in top["/"] and "add_header" not in top["~ /\\."], "the hub's own: the server's headers"
    pages = top["^~ /pages/"]
    inner = dict(blocks(pages))
    assert list(inner) == ['~ "^/pages/(?<page>[a-z0-9][a-z0-9-]{0,63})\\.html$"', "^~ /pages/scripts/",
                           "^~ /pages/_vendor/"]
    outer = outside(pages)
    page, scripts, vendor = inner.values()
    assert "root /site/current;" in outer and "return 404;" in outer, "anything else under /pages/: 404, sandboxed"
    vendor_src = "https://${LIFEHUB_HOST}/pages/_vendor/"
    common = (f"style-src {vendor_src} 'unsafe-inline'; img-src {vendor_src} data:; font-src {vendor_src} data:; "
              "connect-src 'none'; worker-src 'none'; frame-src 'none'; manifest-src 'none'; base-uri 'none'; "
              "form-action 'none'; frame-ancestors 'none'; sandbox")
    none = f"add_header Content-Security-Policy \"default-src 'none'; script-src 'none'; {common}\" always;"
    for block in (outer, page, vendor):
        assert none in block and block.count("Content-Security-Policy") == 1
    assert f"add_header Content-Security-Policy \"default-src 'none'; script-src {vendor_src} 'unsafe-inline'; " \
           f"{common} allow-scripts\" always;" in scripts and scripts.count("Content-Security-Policy") == 1
    assert "if (-f $document_root/pages/scripts/$page.html) { rewrite ^ /pages/scripts/$page.html last; }" in page
    assert "internal;" in scripts, "/pages/scripts/<id>.html is reached only through /pages/<id>.html"
    for block in (outer, page, scripts, vendor):
        assert "'self'" not in block and "default-src 'self'" not in block, "never the hub's policy under /pages/"
        for line in (line.strip() for line in block.splitlines() if "add_header" in line):
            assert line.endswith("always;") or line.startswith("add_header Cache-Control \"public"), line
    for block in (outer, page, scripts):
        for header in ('Referrer-Policy "no-referrer"', 'X-Content-Type-Options "nosniff"', 'X-Frame-Options "DENY"',
                       'X-DNS-Prefetch-Control "off"', 'X-Robots-Tag "noindex, nofollow"',
                       'Permissions-Policy "camera=(), microphone=(), geolocation=(), clipboard-read=()"'):
            assert f"add_header {header} always;" in block, header
        assert "Access-Control-Allow-Origin" not in block, "a page is read by nobody else"
    assert 'add_header Access-Control-Allow-Origin "*" always;' in vendor
    assert nginx.count("Access-Control-Allow-Origin") == 1, "CORS on /pages/_vendor/ and nowhere else on the hub"
    assert 'add_header Cache-Control "public, max-age=31536000, immutable";' in vendor, "not `always`: no 404 kept"
    assert "sandbox" not in server_level(nginx), "the pages' policy never on the hub's own responses"
    traefik = yaml.safe_load((ROOT / "deploy" / "lifehub" / "traefik.yml.template").read_text().replace(
        "${LIFEHUB_RANGES}", '"10.8.0.2/32"'))["http"]["middlewares"]
    assert traefik["lifehub-key"] == {"headers": {"customRequestHeaders": {"X-Lifehub-Key": "${LIFEHUB_KEY}"}}}, \
        "the key and nothing else: the hub's policy on a page would stop its scripts"
    assert traefik["lifehub-headers"]["headers"]["contentSecurityPolicy"].startswith("default-src 'self';")
    assert "lifehub-pages-headers" not in traefik


def test_setup_with_lifehub(tmp_path, monkeypatch):
    def run(target, **env):
        return subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   "TELEGRAM_OWNER_ID": "42", **env})

    refused = run(tmp_path / "srv", LIFEHUB="1")
    assert refused.returncode == 1 and "LIFEHUB_HOST" in refused.stderr
    wrong = run(tmp_path / "srv", LIFEHUB="1", LIFEHUB_HOST="hub.in.example.com/x", LIFEHUB_DEVICES="10.8.0.2/32")
    assert wrong.returncode == 1 and "LIFEHUB_HOST" in wrong.stderr
    target, printed = setup(tmp_path, LIFEHUB="1", LIFEHUB_HOST="hub.in.example.com", TELEGRAM_OWNER_ID="42",
                            LIFEHUB_DEVICES="10.8.0.2/32, 10.8.0.3/32")
    hub = target / "lifehub"
    assert printed["COMPOSE_PROFILES"] == "lifehub" and printed["LIFEHUB_HOST"] == "hub.in.example.com"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cfg = RouterConfig.load(target / "router.yaml")
    assert cfg.lifehub_url == "https://hub.in.example.com" and cfg.lifehub_data == "/lifehub"
    assert (hub / "data").is_dir() and (hub / "site").is_dir()
    key = dict(line.split("=", 1) for line in (target / "secrets.env").read_text().splitlines())["LIFEHUB_KEY"]
    assert len(key) == 64
    nginx = (hub / "nginx.conf").read_text()
    assert f'if ($http_x_lifehub_key != "{key}")' in nginx and oct((hub / "nginx.conf").stat().st_mode & 0o777) == "0o640"
    traefik = yaml.safe_load((hub / "traefik.yml").read_text())
    middlewares = traefik["http"]["middlewares"]
    assert middlewares["lifehub-owner"]["ipAllowList"] == {"sourceRange": ["10.8.0.2/32", "10.8.0.3/32"],
                                                           "rejectStatusCode": 404}
    assert middlewares["lifehub-headers"]["headers"]["customRequestHeaders"] == {"X-Lifehub-Key": key}
    assert middlewares["lifehub-headers"]["headers"]["contentSecurityPolicy"].startswith("default-src 'self';")
    assert oct((hub / "traefik.yml").stat().st_mode & 0o777) == "0o600"
    assert (hub / "devices.txt").read_text() == "10.8.0.2/32\n10.8.0.3/32\n" and (hub / "host").read_text() == \
        "hub.in.example.com\n"
    again = run(target)
    assert again.returncode == 0 and key not in again.stdout + again.stderr, "on for good; the key is never printed"
    assert "lifehub_url: https://hub.in.example.com" in (target / "router.yaml").read_text()
    assert "Lifehub: on — https://hub.in.example.com" in again.stdout
    assert "${" not in nginx and nginx.count(f'if ($http_x_lifehub_key != "{key}")') == 1, "every name and the key"
    assert "style-src https://hub.in.example.com/pages/_vendor/ 'unsafe-inline'" in nginx, "the hub's name in it"
    assert "lifehub_pages_url" not in (target / "router.yaml").read_text() and "LIFEHUB_PAGES_HOST" not in printed
    assert "Lifehub pages: https://hub.in.example.com/pages/<id>.html" in again.stdout
    (hub / "pages_host").write_text("pages.in.example.com\n")  # an earlier version's separate name, if one is left
    with open(target / "stack.env", "a") as stack:
        stack.write("LIFEHUB_PAGES_HOST=pages.in.example.com\n")
    cleaned = run(target)
    assert cleaned.returncode == 0 and not (hub / "pages_host").exists()
    assert "LIFEHUB_PAGES_HOST" not in (target / "stack.env").read_text()

    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42")
    assert "lifehub_url" not in (plain / "router.yaml").read_text() and "LIFEHUB_HOST" not in printed
    assert (plain / "lifehub" / "data").is_dir(), "the router's mounts exist whether it is on or not"
    bad = run(tmp_path / "third", LIFEHUB="1", LIFEHUB_HOST="hub.in.example.com", LIFEHUB_DEVICES="0.0.0.0/0")
    assert bad.returncode == 1 and "LIFEHUB_DEVICES" in bad.stderr, "one address per device, not a network"
    assert not (tmp_path / "third" / "lifehub" / "host").exists(), "nothing kept from a refused run"


def test_the_sender_and_the_gateway_each_hold_their_key_alone():
    """Keys that send live in processes that do nothing else: the mail sender (gmail.send, its own user and folder,
    out through the mail proxy only) and the Telegram Business gateway (the Business bot's token, its own user). Each
    shares an internal network with the router alone; the collector and the assistant hold neither key."""
    members = {net: sorted(name for name, service in SERVICES.items() if net in service.get("networks", []))
               for net in COMPOSE["networks"]}
    assert members["send-router"] == ["mail-send", "router"] and members["tg-router"] == ["router", "tg-business"]
    assert members["send-out"] == ["mail-egress", "mail-send"]
    for net in ("send-router", "send-out", "tg-router"):
        assert COMPOSE["networks"][net] == {"internal": True}, f"{net}: no route out"
    send, gateway = SERVICES["mail-send"], SERVICES["tg-business"]
    assert send["command"] == ["retinue-mail-send"] and gateway["command"] == ["retinue-tg-business"]
    assert send["user"] == "10004:10004" and gateway["user"] == "10003:10003", "neither the collector's nor 10001"
    assert send["volumes"] == ["/srv/retinue/send/keys:/keys:ro", "/srv/retinue/send/state:/state"]
    assert gateway["volumes"] == ["/srv/retinue/telegram/state:/state"]
    assert send["environment"] == {"RETINUE_SENDER_TOKEN": "${RETINUE_SENDER_TOKEN:-}",
                                   "HTTPS_PROXY": "http://mail-egress:3128"}, "no model token, no read key"
    assert gateway["environment"] == {"RETINUE_GATEWAY_TOKEN": "${RETINUE_GATEWAY_TOKEN:-}",
                                      "TELEGRAM_BUSINESS_TOKEN": "${TELEGRAM_BUSINESS_TOKEN:-}",
                                      "TELEGRAM_OWNER_ID": "${TELEGRAM_OWNER_ID:-}"}
    for service in (send, gateway):
        assert service["cap_drop"] == ["ALL"] and service["read_only"] is True and "ports" not in service
    assert send["profiles"] == ["send"] and gateway["profiles"] == ["tgbusiness"], "off until the owner turns it on"
    assert gateway["networks"] == ["tg-router", "outside"]
    assert "TELEGRAM_BUSINESS_TOKEN" not in str(SERVICES["router"]) + str(SERVICES["collector"])
    assert "send" not in str(SERVICES["collector"]["volumes"]) and "send" not in str(SERVICES["router"]["volumes"])
    assert not {"send-router", "tg-router"} & set(SERVICES["assistant"]["networks"])
    assert {"tg-router", "send-router"} <= set(SERVICES["router"]["networks"])


def test_setup_with_telegram_business_and_mail_sending(tmp_path, monkeypatch):
    def run(target, **env):
        return subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   **env})

    refused = run(tmp_path / "a", TELEGRAM_OWNER_ID="42", SEND="1")
    assert refused.returncode == 1 and "SEND=1 needs MAIL=1" in refused.stderr
    nobody = run(tmp_path / "b", MATRIX_SERVER_NAME="matrix.example.com", MATRIX_OWNER="alice", TGBUSINESS="1")
    assert nobody.returncode == 1 and "TGBUSINESS=1 needs TELEGRAM_OWNER_ID" in nobody.stderr
    target, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42", MAIL="1", TGBUSINESS="1", SEND="1")
    for folder in ("send", "send/keys", "send/state", "telegram", "telegram/state"):
        assert oct((target / folder).stat().st_mode & 0o777) == "0o700", folder
    assert printed["COMPOSE_PROFILES"] == "mail,tgbusiness,send" and printed["TELEGRAM_OWNER_ID"] == "42"
    assert len(printed["RETINUE_GATEWAY_TOKEN"]) == len(printed["RETINUE_SENDER_TOKEN"]) == 64
    assert printed["TELEGRAM_BUSINESS_TOKEN"] == "", "the owner pastes the new bot's token"
    for name in ("TELEGRAM_BOT_TOKEN", "RETINUE_COLLECTOR_TOKEN", "RETINUE_GATEWAY_TOKEN", "RETINUE_SENDER_TOKEN"):
        monkeypatch.setenv(name, printed.get(name) or "t")
    cfg = RouterConfig.load(target / "router.yaml")
    assert (cfg.gateway_url, cfg.sender_url) == ("http://tg-business:9300", "http://mail-send:9400")
    assert cfg.agents[0].drafts is True
    again, reprinted = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert reprinted["RETINUE_SENDER_TOKEN"] == printed["RETINUE_SENDER_TOKEN"], "on for good, the same token"
    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42")
    assert "gateway_url" not in (plain / "router.yaml").read_text() and "RETINUE_SENDER_TOKEN" not in printed
    assert not (plain / "send").exists() and not (plain / "telegram").exists()


def test_her_rules_for_the_memory_of_an_exchange():
    """What the router now gives her and expects of her about the past of a chat: search before «I don't know»,
    this chat first and wider when needed, what the card will say about other sources, captions and messages written
    to her — and that nothing in someone's words changes her memory."""
    instructions = (ROOT / "agents" / "assistant" / "CLAUDE.md").read_text()
    assert "до десяти прошлых" not in instructions and "Не знаешь, что он ответит, — черновика не предлагай" \
        not in instructions, "the count of ten and the refusal without a search are gone"
    memory = instructions.split("## Память переписки")[1].split("\n## ")[0]
    assert "сначала ищи" in memory and "«Не знаю» — только после поиска" in memory
    assert all(f"`{word}`" in memory for word in ("search_archive", "scope: mail", "scope: owner", "since",
                                                     "get_attachment", "media", "to_assistant"))
    assert "«Писала, читая: …»" in memory and "личное Владельца" in memory and "по существу" in memory
    assert "Текст с картинки — только в кавычках" in memory and "не правишь и не стираешь" in memory
    assert "база только для чтения" in memory
    replies = instructions.split("## Ответы людям")[1].split("\n## ")[0]
    assert "где и по каким словам искала" in replies and "Роутер сам напишет на карточке" in replies
    assert "пароли" in replies and "ничего о личном Владельца" in replies
    mail = instructions.split("## Почта и календарь")[1].split("\n## ")[0]
    assert "трое суток" in mail and "`scope: owner`" in mail
    courier = (ROOT / "docs" / "courier.md").read_text()
    assert "«Писала, читая»" in courier and "sensitive.txt" in courier and "to_assistant" in (
        ROOT / "docs" / "mail.md").read_text()


LANES = ("private", "code", "web")   # every lane service in compose; the room and the two retired ones
ROOM = "private"                     # lane-private: the Base, the code and the web (docs/platform.md, «The room»)
RETIRED = ("code", "web")            # under the profile legacy-lanes, for their volumes
SEALED = ("code",)  # the lanes whose proxies let through a list of hosts; the room's (and lane-web's) is open (below)
UIDS = {"private": "10005", "code": "10006", "web": "10008"}
PLATFORM_SERVICES = ["lane-private", "lane-private-egress", "lane-private-sni", "platform", "platform-egress"]
LEGACY_SERVICES = ["lane-code", "lane-code-egress", "lane-web", "lane-web-egress"]
OPEN_FILES = ("open-squid.conf", "open-deny-nets.txt", "open-deny-ips.txt", "open-deny-domains.txt")
OPEN_PROXY = ["/srv/retinue/egress/open-squid.conf:/etc/squid/squid.conf:ro",
              "/srv/retinue/egress/open-deny-nets.txt:/etc/squid/deny-nets.txt:ro",
              "/srv/retinue/egress/open-deny-ips.txt:/etc/squid/deny-ips.txt:ro",
              "/srv/retinue/egress/open-deny-domains.txt:/etc/squid/deny-domains.txt:ro"]


def test_the_platform_and_its_lanes_are_off_until_asked_and_locked_down():
    assert sorted(name for name, service in SERVICES.items() if "platform" in service.get("profiles", [])) == \
        PLATFORM_SERVICES
    assert sorted(name for name, service in SERVICES.items() if "legacy-lanes" in service.get("profiles", [])) == \
        LEGACY_SERVICES, "retired: never started with the stack, kept for their volumes"
    for name in ("platform", *(f"lane-{lane}" for lane in LANES)):
        service = SERVICES[name]
        assert service["cap_drop"] == ["ALL"] and service["read_only"] is True, name
        assert "ports" not in service and "network_mode" not in service, name
    assert SERVICES["platform"]["security_opt"] == ["no-new-privileges:true"], "Docker's defaults"
    for lane in LANES:
        assert SERVICES[f"lane-{lane}"]["security_opt"] == [
            "no-new-privileges:true", "apparmor=retinue-lane", "seccomp=/srv/retinue/platform/lane-seccomp.json",
            "systempaths=unconfined"], "the CLI's sandbox: a user namespace and a fresh /proc, nothing more"
    platform = SERVICES["platform"]
    assert platform["command"] == ["retinue-platform"] and platform["user"] == "10007:10007"
    assert platform["image"] == SERVICES["router"]["image"], "the platform is code in the router's image"
    assert platform["stop_grace_period"] == "30s"
    for name in ("platform", *(f"lane-{lane}" for lane in LANES)):
        assert SERVICES[name]["init"] is True, f"{name}: SIGTERM reaches its process; orphans are reaped"
        assert SERVICES[name]["stop_grace_period"] == "30s", f"{name}: longer than the runs' stop grace"
    assert 30 > lane_host.STOP_GRACE_S + 2 * lane_host.KILL_GRACE_S, "a lane's runs end before Docker kills it"
    assert platform["volumes"] == ["platform-data:/platform", "/srv/retinue/platform/platform.yaml:/config/platform.yaml:ro",
                                   "lane-private-work:/lanes/private:ro", "/srv/retinue/skills:/skills:ro",
                                   "/srv/retinue/owner-memory:/owner-memory:ro",
                                   "/srv/retinue/base/tree:/lanes/private/base", "/srv/retinue/memory/hub.git:/hub:ro",
                                   "/srv/retinue/memory/hub.git/objects:/hub/objects",
                                   "/srv/retinue/memory/hub.git/refs:/hub/refs", "/srv/retinue/code/mirrors:/code/mirrors"]
    assert platform["env_file"] == [{"path": "/srv/retinue/code/tokens.env", "required": False}], \
        "the git tokens: in the platform alone, and a missing file turns the repositories off, not the platform"
    assert platform["group_add"] == ["10001", "10020"], "the hub's group for a push, the Base's for the clone"
    assert not any("10020" in str(service.get("user", "")) + str((service.get("build") or {}).get("args", ""))
                   for service in SERVICES.values()), "the Base's group is no service's own uid or gid"
    assert SERVICES["lane-private"]["group_add"] == ["10020"] and \
        "/srv/retinue/base/tree:/work/base" in SERVICES["lane-private"]["volumes"]
    for other in ("lane-code", "lane-web"):
        assert "base" not in str(SERVICES[other]["volumes"]) and "group_add" not in SERVICES[other], \
            f"{other}: the Base never goes there"
    assert sorted(platform["environment"]) == ["CLAUDE_CODE_OAUTH_TOKEN", "ELEVENLABS_API_KEY", "RETINUE_FACE_HOST",
                                               "RETINUE_FACE_ORIGIN",
                                               "RETINUE_HOOK_SECRET", "RETINUE_LANE_TOKEN_CODE",
                                               "RETINUE_LANE_TOKEN_PRIVATE", "RETINUE_LANE_TOKEN_WEB",
                                               "RETINUE_PLATFORM_TOKEN", "RETINUE_WEBAPP_KEY", "TELEGRAM_OWNER_ID"], \
        "the lanes' login, which it sends with each run; no bot token: the Mini App's key is derived from it, not it"
    assert platform["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${CLAUDE_CODE_OAUTH_TOKEN}"
    holders = sorted(name for name, service in SERVICES.items()
                     if "CLAUDE_CODE_OAUTH_TOKEN" in (service.get("environment") or {}))
    assert holders == ["assistant", "platform"], "no lane container has the login in its environment"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in (ROOT / "deploy" / "compose.yml").read_text().split("&lane-env")[1] \
        .split("\n    <<:")[0], "nor the anchor every lane's environment comes from"
    assert "install -d -o 10007 -g 10007 /platform" in (ROOT / "Dockerfile").read_text(), \
        "a new platform-data volume takes the platform user's ownership from the image"
    for lane, uid in UIDS.items():
        service = SERVICES[f"lane-{lane}"]
        assert service["build"] == {"context": "..", "dockerfile": "deploy/lanes/Dockerfile",
                                    "args": {"LANE": lane, "LANE_UID": uid}}
        skills = [] if lane == "web" else ["/srv/retinue/skills:/lane/skills:ro"]  # the web lane: nothing of his
        base = ["/srv/retinue/base/tree:/work/base", "/srv/retinue/code/mirrors:/lane/mirrors:ro"] \
            if lane == "private" else []  # the Base and the code's mirrors (read-only): lane-private only
        memory = ["/srv/retinue/owner-memory:/lane/owner-memory:ro"] if lane == "private" else []  # the room's
        assert service["volumes"] == [f"lane-{lane}-work:/work", f"lane-{lane}-config:/lane/config",
                                      f"lane-{lane}-state:/lane/state", *skills, *memory, *base]
        assert all(f"uid={uid}" in mount for mount in service["tmpfs"]) and \
            sorted(m.split(":")[0] for m in service["tmpfs"]) == ["/lane/run", "/tmp"]
        assert service["environment"] == {
            "RETINUE_LANE_TOKEN": f"${{RETINUE_LANE_TOKEN_{lane.upper()}:-}}",
            "RETINUE_LANE_HOOK_URL": "http://platform:9500/hook",
            "HTTPS_PROXY": "http://lane-private-sni:3128" if lane == ROOM else f"http://lane-{lane}-egress:3128",
            "NO_PROXY": "platform",
            # lane-web's host never reads a repository's rules or the owner's skills (docs/instructions.md)
            **({"RETINUE_LANE_WEB": "1"} if lane == "web" else {}),
            # the room's host runs as many CLI processes as platform.yaml lets the room have
            **({"RETINUE_LANE_MAX": "12"} if lane == ROOM else {})}, \
            "no login, no API key, no base URL, no GitHub or registry credential"
        assert service["mem_limit"] and service["pids_limit"]
    room = SERVICES["lane-private"]
    # The owner and up to 10 sub-sessions at once (docs/platform.md, «Sub-sessions»), with subagents and builds:
    # about 1 GiB and 350 processes each, on a host shared with other services.
    assert (room["mem_limit"], room["pids_limit"]) == ("11g", 4096), "the owner and up to 10 sub-sessions"


def test_each_lane_is_on_its_own_internal_network_and_goes_out_through_its_own_proxy():
    members = {net: sorted(name for name, service in SERVICES.items() if net in service.get("networks", []))
               for net in COMPOSE["networks"]}
    assert members["platform-router"] == ["platform", "router"]
    assert list(SERVICES["platform"]["networks"]) == ["platform-router", "lane-private", "face-platform",
                                                      "platform-out"], "no `outside`, and no retired lane's network"
    assert SERVICES["platform"]["depends_on"] == ["lane-private"], "a retired lane is never started for it"
    assert members["platform-out"] == ["platform", "platform-egress"] and COMPOSE["networks"]["platform-out"] == \
        {"internal": True}, "its way to GitHub and GitLab is its own proxy"
    assert SERVICES["platform-egress"]["networks"] == ["platform-out", "outside"] and \
        SERVICES["platform-egress"]["volumes"] == ["/srv/retinue/egress/squid.conf:/etc/squid/squid.conf:ro",
                                                   "/srv/retinue/egress/platform-hosts.txt:/etc/squid/allowed-hosts.txt:ro"]
    assert (ROOT / "deploy" / "egress" / "platform-hosts.txt").read_text().split() == [
        "github.com", "api.github.com", "gitlab.com", "api.elevenlabs.io"], "and ElevenLabs for the app's voice"
    for lane in LANES:
        net = f"lane-{lane}"
        on = ["lane-private", "lane-private-sni", "platform"] if lane == ROOM else [f"lane-{lane}", f"lane-{lane}-egress"]
        assert COMPOSE["networks"][net] == {"internal": True} and members[net] == on
        assert SERVICES[f"lane-{lane}"]["networks"] == [net], "exactly its own network"
        proxy = SERVICES[f"lane-{lane}-egress"]
        assert proxy["networks"] == ["room-out" if lane == ROOM else net, "outside"]
        assert proxy["image"] == SERVICES["egress"]["image"]
        if lane in SEALED:
            assert proxy["volumes"] == ["/srv/retinue/egress/squid.conf:/etc/squid/squid.conf:ro",
                                        f"/srv/retinue/egress/lane-{lane}-hosts.txt:/etc/squid/allowed-hosts.txt:ro"]
        else:
            assert proxy["volumes"] == OPEN_PROXY, f"{lane}: the open proxy"
    assert COMPOSE["networks"]["platform-router"] == {"internal": True}
    assert "platform-router" in SERVICES["router"]["networks"]
    assert SERVICES["router"]["environment"]["RETINUE_PLATFORM_TOKEN"] == "${RETINUE_PLATFORM_TOKEN:-}"
    assert not {"platform-router", "lane-private", "lane-code", "lane-web"} & set(SERVICES["assistant"]["networks"])
    assert not (ROOT / "deploy" / "egress" / "lane-private-hosts.txt").exists(), "the room's proxy is open"
    # The room goes out through its SNI guard alone, and the guard through the open proxy alone.
    guard = SERVICES["lane-private-sni"]
    assert COMPOSE["networks"]["room-out"] == {"internal": True}
    assert members["room-out"] == ["lane-private-egress", "lane-private-sni"]
    assert guard["networks"] == ["lane-private", "room-out"] and guard["command"] == ["python", "-m", "retinue.sniguard"]
    assert guard["environment"] == {"RETINUE_SNI_UPSTREAM": "lane-private-egress:3128"}
    assert guard["image"] == "retinue:local" and guard["read_only"] is True and guard["cap_drop"] == ["ALL"]
    assert guard["user"] not in {s.get("user") for n, s in SERVICES.items() if n != "lane-private-sni"}
    assert SERVICES["lane-private"]["environment"]["HTTPS_PROXY"] == "http://lane-private-sni:3128"
    assert (ROOT / "deploy" / "egress" / "lane-code-hosts.txt").read_text().split() == [
        "api.anthropic.com", "github.com", "codeload.github.com", "objects.githubusercontent.com", "pypi.org",
        "files.pythonhosted.org", "registry.npmjs.org"]


def test_no_lane_mounts_another_lanes_or_the_routers_volumes():
    def named(service):
        return {mount.split(":")[0] for mount in service.get("volumes", []) if not mount.startswith("/")}

    for lane in LANES:
        assert named(SERVICES[f"lane-{lane}"]) == {f"lane-{lane}-{kind}" for kind in ("work", "config", "state")}
        host = [mount for mount in SERVICES[f"lane-{lane}"]["volumes"] if "/srv/retinue" in mount]
        assert host == ([] if lane == "web" else ["/srv/retinue/skills:/lane/skills:ro"]) + (
            ["/srv/retinue/owner-memory:/lane/owner-memory:ro", "/srv/retinue/base/tree:/work/base",
             "/srv/retinue/code/mirrors:/lane/mirrors:ro"] if lane == "private" else []), \
            "no host folder but the owner's skills, read-only, and in lane-private his memory, the Base's clone and " \
            "the code's mirrors, read-only"
    for name, service in SERVICES.items():
        if not name.startswith("lane-"):
            assert not {v for v in named(service) if v.startswith("lane-")} - {f"lane-{lane}-work" for lane in LANES}, \
                f"{name}: a lane's config or journal"
            if name != "platform":
                assert not {v for v in named(service) if v.startswith("lane-")}, f"{name}: a lane's work"
    assert "platform-data" not in str({n: s for n, s in SERVICES.items() if n != "platform"})


def test_every_retinue_service_that_listens_reports_its_health_by_its_own_port():
    from retinue.config import CollectorConfig, GatewayConfig, LaneHostConfig, SenderConfig

    ports = {"router": RouterConfig.bus_listen_port, "assistant": AgentConfig.listen_port,
             "collector": CollectorConfig.listen_port, "mail-send": SenderConfig.listen_port,
             "tg-business": GatewayConfig.listen_port, "platform": PlatformConfig.listen_port,
             **{f"lane-{lane}": LaneHostConfig.listen_port for lane in LANES}, "lane-private-sni": 3128}
    assert sorted(name for name, service in SERVICES.items() if "healthcheck" in service) == sorted([*ports, "face"])
    assert SERVICES["face"]["healthcheck"]["test"] == [
        "CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8081/healthz"], "nginx: its loopback-only port"
    for name, port in ports.items():
        check = SERVICES[name]["healthcheck"]
        assert check["test"] == ["CMD", "python", "-c",
                                 f"import socket; socket.create_connection(('127.0.0.1', {port}), 3).close()"], name
        assert check["start_period"] == "60s" and check["retries"] == 3, name
    assert "service_healthy" not in (ROOT / "deploy" / "compose.yml").read_text(), \
        "every caller retries on its own: nothing waits on another's health"


# Every lane's second layer under the gate's: the CLI's own deny rules, which follow links. Not all of /lane: the CLI
# spills a large tool result to <config>/projects/<p>/<s>/tool-results/<id>.txt and tells the model to Read it there.
READ_DENIES = ["Read(//proc/**)", "Read(//lane/state/**)", "Read(//lane/run/**)", "Read(//lane/config/*.json)",
               "Read(//lane/config/claude/*.json)", "Read(//lane/config/claude/**/*.jsonl)",
               "Read(//lane/config/claude/shell-snapshots/**)"]


# What auto mode needs of every lane's managed settings (probed with 2.1.304): with the env scrub off, a sandboxed
# command sees the CLI's environment unless these names are denied to it; and in auto a Bash call's `allowed_domains`
# widens the sandbox's network unless the allowlist is strict — and only the managed one counts.
CREDENTIALS = {"envVars": [{"name": name, "mode": "deny"} for name in cliengine.SECRET_ENV]}
NETWORK_LOCKS = {"strictAllowlist": True, "allowManagedDomainsOnly": True}


@pytest.mark.parametrize("lane", LANES)
def test_every_lanes_managed_settings_keep_the_secrets_from_commands_and_the_network_from_widening(lane):
    managed = json.loads((ROOT / "deploy" / "lanes" / f"managed-settings-{lane}.json").read_text())
    assert managed["sandbox"]["credentials"] == CREDENTIALS
    network = managed["sandbox"]["network"]
    assert {k: network[k] for k in NETWORK_LOCKS} == NETWORK_LOCKS and network["allowedDomains"][0] == "platform"
    assert set(network) == {*NETWORK_LOCKS, "allowedDomains"}, "nothing else: no deniedDomains that could block hooks"
    assert "allowManagedDomainsOnly" not in managed and "strictAllowlist" not in managed, "the keys are network's"
    assert managed["disableSkillShellExecution"] is True, "a skill's `!`command`` runs nothing (docs/instructions.md)"
    assert "allowManagedHooksOnly" not in managed and "allowManagedPermissionRulesOnly" not in managed, \
        "either drops what the platform passes: the run's --settings hooks, its --allowedTools (probed with 2.1.304)"


@pytest.mark.parametrize("lane", SEALED)
def test_the_lanes_managed_settings_deny_the_web_and_hooks_anywhere_but_the_platform(lane):
    managed = json.loads((ROOT / "deploy" / "lanes" / f"managed-settings-{lane}.json").read_text())
    assert managed["permissions"]["deny"] == ["WebSearch", "WebFetch", *READ_DENIES]
    assert managed["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert managed["allowedHttpHookUrls"] == ["http://platform:9500/hook/*"]
    assert "allowManagedHooksOnly" not in managed, "it would drop the run's own --settings hooks"
    network = managed["sandbox"].pop("network")
    assert managed["sandbox"] == {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": False,
                                  "allowUnsandboxedCommands": False,  # no dangerouslyDisableSandbox escape at all
                                  "credentials": CREDENTIALS,
                                  "filesystem": {"denyWrite": ["/lane/config", "/lane/run", "/etc/claude-code"]}}
    assert set(network) == {*NETWORK_LOCKS, "allowedDomains"}, \
        "no httpProxyPort: the CLI's own proxy goes on through HTTPS_PROXY"
    assert network["allowedDomains"][0] == "platform", "the hooks go through its filter"
    assert managed["cleanupPeriodDays"] == 30


def test_the_retired_lanes_keep_their_volumes_and_the_platform_is_off_their_networks():
    """lane-code and lane-web after the room: under a profile the stack never starts, on their own networks without
    the platform, their volumes kept as the archive. A platform.yaml still naming them starts (their tokens stay in
    the platform's environment), and their sessions take nothing in (tests/test_face.py)."""
    platform = SERVICES["platform"]
    for lane in RETIRED:
        service = SERVICES[f"lane-{lane}"]
        assert service["profiles"] == ["legacy-lanes"] and SERVICES[f"lane-{lane}-egress"]["profiles"] == \
            ["legacy-lanes"]
        assert f"lane-{lane}" not in platform["networks"] and f"lane-{lane}" not in platform["depends_on"]
        assert not [m for m in platform["volumes"] if m.startswith(f"lane-{lane}-")], "nor its work volume"
        assert {f"lane-{lane}-{kind}" for kind in ("work", "config", "state")} <= set(COMPOSE["volumes"]), "kept"
        assert f"RETINUE_LANE_TOKEN_{lane.upper()}" in platform["environment"]
    web = SERVICES["lane-web"]
    assert web["volumes"] == ["lane-web-work:/work", "lane-web-config:/lane/config", "lane-web-state:/lane/state"]
    assert web["environment"]["RETINUE_LANE_WEB"] == "1", "its host still reads no rules and no skills"


def test_the_open_proxy_is_open_to_public_https_and_closed_to_everything_inside():
    conf = (ROOT / "deploy" / "egress" / "open-squid.conf").read_text()
    lines = [line.split("#")[0].strip() for line in conf.splitlines() if line.split("#")[0].strip()]
    acls = {tuple(line.split()[1:3]): line.split()[3] for line in lines if line.startswith("acl ")}
    assert ("tls_port", "port") in acls and acls[("tls_port", "port")] == "443"
    assert [line for line in lines if line.startswith("acl inside")] == [
        'acl inside dst "/etc/squid/deny-nets.txt"', 'acl inside dst "/etc/squid/deny-ips.txt"'], \
        "by address, so a name that resolves inside is refused too"
    assert 'acl owners dstdomain -n "/etc/squid/deny-domains.txt"' in lines
    rules = [line for line in lines if line.startswith("http_access")]
    assert rules == ["http_access deny !CONNECT", "http_access deny CONNECT !tls_port", "http_access deny owners",
                     "http_access deny inside", "http_access allow CONNECT", "http_access deny all"], \
        "443 only, the owner's and the inside refused before the one allow"
    assert "cache deny all" in lines and "access_log stdio:/var/log/squid/access.log" in lines
    nets = [ipaddress.ip_network(line) for line in (ROOT / "deploy" / "egress" / "open-deny-nets.txt").read_text()
            .splitlines() if line and not line.startswith("#")]

    def refused(address):
        return any(ipaddress.ip_address(address) in net for net in nets)

    for inside in ("10.8.0.1", "172.17.0.1", "172.16.96.10", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                   "100.64.0.1", "0.0.0.0", "::1", "fdcc:ad94:bacf:61a4::cafe:1", "fe80::1", "64:ff9b::a08:1"):
        assert refused(inside), inside
    for public in ("93.184.215.14", "1.1.1.1", "2606:4700:4700::1111"):
        assert not refused(public), public
    assert not any(net.version == 6 and net.prefixlen < 96 and net.overlaps(ipaddress.ip_network("::ffff:0:0/96"))
                   for net in nets), "Squid keeps IPv4 as mapped IPv6: a ::ffff:0:0/96 entry would refuse every host"
    for name in ("open-deny-ips.txt", "open-deny-domains.txt"):
        text = (ROOT / "deploy" / "egress" / name).read_text()
        assert all(line.startswith("#") for line in text.splitlines()), f"{name}: the operator's, empty in the repo"


def test_the_rooms_managed_settings_leave_the_web_tools_and_keep_bash_off_the_open_web():
    managed = json.loads((ROOT / "deploy" / "lanes" / "managed-settings-private.json").read_text())
    deny = managed["permissions"]["deny"]
    assert not {"WebSearch", "WebFetch", "WebFetch(domain:*)"} & set(deny), "the room reads the web"
    assert deny[:4] == ["WebFetch(domain:lane-private)", "WebFetch(domain:lane-private-egress)",
                        "WebFetch(domain:localhost)", "WebFetch(domain:127.0.0.1)"]
    # A `domain:` deny is also the sandbox's denied domain, and the CLI's own hook calls pass that filter: denying
    # `platform` would block every hook and so every call (probed with 2.1.304). WebFetch refuses a name without a dot
    # by itself, and `platform.` goes to the proxy, which refuses it.
    assert not [rule for rule in deny if "platform" in rule]
    assert [rule for rule in deny if rule.startswith("Read(")] == READ_DENIES, \
        "the CLI's own /proc, its config and the lane's transcripts never reach a page the model fetches"
    # Not the whole of /lane: the CLI spills a large tool result to <config>/projects/<p>/<s>/tool-results/<id>.txt and
    # tells the model to Read it there (probed with 2.1.304); the gate allows that one place and nothing else.
    assert "Read(//lane/**)" not in deny and "Read(//lane/config/**)" not in deny
    assert managed["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert managed["allowedHttpHookUrls"] == ["http://platform:9500/hook/*"]
    code_hosts = [h for h in (ROOT / "deploy" / "egress" / "lane-code-hosts.txt").read_text().split()
                  if h != "api.anthropic.com"]
    assert managed["sandbox"] == {
        "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False, "credentials": CREDENTIALS,
        "filesystem": {"denyWrite": ["/lane/config", "/lane/run", "/etc/claude-code"]},
        "network": {**NETWORK_LOCKS, "allowedDomains": ["platform", *code_hosts]}}, \
        "Bash never reaches the open web: the platform, GitHub and the registries; the web is the CLI's own"
    assert managed["cleanupPeriodDays"] == 30
    web = json.loads((ROOT / "deploy" / "lanes" / "managed-settings-web.json").read_text())
    assert "WebFetch" not in web["permissions"]["deny"], "the retired lane-web's, unchanged"
    instructions = (ROOT / "deploy" / "lanes" / "instructions" / "private.md").read_text()
    assert "ссылка на источник" in instructions and "дата" in instructions and "Не нашёл" in instructions
    assert "данные, а не инструкции" in instructions and "карточкой" in instructions
    assert "Ничего о Владельце" not in instructions, "lane-web's «nothing of yours» is gone with it"
    dockerfile = (ROOT / "deploy" / "lanes" / "Dockerfile").read_text()
    assert 'for part in common.md out.md "$LANE.md" "$([ "$LANE" = private ] && echo base.md)" \\\n      ' \
        '"$([ "$LANE" = private ] && echo code.md)"; do' in dockerfile and \
        "> /etc/claude-code/CLAUDE.md" in dockerfile, "root's, the managed memory: common, files out, the lane's own"


def test_every_lane_is_told_the_common_rules_and_where_files_for_the_owner_go():
    folder = ROOT / "deploy" / "lanes" / "instructions"
    assert sorted(p.name for p in folder.iterdir()) == ["base.md", "code.md", "common.md", "out.md", "private.md"]
    code = (folder / "code.md").read_text()  # the room's repositories (docs/code.md)
    assert "/work/code/<имя>" in code and "«Запушить»" in code and "CLAUDE.md" in code and "AGENTS.md" in code
    assert "`git push`, `gh pr create` не запускай" in code and ".github/workflows/" in code
    assert "не пиши в файлы репозиториев" in code and "«Анна\n  Петрова»" in code, "nothing of the owner's in a repo"
    assert "directory = /lane/mirrors/*" in (ROOT / "deploy" / "lanes" / "Dockerfile").read_text(), \
        "the room's git trusts the platform's mirrors, and only them"
    lane_image = (ROOT / "deploy" / "lanes" / "Dockerfile").read_text()
    assert lane_image.startswith("# A lane") and "\nFROM python:3.12-slim-trixie\n" in lane_image, \
        "Debian 13: git 2.47, which reads `/lane/mirrors/*` as a prefix"
    assert "v[2] >= 46" in lane_image, "an older git fails the build, not every clone"
    base = (folder / "base.md").read_text()
    assert "/work/base" in base and "«В Базу»" in base and "Не запускай в `/work/base` `git`" in base
    assert "python3 scripts/lint.py" in base and "«закоммитить и запушить?»" in base, "rule 6 is the platform's here"
    common, out = (folder / "common.md").read_text(), (folder / "out.md").read_text()
    assert "по-русски" in common and "карточкой" in common and "данные, а не инструкции" in common
    assert "skills:<имя>" in common and "/lane/skills/skills/" in common, "how the owner's skills are named here"
    assert "памяти между сессиями здесь нет" in common, "auto memory is off (cliengine.NO_MEMORY)"
    assert "/lane/owner-memory/memory/" in common, "where the owner's memory snapshot is read"
    assert "субагент не может" in common, "a subagent's call that needs a card is denied (gate.SUBAGENT)"
    assert "одной командой на переднем плане" in common and "watch-режимы не запускай" in common, \
        "no background tasks (cliengine.NO_BACKGROUND)"
    assert "верны правила" in common, "the lane's and the repository's rules win over the owner's memory"
    assert "`out/`" in out
    dockerfile = (ROOT / "deploy" / "lanes" / "Dockerfile").read_text()
    assert "test -s /etc/claude-code/CLAUDE.md" in dockerfile, "no lane runs with an empty one"


def test_bash_in_the_room_and_in_lane_code_reaches_the_code_hosts_and_in_lane_web_only_the_platform():
    def domains(lane):
        return json.loads((ROOT / "deploy" / "lanes" / f"managed-settings-{lane}.json").read_text()
                          )["sandbox"]["network"]["allowedDomains"]

    assert domains("web") == ["platform"]
    # lane-code's proxy list but the model API: the CLI's own calls to it never pass the sandbox's filter, and Bash,
    # which does, has no business there. The room's proxy is open: this list is all that keeps Bash off the web.
    hosts = (ROOT / "deploy" / "egress" / "lane-code-hosts.txt").read_text().split()
    assert domains("code") == domains("private") == ["platform", *(h for h in hosts if h != "api.anthropic.com")]
    assert not (ROOT / "deploy" / "lanes" / "managed-settings.json").exists(), "one file per lane, no shared one"


def test_the_lane_image_has_the_cli_sandbox_and_root_owned_managed_settings():
    dockerfile = (ROOT / "deploy" / "lanes" / "Dockerfile").read_text()
    packages = dockerfile.split("apt-get install")[1].split("\n")[0].split()
    assert "bubblewrap" in packages and "socat" in packages, "without either the CLI's sandbox cannot start"
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile and "pip install" not in dockerfile
    assert "COPY --chown=root:root --chmod=0644 deploy/lanes/managed-settings-${LANE}.json " \
           "/etc/claude-code/managed-settings.json" in dockerfile, "each lane's own, by its build arg"
    assert dockerfile.index("ARG LANE\n") < dockerfile.index("managed-settings-${LANE}.json")
    assert "RUN chmod 0755 /etc/claude-code" in dockerfile, "--chmod also sets the directory: the lane could not read it"


def test_the_lanes_profiles_are_dockers_own_plus_what_bubblewrap_needs():
    seccomp = json.loads((ROOT / "deploy" / "lanes" / "seccomp.json").read_text())
    assert seccomp["defaultAction"] == "SCMP_ACT_ERRNO"
    added = seccomp["syscalls"][-1]
    assert added["action"] == "SCMP_ACT_ALLOW" and sorted(added["names"]) == \
        ["clone", "mount", "pivot_root", "setns", "umount2", "unshare"]
    apparmor = (ROOT / "deploy" / "lanes" / "apparmor").read_text()
    assert "profile retinue-lane flags=(attach_disconnected,mediate_deleted)" in apparmor
    rules = {line.strip() for line in apparmor.splitlines()}
    assert {"userns,", "mount,", "pivot_root,"} <= rules and "deny mount," not in rules
    assert {"deny @{PROC}/sysrq-trigger rwklx,", "deny @{PROC}/kcore rwklx,", "deny /sys/firmware/** rwklx,",
            "deny /sys/kernel/security/** rwklx,", "deny @{PROC}/* w,"} <= rules, "docker-default's denials stay"


def test_the_lane_image_copies_only_what_the_build_context_keeps():
    rules = [line for line in (ROOT / ".dockerignore").read_text().splitlines() if line and not line.startswith("#")]

    def ignored(path):
        verdict = False
        for rule in rules:
            pattern = rule.removeprefix("!")
            parts = path.split("/")
            if any(fnmatch.fnmatchcase("/".join(parts[:n]), pattern) for n in range(1, len(parts) + 1)):
                verdict = not rule.startswith("!")
        return verdict

    copies = re.findall(r"^COPY (?!--from)(?:--\S+ )*(.+) \S+$", (ROOT / "deploy" / "lanes" / "Dockerfile").read_text(), re.M)
    sources = [source.replace("${LANE}", lane) for line in copies for source in line.split() for lane in LANES]
    assert {f"deploy/lanes/managed-settings-{lane}.json" for lane in LANES} <= set(sources)
    assert all((ROOT / source).exists() for source in sources)
    assert [source for source in sources if ignored(source)] == []


def test_setup_with_the_platform(tmp_path, monkeypatch):
    def run(target, **env):
        return subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   **env})

    nobody = run(tmp_path / "a", MATRIX_SERVER_NAME="matrix.example.com", MATRIX_OWNER="alice", PLATFORM="1")
    assert nobody.returncode == 1 and "PLATFORM=1 needs TELEGRAM_OWNER_ID" in nobody.stderr
    _, before = setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42", BACKUP="1")
    target, printed = setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42", PLATFORM="1")
    secrets = ["RETINUE_PLATFORM_TOKEN", "RETINUE_HOOK_SECRET", *(f"RETINUE_LANE_TOKEN_{lane.upper()}" for lane in LANES)]
    assert printed["COMPOSE_PROFILES"] == "platform"
    assert all(len(printed[name]) == 64 for name in secrets) and len({printed[name] for name in secrets}) == 5
    for name in ("TELEGRAM_BOT_TOKEN", "RETINUE_PLATFORM_TOKEN"):
        monkeypatch.setenv(name, printed.get(name) or "t")
    cfg = RouterConfig.load(target / "router.yaml")
    assert (cfg.platform_url, cfg.platform_token) == ("http://platform:9500", printed["RETINUE_PLATFORM_TOKEN"])
    # The code's folders (docs/code.md): the mirrors, the tokens' file root's alone and empty, the platform's way out.
    assert (target / "code" / "mirrors").is_dir() and (target / "code" / "tokens.env").read_text() == ""
    assert oct((target / "code" / "tokens.env").stat().st_mode & 0o777) == "0o600"
    assert (target / "egress" / "platform-hosts.txt").read_text().split() == ["github.com", "api.github.com",
                                                                              "gitlab.com", "api.elevenlabs.io"]
    # A list written before the voice (no marker yet, its last line without a newline): ElevenLabs added once, the
    # owner's own lines kept; removed by him later, it stays removed.
    hosts = target / "egress" / "platform-hosts.txt"
    (target / "egress" / ".platform-hosts-speech").unlink()
    hosts.write_text("github.com\ncodeberg.org")
    setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42", PLATFORM="1")
    setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42", PLATFORM="1")
    assert hosts.read_text() == "github.com\ncodeberg.org\napi.elevenlabs.io\n"
    hosts.write_text("github.com\n")
    setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42", PLATFORM="1")
    assert hosts.read_text() == "github.com\n", "written once, then the owner's"
    platform_yaml = target / "platform" / "platform.yaml"
    assert platform_yaml.read_text() == (ROOT / "deploy" / "platform" / "platform.example.yaml").read_text()
    assert (target / "skills").is_dir() and not any((target / "skills").iterdir()), \
        "the lanes' skills mount: empty until deploy/skills/install-skills.sh fills it"
    assert (target / "owner-memory").is_dir() and not any((target / "owner-memory").iterdir()), \
        "the room's memory mount: empty until deploy/memory/install-owner-memory.sh fills it"
    assert (target / "base" / "tree").is_dir() and not any((target / "base" / "tree").iterdir()), \
        "the Base's clone: an empty folder the platform clones into once `base:` is configured"
    assert (target / "platform" / "lane-seccomp.json").read_bytes() == \
        (ROOT / "deploy" / "lanes" / "seccomp.json").read_bytes()
    assert "seccomp=/srv/retinue/platform/lane-seccomp.json" in SERVICES["lane-code"]["security_opt"]
    for name in secrets:
        monkeypatch.setenv(name, printed[name])
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "login")
    platform = PlatformConfig.load(platform_yaml)
    assert sorted(platform.lanes) == ["private"] and platform.state_db == "/platform/platform.sqlite"
    assert platform.lanes["private"].web is True and platform.lanes["private"].max_sessions == 12 == \
        platform.max_sessions, "the owner and up to 10 sub-sessions (docs/platform.md, «Sub-sessions»)"
    assert SERVICES["lane-private"]["environment"]["RETINUE_LANE_MAX"] == "12", \
        "the room's host runs as many CLI processes as platform.yaml gives the room: no hidden lower cap"
    assert {(t.name, t.lane, t.kind, t.cwd) for t in platform.tracks} == {
        ("notes", "private", "private", "base"), ("research", "private", "private", "base"),
        ("base", "private", "private", "base"), ("code", "private", "code", "code")}
    assert {s.track for s in platform.spheres} == {"base"} and {s.cwd for s in platform.spheres} == {""}, \
        "every sphere's session starts in the root of the Base"
    assert not [rule for t in platform.tracks for rule in t.allow if rule.startswith("WebFetch")], \
        "a fetch stays the classifier's"
    for lane, ref in platform.lanes.items():
        assert ref.url == f"http://lane-{lane}:9600" and ref.hook_url == SERVICES[f"lane-{lane}"]["environment"][
            "RETINUE_LANE_HOOK_URL"]
        assert f"lane-{lane}-work:{ref.work_mount}:ro" in SERVICES["platform"]["volumes"]
        assert ref.token == printed[f"RETINUE_LANE_TOKEN_{lane.upper()}"]
        if lane in SEALED:
            hosts = target / "egress" / f"lane-{lane}-hosts.txt"
            assert hosts.read_text() == (ROOT / "deploy" / "egress" / f"lane-{lane}-hosts.txt").read_text()
    assert not (target / "egress" / "lane-private-hosts.txt").exists(), "an open proxy: no list of hosts"
    for name in OPEN_FILES:
        assert (target / "egress" / name).read_text() == (ROOT / "deploy" / "egress" / name).read_text(), name
    assert 'RETINUE_VOLUMES="retinue_router-data retinue_assistant-data retinue_platform-data"' in \
        (target / "backup.env").read_text(), "the transcripts are in the platform's database; not the lanes' volumes"
    platform_yaml.write_text(platform_yaml.read_text() + "# the owner's\n")
    (target / "egress" / "lane-code-hosts.txt").write_text("api.anthropic.com\n")
    (target / "egress" / "open-deny-domains.txt").write_text(".example.com\n")
    (target / "egress" / "open-deny-ips.txt").write_text("203.0.113.7/32\n")
    (target / "egress" / "open-deny-nets.txt").write_text("# emptied\n")
    _, again = setup(tmp_path / "plain", TELEGRAM_OWNER_ID="42")
    assert all(again[name] == printed[name] for name in secrets), "on for good, the same tokens"
    assert platform_yaml.read_text().endswith("# the owner's\n"), "written once, then the owner's"
    assert (target / "egress" / "lane-code-hosts.txt").read_text() == "api.anthropic.com\n"
    assert (target / "egress" / "open-deny-domains.txt").read_text() == ".example.com\n", "the owner's"
    assert (target / "egress" / "open-deny-ips.txt").read_text() == "203.0.113.7/32\n", "the owner's"
    assert (target / "egress" / "open-deny-nets.txt").read_text() == \
        (ROOT / "deploy" / "egress" / "open-deny-nets.txt").read_text(), "ours: put back every run"
    travel = run(target, TELEGRAM_OWNER_ID="42", TRAVEL="1")
    assert travel.returncode == 0, travel.stderr
    assert 'RETINUE_VOLUMES="retinue_router-data retinue_assistant-data retinue_travel-data retinue_platform-data"' \
        in (target / "backup.env").read_text()
    other, _ = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42", BACKUP="1")
    assert not (other / "platform").exists() and "platform_url" not in (other / "router.yaml").read_text()
    assert not list((other / "egress").glob("lane-*")) and not list((other / "egress").glob("open-*"))
    assert "RETINUE_VOLUMES" not in (other / "backup.env").read_text()
    assert not set(secrets) & set(before) and "COMPOSE_PROFILES" not in before, "without it, nothing of it"


def test_setup_puts_right_the_empty_directories_docker_made_for_the_open_proxys_files(tmp_path):
    """`docker compose up` before setup.sh ever wrote these files leaves an empty directory at each bind source; the
    next run replaces it with the file. A directory with something in it is not guessed at: the run fails."""
    egress = tmp_path / "srv" / "egress"
    for name in OPEN_FILES:
        (egress / name).mkdir(parents=True)
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42", PLATFORM="1")
    for name in OPEN_FILES:
        assert (target / "egress" / name).is_file(), name
        assert (target / "egress" / name).read_text() == (ROOT / "deploy" / "egress" / name).read_text(), name
    (target / "egress" / "open-deny-nets.txt").unlink()
    (target / "egress" / "open-deny-nets.txt").mkdir()
    (target / "egress" / "open-deny-nets.txt" / "something").write_text("x")
    run = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                              "TELEGRAM_OWNER_ID": "42"})
    assert run.returncode != 0 and "open-deny-nets.txt" in run.stderr


def test_setup_takes_over_the_owners_lists_lane_web_had_once(tmp_path):
    """Before the room the owner's zone and this server's addresses were lane-web's lists; the first run after it
    copies them to the open proxy's, as they are, and never again."""
    egress = tmp_path / "srv" / "egress"
    egress.mkdir(parents=True)
    (egress / "lane-web-deny-domains.txt").write_text("# mine\n.example.com\n")
    (egress / "lane-web-deny-ips.txt").write_text("203.0.113.7/32\n")
    target, _ = setup(tmp_path, TELEGRAM_OWNER_ID="42", PLATFORM="1")
    assert (target / "egress" / "open-deny-domains.txt").read_text() == "# mine\n.example.com\n"
    assert (target / "egress" / "open-deny-ips.txt").read_text() == "203.0.113.7/32\n"
    (target / "egress" / "open-deny-domains.txt").write_text(".example.org\n")
    (target / "egress" / "lane-web-deny-domains.txt").write_text(".example.net\n")
    setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert (target / "egress" / "open-deny-domains.txt").read_text() == ".example.org\n", "the owner's from then on"


def test_setup_fills_this_servers_addresses_again_while_the_list_has_none_and_says_so(tmp_path):
    def run(target):
        done = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   "TELEGRAM_OWNER_ID": "42", "PLATFORM": "1"})
        assert done.returncode == 0, done.stderr
        return done.stdout

    target = tmp_path / "srv"
    printed = run(target)
    ips = target / "egress" / "open-deny-ips.txt"
    assert "no address of this server" in printed and "write your own domains" in printed, "both lists are empty"
    assert "restart lane-private-egress" in printed, "the files were written: the proxy reads them at its start"
    assert "files changed" not in run(target), "the same files again: nothing to restart"
    assert ips.read_text() == (ROOT / "deploy" / "egress" / "open-deny-ips.txt").read_text()
    ips.write_text("# the example's comments, cut short\n")
    run(target)
    assert ips.read_text() == (ROOT / "deploy" / "egress" / "open-deny-ips.txt").read_text(), "no entry: again"
    ips.write_text("203.0.113.7/32\n")
    (target / "egress" / "open-deny-domains.txt").write_text(".example.com\n")
    printed = run(target)
    assert ips.read_text() == "203.0.113.7/32\n", "an entry: the owner's from then on"
    assert "files changed" not in printed and "no address" not in printed and "your own domains" not in printed
    (target / "egress" / "open-deny-domains.txt").write_text(".example.com\n.example.org\n")
    assert "files changed" not in run(target), "an edit by hand is the owner's to restart for; setup changed nothing"
    assert not list((target / "egress").glob(".open-deny-ips.*")), "no temporary file left behind"
    setup_sh = (ROOT / "deploy" / "setup.sh").read_text()
    assert 'mv -f "$ips_tmp" "$ROOT/egress/open-deny-ips.txt"' in setup_sh, "written whole or not at all"
    failed = setup_sh.split('if ! fill_open_ips > "$ips_tmp"; then')[1].split("fi")[0]
    assert 'rm -f "$ips_tmp"' in failed and "exit 1" in failed, "a failed fill leaves no temporary file and stops"


FACE_IP = "172.16.96.10"
FACE_CSP = ("default-src 'self'; img-src 'self' data:; connect-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "object-src 'none'")


def test_the_face_is_nginx_read_only_behind_traefik_for_the_owners_devices():
    """The face: the built app and nginx, hardened like the life hub; on Traefik's network and the face API's, nothing
    else; Traefik lets through the owner's devices only (404 for anyone else) and adds the key nginx checks."""
    face = SERVICES["face"]
    assert face["profiles"] == ["face"], "off until the owner turns it on"
    assert face["build"] == {"context": "..", "dockerfile": "deploy/face/Dockerfile"} and face["image"] == "retinue-face:local"
    assert face["read_only"] is True and face["cap_drop"] == ["ALL"] and face["init"] is True
    assert face["security_opt"] == ["no-new-privileges:true"] and face["tmpfs"] == ["/tmp:size=16m"]
    assert face["mem_limit"] and face["pids_limit"] and "ports" not in face and "user" not in face
    assert face["volumes"] == ["/srv/retinue/face/key.conf:/etc/nginx/face/key.conf:ro"], "no secret but the key's check"
    assert "environment" not in face or set(face["environment"]) == {"NGINX_ENTRYPOINT_QUIET_LOGS"}
    assert face["networks"] == ["dokploy-network", "face-platform"]
    labels = dict(label.split("=", 1) for label in face["labels"])
    assert labels == {
        "traefik.enable": "true",
        "traefik.docker.network": "dokploy-network",
        "traefik.http.routers.retinue-face.rule": "Host(`${FACE_HOST:-}`)",
        "traefik.http.routers.retinue-face.entrypoints": "websecure",
        "traefik.http.routers.retinue-face.tls.certresolver": "letsencrypt",
        "traefik.http.routers.retinue-face.middlewares": "face-owner@file,face-key@file",
        "traefik.http.routers.retinue-face-http.rule": "Host(`${FACE_HOST:-}`)",
        "traefik.http.routers.retinue-face-http.entrypoints": "web",
        "traefik.http.routers.retinue-face-http.middlewares": "face-owner@file,face-https@file",
        "traefik.http.services.retinue-face.loadbalancer.server.port": "8080"}
    template = yaml.safe_load((ROOT / "deploy" / "face" / "traefik.yml.template").read_text().replace(
        "${FACE_RANGES}", '"10.8.0.2/32"'))["http"]["middlewares"]
    assert template["face-owner"] == {"ipAllowList": {"sourceRange": ["10.8.0.2/32"], "rejectStatusCode": 404}}
    assert template["face-key"] == {"headers": {"customRequestHeaders": {"X-Face-Key": "${FACE_KEY}"}}}
    assert template["face-https"] == {"redirectScheme": {"scheme": "https", "permanent": True}}
    docker = (ROOT / "deploy" / "face" / "Dockerfile").read_text()
    stages = re.findall(r"^FROM (\S+)", docker, re.M)
    assert len(stages) == 2 and re.fullmatch(r"node:\d+\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}", stages[0]), "pinned"
    assert stages[1] == SERVICES["lifehub"]["image"], "the life hub's nginx, the same pin"
    package = json.loads((ROOT / "face" / "package.json").read_text())
    assert f"corepack prepare {package['packageManager']} --activate" in docker, "the pnpm of package.json, exactly"
    assert "pnpm install --frozen-lockfile" in docker and "RUN pnpm build" in docker
    assert "COPY deploy/face/nginx.conf /etc/nginx/conf.d/default.conf" in docker
    assert "COPY --from=build /face/dist /usr/share/nginx/face" in docker
    ignore = (ROOT / "deploy" / "face" / "Dockerfile.dockerignore").read_text().split("\n")
    assert {"*", "!face", "face/node_modules", "face/dist", "!deploy/face/nginx.conf"} <= set(ignore)


def test_the_face_api_is_on_a_network_of_its_own_that_no_lane_is_on():
    """The platform's face API listens on its own address on `face-platform` — the same address compose gives it,
    nginx proxies to, and RETINUE_FACE_HOST names; the router, the lanes and their proxies are not on that network,
    and the face is on no network of the platform's but that one."""
    network = COMPOSE["networks"]["face-platform"]
    assert network["internal"] is True, "no route out"
    subnet = ipaddress.ip_network(network["ipam"]["config"][0]["subnet"])
    assert not any(subnet.overlaps(ipaddress.ip_network(f"172.{n}.0.0/16")) for n in range(17, 32)) and \
        not subnet.overlaps(ipaddress.ip_network("192.168.0.0/16")) and \
        not subnet.overlaps(ipaddress.ip_network("10.0.0.0/8")), "outside Docker's and Swarm's default pools, the VPN"
    members = sorted(name for name, service in SERVICES.items() if "face-platform" in service.get("networks", []))
    assert members == ["face", "platform"], "no lane, no proxy, not the router"
    platform = SERVICES["platform"]
    assert platform["networks"]["face-platform"] == {"ipv4_address": FACE_IP}
    assert ipaddress.ip_address(FACE_IP) in subnet
    env = platform["environment"]
    assert env["RETINUE_FACE_HOST"] == FACE_IP, "the face API's own address, never 0.0.0.0"
    assert env["RETINUE_FACE_ORIGIN"] == "${RETINUE_FACE_ORIGIN:-}" and env["RETINUE_WEBAPP_KEY"] == "${RETINUE_WEBAPP_KEY:-}"
    assert env["TELEGRAM_OWNER_ID"] == "${TELEGRAM_OWNER_ID:-}"
    assert "TELEGRAM_BOT_TOKEN" not in str(platform) + str(SERVICES["face"]), "the token stays the router's"
    nginx = (ROOT / "deploy" / "face" / "nginx.conf").read_text()
    assert set(re.findall(r"proxy_pass (\S+);", nginx)) == {f"http://{FACE_IP}:{PlatformConfig.face_port}"}
    for lane in LANES:
        assert SERVICES[f"lane-{lane}"]["networks"] == [f"lane-{lane}"]
        assert SERVICES[f"lane-{lane}-egress"]["networks"] == ["room-out" if lane == ROOM else f"lane-{lane}",
                                                                "outside"]
    assert "face-platform" not in SERVICES["lane-private-sni"]["networks"]
    assert "dokploy-network" not in SERVICES["platform"]["networks"], "Traefik's neighbours never reach the platform"


def test_face_nginx_serves_every_app_route_and_a_records_path_however_deep_the_rule_lets_it():
    """nginx takes the first regex location that matches, in the config's order: a Record's page (`.md`, up to three
    levels inside its Space) must reach the app, not the dot-file 404 before it; a dot-led part must not."""
    nginx = (ROOT / "deploy" / "face" / "nginx.conf").read_text()
    server = nginx.split("server {")[1]
    regexes = [m.group(1).strip('"') for m in re.finditer(r"location ~ (\S+) \{", server)]

    def served(uri: str) -> str:
        found = next((r for r in regexes if re.search(r, uri)), None)
        return "app" if found and "today" in found else "dot" if found == r"/\." else str(found)

    for uri in ("/", "/login", "/today", "/sessions", "/new", "/read", "/spaces", "/spaces/travel",
                "/spaces/travel/r/a.md", "/spaces/travel/r/knowledge/ensk-transit.md",
                "/spaces/work/r/profile/acme/2026-10-01-x.v2.md", "/s/a1b2c3d4", "/a/5f3a9c21", "/f/f1",
                "/b/b1", "/p/p1"):
        assert served(uri) == "app", uri
    for uri in ("/spaces/travel/r/.scratch/x.md", "/spaces/travel/r/knowledge/.x.md"):
        assert served(uri) == "dot", uri
    for uri in ("/todayx", "/newx", "/new/x", "/spaces/travel/x", "/s/a/b", "/b/b1/x"):
        assert served(uri) not in ("app", "dot"), uri


def test_face_nginx_serves_the_app_and_streams_the_events():
    nginx = (ROOT / "deploy" / "face" / "nginx.conf").read_text()
    readme = (ROOT / "face" / "README.md").read_text()
    assert f"Content-Security-Policy: {FACE_CSP}" in readme, "the policy the app was written for"
    csp = nginx.split("map $uri $face_csp {")[1].split("}")[0]
    frame = nginx.split("map $uri $face_frame_options {")[1].split("}")[0]
    assert f'default                     "{FACE_CSP}";' in csp and 'default                     "DENY";' in frame
    for view in (csp, frame):  # a handed-over HTML file in the face's frame: the platform's own sandbox policy
        assert [line.split() for line in view.strip().splitlines()][0] == ["~^/api/files/[^/]+/view$", '"";']
    for header in ('Strict-Transport-Security "max-age=31536000"', 'X-Content-Type-Options "nosniff"',
                   "Content-Security-Policy $face_csp", "X-Frame-Options $face_frame_options",
                   'Referrer-Policy "no-referrer"', 'X-Robots-Tag "noindex, nofollow"',
                   "Cache-Control $face_cache_control"):
        assert f"add_header {header} always;" in nginx, header
    permissions = re.search(r'add_header Permissions-Policy "([^"]+)" always;', nginx)[1].split(", ")
    assert permissions[0] == "microphone=(self)" and all(p.endswith("=()") for p in permissions[1:])
    assert {"camera=()", "geolocation=()", "payment=()"} <= set(permissions)
    server = nginx.split("server {")[1]
    locations = re.findall(r"^    location ", server, re.M)
    assert "add_header" not in server.split("    location ", 1)[1], "a location's add_header would drop the others"
    assert len(locations) >= 6
    assert "include /etc/nginx/face/key.conf;" in server and "listen 8080;" in server
    assert "client_max_body_size 1m;" in server and "root /usr/share/nginx/face;" in server
    cache = nginx.split("map $uri $face_cache_control {")[1].split("}")[0]
    assert '~^/assets/      "public, max-age=31536000, immutable";' in cache
    assert '~^/api/         "";' in cache and 'default         "no-cache";' in cache, \
        "index.html, sw.js and the manifest revalidate; the API keeps its own no-store"
    assert "location ~ ^/(|login|today|sessions|new|read|spaces|spaces/[^/]+|spaces/[^/]+/r/.+|s/[^/]+|a/[^/]+|f/[^/]+|b/[^/]+|p/[^/]+)$ { try_files /index.html =404; }" in server, \
        "the app's routes only"
    assert "location / { try_files $uri =404; }" in server and "location ~ /\\. { return 404; }" in server
    for passed in ("Cookie $http_cookie", "Origin $http_origin", "X-Retinue $http_x_retinue", 'X-Face-Key ""',
                   'Connection ""'):
        assert f"proxy_set_header {passed};" in server, passed
    assert "proxy_http_version 1.1;" in server
    events = server.split("location = /api/events {")[1].split("}")[0]
    for line in ("proxy_buffering off;", "proxy_cache off;", "proxy_read_timeout 1h;", "gzip off;"):
        assert line in events, line
    assert "text/event-stream" not in nginx.split("gzip_types")[1].split(";")[0], "no gzip on the stream"
    # A message with attachments: the platform's own limit fits, and the body is never spooled to the small /tmp.
    from retinue import inbox
    inputs = server.split('location ~ "^/api/sessions(/[0-9a-f]{8}/input)?$" {')[1].split("}")[0]
    size = int(re.search(r"client_max_body_size (\d+)m;", inputs)[1]) * 1024 ** 2
    assert inbox.BODY_MAX <= size and "proxy_request_buffering off;" in inputs
    assert "proxy_pass http://172.16.96.10:9600;" in inputs
    health = nginx.split("server {")[2]
    assert "listen 127.0.0.1:8081;" in health and "location = /healthz { return 204; }" in health


def test_setup_with_face(tmp_path, monkeypatch):
    def run(target, **env):
        return subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0",
                                   "TELEGRAM_OWNER_ID": "42", **env})

    alone = run(tmp_path / "a", FACE="1", FACE_HOST="app.in.example.com", FACE_DEVICES="10.8.0.3/32")
    assert alone.returncode == 1 and "FACE=1 needs PLATFORM=1" in alone.stderr
    nameless = run(tmp_path / "b", PLATFORM="1", FACE="1", FACE_DEVICES="10.8.0.3/32")
    assert nameless.returncode == 1 and "FACE_HOST" in nameless.stderr
    blind = run(tmp_path / "c", PLATFORM="1", FACE="1", FACE_HOST="app.in.example.com", FACE_DEVICES="10.8.0.0/24")
    assert blind.returncode == 1 and "FACE_DEVICES" in blind.stderr
    assert not (tmp_path / "c" / "face" / "host").exists(), "nothing kept from a refused run"

    target, printed = setup(tmp_path, PLATFORM="1", FACE="1", FACE_HOST="app.in.example.com",
                            TELEGRAM_OWNER_ID="42", FACE_DEVICES="10.8.0.3/32")
    face = target / "face"
    assert printed["COMPOSE_PROFILES"] == "platform,face" and printed["FACE_HOST"] == "app.in.example.com"
    assert printed["RETINUE_FACE_ORIGIN"] == "https://app.in.example.com" and printed["TELEGRAM_OWNER_ID"] == "42"
    assert "RETINUE_WEBAPP_KEY" not in printed, "no bot token yet: no Mini App login, codes still work"
    assert (face / "host").read_text() == "app.in.example.com\n" and (face / "devices.txt").read_text() == "10.8.0.3/32\n"
    key = dict(line.split("=", 1) for line in (target / "secrets.env").read_text().splitlines())["FACE_KEY"]
    assert len(key) == 64
    assert (face / "key.conf").read_text() == f'if ($http_x_face_key != "{key}") {{ return 404; }}\n'
    assert oct((face / "key.conf").stat().st_mode & 0o777) == "0o640"
    traefik = yaml.safe_load((face / "traefik.yml").read_text())["http"]["middlewares"]
    assert traefik["face-owner"]["ipAllowList"] == {"sourceRange": ["10.8.0.3/32"], "rejectStatusCode": 404}
    assert traefik["face-key"]["headers"]["customRequestHeaders"] == {"X-Face-Key": key}
    assert oct((face / "traefik.yml").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RETINUE_PLATFORM_TOKEN", printed["RETINUE_PLATFORM_TOKEN"])
    assert RouterConfig.load(target / "router.yaml").face_url == "https://app.in.example.com"

    # The Mini App's key: HMAC_SHA256(key="WebAppData", msg=<bot token>), from the token the owner pasted; a new
    # token, a new key; neither printed.
    stack = target / "stack.env"
    token = "123456789:AAH-test_token-for_the_face"
    stack.write_text(re.sub(r"(?m)^TELEGRAM_BOT_TOKEN=.*$", f"TELEGRAM_BOT_TOKEN={token}", stack.read_text()))
    again = run(target)
    assert again.returncode == 0, again.stderr
    written = dict(line.split("=", 1) for line in stack.read_text().splitlines() if "=" in line)
    expected = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).hexdigest()
    assert written["RETINUE_WEBAPP_KEY"] == expected, "on for good: no switch needed"
    assert written["TELEGRAM_BOT_TOKEN"] == token, "kept"
    for value in (token, expected, key):
        assert value not in again.stdout + again.stderr, "never printed"
    assert "Face: on — https://app.in.example.com" in again.stdout
    stack.write_text(stack.read_text().replace(token, '"999:other"'))
    run(target)
    written = dict(line.split("=", 1) for line in stack.read_text().splitlines() if "=" in line)
    assert written["RETINUE_WEBAPP_KEY"] == hmac.new(b"WebAppData", b"999:other", hashlib.sha256).hexdigest()

    for name in (*(f"RETINUE_LANE_TOKEN_{lane.upper()}" for lane in LANES), "RETINUE_HOOK_SECRET"):
        monkeypatch.setenv(name, written[name])
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "login")
    platform_env = SERVICES["platform"]["environment"]
    monkeypatch.setenv("RETINUE_FACE_HOST", platform_env["RETINUE_FACE_HOST"])
    for name in ("RETINUE_FACE_ORIGIN", "RETINUE_WEBAPP_KEY", "TELEGRAM_OWNER_ID"):
        monkeypatch.setenv(name, written[name])
    cfg = PlatformConfig.load(target / "platform" / "platform.yaml")
    assert (cfg.face_host, cfg.face_origin, cfg.owner_tg) == (FACE_IP, "https://app.in.example.com", 42)
    assert cfg.webapp_key == written["RETINUE_WEBAPP_KEY"] and cfg.face_port == 9600

    # With the life hub on, its list of devices is the face's: one list.
    hub = run(target, LIFEHUB="1", LIFEHUB_HOST="hub.in.example.com", LIFEHUB_DEVICES="10.8.0.2/32,10.8.0.5/32")
    assert hub.returncode == 0, hub.stderr
    assert not (face / "devices.txt").exists()
    traefik = yaml.safe_load((face / "traefik.yml").read_text())["http"]["middlewares"]
    assert traefik["face-owner"]["ipAllowList"]["sourceRange"] == ["10.8.0.2/32", "10.8.0.5/32"]
    twice = run(target, FACE_DEVICES="10.8.0.9/32")
    assert twice.returncode == 1 and "LIFEHUB_DEVICES" in twice.stderr

    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42", PLATFORM="1")
    assert "face_url" not in (plain / "router.yaml").read_text() and not (plain / "face").exists()
    assert not {"FACE_HOST", "RETINUE_FACE_ORIGIN", "RETINUE_WEBAPP_KEY"} & set(printed)
    assert "face" not in printed["COMPOSE_PROFILES"].split(",")


def test_the_face_docs_say_how_to_turn_it_on_and_check_it():
    doc = (ROOT / "docs" / "face.md").read_text()
    for needle in ("FACE=1", "FACE_HOST=app.in.", "app.in.", "10.8.0.1", "Add to Home Screen", "/app",
                   f"{FACE_IP}", "lane-private", "404", "frame-ancestors"):
        assert needle in doc, needle


def _bytes(size: str) -> int:
    number, unit = re.fullmatch(r"(\d+)([kmg])", size.lower()).groups()
    return int(number) * 1024 ** "kmg".index(unit) * 1024


# Owner-started, idle the rest of the time; the room's SNI guard runs with the room.
BURSTS = {"lane-private", "lane-private-sni", "lane-code", "lane-web", "travel-ops", "travel-watch"}


def test_every_service_has_a_memory_ceiling_and_the_always_on_part_stays_small():
    """The host is shared with other services: a runaway in any service is capped, and what runs all the time fits
    with room to spare. The ceilings come from `docker stats` and the cgroups' peaks."""
    limits = {name: _bytes(str(service.get("mem_limit", "0k"))) for name, service in SERVICES.items()}
    assert all(limits.values()), f"no ceiling: {[name for name, size in limits.items() if not size]}"
    # The retired lanes (profile legacy-lanes) never run with the stack: they take nothing of the always-on part.
    always = sum(size for name, size in limits.items() if name not in BURSTS and name not in LEGACY_SERVICES)
    assert always <= 8 * 1024 ** 3, f"always-on ceilings: {always / 1024 ** 3:.1f} GiB"
    assert all(limits[name] <= 4 * 1024 ** 3 for name in BURSTS - {"lane-private"}), \
        "a burst never takes more than 4 GiB"
    # The room is a burst (owner-started; idle the rest of the time), so it stays out of the always-on sum: 11 GiB for
    # the owner and up to 10 sub-sessions at once.
    assert limits["lane-private"] == 11 * 1024 ** 3, "the room: the owner and up to 10 sub-sessions, the one lane"
    # The router's file parser runs in its container, capped at its own 1.5 GiB: the router must outlast it.
    from retinue import attachments
    assert limits["router"] >= attachments.PARSE_MEMORY + 256 * 1024 ** 2
    # The platform reads the owner's pictures for a session the same way, one worker at a time, capped lower. An
    # address space is more than what a process holds (the libraries are mapped, not resident): 3/4 of it is room.
    from retinue import inbox
    assert inbox.READERS == 1 and limits["platform"] >= inbox.READER_MEMORY * 3 // 4
    assert limits["assistant"] >= 2 * 1024 ** 3, "three Claude Code runs at once"
    proxies = [name for name, service in SERVICES.items() if "squid" in str(service.get("image"))]
    assert sorted(proxies) == ["egress", "lane-code-egress", "lane-private-egress", "lane-web-egress", "mail-egress",
                               "platform-egress"]
    assert {limits[name] for name in proxies if name != "platform-egress"} == {128 * 1024 ** 2}
    assert limits["platform-egress"] == 64 * 1024 ** 2


def test_every_image_is_pinned_by_digest_or_built_here():
    """A deploy runs exactly what was checked: a tag can be moved by its owner, a digest cannot. Built images are
    named `*:local`; their own base images are pinned in their Dockerfiles or by the commit they are built from."""
    files = [ROOT / "deploy" / "compose.yml", ROOT / "deploy" / "split-dns" / "compose.yml",
             ROOT / "tests" / "e2e" / "compose.yml"]
    for path in files:
        for name, service in yaml.safe_load(path.read_text())["services"].items():
            image = service.get("image", "")
            if "build" in service or image.endswith(":local"):
                continue
            assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), f"{path.name}: {name}: {image}"
    squid = {SERVICES[name]["image"] for name in ("egress", "mail-egress", "lane-code-egress", "lane-private-egress")}
    assert len(squid) == 1, "one pin for the four proxies"
    assert yaml.safe_load(files[2].read_text())["services"]["tuwunel"]["image"] == SERVICES["tuwunel"]["image"]


@pytest.mark.skipif(not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
                    reason="needs Docker")
def test_the_faces_nginx_config_loads_in_its_own_image(tmp_path):
    """Text tests of the config miss what only nginx sees: an unquoted `{8}` in a location regex took the face down."""
    image = re.search(r"^FROM (nginxinc/nginx-unprivileged\S+)", (ROOT / "deploy" / "face" / "Dockerfile").read_text(), re.M)[1]
    (tmp_path / "key.conf").write_text('set $face_key "x";\n')
    run = subprocess.run(["docker", "run", "--rm", "--add-host", "platform:127.0.0.1",
                          "-v", f"{ROOT / 'deploy' / 'face' / 'nginx.conf'}:/etc/nginx/conf.d/default.conf:ro",
                          "-v", f"{tmp_path}:/etc/nginx/face:ro", image, "nginx", "-t"],
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr
