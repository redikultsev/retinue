"""Engine without a model: what a run is allowed, and the agent's tools talking to the router's bus."""

import asyncio
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from claude_agent_sdk import ResultMessage, SystemMessage

from retinue.archive import ASSISTANT, Archive
from retinue.bus import BusServer, bus_token
from retinue.config import EngineConfig, RouterAgent
from retinue.core import Core
from retinue.engine import ClaudeEngine, bus_tools
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
        options = engine.options(turn_id, False, "/tmp/run")
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

def test_a_run_never_resumes_a_session(instructions):
    options = ClaudeEngine(EngineConfig(instructions=instructions), "/workspace").options("turn-1", True, "/tmp/run")
    assert options.resume is None and options.continue_conversation is False and options.session_id is None
    assert options.include_partial_messages is True

def test_a_run_reads_no_settings_from_any_folder(instructions, tmp_path):
    cfg = EngineConfig(instructions=instructions, tools=[], bus_tools=["search_archive"],
                       disallowed_tools=["Bash", "WebSearch", "WebFetch"])
    options = ClaudeEngine(cfg, str(tmp_path), "http://router:9100", "token").options("turn-1", False, "/tmp/run")
    assert options.setting_sources == [], "no settings.json, CLAUDE.md, hooks or skills from the working folder"
    assert options.system_prompt == "Ты — ассистентка.", "the instructions come from the read-only file"
    assert options.tools == [] and options.allowed_tools == ["mcp__retinue__search_archive"]
    assert {"Bash", "WebSearch", "WebFetch"} <= set(options.disallowed_tools)
    assert options.strict_mcp_config and options.verbatim_prompts and options.permission_mode == "dontAsk"
    assert options.env == {"CLAUDE_CONFIG_DIR": "/tmp/run", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                           "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    with pytest.raises(SystemExit, match="instructions file"):
        ClaudeEngine(EngineConfig(instructions=str(tmp_path / "missing.md")), str(tmp_path))
    assert "CLAUDE_CONFIG_DIR" not in (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

def test_config_dir_lives_for_one_run(instructions, tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    run_root.mkdir()
    seen = []

    async def fake_cli(prompt, options):
        config_dir = options.env["CLAUDE_CONFIG_DIR"]
        seen.append((config_dir, os.path.isdir(config_dir), os.listdir(config_dir)))
        Path(config_dir, "transcript.jsonl").write_text("everything the model read")
        yield SystemMessage(subtype="init", data={"tools": [], "mcp_servers": [{"name": "retinue"}], "apiKeySource": "none"})
        if prompt == "упади":
            raise RuntimeError("cli died")
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id="s", result="готово")

    monkeypatch.setattr("retinue.engine.query", fake_cli)
    engine = ClaudeEngine(EngineConfig(instructions=instructions, run_root=str(run_root)), str(tmp_path))
    assert asyncio.run(engine.run("привет")).text == "готово"
    with pytest.raises(RuntimeError):
        asyncio.run(engine.run("упади"))
    (first, existed, files), (second, _, _) = seen
    assert existed and files == [] and first != second, "every run starts in its own empty directory"
    assert first.startswith(str(run_root)) and os.listdir(run_root) == [], "and leaves nothing, even when it fails"
