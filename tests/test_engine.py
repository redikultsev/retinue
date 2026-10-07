"""Engine without a model: what a run is allowed, and the agent's tools talking to the router's bus."""

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestServer
from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultError, ResultMessage, SystemMessage

from retinue.archive import ASSISTANT, Archive
from retinue.bus import BusServer, bus_token
from retinue.config import EngineConfig, RouterAgent
from retinue.core import Core
from retinue.engine import SESSION_LOST, TOO_LARGE, UNREADABLE, UNREADABLE_NOTE, ClaudeEngine, bus_tools
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
    reminders = ["set_reminder", "list_reminders", "cancel_reminder", "move_reminder"]
    assert tools_of(bus_tools=reminders)[0] == [f"mcp__retinue__{name}" for name in reminders]

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
    assert result.new_session, "the router sends «now» to the new session"


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


def test_a_run_reports_compaction_and_tokens(instructions, tmp_path, monkeypatch):
    async def cli(prompt, options):
        yield SystemMessage(subtype="init", data={"tools": [], "mcp_servers": [], "model": "claude-opus-5-5"})
        if prompt == "длинный разговор":
            yield SystemMessage(subtype="compact_boundary",
                                data={"compact_metadata": {"trigger": "auto", "pre_tokens": 160000}})
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id="s-1", result="ответ", total_cost_usd=0.25,
                            usage={"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5600,
                                   "cache_creation_input_tokens": 78, "server_tool_use": {"web_search_requests": 0}})

    monkeypatch.setattr("retinue.engine.query", cli)
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))
    long = asyncio.run(engine.run("длинный разговор", "s-1"))
    assert long.compacted and not long.new_session
    assert long.usage == {"input_tokens": 12, "output_tokens": 34, "cache_read_tokens": 5600, "cache_write_tokens": 78}
    assert long.cost_usd == 0.25
    assert not asyncio.run(engine.run("коротко", "s-1")).compacted


PHOTO = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/"}}
TURN = [{"type": "text", "text": "[вложение #1]"}, PHOTO, {"type": "text", "text": "что на фото?"}]


def blocks_cli(seen, refuse_files=False, lost=()):
    """A CLI that reads its prompt as the SDK would: a string, or every message of the stream."""
    async def cli(prompt, options):
        messages = prompt if isinstance(prompt, str) else [message async for message in prompt]
        seen.append({"prompt": messages, "resume": options.resume})
        if options.resume in lost:
            raise ResultError(f"No conversation found with session ID: {options.resume}")
        content = messages if isinstance(messages, str) else messages[0]["message"]["content"]
        if refuse_files and any(block["type"] == "image" for block in content):
            yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=True, num_turns=1,
                                session_id=options.resume or "s-new", api_error_status=400,
                                result="API Error: 400 Could not process image")
            raise ResultError("Claude Code returned an error result: API Error", data={"api_error_status": 400})
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id=options.resume or "s-new", result="вижу")
    return cli


def test_files_go_as_one_prebuilt_message_of_content_blocks(instructions, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("retinue.engine.query", blocks_cli(seen, lost={"gone"}))
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))
    result = asyncio.run(engine.run(TURN, "gone"))
    message = {"type": "user", "session_id": "", "parent_tool_use_id": None,
               "message": {"role": "user", "content": TURN}}
    assert [s["prompt"] for s in seen] == [[message], [message]], "the retry without resume gets a fresh stream"
    assert [s["resume"] for s in seen] == ["gone", None] and result.text.startswith(SESSION_LOST)
    asyncio.run(engine.run("просто текст", "s-new"))
    assert seen[-1]["prompt"] == "просто текст", "a turn without files goes as before"


def test_files_the_api_refuses_start_a_new_session_without_them(instructions, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("retinue.engine.query", blocks_cli(seen, refuse_files=True))
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))
    result = asyncio.run(engine.run(TURN, "s-1"))
    assert [s["resume"] for s in seen] == ["s-1", None], "the session that holds the refused file is left"
    content = seen[1]["prompt"][0]["message"]["content"]
    assert content == [{"type": "text", "text": UNREADABLE_NOTE}, TURN[0], TURN[2]], "the same turn, without files"
    assert result.text.startswith(UNREADABLE) and result.new_session and not result.is_error
    monkeypatch.setattr("retinue.engine.query", limit_cli(None, http_status=400))
    with pytest.raises(ResultError):
        asyncio.run(engine.run("текст", "s-1")), "a 400 on a turn without files is not this case"


def limit_cli(rate_status, http_status=429, raises=True):
    """What the CLI streams when the subscription window is closed: the rate limit event (when it says so), the
    failed result, then the SDK raises because the CLI exits non-zero."""
    async def cli(prompt, options):
        if rate_status:
            yield RateLimitEvent(rate_limit_info=RateLimitInfo(status=rate_status, resets_at=1760000000,
                                                               rate_limit_type="five_hour", utilization=1.0),
                                 uuid="u", session_id="s-1")
        failed = http_status is not None
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=failed, num_turns=1,
                            session_id="s-1", result="API Error: limit" if failed else "ответ",
                            api_error_status=http_status)
        if failed and raises:
            raise ResultError("Claude Code returned an error result: API Error",
                              data={"api_error_status": http_status, "session_id": "s-1"})
    return cli


def test_the_subscription_limit_is_an_answer_not_a_crash(instructions, tmp_path, monkeypatch):
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))

    def run(cli):
        monkeypatch.setattr("retinue.engine.query", cli)
        return asyncio.run(engine.run("привет", "s-1"))

    hit = run(limit_cli("rejected"))
    assert hit.is_error and hit.limit and hit.limit_until == 1760000000 and hit.session_id == "s-1"
    assert hit.rate_limit == {"status": "rejected", "rate_limit_type": "five_hour", "utilization": 1.0,
                              "resets_at": 1760000000}
    bare = run(limit_cli(None))
    assert bare.limit and bare.limit_until is None, "a 429 without the event: a limit, reset time unknown"
    quiet = run(limit_cli("rejected", raises=False))
    assert quiet.limit, "the failed result alone is enough"
    warned = run(limit_cli("allowed_warning", http_status=None))
    assert not warned.is_error and not warned.limit and warned.rate_limit["status"] == "allowed_warning"
    with pytest.raises(ResultError):
        run(limit_cli(None, http_status=500))


def test_reminder_tools_reach_the_router(tmp_path):
    friday = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=7)
    while friday.weekday() != 4:
        friday += timedelta(days=1)
    when = friday.strftime("%Y-%m-%dT18:00")

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private", reminders=True)
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner")
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            tools = bus_tools(str(server.make_url("")).rstrip("/"), bus_token("secret", "assistant"), turn.id)
            call = lambda name, **args: tools[name].handler(args)  # noqa: E731
            out = [await call("set_reminder", text="позвонить Х", when=when, weekday="пятница"),
                   await call("set_reminder", text="позвонить Х", when=when, weekday="четверг"),
                   await call("list_reminders"),
                   await call("move_reminder", id=1, when=when.replace("18:00", "19:30"), weekday="пт"),
                   await call("cancel_reminder", id=1),
                   await call("cancel_reminder", id=1)]
            core.turns.close(turn)
            out.append(await call("list_reminders"))
            return out
        finally:
            await server.close()

    set_, wrong, listed, moved, cancelled, again, late = [(r["is_error"], r["content"][0]["text"]) for r in asyncio.run(run())]
    assert not set_[0] and set_[1].startswith(f"Поставила: пт {friday.day} ") and ", 18:00 МСК — позвонить Х [id 1]." in set_[1]
    assert wrong[0] and "пятница, а не четверг" in wrong[1], "code checks the weekday, not the model"
    assert not listed[0] and "— позвонить Х [id 1]" in listed[1] and "#" not in listed[1]
    assert not moved[0] and moved[1].startswith("Перенесла: ") and "19:30 МСК" in moved[1]
    assert not cancelled[0] and cancelled[1].startswith("Отменила: ") and again[0]
    assert late[0] and "Нет активного запроса" in late[1], "only during the agent's own turn"


def test_get_attachment_tool_returns_the_same_picture_and_text(tmp_path):
    archive = Archive(str(tmp_path / "archive.sqlite"))
    photo = archive.attach("telegram:5", "photo", "фото 800×600", "своё", "", [("image/jpeg", b"jpeg")])
    paper = archive.attach("telegram:5", "document", "PDF «счёт.pdf», 1 стр.", "своё", "Invoice 4711",
                           [("application/pdf", b"%PDF")])
    store = Store(str(tmp_path / "r.sqlite"))
    archive.append("owner", "[вложения]", conversation_id=store.conversation("assistant"), channel="telegram",
                   native_id="5")
    stranger = archive.attach("system-x", "photo", "фото", "своё", "", [("image/jpeg", b"j")])  # not an owner message

    async def run():
        agents = [RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private", attachments=True),
                  RouterAgent(id="other", name="Другой", url="o", trust_class="private")]
        core = Core(agents, store, "owner", archive=archive)
        await core.start([FakeChannel("telegram", False)])
        turn, other = core.turns.open_root("assistant"), core.turns.open_root("other")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            get = bus_tools(url, bus_token("secret", "assistant"), turn.id)["get_attachment"].handler
            out = [await get({"id": photo.id}), await get({"id": paper.id}), await get({"id": 99}),
                   await get({"id": stranger.id}),
                   await bus_tools(url, bus_token("secret", "other"), other.id)["get_attachment"].handler({"id": 1})]
            core.turns.close(turn)
            out.append(await get({"id": photo.id}))
            return out
        finally:
            await server.close()

    picture, pdf, missing, foreign, denied, late = asyncio.run(run())
    image, text = picture["content"]
    assert not picture["is_error"] and image == {"type": "image", "data": "anBlZw==", "mimeType": "image/jpeg"}
    assert text["text"].startswith("[вложение #1: фото 800×600 · своё] — из сообщения 20") and "МСК" in text["text"]
    assert not pdf["is_error"] and [c["type"] for c in pdf["content"]] == ["text"], "a PDF cannot travel in a tool result"
    assert "Текстовый слой PDF" in pdf["content"][0]["text"] and "Invoice 4711" in pdf["content"][0]["text"]
    assert missing["is_error"] and "Вложения #99 нет" in missing["content"][0]["text"]
    assert foreign["is_error"], "only what the owner sent"
    assert denied["is_error"] and "не выданы" in denied["content"][0]["text"], "a grant in router.yaml, like reminders"
    assert late["is_error"] and "Нет активного запроса" in late["content"][0]["text"]


def refusing_cli(seen, status, message, when):
    """A CLI whose API refuses a run with `status` and `message` when `when(content, resume)` holds."""
    async def cli(prompt, options):
        messages = prompt if isinstance(prompt, str) else [message async for message in prompt]
        seen.append({"prompt": messages, "resume": options.resume})
        content = messages if isinstance(messages, str) else messages[0]["message"]["content"]
        if when(content, options.resume):
            yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=True, num_turns=1,
                                session_id=options.resume or "s-new", api_error_status=status,
                                result=f"API Error: {status} {message}")
            raise ResultError("Claude Code returned an error result: API Error",
                              data={"api_error_status": status, "result": f"API Error: {status} {message}"})
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id=options.resume or "s-files", result="ответ")
    return cli


def test_only_a_refusal_about_files_resets_the_session(instructions, tmp_path, monkeypatch):
    config = str(tmp_path / "c")
    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=config), str(tmp_path))
    seen = []
    monkeypatch.setattr("retinue.engine.query", refusing_cli(
        seen, 400, "messages.0.content: text content blocks must be non-empty", lambda content, resume: True))
    with pytest.raises(ResultError):
        asyncio.run(engine.run(TURN, "s-1")), "a 400 about something else is an ordinary failure"
    assert [s["resume"] for s in seen] == ["s-1"]

    seen.clear()
    monkeypatch.setattr("retinue.engine.query", refusing_cli(seen, 413, "request_too_large", lambda c, r: False))
    assert asyncio.run(engine.run(TURN, None)).session_id == "s-files", "a session that took files is remembered"
    monkeypatch.setattr("retinue.engine.query", refusing_cli(
        seen, 413, "request_too_large: Request exceeds the maximum size", lambda content, resume: resume in ("s-files", "s-other")))
    later = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=config), str(tmp_path))  # a restart
    result = asyncio.run(later.run("а теперь?", "s-files"))
    assert [s["resume"] for s in seen[1:]] == ["s-files", None] and result.new_session and not result.is_error
    assert result.text.startswith(TOO_LARGE) and "get_attachment" in seen[-1]["prompt"], \
        "a turn without files in a session full of them: too large, a new session, and the way back to the file"
    with pytest.raises(ResultError):
        asyncio.run(later.run("просто текст", "s-other")), "413 in a session without files is not about files"
    assert "get_attachment" in UNREADABLE_NOTE
