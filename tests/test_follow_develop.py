"""Following develop: CI builds the images of a push to develop that passed (release.yml, called from ci.yml) and its
last job hands the commit to deploy/deploy.sh on the server, which checks it out and switches to its images with
deploy/update.sh — and back, checkout and data, when they are not healthy.

The server is tests/test_update.py's: a fake Docker keeping the stack in a file, the real health check. The checkout is
a real git repository with a real origin, holding this repository's deploy/ folder.
"""

import json
import os
import shutil
import subprocess

import yaml

from test_update import IMAGE, ROOT, TOKEN, compose, server, stack_env

DEPLOY = ROOT / "deploy" / "deploy.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def checkout(tmp_path):
    """The server's checkout on commit A of develop; develop's origin has A and B after it."""
    origin, repo = tmp_path / "origin", tmp_path / "repo"
    shutil.copytree(ROOT / "deploy", origin / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
    git("init", "-q", "-b", "develop", cwd=origin)
    commits = []
    for name in ("A", "B"):
        (origin / "change.txt").write_text(name)
        git("add", ".", cwd=origin)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name, cwd=origin)
        commits.append(git("rev-parse", "HEAD", cwd=origin))
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    git("checkout", "-q", "--detach", commits[0], cwd=repo)
    return repo, commits


def deploy(tmp_path, sha, shas, **fake):
    tag = f"sha-{shas[-1][:12]}"
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "RETINUE_REPO": str(tmp_path / "repo"), "RETINUE_ROOT": str(tmp_path / "srv"),
           "RETINUE_VOLUME_ROOT": str(tmp_path / "volumes"), "RETINUE_SNAPSHOTS": str(tmp_path / "snapshots"),
           "RETINUE_UPDATE_TIMEOUT": "5", "RETINUE_UPDATE_POLL": "0.1", "RETINUE_DEPLOY_IMAGE": IMAGE,
           "FAKE_STATE": str(tmp_path / "state.json"), "FAKE_LOG": str(tmp_path / "docker.log"),
           "FAKE_PUBLISHED": f"{IMAGE}:{tag},{IMAGE}-face:{tag}", "FAKE_SHAS": ",".join(f"sha-{s[:12]}" for s in shas),
           "SSH_ORIGINAL_COMMAND": sha, **fake}
    run = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env, timeout=120)
    assert TOKEN not in run.stdout + run.stderr, "stack.env's values are never printed"
    log = tmp_path / "docker.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    log.unlink(missing_ok=True)
    return run, calls, json.loads((tmp_path / "state.json").read_text())


def test_a_commit_ci_built_is_pulled_and_started_and_the_checkout_follows(tmp_path):
    srv, volumes = server(tmp_path)
    repo, (a, b) = checkout(tmp_path)
    run, calls, state = deploy(tmp_path, b, [a, b])
    assert run.returncode == 0, run.stdout + run.stderr
    tag = f"sha-{b[:12]}"
    assert state["running"] == {"version": tag, "healthy": True}
    assert stack_env(srv)["RETINUE_VERSION"] == tag and stack_env(srv)["RETINUE_IMAGE"] == IMAGE
    assert git("rev-parse", "HEAD", cwd=repo) == b, "the checkout is the commit: its compose file names its images"
    assert [call["args"][1] for call in calls if call["args"][0] == "pull"] == [f"{IMAGE}:{tag}", f"{IMAGE}-face:{tag}"]
    builds = [calls[n]["args"][-1] for n in compose(calls, "build")]
    assert builds == ["lane-private"], "only the image no registry has is built on the server"
    assert f"now on {b[:12]}" in run.stdout and (srv / "deploy.log").read_text().count("now on") == 1


def test_a_commit_that_is_not_healthy_goes_back_with_its_data_and_its_checkout(tmp_path):
    srv, volumes = server(tmp_path)
    repo, (a, b) = checkout(tmp_path)
    run, calls, state = deploy(tmp_path, b, [a, b], FAKE_BROKEN=f"sha-{b[:12]}")
    assert run.returncode == 1, run.stdout + run.stderr
    assert git("rev-parse", "HEAD", cwd=repo) == a
    assert state["running"] == {"version": "v1.0.0", "healthy": True}
    data = volumes / "retinue_router-data" / "_data"
    assert (data / "schema.txt").read_text() == "v1.0.0" and (data / "router.sqlite").read_text()
    assert [calls[n]["version"] for n in compose(calls, "up")][-1] == "v1.0.0", \
        "started once more with the compose file of the commit before"
    assert stack_env(srv)["RETINUE_VERSION"] == "v1.0.0"


def test_a_commit_whose_images_are_not_in_the_registry_changes_nothing(tmp_path):
    srv, volumes = server(tmp_path)
    repo, (a, b) = checkout(tmp_path)
    before = (srv / "stack.env").read_text()
    run, calls, state = deploy(tmp_path, b, [a, b], FAKE_PUBLISHED="")
    assert run.returncode != 0
    assert not compose(calls, "down"), "nothing stopped"
    assert git("rev-parse", "HEAD", cwd=repo) == a and (srv / "stack.env").read_text() == before


def test_only_a_full_sha_is_taken_and_the_one_running_is_left_alone(tmp_path):
    server(tmp_path)
    repo, (a, b) = checkout(tmp_path)
    for bad in ("", "main", b[:12], f"{b}; rm -rf /"):
        run, calls, _ = deploy(tmp_path, bad, [a, b])
        assert run.returncode == 2 and not calls, bad
    run, calls, _ = deploy(tmp_path, a, [a, b])
    assert run.returncode == 0 and not calls and "already on" in run.stdout


def test_ci_builds_then_deploys_only_pushes_to_develop_that_passed_every_check():
    ci = yaml.safe_load(CI.read_text())
    images, deploy_job = ci["jobs"]["images"], ci["jobs"]["deploy"]
    checks = sorted(name for name in ci["jobs"] if name not in ("images", "deploy"))
    assert sorted(images["needs"]) == checks, "built after every check"
    assert images["uses"] == "./.github/workflows/release.yml" and images["with"] == {"commit": "${{ github.sha }}"}, \
        "the images are built as a release's are"
    assert images["permissions"] == {"contents": "read", "packages": "write"}, "only this job may write packages"
    assert deploy_job["needs"] == ["images"]
    develop_push = "github.event_name == 'push' && github.ref == 'refs/heads/develop'"
    assert images["if"] == develop_push and deploy_job["if"] == develop_push
    assert deploy_job["environment"] == "server", "the key lives in an environment only develop may use"
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/develop' }}", \
        "a deploy is never cancelled halfway by the next push"
    assert ci["permissions"] == {"contents": "read"}
    for step in deploy_job["steps"]:
        assert "${{" not in step.get("run", ""), "values reach the script through env, never pasted in"
        assert "uses" not in step, "no third-party action holds the key"
