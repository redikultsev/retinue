"""The lane host against a fake `claude`: a script that prints scripted stream-json lines and records its argv, its
environment, its settings file and what came on its stdin — so what reaches the CLI is seen exactly as it was built."""

import asyncio
import json
import re
import signal
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from retinue import cliengine, lane_host
from retinue.config import LaneHostConfig

TOKEN = "lane-token"
SIGNED = {"Authorization": f"Bearer {TOKEN}"}
HOOK_URL = "http://platform:9500/hook"
CLI_SESSION = "68c88c62-f9c6-44e3-bbba-6e45dfa967a2"
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": CLI_SESSION, "claude_code_version": "2.1.304"})

FAKE = """#!{python}
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(here, "plan.json")))
args = sys.argv[1:]
if args == ["--version"]:
    print(plan.get("version", "2.1.304 (Claude Code)"))
    sys.exit(0)
if args == ["auth", "status", "--json"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "oauth_token"}}))
    sys.exit(0)
if plan.get("ignore_int"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
settings = args[args.index("--settings") + 1]
run = os.path.basename(settings)[:-len(".json")]
with open(os.path.join(here, f"call-{{run}}.json"), "w") as f:
    json.dump({{"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "settings": json.load(open(settings)),
               "mode": os.stat(settings).st_mode & 0o777}}, f)
def out(line):
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()
result = json.dumps({{"type": "result", "subtype": "success", "result": "готово"}})
for line in plan.get("lines", []):
    out(line)
inputs = 0
try:
    for raw in sys.stdin:
        inputs += 1
        with open(os.path.join(here, f"stdin-{{run}}.jsonl"), "a") as f:
            f.write(raw)
        if inputs == plan.get("result_after", 1):
            out(result)
    if not inputs:
        out(result)
    time.sleep(plan.get("linger", 0))
except KeyboardInterrupt:
    sys.exit(130)
sys.exit(plan.get("exit", 0))
"""


class Lane:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "cli"
        self.work = tmp_path / "work"
        (self.work / "repo").mkdir(parents=True)
        self.dir.mkdir()
        self.cfg = LaneHostConfig(token=TOKEN, oauth_token="oauth", hook_url=HOOK_URL, proxy="http://egress:3128",
                                  no_proxy="platform", max_running=2, work=str(self.work),
                                  home=str(tmp_path / "config"), config_dir=str(tmp_path / "config" / "claude"),
                                  state=str(tmp_path / "state"), run_dir=str(tmp_path / "run"))
        Path(self.cfg.run_dir).mkdir()
        self.plan()

    def plan(self, **plan) -> None:
        (self.dir / "plan.json").write_text(json.dumps({"lines": [INIT], **plan}))

    def call(self, run: str) -> dict | None:
        path = self.dir / f"call-{run}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def stdin(self, run: str) -> list[str]:
        path = self.dir / f"stdin-{run}.jsonl"
        return path.read_text().splitlines(keepends=True) if path.exists() else []

    def host(self) -> lane_host.LaneHost:
        return lane_host.LaneHost(self.cfg, lane_host.Journal(f"{self.cfg.state}/journal.sqlite"))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    lane = Lane(tmp_path)
    fake = lane.dir / "claude"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("RETINUE_LANE_CLI", str(fake))
    monkeypatch.setattr(cliengine, "SETTINGS_PATH", re.compile(re.escape(lane.cfg.run_dir) + r"/[A-Za-z0-9_-]{1,64}\.json"))
    return lane


def run_body(session="0a1b2c3d", run="r1", prompt="Почини тест", **over) -> dict:
    return {"session": session, "run": run, "cli_session": None, "cwd": "repo",
            "settings": cliengine.settings(HOOK_URL, 30, ["Bash(npm test:*)"]), "allowed": ["Read", "Bash(npm test:*)"],
            "disallowed": ["WebFetch", "Bash(git push:*)"], "prompt": prompt, "hook_token": "hook-1", **over}


def served(lane: Lane, scenario, host: lane_host.LaneHost | None = None):
    async def go():
        nonlocal host
        host = host or lane.host()
        await host.start()
        client = TestClient(TestServer(host.app()))
        await client.start_server()

        async def post(path, body, headers=SIGNED):
            response = await client.post(path, json=body, headers=headers)
            return response.status, await response.json()

        async def items():
            return [{**i, "data": json.loads(i["line"])} for i in (await post("/items", {"limit": 200}))[1]["items"]]

        async def until(check, timeout=10.0):
            for _ in range(int(timeout / 0.02)):
                found = check()
                if found := (await found if asyncio.iscoroutine(found) else found):
                    return found
                await asyncio.sleep(0.02)
            raise AssertionError("timed out")

        async def event(name, session="0a1b2c3d"):
            async def found():
                return [i for i in await items() if i["session"] == session and i["data"].get("event") == name]
            return await until(found)

        try:
            return await scenario(host, client, post, items, until, event)
        finally:
            for live in list(host.live.values()):
                live.proc.kill()
            await asyncio.gather(*host.tasks, return_exceptions=True)
            await client.close()

    return asyncio.run(go())


def test_a_run_gets_exactly_the_argv_env_and_settings_built_for_it_and_its_prompt_only_on_stdin(lane):
    body = run_body(cli_session=CLI_SESSION)

    async def scenario(host, client, post, items, until, event):
        assert await post("/run", body) == (200, {"status": "started"})
        await event("exited")
        return await items()

    journal = served(lane, scenario)
    settings = f"{lane.cfg.run_dir}/r1.json"
    call = lane.call("r1")
    assert call["argv"] == cliengine.argv(settings, body["allowed"], body["disallowed"], CLI_SESSION)[1:]
    assert call["env"] == cliengine.env("oauth", lane.cfg.home, lane.cfg.config_dir, "http://egress:3128", "platform",
                                        "hook-1")
    assert call["cwd"] == str(lane.work / "repo")
    assert call["settings"] == body["settings"] and call["mode"] == 0o444
    assert lane.stdin("r1") == [cliengine.user_line("Почини тест").decode()]
    assert not any("Почини" in arg for arg in call["argv"])
    assert not Path(settings).exists()  # the run's settings file goes with its process
    assert [i["data"].get("event") or i["data"]["type"] for i in journal] == ["started", "system", "result", "exited"]
    assert {(i["session"], i["run"]) for i in journal} == {("0a1b2c3d", "r1")}
    assert journal[-1]["data"]["code"] == 0 and journal[1]["line"] == INIT


def test_a_resume_with_no_prompt_gets_its_stdin_closed_at_once(lane):
    async def scenario(host, client, post, items, until, event):
        assert (await post("/run", run_body(prompt=None, cli_session=CLI_SESSION)))[1] == {"status": "started"}
        await event("exited")
        return await items()

    journal = served(lane, scenario)
    assert lane.stdin("r1") == [] and lane.call("r1")["argv"][-2:] == ["--resume", CLI_SESSION]
    assert [i["data"]["type"] for i in journal][1:3] == ["system", "result"]


def test_the_journal_is_given_in_order_until_confirmed_and_a_long_line_only_by_its_length(lane, monkeypatch):
    monkeypatch.setattr(lane_host, "LINE_CAP", 100)
    lane.plan(lines=[INIT, json.dumps({"type": "assistant", "text": "x" * 500}), json.dumps({"type": "assistant"})])

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body())
        await event("exited")
        first = await items()
        confirmed = await post("/items/confirm", {"upto": first[2]["seq"]})
        return first, confirmed, await items()

    first, confirmed, rest = served(lane, scenario)
    assert [i["seq"] for i in first] == sorted(i["seq"] for i in first)
    assert first[2]["data"] == {"type": "lane", "event": "truncated", "bytes": len(json.dumps({"type": "assistant",
                                                                                                "text": "x" * 500}))}
    assert confirmed == (200, {"confirmed": 3}) and rest == first[3:]

    async def again(host, client, post, items, until, event):  # the platform reconnects to a new host process
        return await items()

    assert served(lane, again) == rest


def test_two_live_processes_fill_the_lane_and_an_answer_reaches_a_live_ones_stdin(lane):
    lane.plan(result_after=2)

    async def scenario(host, client, post, items, until, event):
        started = [(await post("/run", run_body(session=s, run=f"r{s[0]}")))[1]["status"]
                   for s in ("1aaaaaaa", "2bbbbbbb")]
        again = (await post("/run", run_body(session="1aaaaaaa", run="r9")))[1]
        third = (await post("/run", run_body(session="3ccccccc", run="r3")))[1]
        answer = await post("/answer", {"session": "1aaaaaaa", "prompt": "И ещё README"})
        await event("exited", "1aaaaaaa")
        after = await post("/answer", {"session": "1aaaaaaa", "prompt": "поздно"})
        return started, again, third, answer, after

    started, again, third, answer, after = served(lane, scenario)
    assert started == ["started", "started"] and again == {"status": "running"} and third == {"status": "busy"}
    assert lane.call("r3") is None and lane.call("r9") is None
    assert answer == (200, {"status": "sent"}) and after == (200, {"status": "not_running"})
    assert lane.stdin("r1") == [cliengine.user_line(t).decode() for t in ("Почини тест", "И ещё README")]


def test_an_answer_with_no_process_or_a_closed_stdin_is_not_running(lane):
    lane.plan(linger=30)

    async def scenario(host, client, post, items, until, event):
        nobody = await post("/answer", {"session": "0a1b2c3d", "prompt": "привет"})
        await post("/run", run_body())
        await until(lambda: _results(items))
        closed = await post("/answer", {"session": "0a1b2c3d", "prompt": "привет"})
        return nobody, closed, sorted(host.live)

    nobody, closed, live = served(lane, scenario)
    assert nobody == closed == (200, {"status": "not_running"}) and live == ["0a1b2c3d"]


async def _results(items):
    return [i for i in await items() if i["data"]["type"] == "result"]


@pytest.mark.parametrize("ignore_int, code", [(False, 130), (True, -signal.SIGTERM)])
def test_stop_interrupts_and_terminates_a_process_that_does_not_go(lane, monkeypatch, ignore_int, code):
    monkeypatch.setattr(lane_host, "STOP_GRACE_S", 0.3)
    lane.plan(result_after=99, ignore_int=ignore_int)

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body())
        await until(lambda: lane.stdin("r1"))
        stopping = await post("/stop", {"session": "0a1b2c3d"})
        await event("exited")
        return stopping, await post("/stop", {"session": "0a1b2c3d"}), await items()

    stopping, again, journal = served(lane, scenario)
    assert stopping == (200, {"status": "stopping"}) and again == (200, {"status": "not_running"})
    assert [i["data"].get("event") for i in journal if i["data"]["type"] == "lane"] == ["started", "stopped", "exited"]
    assert journal[-1]["data"]["code"] == code


def test_a_host_that_comes_up_again_journals_the_runs_left_running_as_restarted(lane):
    lane.plan(result_after=99)
    left = []

    async def first(host, client, post, items, until, event):
        await post("/run", run_body())
        left.append(host.live["0a1b2c3d"].proc.pid)

    served(lane, first, host := lane.host())
    host.journal.db.execute("INSERT INTO running VALUES ('1aaaaaaa', 'r7', 4242)")  # the host died, these remained
    host.journal.db.execute("INSERT OR REPLACE INTO running VALUES ('0a1b2c3d', 'r1', ?)", (left[0],))
    host.journal.db.commit()

    async def second(host, client, post, items, until, event):
        return await items(), host.journal.db.execute("SELECT * FROM running").fetchall()

    journal, running = served(lane, second)
    restarted = [(i["session"], i["run"], i["data"]) for i in journal if i["data"].get("event") == "restarted"]
    assert restarted == [("0a1b2c3d", "r1", {"type": "lane", "event": "restarted", "pid": left[0]}),
                         ("1aaaaaaa", "r7", {"type": "lane", "event": "restarted", "pid": 4242})]
    assert running == []


@pytest.mark.parametrize("over, why", [
    ({"settings": cliengine.settings("http://evil:9500/hook", 30, [])}, "settings: PreToolUse — чужой адрес"),
    ({"settings": {**cliengine.settings(HOOK_URL, 30, []), "permissions": {"allow": ["Bash"]}}}, "settings: верхний"),
    ({"cwd": "../state"}, "cwd: не путь внутри"),
    ({"cwd": "/etc"}, "cwd: не путь внутри"),
    ({"cwd": "nowhere"}, "cwd: нет такой папки"),
    ({"allowed": ["Bash(ls\n)"]}, "argv: не правило"),
    ({"allowed": "Bash"}, "allowed/disallowed: не списки правил"),
    ({"cli_session": "../../x"}, "argv: --resume без id сессии"),
    ({"prompt": None}, "новая сессия без запроса"),
    ({"hook_token": ""}, "hook_token: нет"),
])
def test_a_run_off_its_shape_is_refused_journaled_and_nothing_is_spawned(lane, over, why):
    async def scenario(host, client, post, items, until, event):
        return await post("/run", run_body(**over)), await items(), host.live

    (status, answer), journal, live = served(lane, scenario)
    assert status == 400 and answer["status"] == "refused" and answer["error"].startswith(why), answer
    assert lane.call("r1") is None and live == {} and list(Path(lane.cfg.run_dir).iterdir()) == []
    assert [(i["session"], i["data"]["event"], i["data"]["error"]) for i in journal] == \
        [("0a1b2c3d", "refused", answer["error"])]


def test_a_cli_off_the_pin_starts_no_run_and_ping_says_so(lane):
    lane.plan(version="2.1.305 (Claude Code)")

    async def scenario(host, client, post, items, until, event):
        return await post("/run", run_body()), await (await client.get("/ping", headers=SIGNED)).json()

    (status, answer), ping = served(lane, scenario)
    assert status == 400 and answer["error"] == "CLI не 2.1.304: 2.1.305 (Claude Code)" and lane.call("r1") is None
    assert ping == {"version": "2.1.305 (Claude Code)", "pinned": False,
                    "login": {"loggedIn": True, "authMethod": "oauth_token"}, "running": [], "max": 2}


def test_ping_reports_the_pin_the_login_and_what_runs(lane):
    lane.plan(result_after=99)

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body())
        return await (await client.get("/ping", headers=SIGNED)).json()

    assert served(lane, scenario) == {"version": "2.1.304 (Claude Code)", "pinned": True,
                                      "login": {"loggedIn": True, "authMethod": "oauth_token"},
                                      "running": ["0a1b2c3d"], "max": 2}


def test_unsigned_requests_are_refused_and_start_nothing(lane):
    async def scenario(host, client, post, items, until, event):
        statuses = [(await post(path, body, {"Authorization": "Bearer nope"}))[0]
                    for path, body in (("/run", run_body()), ("/answer", {}), ("/stop", {}), ("/items", {}),
                                       ("/items/confirm", {"upto": 1}))]
        return statuses, (await client.get("/ping")).status

    statuses, ping = served(lane, scenario)
    assert statuses == [401] * 5 and ping == 401 and lane.call("r1") is None
    with pytest.raises(SystemExit):
        lane_host.LaneHost(LaneHostConfig(), lane_host.Journal(":memory:"))


def test_the_lane_host_config_comes_from_the_environment(monkeypatch):
    for name, value in {"RETINUE_LANE_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "o", "RETINUE_LANE_HOOK_URL":
                        "http://platform:9500/hook/", "HTTPS_PROXY": "http://egress:3128", "NO_PROXY": "platform"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RETINUE_LANE_MAX", raising=False)
    cfg = LaneHostConfig.load()
    assert (cfg.token, cfg.oauth_token, cfg.hook_url, cfg.proxy, cfg.no_proxy, cfg.max_running) == \
        ("t", "o", "http://platform:9500/hook", "http://egress:3128", "platform", 2)
    assert cfg.run_dir == cliengine.SETTINGS_DIR
    monkeypatch.setenv("RETINUE_LANE_MAX", "0")
    with pytest.raises(SystemExit):
        LaneHostConfig.load()
