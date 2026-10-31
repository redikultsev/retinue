"""Router config: every channel is optional, secrets come from the environment, one agent is the default."""

import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from retinue.config import AgentConfig, PlatformConfig, RouterConfig, keyterm_problem

AGENT = "agents:\n  - {id: assistant, name: Assistant, url: 'http://assistant:9000', trust_class: private, archive: true}\n"
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
    assert cfg.owner_tz == "UTC", "UTC until the owner names his zone"
    assert not cfg.agents[0].reminders, "reminders are a grant, off unless given"
    assert load(tmp_path, AGENT + telegram + "owner_tz: Europe/Berlin\n").owner_tz == "Europe/Berlin"
    with pytest.raises(SystemExit, match="not a time zone"):
        load(tmp_path, AGENT + telegram + "owner_tz: Berlin\n")


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
name: Assistant
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
    assert memory is None and error.startswith("The Base is not connected: the hub /srv/hub did not answer"), \
        "a hub that is not there: the router runs on and says why"


def test_the_owners_own_words_for_memory_load_from_router_yaml(tmp_path, monkeypatch):
    """`location`, `private` and `tracker` come from the server's owner.yaml under `memory:` (docs/memory.md):
    router.yaml takes them, and without them each keeps its neutral default."""
    from retinue.pages import Tracker
    from retinue.mail import Private
    from retinue.proactive import LOCATION, location_record

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    bare = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nmemory:\n  hub: /srv/hub\n").memory
    assert (bare.location, bare.private, bare.tracker) == ("", {}, {})
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nmemory:\n  hub: /srv/hub\n"
                                 "  location: home/where.md\n"
                                 "  private: {about: переговоры о покупке квартиры, sphere: home}\n"
                                 "  tracker: {path: home/flats.md, title: Квартиры}\n").memory

    class Memory:  # what the modules read: the memory's config
        pass

    memory = Memory()
    memory.cfg = cfg
    assert location_record(memory) == "home/where.md" and location_record(Memory()) == LOCATION
    assert Private.of(memory) == Private(about="переговоры о покупке квартиры", sphere="home")
    tracker = Tracker.of(memory)
    assert (tracker.path, tracker.title) == ("home/flats.md", "Квартиры")


def test_the_router_reaches_the_mail_collector_with_its_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("RETINUE_COLLECTOR_TOKEN", raising=False)
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").collector_url == "", "off unless the config says so"
    with pytest.raises(SystemExit):
        load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ncollector_url: http://collector:9200\n")
    monkeypatch.setenv("RETINUE_COLLECTOR_TOKEN", "c")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ncollector_url: http://collector:9200\n")
    assert (cfg.collector_url, cfg.collector_token) == ("http://collector:9200", "c")


def test_the_pages_are_on_when_the_config_names_their_address(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    plain = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n")
    assert (plain.pages_url, plain.pages_data, plain.pages_build) == ("", "/pages", "/pages-site/build.json"), \
        "off unless the config says so"
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\npages_url: https://pages.in.example.com\n")
    assert cfg.pages_url == "https://pages.in.example.com"
    for wrong in ("http://pages.in.example.com", "https://pages.in.example.com/x", "pages.in.example.com",
                  "https://Pages.in.example.com", "https://pages.in.example.com:8443"):
        with pytest.raises(SystemExit, match="pages_url"):
            load(tmp_path, AGENT + f"telegram:\n  owner_id: 42\npages_url: {wrong}\n")
    old = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nlifehub_url: https://hub.in.example.com\n"
                              "lifehub_data: /lifehub\n")
    assert old.pages_url == "", "the retired hub's keys start the router with no pages, not a crash"


def test_a_link_to_the_owners_pages_is_clickable_in_telegram(tmp_path, monkeypatch):
    from retinue.render import render_telegram
    from retinue.router import build_channels

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nlink_hosts: [kiwi.com]\n"
                                 "pages_url: https://pages.in.example.com\n")
    (channel,) = build_channels(cfg, None, None)
    assert channel.link_hosts == ("kiwi.com", "pages.in.example.com/")
    (html,) = render_telegram("Нашла.\n\n[подробнее](https://pages.in.example.com/trip-0123456789abcdef)",
                              channel.link_hosts)
    assert '<a href="https://pages.in.example.com/trip-0123456789abcdef">подробнее</a>' in html
    (page,) = render_telegram("[курс](https://pages.in.example.com/regex)", channel.link_hosts)
    assert '<a href="https://pages.in.example.com/regex">курс</a>' in page, "the answer to a page he sent"
    (other,) = render_telegram("[статус](https://pages.in.example.com.evil.example/x)", channel.link_hosts)
    assert "<a " not in other, "only the pages' own host"


def test_the_router_reaches_the_gateway_and_the_sender_with_their_tokens(tmp_path, monkeypatch):
    from retinue.config import BUS_TOOLS, RouterAgent

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    for name in ("RETINUE_GATEWAY_TOKEN", "RETINUE_SENDER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    plain = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n")
    assert plain.gateway_url == plain.sender_url == "", "off unless the config says so"
    for line, name in (("gateway_url: http://tg-business:9300", "RETINUE_GATEWAY_TOKEN"),
                       ("sender_url: http://mail-send:9400", "RETINUE_SENDER_TOKEN")):
        with pytest.raises(SystemExit, match=name):
            load(tmp_path, AGENT + f"telegram:\n  owner_id: 42\n{line}\n")
        monkeypatch.setenv(name, "x")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\ngateway_url: http://tg-business:9300\n"
                                 "sender_url: http://mail-send:9400\n")
    assert (cfg.gateway_token, cfg.sender_token) == ("x", "x")
    assert RouterAgent(id="a", name="A", url="u").drafts is False, "no agent drafts unless granted"
    assert "draft_reply" in BUS_TOOLS


def test_the_router_reaches_the_platform_with_its_token_and_only_through_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RETINUE_AS_TOKEN", "as")
    monkeypatch.setenv("RETINUE_HS_TOKEN", "hs")
    monkeypatch.delenv("RETINUE_PLATFORM_TOKEN", raising=False)
    assert load(tmp_path, AGENT + "telegram:\n  owner_id: 42\n").platform_url == "", "off unless the config says so"
    with pytest.raises(SystemExit, match="RETINUE_PLATFORM_TOKEN"):
        load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nplatform_url: http://platform:9500\n")
    monkeypatch.setenv("RETINUE_PLATFORM_TOKEN", "p")
    with pytest.raises(SystemExit, match="telegram"):
        load(tmp_path, AGENT + MATRIX + "platform_url: http://platform:9500\n")
    cfg = load(tmp_path, AGENT + "telegram:\n  owner_id: 42\nplatform_url: http://platform:9500\n")
    assert (cfg.platform_url, cfg.platform_token) == ("http://platform:9500", "p")


PLATFORM = """\
lanes:
  code: {url: 'http://lane-code:9600', hook_url: 'http://platform:9500/hook', work_mount: /lanes/code,
         deny: [WebSearch, WebFetch]}
tracks:
  - {name: retinue, lane: code, kind: code, cwd: retinue, allow: ['Bash(make test:*)'], deny: []}
"""


def load_platform(tmp_path, monkeypatch, text=PLATFORM):
    for name, value in (("RETINUE_PLATFORM_TOKEN", "router"), ("RETINUE_HOOK_SECRET", "hooks"),
                        ("RETINUE_LANE_TOKEN_CODE", "lane"), ("CLAUDE_CODE_OAUTH_TOKEN", "login")):
        monkeypatch.setenv(name, value)
    path = tmp_path / "platform.yaml"
    path.write_text(text)
    return PlatformConfig.load(path)


def test_platform_lanes_tracks_and_their_tokens(tmp_path, monkeypatch):
    cfg = load_platform(tmp_path, monkeypatch)
    lane = cfg.lanes["code"]
    assert (lane.max_sessions, lane.deny, lane.token) == (2, ["WebSearch", "WebFetch"], "lane")
    assert (cfg.max_sessions, cfg.state_db, cfg.listen_port) == (5, "/data/platform.sqlite", 9500)
    assert (cfg.router_token, cfg.hook_secret, cfg.oauth_token) == ("router", "hooks", "login")
    assert cfg.owner_tz == "UTC" and load_platform(tmp_path, monkeypatch, "owner_tz: Europe/Berlin\n"
                                                             + PLATFORM).owner_tz == "Europe/Berlin"
    assert cfg.tracks[0].allow == ["Bash(make test:*)"] and cfg.tracks[0].kind == "code"
    assert lane.web is False, "the web tools stay denied unless a lane says otherwise"
    assert lane.card == cfg.tracks[0].card == [], "no card rules unless the config names them"
    carded = load_platform(tmp_path, monkeypatch,
                           PLATFORM.replace("deny: []}", "deny: [], card: ['Bash(git commit:*)']}"))
    assert carded.tracks[0].card == ["Bash(git commit:*)"]
    monkeypatch.delenv("RETINUE_LANE_TOKEN_CODE")
    with pytest.raises(SystemExit, match="RETINUE_LANE_TOKEN_CODE"):
        PlatformConfig.load(tmp_path / "platform.yaml")
    monkeypatch.setenv("RETINUE_LANE_TOKEN_CODE", "lane")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(SystemExit, match="CLAUDE_CODE_OAUTH_TOKEN"):
        PlatformConfig.load(tmp_path / "platform.yaml")


@pytest.mark.parametrize("change, error", [
    (("lane: code", "lane: private"), "unknown lane 'private'"),
    (("name: retinue", "name: Retinue"), "track name"),
    (("kind: code", "kind: web"), "kind 'web'"),
    (("cwd: retinue", "cwd: ../etc"), "not a path inside /work"),
    (("cwd: retinue", "cwd: /etc"), "not a path inside /work"),
    (("'Bash(make test:*)'", "'Bash(make (x))'"), "not a permission rule"),
    (("deny: [WebSearch", "deny: ['Web Search'"), "lane code: not a permission rule"),
    (("deny: [WebSearch", "card: ['Bash(a (b))'], deny: [WebSearch"), "lane code: not a permission rule"),
    (("deny: []}", "deny: [], card: ['git commit']}"), "track retinue: not a permission rule"),
    (("  code: {", "  lane-code: {"), "lane name 'lane-code'"),
    (("work_mount: /lanes/code,", "work_mount: /lanes/code, max_sessions: 0,"), "lane code: max_sessions"),
    (("work_mount: /lanes/code,", "work_mount: /lanes/code, web: 'yes',"), "lane code: web or retired is not true or false"),
    (("lanes:", "max_sessions: 0\nlanes:"), "platform config: max_sessions"),
    (("lanes:", "owner_tz: Mars/Olympus\nlanes:"), "platform config: owner_tz 'Mars/Olympus'"),
    (("  - {name: retinue", "  - {name: retinue, lane: code, kind: code, cwd: x}\n  - {name: retinue"),
     "two tracks with one name"),
])
def test_platform_refuses_a_track_or_rule_it_cannot_run(tmp_path, monkeypatch, change, error):
    with pytest.raises(SystemExit, match=re.escape(error)):
        load_platform(tmp_path, monkeypatch, PLATFORM.replace(*change))


SPHERES = "spheres:\n  - {name: travel, title: Путешествия, icon: '✈️', track: retinue}\n"


def test_platform_spheres_are_optional_and_map_to_a_track(tmp_path, monkeypatch):
    assert load_platform(tmp_path, monkeypatch).spheres == []
    [travel] = load_platform(tmp_path, monkeypatch, PLATFORM + SPHERES).spheres
    assert (travel.name, travel.title, travel.icon, travel.track) == ("travel", "Путешествия", "✈️", "retinue")
    assert load_platform(tmp_path, monkeypatch, PLATFORM + SPHERES.replace(", icon: '✈️'", "")).spheres[0].icon == ""


def test_the_example_platform_config_loads_with_its_spheres_in_the_root_of_the_base(tmp_path, monkeypatch):
    example = (Path(__file__).parent.parent / "deploy" / "platform" / "platform.example.yaml").read_text()
    monkeypatch.setenv("RETINUE_LANE_TOKEN_PRIVATE", "lane")
    cfg = load_platform(tmp_path, monkeypatch, example)
    assert {s.track for s in cfg.spheres} == {"base"} and "travel" in {s.name for s in cfg.spheres}
    assert list(cfg.lanes) == ["private"] and cfg.lanes["private"].web, "one room"


@pytest.mark.parametrize("change, error", [
    (("track: retinue}", "track: notes}"), "sphere travel: unknown track 'notes'"),
    (("name: travel", "name: Travel"), "sphere name 'Travel'"),
    (("name: travel", "name: путешествия"), "sphere name"),
    (("name: travel", "name: retinue"), "sphere retinue: the name is taken"),
    (("name: travel", "name: new"), "sphere new: the name is taken"),
    (("name: travel", "name: wait"), "sphere wait: the name is taken"),
    (("title: Путешествия", "title: ''"), "sphere travel: title"),
    (("icon: '✈️'", "icon: 'a very long mark'"), "sphere travel: icon"),
    (("  - {name: travel", "  - {name: travel, title: x, track: retinue}\n  - {name: travel"),
     "sphere travel: the name is taken"),
])
def test_platform_refuses_a_sphere_it_cannot_serve(tmp_path, monkeypatch, change, error):
    with pytest.raises(SystemExit, match=re.escape(error)):
        load_platform(tmp_path, monkeypatch, PLATFORM + SPHERES.replace(*change))


def test_the_face_is_off_without_its_origin_and_takes_its_keys_from_the_environment(tmp_path, monkeypatch):
    for name in ("RETINUE_FACE_ORIGIN", "RETINUE_WEBAPP_KEY", "TELEGRAM_OWNER_ID", "RETINUE_FACE_HOST"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_platform(tmp_path, monkeypatch)
    assert (cfg.face_port, cfg.face_origin, cfg.webapp_key, cfg.owner_tg) == (9600, "", "", 0)
    assert cfg.face_host == "127.0.0.1", "nothing exposed unless the deployment names the face's own address"
    monkeypatch.setenv("RETINUE_FACE_HOST", "172.30.9.2")
    assert load_platform(tmp_path, monkeypatch).face_host == "172.30.9.2"
    monkeypatch.setenv("RETINUE_FACE_HOST", "platform")
    with pytest.raises(SystemExit, match="RETINUE_FACE_HOST"):
        load_platform(tmp_path, monkeypatch)
    monkeypatch.delenv("RETINUE_FACE_HOST")
    monkeypatch.setenv("RETINUE_FACE_ORIGIN", "https://app.in.example.com/")
    monkeypatch.setenv("RETINUE_WEBAPP_KEY", "AB" * 32)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    cfg = load_platform(tmp_path, monkeypatch)
    assert (cfg.face_origin, cfg.webapp_key, cfg.owner_tg) == ("https://app.in.example.com", "ab" * 32, 42)
    for name, value, error in (("RETINUE_FACE_ORIGIN", "http://app.in.example.com", "RETINUE_FACE_ORIGIN"),
                               ("RETINUE_FACE_ORIGIN", "https://app.example.com/x", "RETINUE_FACE_ORIGIN"),
                               ("RETINUE_WEBAPP_KEY", "bot-token-itself", "RETINUE_WEBAPP_KEY"),
                               ("TELEGRAM_OWNER_ID", "@me", "TELEGRAM_OWNER_ID")):
        with monkeypatch.context() as m:
            m.setenv(name, value)
            with pytest.raises(SystemExit, match=error):
                load_platform(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="face_port"):
        load_platform(tmp_path, monkeypatch, "face_port: 9500\n" + PLATFORM)


def test_the_router_knows_the_faces_address_only_with_the_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RETINUE_PLATFORM_TOKEN", "p")
    base = AGENT + "telegram:\n  owner_id: 42\n"
    cfg = load(tmp_path, base + "platform_url: http://platform:9500\nface_url: https://app.in.example.com\n")
    assert cfg.face_url == "https://app.in.example.com"
    with pytest.raises(SystemExit, match="face_url"):
        load(tmp_path, base + "face_url: https://app.in.example.com\n")
    with pytest.raises(SystemExit, match="face_url"):
        load(tmp_path, base + "platform_url: http://platform:9500\nface_url: https://app.in.example.com/\n")


BASE = """\
lanes:
  private: {url: 'http://lane-private:9600', hook_url: 'http://platform:9500/hook', work_mount: /lanes/private}
  web: {url: 'http://lane-web:9600', hook_url: 'http://platform:9500/hook', work_mount: /lanes/web, web: true}
tracks:
  - {name: base, lane: private, kind: private, cwd: base}
spheres:
  - {name: travel, title: Путешествия, track: base, cwd: base/travel}
base: {author: 'Иван <owner@example.com>'}
"""


def test_the_base_is_off_unless_configured_and_its_spheres_work_in_a_space(tmp_path, monkeypatch):
    for lane in ("PRIVATE", "WEB"):
        monkeypatch.setenv(f"RETINUE_LANE_TOKEN_{lane}", "lane")
    assert load_platform(tmp_path, monkeypatch).base is None
    cfg = load_platform(tmp_path, monkeypatch, BASE)
    assert (cfg.base.hub, cfg.base.tree, cfg.base.lane, cfg.base.mount, cfg.base.git_dir) == (
        "/hub", "base", "private", "/lanes/private/base", "/platform/base.git")
    assert "scripts/**" in cfg.base.protect and cfg.spheres[0].cwd == "base/travel"
    room = load_platform(tmp_path, monkeypatch, BASE.replace("base: {author", "base: {lane: web, author"))
    assert room.base.lane == "web" and room.base.mount == "/lanes/web/base", \
        "the room: the Base beside the open web (docs/platform.md, «The room»)"


@pytest.mark.parametrize("change, error", [
    (("base: {author", "base: {lane: code, author"), "base: unknown lane 'code'"),
    (("base: {author", "base: {tree: ../x, author"), "base: tree '../x' is not a folder inside"),
    (("base: {author", "base: {tree: /work/base, author"), "base: tree '/work/base' is not a folder inside"),
    (("base: {author", "base: {tree: .base, author"), "base: tree '.base' is not a folder inside"),
    (("'Иван <owner@example.com>'", "'Иван'"), "base: author is not «Name <email>»"),
    (("base: {author", "base: {every_s: 1, author"), "base: every_s is not a number of at least 10"),
    (("base: {author", "base: {git_dir: base.git, author"), "base: git_dir is not an absolute path"),
    (("cwd: base/travel", "cwd: travel"), "sphere travel: cwd 'travel' is not a path inside its track's base"),
    (("cwd: base/travel", "cwd: base/../x"), "sphere travel: cwd 'base/../x' is not a path inside"),
    (("base: {author: 'Иван <owner@example.com>'}", "base: yes"), "base is not a section"),
    (("base: {author", "base: {mount_point: /x, author"), "base: unknown mount_point"),
    (("base: {author: 'Иван <owner@example.com>'}", "base: {tree: base}"), "base: author is not"),
])
def test_platform_refuses_a_base_it_cannot_keep(tmp_path, monkeypatch, change, error):
    for lane in ("PRIVATE", "WEB"):
        monkeypatch.setenv(f"RETINUE_LANE_TOKEN_{lane}", "lane")
    with pytest.raises(SystemExit, match=re.escape(error)):
        load_platform(tmp_path, monkeypatch, BASE.replace(*change))


def test_the_example_turns_the_base_on_by_removing_its_marks(tmp_path, monkeypatch):
    example = (Path(__file__).parent.parent / "deploy" / "platform" / "platform.example.yaml").read_text()
    head, _, tail = example.partition("\n#base:")
    on = head + "\nbase:" + "".join("\n" + line.removeprefix("#") for line in tail.split("\n")[1:])
    monkeypatch.setenv("RETINUE_LANE_TOKEN_PRIVATE", "lane")
    cfg = load_platform(tmp_path, monkeypatch, on)
    assert (cfg.base.tree, cfg.base.lane, cfg.base.mount) == ("base", "private", "/lanes/private/base")
    assert [(s.track, s.cwd) for s in cfg.spheres if s.name == "travel"] == [("base", "")], "the root of the Base"
    assert load_platform(tmp_path, monkeypatch, example).base is None, "off as written"


@pytest.mark.parametrize("rule", ["WebFetch", "WebSearch", "WebFetch(domain:*)"])
def test_a_web_lane_refuses_an_allow_rule_that_takes_the_web_past_the_classifier(tmp_path, monkeypatch, rule):
    room = PLATFORM.replace("deny: [WebSearch, WebFetch]}", "web: true}").replace(
        "allow: ['Bash(make test:*)']", f"allow: ['Bash(make test:*)', '{rule}']")
    with pytest.raises(SystemExit, match=re.escape(f"track retinue: allow {rule} on a web lane")):
        load_platform(tmp_path, monkeypatch, room)
    narrow = room.replace(f"'{rule}'", "'WebFetch(domain:docs.python.org)'")
    assert load_platform(tmp_path, monkeypatch, narrow).tracks[0].allow[-1] == "WebFetch(domain:docs.python.org)"
    sealed = PLATFORM.replace("allow: ['Bash(make test:*)']", f"allow: ['{rule}']")
    assert load_platform(tmp_path, monkeypatch, sealed).tracks[0].allow == [rule], "denied there by code anyway"


def test_a_retired_lane_needs_no_token_and_never_holds_the_base(tmp_path, monkeypatch):
    retired = PLATFORM.replace("deny: [WebSearch, WebFetch]}", "retired: true}")
    monkeypatch.delenv("RETINUE_LANE_TOKEN_CODE", raising=False)
    for name, value in (("RETINUE_PLATFORM_TOKEN", "router"), ("RETINUE_HOOK_SECRET", "hooks"),
                        ("CLAUDE_CODE_OAUTH_TOKEN", "login")):
        monkeypatch.setenv(name, value)
    path = tmp_path / "platform.yaml"
    path.write_text(retired)
    cfg = PlatformConfig.load(path)
    assert cfg.lanes["code"].retired is True and cfg.lanes["code"].token == ""
    path.write_text(retired + "base: {lane: code, author: 'Иван <owner@example.com>'}\n")
    with pytest.raises(SystemExit, match="base: lane code is retired"):
        PlatformConfig.load(path)


def test_the_example_turns_the_code_on_by_removing_its_marks(tmp_path, monkeypatch):
    example = (Path(__file__).parent.parent / "deploy" / "platform" / "platform.example.yaml").read_text()
    head, _, tail = example.partition("\n#code:")
    rows = tail.split("\n")[1:]
    taken = [row.removeprefix("#") for row in itertools.takewhile(lambda r: r.startswith("#  "), rows)]
    on = head + "\ncode:\n" + "\n".join(taken) + "\n" + "\n".join(rows[len(taken):])
    on = on.replace("#  # author:", "  author:").replace("  # author:", "  author:")
    for lane in ("PRIVATE", "WEB", "CODE"):
        monkeypatch.setenv(f"RETINUE_LANE_TOKEN_{lane}", "lane")
    monkeypatch.setenv("RETINUE_GIT_TOKEN_YOU", "github_pat_x")
    monkeypatch.delenv("RETINUE_GIT_TOKEN_GROUP_GITLAB", raising=False)
    cfg = load_platform(tmp_path, monkeypatch, on)
    assert [(r.name, r.host, r.off) for r in cfg.code.repos] == [
        ("retinue", "github.com", ""), ("example-app", "gitlab.com", "no token RETINUE_GIT_TOKEN_GROUP_GITLAB")]
    assert (cfg.code.lane, cfg.code.track, cfg.code.proxy) == ("private", "code", "http://platform-egress:3128")
    assert load_platform(tmp_path, monkeypatch, example).code is None, "off as written"


SPEECH_CODE = "code: {lane: code, track: '', author: 'Иван <owner@example.com>', proxy: 'http://platform-egress:3128'}\n"


def test_speech_takes_the_key_out_of_the_environment_and_codes_way_out(tmp_path, monkeypatch):
    """The app's voice: the key read once and dropped from the environment (no child inherits it); the way out is
    speech.proxy, else code's; the example's terms pass Scribe's rules."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", " sk-el ")
    cfg = load_platform(tmp_path, monkeypatch, PLATFORM + SPEECH_CODE)
    assert cfg.stt_key == "sk-el" and "ELEVENLABS_API_KEY" not in os.environ
    assert (cfg.speech.keyterms, cfg.speech.proxy) == ([], "http://platform-egress:3128"), "code's proxy, no terms"
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    bare = load_platform(tmp_path, monkeypatch)
    assert (bare.stt_key, bare.speech.proxy) == ("", ""), "no key, no way out: refused aloud in the app"
    own = load_platform(tmp_path, monkeypatch, PLATFORM + "speech: {proxy: 'http://speech-out:3128', keyterms: [' RAG']}\n")
    assert (own.speech.proxy, own.speech.keyterms) == ("http://speech-out:3128", ["RAG"])
    example = (Path(__file__).parent.parent / "deploy" / "platform" / "platform.example.yaml").read_text()
    seeded = yaml.safe_load(example)["speech"]["keyterms"]
    assert "Claude Code" in seeded and "Markdown" in seeded and len(seeded) <= 100, "a short list: past 100 costs 20 s a call"
    assert not any(keyterm_problem(term) for term in seeded)


@pytest.mark.parametrize("section, error", [
    ("speech: [RAG]", "speech is not a section"),
    ("speech: {terms: [RAG]}", "speech: unknown terms"),
    ("speech: {keyterms: RAG}", "keyterms is not a list"),
    ("speech: {keyterms: ['']}", "not a non-empty string"),
    ("speech: {keyterms: [42]}", "not a non-empty string"),
    ("speech: {keyterms: ['" + "x" * 50 + "']}", "50 characters or more"),
    ("speech: {keyterms: ['one two three four five six']}", "more than 5 words"),
    ("speech: {keyterms: ['a<b']}", "has <"),
    ("speech: {keyterms: ['[RAG]']}", "has [ ]"),
    ("speech: {keyterms: ['C\\\\D']}", "has \\"),
    ("speech: {keyterms: [" + ", ".join(f"t{n}" for n in range(1001)) + "]}", "1001, Scribe takes 1000 at most"),
    ("speech: {proxy: 'https://out:3128'}", "speech: proxy is not"),
])
def test_speech_refuses_terms_scribe_would_refuse(tmp_path, monkeypatch, section, error):
    with pytest.raises(SystemExit, match=re.escape(error)):
        load_platform(tmp_path, monkeypatch, PLATFORM + section + "\n")
    assert keyterm_problem("x" * 49) is None and keyterm_problem("one two three four five") is None
