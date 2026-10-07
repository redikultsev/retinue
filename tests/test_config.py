"""Router config: every channel is optional, secrets come from the environment, one agent is the default."""

import subprocess
import sys
from pathlib import Path

import pytest

from retinue.config import AgentConfig, RouterConfig

AGENT = "agents:\n  - {id: assistant, name: Ассистентка, url: 'http://assistant:9000', trust_class: private, archive: true}\n"
MATRIX = "matrix:\n  homeserver: http://tuwunel:6167\n  server_name: example.com\n  owner: '@alice:example.com'\n"


def load(tmp_path, text):
    path = tmp_path / "router.yaml"
    path.write_text(text)
    return RouterConfig.load(path)


def test_telegram_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("RETINUE_AS_TOKEN", raising=False)
    monkeypatch.delenv("RETINUE_HS_TOKEN", raising=False)
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n")
    assert cfg.matrix is None and cfg.telegram.owner_id == 42 and cfg.telegram.bot_token == "t"
    assert cfg.default_agent == "assistant", "the only agent gets the messages without an address"
    assert cfg.agents[0].archive and cfg.archive_db == "/data/archive.sqlite"


def test_matrix_section_brings_its_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("RETINUE_AS_TOKEN", "as")
    monkeypatch.setenv("RETINUE_HS_TOKEN", "hs")
    cfg = load(tmp_path, AGENT + MATRIX)
    assert cfg.telegram is None and (cfg.matrix.as_token, cfg.matrix.hs_token) == ("as", "hs")
    assert cfg.matrix.agent_mxid("assistant") == "@agent_assistant:example.com"
    monkeypatch.delenv("RETINUE_AS_TOKEN")
    with pytest.raises(SystemExit, match="RETINUE_AS_TOKEN"):
        load(tmp_path, AGENT + MATRIX)


def test_refuses_a_config_nobody_can_talk_to(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    with pytest.raises(SystemExit, match="no channel"):
        load(tmp_path, AGENT)
    two = AGENT + "  - {id: second, name: Второй, url: 'http://second:9000'}\ntelegram:\n  owner_id: 42\n"
    with pytest.raises(SystemExit, match="default_agent"):
        load(tmp_path, two)
    assert load(tmp_path, two + "default_agent: second\n").default_agent == "second"
    with pytest.raises(SystemExit, match="not in `agents`"):
        load(tmp_path, two + "default_agent: nobody\n")


def test_owner_time_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    telegram = "telegram:\n  owner_id: 42\n"
    cfg = load(tmp_path, AGENT + telegram)
    assert cfg.owner_tz == "Europe/Moscow", "the owner's zone by default"
    assert not cfg.agents[0].reminders, "reminders are a grant, off unless given"
    assert load(tmp_path, AGENT + telegram + "owner_tz: Europe/Belgrade\n").owner_tz == "Europe/Belgrade"
    with pytest.raises(SystemExit, match="not a time zone"):
        load(tmp_path, AGENT + telegram + "owner_tz: Moscow\n")


def test_the_speech_key_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").stt_key == "", "optional: voice is refused aloud"
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").stt_key == "sk-test"


def test_router_does_not_load_matrix_without_the_section():
    code = "import sys, retinue.router; sys.exit('mautrix' in sys.modules or 'retinue.channels.matrix' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_matrix_adapter_is_built_from_its_section(tmp_path, monkeypatch):
    import asyncio

    from retinue.protocol import Store
    from retinue.router import build_channels

    monkeypatch.setenv("RETINUE_AS_TOKEN", "as")
    monkeypatch.setenv("RETINUE_HS_TOKEN", "hs")
    cfg = load(tmp_path, AGENT + MATRIX + f"state_db: {tmp_path / 'r.sqlite'}\n")
    loop = asyncio.new_event_loop()
    try:
        channels = build_channels(cfg, Store(cfg.state_db), loop)
    finally:
        loop.close()
    assert [c.name for c in channels] == ["matrix"] and channels[0].agents == cfg.agents


AGENT_YAML = """
id: assistant
name: Ассистентка
description: d
trust_class: private
skills: [{id: talk, name: Разговор, description: d}]
"""


def test_agent_engine_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("RETINUE_BUS_URL", "http://router:9100")
    monkeypatch.setenv("RETINUE_BUS_TOKEN", "tok")
    path = tmp_path / "agent.yaml"
    path.write_text(AGENT_YAML)
    engine = AgentConfig.load(path).engine
    assert engine.instructions == "/agent/CLAUDE.md" and engine.tools is None and engine.config_dir is None
    assert engine.bus_tools == ["ask_agent", "list_agents"], "an agent without the field keeps the old bus tools"
    path.write_text(AGENT_YAML + "engine: {tools: [], bus_tools: [search_archive], config_dir: /data/claude,"
                                 " instructions: /agent/CLAUDE.md, disallowed_tools: [Bash, WebSearch, WebFetch]}\n")
    cfg = AgentConfig.load(path)
    assert cfg.engine.tools == [] and cfg.engine.bus_tools == ["search_archive"]
    assert cfg.engine.config_dir == "/data/claude" and cfg.state_db == "/data/agent.sqlite" and (cfg.bus_url, cfg.bus_token) == ("http://router:9100", "tok")
    path.write_text(AGENT_YAML + "engine: {bus_tools: [send_email]}\n")
    with pytest.raises(SystemExit, match="send_email"):
        AgentConfig.load(path)


def test_every_agent_in_the_repo_loads():
    configs = sorted((Path(__file__).resolve().parents[1] / "agents").glob("*/agent.yaml"))
    assert configs, "the repo ships at least one agent"
    for path in configs:
        assert AgentConfig.load(path).id == path.parent.name
