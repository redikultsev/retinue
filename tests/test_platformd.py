"""The platform against fake lane hosts: aiohttp apps in this process that record every command and serve a scripted
journal (the transcript fixtures). Hooks and the owner's calls go over HTTP, as a lane CLI and the router send them.
A platform is torn down and built again from the same files where the scenario is a restart."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image
from aiohttp.test_utils import TestClient, TestServer

from retinue import gate, platformd, sessions
from retinue.config import LaneRef, PlatformConfig, Track
from retinue.platformd import Platform
from retinue.sessions import PlatformStore

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
ROUTER = {"Authorization": "Bearer router-token"}
HOUR, DAY = 3600, 86400


def fixture(name: str) -> list[str]:
    """The recorded lines, as a run in auto journals them: the fixtures were recorded in manual, and the init line's
    mode is the only field that differs (`test_a_run_that_did_not_start_in_auto_is_told_once` keeps manual)."""
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [line.replace('"permissionMode": "default"', '"permissionMode": "auto"') for line in lines]


DEFERRED = fixture("deferred")
CALL = json.loads(DEFERRED[-1])["deferred_tool_use"]  # Bash `echo ran >> ran.txt`, as the CLI deferred it
CLI_SESSION = json.loads(DEFERRED[0])["session_id"]


class FakeLane:
    """A lane host's surface: the commands it got, its journal, and what it answers."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []
        self.journal: list[dict] = []
        self.answers = {"/run": "started", "/answer": "sent", "/stop": "stopping"}
        self.delays: dict[str, float] = {}  # a path whose answer is this late
        self.server: TestServer | None = None

    def runs(self) -> list[dict]:
        return [body for path, body in self.commands if path == "/run"]

    def add(self, session: str, run: str, *lines: str) -> None:
        for line in lines:
            self.journal.append({"seq": len(self.journal) + 1, "session": session, "run": run, "line": line,
                                 "given": False})

    def exited(self, session: str, run: str) -> None:
        self.add(session, run, json.dumps({"type": "lane", "event": "exited", "code": 0}))

    async def start(self) -> str:
        async def command(request: web.Request) -> web.Response:
            self.commands.append((request.path, await request.json()))
            await asyncio.sleep(self.delays.get(request.path, 0))
            return web.json_response({"status": self.answers[request.path]})

        async def items(request: web.Request) -> web.Response:
            limit = (await request.json())["limit"]
            return web.json_response({"items": [{k: i[k] for k in ("seq", "session", "run", "line")}
                                                 for i in self.journal if not i["given"]][:limit]})

        async def confirm(request: web.Request) -> web.Response:
            upto = (await request.json())["upto"]
            for i in self.journal:
                i["given"] = i["given"] or i["seq"] <= upto
            return web.json_response({"confirmed": upto})

        app = web.Application()
        app.add_routes([web.post("/run", command), web.post("/answer", command), web.post("/stop", command),
                        web.post("/items", items), web.post("/items/confirm", confirm)])
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class World:
    """Two lanes, two tracks, one platform at a time over the same SQLite file."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.lanes = {"code": FakeLane(), "private": FakeLane()}
        self.clock = Clock()
        self.platform: Platform | None = None
        self.client: TestClient | None = None
        (tmp_path / "work" / "repo").mkdir(parents=True)

    async def start(self, max_sessions: int = 5) -> None:
        urls = {name: await lane.start() for name, lane in self.lanes.items()}
        self.cfg = PlatformConfig(
            lanes={name: LaneRef(name, url, "http://platform:9500/hook", str(self.tmp / "work"), token="lane-token")
                   for name, url in urls.items()},
            tracks=[Track("retinue", "code", "code", "repo", allow=["Bash(npm test:*)"]),
                    Track("notes", "private", "private")],
            max_sessions=max_sessions, state_db=str(self.tmp / "platform.sqlite"), router_token="router-token",
            hook_secret="hook-secret")
        await self.up()

    async def up(self) -> None:
        self.store = PlatformStore(self.cfg.state_db)
        self.platform = Platform(self.cfg, self.store, clock=self.clock)
        self.client = TestClient(TestServer(self.platform.app()))
        await self.client.start_server()

    async def down(self) -> None:
        await self.client.close()
        self.store.db.close()

    async def stop(self) -> None:
        await self.down()
        for lane in self.lanes.values():
            await lane.server.close()

    async def post(self, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
        response = await self.client.post(path, json=body, headers=ROUTER if headers is None else headers)
        return response.status, await response.json()

    async def owner(self, path: str, body: dict) -> dict:
        status, data = await self.post(f"/owner/{path}", body)
        assert status == 200, data
        return data

    async def hook(self, event: str, run: dict, body: dict) -> dict:
        return (await self.post(f"/hook/{event}", body, {"Authorization": f"Bearer {run['hook_token']}"}))[1]

    async def poll(self) -> None:
        for name in self.lanes:
            await self.platform.poll(name)

    def notes(self, kind: str | None = None) -> list[dict]:
        rows = self.store.db.execute("SELECT seq, kind, session, approval, data FROM notes ORDER BY seq").fetchall()
        return [{"seq": s, "kind": k, "session": se, "approval": a, "data": json.loads(d)} for s, k, se, a, d in rows
                if kind in (None, k)]


@pytest.fixture(autouse=True)
def short_wait(monkeypatch):
    monkeypatch.setattr(gate, "MARK_WAIT_S", 0.05)


def scenario(tmp_path, body, **start):
    async def go():
        world = World(tmp_path)
        await world.start(**start)
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


def pre_tool_use(call: dict = CALL, cwd: str = "/work/repo") -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": call["name"], "tool_input": call["input"],
            "tool_use_id": call["id"], "cwd": cwd}


def decision(answer: dict) -> str | None:
    return answer.get("hookSpecificOutput", {}).get("permissionDecision")


async def deferred_session(world: World) -> tuple[str, dict, dict]:
    """A code session whose first run deferred CALL, and the card the owner got for it."""
    session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
    [run] = world.lanes["code"].runs()
    assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer"
    world.lanes["code"].add(session, run["run"], *DEFERRED)
    world.lanes["code"].exited(session, run["run"])
    await world.poll()
    [card] = world.notes("card")
    return session, run, card


def test_a_deferred_call_is_granted_after_a_restart_and_runs_exactly_once(tmp_path):
    """E4: the card survives the platform going down; an hour later the press resumes the CLI once, the resumed
    PreToolUse is allowed once, and the lane's replay of what it already gave sends no second card."""
    async def body(world: World):
        session, first, card = await deferred_session(world)
        assert world.store.session(session)["state"] == "waiting"
        assert [label for label, _ in card["data"]["buttons"]] == ["Разрешить", "Отказать"]
        assert card["data"]["view"]["body"] == CALL["input"]["command"]

        await world.down()
        world.clock.now += HOUR
        await world.up()
        approve = card["data"]["buttons"][0][1]
        pressed = await world.owner("press", {"button": approve})
        assert pressed["toast"] == "Разрешено." and pressed["keep"] == []
        runs = world.lanes["code"].runs()
        assert len(runs) == 2
        resume = runs[1]
        assert (resume["prompt"], resume["cli_session"], resume["session"]) == (None, CLI_SESSION, session)
        assert resume["hook_token"] != first["hook_token"]
        assert first["instructions"] is True and resume["instructions"] is True, "a code lane: rules and skills"

        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "allow"
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) != "allow"
        assert (await world.owner("press", {"button": approve}))["toast"] == "Кнопка больше не действует."

        for line in world.lanes["code"].journal:
            line["given"] = False
        await world.poll()
        assert len(world.notes("card")) == 1
        assert world.store.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == len(DEFERRED) + 1
        assert len(world.lanes["code"].runs()) == 2

    scenario(tmp_path, body)


def test_a_never_list_call_becomes_a_courier_card_and_is_denied(tmp_path):
    """E6: `git push` — the marker fires, the CLI is told no, the owner gets the command to run himself."""
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "запушь"}))["session"]
        [run] = world.lanes["code"].runs()
        never = run["settings"]["hooks"]["PreToolUse"][0]["hooks"][0]
        assert never["if"] == "Bash(git push:*)" and never["url"].endswith("/hook/mark/0")
        assert "Bash(git push:*)" in run["disallowed"]
        push = pre_tool_use({"id": "toolu_push", "name": "Bash", "input": {"command": "git push origin main"}})
        marked, answer = await asyncio.gather(world.hook("mark/0", run, push), world.hook("PreToolUse", run, push))
        assert marked == {} and decision(answer) == "deny"
        assert answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.NEVER
        [card] = world.notes("card")
        assert card["data"]["view"]["head"][0].startswith("Курьер · retinue") and card["data"]["buttons"] == []
        assert world.store.approvals(session=session, states=("pending", "shown", "granted")) == []

    scenario(tmp_path, body)


def test_a_card_rule_is_neither_allowed_nor_disallowed_and_its_call_comes_as_a_card_in_auto(tmp_path):
    """The CLI must neither run a card rule's call by itself nor deny it past the owner's grant: the rule is only a
    marker. A call nothing names gets no decision in auto: no card, the classifier's."""
    async def body(world: World):
        world.cfg.tracks[0].card = ["Bash(git commit:*)"]
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "закоммить"}))["session"]
        [run] = world.lanes["code"].runs()
        assert "Bash(git commit:*)" not in run["allowed"] + run["disallowed"]
        marks = [h["hooks"][0] for h in run["settings"]["hooks"]["PreToolUse"]]
        n = next(i for i, h in enumerate(marks) if h.get("if") == "Bash(git commit:*)")
        commit = {**pre_tool_use({"id": "toolu_c", "name": "Bash", "input": {"command": "git commit -m x"}}),
                  "permission_mode": "auto"}
        _, answer = await asyncio.gather(world.hook(f"mark/{n}", run, commit), world.hook("PreToolUse", run, commit))
        assert decision(answer) == "defer"
        [approval] = world.store.approvals(session=session)
        assert approval["why"] == "Всегда спрашивать: правило Bash(git commit:*)"
        assert await world.hook("PreToolUse", run, {**pre_tool_use(), "permission_mode": "auto"}) == {}
        assert len(world.store.approvals(session=session)) == 1

    scenario(tmp_path, body)


def test_a_block_the_model_went_past_reaches_the_owner_when_the_run_ends(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "сделай"}))["session"]
        [run] = world.lanes["private"].runs()
        blocked = {**pre_tool_use(), "hook_event_name": "PermissionDenied", "permission_mode": "auto",
                   "reason": "[Remote Repoint]"}
        answer = await world.hook("PermissionDenied", run, blocked)
        assert answer == {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}}
        done = {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn", "result": "ок"}
        world.lanes["private"].add(session, run["run"], json.dumps(done))
        world.lanes["private"].exited(session, run["run"])
        await world.poll()
        [card] = world.notes("card")
        assert card["data"]["view"]["head"][0] == f"Заблокировано · notes · сессия {session}"
        assert "Классификатор остановил: [Remote Repoint]" in card["data"]["view"]["head"]
        assert card["data"]["buttons"] == [] and card["data"]["view"]["body"] == CALL["input"]["command"]

    scenario(tmp_path, body)


def test_a_run_that_did_not_start_in_auto_is_told_once(tmp_path):
    """The init line names the mode the CLI really runs in: another than auto, and the owner hears it once per
    session — not again from the session's next run, not from the same line pulled twice."""
    manual = [line.replace('"permissionMode": "auto"', '"permissionMode": "default"') for line in DEFERRED]

    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        world.lanes["code"].add(session, run["run"], *manual)
        await world.poll()  # its process has not exited yet: a token of an ended run opens no hook
        told = [n for n in world.notes("summary") if "Режим auto" in n["data"]["text"]]
        assert len(told) == 1 and "режиме default" in told[0]["data"]["text"]
        assert "claude-opus-5-5" in told[0]["data"]["text"] and "карточкой" in told[0]["data"]["text"]
        assert world.store.run_of(world.platform.gate.token_hash(run["hook_token"]))["mode"] == "default"
        assert decision(await world.hook("PreToolUse", run, {**pre_tool_use(), "permission_mode": "auto"})) == \
            "defer", "the run's init said manual: its calls fall back to cards"
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "deny", "ended: its token is spent"
        for line in world.lanes["code"].journal:
            line["given"] = False
        await world.poll()
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "ещё"})
        first, later = world.lanes["code"].runs()
        assert later["run"] != first["run"]
        world.lanes["code"].add(session, later["run"], manual[0])
        await world.poll()
        assert len([n for n in world.notes("summary") if "Режим auto" in n["data"]["text"]]) == 1

    scenario(tmp_path, body)


def test_lines_journaled_while_the_platform_was_down_are_ingested_once_in_order(tmp_path):
    """E12: the lane keeps its journal while the platform is away; a new platform takes it by number, confirms it,
    and a second replay adds no row."""
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        await world.down()
        lines = fixture("edit") + fixture("subagent")
        world.lanes["code"].add(session, run["run"], *lines)
        await world.up()
        await world.poll()
        assert all(i["given"] for i in world.lanes["code"].journal)
        stored = [row[0] for row in world.store.db.execute("SELECT line FROM raw ORDER BY lane_seq")]
        assert stored == lines
        projected = world.store.items(session)
        for line in world.lanes["code"].journal:
            line["given"] = False
        await world.poll()
        assert world.store.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == len(lines)
        assert world.store.items(session) == projected
        assert [n["kind"] for n in world.notes()] == ["summary", "summary"], "one per result, not per replay"

    scenario(tmp_path, body)


def test_the_owners_message_is_taken_once(tmp_path):
    async def body(world: World):
        session, _, _ = await deferred_session(world)
        first = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "лучше так"})
        again = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "лучше так"})
        assert first["say"] == "Принято." and again["say"] == "Это сообщение сессия уже получила."
        resumes = world.lanes["code"].runs()[1:]
        assert [(r["prompt"], r["cli_session"]) for r in resumes] == [("лучше так", CLI_SESSION)]

    scenario(tmp_path, body)


def test_a_new_message_makes_the_card_stale_and_its_call_is_denied(tmp_path):
    async def body(world: World):
        session, _, card = await deferred_session(world)
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "не надо, сделай иначе"})
        [update] = world.notes("update")
        assert update["approval"] == card["approval"] and update["data"]["buttons"] == []
        assert update["data"]["view"]["status"] == "Устарела: ты написал новое сообщение"
        resume = world.lanes["code"].runs()[-1]
        answer = await world.hook("PreToolUse", resume, pre_tool_use())
        assert decision(answer) == "deny" and answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.STALE
        pressed = await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        assert pressed == {"toast": "Карточка устарела — решение не принято."}
        assert len(world.lanes["code"].runs()) == 2

    scenario(tmp_path, body)


def test_a_rejection_resumes_the_cli_and_its_call_is_denied(tmp_path):
    async def body(world: World):
        session, _, card = await deferred_session(world)
        pressed = await world.owner("press", {"button": card["data"]["buttons"][1][1]})
        assert pressed["view"]["status"] == "Отказано."
        resume = world.lanes["code"].runs()[-1]
        assert resume["prompt"] is None
        answer = await world.hook("PreToolUse", resume, pre_tool_use())
        assert answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.REJECTED

    scenario(tmp_path, body)


def test_runs_wait_for_room_on_their_lane_and_on_the_platform(tmp_path):
    async def body(world: World):
        code = [(await world.owner("new", {"input_id": f"tg:c{i}", "track": "retinue", "text": "x"}))["session"]
                for i in range(3)]
        private = [(await world.owner("new", {"input_id": f"tg:p{i}", "track": "notes", "text": "x"}))["session"]
                   for i in range(2)]
        assert [r["session"] for r in world.lanes["code"].runs()] == code[:2], "two per lane"
        assert [r["session"] for r in world.lanes["private"].runs()] == private[:1], "three in all"
        first = world.lanes["code"].runs()[0]
        world.lanes["code"].exited(code[0], first["run"])
        await world.poll()
        assert [r["session"] for r in world.lanes["code"].runs()] == code, "the lane's queue goes first in, first out"
        assert len(world.lanes["private"].runs()) == 1, "still three running"

    scenario(tmp_path, body, max_sessions=3)


def test_a_lane_that_says_busy_keeps_the_run_queued(tmp_path):
    async def body(world: World):
        world.lanes["private"].answers["/run"] = "busy"
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        assert len(world.store.queued()) == 1 and world.store.open_runs() == []
        world.lanes["private"].answers["/run"] = "started"
        await world.poll()
        assert world.store.queued() == [] and [r["session"] for r in world.store.open_runs()] == [session]

    scenario(tmp_path, body)


def test_an_idle_session_closes_after_seven_days(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        world.lanes["private"].add(session, run["run"], *fixture("reads"))
        await world.poll()
        assert world.store.session(session)["state"] == "idle", "a private session goes idle on its summary"
        world.clock.now += 7 * DAY - 1
        world.platform.tick()
        assert world.store.session(session)["state"] == "idle"
        world.clock.now += 2
        world.platform.tick()
        assert world.store.session(session)["state"] == "closed"

    scenario(tmp_path, body)


def test_an_unpressed_card_expires_after_seven_days(tmp_path):
    async def body(world: World):
        session, _, card = await deferred_session(world)
        world.clock.now += 7 * DAY
        world.platform.tick()
        assert world.store.approval(card["approval"])["state"] == "expired"
        assert world.store.session(session)["state"] == "idle"
        assert world.notes("update")[0]["data"]["view"]["status"].startswith("Истекла")
        assert (await world.owner("press", {"button": card["data"]["buttons"][0][1]}))["toast"] == \
            "Кнопка больше не действует."

    scenario(tmp_path, body)


def test_a_long_input_is_opened_as_a_document_with_its_own_buttons(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        long = {"id": "toolu_long", "name": "Bash", "input": {"command": "echo " + "x" * 5000}}
        await world.hook("PreToolUse", run, pre_tool_use(long))
        result = {"type": "result", "subtype": "success", "stop_reason": "tool_deferred", "deferred_tool_use": long}
        world.lanes["code"].add(session, run["run"], DEFERRED[0], json.dumps(result))
        await world.poll()
        [card] = world.notes("card")
        [(label, button)] = card["data"]["buttons"]
        opened = await world.owner("press", {"button": button})
        assert label == "Открыть" and opened["document"]["text"] == long["input"]["command"]
        assert [k[0] for k in opened["keep"]] == ["Разрешить", "Отказать"]
        [kept] = world.notes("update")
        assert kept["data"]["buttons"] == opened["keep"], "a note journaled before cannot bring «Открыть» back"
        assert (await world.owner("press", {"button": opened["keep"][0][1]}))["toast"] == "Разрешено."

    scenario(tmp_path, body)


def test_a_deferral_while_the_gate_shut_down_is_asked_again(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        world.platform.gate.close()
        assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer"
        world.lanes["code"].add(session, run["run"], *DEFERRED)
        world.lanes["code"].exited(session, run["run"])
        await world.down()
        await world.up()
        await world.poll()
        resume = world.lanes["code"].runs()[-1]
        assert len(world.lanes["code"].runs()) == 2 and resume["prompt"] is None
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "defer"
        await world.poll()
        assert world.notes("card") == [] and world.store.approvals(session=session, states=("pending",))

    scenario(tmp_path, body)


def test_shutdown_stops_every_running_session(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        await world.platform.shutdown()
        assert world.lanes["private"].commands[-1] == ("/stop", {"session": session})

    scenario(tmp_path, body)


def lane_event(event: str, **fields) -> str:
    return json.dumps({"type": "lane", "event": event, **fields})


async def cut_by_a_restart(world: World, *lines: str) -> tuple[str, dict]:
    """A code session whose run has begun its turn (`lines`) when the platform shuts down and comes back up; the lane
    journals the stop it was asked for."""
    session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
    [run] = world.lanes["code"].runs()
    world.lanes["code"].add(session, run["run"], *lines)
    await world.platform.shutdown()
    world.lanes["code"].add(session, run["run"], lane_event("stopped"))
    await world.down()
    await world.up()
    return session, run


def continued(world: World) -> list[dict]:
    return [r for r in world.lanes["code"].runs() if r["prompt"] == platformd.CONTINUE]


def test_a_turn_cut_by_a_platform_restart_is_continued_once_and_the_owner_is_told(tmp_path):
    async def body(world: World):
        session, run = await cut_by_a_restart(world, DEFERRED[0])
        await world.poll()
        assert world.store.session(session)["state"] == "active", "the stop is the platform's, not a pause"
        assert continued(world) == []
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        [resume] = continued(world)
        assert (resume["session"], resume["cli_session"]) == (session, CLI_SESSION)
        [summary] = world.notes("summary")
        assert summary["data"]["text"] == "Ход прервался: платформа перезапускалась. Продолжаю с того места, где " \
                                          "сессия остановилась."
        assert world.store.session(session)["state"] == "active"
        for line in world.lanes["code"].journal:  # a replay, and a lane that comes up again and names the run too
            line["given"] = False
        world.lanes["code"].add(session, run["run"], lane_event("restarted"))
        await world.poll()
        assert len(continued(world)) == 1 and len(world.notes("summary")) == 1

    scenario(tmp_path, body)


def test_a_run_stopped_by_two_shutdowns_is_continued_once(tmp_path):
    async def body(world: World):
        session, run = await cut_by_a_restart(world, DEFERRED[0])
        await world.platform.shutdown()  # down again before the lane's lines were pulled
        assert [path for path, _ in world.lanes["code"].commands].count("/stop") == 2
        world.lanes["code"].add(session, run["run"], lane_event("stopped"))
        await world.down()
        await world.up()
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert len(continued(world)) == 1 and len(world.notes("summary")) == 1

    scenario(tmp_path, body)


def test_the_error_a_shutdowns_interrupt_ends_the_turn_with_is_not_told_as_a_failure(tmp_path):
    async def body(world: World):
        aborted = {"type": "result", "subtype": "error_during_execution", "is_error": True}
        session, run = await cut_by_a_restart(world, DEFERRED[0])
        world.lanes["code"].add(session, run["run"], json.dumps(aborted))
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert len(continued(world)) == 1
        assert [n["data"]["text"] for n in world.notes("summary")] == [
            "Ход прервался: платформа перезапускалась. Продолжаю с того места, где сессия остановилась."]

    scenario(tmp_path, body)


def test_a_session_the_cli_never_named_is_not_continued_but_told(tmp_path):
    async def body(world: World):
        session, run = await cut_by_a_restart(world)
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert continued(world) == [] and world.store.session(session)["state"] == "idle"
        assert [n["data"]["text"] for n in world.notes("summary")] == [
            "Ход прервался: платформа перезапускалась, а сессия ещё не началась. Напиши задачу ещё раз."]

    scenario(tmp_path, body)


def test_a_turn_that_finished_before_the_stop_took_is_not_continued(tmp_path):
    async def body(world: World):
        session, run = await cut_by_a_restart(world, *fixture("reads"))
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert continued(world) == [] and world.store.session(session)["state"] == "closed"
        assert [n["data"]["text"] for n in world.notes("summary")] == ["done"]

    scenario(tmp_path, body)


def test_a_session_the_owner_stopped_or_that_waits_on_a_card_is_not_continued(tmp_path):
    async def body(world: World):
        stopped = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        world.lanes["private"].add(stopped, run["run"], DEFERRED[0], lane_event("stopped"))
        world.lanes["private"].exited(stopped, run["run"])
        await world.poll()
        assert world.store.session(stopped)["state"] == "idle", "a stop that was not the platform's"

        waiting = (await world.owner("new", {"input_id": "tg:2", "track": "retinue", "text": "почини"}))["session"]
        [deferring] = world.lanes["code"].runs()
        assert decision(await world.hook("PreToolUse", deferring, pre_tool_use())) == "defer"
        world.lanes["code"].add(waiting, deferring["run"], *DEFERRED)
        await world.poll()
        await world.platform.shutdown()  # its process has not exited yet
        assert world.store.interrupted(deferring["run"])
        world.lanes["code"].add(waiting, deferring["run"], lane_event("stopped"))
        world.lanes["code"].exited(waiting, deferring["run"])
        await world.down()
        await world.up()
        await world.poll()
        assert world.store.session(waiting)["state"] == "waiting", "it keeps its card"
        assert len(world.lanes["code"].runs()) == 1 and len(world.lanes["private"].runs()) == 1
        assert world.notes("summary") == []

    scenario(tmp_path, body)


def test_a_run_that_ended_before_the_shutdowns_stop_is_not_marked(tmp_path):
    async def body(world: World):
        world.lanes["private"].answers["/stop"] = "not_running"
        await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"})
        [run] = world.lanes["private"].runs()
        await world.platform.shutdown()
        assert not world.store.interrupted(run["run"])

    scenario(tmp_path, body)


def test_each_side_holds_only_its_own_key(tmp_path):
    async def body(world: World):
        await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"})
        [run] = world.lanes["private"].runs()
        hook_token = {"Authorization": f"Bearer {run['hook_token']}"}
        for path in ("/owner/new", "/owner/input", "/owner/press", "/items", "/items/confirm"):
            assert (await world.post(path, {}, hook_token))[0] == 401
            assert (await world.post(path, {}, {"Authorization": "Bearer lane-token"}))[0] == 401
        status, answer = await world.post("/hook/PreToolUse", pre_tool_use(), ROUTER)
        assert status == 200 and decision(answer) == "deny"
        assert world.store.approvals() == []

    scenario(tmp_path, body)


def test_an_unknown_track_is_named_and_none_is_started(tmp_path):
    async def body(world: World):
        answer = await world.owner("new", {"input_id": "tg:1", "track": "nope", "text": "x"})
        assert answer == {"ok": False, "say": "Нет такого трека: nope. Есть: retinue, notes"}
        assert world.lanes["code"].commands == world.lanes["private"].commands == []

    scenario(tmp_path, body)


def test_the_notes_are_the_routers_by_number(tmp_path):
    async def body(world: World):
        await deferred_session(world)
        _, taken = await world.post("/items", {"limit": 10})
        assert [i["kind"] for i in taken["items"]] == ["card"]
        await world.post("/items/confirm", {"upto": taken["items"][-1]["seq"]})
        assert (await world.post("/items", {"limit": 10}))[1]["items"] == []

    scenario(tmp_path, body)


def test_rebuild_projection_keeps_what_was_projected(tmp_path, monkeypatch):
    async def body(world: World):
        session, _, _ = await deferred_session(world)
        return world.cfg, session, world.store.items(session)

    cfg, session, items = scenario(tmp_path, body)
    PlatformStore(cfg.state_db).db.execute("DELETE FROM items").connection.commit()
    monkeypatch.setattr(platformd.PlatformConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("sys.argv", ["retinue-platform", "--rebuild-projection"])
    platformd.main()
    assert PlatformStore(cfg.state_db).items(session) == items


@pytest.mark.parametrize("press, resumed", [(0, "allow"), (1, "deny")])
def test_a_decision_deferred_by_a_shutdown_is_given_after_the_restart(tmp_path, press, resumed):
    """The platform stops between the press and the resumed PreToolUse: the gate defers it, and once the platform is
    back the CLI is resumed again and hears the decision."""
    async def body(world: World):
        session, _, card = await deferred_session(world)
        await world.owner("press", {"button": card["data"]["buttons"][press][1]})
        resume = world.lanes["code"].runs()[-1]
        world.platform.gate.close()
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "defer"
        world.lanes["code"].add(session, resume["run"], *DEFERRED)
        world.lanes["code"].exited(session, resume["run"])
        await world.down()
        await world.up()
        await world.poll()
        runs = world.lanes["code"].runs()
        assert len(runs) == 3 and runs[-1]["prompt"] is None
        assert decision(await world.hook("PreToolUse", runs[-1], pre_tool_use())) == resumed
        assert len(world.notes("card")) == 1

    scenario(tmp_path, body)


async def pull(world: World, lane: str = "code") -> None:
    """One pull of the lane's journal, as the poller makes it while a drain still awaits a `/run` answer."""
    async def waits() -> None:
        pass

    drain, world.platform.drain = world.platform.drain, waits
    try:
        await world.platform.poll(lane)
    finally:
        world.platform.drain = drain


def lose_run_answer(world: World, reached: str | None, *, pulled: bool = False, unrelated: int = 0,
                    loss: BaseException | None = None) -> None:
    """The next `/run` answer is lost (`loss`, a timeout by default); the request reached the lane, which journaled
    `reached` after `unrelated` lines of another session, or not. `pulled`: the poller pulls the journal before the
    loss arrives."""
    client = world.platform.lanes["code"]
    run, lost = client.run, [True]

    async def losing(body: dict) -> dict:
        if not lost[0]:
            return await run(body)
        lost[0] = False
        if reached:
            await run(body)
            for _ in range(unrelated):
                world.lanes["code"].add("other", "other", json.dumps({"type": "lane", "event": "exited"}))
            event = {"type": "lane", "event": reached, "pid": 7} if reached == "started" else \
                {"type": "lane", "event": reached, "error": "CLI не запустился"}
            world.lanes["code"].add(body["session"], body["run"], json.dumps(event))
        if pulled:
            await pull(world)
        raise loss or platformd.Unavailable("линия code: ReadTimeout")
    client.run = losing


def test_a_run_whose_answer_was_lost_but_started_is_not_sent_again(tmp_path):
    async def body(world: World):
        lose_run_answer(world, "started")
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        assert [r["run"] for r in world.store.open_runs()] == [run["run"]], "it holds its room while unknown"
        await world.platform.drain()
        assert len(world.lanes["code"].runs()) == 1
        await world.poll()
        assert world.store.queued() == [] and world.store.session(session)["state"] == "active"
        world.lanes["code"].add(session, run["run"], *fixture("reads"))
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert [r["prompt"] for r in world.lanes["code"].runs()] == ["почини"]
        assert world.store.open_runs() == []

    scenario(tmp_path, body)


def test_a_run_whose_lost_answer_never_reached_the_lane_is_sent_again(tmp_path):
    async def body(world: World):
        lose_run_answer(world, None)
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        assert world.lanes["code"].runs() == [] and len(world.store.open_runs()) == 1
        await world.poll()
        [run] = world.lanes["code"].runs()
        assert (run["session"], run["prompt"]) == (session, "почини")
        assert world.store.queued() == [] and [r["run"] for r in world.store.open_runs()] == [run["run"]]

    scenario(tmp_path, body)


def test_a_lost_answer_the_lane_journaled_as_refused_is_told_and_not_sent_again(tmp_path):
    async def body(world: World):
        lose_run_answer(world, "refused")
        await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"})
        await world.poll()
        assert len(world.lanes["code"].runs()) == 1 and world.store.queued() == []
        [summary] = world.notes("summary")
        assert summary["data"]["text"] == "Линия не запустила сессию: CLI не запустился"

    scenario(tmp_path, body)


def test_a_started_line_pulled_before_the_answer_was_lost_is_not_sent_again(tmp_path):
    async def body(world: World):
        lose_run_answer(world, "started", pulled=True)
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        await world.poll()
        assert len(world.lanes["code"].runs()) == 1 and world.store.queued() == []
        assert [r["run"] for r in world.store.open_runs()] == [run["run"]]
        assert world.store.session(session)["state"] == "active"

    scenario(tmp_path, body)


def test_a_run_still_awaiting_its_answer_is_not_settled_by_a_pull(tmp_path):
    async def body(world: World):
        client = world.platform.lanes["code"]
        run = client.run

        async def slow(body: dict) -> dict:
            await pull(world)  # the lane has not journaled it yet
            return await run(body)
        client.run = slow
        await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"})
        [sent] = world.lanes["code"].runs()
        assert world.store.queued() == [] and [r["run"] for r in world.store.open_runs()] == [sent["run"]]

    scenario(tmp_path, body)


def test_a_refusal_pulled_before_its_answer_is_told_once(tmp_path):
    async def body(world: World):
        client = world.platform.lanes["code"]
        run = client.run

        async def refusing(body: dict) -> dict:
            answer = await run(body)
            world.lanes["code"].add(body["session"], body["run"], json.dumps({"type": "lane", "event": "refused",
                                                                              "error": "CLI не запустился"}))
            await pull(world)
            return answer
        client.run = refusing
        world.lanes["code"].answers["/run"] = "refused"
        await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"})
        assert [n["data"]["text"] for n in world.notes("summary")] == ["Линия не запустила сессию: CLI не запустился"]
        assert world.store.queued() == [] and world.store.open_runs() == []

    scenario(tmp_path, body)


def test_a_full_page_of_the_journal_does_not_settle_a_lost_answer(tmp_path):
    async def body(world: World):
        lose_run_answer(world, "started", unrelated=platformd.POLL_LIMIT)
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        await world.poll()
        assert len(world.lanes["code"].runs()) == 1 and len(world.store.queued()) == 1
        await world.poll()
        assert len(world.lanes["code"].runs()) == 1 and world.store.queued() == []
        assert [r["run"] for r in world.store.open_runs()] == [run["run"]]
        assert world.store.session(session)["state"] == "active"

    scenario(tmp_path, body)


def test_a_run_of_unknown_fate_holds_its_room(tmp_path):
    async def body(world: World):
        world.lanes["code"].answers["/run"] = "busy"
        first, second = [(await world.owner("new", {"input_id": f"tg:{i}", "track": "retinue", "text": "x"}))["session"]
                         for i in range(2)]
        world.lanes["code"].answers["/run"] = "started"
        lose_run_answer(world, None)
        asked = len(world.lanes["code"].runs())
        await world.platform.drain()
        assert len(world.lanes["code"].runs()) == asked, "the unknown run holds the only room"
        assert [q["session"] for q in world.store.queued()] == [first, second]

    scenario(tmp_path, body, max_sessions=1)


@pytest.mark.parametrize("reached", [None, "started"])
def test_a_send_cut_by_a_shutdown_is_settled_after_the_restart(tmp_path, reached):
    async def body(world: World):
        world.lanes["code"].answers["/run"] = "busy"
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        world.lanes["code"].answers["/run"] = "started"
        lose_run_answer(world, reached, loss=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await world.platform.drain()
        await world.down()
        await world.up()
        await world.poll()
        [busy, sent] = world.lanes["code"].runs()
        assert sent["prompt"] == "почини", "sent once, before the shutdown or after it"
        assert world.store.queued() == [] and [r["run"] for r in world.store.open_runs()] == [sent["run"]]
        assert world.store.session(session)["state"] == "active"

    scenario(tmp_path, body)


def test_a_call_deferred_again_while_its_card_is_shown_waits_on_the_owner(tmp_path):
    async def body(world: World):
        session, run, _ = await deferred_session(world)
        world.lanes["code"].add(session, run["run"], DEFERRED[-1])
        await world.poll()
        assert world.store.session(session)["state"] == "waiting"
        assert world.store.queued() == [] and len(world.lanes["code"].runs()) == 1

    scenario(tmp_path, body)


def test_a_failed_turn_is_told_as_a_failure_and_the_code_session_stays_open(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        failed = {"type": "result", "subtype": "error_max_turns", "is_error": True, "stop_reason": None}
        world.lanes["code"].add(session, run["run"], DEFERRED[0], json.dumps(failed))
        await world.poll()
        [summary] = world.notes("summary")
        assert summary["data"]["text"] == "Ход оборвался ошибкой: error_max_turns"
        assert world.store.session(session)["state"] == "idle"

    scenario(tmp_path, body)


def test_a_code_session_closes_on_its_summary(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        world.lanes["code"].add(session, run["run"], *fixture("reads"))
        await world.poll()
        assert world.store.session(session)["state"] == "closed"
        assert [n["data"]["text"] for n in world.notes("summary")] == ["done"]

    scenario(tmp_path, body)


def _later_cli_line(world: World, session: str, run: str) -> None:
    world.lanes["code"].add(session, run, DEFERRED[1])


def _another_call_deferred(world: World, session: str, run: str) -> None:
    other = {**json.loads(DEFERRED[-1]), "deferred_tool_use": {**CALL, "id": "toolu_other"}}
    world.store.add_raw("code", 999, session, run, json.dumps(other), world.clock.now + 1)


def _owner_wrote_since(world: World, session: str, run: str) -> None:
    world.store.db.execute("INSERT INTO inputs (input_id, session, text, at) VALUES ('tg:2', ?, 'x', ?)", (session, world.clock.now + 1))
    world.store.db.commit()


@pytest.mark.parametrize("since", [_later_cli_line, _another_call_deferred, _owner_wrote_since])
def test_a_card_is_pressable_only_while_its_call_is_what_the_session_waits_on(tmp_path, since):
    async def body(world: World):
        session, run, card = await deferred_session(world)
        since(world, session, run["run"])
        await world.poll()
        pressed = await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        assert pressed == {"toast": "Карточка устарела — решение не принято."}
        assert len(world.lanes["code"].runs()) == 1
        assert world.store.approval(card["approval"])["state"] == "shown"

    scenario(tmp_path, body)


@pytest.mark.parametrize("event", ["stopped", "restarted"])
def test_a_run_stopped_after_its_deferral_keeps_the_card_pressable(tmp_path, event):
    async def body(world: World):
        session, run, card = await deferred_session(world)
        world.lanes["code"].add(session, run["run"], json.dumps({"type": "lane", "event": event}))
        await world.poll()
        assert world.store.session(session)["state"] == "waiting"
        assert (await world.owner("press", {"button": card["data"]["buttons"][0][1]}))["toast"] == "Разрешено."
        assert world.store.approval(card["approval"])["state"] == "granted"
        assert len(world.lanes["code"].runs()) == 2

    scenario(tmp_path, body)


def test_the_room_with_the_web_on_still_gets_the_repository_rules_and_the_owners_skills(tmp_path):
    """`web: true` means the CLI may use the web, nothing more: the room keeps its rules, skills and memory (the lane
    host reads them unless its own RETINUE_LANE_WEB says it is the retired lane-web)."""
    async def body(world: World):
        world.cfg.lanes["private"].web = True
        await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"})
        [run] = world.lanes["private"].runs()
        assert run["instructions"] is True
        assert "WebFetch" not in run["disallowed"] and "WebSearch" not in run["disallowed"]

    scenario(tmp_path, body)


def test_a_session_whose_lane_left_the_config_is_refused_and_blocks_no_one(tmp_path):
    async def body(world: World):
        running = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        queued = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "y"}))["session"]
        assert [q["session"] for q in world.store.queued()] == [queued], "the open run holds the only room"
        await world.down()
        world.cfg = dataclasses.replace(world.cfg, lanes={"code": world.cfg.lanes["code"]})
        await world.up()
        fix = (await world.owner("new", {"input_id": "tg:3", "track": "retinue", "text": "почини"}))["session"]
        assert [r["session"] for r in world.lanes["code"].runs()] == [fix], "the stranded run holds no room"
        assert world.store.queued() == []
        assert await world.owner("input", {"input_id": "tg:4", "session": running, "text": "z"}) == \
            {"say": platformd.RETIRED}, "a retired lane's session takes nothing in"
        assert [n["data"]["text"] for n in world.notes("summary")] == \
            ["Линия не запустила сессию: линии private больше нет в настройках"]
        await world.platform.shutdown()
        assert world.lanes["code"].commands[-1] == ("/stop", {"session": fix})
        assert len(world.lanes["private"].commands) == 1

    scenario(tmp_path, body, max_sessions=1)


def test_a_retired_lane_is_never_run_its_stuck_run_holds_no_room_and_its_cards_go_stale_at_start(tmp_path):
    """A lane still named in platform.yaml with `retired: true`: no client, no poller; a run it never answered holds
    no room; its waiting session's card goes stale at start, so it leaves «Решить»."""
    async def body(world: World):
        session, _, card = await deferred_session(world)
        world.lanes["code"].answers["/run"] = "busy"  # never mind: the lane goes away
        await world.down()
        lanes = dict(world.cfg.lanes)
        lanes["code"] = dataclasses.replace(lanes["code"], retired=True)
        world.cfg = dataclasses.replace(world.cfg, lanes=lanes, max_sessions=1)
        await world.up()
        assert "code" not in world.platform.lanes and "code" in world.platform.gate.lanes
        world.platform.retire_cards()
        approval = world.store.approval(card["approval"])
        assert approval["state"] == "stale" and world.store.session(session)["state"] == "idle"
        assert world.notes("update")[-1]["data"]["view"]["status"] == "Устарела: полоса выключена"
        assert (await world.owner("input", {"input_id": "tg:9", "session": session, "text": "ау"})) == \
            {"say": platformd.RETIRED}
        world.store.add_run(session, "stuck", [], world.clock.now)  # a run the lane never answered
        notes = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "y"}))["session"]
        assert [r["session"] for r in world.lanes["private"].runs()] == [notes], "the stuck run holds no room"
        late = await world.owner("new", {"input_id": "tg:3", "track": "retinue", "text": "z"})
        assert world.store.session(late["session"])["state"] == "idle"
        assert world.notes("summary")[-1]["data"]["text"] == \
            "Линия не запустила сессию: полоса code выключена"

    scenario(tmp_path, body)


def test_the_clock_prunes_raw_lines_after_ninety_days_and_keeps_the_projection(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        world.lanes["private"].add(session, run["run"], *fixture("reads"))
        await world.poll()
        projected, raw = world.store.items(session), world.store.db.execute("SELECT COUNT(*) FROM raw").fetchone()
        world.clock.now += sessions.RAW_KEEP_S
        world.platform.tick()
        assert world.store.db.execute("SELECT COUNT(*) FROM raw").fetchone() == raw
        world.clock.now += 1
        world.platform.tick()
        assert world.store.db.execute("SELECT COUNT(*) FROM raw").fetchone() == (0,)
        assert world.store.items(session) == projected

    scenario(tmp_path, body)


def test_a_deferred_call_whose_tool_is_gone_expires_its_grant(tmp_path):
    async def body(world: World):
        session, _, card = await deferred_session(world)
        await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        resume = world.lanes["code"].runs()[-1]
        gone = {"type": "result", "subtype": "success", "stop_reason": "tool_deferred_unavailable"}
        world.lanes["code"].add(session, resume["run"], json.dumps(gone))
        await world.poll()
        assert world.store.approval(card["approval"])["state"] == "expired"
        assert world.store.session(session)["state"] == "idle"
        [summary] = world.notes("summary")
        assert summary["data"]["text"].startswith("Отложенный вызов продолжить нельзя")

    scenario(tmp_path, body)


def test_a_message_to_a_running_session_goes_into_its_run(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        answer = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "и тесты"})
        assert answer == {"say": "Передал в сессию."}
        assert world.lanes["code"].commands[-1] == ("/answer", {"session": session, "prompt": "и тесты"})
        assert len(world.lanes["code"].runs()) == 1 and world.store.queued() == []

    scenario(tmp_path, body)


def test_a_session_runs_one_queued_entry_at_a_time(tmp_path):
    async def body(world: World):
        world.lanes["private"].answers["/run"] = "busy"
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "y"})
        assert [q["prompt"] for q in world.store.queued()] == ["x", "y"]
        tried = len(world.lanes["private"].runs())
        world.lanes["private"].answers["/run"] = "started"
        await world.poll()
        assert [r["prompt"] for r in world.lanes["private"].runs()[tried:]] == ["x"]
        assert [q["prompt"] for q in world.store.queued()] == ["y"]

    scenario(tmp_path, body)


def test_a_turn_cut_by_the_lanes_own_shutdown_is_continued_once_while_the_platform_runs(tmp_path):
    """An update recreates the lanes before the platform: the lane's SIGTERM stops the run, and its `stopped` says
    so; no shutdown of the platform's marked it."""
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        world.lanes["code"].add(session, run["run"], DEFERRED[0], lane_event("stopped", by="shutdown"))
        await world.poll()
        assert world.store.session(session)["state"] == "active" and continued(world) == []
        world.lanes["code"].exited(session, run["run"])
        world.lanes["code"].add(session, run["run"], lane_event("restarted", pid=7))
        await world.poll()
        [resume] = continued(world)
        assert (resume["session"], resume["cli_session"]) == (session, CLI_SESSION)
        assert len(world.notes("summary")) == 1

    scenario(tmp_path, body)


def test_a_message_the_owner_wrote_while_the_platform_was_down_goes_first_and_alone(tmp_path):
    async def body(world: World):
        session, run = await cut_by_a_restart(world, DEFERRED[0])
        world.lanes["code"].answers["/answer"] = "not_running"
        said = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "лучше сделай иначе"})
        assert said["say"] == "Принято — сессия ждёт места на линии."
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        assert continued(world) == [] and world.lanes["code"].runs()[-1]["prompt"] == "лучше сделай иначе"
        assert world.store.queued() == []
        assert [n["data"]["text"] for n in world.notes("summary")] == [
            "Ход прервался: платформа перезапускалась. Сессия продолжит с твоего нового сообщения."]

    scenario(tmp_path, body)


def test_a_hung_lane_does_not_hold_the_platforms_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(platformd, "STOP_TIMEOUT_S", 0.2)

    async def body(world: World):
        await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"})
        await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "x"})
        [hung], [answering] = world.lanes["code"].runs(), world.lanes["private"].runs()
        world.lanes["code"].delays["/stop"] = 0.6
        world.lanes["private"].delays["/stop"] = 0.15
        started = asyncio.get_running_loop().time()
        await world.platform.shutdown()
        assert asyncio.get_running_loop().time() - started < 0.5, "both asked at once, the hung one not waited for"
        assert world.store.interrupted(answering["run"]) and not world.store.interrupted(hung["run"])

    scenario(tmp_path, body)


def assistant_usage(tokens: int) -> str:
    """A top-level assistant line carrying its request's usage, as the CLI prints one per content block."""
    return json.dumps({"type": "assistant", "parent_tool_use_id": None, "session_id": CLI_SESSION,
                       "message": {"id": "msg_x", "role": "assistant", "content": [{"type": "text", "text": "…"}],
                                   "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1000,
                                             "cache_read_input_tokens": tokens - 1002, "output_tokens": 50}}})


def test_a_card_left_past_the_prompt_cache_says_what_pressing_it_costs_and_keeps_its_buttons(tmp_path):
    """55 minutes after the session stopped, its card gets the idle line in Telegram and in the face — with the
    context its last request carried — once, then as the hours change; its age, expiry and buttons stay."""
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer"
        world.lanes["code"].add(session, run["run"], assistant_usage(183_400), *DEFERRED)
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        [card] = world.notes("card")
        shown = world.store.approval(card["approval"])
        assert world.store.session(session)["context_tokens"] == 10, "the deferred turn's own last request counts"

        world.clock.now += 54 * 60
        world.platform.tick()
        assert world.notes("update") == [], "the cache still holds"
        world.clock.now += 60
        world.platform.tick()
        world.platform.tick()
        [update] = world.notes("update")
        head = update["data"]["view"]["head"]
        assert head[-2] == "Сессия простаивает 55 мин — продолжение перечитает контекст без кэша", \
            "10 tokens: no number"
        assert head[-1].startswith("Ввод") and update["approval"] == card["approval"]
        assert update["data"]["buttons"] == card["data"]["buttons"], "the card still asks"
        now = world.store.approval(card["approval"])
        assert now["view"]["head"] == head and (now["state"], now["at"]) == ("shown", shown["at"])

        world.store.set_context(session, 183_400)
        world.clock.now += HOUR + 5 * 60
        world.platform.tick()
        assert world.notes("update")[-1]["data"]["view"]["head"][-2] == \
            "Сессия простаивает 2 ч — продолжение перечитает контекст (~183 тыс. токенов) без кэша"
        assert len(world.notes("update")) == 2
        approve = card["data"]["buttons"][0][1]
        assert (await world.owner("press", {"button": approve}))["toast"] == "Разрешено."
        decided = world.notes("update")[-1]["data"]
        assert decided["buttons"] == [] and decided["view"]["status"] == "Разрешено — выполняю."
        assert not any(h.startswith("Сессия простаивает") for h in decided["view"]["head"]), "moot once decided"
        world.clock.now += HOUR
        world.platform.tick()
        assert len(world.notes("update")) == 3, "a decided card is not rewritten"

    scenario(tmp_path, body)


def test_a_run_waiting_for_the_limits_reset_tells_the_owner_once_in_his_time(tmp_path):
    async def body(world: World):
        world.cfg.owner_tz = "Europe/Berlin"
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 300, "error_status": 429,
                 "error": "rate_limit", "session_id": CLI_SESSION}
        wait = 2 * HOUR + 30 * 60
        world.lanes["private"].add(session, run["run"], json.dumps({**retry, "retry_delay_ms": 30_000}),
                                   json.dumps({**retry, "retry_delay_ms": wait * 1000}))
        await world.poll()
        world.clock.now += 30
        world.lanes["private"].add(session, run["run"], json.dumps({**retry, "retry_delay_ms": (wait - 30) * 1000}))
        await world.poll()
        [said] = world.notes("summary")
        # 1_000_000 s is 1970-01-12 13:46:40 UTC; the reset 2 h 30 min later, in Berlin (UTC+1 in January)
        assert said["data"]["text"] == "Лимит подписки — жду сброса до 17:16"
        assert world.store.session(session)["state"] == "active", "the run goes on waiting"

    scenario(tmp_path, body)


def test_a_side_question_is_a_session_of_its_own_that_only_reads_the_main_one(tmp_path):
    async def body(world: World):
        session, _, _ = await deferred_session(world)
        inputs = world.store.db.execute("SELECT COUNT(*) FROM inputs").fetchone()[0]
        answer = await world.platform.side("face:q1", session, "что ты сейчас делаешь?")
        side = answer["session"]
        assert answer["ok"] and side != session
        row = world.store.session(side)
        assert (row["parent"], row["track"], row["lane"]) == (session, "retinue", "code")
        assert world.store.face_session(side)["title"] == "что ты сейчас делаешь?", "titled by the question"
        runs = world.lanes["code"].runs()
        prompt = runs[-1]["prompt"]
        assert runs[-1]["session"] == side and runs[-1]["cli_session"] is None, "a new conversation, not a resume"
        assert prompt.startswith(f"Это побочный вопрос к сессии {session} (трек retinue). Вопрос: что ты сейчас "
                                 "делаешь?")
        assert "\nЗадача сессии: почини\nВызов Bash: echo ran ›› ran.txt\nХод остановлен: вызов ждёт решения " \
            "владельца." in prompt and prompt.endswith("Вопрос ещё раз: что ты сейчас делаешь?")
        side_run = runs[-1]
        assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} <= set(side_run["disallowed"]), "read-only"
        assert not {"Edit", "Bash"} & set(runs[0]["disallowed"]), "the main session keeps its tools"
        marked = {h["url"].rsplit("/", 1)[-1]: (e["matcher"], h.get("if"))
                  for e in side_run["settings"]["hooks"]["PreToolUse"] for h in e["hooks"]}
        assert ("Bash", None) in marked.values() and ("Edit", None) in marked.values()
        assert world.store.db.execute("SELECT COUNT(*) FROM inputs WHERE session = ?", (session,)).fetchone()[0] == \
            inputs, "nothing written into the main session"
        assert world.store.session(session)["state"] == "waiting", "its card still stands"
        again = await world.platform.side("face:q1", session, "что ты сейчас делаешь?")
        assert again["session"] == side and len(world.lanes["code"].runs()) == len(runs), "a retried POST is one"
        nested = await world.platform.side("face:q2", side, "а подробнее?")
        assert world.store.session(nested["session"])["parent"] == session, "one level: under the main session"
        assert (await world.platform.side("face:q3", "nope0000", "?"))["ok"] is False

        world.lanes["code"].add(side, runs[-1]["run"], *fixture("reads"))
        await world.poll()
        [*_, summary] = world.notes("summary")
        assert summary["data"]["head"] == f"Итог · retinue · сессия {side} · вопрос к {session}"
        assert summary["data"]["turn"] == 0

    scenario(tmp_path, body)


def test_an_idle_rewrite_still_on_its_way_never_outlives_a_telegram_press(tmp_path):
    """The race: the clock journals the idle line with the card's buttons; before the router delivers it, the owner
    presses in Telegram, which edits the card directly. The decision is journaled after the idle note, so the card
    ends decided, without buttons, whatever order the router sees the press and the notes in."""
    async def body(world: World):
        session, _, card = await deferred_session(world)
        world.clock.now += HOUR
        world.platform.tick()
        _, pending = await world.post("/items", {"limit": 10})  # the router has not delivered it yet
        assert [n["kind"] for n in pending["items"]] == ["card", "update"]
        assert pending["items"][1]["data"]["buttons"] == card["data"]["buttons"]
        assert (await world.owner("press", {"button": card["data"]["buttons"][1][1]}))["toast"] == "Отказано."
        _, pending = await world.post("/items", {"limit": 10})
        last = [n for n in pending["items"] if n["approval"] == card["approval"]][-1]
        assert last["kind"] == "update" and last["data"]["buttons"] == []
        assert last["data"]["view"]["status"] == "Отказано." and \
            not any(h.startswith("Сессия простаивает") for h in last["data"]["view"]["head"])

    scenario(tmp_path, body)


def test_a_malformed_retry_line_never_breaks_the_ingest(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        base = ('{"type": "system", "subtype": "api_retry", "error_status": 429, "error": "rate_limit", '
                '"retry_delay_ms": ')
        world.lanes["private"].add(session, run["run"], base + "Infinity}", base + "NaN}", base + "-Infinity}",
                                   base + "1e308}", *fixture("reads"))
        await world.poll()
        [limit, done] = world.notes("summary")
        assert limit["data"]["text"].startswith("Лимит подписки — жду сброса до ")
        assert limit["data"]["limit_until"] == world.clock.now + 6 * HOUR, "the huge one, clamped"
        assert world.store.session(session)["state"] == "idle", "every line after them still ingested"

    scenario(tmp_path, body)


# --- the owner's attachments ----------------------------------------------------------------------------------------

def _wire(name: str, data: bytes, media_type: str = "") -> dict:
    return {"name": name, "media_type": media_type, "data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest()}


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (0, 90, 200)).save(out, "PNG")
    return out.getvalue()


def test_a_message_with_a_photo_and_a_file_goes_to_the_lane_as_blocks_and_inbox_files_and_waits_queued(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "",
                                             "attachments": [_wire("", _png(), "image/jpeg"),
                                                             _wire("logs.txt", b"boom")]}))["session"]
        [run] = world.lanes["private"].runs()
        assert run["prompt"][0] == {"type": "text", "text": "Приложено: фото 40×20; файл «logs.txt» (в папке "
                                                            "inbox/, путь ниже)."}
        assert run["prompt"][1]["type"] == "image"
        assert run["files"] == [{"name": "logs.txt", "data": base64.b64encode(b"boom").decode(),
                                 "sha256": hashlib.sha256(b"boom").hexdigest()}]
        assert run["date"] == "1970-01-12", "the owner's own day, in owner_tz"
        world.lanes["private"].add(session, run["run"], json.dumps({"type": "result", "subtype": "success",
                                                                    "result": "ok"}))
        world.lanes["private"].exited(session, run["run"])
        await world.poll()

        world.lanes["private"].answers["/run"] = "busy"
        said = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "и это",
                                           "attachments": [_wire("b.md", b"# b")]})
        assert said == {"say": "Принято — сессия ждёт места на линии."}
        [queued] = world.store.queued()
        assert queued["prompt"] == "и это" and "content" not in queued
        assert world.store.queued_content(queued["seq"])["files"][0]["name"] == "b.md"
        await world.down()  # the queue keeps the attachments across a restart
        await world.up()
        world.lanes["private"].answers["/run"] = "started"
        await world.platform.drain()
        last = world.lanes["private"].runs()[-1]
        assert last["prompt"][0]["text"].startswith("и это\n\nПриложено: файл «b.md»") and last["files"]

        [first, second] = world.store.face_inputs(session)
        assert [a["kind"] for a in first["attachments"]] == ["image", "file"] and first["attachments"][0]["thumb"]
        assert world.store.input_thumb(session, "tg:1", 0)[:2] == b"\xff\xd8"
        assert world.store.input_thumb(session, "tg:1", 1) is None and world.store.input_thumb("x", "tg:1", 0) is None
        assert (second["text"], second["turn"]) == ("и это", 1)

    scenario(tmp_path, body)


def test_attachments_to_a_live_run_go_with_its_answer(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        said = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "вот лог",
                                           "attachments": [_wire("ci.log", b"FAIL")]})
        assert said == {"say": "Передал в сессию."}
        path, sent = world.lanes["code"].commands[-1]
        assert path == "/answer" and set(sent) == {"session", "prompt", "files", "date"}
        assert sent["prompt"][0]["text"] == "вот лог\n\nПриложено: файл «ci.log» (в папке inbox/, путь ниже)."

    scenario(tmp_path, body)


def test_what_cannot_be_read_is_said_and_nothing_or_the_rest_goes_in(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": ""}))["session"]
        broken = _wire("x.png", b"\x89PNG broken", "image/png")
        said = await world.owner("input", {"input_id": "tg:2", "session": session, "attachments": [broken]})
        assert said == {"say": "Не передано в сессию: картинка «x.png»: картинку не удалось открыть."}
        assert world.lanes["private"].runs() == [] and world.store.input_session("tg:2") is None
        said = await world.owner("input", {"input_id": "tg:3", "session": session, "text": "всё равно",
                                           "attachments": [broken]})
        assert said == {"say": "Принято.\nНе передано в сессию: картинка «x.png»: картинку не удалось открыть."}
        assert world.lanes["private"].runs()[-1]["prompt"] == "всё равно", "text alone, as before"
        torn = {**_wire("a.txt", b"a"), "sha256": "0" * 64}
        said = await world.owner("input", {"input_id": "tg:4", "session": session, "attachments": [torn]})
        assert said == {"say": "Не передано в сессию: «a.txt»: пришло не целым — sha256 не сходится."}
        again = await world.owner("input", {"input_id": "tg:3", "session": session, "attachments": [_wire("b", b"b")]})
        assert again == {"say": "Это сообщение сессия уже получила."}
        status, _ = await world.post("/owner/input", {"input_id": "tg:5", "session": session, "text": ""})
        assert status == 400, "neither text nor attachments"

    scenario(tmp_path, body)


def test_a_hook_body_is_held_to_its_old_limit_while_the_owner_may_send_files(tmp_path, monkeypatch):
    monkeypatch.setattr(platformd, "HOOK_BODY_MAX", 1000)

    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        auth = {"Authorization": f"Bearer {run['hook_token']}"}
        big = {**pre_tool_use(), "tool_input": {"command": "x" * 2000}}
        assert (await world.post("/hook/PreToolUse", big, auth))[0] == 413
        assert (await world.post("/hook/mark/0", big, auth))[0] == 413
        assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer", "a hook in size still works"
        said = await world.owner("input", {"input_id": "tg:2", "session": session, "text": "лог",
                                           "attachments": [_wire("big.log", b"y" * 5000)]})
        assert said == {"say": "Передал в сессию."}

    scenario(tmp_path, body)


def test_a_new_session_sent_twice_while_its_picture_is_read_is_one_session(tmp_path):
    async def body(world: World):
        new = {"input_id": "tg:1", "track": "notes", "text": "", "attachments": [_wire("", _png(), "image/jpeg")]}
        first, second = await asyncio.gather(world.owner("new", new), world.owner("new", new))
        assert first["session"] == second["session"]
        assert world.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert len(world.lanes["private"].runs()) == 1

    scenario(tmp_path, body)
