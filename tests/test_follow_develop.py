"""Following develop: CI's last job hands a push to develop that passed to deploy/deploy.sh on the server, which
builds it, starts it with `docker compose up --wait`, and goes back to the commit before, with its data, when it is
not healthy.

Docker is a fake that keeps the stack in a file; the checkout is a real git repository with a real origin. A commit
migrates the database as it starts and the one before refuses a database a later one migrated, as Retinue does: so a
rollback that did not put the data back would stay red.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
TOKEN = "subscription-token-never-printed"

FAKE_DOCKER = r'''#!{python}
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_STATE"])
state = json.loads(state_file.read_text())
head = subprocess.run(["git", "-C", os.environ["RETINUE_REPO"], "rev-parse", "HEAD"],
                      capture_output=True, text=True, check=True).stdout.strip()
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "head": head}) + "\n")
data = Path(os.environ["RETINUE_VOLUME_ROOT"]) / "retinue_router-data" / "_data"
order = os.environ["FAKE_ORDER"].split(",")    # the commits, oldest first


def save():
    state_file.write_text(json.dumps(state))


if args[0] == "compose":
    sub = args[args.index("-f") + 2:]
    if sub[0] == "build":
        if head in os.environ.get("FAKE_NO_BUILD", "").split(","):
            sys.exit("build failed")
        state["built"] = head
    elif sub[0] == "down":
        state["running"] = None
    elif sub[0] == "up":
        assert "--wait" in sub
        if state.get("built") != head:
            sys.exit("no such image")
        schema = (data / "schema.txt").read_text()
        healthy = head not in os.environ.get("FAKE_BROKEN", "").split(",") and order.index(schema) <= order.index(head)
        if healthy:
            (data / "schema.txt").write_text(head)
        state["running"] = {"commit": head, "healthy": healthy}
        save()
        sys.exit(0 if healthy else 1)
    elif sub[0] == "ps":
        pass
    else:
        sys.exit(f"fake compose: {sub}")
    save()
elif args[:2] == ["volume", "ls"]:
    print("retinue_router-data\nretinue_lane-private-work")
else:
    sys.exit(f"fake docker: {args}")
'''


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def server(tmp_path):
    """A server on commit A of develop; develop's origin has A and B after it."""
    origin, repo = tmp_path / "origin", tmp_path / "repo"
    origin.mkdir()
    git("init", "-q", "-b", "develop", cwd=origin)
    commits = []
    for name in ("A", "B"):
        (origin / "change.txt").write_text(name)
        git("add", ".", cwd=origin)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name, cwd=origin)
        commits.append(git("rev-parse", "HEAD", cwd=origin))
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    git("checkout", "-q", "--detach", commits[0], cwd=repo)
    srv, volumes, bin_ = tmp_path / "srv", tmp_path / "volumes", tmp_path / "bin"
    for folder in (srv / "mail" / "state", bin_, volumes / "retinue_router-data" / "_data",
                   volumes / "retinue_lane-private-work" / "_data"):
        folder.mkdir(parents=True)
    (srv / "stack.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    (srv / "mail" / "state" / "mail.sqlite").write_text("the mail rows")
    (volumes / "retinue_router-data" / "_data" / "schema.txt").write_text(commits[0])
    (volumes / "retinue_router-data" / "_data" / "router.sqlite").write_text("the owner's rows")
    (volumes / "retinue_lane-private-work" / "_data" / "checkout.txt").write_text("a session's working tree")
    docker = bin_ / "docker"
    docker.write_text(FAKE_DOCKER.replace("{python}", sys.executable))
    docker.chmod(0o755)
    (tmp_path / "state.json").write_text(json.dumps({"built": commits[0],
                                                     "running": {"commit": commits[0], "healthy": True}}))
    return repo, volumes, commits


def deploy(tmp_path, commits, sha, **fake):
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "RETINUE_REPO": str(tmp_path / "repo"), "RETINUE_ROOT": str(tmp_path / "srv"),
           "RETINUE_VOLUME_ROOT": str(tmp_path / "volumes"), "RETINUE_SNAPSHOTS": str(tmp_path / "snapshots"),
           "FAKE_STATE": str(tmp_path / "state.json"), "FAKE_LOG": str(tmp_path / "docker.log"),
           "FAKE_ORDER": ",".join(commits), "SSH_ORIGINAL_COMMAND": sha, **fake}
    run = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env, timeout=120)
    assert TOKEN not in run.stdout + run.stderr, "stack.env's values are never printed"
    log = tmp_path / "docker.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    log.unlink(missing_ok=True)
    return run, calls, json.loads((tmp_path / "state.json").read_text())


def subcommands(calls):
    return [(call["args"][call["args"].index("-f") + 2], call["head"]) for call in calls if call["args"][0] == "compose"]


def test_a_commit_that_passed_ci_is_built_then_started_and_waited_for(tmp_path):
    repo, volumes, (a, b) = server(tmp_path)
    run, calls, state = deploy(tmp_path, [a, b], b)
    assert run.returncode == 0, run.stdout + run.stderr
    assert state["running"] == {"commit": b, "healthy": True}
    assert git("rev-parse", "HEAD", cwd=repo) == b
    assert subcommands(calls) == [("build", b), ("down", b), ("up", b)], "built while A ran; stopped only then"
    snapshot, = (tmp_path / "snapshots").iterdir()
    assert sorted(p.name for p in (snapshot / "volumes").iterdir()) == ["retinue_router-data.tar"], \
        "the lanes' working trees are checkouts, not state"
    assert (snapshot / "srv" / "mail_state.tar").exists()
    assert f"now on {b[:12]}" in run.stdout


def test_a_commit_that_is_not_healthy_goes_back_to_the_one_before_with_its_data(tmp_path):
    repo, volumes, (a, b) = server(tmp_path)
    run, calls, state = deploy(tmp_path, [a, b], b, FAKE_BROKEN=b)
    assert run.returncode == 1, run.stdout + run.stderr
    assert git("rev-parse", "HEAD", cwd=repo) == a
    assert [step for step in subcommands(calls) if step[0] == "up"] == [("up", b), ("up", a)]
    assert state["running"] == {"commit": a, "healthy": True}, "healthy again: the data was put back first"
    data = volumes / "retinue_router-data" / "_data"
    assert (data / "schema.txt").read_text() == a and (data / "router.sqlite").read_text() == "the owner's rows"
    assert f"back on {a[:12]}" in run.stdout


def test_a_commit_that_does_not_build_changes_nothing(tmp_path):
    repo, volumes, (a, b) = server(tmp_path)
    run, calls, state = deploy(tmp_path, [a, b], b, FAKE_NO_BUILD=b)
    assert run.returncode == 1
    assert [step[0] for step in subcommands(calls)] == ["build"], "nothing stopped"
    assert git("rev-parse", "HEAD", cwd=repo) == a and state["running"]["commit"] == a


def test_only_a_full_sha_is_taken_and_the_one_running_is_left_alone(tmp_path):
    repo, volumes, (a, b) = server(tmp_path)
    for bad in ("", "main", b[:12], f"{b}; rm -rf /"):
        run, calls, _ = deploy(tmp_path, [a, b], bad)
        assert run.returncode == 2 and not calls, bad
    run, calls, _ = deploy(tmp_path, [a, b], a)
    assert run.returncode == 0 and not calls and "already on" in run.stdout


def test_ci_deploys_only_pushes_to_develop_that_passed_every_check():
    ci = yaml.safe_load(CI.read_text())
    job = ci["jobs"]["deploy"]
    assert sorted(job["needs"]) == sorted(name for name in ci["jobs"] if name != "deploy"), "after every check"
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/develop'"
    assert job["environment"] == "server", "the key lives in an environment only develop may use"
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/develop' }}", \
        "a deploy is never cancelled halfway by the next push"
    assert ci["permissions"] == {"contents": "read"}
    for step in job["steps"]:
        assert "${{" not in step.get("run", ""), "values reach the script through env, never pasted in"
        assert "uses" not in step, "no third-party action holds the key"
