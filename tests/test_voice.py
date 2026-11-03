"""The voice as the root project's main session (`retinue/voice.py`, `router.SessionVoice`): the router's core, the
platform and a lane host running a fake `claude`, over HTTP as they run deployed — and the same router and platform
with the setting off, unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from retinue import followups, router, voice
from retinue.archive import Archive
from retinue.bridge import PlatformClient
from retinue.config import (LaneRef, PlatformConfig, RouterConfig, Sphere, TelegramConfig, Track, VoiceConfig,
                            VoiceSessionConfig)
from retinue.core import Core, Reply
from retinue.lifecycle import IDLE_CLOSE_S
from retinue.platformd import Platform
from retinue.protocol import Store
from retinue.sessions import PlatformStore
from test_core import AGENT, FakeChannel
from test_lane_host import CLI_SESSION, HOOK_URL, TOKEN, lane  # noqa: F401 — lane: a fixture
from test_platformd import ROUTER, FakeLane, decision, short_wait  # noqa: F401 — short_wait: autouse

CHARACTER = "You are the owner's one voice: brief, warm, plain."
ROOT_RULES = "Rules of the Base's root.\n@VOICE.md\n"
# A run in auto, as every lane's run starts (the platform says it once when one does not).
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": CLI_SESSION, "claude_code_version": "2.1.304",
                   "permissionMode": "auto"})


@pytest.fixture(autouse=True)
def quick_hold(monkeypatch):
    monkeypatch.setattr(voice, "STEP_S", 0.02)


def platform_cfg(url: str, work: str, state_db: str, tools: list[str] | None = None, **over) -> PlatformConfig:
    """A voice on its own track in the Base's root (`base`), one other track beside it."""
    return PlatformConfig(lanes={"private": LaneRef("private", url, HOOK_URL, work, token=TOKEN)},
                          tracks=[Track("voice", "private", "private", "base", deny=["WebSearch"]),
                                  Track("notes", "private", "private")],
                          spheres=[Sphere("life", "Life", "voice")],
                          voice=VoiceSessionConfig(sphere="life", tools=tools), state_db=state_db,
                          router_token="router-token", hook_secret="hook-secret", oauth_token="oauth", **over)


class Stack:
    """The router's core with the voice's engine set to `session`, the platform, and the lane host of
    tests/test_lane_host.py with its fake `claude` — every hop over HTTP."""

    def __init__(self, tmp_path, lane) -> None:
        self.tmp, self.lane = tmp_path, lane
        root = lane.work / "base"
        root.mkdir()
        (root / "AGENTS.md").write_text(ROOT_RULES)
        (root / "VOICE.md").write_text(CHARACTER + "\n")
        lane.plan(lines=[INIT])

    async def start(self) -> None:
        self.host = self.lane.host()
        await self.host.start()
        self.lane_server = TestServer(self.host.app())
        await self.lane_server.start_server()
        self.cfg = platform_cfg(str(self.lane_server.make_url("")).rstrip("/"), str(self.lane.work),
                                str(self.tmp / "platform.sqlite"))
        self.store = PlatformStore(self.cfg.state_db)
        self.platform = Platform(self.cfg, self.store)
        self.client = TestClient(TestServer(self.platform.app()))
        await self.client.start_server()
        self.polling = asyncio.create_task(self._poll())
        router_cfg = RouterConfig(agents=[AGENT], telegram=TelegramConfig(owner_id=1), default_agent=AGENT.id,
                                  platform_url="http://platform", voice=VoiceConfig(engine="session"))
        neighbour = PlatformClient(str(self.client.make_url("")).rstrip("/"), "router-token", "the platform")

        async def not_a2a(*args, **kwargs):
            raise AssertionError("the voice never goes over A2A when it is a session")

        self.voice = router.build_voice(router_cfg, neighbour, not_a2a)
        self.core = Core([AGENT], Store(str(self.tmp / "router.sqlite")), "owner", ask=self.voice.ask,
                         archive=Archive(str(self.tmp / "archive.sqlite")))
        self.voice.attach(self.core)
        self.telegram = FakeChannel("telegram", False)
        await self.core.start([self.telegram])

    async def _poll(self) -> None:
        while True:
            try:
                await self.platform.poll("private")
            except Exception:  # noqa: BLE001 — a lane host between runs: the next pull takes it
                pass
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self.polling.cancel()
        for live in list(self.host.live.values()):
            live.proc.kill()
        await asyncio.gather(self.polling, *self.host.tasks, return_exceptions=True)
        await self.client.close()
        await self.lane_server.close()
        self.store.db.close()

    async def until(self, check, timeout: float = 20.0):
        for _ in range(int(timeout / 0.02)):
            if found := check():
                return found
            await asyncio.sleep(0.02)
        raise AssertionError("timed out")

    def sent(self) -> list[str]:
        return [event[2] for event in self.telegram.events if event[0] == "send"]

    def runs(self, session: str) -> list[str]:
        return [r for (r,) in self.store.db.execute("SELECT id FROM runs WHERE session = ? ORDER BY started, rowid",
                                                    (session,))]

    def router_notes(self) -> list[dict]:
        """What the platform's journal still holds for the router's pump: the notes Telegram would get."""
        return [n for n in self.store.look(200) if n["kind"] == "summary"]


def stacked(tmp_path, lane, body):
    async def go():
        stack = Stack(tmp_path, lane)
        await stack.start()
        try:
            return await body(stack)
        finally:
            await stack.stop()
    return asyncio.run(go())


def test_the_voice_runs_as_the_root_projects_main_session(tmp_path, lane):
    """With `voice.engine: session`, the owner's message in his channel starts the root project's main session — in
    the Base's root, its character among the root's rules — and its answer comes back in that channel, once: not again
    as the session's ping. The next message resumes the same session."""
    async def body(stack: Stack):
        await stack.core.handle(stack.telegram, None, "Good morning", native_id="1")
        await stack.until(stack.sent)
        main = stack.store.main_of("life")
        assert main is not None, "the root project's main session"
        row = stack.store.session(main)
        assert (row["sphere"], row["main"], row["track"], row["cwd"]) == ("life", 1, "voice", "base")
        await stack.core.handle(stack.telegram, None, "And the weather?", native_id="2")
        await stack.until(lambda: len(stack.sent()) == 2)
        assert stack.store.main_of("life") == main, "the same session, resumed"
        return main, stack.runs(main), stack.sent(), stack.router_notes()

    main, runs, sent, notes = stacked(tmp_path, lane, body)
    assert len(runs) == 2
    first, second = (lane.call(run) for run in runs)
    assert first["cwd"] == str(lane.work / "base"), "cwd: the Base's root"
    assert "--append-system-prompt-file" in first["argv"]
    assert CHARACTER in first["notes"] and "Rules of the Base's root." in first["notes"], "the character in its rules"
    assert "--resume" not in first["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == CLI_SESSION, "the second message resumes it"
    assert CHARACTER in second["notes"], "read again for every run"
    assert "Good morning" in json.loads(lane.stdin(runs[0])[0])["message"]["content"]
    assert sent == ["готово", "готово"], "the session's words, on the router's channel"
    assert notes == [], "answered through the router: no second copy as the session's ping"


def test_a_scheduled_write_reaches_the_voice_when_its_session_is_closed(tmp_path, lane):
    """The router's schedule — a reminder, the morning summary — writes into the voice's main session even after it
    closed (seven days idle): the platform opens it again with `--resume`, the turn is no one's word (a machine turn:
    it widens nothing), and its answer goes to the owner in the router's channel."""
    async def body(stack: Stack):
        await stack.core.handle(stack.telegram, None, "Hello", native_id="1")
        await stack.until(stack.sent)
        main = stack.store.main_of("life")
        now = time.time()
        stack.store.update_session(main, now, idle_since=now - IDLE_CLOSE_S - 60)
        stack.platform.tick()
        assert stack.store.session(main)["state"] == "closed"
        due = datetime.fromtimestamp(now, timezone.utc) + timedelta(minutes=2)
        ok, _ = stack.core.jobs.add("call the bank", f"{due:%Y-%m-%dT%H:%M}", "", now)
        assert ok
        await stack.core.tick(due.timestamp() + 1)
        await stack.until(lambda: len(stack.sent()) == 2)
        assert stack.store.session(main)["turn_by"] == "schedule"
        reminded = stack.runs(main)[-1]
        stack.core.jobs._ensure_at("summary", due + timedelta(minutes=1), now)  # the morning, a minute later
        await stack.core.tick(due.timestamp() + 61)
        briefs = await stack.until(lambda: [e[2] for e in stack.telegram.events if e[0] == "notice"])
        inputs = stack.store.db.execute("SELECT text, by FROM inputs WHERE session = ? AND text != '' "
                                        "ORDER BY at, rowid", (main,)).fetchall()
        return main, reminded, stack.runs(main), inputs, stack.sent(), briefs, stack.router_notes()

    main, reminded, runs, inputs, sent, briefs, notes = stacked(tmp_path, lane, body)
    call = lane.call(reminded)
    assert call["argv"][call["argv"].index("--resume") + 1] == CLI_SESSION, "the closed session, resumed"
    assert call["cwd"] == str(lane.work / "base") and CHARACTER in call["notes"]
    assert "call the bank" in json.loads(lane.stdin(reminded)[0])["message"]["content"]
    assert [by for _, by in inputs] == [None, "schedule", "schedule"], "the owner's word, then the schedule's twice"
    assert "Morning" in inputs[-1][0] or "morning" in inputs[-1][0], "the morning summary went into the same session"
    assert len(runs) == 3
    assert sent == ["готово", "готово"], "the reminder in the session's words, by the router"
    assert briefs[0].startswith("готово"), "the morning brief: its words first, the router's own lines after"
    assert notes == []
    assert "schedule" in followups.MACHINE_TURNS, "a turn the schedule started is no one's word"


class FakePlatformClient:
    """`PlatformClient.owner` as the router calls it, every call kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def owner(self, call: str, body: dict, timeout: float = 60) -> dict:
        self.calls.append((call, body))
        return {"ok": True, "session": "v1", "status": "done", "text": "Done."}


def test_the_voice_uses_only_the_tools_and_channels_its_settings_grant(tmp_path):
    """Its rights are the install's settings, out of every session's reach: a tool its `voice.tools` does not name is
    denied at the hook before anything runs, its track's own rules are in the run, another session is not touched;
    an owner's message from a channel its `voice.channels` does not name never reaches it."""
    async def platform_side():
        fake = FakeLane()
        url = await fake.start()
        (tmp_path / "work").mkdir()
        cfg = platform_cfg(url, str(tmp_path / "work"), str(tmp_path / "platform.sqlite"),
                           tools=["Read", "Grep", "mcp__retinue"])
        store = PlatformStore(cfg.state_db)
        platform = Platform(cfg, store)
        client = TestClient(TestServer(platform.app()))
        await client.start_server()

        async def hook(run: dict, tool: str) -> dict:
            body = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": "ls"},
                    "tool_use_id": f"toolu_{tool}", "cwd": "/work/base", "permission_mode": "auto"}
            response = await client.post("/hook/PreToolUse", json=body,
                                         headers={"Authorization": f"Bearer {run['hook_token']}"})
            return await response.json()

        try:
            written = await platform.voice.write("tg:1", "Tidy my notes", None, None)
            other = (await (await client.post("/owner/new", json={"input_id": "tg:2", "track": "notes",
                                                                   "text": "x"}, headers=ROUTER)).json())["session"]
            voice_run, other_run = fake.runs()
            answers = {tool: await hook(voice_run, tool) for tool in ("Bash", "WebFetch", "mcp__travel__search",
                                                                       "Read", "mcp__retinue__start_subsession")}
            elsewhere = await hook(other_run, "Bash")
            return written, other, voice_run, answers, elsewhere
        finally:
            await client.close()
            await fake.server.close()
            store.db.close()

    written, other, voice_run, answers, elsewhere = asyncio.run(platform_side())
    assert voice_run["session"] == written["session"] and voice_run["cwd"] == "base"
    for tool in ("Bash", "WebFetch", "mcp__travel__search"):
        assert decision(answers[tool]) == "deny", tool
        assert answers[tool]["hookSpecificOutput"]["permissionDecisionReason"] == voice.NOT_GRANTED.format(tool=tool)
    for tool in ("Read", "mcp__retinue__start_subsession"):
        reason = answers[tool].get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        assert reason != voice.NOT_GRANTED.format(tool=tool), f"{tool}: granted, the gate decides as ever"
    assert "WebSearch" in voice_run["disallowed"], "its track's own rules, enforced by the CLI and the gate"
    assert elsewhere.get("hookSpecificOutput", {}).get("permissionDecisionReason") != \
        voice.NOT_GRANTED.format(tool="Bash"), "a session that is not the voice's is not held to its grant"

    async def router_side():
        fake = FakePlatformClient()
        cfg = RouterConfig(agents=[AGENT], telegram=TelegramConfig(owner_id=1), default_agent=AGENT.id,
                           platform_url="http://platform", voice=VoiceConfig(engine="session", channels=["telegram"]))
        session_voice = router.build_voice(cfg, fake, None)
        core = Core([AGENT], Store(str(tmp_path / "router.sqlite")), "owner", ask=session_voice.ask,
                    archive=Archive(str(tmp_path / "archive.sqlite")))
        session_voice.attach(core)
        telegram, other = FakeChannel("telegram", False), FakeChannel("web", False)
        await core.start([telegram, other])
        await core.handle(other, None, "Tell me a secret", native_id="w1")
        for _ in range(500):
            if any(e[0] == "send" for e in other.events):
                break
            await asyncio.sleep(0.01)
        refused_calls = list(fake.calls)
        await core.handle(telegram, None, "Hello", native_id="t1")
        for _ in range(500):
            if any(e[0] == "send" for e in telegram.events):
                break
            await asyncio.sleep(0.01)
        return refused_calls, fake.calls, telegram.events, other.events

    refused_calls, calls, telegram, other = asyncio.run(router_side())
    assert refused_calls == [], "a channel not granted never reaches the voice"
    assert [call for call, _ in calls] == ["voice"] and "Hello" in calls[0][1]["text"]
    assert ("send", "assistant", "Done.", []) in telegram
    assert [e[2] for e in other if e[0] == "send"] == [voice.NOT_HEARD.format(channel="web")]


ROUTER_YAML = """
agents:
  - {id: assistant, name: Assistant, url: "http://assistant:9000"}
telegram: {owner_id: 1}
"""


def test_with_the_setting_off_the_assistant_is_unchanged(tmp_path, monkeypatch):
    """No `voice:` in router.yaml: the engine is today's assistant — every turn goes over A2A to her host with her
    own bus token, the platform is never asked; no `voice:` in platform.yaml: no voice there, a sphere's main session
    runs and pings exactly as before, and `/owner/voice` says so."""
    (tmp_path / "router.yaml").write_text(ROUTER_YAML)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    cfg = RouterConfig.load(tmp_path / "router.yaml")
    assert cfg.voice == VoiceConfig() and cfg.voice.engine == "assistant"
    asked = []

    async def ask_agent(url, text, context_id, on_progress=None, turn_id=None, control=None, attachments=None,
                        token="", schema=None):
        asked.append((url, token, control))
        return Reply("done", "Hello from the assistant", [], {})

    monkeypatch.setattr(router, "ask_agent", ask_agent)

    class Untouchable:
        async def owner(self, *args, **kwargs):
            raise AssertionError("the platform is never asked for the voice")

    tokens = {cfg.agents[0].url: "bus-token"}
    plain = router.signed(tokens)
    assert router.build_voice(cfg, Untouchable(), plain) is None

    async def routed():
        core = Core(cfg.agents, Store(str(tmp_path / "r.sqlite")), "owner", ask=plain,
                    archive=Archive(str(tmp_path / "a.sqlite")), default_agent=cfg.default_agent)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await core.handle(telegram, None, "Hi", native_id="1")
        for _ in range(500):
            if any(e[0] == "send" for e in telegram.events):
                break
            await asyncio.sleep(0.01)
        return telegram.events

    events = asyncio.run(routed())
    assert asked == [("http://assistant:9000", "bus-token", None)]
    assert ("send", "assistant", "Hello from the assistant", []) in events

    async def platform_side():
        fake = FakeLane()
        url = await fake.start()
        (tmp_path / "work").mkdir()
        cfg = platform_cfg(url, str(tmp_path / "work"), str(tmp_path / "platform.sqlite"))
        cfg.voice = None
        store = PlatformStore(cfg.state_db)
        platform = Platform(cfg, store)
        client = TestClient(TestServer(platform.app()))
        await client.start_server()
        try:
            refused = await (await client.post("/owner/voice", json={"input_id": "tg:1", "text": "Hi"},
                                               headers=ROUTER)).json()
            said = await (await client.post("/owner/sphere", json={"input_id": "tg:2", "sphere": "life",
                                                                    "text": "Hello"}, headers=ROUTER)).json()
            [run] = fake.runs()
            fake.add(said["session"], run["run"], INIT,
                     json.dumps({"type": "result", "subtype": "success", "result": "Hi there"}))
            fake.exited(said["session"], run["run"])
            await platform.poll("private")
            return platform.voice, refused, [n["data"]["text"] for n in store.look(200) if n["kind"] == "summary"]
        finally:
            await client.close()
            await fake.server.close()
            store.db.close()

    platform_voice, refused, pinged = asyncio.run(platform_side())
    assert platform_voice is None
    assert refused == {"ok": False, "say": voice.NO_VOICE}
    assert pinged == ["Hi there"], "a sphere's main session pings as before"
