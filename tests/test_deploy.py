"""What gets deployed: deploy/compose.yml and the files deploy/setup.sh writes. No Docker and no root needed."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from retinue.bus import bus_token
from retinue.config import AgentConfig, RouterConfig

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
                                "move_reminder", "get_attachment"], "the archive, reminders, attachments; no ask_agent"
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
    assert "отправлять письма" in instructions.split("## Что ты умеешь сейчас")[1], "she cannot send mail and says so"
    guide = (ROOT / "docs" / "mail.md").read_text()
    assert "MAIL=1" in guide and "deploy/mail/login.py" in guide and "In production" in guide
    assert "Only if\nthe sender is known" in guide and "MAIL=1" in (ROOT / "docs" / "install.md").read_text()
    assert sorted(SERVICES) == ["assistant", "collector", "egress", "mail-egress", "router", "travel-ops",
                                "travel-watch", "tuwunel"]
    assert sorted(COMPOSE["volumes"]) == ["assistant-data", "router-data", "travel-data", "tuwunel-db"]


def test_only_the_router_holds_the_speech_key():
    assert SERVICES["router"]["environment"]["ELEVENLABS_API_KEY"] == "${ELEVENLABS_API_KEY:-}"
    assert "ELEVENLABS_API_KEY" not in SERVICES["assistant"]["environment"], "the assistant never sees the key"


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
    assert [(a.id, a.archive, a.reminders, a.attachments, a.can_call) for a in cfg.agents] == [
        ("assistant", True, True, True, [])]
    assert cfg.owner_tz == "Europe/Moscow"
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
    assert "OnCalendar=*-*-* 03:30 Europe/Moscow" in timer and "Persistent=true" in timer
    service = (ROOT / "deploy" / "backup" / "retinue-backup.service").read_text()
    assert "Type=oneshot" in service and "EnvironmentFile=/srv/retinue/backup.env" in service
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/retinue/retinue-backup.py" in service
    script = (ROOT / "deploy" / "setup.sh").read_text()
    assert "/usr/local/lib/retinue/retinue-backup.py" in script and "RESTIC_VERSION=0.19.1" in script
    assert "sha256sum -c" in script, "the restic binary is checked against a pinned hash"


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
    import re

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
    assert re.fullmatch(r"https://github\.com/redikultsev/travel-ops\.git#[0-9a-f]{40}", search["build"]["context"]), \
        "built from a pinned commit of the public repo"
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


def test_the_travel_ops_commit_is_bumped_before_retinue_is_committed():
    """The plan's placeholder is travel-ops' base commit, which has no Dockerfile: compose would fail to build.
    This test is red until the SHA of the pushed travel-ops commit replaces it (plan, step С1)."""
    context = SERVICES["travel-ops"]["build"]["context"]
    assert not context.endswith("#a0acb3f18c5042f4942f9bd9876610aec02de069"), \
        "bump the travel-ops SHA in deploy/compose.yml to the pushed commit with the Dockerfile (plan 8в, С1)"


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
    links = ["https://www.aviasales.ru/search/BEG2210TGD23101", "https://avia.tutu.ru/f/Belgrad/Podgoritsa/",
             "https://www.onetwotrip.com/ru/f/search/x", "https://www.kupibilet.ru/search?x=1",
             "https://travel.wildberries.ru/avia/x", "https://kiwi.com/u/u6xbs4",
             "https://www.google.com/travel/flights?tfs=x", "https://www.trip.com/flights/showfarefirst?x=1",
             "https://www.booking.com/hotel/me/golden-bay-apartman.en-gb.html", "https://www.airbnb.com/rooms/1",
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
    plain, printed = setup(tmp_path / "other", TELEGRAM_OWNER_ID="42")
    assert not (plain / "travel").exists() and "travel" not in (plain / "router.yaml").read_text()
    assert printed["RETINUE_TRAVEL_URL"] == "", "no server for the assistant either"


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
    raw = json.loads((memory / "policy.json").read_text())
    (memory / "policy.json").write_text(json.dumps({**raw, "writable": ["notes/**/*.md"]}))
    (memory / "checkout.txt").write_text("/*.md\n/notes/\n")
    again, printed = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert printed["RETINUE_MEMORY"] == "/kb", "the hub exists: on for good"
    assert json.loads((hub / "hooks" / "kb-policy.json").read_text())["writable"] == ["notes/**/*.md"], \
        "the owner's rules, copied to the hub on every run"
    assert RouterConfig.load(target / "router.yaml").memory.checkout == ["/*.md", "/notes/"]


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
    assert proxy["profiles"] == ["mail"] and set(proxy["networks"]) == {"mail-out", "outside"} and "ports" not in proxy
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
