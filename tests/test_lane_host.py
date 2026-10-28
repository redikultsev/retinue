"""The lane host against a fake `claude`: a script that prints scripted stream-json lines and records its argv, its
environment, its settings file and what came on its stdin — so what reaches the CLI is seen exactly as it was built."""

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import stat
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from retinue import cliengine, lane_host
from retinue.config import LaneHostConfig
from retinue.sessions import PlatformStore

TOKEN = "lane-token"
SIGNED = {"Authorization": f"Bearer {TOKEN}"}
HOOK_URL = "http://platform:9500/hook"
CLI_SESSION = "68c88c62-f9c6-44e3-bbba-6e45dfa967a2"
# macOS adds this one to every process's environment; anywhere else the CLI's environment must match exactly.
ADDED_BY_OS = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
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
# Python leaves SIGINT ignored when it starts ignored — as under a shell's `&`, where a test run in the background
# finds it — and the CLI it stands for handles SIGINT itself. So: set, either way.
signal.signal(signal.SIGINT, signal.SIG_IGN if plan.get("ignore_int") else signal.default_int_handler)
if plan.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if plan.get("grandchild"):  # a child of its own that keeps its stdout open after it is gone
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    open(os.path.join(here, "grandchild.pid"), "w").write(str(child.pid))
settings = args[args.index("--settings") + 1]
run = os.path.basename(settings)[:-len(".json")]
with open(os.path.join(here, f"call-{{run}}.json"), "w") as f:
    fd = int(os.environ["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"])
    json.dump({{"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "settings": json.load(open(settings)),
               "token": os.read(fd, 4096).decode(), "token_then": os.read(fd, 4096).decode(),
               "mode": os.stat(settings).st_mode & 0o777,
               "notes": open(args[args.index("--append-system-prompt-file") + 1]).read()
               if "--append-system-prompt-file" in args else None}}, f)
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
        self.cfg = LaneHostConfig(token=TOKEN, hook_url=HOOK_URL, proxy="http://egress:3128",
                                  no_proxy="platform", max_running=2, work=str(self.work),
                                  home=str(tmp_path / "config"), config_dir=str(tmp_path / "config" / "claude"),
                                  state=str(tmp_path / "state"), run_dir=str(tmp_path / "run"))
        Path(self.cfg.run_dir).mkdir()
        self.plan()

    def plan(self, **plan) -> None:
        (self.dir / "plan.json").write_text(json.dumps({"lines": [INIT], **plan}))

    def call(self, run: str) -> dict | None:
        path = self.dir / f"call-{run}.json"
        if not path.exists():
            return None
        call = json.loads(path.read_text())
        call["env"] = {name: value for name, value in call["env"].items() if name not in ADDED_BY_OS}
        return call

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
    monkeypatch.setattr(cliengine, "SETTINGS_PATH",
                        re.compile(re.escape(lane.cfg.run_dir) + "/" + cliengine.RUN_ID.pattern + r"\.json"))
    return lane


def run_body(session="0a1b2c3d", run="r1", prompt="Почини тест", **over) -> dict:
    return {"session": session, "run": run, "cli_session": None, "cwd": "repo",
            "settings": cliengine.settings(HOOK_URL, 30, ["Bash(npm test:*)"]), "allowed": ["Read", "Bash(npm test:*)"],
            "disallowed": ["WebFetch", "Bash(git push:*)"], "prompt": prompt, "hook_token": "hook-1",
            "oauth_token": "oauth", **over}


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
    fd = int(call["env"][cliengine.TOKEN_FD])
    assert call["env"] == cliengine.env(lane.cfg.home, lane.cfg.config_dir, "http://egress:3128", "platform",
                                        "hook-1", fd)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"], "the token is in no process's environment"
    assert (call["token"], call["token_then"]) == ("oauth", ""), "read once from its pipe, then empty"
    assert fd > 2, "a descriptor of its own, not stdin, stdout or stderr"
    assert call["cwd"] == str(lane.work / "repo")
    assert call["settings"] == body["settings"] and call["mode"] == 0o444
    assert lane.stdin("r1") == [cliengine.user_line("Почини тест").decode()]
    assert not any("Почини" in arg for arg in call["argv"])
    assert not Path(settings).exists()  # the run's settings file goes with its process
    assert [i["data"].get("event") or i["data"]["type"] for i in journal] == ["started", "system", "result", "exited"]
    assert {(i["session"], i["run"]) for i in journal} == {("0a1b2c3d", "r1")}
    assert journal[-1]["data"]["code"] == 0 and journal[1]["line"] == INIT


def test_a_forks_first_run_resumes_its_parent_with_fork_session_and_a_fork_without_a_parent_or_text_is_refused(lane):
    async def scenario(host, client, post, items, until, event):
        assert await post("/run", run_body(cli_session=CLI_SESSION, fork=True)) == (200, {"status": "started"})
        await event("exited")
        for bad in (run_body(run="r2", fork=True), run_body(run="r3", cli_session=CLI_SESSION, fork=True, prompt=None),
                    run_body(run="r4", cli_session=CLI_SESSION, fork="yes")):
            status, answer = await post("/run", bad)
            assert status != 200 or answer.get("status") != "started", bad

    served(lane, scenario)
    assert lane.call("r1")["argv"][-3:] == ["--resume", CLI_SESSION, "--fork-session"]


def test_a_run_the_platform_marks_gets_its_sub_sessions_server_at_the_lanes_own_hook_address(lane):
    """`subs: true` (`retinue/subs.py`): the lane host writes the server's address from its own hook address — the
    platform never sends one — and refuses anything but a boolean."""
    async def scenario(host, client, post, items, until, event):
        assert await post("/run", run_body(subs=True)) == (200, {"status": "started"})
        await event("exited")
        status, answer = await post("/run", run_body(run="r2", subs="http://evil/mcp"))
        assert status != 200 or answer.get("status") != "started"

    served(lane, scenario)
    assert lane.call("r1")["argv"][-3:] == ["--mcp-config", cliengine.mcp_config(lane.cfg.hook_url),
                                            "--strict-mcp-config"]


def test_a_run_in_a_repository_with_rules_gets_them_and_the_owners_skills_beside_its_settings(lane, tmp_path,
                                                                                               monkeypatch):
    """The rules are read for every run, resumes too, into `<run>.md` beside the settings, and go with the process;
    the skills folder is passed only while it holds nothing but skills."""
    repo = lane.work / "repo"
    (repo / "CLAUDE.md").write_text("Правила репозитория.\n@AGENTS.md\n")
    (repo / "AGENTS.md").write_text("Канон.\n")
    skills = tmp_path / "skills"
    (skills / "skills" / "write-ru").mkdir(parents=True)
    (skills / "skills" / "write-ru" / "SKILL.md").write_text("---\nname: write-ru\ndescription: x\n---\nbody\n")
    monkeypatch.setenv("RETINUE_LANE_SKILLS", str(skills))

    async def scenario(host, client, post, items, until, event):
        assert await post("/run", run_body(cli_session=CLI_SESSION, instructions=True)) == (200, {"status": "started"})
        await event("exited")
        (skills / "hooks").mkdir()  # a plugin's hooks run outside the sandbox: the folder is not loaded now
        assert await post("/run", run_body(run="r2", prompt=None, cli_session=CLI_SESSION, instructions=True)) == \
            (200, {"status": "started"})
        await until(lambda: lane.call("r2"))
        await event("exited")

    served(lane, scenario)
    first, second = lane.call("r1"), lane.call("r2")
    notes = f"{lane.cfg.run_dir}/r1.md"
    assert first["argv"][-4:] == ["--append-system-prompt-file", notes, "--plugin-dir", str(skills)]
    assert "Правила репозитория." in first["notes"] and "Канон." in first["notes"], "with its include"
    assert first["notes"].split("\n\n", 1)[1] != second["notes"].split("\n\n", 1)[1], "a nonce of its own"
    assert "--plugin-dir" not in second["argv"] and second["argv"][-2:] == ["--append-system-prompt-file",
                                                                           f"{lane.cfg.run_dir}/r2.md"]
    assert os.listdir(lane.cfg.run_dir) == [], "the rules go with the process, as its settings"


@pytest.mark.parametrize("asked, web", [(False, False), (None, False), (True, True)])
def test_a_web_lane_or_a_run_the_platform_did_not_ask_it_for_reads_no_rules_and_no_skills(lane, tmp_path, monkeypatch,
                                                                                         asked, web):
    (lane.work / "repo" / "CLAUDE.md").write_text("Правила.\n")
    skills = tmp_path / "skills"
    (skills / "skills" / "write-ru").mkdir(parents=True)
    (skills / "skills" / "write-ru" / "SKILL.md").write_text("---\nname: write-ru\ndescription: x\n---\nbody\n")
    monkeypatch.setenv("RETINUE_LANE_SKILLS", str(skills))
    lane.cfg.web = web

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body(**({} if asked is None else {"instructions": asked})))
        await event("exited")

    served(lane, scenario)
    argv = lane.call("r1")["argv"]
    assert "--append-system-prompt-file" not in argv and "--plugin-dir" not in argv


def test_the_owners_memory_index_comes_first_in_every_run_asked_for_it_and_never_on_a_web_lane(lane, tmp_path,
                                                                                               monkeypatch):
    """Before the repository's rules, and alone when there are none; a snapshot that fails the check is left out."""
    memory = tmp_path / "owner-memory"
    (memory / "memory").mkdir(parents=True)
    (memory / "memory" / "MEMORY.md").write_text("- [Ссылки](always-give-links.md) — всегда давать ссылки\n")
    (memory / "memory" / "always-give-links.md").write_text("Ссылки.\n")
    monkeypatch.setenv("RETINUE_LANE_OWNER_MEMORY", str(memory))
    (lane.work / "repo" / "CLAUDE.md").write_text("Правила.\n")
    (lane.work / "bare").mkdir()

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body(instructions=True))
        await event("exited")
        await post("/run", run_body(run="r2", cwd="bare", instructions=True))
        await until(lambda: lane.call("r2"))
        await event("exited")
        (memory / "memory" / "notes.txt").write_text("x")
        await post("/run", run_body(run="r3", instructions=True))
        await until(lambda: lane.call("r3"))
        await event("exited")
        host.cfg.web = True
        (memory / "memory" / "notes.txt").unlink()
        await post("/run", run_body(run="r4", cwd="bare", instructions=True))
        await until(lambda: lane.call("r4"))
        await event("exited")

    served(lane, scenario)
    first, bare, refused, web = (lane.call(r) for r in ("r1", "r2", "r3", "r4"))
    assert first["notes"].startswith("Память Владельца") and "всегда давать ссылки" in first["notes"]
    assert first["notes"].index("всегда давать ссылки") < first["notes"].index("Правила репозитория")
    assert bare["notes"].startswith("Память Владельца") and "Правила репозитория" not in bare["notes"]
    assert "--append-system-prompt-file" in bare["argv"], "a session in no repository still gets the owner's memory"
    assert "Память Владельца" not in refused["notes"] and refused["notes"].startswith("Правила репозитория")
    assert "--append-system-prompt-file" not in web["argv"]


def test_a_repository_that_breaks_the_reading_never_fails_the_run(lane, monkeypatch):
    (lane.work / "repo" / "CLAUDE.md").write_text("Правила.\n")

    def broken(cwd, work):
        raise RecursionError("deep")

    monkeypatch.setattr(lane_host.instructions, "compose", broken)

    async def scenario(host, client, post, items, until, event):
        assert (await post("/run", run_body(instructions=True)))[1] == {"status": "started"}
        await event("exited")

    served(lane, scenario)
    assert lane.call("r1")["notes"].startswith("Правила репозитория этой сессии не прочитаны (RecursionError)")


def test_a_run_in_a_repository_without_rules_has_no_instructions_file(lane):
    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body(instructions=True))
        await event("exited")

    served(lane, scenario)
    assert "--append-system-prompt-file" not in lane.call("r1")["argv"] and lane.call("r1")["notes"] is None
    assert "--plugin-dir" not in lane.call("r1")["argv"], "no skills folder on this lane"


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


def grace(code: int) -> float:
    """STOP_GRACE_S for a stop test. A fake that obeys SIGINT exits as soon as Python has run its handler — on a loaded
    machine that is more than any short grace, and SIGTERM would beat it — so it gets a long one, which it never
    waits out. A fake that ignores SIGINT is to be escalated: a short one."""
    return 30.0 if code == 130 else 0.3


@pytest.mark.parametrize("ignore_int, code", [(False, 130), (True, -signal.SIGTERM)])
def test_stop_interrupts_and_terminates_a_process_that_does_not_go(lane, monkeypatch, ignore_int, code):
    monkeypatch.setattr(lane_host, "STOP_GRACE_S", grace(code))
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


@pytest.mark.parametrize("plan, code", [({}, 130), ({"ignore_int": True}, -signal.SIGTERM),
                                        ({"ignore_int": True, "ignore_term": True}, -signal.SIGKILL)])
def test_the_hosts_shutdown_stops_every_run_and_journals_its_end(lane, monkeypatch, plan, code):
    """What SIGTERM from Docker sets off: each live run stopped as `/stop` stops it and waited for, no run started
    after it, and nothing left for the next host to journal as `restarted`."""
    monkeypatch.setattr(lane_host, "STOP_GRACE_S", grace(code))
    monkeypatch.setattr(lane_host, "KILL_GRACE_S", 0.3)
    lane.plan(result_after=99, **plan)

    async def scenario(host, client, post, items, until, event):
        for session, run in (("0a1b2c3d", "r1"), ("1aaaaaaa", "r2")):
            await post("/run", run_body(session=session, run=run))
            await until(lambda: lane.stdin(run))
        await host.shutdown()
        late = await post("/run", run_body(session="2bbbbbbb", run="r3"))
        return late, host.live, host.journal.db.execute("SELECT * FROM running").fetchall(), await items()

    late, live, running, journal = served(lane, scenario)
    assert late == (200, {"status": "busy"}) and lane.call("r3") is None
    assert live == {} and running == []
    for session in ("0a1b2c3d", "1aaaaaaa"):
        lane_events = [i["data"] for i in journal if i["session"] == session and i["data"]["type"] == "lane"]
        assert [e["event"] for e in lane_events] == ["started", "stopped", "exited"]
        assert lane_events[1]["by"] == "shutdown", "the platform resumes a turn the lane's own stop cut"
        assert lane_events[-1]["code"] == code


def test_a_pump_a_grandchild_holds_open_is_cut_and_its_end_journaled_all_the_same(lane, monkeypatch):
    monkeypatch.setattr(lane_host, "STOP_GRACE_S", 0.2)
    monkeypatch.setattr(lane_host, "KILL_GRACE_S", 0.2)
    lane.plan(result_after=99, grandchild=True)

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body())
        await until(lambda: lane.stdin("r1"))
        try:
            await asyncio.wait_for(host.shutdown(), 5)
        finally:
            os.kill(int((lane.dir / "grandchild.pid").read_text()), signal.SIGKILL)
        return host.live, host.tasks, host.journal.db.execute("SELECT * FROM running").fetchall(), await items()

    live, tasks, running, journal = served(lane, scenario)
    assert live == {} and not tasks and running == []
    assert [i["data"]["event"] for i in journal if i["data"]["type"] == "lane"] == ["started", "stopped", "exited"]


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
    ({"cwd": "repo/out"}, "cwd: нет такой папки"),
    ({"allowed": ["Bash(ls\n)"]}, "argv: не правило"),
    ({"allowed": "Bash"}, "allowed/disallowed: не списки правил"),
    ({"cli_session": "../../x"}, "argv: --resume без id сессии"),
    ({"prompt": None}, "новая сессия без запроса"),
    ({"prompt": None, "cli_session": ""}, "новая сессия без запроса"),
    ({"hook_token": ""}, "hook_token: нет"),
])
def test_a_run_off_its_shape_is_refused_journaled_and_nothing_is_spawned(lane, over, why):
    (lane.work / "repo" / "out").symlink_to(lane.dir)  # a directory, but outside /work
    async def scenario(host, client, post, items, until, event):
        return await post("/run", run_body(**over)), await items(), host.live

    (status, answer), journal, live = served(lane, scenario)
    assert status == 400 and answer["status"] == "refused" and answer["error"].startswith(why), answer
    assert lane.call("r1") is None and live == {} and list(Path(lane.cfg.run_dir).iterdir()) == []
    assert [(i["session"], i["data"]["event"], i["data"]["error"]) for i in journal] == \
        [("0a1b2c3d", "refused", answer["error"])]


def test_a_settings_file_that_cannot_be_written_is_a_refusal_not_a_crash(lane):
    Path(lane.cfg.run_dir).rmdir()  # the run tmpfs is not there

    async def scenario(host, client, post, items, until, event):
        return await post("/run", run_body()), await items(), host.live

    (status, answer), journal, live = served(lane, scenario)
    assert (status, answer) == (400, {"status": "refused", "error": "настройки не записаны: FileNotFoundError"})
    assert lane.call("r1") is None and live == {} and [i["data"]["event"] for i in journal] == ["refused"]


def test_concurrent_runs_keep_one_process_per_session_and_the_cap(lane):
    lane.plan(linger=2)

    async def scenario(host, client, post, items, until, event):
        same = await asyncio.gather(*(post("/run", run_body(run=f"r{i}")) for i in range(3)))
        others = await asyncio.gather(*(post("/run", run_body(session=f"{i}bbbbbbb", run=f"q{i}")) for i in range(3)))
        return same, others, sorted(host.live)

    same, others, live = served(lane, scenario)
    assert sorted(answer["status"] for _, answer in same) == ["running", "running", "started"]
    assert sorted(answer["status"] for _, answer in others) == ["busy", "busy", "started"]
    assert len(live) == 2 and live[0] == "0a1b2c3d"


def test_stop_and_answer_without_a_session_id_are_refused(lane):
    async def scenario(host, client, post, items, until, event):
        return await post("/stop", {"session": 1}), await post("/answer", {"prompt": "привет"})

    stop, answer = served(lane, scenario)
    assert stop == (400, {"status": "refused", "error": "session: не id"})
    assert answer == (400, {"status": "refused", "error": "session: не id"})


def test_lines_never_hold_a_line_over_the_cap_across_reads():
    class Pieces:
        def __init__(self, *pieces):
            self.pieces = list(pieces)

        async def read(self, n):
            return self.pieces.pop(0) if self.pieces else b""

    async def go():
        pieces = Pieces(b"a" * 120, b"b" * 120, b"c" * 60 + b"\nshort\n" + b"d" * 100, b"\nta", b"il")
        return [line async for line in lane_host.lines(pieces, 100)]

    assert asyncio.run(go()) == [300, b"short", b"d" * 100, b"tail"]


def test_the_ids_the_platform_makes_are_the_ids_the_lane_takes(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    run = store.add_run(session, "hash", [], 0.0)
    assert lane_host.SESSION_ID.fullmatch(session) and cliengine.RUN_ID.fullmatch(run)
    assert cliengine.SETTINGS_PATH.fullmatch(f"{LaneHostConfig.run_dir}/{run}.json")
    assert cliengine.SETTINGS_PATH.fullmatch(f"{LaneHostConfig.run_dir}/{'R_-9' * 16}.json")


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
    assert (cfg.token, cfg.hook_url, cfg.proxy, cfg.no_proxy, cfg.max_running) == \
        ("t", "http://platform:9500/hook", "http://egress:3128", "platform", 2)
    assert not hasattr(cfg, "oauth_token"), "the login comes from the platform with each run, never the environment"
    assert cfg.run_dir == cliengine.SETTINGS_DIR
    assert cfg.web is False
    monkeypatch.setenv("RETINUE_LANE_WEB", "1")
    assert LaneHostConfig.load().web is True, "lane-web: no repository rules, no skills"
    monkeypatch.setenv("RETINUE_LANE_MAX", "0")
    with pytest.raises(SystemExit):
        LaneHostConfig.load()


def test_a_partial_line_leaves_the_journal_once_given_and_every_other_line_stays(tmp_path):
    journal = lane_host.Journal(str(tmp_path / "journal.sqlite"))
    partial = json.dumps({"type": "stream_event", "event": {"type": "content_block_delta"}})
    for line in ('{"type": "system"}', partial, partial, "not json", '{"type": "result"}', partial):
        journal.add("ab12cd34", "r1", line)
    given = journal.look()
    assert [i["line"] for i in given] == ['{"type": "system"}', partial, partial, "not json", '{"type": "result"}',
                                          partial]
    assert journal.confirm(given[4]["seq"]) == 5
    kept = journal.db.execute("SELECT line, given FROM lines ORDER BY seq").fetchall()
    assert kept == [('{"type": "system"}', 1), ("not json", 1), ('{"type": "result"}', 1), (partial, 0)], \
        "a partial line not yet given stays until it is"
    assert [i["line"] for i in journal.look()] == [partial]


@pytest.mark.parametrize("given", [None, "", 42, "a\nb"])
def test_a_run_without_the_login_from_the_platform_is_refused_and_the_token_is_never_journaled(lane, given):
    body = {k: v for k, v in run_body().items() if k != "oauth_token"}
    if given is not None:
        body["oauth_token"] = given

    async def scenario(host, client, post, items, until, event):
        status, answer = await post("/run", body)
        return status, answer, await items()

    status, answer, journal = served(lane, scenario)
    assert status == 400 and answer["error"].startswith("oauth_token: платформа не передала вход подписки")
    assert lane.call("r1") is None, "no CLI started"
    assert [i["data"]["event"] for i in journal] == ["refused"]


def test_the_login_lives_in_the_lane_hosts_memory_only(lane, tmp_path):
    secret = "sk-ant-oat01-" + "s" * 40

    async def scenario(host, client, post, items, until, event):
        assert await post("/run", run_body(oauth_token=secret)) == (200, {"status": "started"})
        await event("exited")
        return await items(), host.oauth_token

    journal, kept = served(lane, scenario)
    assert kept == secret and lane.call("r1")["token"] == secret, "in memory, and to the CLI through its pipe"
    assert secret not in json.dumps(journal)
    stored = [path for path in tmp_path.rglob("*") if path.is_file() and path.parent != lane.dir and
              secret.encode() in path.read_bytes()]
    assert stored == [], "on no disk of the lane's: not the journal, not the run's settings"


# --- the owner's attachments ----------------------------------------------------------------------------------------

JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0jpeg").decode()
BLOCKS = [{"type": "text", "text": "Что на фото? Приложено: фото 1×1; файл «data.csv»."},
          {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG_B64}}]


def inbox_file(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}


def test_blocks_go_to_stdin_as_one_line_and_files_into_the_inbox_named_after_it(lane):
    files = [inbox_file("data.csv", b"a,b\n1,2\n")]

    async def scenario(host, client, post, items, until, event):
        assert await post("/run", run_body(prompt=BLOCKS, files=files, date="2026-10-27")) == \
            (200, {"status": "started"})
        await event("exited")

    served(lane, scenario)
    assert (lane.work / "repo" / "inbox" / "2026-10-27-data.csv").read_bytes() == b"a,b\n1,2\n"
    [line] = lane.stdin("r1")
    content = json.loads(line)["message"]["content"]
    assert content[:2] == BLOCKS
    assert content[2] == {"type": "text", "text": "Файлы владельца сохранены в папке сессии:\n"
                                                  "- inbox/2026-10-27-data.csv (8 Б)"}


def test_a_file_sent_again_is_the_same_file_and_other_bytes_under_its_name_get_another(lane):
    async def scenario(host, client, post, items, until, event):
        for run, data in (("r1", b"one"), ("r2", b"one"), ("r3", b"two")):
            await post("/run", run_body(run=run, prompt="x", files=[inbox_file("a.txt", data)], date="2026-10-27"))
            await event("exited")
            await post("/items/confirm", {"upto": 10_000})

    served(lane, scenario)
    inbox = lane.work / "repo" / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["2026-10-27-a-2.txt", "2026-10-27-a.txt"]
    assert (inbox / "2026-10-27-a-2.txt").read_bytes() == b"two"
    assert [json.loads(lane.stdin(r)[0])["message"]["content"][-1]["text"].splitlines()[-1] for r in ("r2", "r3")] \
        == ["- inbox/2026-10-27-a.txt (3 Б)", "- inbox/2026-10-27-a-2.txt (3 Б)"]


def test_an_inbox_or_a_file_name_the_session_made_a_link_is_never_written_through(lane, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (lane.work / "repo" / "inbox").symlink_to(outside)

    async def scenario(host, client, post, items, until, event):
        linked = await post("/run", run_body(prompt="x", files=[inbox_file("a.txt", b"one")], date="2026-10-27"))
        (lane.work / "repo" / "inbox").unlink()
        (lane.work / "repo" / "inbox").mkdir()
        (lane.work / "repo" / "inbox" / "2026-10-27-a.txt").symlink_to(outside / "target")
        taken = await post("/run", run_body(run="r2", prompt="x", files=[inbox_file("a.txt", b"one")],
                                            date="2026-10-27"))
        await event("exited")
        return linked, taken

    linked, taken = served(lane, scenario)
    assert linked == (400, {"status": "refused", "error": "inbox: не папка (или ссылка) в папке сессии"})
    assert taken == (200, {"status": "started"})
    assert list(outside.iterdir()) == [], "nothing written through a link"
    assert (lane.work / "repo" / "inbox" / "2026-10-27-a-2.txt").read_bytes() == b"one"


@pytest.mark.parametrize("over, why", [
    ({"files": [{"name": "a.txt", "data": "b25l", "sha256": "0" * 64}], "date": "2026-10-27"},
     "files: «a.txt» не сходится с sha256"),
    ({"files": [inbox_file("../a", b"x")], "date": "2026-10-27"}, "files: файл не того вида"),
    ({"files": [inbox_file("a", b"x")]}, "files: без даты YYYY-MM-DD"),
    ({"prompt": [{"type": "image", "source": {"type": "url", "url": "https://x"}}]},
     "prompt: источник блока image не base64 нужного типа"),
    ({"prompt": None, "cli_session": CLI_SESSION, "files": [inbox_file("a", b"x")], "date": "2026-10-27"},
     "files: без запроса"),
])
def test_attachments_off_their_shape_are_refused_and_nothing_is_written(lane, over, why):
    async def scenario(host, client, post, items, until, event):
        return await post("/run", run_body(**over))

    assert served(lane, scenario) == (400, {"status": "refused", "error": why})
    assert not (lane.work / "repo" / "inbox").exists() and lane.call("r1") is None


def test_an_answer_with_files_writes_them_into_the_live_runs_own_folder(lane):
    lane.plan(result_after=2)

    async def scenario(host, client, post, items, until, event):
        await post("/run", run_body())
        answer = await post("/answer", {"session": "0a1b2c3d", "prompt": BLOCKS,
                                        "files": [inbox_file("b.txt", b"bee")], "date": "2026-10-28"})
        bad = await post("/answer", {"session": "0a1b2c3d", "prompt": "x", "files": [inbox_file("c", b"c")]})
        await event("exited")
        return answer, bad

    answer, bad = served(lane, scenario)
    assert answer == (200, {"status": "sent"}) and bad[0] == 400
    assert (lane.work / "repo" / "inbox" / "2026-10-28-b.txt").read_bytes() == b"bee"
    second = json.loads(lane.stdin("r1")[1])["message"]["content"]
    assert second[:2] == BLOCKS and second[2]["text"].endswith("- inbox/2026-10-28-b.txt (3 Б)")


def test_a_fifo_or_a_huge_sparse_file_planted_at_the_name_is_never_waited_on_or_read(lane):
    inbox = lane.work / "repo" / "inbox"
    inbox.mkdir()
    os.mkfifo(inbox / "2026-10-27-a.txt")
    with open(inbox / "2026-10-27-a-2.txt", "wb") as sparse:
        sparse.truncate(64 * 2**30)  # 64 GB of nothing: read whole, it would take the host's memory

    async def scenario(host, client, post, items, until, event):
        answer = await asyncio.wait_for(
            post("/run", run_body(prompt="x", files=[inbox_file("a.txt", b"one")], date="2026-10-27")), 10)
        await event("exited")
        return answer

    assert served(lane, scenario) == (200, {"status": "started"})
    assert (inbox / "2026-10-27-a-3.txt").read_bytes() == b"one"
    assert stat.S_ISFIFO(os.lstat(inbox / "2026-10-27-a.txt").st_mode)


def test_the_inbox_goes_into_its_repositorys_own_exclude_once_and_nothing_is_followed(tmp_path):
    work = tmp_path / "work"
    repo = work / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "info" / "exclude").write_text("# git ls-files --others --exclude-from=.git/info/exclude")
    (repo / "sub").mkdir()
    one = [inbox_file("a.txt", b"one")]
    lane_host.save_inbox(repo / "sub", one, "2026-10-27", work)
    lane_host.save_inbox(repo / "sub", one, "2026-10-27", work)
    lane_host.save_inbox(repo, one, "2026-10-27", work)
    assert (repo / ".git" / "info" / "exclude").read_text().splitlines()[1:] == [
        "# the owner's files from Retinue", "/sub/inbox/", "# the owner's files from Retinue", "/inbox/"]
    assert sorted(p.name for p in (repo / ".git").iterdir()) == ["info"], "no hook, no config"

    linked = work / "linked"
    (linked / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (linked / ".git" / "info").symlink_to(outside)
    lane_host.save_inbox(linked, one, "2026-10-27", work)
    assert list(outside.iterdir()) == [] and (linked / "inbox" / "2026-10-27-a.txt").exists()

    hooked = work / "hooked"
    (hooked / ".git" / "info").mkdir(parents=True)
    (hooked / ".git" / "info" / "exclude").symlink_to(hooked / ".git" / "hooks-post-checkout")
    lane_host.save_inbox(hooked, one, "2026-10-27", work)
    assert not (hooked / ".git" / "hooks-post-checkout").exists()

    plain = work / "plain"
    plain.mkdir()
    lane_host.save_inbox(plain, one, "2026-10-27", work)
    assert not (work / ".git").exists() and lane_host.exclude_inbox(plain, work) is False


def test_an_ask_that_reaches_stdio_is_denied_by_the_host_and_the_run_goes_on(lane):
    """`--permission-prompt-tool stdio`: an ask the gate's hook did not decide comes as a `control_request` on stdout;
    the host answers it with a deny at once, so the CLI never waits on it — and nothing is ever granted this way."""
    ask = json.dumps({"type": "control_request", "request_id": "req-1",
                      "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {}}})
    other = json.dumps({"type": "control_request", "request_id": "req-2", "request": {"subtype": "elicitation"}})
    lane.plan(lines=[INIT, ask, other], result_after=3)

    async def scenario(host, client, post, items, until, event):
        assert (await post("/run", run_body()))[1] == {"status": "started"}
        await event("exited")

    served(lane, scenario)
    sent = [json.loads(line) for line in lane.stdin("r1")]
    assert sent[0] == json.loads(cliengine.user_line("Почини тест"))
    assert sent[1]["response"]["request_id"] == "req-1"
    assert sent[1]["response"]["response"]["behavior"] == "deny"
    assert sent[2]["response"] == {"subtype": "error", "request_id": "req-2",
                                   "error": "the lane host answers no such request"}
