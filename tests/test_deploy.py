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
    """Run deploy/setup.sh into a temporary folder; return (folder, the environment block it printed)."""
    target = tmp_path / "srv"
    run = subprocess.run(["bash", str(ROOT / "deploy" / "setup.sh")], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "RETINUE_ROOT": str(target), "RETINUE_HOST_SETUP": "0", **env})
    assert run.returncode == 0, run.stderr
    printed = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line and " " not in line.split("=")[0])
    return target, printed


def test_one_assistant_and_nothing_else():
    agents = sorted(p.name for p in (ROOT / "agents").iterdir())
    assert agents == ["assistant"], "Главная, Учёба and Путешествия are gone"
    assert sorted(p.name for p in (ROOT / "agents" / "assistant").iterdir()) == ["CLAUDE.md", "agent.yaml"]
    engine = AgentConfig.load(ROOT / "agents" / "assistant" / "agent.yaml").engine
    assert engine.tools == [] and engine.allowed_tools == [], "no built-in tool is offered or allowed"
    assert {"Bash", "WebSearch", "WebFetch"} <= set(engine.disallowed_tools)
    assert engine.bus_tools == ["search_archive"], "the archive, and no ask_agent"
    assert engine.instructions == "/agent/CLAUDE.md" and engine.run_root == "/run/retinue"
    assert sorted(SERVICES) == ["assistant", "egress", "router", "tuwunel"]
    assert sorted(COMPOSE["volumes"]) == ["router-data", "tuwunel-db"], "no agent has a volume: no sessions on disk"


def test_assistant_container_cannot_write_its_settings_and_keeps_nothing():
    assistant = SERVICES["assistant"]
    assert assistant["volumes"] == ["/srv/retinue/agents/assistant:/agent:ro"], "one mount, read-only"
    assert [t.split(":")[0] for t in assistant["tmpfs"]] == ["/run/retinue"], "the run directory is memory"
    assert "CLAUDE_CONFIG_DIR" not in assistant["environment"] and "ANTHROPIC_API_KEY" not in assistant["environment"]
    router_mounts = " ".join(SERVICES["router"]["volumes"])
    assert "router-data:/data" in router_mounts and "/data" not in str(assistant), "the archive file is the router's alone"


def test_agents_network_is_closed_and_the_proxy_is_the_only_way_out():
    assert COMPOSE["networks"]["agents"] == {"internal": True}
    assert SERVICES["assistant"]["networks"] == ["agents"], "the assistant is on the internal network and no other"
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
    assert [(a.id, a.archive, a.can_call) for a in cfg.agents] == [("assistant", True, [])]
    assert (target / "agents" / "assistant" / "CLAUDE.md").read_text() == (ROOT / "agents" / "assistant" / "CLAUDE.md").read_text()
    assert "${RETINUE_BUS_TOKEN_ASSISTANT:-}" in (ROOT / "deploy" / "compose.yml").read_text()
    for agent in cfg.agents:
        assert printed[f"RETINUE_BUS_TOKEN_{agent.id.upper()}"] == bus_token(printed["RETINUE_BUS_SECRET"], agent.id)
        assert AgentConfig.load(target / "agents" / agent.id / "agent.yaml").public_url == agent.url
    again, printed_again = setup(tmp_path, TELEGRAM_OWNER_ID="42")
    assert printed_again["RETINUE_BUS_SECRET"] == printed["RETINUE_BUS_SECRET"], "a second run keeps the secrets"
    assert oct((target / "secrets.env").stat().st_mode & 0o777) == "0o600"


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
