"""Engine without a model: what a run is allowed, and the agent's tools talking to the router's bus."""

import asyncio
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from claude_agent_sdk import ResultError, ResultMessage, SystemMessage

from retinue.archive import ASSISTANT, Archive
from retinue.bus import BusServer, bus_token
from retinue.config import EngineConfig, RouterAgent
from retinue.core import Core
from retinue.engine import SESSION_LOST, ClaudeEngine, bus_tools
from retinue.protocol import Store

from test_core import FakeChannel

@pytest.fixture
def instructions(tmp_path):
    path = tmp_path / "agent" / "CLAUDE.md"
    path.parent.mkdir()
    path.write_text("Ты — ассистентка.")
    return str(path)

def test_agent_gets_only_the_bus_tools_in_its_config(instructions):
    def tools_of(bus_url="http://router:9100", turn_id="turn-1", **cfg):
        engine = ClaudeEngine(EngineConfig(instructions=instructions, **cfg), "/workspace", bus_url, "token")
        options = engine.options(turn_id, False)
        return options.allowed_tools, sorted(options.mcp_servers)

    assert tools_of(bus_tools=["search_archive"]) == (["mcp__retinue__search_archive"], ["retinue"])
    assert tools_of(allowed_tools=["Glob"]) == (
        ["Glob", "mcp__retinue__ask_agent", "mcp__retinue__list_agents"], ["retinue"]), "the default is the old pair"
    assert tools_of(bus_tools=[]) == ([], [])
    assert tools_of(bus_tools=["search_archive"], bus_url="") == ([], []), "no bus, no tools"
    assert tools_of(bus_tools=["search_archive"], turn_id=None) == ([], [])

def test_search_archive_tool_reaches_the_router(tmp_path):
    archive = Archive(str(tmp_path / "archive.sqlite"))
    archive.append(ASSISTANT, "По Еревану советую Каскад.", conversation_id="c", channel="telegram")

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private", archive=True)
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner", archive=archive)
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            search = bus_tools(url, bus_token("secret", "assistant"), turn.id)["search_archive"].handler
            found = await search({"query": "Ереван"})
            core.turns.close(turn)
            late = await search({"query": "Ереван"})
            return found, late
        finally:
            await server.close()

    found, late = asyncio.run(run())
    assert not found["is_error"] and "Каскад" in found["content"][0]["text"]
    assert late["is_error"] and "Нет активного запроса" in late["content"][0]["text"], "only during the agent's own turn"

def test_a_run_resumes_the_conversation_session(instructions):
    engine = ClaudeEngine(EngineConfig(instructions=instructions), "/workspace")
    options = engine.options("turn-1", True, "session-1")
    assert options.resume == "session-1" and options.include_partial_messages is True
    assert engine.options("turn-1", False).resume is None, "no session yet: a new one starts"

def test_a_run_reads_no_settings_from_any_folder(instructions, tmp_path):
    cfg = EngineConfig(instructions=instructions, tools=[], bus_tools=["search_archive"],
                       disallowed_tools=["Bash", "WebSearch", "WebFetch"])
    cfg.config_dir = str(tmp_path / "claude")
    options = ClaudeEngine(cfg, str(tmp_path), "http://router:9100", "token").options("turn-1", False)
    assert options.setting_sources == [], "no settings.json, CLAUDE.md, hooks or skills from the working folder"
    assert options.system_prompt == "Ты — ассистентка.", "the instructions come from the read-only file"
    assert options.tools == [] and options.allowed_tools == ["mcp__retinue__search_archive"]
    assert {"Bash", "WebSearch", "WebFetch"} <= set(options.disallowed_tools)
    assert options.strict_mcp_config and options.verbatim_prompts and options.permission_mode == "dontAsk"
    assert options.env == {"CLAUDE_CONFIG_DIR": cfg.config_dir, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                           "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    with pytest.raises(SystemExit, match="instructions file"):
        ClaudeEngine(EngineConfig(instructions=str(tmp_path / "missing.md")), str(tmp_path))
    assert "CLAUDE_CONFIG_DIR" not in (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

def fake_cli(seen, lost=()):
    """A CLI that remembers what each run got; a session in `lost` is gone from disk."""
    async def run(prompt, options):
        config_dir = options.env["CLAUDE_CONFIG_DIR"]
        seen.append({"prompt": prompt, "resume": options.resume, "config_dir": config_dir,
                     "verbatim": options.verbatim_prompts, "servers": sorted(options.mcp_servers),
                     "mode": oct(os.stat(config_dir).st_mode & 0o777)})
        if options.resume in lost:
            raise ResultError(f"No conversation found with session ID: {options.resume}")
        yield SystemMessage(subtype="init", data={"tools": [], "mcp_servers": [], "apiKeySource": "none"})
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id=options.resume or "s-new", result=f"ответ на {prompt}")
    return run


def test_the_session_lives_in_one_config_dir(instructions, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("retinue.engine.query", fake_cli(seen))
    config_dir = tmp_path / "data" / "claude"
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(config_dir)), str(tmp_path))
    first = asyncio.run(engine.run("привет", None))
    second = asyncio.run(engine.run("а теперь?", first.session_id))
    assert (first.session_id, second.session_id) == ("s-new", "s-new")
    assert [s["resume"] for s in seen] == [None, "s-new"], "the second message continues the first one's session"
    assert {s["config_dir"] for s in seen} == {str(config_dir)} and config_dir.is_dir(), "one directory, kept"
    assert seen[0]["mode"] == "0o700"


def test_a_lost_session_starts_over_and_says_so(instructions, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("retinue.engine.query", fake_cli(seen, lost={"gone"}))
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))
    result = asyncio.run(engine.run("привет", "gone"))
    assert [s["resume"] for s in seen] == ["gone", None]
    assert result.text.startswith(SESSION_LOST) and result.session_id == "s-new" and not result.is_error


def test_compact_is_our_own_command(instructions, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("retinue.engine.query", fake_cli(seen))
    cfg = EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c"), bus_tools=["search_archive"])
    engine = ClaudeEngine(cfg, str(tmp_path), "http://router:9100", "token")
    result = asyncio.run(engine.compact("s-1"))
    assert not result.is_error and result.session_id == "s-1"
    (run,) = seen
    assert run["prompt"] == "/compact" and run["resume"] == "s-1"
    assert run["verbatim"] is False, "the one prompt a slash command may run from: a constant of ours"
    assert run["servers"] == [], "no tools while compacting"
    nothing = asyncio.run(engine.compact(None))
    assert len(seen) == 1 and not nothing.is_error and nothing.session_id is None, "no session, nothing to compact"
