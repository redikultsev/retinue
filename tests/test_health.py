"""The containers' health: the host's script that writes health.json, and the router's watch that tells the owner.

Docker is replaced by a fake that prints what `ps` and `inspect --format` would: no daemon, no root.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from retinue.archive import Archive
from retinue.core import Core
from retinue.health import BAD_CHECKS, STALE_S, Watch
from retinue.protocol import Store

from test_core import AGENT, WEDNESDAY, FakeChannel

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "health" / "retinue-health.py"
FAKE_DOCKER = """#!{python}
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_DOWN"):
    print("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.", file=sys.stderr)
    sys.exit(1)
lines = [
    "d1b2c3d4e5f6a7b8\\tGONE",
    "a1b2c3d4e5f6a7b8\\t/retinue-router-1\\trouter\\t0\\trunning\\thealthy\\t2026-10-26T20:54:37.545049129Z\\t0\\tfalse",
    "b1b2c3d4e5f6a7b8\\t/retinue-egress-1\\tegress\\t3\\texited\\t\\t2026-10-18T15:23:02.131476946Z\\t137\\ttrue",
    "c1b2c3d4e5f6a7b8\\t/retinue-lane-code-1\\tlane-code\\t0\\tcreated\\t\\t0001-01-01T00:00:00Z\\t0\\tfalse",
]
if sys.argv[1] == "ps":
    print("\\n".join(line.split("\\t")[0] for line in lines))
    sys.exit(0)
line = next(line for line in lines if line.startswith(sys.argv[-1]))  # one container per call
if line.endswith("GONE"):  # recreated by a deploy between `ps` and `inspect`
    print("Error: No such object: " + sys.argv[-1], file=sys.stderr)
    sys.exit(1)
print(line)
"""
NOW = WEDNESDAY


def night(hour: int, minute: int) -> float:
    """Thursday after WEDNESDAY, at this hour in the owner's zone (UTC here)."""
    return datetime(2026, 10, 8, hour, minute, tzinfo=timezone.utc).timestamp()
STARTED = datetime(2026, 10, 26, 20, 54, 37, 545049, tzinfo=timezone.utc).timestamp()  # nanoseconds cut to micro


def host(tmp_path, **extra):
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "RETINUE_ROOT": str(tmp_path / "srv"), "DOCKER": str(docker),
           "FAKE_LOG": str(tmp_path / "docker.log"), **extra}
    run = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
    path = tmp_path / "srv" / "status" / "health.json"
    calls = [json.loads(line) for line in (tmp_path / "docker.log").read_text().splitlines()]
    return run, json.loads(path.read_text()), path, calls


def test_the_host_writes_each_containers_state_and_nothing_of_its_environment(tmp_path):
    run, report, path, calls = host(tmp_path)
    assert run.returncode == 0, run.stderr
    assert oct(path.stat().st_mode & 0o777) == "0o644", "the router reads it as an unprivileged user"
    by = {c["name"]: c for c in report["containers"]}
    assert by["router"] == {"name": "router", "container": "retinue-router-1", "id": "a1b2c3d4e5f6", "state": "running",
                            "health": "healthy", "restarts": 0, "since": STARTED, "exit_code": 0,
                            "oom": False}
    assert by["egress"]["state"] == "exited" and by["egress"]["exit_code"] == 137 and by["egress"]["oom"] is True
    assert by["lane-code"]["since"] is None, "never started"
    assert len(by) == 3, "the one gone between ps and inspect is skipped, not the whole report"
    ps, inspect, *_ = calls
    assert "label=com.docker.compose.project=retinue" in ps and "label=com.docker.compose.oneoff=False" in ps
    template = inspect[inspect.index("--format") + 1]
    assert "{{json" not in template and ".Config.Env" not in template, "the environment holds secrets: never read"


def test_docker_not_answering_is_written_too(tmp_path):
    run, report, _, _ = host(tmp_path, FAKE_DOWN="1")
    assert run.returncode == 1 and report["error"].startswith("Cannot connect to the Docker daemon")
    assert "containers" not in report and report["checked"] > 0


class Host:
    """health.json as the host would write it, check after check."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "health.json"
        self.checked = NOW - 600
        self.containers = {name: {"name": name, "id": f"{name}-1", "state": "running", "health": "healthy",
                                  "restarts": 0} for name in ("router", "assistant", "egress")}

    def check(self, **changes) -> float:
        """One run of the timer: two minutes later, with the containers changed as given."""
        self.checked += 120
        for name, change in changes.items():
            if change is None:
                self.containers.pop(name)
            else:
                self.containers[name] = {**self.containers.get(name, {"name": name}), **change}
        self.path.write_text(json.dumps({"checked": self.checked, "containers": list(self.containers.values())}))
        return self.checked


def watch(tmp_path, db=None):
    return Watch(str(tmp_path / "health.json"), db or sqlite3.connect(tmp_path / "r.sqlite"))


def test_unhealthy_is_told_after_two_checks_once_and_its_recovery_once(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    assert w.check(h.check()) == ""
    assert w.check(h.check(assistant={"health": "unhealthy"})) == "", "one check could be a deploy in the middle"
    at = h.check()
    assert BAD_CHECKS == 2 and w.check(at) == "Server: not OK.\n- assistant: not responding (healthcheck: unhealthy)"
    assert w.check(at) == "", "the same file is counted once, however often the router's clock looks"
    assert w.check(h.check(assistant={"health": "unhealthy", "state": "restarting"})) == "", "no repeat while bad"
    assert w.check(h.check(assistant={"health": "healthy", "state": "running"})) == "", "one good check is not enough"
    assert w.check(h.check(assistant={"health": "unhealthy"})) == "", "a flap: still bad, not told again"
    assert w.check(h.check(assistant={"health": "healthy"})) == ""
    assert w.check(h.check()) == "Server: OK again — assistant.", "two good checks in a row"
    assert w.check(h.check()) == ""


def test_a_restart_by_docker_is_told_at_once_and_a_new_container_is_a_deploy(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check())
    assert w.check(h.check(router={"restarts": 1})) == \
        "Server: not OK.\n- router: restarted by Docker (1 in the container's life), running now"
    assert w.check(h.check()) == "", "running again: nothing to recover from"
    assert w.check(h.check(router={"id": "router-2", "restarts": 0})) == "", "recreated by a deploy"
    assert w.check(h.check(egress={"state": "restarting", "restarts": 5, "oom": True})) == \
        "Server: not OK.\n- egress: restarting, out of memory, restarted by Docker (5 in the container's life)"
    assert w.check(h.check(egress={"state": "running", "restarts": 6, "oom": False})) == "", "still restarting"
    w.check(h.check())
    assert w.check(h.check()) == "Server: OK again — egress."


def test_several_at_once_make_one_message_and_a_removed_one_is_let_go(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check(egress={"state": "exited", "exit_code": 137}, assistant={"state": "exited", "exit_code": 1}))
    assert w.check(h.check()) == ("Server: not OK.\n- assistant: stopped, code 1\n"
                                  "- egress: stopped, code 137")
    assert w.check(h.check(egress=None, assistant={"state": "running"})) == \
        "Server: OK again — egress (the container is gone)."
    assert w.check(h.check()) == "Server: OK again — assistant."


def test_a_status_that_stops_coming_is_itself_told_and_so_is_its_return(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    at = h.check()
    assert w.check(at + STALE_S - 1) == ""
    assert w.check(at + STALE_S + 60) == ("Server: not OK.\n- the container watch is silent (last one 11 min "
                                          "ago): the retinue-health timer on the host is not running, and nobody is "
                                          "left to report a container going down")
    assert w.check(at + STALE_S + 180) == ""
    h.checked = at + STALE_S + 300
    assert w.check(h.check()) == "Server: OK again — the container watch."
    assert w.health(h.checked) == "containers: all 3 OK"


def test_docker_silent_is_one_bad_container_and_nothing_is_forgotten(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check(router={"restarts": 2}))
    for _ in range(BAD_CHECKS):
        h.checked += 120
        h.path.write_text(json.dumps({"checked": h.checked, "error": "Cannot connect to the Docker daemon"}))
        said = w.check(h.checked)
    assert said == "Server: not OK.\n- docker: Docker did not answer: Cannot connect to the Docker daemon"
    assert w.health(h.checked) == "Docker did not answer the watch: Cannot connect to the Docker daemon"
    assert w.check(h.check()) == "Server: OK again — docker.", "no container is «gone»: their rows were kept"
    h.checked += 3600  # past the hour of the last notice about it
    assert w.check(h.check(router={"restarts": 3})).startswith("Server: not OK.\n- router: restarted by Docker"), \
        "the count before Docker went silent is still known"


def test_what_was_told_survives_a_router_restart(tmp_path):
    h, db = Host(tmp_path), sqlite3.connect(tmp_path / "r.sqlite")
    first = watch(tmp_path, db)
    first.check(h.check(egress={"state": "exited", "exit_code": 1}))
    assert first.check(h.check()).startswith("Server: not OK.")
    again = watch(tmp_path, sqlite3.connect(tmp_path / "r.sqlite"))
    assert again.check(h.check()) == "", "told once, not again by the next process"
    again.check(h.check(egress={"state": "running"}))
    assert again.check(h.check()) == "Server: OK again — egress."


def test_no_file_says_nothing_and_a_torn_one_waits_for_its_time(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    assert w.check(NOW) == "" and w.health(NOW) == "container watch not set up"
    w.check(h.check())
    h.path.write_text("{torn")
    os.utime(h.path, (NOW, NOW))
    assert w.check(NOW + 60) == "" and w.health(NOW + 60) == "container status unreadable"
    assert "status unreadable" in w.check(NOW + STALE_S + 60)


def test_the_router_tells_the_owner_once_and_the_morning_summary_has_a_line(tmp_path):
    h = Host(tmp_path)
    store = Store(str(tmp_path / "r.sqlite"))
    core = Core([AGENT], store, "owner", archive=Archive(":memory:"), watch=Watch(str(h.path), store.db))
    channel = FakeChannel("telegram", True)

    async def run():
        await core.start([channel])
        h.check(assistant={"state": "exited", "exit_code": 1})
        await core.step(h.checked)
        h.check()
        for _ in range(3):  # the clock ticks every ten seconds; the file changes every two minutes
            await core.step(h.checked + 10)
            await asyncio.sleep(0)
        await asyncio.gather(*core.watch.tasks)

    asyncio.run(run())
    told = [text for text, _, _ in channel.cards]
    assert told == ["Server: not OK.\n- assistant: stopped, code 1"]
    line = core.health(h.checked)
    assert "containers: 2 of 3 OK, not OK — assistant (stopped, code 1), failures in a day — 1 " \
           "(assistant)." in line


def test_a_restart_loop_is_one_notice_an_hour_and_the_rest_are_counted(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check())
    said = [w.check(h.check(egress={"restarts": n})) for n in range(1, 11)]  # an OOM loop: a restart every check
    assert [bool(s) for s in said] == [True] + [False] * 9, "one notice; nine more in twenty minutes, counted"
    assert "failures in a day — 10 (egress)" in w.health(h.checked)
    h.checked += 3600
    assert w.check(h.check(egress={"restarts": 11})).startswith("Server: not OK.\n- egress: restarted by Docker"), \
        "an hour on: told again"


def test_a_flapping_check_told_once_is_counted_not_resent_within_the_hour(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    told = []
    for _ in range(4):  # bad two checks, good two checks, again and again
        for health in ("unhealthy", "unhealthy", "healthy", "healthy"):
            told.append(w.check(h.check(assistant={"health": health})))
    assert [t.split("\n")[0] for t in told if t] == ["Server: not OK.", "Server: OK again — assistant."]
    assert "failures in a day — 4 (assistant)" in w.health(h.checked)


def test_quiet_hours_hold_notices_for_one_morning_message_but_not_an_outage(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.attach(SimpleNamespace(tz="UTC", proactive=None))  # the default quiet hours: 22–9
    h.checked = night(2, 50)
    w.check(h.check())
    assert w.check(h.check(egress={"restarts": 1})) == "", "held: 02:54"
    w.check(h.check(router={"state": "exited", "exit_code": 1}))
    assert w.check(h.check()) == "Server: not OK.\n- router: stopped, code 1", "the router down: at once"
    h.checked = night(9, 0)
    assert w.check(h.check(router={"state": "running"})) == ("Server during quiet hours:\n- 02:54 egress: restarted "
                                                             "by Docker (1 in the container's life), running now")
    assert w.check(h.check()) == "Server: OK again — router."


def test_an_operators_stop_is_told_once_and_is_no_failure(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check(egress={"state": "exited", "exit_code": 0}))
    assert w.check(h.check()) == "Server: not OK.\n- egress: stopped manually (code 0)"
    assert w.check(h.check()) == ""
    assert w.health(h.checked) == "containers: all 2 OK, stopped manually — egress", "not a failure"


def test_no_container_at_all_is_an_outage_not_all_well(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check())
    h.containers.clear()
    w.check(h.check())
    assert w.check(h.check()) == "Server: not OK.\n- docker: Docker shows no retinue container"
    assert w.health(h.checked) == "containers: none visible"


def test_an_operators_stop_kept_in_the_old_words_is_still_no_failure(tmp_path):
    h, w = Host(tmp_path), watch(tmp_path)
    w.check(h.check(egress={"state": "exited", "exit_code": 0}))
    w.check(h.check())
    w.db.execute("UPDATE health_services SET bad = 'остановлен вручную (код 0)' WHERE name = 'egress'")
    assert w.health(h.checked) == "containers: all 2 OK, stopped manually — egress", "a row from before the English"
