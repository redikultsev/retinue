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


def test_travel_ops_address_comes_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "agent.yaml"
    path.write_text(AGENT_YAML)
    monkeypatch.delenv("RETINUE_TRAVEL_URL", raising=False)
    assert AgentConfig.load(path).travel_url == "", "off unless the stack says so"
    monkeypatch.setenv("RETINUE_TRAVEL_URL", "http://travel-ops:8765/mcp")
    assert AgentConfig.load(path).travel_url == "http://travel-ops:8765/mcp"


def test_the_router_reaches_travel_ops_for_price_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").travel_url == "", "off unless the config says so"
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ntravel_url: http://travel-ops:8765/mcp\n")
    assert cfg.travel_url == "http://travel-ops:8765/mcp"


def test_the_travel_sites_whose_links_are_clickable(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").link_hosts == [], "none: every address is code"
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nlink_hosts: [kiwi.com, www.trivago.*]\n")
    assert cfg.link_hosts == ["kiwi.com", "www.trivago.*"]
    from retinue.router import build_channels

    (channel,) = build_channels(cfg, None, None)
    assert channel.link_hosts == ("kiwi.com", "www.trivago.*")


def test_the_knowledge_base_comes_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "agent.yaml"
    path.write_text("id: a\nname: A\ndescription: d\ntrust_class: private\nskills: []\n")
    assert AgentConfig.load(path).memory == "", "off unless the stack says so"
    monkeypatch.setenv("RETINUE_MEMORY", "/kb")
    with pytest.raises(SystemExit, match="RETINUE_MEMORY_POLICY"):
        AgentConfig.load(path)
    monkeypatch.setenv("RETINUE_MEMORY_POLICY", "/memory-policy.json")
    cfg = AgentConfig.load(path)
    assert (cfg.memory, cfg.memory_policy) == ("/kb", "/memory-policy.json")


def test_the_router_holds_the_knowledge_base_when_the_config_says_so(tmp_path, monkeypatch):
    import sqlite3

    from retinue.router import build_memory

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").memory is None, "off unless the config says so"
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nmemory:\n  hub: /srv/hub\n  checkout: ['/*.md', '/notes/']\n")
    assert (cfg.memory.tree, cfg.memory.git_dir, cfg.memory.hub, cfg.memory.digest_at) == ("/kb", "/kb-git", "/srv/hub",
                                                                                          "21:00")
    assert cfg.memory.checkout == ["/*.md", "/notes/"] and cfg.memory.policy == "/config/memory-policy.json"
    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    cfg.memory.policy, cfg.memory.tree, cfg.memory.git_dir = str(policy), str(tmp_path / "t"), str(tmp_path / "g")
    memory, error = build_memory(cfg, sqlite3.connect(":memory:"))
    assert memory is None and error.startswith("База знаний не подключена: хаб /srv/hub не ответил"), \
        "a hub that is not there: the router runs on and says why"


def test_the_router_reaches_the_mail_collector_with_its_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("RETINUE_COLLECTOR_TOKEN", raising=False)
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").collector_url == "", "off unless the config says so"
    with pytest.raises(SystemExit):
        load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ncollector_url: http://collector:9200\n")
    monkeypatch.setenv("RETINUE_COLLECTOR_TOKEN", "c")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ncollector_url: http://collector:9200\n")
    assert (cfg.collector_url, cfg.collector_token) == ("http://collector:9200", "c")


def test_the_life_hub_is_on_when_the_config_names_its_address(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    plain = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n")
    assert plain.lifehub_url == "" and plain.lifehub_data == "/lifehub", "off unless the config says so"
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nlifehub_url: https://hub.in.example.com\n")
    assert cfg.lifehub_url == "https://hub.in.example.com"
    for wrong in ("http://hub.in.example.com", "https://hub.in.example.com/x", "hub.in.example.com"):
        with pytest.raises(SystemExit, match="lifehub_url"):
            load(tmp_path, AGENT + f"telegram:\n  owner_id: 42\nlifehub_url: {wrong}\n")


def test_a_link_to_a_trip_page_is_clickable_in_telegram_and_nothing_else_of_the_hub(tmp_path, monkeypatch):
    from retinue.render import render_telegram
    from retinue.router import build_channels

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nlink_hosts: [kiwi.com]\n"
                                 "lifehub_url: https://hub.in.example.com\n")
    (channel,) = build_channels(cfg, None, None)
    assert channel.link_hosts == ("kiwi.com", "hub.in.example.com/trips/")
    (html,) = render_telegram("Нашла.\n\n[подробнее](https://hub.in.example.com/trips/0123456789abcdef/)",
                              channel.link_hosts)
    assert '<a href="https://hub.in.example.com/trips/0123456789abcdef/">подробнее</a>' in html
    (other,) = render_telegram("[статус](https://hub.in.example.com/status/)", channel.link_hosts)
    assert "<a " not in other, "only a trip page: the model chooses the path under /trips/, nothing else"
