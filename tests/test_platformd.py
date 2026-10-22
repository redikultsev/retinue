"""The platform against fake lane hosts: aiohttp apps in this process that record every command and serve a scripted
journal (the transcript fixtures). Hooks and the owner's calls go over HTTP, as a lane CLI and the router send them.
A platform is torn down and built again from the same files where the scenario is a restart."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from retinue import gate, platformd
from retinue.config import LaneRef, PlatformConfig, Track
from retinue.platformd import Platform
from retinue.sessions import PlatformStore

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
ROUTER = {"Authorization": "Bearer router-token"}
HOUR, DAY = 3600, 86400


def fixture(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text().splitlines()


DEFERRED = fixture("deferred")
CALL = json.loads(DEFERRED[-1])["deferred_tool_use"]  # Bash `echo ran >> ran.txt`, as the CLI deferred it
CLI_SESSION = json.loads(DEFERRED[0])["session_id"]


class FakeLane:
    """A lane host's surface: the commands it got, its journal, and what it answers."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []
        self.journal: list[dict] = []
        self.answers = {"/run": "started", "/answer": "sent", "/stop": "stopping"}
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
