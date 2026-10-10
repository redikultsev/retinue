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
                           "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "MCP_CONNECT_TIMEOUT_MS": "5000",
                           "MCP_TIMEOUT": "5000"}
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


def test_travel_ops_is_her_own_mcp_server_with_a_guard(instructions):
    """travel-ops is an MCP server of the run itself: every tool but the alerts the router takes, every answer
    whole; a PreToolUse hook checks the arguments of each call to it."""
    engine = ClaudeEngine(EngineConfig(instructions=instructions, tools=[], bus_tools=["search_archive"]),
                          "/workspace", "http://router:9100", "token", "http://travel-ops:8765/mcp")
    options = engine.options("turn-1", False)
    assert options.mcp_servers["travel"] == {"type": "http", "url": "http://travel-ops:8765/mcp",
                                             "timeout": 840_000, "alwaysLoad": True}
    assert "mcp__travel" in options.allowed_tools and "mcp__travel__watch_alerts" in options.disallowed_tools
    (matcher,) = options.hooks["PreToolUse"]
    assert matcher.matcher is None and len(matcher.hooks) == 1, "every call goes through it; it picks travel's"
    assert "travel" not in ClaudeEngine(EngineConfig(instructions=instructions), "/w", "http://r", "t").options(
        "turn-1", False).mcp_servers, "no address, no server"
    assert "travel" not in engine.options(None, False).mcp_servers, "a run outside a turn (the summary) has none"


def test_the_guard_refuses_owners_data_in_a_search_and_says_why(tmp_path):
    """An injected «city» with digits, a long text, Cyrillic personal data: refused before the call, with a reason
    the model reads; every decision is a protocol line at the router."""
    from retinue.engine import travel_guard

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private")
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner")
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            guard = travel_guard(str(server.make_url("")).rstrip("/"), bus_token("secret", "assistant"), turn.id)

            async def call(tool, **args):
                return await guard({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args,
                                    "tool_use_id": "t1"}, "t1", {"signal": None})

            trip = {"origin": "BEG", "place": "Kotor", "depart": "2026-10-22"}
            out = [await call("mcp__travel__search_trip", **trip),
                   await call("mcp__travel__search_trip", **dict(trip, place="Kotor 4510 123456")),
                   await call("mcp__travel__search_trip", **dict(trip, place="Kotor, " + "my employer Acme " * 5)),
                   await call("mcp__travel__airports_near", place="Иванов Иван Иванович, ул. Ленина"),
                   await call("mcp__travel__stay_photos", search_id="sj4bt6gc", stays=["Golden Bay Apartment"]),
                   await call("mcp__retinue__search_archive", query="паспорт")]
            return core, out
        finally:
            await server.close()

    core, (allowed, digits, long, cyrillic, photos, other) = asyncio.run(run())
    assert allowed == {} and photos == {} and other == {}, "allowed calls and other tools: no decision of its own"
    for denied, field in ((digits, "place"), (long, "place"), (cyrillic, "place")):
        out = denied["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"].startswith(f"Не отправлено на сайты: {field}: название латиницей")
    rows = core.store.db.execute("SELECT target, status FROM protocol WHERE channel = 'bus' ORDER BY id").fetchall()
    assert rows == [("travel/search_trip", "allowed"), ("travel/search_trip", "denied"),
                    ("travel/search_trip", "denied"), ("travel/airports_near", "denied"),
                    ("travel/stay_photos", "allowed")], "the archive search is not travel's"


def test_the_guard_lets_a_call_through_when_the_router_cannot_be_told():
    from retinue.engine import travel_guard

    guard = travel_guard("http://127.0.0.1:9", "token", "turn-1")  # nothing listens there
    out = asyncio.run(guard({"tool_name": "mcp__travel__sources", "tool_input": {}}, "t1", {"signal": None}))
    assert out == {}, "the record is best effort; the check is not"
    out = asyncio.run(guard({"tool_name": "mcp__travel__book", "tool_input": {}}, "t1", {"signal": None}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", "an unknown tool of travel-ops: refused"


def test_the_guard_fails_closed(tmp_path, monkeypatch):
    """CLI 2.1.286 takes a hook that raised for «no decision», and the call goes out. So nothing in the guard may
    raise: a key no encoder takes, a null where the form needs a value, a bug in the check — each is a refusal,
    decided before the router is told, and telling the router cannot break it."""
    import json

    from retinue import engine as engine_module
    from retinue.engine import travel_guard

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private")
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner")
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            guard = travel_guard(str(server.make_url("")).rstrip("/"), bus_token("secret", "assistant"), turn.id)

            async def call(tool, args):
                return await guard({"tool_name": f"mcp__travel__{tool}", "tool_input": args}, "t", {"signal": None})

            trip = {"origin": "BEG", "place": "Kotor", "depart": "2026-10-22"}
            out = {"surrogate": await call("search_trip", {**trip, json.loads('"\\ud800"'): 1}),
                   "kind-null": await call("watch_price", {"kind": None, "arguments": {"x": "secret 123"}}),
                   "lower": await call("search_trip", dict(trip, origin="beg")),
                   "not-a-dict": await call("search_trip", "BEG Kotor")}
            monkeypatch.setattr(engine_module.travel, "exact", lambda tool, args: 1 / 0)
            out["bug"] = await call("search_trip", trip)
            return core, out
        finally:
            await server.close()

    core, out = asyncio.run(run())
    for name, result in out.items():
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", name
    assert "проверка аргументов не удалась (ZeroDivisionError)" in out["bug"]["hookSpecificOutput"][
        "permissionDecisionReason"], "a bug refuses, and says so"
    assert "нет поля kind" in out["kind-null"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert "пиши ровно 'BEG'" in out["lower"]["hookSpecificOutput"]["permissionDecisionReason"]
    rows = core.store.db.execute("SELECT target, status FROM protocol WHERE channel = 'bus'").fetchall()
    assert rows == [("travel/search_trip", "denied"), ("travel/watch_price", "denied"),
                    ("travel/search_trip", "denied"), ("travel/search_trip", "denied"),
                    ("travel/search_trip", "denied")], "even the key no encoder takes is on record"


def test_a_run_may_call_travel_ops_twelve_times():
    """Each allowed call can carry a few words to the sites; a run gets at most twelve of them."""
    from retinue.engine import TRAVEL_CALLS, travel_guard

    guard = travel_guard("http://127.0.0.1:9", "token", "turn-1")
    call = {"tool_name": "mcp__travel__refine_flights", "tool_input": {"search_id": "fwgug2dt"}}
    outs = [asyncio.run(guard(call, "t", {"signal": None})) for _ in range(TRAVEL_CALLS + 1)]
    assert TRAVEL_CALLS == 12 and outs[:-1] == [{}] * 12
    assert "не больше 12 вызовов travel-ops" in outs[-1]["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_travel_ops_that_does_not_answer_costs_a_turn_seconds():
    """The CLI connects every MCP server before the turn; travel-ops down or hung must not hold the owner's turn
    for the CLI's 30 s default. Five seconds for connecting and listing; a tool call keeps its own limit."""
    from retinue.engine import RUN_ENV

    assert RUN_ENV["MCP_TIMEOUT"] == "5000" and RUN_ENV["MCP_CONNECT_TIMEOUT_MS"] == "5000"
    assert "MCP_TOOL_TIMEOUT" not in RUN_ENV, "a search's minutes are the server's own `timeout`"


MARKER = "<!-- Витрина: выше — правила, ниже — строки. -->"


@pytest.fixture
def kb(tmp_path):
    """A base as her container sees it, and the policy next to it."""
    import json

    root = tmp_path / "kb"
    (root / "notes" / "knowledge").mkdir(parents=True)
    (root / "notes" / "AGENTS.md").write_text(f"# Заметки\n\n- правило\n\n{MARKER}\n\n- [a](knowledge/a.md)\n")
    (root / "notes" / "knowledge" / "a.md").write_text("---\nid: a\n---\n\nФакт.\n")
    (root / "notes" / "knowledge" / "out.md").symlink_to("/etc/hosts")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"marker": MARKER, "writable": ["notes/knowledge/**/*.md"],
                                  "lines_below": ["*/AGENTS.md"], "never": ["**/CLAUDE.md", "**/AGENTS.md", ".*/**"]},
                                 ensure_ascii=False))
    return str(root), str(policy)


def test_the_base_is_hers_through_her_file_tools_and_a_guard(instructions, kb):
    root, policy = kb
    cfg = EngineConfig(instructions=instructions, tools=[], disallowed_tools=["Bash", "WebSearch", "WebFetch"])
    options = ClaudeEngine(cfg, "/workspace", "http://router:9100", "token", "", root, policy).options("turn-1", False)
    assert options.tools == ["Read", "Grep", "Glob", "Edit", "Write"] and options.add_dirs == [root]
    assert {"Read", "Grep", "Glob", "Edit", "Write"} <= set(options.allowed_tools) and "Bash" in options.disallowed_tools
    assert options.setting_sources == [] and options.cwd == "/workspace", "the base is a folder, never settings"
    (matcher,) = options.hooks["PreToolUse"]
    assert matcher.matcher is None and len(matcher.hooks) == 1
    plain = ClaudeEngine(cfg, "/workspace", "http://router:9100", "token").options("turn-1", False)
    assert plain.tools == [] and plain.add_dirs == [] and "PreToolUse" not in (plain.hooks or {}), "no base, no files"


def test_the_guard_keeps_reading_in_the_base_and_writing_in_records(kb, monkeypatch):
    from retinue import kbcheck
    from retinue.engine import memory_guard

    root, policy = kb

    async def ack(*args):  # the router says the turn is open and holds the queue; its own test is below
        return True

    monkeypatch.setattr("retinue.engine._ack", ack)
    guard = memory_guard(root, kbcheck.Policy.load(policy), writable=True)
    summary = memory_guard(root, kbcheck.Policy.load(policy), writable=False)

    def call(tool, which=guard, **args):
        out = asyncio.run(which({"tool_name": tool, "tool_input": args}, "t", {"signal": None}))
        return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "ok")

    showcase = f"{root}/notes/AGENTS.md"
    allowed = [call("Read", file_path=f"{root}/notes/knowledge/a.md"), call("Grep", pattern="Факт", path=root),
               call("Glob", pattern="**/*.md", path=f"{root}/notes"),
               call("Write", file_path=f"{root}/notes/knowledge/b.md", content="---\nid: b\n---\n"),
               call("Edit", file_path=showcase, old_string="- [a](knowledge/a.md)", new_string="- [a]\n- [b]"),
               call("Write", file_path=showcase, content=open(showcase).read() + "- [b](knowledge/b.md)\n"),
               call("mcp__retinue__search_archive", query="паспорт"), call("Read", which=summary,
                                                                            file_path=f"{root}/notes/AGENTS.md")]
    assert allowed == ["ok"] * 8
    assert call("Read", file_path="/proc/self/environ") == f"Не выполнено: вне базы. Читать и писать можно только в {root}/."
    assert call("Read", file_path=f"{root}/notes/knowledge/out.md").startswith("Не выполнено: вне базы"), "a symlink out"
    assert call("Read", file_path=f"{root}/../agent/CLAUDE.md").startswith("Не выполнено: вне базы")
    assert call("Read", file_path="notes/knowledge/a.md") == f"Не выполнено: путь — абсолютный, от {root}/."
    assert call("Grep", pattern="токен") == f"Не выполнено: укажи path — абсолютный путь в базе, от {root}/."
    assert call("Glob", pattern="../../proc/*", path=root).startswith("Не выполнено: pattern — относительный")
    assert call("Write", file_path=f"{root}/notes/knowledge/CLAUDE.md", content="x").startswith(
        "Не записано: notes/knowledge/CLAUDE.md — сюда писать нельзя.")
    assert call("Write", file_path=f"{root}/scripts/lint.py", content="x").startswith("Не записано: scripts/lint.py")
    assert call("Edit", file_path=showcase, old_string="- правило", new_string="- другое").endswith(
        "выше маркера Витрины правила; меняй только строки ниже него.")
    assert call("Write", file_path=showcase, content="# Заметки\n").endswith("меняй только строки ниже него.")
    assert call("Write", which=summary, file_path=f"{root}/notes/knowledge/b.md", content="x") == \
        "Не записано: вне хода разговора база только для чтения."
    assert call("Write", file_path=None) == f"Не выполнено: укажи file_path — абсолютный путь в базе, от {root}/."
    out = asyncio.run(guard({"tool_name": "Edit", "tool_input": "notes"}, "t", {"signal": None}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", "fails closed"


def test_every_guard_decision_on_the_base_is_on_record(kb, tmp_path):
    """Like travel-ops: each file tool call she makes is a protocol line `kb/<tool>` with the path and, for a
    refusal, the reason."""
    from retinue import kbcheck
    from retinue.engine import memory_guard

    root, policy = kb

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private")
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner")
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            guard = memory_guard(root, kbcheck.Policy.load(policy), True, str(server.make_url("")).rstrip("/"),
                                 bus_token("secret", "assistant"), turn.id)

            async def call(tool, **args):
                return await guard({"tool_name": tool, "tool_input": args}, "t", {"signal": None})

            await call("Read", file_path=f"{root}/notes/knowledge/a.md")
            await call("Write", file_path=f"{root}/notes/knowledge/CLAUDE.md", content="x")
            await call("mcp__retinue__search_archive", query="паспорт")
            return core, telegram
        finally:
            await server.close()

    core, telegram = asyncio.run(run())
    rows = core.store.db.execute("SELECT target, status FROM protocol WHERE channel = 'bus' ORDER BY id").fetchall()
    assert rows == [("kb/Read", "allowed"), ("kb/Write", "denied")], "the archive search is not the base's"
    lines = [e[1] for e in telegram.events if e[0] == "protocol"]
    assert lines[0] == f"kb: Ассистентка → Read {root}/notes/knowledge/a.md: allowed"
    assert lines[1].startswith(f"kb: Ассистентка → Write {root}/notes/knowledge/CLAUDE.md: denied — Не записано:")


def test_the_guard_refuses_hidden_names_and_case_tricks(kb, monkeypatch):
    """`claude.md` is CLAUDE.md on the owner's Mac; `.git/` inside the base is storage git never sees and never
    cleans; `.claude/` is settings. None of them is hers, in any case or form."""
    from retinue import kbcheck
    from retinue.engine import memory_guard

    root, policy = kb

    async def ack(*args):
        return True

    monkeypatch.setattr("retinue.engine._ack", ack)
    guard = memory_guard(root, kbcheck.Policy.load(policy), writable=True)

    def call(tool, **args):
        out = asyncio.run(guard({"tool_name": tool, "tool_input": args}, "t", {"signal": None}))
        return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "ok")

    for name in ("claude.md", "Claude.md", "agents.md"):
        assert call("Write", file_path=f"{root}/notes/knowledge/{name}", content="x").startswith(
            f"Не записано: notes/knowledge/{name} — сюда писать нельзя."), name
    for path in ("notes/knowledge/.git/x.md", "notes/knowledge/.claude/settings.json", "notes/.hidden.md"):
        assert call("Write", file_path=f"{root}/{path}", content="x") == \
            f"Не выполнено: {path} — имя с точки, скрытое; в базе таких нет.", path
        assert call("Read", file_path=f"{root}/{path}").startswith("Не выполнено:"), path
    assert call("Glob", pattern="**/.git/*", path=root).startswith("Не выполнено: pattern")


def test_a_write_needs_the_routers_word_that_the_turn_is_still_running(kb, tmp_path):
    """A run the router has given up on (timed out, restarted) must not keep writing: its changes would land in
    the next turn's commit, or be wiped under it. Before each Edit or Write the router confirms this turn is open
    and is the one holding the queue; no answer is a no."""
    from retinue import kbcheck
    from retinue.engine import memory_guard

    root, policy = kb
    record = {"file_path": f"{root}/notes/knowledge/b.md", "content": "x"}

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private")
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner")
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        url = str(server.make_url("")).rstrip("/")
        try:
            guard = memory_guard(root, kbcheck.Policy.load(policy), True, url, bus_token("secret", "assistant"), turn.id)

            async def write():
                out = await guard({"tool_name": "Write", "tool_input": record}, "t", {"signal": None})
                return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "ok")

            out = [await write()]                    # open, but another run holds the queue
            core.writer = turn.id
            out.append(await write())                # open and holding it
            core.turns.close(turn)
            out.append(await write())                # given up on
        finally:
            await server.close()
        dead = memory_guard(root, kbcheck.Policy.load(policy), True, "http://127.0.0.1:9", "t", "turn-1")
        out.append((await dead({"tool_name": "Write", "tool_input": record}, "t", {"signal": None}))
                   ["hookSpecificOutput"]["permissionDecisionReason"])
        out.append(await dead({"tool_name": "Read", "tool_input": {"file_path": f"{root}/notes/AGENTS.md"}}, "t",
                              {"signal": None}))
        return out

    refused, allowed, closed, unreachable, read = asyncio.run(run())
    no = "Не записано: Роутер не подтвердил, что этот ход ещё идёт и пишет он один — правка не сохранилась бы."
    assert allowed == "ok" and refused == closed == unreachable == no
    assert read == {}, "reading needs no word from the router"


SCHEMA = {"type": "object", "properties": {"keep": {"type": "boolean"}}, "required": ["keep"]}


def test_a_bare_run_reads_a_letter_with_no_tool_and_answers_by_the_schema(instructions, kb, monkeypatch):
    """The mail's triage: somebody else's letter, one per run — no bus, no travel-ops, no base, no hook, no folder;
    only the CLI's own tool that carries the answer. A run with a schema and tools keeps its tools."""
    root, policy = kb
    cfg = EngineConfig(instructions=instructions, tools=[], disallowed_tools=["Bash", "WebSearch", "WebFetch"],
                       bus_tools=["search_archive"], max_turns=20, model="claude-opus-5-5")
    engine = ClaudeEngine(cfg, "/workspace", "http://router:9100", "token", "http://travel-ops:8765/mcp", root, policy)
    bare = engine.options(None, False, None, SCHEMA, bare=True)
    assert bare.tools == [] and bare.allowed_tools == ["StructuredOutput"] and bare.mcp_servers == {}
    assert not bare.hooks and bare.add_dirs == [] and bare.resume is None and bare.setting_sources == []
    assert bare.output_format == {"type": "json_schema", "schema": SCHEMA} and bare.max_turns == 4
    assert bare.permission_mode == "dontAsk" and bare.verbatim_prompts and bare.model == "claude-opus-5-5"
    assert "Bash" in bare.disallowed_tools and bare.system_prompt == "Ты — ассистентка."
    shaped = engine.options("turn-1", False, None, SCHEMA)
    assert shaped.output_format == {"type": "json_schema", "schema": SCHEMA} and "StructuredOutput" in shaped.allowed_tools
    assert "retinue" in shaped.mcp_servers and shaped.add_dirs == [root], "an event run keeps its tools"
    assert engine.options("turn-1", False).output_format is None, "a turn of the conversation answers in words"
    seen = []

    async def cli(prompt, options):
        seen.append(options)
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                            session_id="s-x", result="", structured_output={"keep": False, "kind": "реклама"})

    monkeypatch.setattr("retinue.engine.query", cli)
    result = asyncio.run(engine.run("[Разбор письма]", None, None, None, schema=SCHEMA, bare=True))
    assert result.text == '{"keep": false, "kind": "реклама"}' and seen[0].mcp_servers == {}, "the answer as JSON"


def test_a_schema_reaches_the_engine_through_the_host(tmp_path):
    """The router names the schema in the message's metadata; `bare` is a fresh session with no turn, never kept."""
    import socket

    import uvicorn
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore
    from starlette.applications import Starlette

    from retinue.agent_host import EngineExecutor, SessionMap, build_card
    from retinue.config import AgentConfig, Skill
    from retinue.core import ask_agent
    from retinue.engine import EngineResult

    calls = []

    class Engine:
        async def run(self, prompt, session_id, on_text=None, turn_id=None, **shaped):
            calls.append((prompt, session_id, turn_id, shaped))
            return EngineResult(text='{"keep": true}', is_error=False, session_id="s-1")

    async def run():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = AgentConfig(id="t", name="T", description="d", trust_class="private",
                          skills=[Skill(id="c", name="c", description="d")], engine=EngineConfig(),
                          public_url=f"http://127.0.0.1:{port}")
        card = build_card(cfg)
        sessions = SessionMap(str(tmp_path / "agent.sqlite"))
        handler = DefaultRequestHandler(agent_executor=EngineExecutor(Engine(), sessions), task_store=InMemoryTaskStore(),
                                        agent_card=card)
        app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{port}"
            bare = await ask_agent(url, "письмо", "triage-1", None, "turn-x", control="bare", schema=SCHEMA)
            shaped = await ask_agent(url, "события", "mail-1", None, "turn-y", control="oneshot", schema=SCHEMA)
            plain = await ask_agent(url, "сводка", "summary-1", None, None, control="oneshot")
            return bare, shaped, plain, sessions.get("triage-1"), sessions.get("mail-1")
        finally:
            server.should_exit = True
            await serve

    bare, shaped, plain, kept_bare, kept_shaped = asyncio.run(run())
    assert bare == ("done", '{"keep": true}', [])
    assert calls == [("письмо", None, None, {"schema": SCHEMA, "bare": True}),
                     ("события", None, "turn-y", {"schema": SCHEMA}), ("сводка", None, None, {})], \
        "a bare run never gets a turn: no bus tool could be bound to it"
    assert kept_bare is None and kept_shaped is None, "background runs keep no session"


def test_both_limit_windows_are_kept_from_what_the_cli_reports(instructions, tmp_path, monkeypatch):
    """The CLI reports the five-hour and the weekly window on every event (`unifiedWindows`, beyond the fields the
    SDK models): kept as numbers, so that the status page shows the share of the subscription even while allowed."""
    def cli(windows):
        async def run(prompt, options):
            yield RateLimitEvent(rate_limit_info=RateLimitInfo(status="allowed", raw={
                "status": "allowed", "unifiedWindows": windows}), uuid="u", session_id="s-1")
            yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=1,
                                session_id="s-1", result="ответ")
        return run

    engine = ClaudeEngine(EngineConfig(instructions=instructions, config_dir=str(tmp_path / "c")), str(tmp_path))
    monkeypatch.setattr("retinue.engine.query", cli({"five_hour": {"utilization": 0.23, "resetsAt": 1760010000},
                                                      "seven_day": {"utilization": 0.63, "resetsAt": 1760500000},
                                                      "seven_day_overage_included": {"utilization": 0.1}}))
    seen = asyncio.run(engine.run("привет", "s-1")).rate_limit
    assert seen == {"status": "allowed", "windows": {"five_hour": {"utilization": 0.23, "resets_at": 1760010000},
                                                     "seven_day": {"utilization": 0.63, "resets_at": 1760500000}}}
    monkeypatch.setattr("retinue.engine.query", cli({"five_hour": {"utilization": "много"}, "seven_day": None}))
    assert asyncio.run(engine.run("привет", "s-1")).rate_limit == {"status": "allowed"}, "nothing that is not a number"


def test_publish_trip_reaches_the_router_and_answers_with_the_page_address(tmp_path):
    """Her tool sends the trip to the router's bus; only during her own turn and with the grant."""
    from retinue import lifehub

    from test_lifehub import FLIGHT, FLIGHTS, trip_with
    from test_travel import travel_ops

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private", trips=True)
        plain = RouterAgent(id="other", name="Другой", url="o", trust_class="private")
        hub = lifehub.Lifehub(lifehub.Data(str(tmp_path / "data")), "https://hub.in.example.com",
                              ["www.kupibilet.ru"])
        core = Core([agent, plain], Store(str(tmp_path / "r.sqlite")), "owner", lifehub=hub,
                    travel=travel_ops([FLIGHTS], []))
        await core.start([FakeChannel("telegram", False)])
        turn, other = core.turns.open_root("assistant"), core.turns.open_root("other")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            publish = bus_tools(url, bus_token("secret", "assistant"), turn.id)["publish_trip"]
            done = await publish.handler(trip_with(FLIGHT))
            refused = await bus_tools(url, bus_token("secret", "other"), other.id)["publish_trip"].handler(
                trip_with(FLIGHT))
            core.turns.close(turn)
            late = await publish.handler(trip_with(FLIGHT))
            return publish, done, refused, late
        finally:
            await server.close()

    publish, done, refused, late = asyncio.run(run())
    assert publish.input_schema is lifehub.TRIP_SCHEMA, "she sees the very schema code checks"
    assert not done["is_error"] and done["content"][0]["text"].startswith("Опубликовано: https://hub.in.example.com/trips/")
    assert refused["is_error"] and "публикация поездок не выдана" in refused["content"][0]["text"]
    assert late["is_error"] and "Нет активного запроса" in late["content"][0]["text"]


def test_draft_reply_reaches_the_router_and_answers_what_the_owner_will_see(tmp_path):
    """Her tool sends the draft to the router's bus; only during her own turn and with the grant. The schema she sees
    is the one code checks; the answer never says «sent»."""
    from retinue import courier
    from retinue.outbox import Outbox, OutboxStore

    from test_outbox import NOW, CardChannel, FakeNeighbour, letter

    async def run():
        agent = RouterAgent(id="assistant", name="Ассистентка", url="a", trust_class="private", drafts=True)
        plain = RouterAgent(id="other", name="Другой", url="o", trust_class="private")
        store = Store(str(tmp_path / "r.sqlite"))
        outbox = Outbox(OutboxStore(store.db), sender=FakeNeighbour(), owner_tg=42)
        outbox.clock = lambda: NOW
        core = Core([agent, plain], store, "owner", archive=Archive(str(tmp_path / "a.sqlite")), outbox=outbox)
        await core.start([CardChannel()])
        event = letter(core.archive)
        turn, other = core.turns.open_root("assistant"), core.turns.open_root("other")
        server = TestServer(BusServer(core, "secret", 0).app)
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            draft = bus_tools(url, bus_token("secret", "assistant"), turn.id)["draft_reply"]
            done = await draft.handler({"reply_to": event.id, "text": "Да, четверг подходит."})
            refused = await bus_tools(url, bus_token("secret", "other"), other.id)["draft_reply"].handler(
                {"reply_to": event.id, "text": "Да."})
            core.turns.close(turn)
            late = await draft.handler({"reply_to": event.id, "text": "Да."})
            return draft, done, refused, late
        finally:
            await server.close()

    draft, done, refused, late = asyncio.run(run())
    assert draft.input_schema is courier.DRAFT_SCHEMA, "she sees the very schema code checks"
    assert not done["is_error"] and "Уйдёт, только если он нажмёт «Отправить»" in done["content"][0]["text"]
    assert refused["is_error"] and "черновики не выданы" in refused["content"][0]["text"]
    assert late["is_error"] and "Нет активного запроса" in late["content"][0]["text"]
