"""Releases by tag: the workflow that publishes Retinue's own images, and deploy/update.sh, which moves a server to a
tag and back to the one before when the stack is not healthy on it.

Docker is replaced by a fake that keeps the stack's state in a file: which images were pulled, which version runs and
whether its containers are healthy. The health check is the real one (deploy/health/retinue-health.py) asking the
fake. A version refuses to start on a database a newer one migrated, as Retinue does: so a rollback that did not put
the snapshot back would stay red.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / "deploy" / "update.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SERVICES = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())["services"]
IMAGE = "ghcr.io/acme/retinue"
TOKEN = "subscription-token-never-printed"

FAKE_DOCKER = r'''#!{python}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_STATE"])
state = json.loads(state_file.read_text())
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "image": os.environ.get("RETINUE_IMAGE"),
                          "version": os.environ.get("RETINUE_VERSION")}) + "\n")
SERVICES = ["assistant", "egress", "router"]
schema = Path(os.environ["RETINUE_VOLUME_ROOT"]) / "retinue_router-data" / "_data" / "schema.txt"


def semver(tag):   # a commit of develop, sha-…: after every release, in the order FAKE_SHAS lists them
    if tag.startswith("sha-"):
        return (999, 0, os.environ.get("FAKE_SHAS", "").split(",").index(tag))
    return tuple(int(part) for part in tag.lstrip("v").split("."))


def save():
    state_file.write_text(json.dumps(state))


if args[0] == "compose":
    sub = args[args.index("-f") + 2:]
    if sub == ["config", "--services"]:
        print("\n".join(SERVICES))
    elif sub == ["config", "--images"]:
        image, version = os.environ["RETINUE_IMAGE"], os.environ["RETINUE_VERSION"]
        print(f"{image}:{version}\n{image}-face:{version}\nubuntu/squid:6.6@sha256:" + "0" * 64)
    elif sub[0] == "down":
        state["running"] = None
        save()
    elif sub[0] == "up":
        version = os.environ["RETINUE_VERSION"]
        if f"{os.environ['RETINUE_IMAGE']}:{version}" not in state["images"]:
            sys.exit("no such image")
        healthy = version not in os.environ.get("FAKE_BROKEN", "").split(",")
        if semver(schema.read_text()) > semver(version):
            healthy = False                  # a database a newer version migrated: it refuses to start
        else:
            schema.write_text(version)       # its migrations, as each service opens its database
        state["running"] = {"version": version, "healthy": healthy}
        save()
    else:
        sys.exit(f"fake compose: {sub}")
elif args[0] == "pull":
    if args[1] not in os.environ["FAKE_PUBLISHED"].split(","):
        sys.exit(f"manifest unknown: {args[1]}")
    state["images"].append(args[1])
    save()
elif args[:2] == ["volume", "ls"]:
    print("retinue_router-data\nretinue_lane-private-work")
elif args[0] == "ps":
    if state["running"]:
        print("\n".join(f"{name}-0123456789" for name in SERVICES))
elif args[0] == "inspect":
    name = args[-1].split("-")[0]
    health = "" if name == "egress" else "healthy" if state["running"]["healthy"] else "unhealthy"
    print("\t".join([args[-1], f"/retinue-{name}-1", name, "0", "running", health,
                     "2026-11-02T10:00:00.000000000Z", "0", "false"]))
else:
    sys.exit(f"fake docker: {args}")
'''


def server(tmp_path):
    """A server running v1.0.0 of the release: its stack.env, a volume with the database's schema, the fake Docker."""
    srv, volumes, bin_ = tmp_path / "srv", tmp_path / "volumes", tmp_path / "bin"
    for folder in (srv, bin_, volumes / "retinue_router-data" / "_data", volumes / "retinue_lane-private-work" / "_data"):
        folder.mkdir(parents=True)
    stack = srv / "stack.env"
    stack.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nRETINUE_IMAGE={IMAGE}\nRETINUE_VERSION=v1.0.0\n")
    stack.chmod(0o600)
    (volumes / "retinue_router-data" / "_data" / "schema.txt").write_text("v1.0.0")
    (volumes / "retinue_router-data" / "_data" / "router.sqlite").write_text("the owner's rows")
    (volumes / "retinue_lane-private-work" / "_data" / "checkout.txt").write_text("a session's working tree")
    docker = bin_ / "docker"
    docker.write_text(FAKE_DOCKER.replace("{python}", sys.executable))
    docker.chmod(0o755)
    (tmp_path / "state.json").write_text(json.dumps({"images": [f"{IMAGE}:v1.0.0", f"{IMAGE}-face:v1.0.0"],
                                                     "running": {"version": "v1.0.0", "healthy": True}}))
    return srv, volumes


def update(tmp_path, tag, **fake):
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "RETINUE_ROOT": str(tmp_path / "srv"),
           "RETINUE_VOLUME_ROOT": str(tmp_path / "volumes"), "RETINUE_SNAPSHOTS": str(tmp_path / "snapshots"),
           "RETINUE_UPDATE_TIMEOUT": "5", "RETINUE_UPDATE_POLL": "0.1", "PYTHON": sys.executable,
           "FAKE_STATE": str(tmp_path / "state.json"), "FAKE_LOG": str(tmp_path / "docker.log"),
           "FAKE_PUBLISHED": f"{IMAGE}:v1.1.0,{IMAGE}-face:v1.1.0", **fake}
    run = subprocess.run(["bash", str(UPDATE), tag], capture_output=True, text=True, env=env, timeout=120)
    assert TOKEN not in run.stdout + run.stderr, "stack.env's values are never printed"
    log = tmp_path / "docker.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return run, calls, json.loads((tmp_path / "state.json").read_text())


def stack_env(srv: Path) -> dict:
    assert oct((srv / "stack.env").stat().st_mode & 0o777) == "0o600"
    return dict(line.split("=", 1) for line in (srv / "stack.env").read_text().splitlines())


def compose(calls: list[dict], command: str) -> list[int]:
    """The positions of the compose calls whose subcommand is `command`."""
    return [n for n, call in enumerate(calls) if call["args"][0] == "compose"
            and call["args"][call["args"].index("-f") + 2] == command]


def test_an_update_to_a_tag_ends_healthy_with_that_version(tmp_path):
    srv, volumes = server(tmp_path)
    run, calls, state = update(tmp_path, "v1.1.0")
    assert run.returncode == 0, run.stdout + run.stderr
    assert state["running"] == {"version": "v1.1.0", "healthy": True}
    assert stack_env(srv) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "RETINUE_IMAGE": IMAGE, "RETINUE_VERSION": "v1.1.0"}, \
        "the version is recorded where compose and the next update read it; the owner's lines are kept"
    assert (volumes / "retinue_router-data" / "_data" / "schema.txt").read_text() == "v1.1.0", "migrated"
    pulls = [call["args"][1] for call in calls if call["args"][0] == "pull"]
    assert pulls == [f"{IMAGE}:v1.1.0", f"{IMAGE}-face:v1.1.0"], "Retinue's own images of the tag, nothing pinned"
    down, = compose(calls, "down")
    up, = compose(calls, "up")
    assert max(n for n, call in enumerate(calls) if call["args"][0] == "pull") < down < up, \
        "every image is in before anything is stopped"
    assert calls[up]["version"] == "v1.1.0" and calls[up]["image"] == IMAGE and "--no-build" in calls[up]["args"]
    snapshot, = (tmp_path / "snapshots").iterdir()
    assert snapshot.name.endswith("v1.0.0-to-v1.1.0"), "named for what it is"
    assert (snapshot / "stack.env").read_text().endswith("RETINUE_VERSION=v1.0.0\n")
    names = sorted(path.name for path in (snapshot / "volumes").iterdir())
    assert names == ["retinue_router-data.tar"], "the lanes' working trees are the owner's checkouts, not state"
    out = run.stdout
    for step in ("health before", "pull", "stop", "snapshot", "start v1.1.0", "health of v1.1.0"):
        assert step in out, f"each step is printed: {step}"


def test_an_update_that_fails_health_goes_back_to_the_previous_tag(tmp_path):
    srv, volumes = server(tmp_path)
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_BROKEN="v1.1.0")
    assert run.returncode == 1, run.stdout + run.stderr
    ups = compose(calls, "up")
    assert [calls[n]["version"] for n in ups] == ["v1.1.0", "v1.0.0"], "the tag, then the one before"
    assert state["running"] == {"version": "v1.0.0", "healthy": True}, \
        "healthy again: the snapshot was put back before the old version opened its database"
    data = volumes / "retinue_router-data" / "_data"
    assert (data / "schema.txt").read_text() == "v1.0.0" and (data / "router.sqlite").read_text() == "the owner's rows"
    assert stack_env(srv)["RETINUE_VERSION"] == "v1.0.0"
    assert "back to v1.0.0" in run.stdout and "router: running, unhealthy" in run.stdout, "what was not healthy"
    assert (tmp_path / "snapshots").is_dir() and any((tmp_path / "snapshots").iterdir()), "the snapshot stays"


def test_an_update_to_a_tag_that_was_not_published_changes_nothing(tmp_path):
    srv, volumes = server(tmp_path)
    before = (srv / "stack.env").read_text()
    run, calls, state = update(tmp_path, "v1.2.0")
    assert run.returncode != 0 and "v1.2.0" in run.stderr
    assert not compose(calls, "down") and not compose(calls, "up"), "nothing stopped"
    assert (srv / "stack.env").read_text() == before and state["running"]["version"] == "v1.0.0"


def workflow_steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_the_release_workflow_parses_and_pins_every_action_by_commit():
    workflow = yaml.safe_load(RELEASE.read_text())
    trigger = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    assert trigger["push"] == {"tags": ["v*.*.*"]}, "a release is a tag"
    assert trigger["workflow_call"]["inputs"] == {"commit": {
        "description": "the commit of develop to build; its images are tagged sha-<its first 12 hex>",
        "type": "string", "required": True}}, "or a commit of develop, when CI calls it (tests/test_follow_develop.py)"
    assert workflow["permissions"] == {"contents": "read", "packages": "write"}
    assert all("permissions" not in job for job in workflow["jobs"].values()), "no job widens them"
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in path.read_text().splitlines():
            if re.match(r"\s*(- )?uses:", line) and not re.fullmatch(r"\s*uses: \./\.github/workflows/[\w-]+\.yml", line):
                # a workflow of this repository's own is its commit already: nothing to pin
                assert re.fullmatch(r"\s*(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+){0,2}", line), \
                    f"{path.name}: an action pinned by commit, its release in a comment: {line.strip()}"
    text = RELEASE.read_text()
    assert "${{ github.repository_owner }}" in text and not re.search(r"ghcr\.io/(?!\$)", text), \
        "the images go to the repository owner's registry, whoever that is"
    for step in workflow_steps(workflow):
        assert "${{" not in step.get("run", ""), f"values reach a script through env, never pasted in: {step}"


def test_the_release_builds_every_image_of_retinues_own_that_compose_runs():
    """Each image compose takes from the release is built by the workflow exactly as compose builds it locally."""
    own = {}
    for name, service in SERVICES.items():
        image = service.get("image", "")
        if not image.startswith("${RETINUE_IMAGE:-retinue}"):
            continue
        assert re.fullmatch(r"\$\{RETINUE_IMAGE:-retinue\}(-[a-z-]+)?:\$\{RETINUE_VERSION:-local\}", image), name
        if "build" in service:
            build = service["build"]
            context = (ROOT / "deploy" / build["context"]).resolve()
            own[image.split(":${RETINUE_VERSION")[0].replace("${RETINUE_IMAGE:-retinue}", "retinue")] = {
                "context": context.relative_to(ROOT).as_posix() or ".",
                "dockerfile": (context / build.get("dockerfile", "Dockerfile")).relative_to(ROOT).as_posix(),
                "args": " ".join(f"{key}={value}" for key, value in (build.get("args") or {}).items())}
    for name, service in SERVICES.items():
        image = service.get("image", "")
        if image.startswith("${RETINUE_IMAGE:-retinue}"):
            assert image.split(":${RETINUE_VERSION")[0].replace("${RETINUE_IMAGE:-retinue}", "retinue") in own, \
                f"{name}: a service of the release's image that no service builds locally"
    assert sorted(own) == ["retinue", "retinue-face", "retinue-lane-private", "retinue-pages-build"]
    workflow = yaml.safe_load(RELEASE.read_text())
    matrix = workflow["jobs"]["images"]["strategy"]["matrix"]["include"]
    assert {entry["image"]: {"context": entry["context"], "dockerfile": entry["dockerfile"],
                             "args": entry.get("args", "")} for entry in matrix} == own


def test_the_release_publishes_every_image_for_amd64_and_arm64_under_one_tag():
    """An x86 server and an arm64 one pull the same vX.Y.Z: each image is built on a runner of each
    architecture, natively, pushed as vX.Y.Z-<arch>, and vX.Y.Z is then made to name both."""
    workflow = yaml.safe_load(RELEASE.read_text())
    images = workflow["jobs"]["images"]
    matrix = images["strategy"]["matrix"]
    assert matrix["arch"] == ["amd64", "arm64"]
    assert sorted(matrix["image"]) == sorted(entry["image"] for entry in matrix["include"]), "every image, both arches"
    assert images["runs-on"] == "${{ matrix.arch == 'arm64' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }}", "no emulation"
    [build] = [step for step in images["steps"] if "run" in step]
    assert 'ref="ghcr.io/${OWNER,,}/$IMAGE:$TAG-$ARCH"' in build["run"] and build["env"]["ARCH"] == "${{ matrix.arch }}"
    assert "{{.Server.Arch}}" in build["run"], "an image is never pushed under the other architecture's name"
    tags = workflow["jobs"]["tags"]
    assert tags["needs"] == "images" and sorted(tags["strategy"]["matrix"]["image"]) == sorted(matrix["image"])
    [merge] = [step for step in tags["steps"] if "run" in step]
    assert 'docker buildx imagetools create -t "$ref" "$ref-amd64" "$ref-arm64"' in merge["run"]
