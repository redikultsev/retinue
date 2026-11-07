"""Releases by tag: the workflow that publishes Retinue's own images, and deploy/update.sh, which moves a server to a
tag — restarting only what the tag changes (deploy/changed.py) — and back to the one before when the stack is not
healthy on it.

Docker is replaced by a fake that keeps the stack's state in a file: the images here, each with an id from its
content, and a container per service, with the version and image it started with and whether it is healthy. The
health check is the real one (deploy/health/retinue-health.py) asking the fake. The router refuses to start on a
database a newer version migrated, as Retinue does: so a rollback that did not put the snapshot back would stay red.
"""

import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / "deploy" / "update.sh"
CHANGED = ROOT / "deploy" / "changed.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SERVICES = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())["services"]
IMAGE = "ghcr.io/acme/retinue"
TOKEN = "subscription-token-never-printed"
SQUID = "ubuntu/squid:6.6@sha256:" + "0" * 64
OWN = ["assistant", "face", "lane-private", "lane-private-sni", "platform", "router"]   # Retinue's images
PINNED = ["egress", "travel-ops"]                                                        # images of a fixed name

FAKE_DOCKER = r'''#!{python}
import hashlib, json, os, sys
from pathlib import Path

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_STATE"])
state = json.loads(state_file.read_text())
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "image": os.environ.get("RETINUE_IMAGE"),
                          "version": os.environ.get("RETINUE_VERSION")}) + "\n")
srv = Path(os.environ["RETINUE_ROOT"])
schema = Path(os.environ["RETINUE_VOLUME_ROOT"]) / "retinue_router-data" / "_data" / "schema.txt"
SQUID = "ubuntu/squid:6.6@sha256:" + "0" * 64
UNHEALTHY = ("egress", "travel-ops")   # no healthcheck


def config():   # what compose.yml renders to, in the parts update.sh and changed.py read
    image, version = os.environ["RETINUE_IMAGE"], os.environ["RETINUE_VERSION"]
    own, built, after = f"{image}:{version}", {"context": ".."}, {"condition": "service_started"}
    volume = lambda name, ro=False: {"type": "volume", "source": name, "target": "/" + name, "read_only": ro}
    bind = lambda path: {"type": "bind", "source": str(srv / path), "target": "/" + path, "read_only": True}
    services = {
        "router": {"image": own, "build": built, "environment": {"RETINUE_VERSION": version},
                   "volumes": [volume("router-data"), bind("status")]},
        "assistant": {"image": own, "build": built, "depends_on": {"router": after, "egress": after},
                      "volumes": [bind("agents/assistant")]},
        "egress": {"image": SQUID},
        "face": {"image": f"{image}-face:{version}", "build": built},
        "platform": {"image": own, "build": built, "environment": {"RETINUE_VERSION": version},
                     "depends_on": {"lane-private": after},
                     "volumes": [volume("lane-private-work", True), bind("platform.yaml")]},
        "lane-private": {"image": f"retinue-lane-private:{version}", "build": built,
                         "depends_on": {"lane-private-sni": after},
                         "volumes": [volume("lane-private-work"), volume("lane-private-state")]},
        "lane-private-sni": {"image": own, "build": built, "depends_on": {"egress": after}},
        "travel-ops": {"image": "travel-ops:local", "build": {"context": "/opt/travel-ops"}},
    }
    named = {name: {"name": f"retinue_{name}"} for name in ("router-data", "lane-private-work", "lane-private-state")}
    return {"name": "retinue", "services": services, "volumes": named}


def image_id(ref):   # by content: FAKE_SAME names the refs whose content is an earlier tag's
    name, _, tag = ref.rpartition(":") if "@" not in ref else (ref, "", "")
    return "sha256:" + hashlib.sha256(f"{name}:{json.loads(os.environ.get('FAKE_SAME') or '{}').get(ref, tag)}".encode()).hexdigest()


def semver(tag):   # a commit of develop, sha-…: after every release, in the order FAKE_SHAS lists them
    if tag.startswith("sha-"):
        return (999, 0, os.environ.get("FAKE_SHAS", "").split(",").index(tag))
    return tuple(int(part) for part in tag.lstrip("v").split("."))


def by_id(cid):
    found = [name for name, c in state["containers"].items() if c["id"] == cid]
    if not found:
        sys.exit(f"Error: No such container: {cid}")
    return found[0]


def save():
    state_file.write_text(json.dumps(state))


if args[0] == "compose":
    sub = args[args.index("-f") + 2:]
    names = [a for a in sub[1:] if not a.startswith("-")]
    if sub == ["config", "--services"]:
        print("\n".join(config()["services"]))
    elif sub == ["config", "--images"]:
        print("\n".join(dict.fromkeys(s["image"] for s in config()["services"].values())))
    elif sub == ["config", "--format", "json"]:
        print(json.dumps(config()))
    elif sub[0] == "build":
        if os.environ.get("FAKE_BUILD_FAILS"):
            sys.exit("failed to solve")
        for name in names:
            ref = config()["services"][name]["image"]
            state["images"][ref] = image_id(ref)
        save()
    elif sub[0] == "up":
        version, services = os.environ["RETINUE_VERSION"], config()["services"]
        for name in names or services:
            if services[name]["image"] not in state["images"]:
                sys.exit(f"no such image: {services[name]['image']}")
        for name in names or services:
            service = services[name]
            healthy = version not in os.environ.get("FAKE_BROKEN", "").split(",")
            if name == "router":
                if semver(schema.read_text()) > semver(version):
                    healthy = False              # a database a newer version migrated: it refuses to start
                else:
                    schema.write_text(version)   # its migrations, as it opens its database
            state["next"] += 1
            mounts = [{"Type": "volume", "Name": f"retinue_{v['source']}"} if v["type"] == "volume"
                      else {"Type": "bind", "Source": v["source"]} for v in service.get("volumes", [])]
            state["containers"][name] = {"id": hashlib.sha256(str(state["next"]).encode()).hexdigest(), "version": version, "healthy": healthy,
                                         "image": state["images"][service["image"]], "state": "running",
                                         "restarts": 0, "mounts": mounts}
        save()
    elif sub[0] == "stop":
        for name in names:
            state["containers"][name]["state"] = "exited"
        save()
    elif sub[0] == "rm" and "--stop" in sub and "--force" in sub and names:
        for name in names:
            state["containers"].pop(name, None)
        save()
    else:
        sys.exit(f"fake compose: {sub}")
elif args[0] == "pull":
    if args[1] not in os.environ["FAKE_PUBLISHED"].split(","):
        sys.exit(f"manifest unknown: {args[1]}")
    state["images"][args[1]] = image_id(args[1])
    save()
elif args[0] in ("stop", "start", "rm"):
    for cid in args[1:]:
        name = by_id(cid)
        if args[0] == "rm":
            del state["containers"][name]
        else:
            state["containers"][name]["state"] = "exited" if args[0] == "stop" else "running"
    save()
elif args[0] == "ps":
    print("\n".join(c["id"] for c in state["containers"].values()))
elif args[:2] == ["image", "inspect"]:
    if args[-1] not in state["images"]:
        sys.exit(f"Error: No such image: {args[-1]}")
    print(state["images"][args[-1]])
elif args[0] == "inspect":
    name = by_id(args[-1])
    c = state["containers"][name]
    if "Mounts" in args[2]:
        print("\t".join([c["id"], name, json.dumps(c["mounts"])]))
    else:
        health = "" if name in UNHEALTHY else "healthy" if c["healthy"] else "unhealthy"
        print("\t".join([c["id"], f"/retinue-{name}-1", name, str(c["restarts"]), c["state"], health,
                         "2026-11-02T10:00:00.000000000Z", "0", "false"]))
else:
    sys.exit(f"fake docker: {args}")
'''


def fake_env(tmp_path, **fake) -> dict:
    return {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "RETINUE_ROOT": str(tmp_path / "srv"),
            "RETINUE_VOLUME_ROOT": str(tmp_path / "volumes"), "RETINUE_SNAPSHOTS": str(tmp_path / "snapshots"),
            "RETINUE_UPDATE_TIMEOUT": "5", "RETINUE_UPDATE_POLL": "0.1", "PYTHON": sys.executable,
            "FAKE_STATE": str(tmp_path / "state.json"), "FAKE_LOG": str(tmp_path / "docker.log"),
            "FAKE_PUBLISHED": f"{IMAGE}:v1.1.0,{IMAGE}-face:v1.1.0", **fake}


def server(tmp_path, noted=True):
    """A server running v1.0.0 of the release: its stack.env, the volumes (the database's schema among them), the
    host's read-only files, the fake Docker. `noted`: the containers were started by this update.sh, which noted
    each; without, they were started some other way (an update.sh before it)."""
    srv, volumes, bin_ = tmp_path / "srv", tmp_path / "volumes", tmp_path / "bin"
    for folder in (srv / "agents" / "assistant", srv / "status", bin_,
                   *(volumes / f"retinue_{name}" / "_data" for name in
                     ("router-data", "lane-private-work", "lane-private-state"))):
        folder.mkdir(parents=True)
    stack = srv / "stack.env"
    stack.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nRETINUE_IMAGE={IMAGE}\nRETINUE_VERSION=v1.0.0\n")
    stack.chmod(0o600)
    (srv / "agents" / "assistant" / "agent.yaml").write_text("model: one\n")
    (srv / "platform.yaml").write_text("lanes: [private]\n")
    (srv / "status" / "health.json").write_text("{}")
    (volumes / "retinue_router-data" / "_data" / "schema.txt").write_text("v1.0.0")
    (volumes / "retinue_router-data" / "_data" / "router.sqlite").write_text("the owner's rows")
    (volumes / "retinue_lane-private-work" / "_data" / "checkout.txt").write_text("a session's working tree")
    (volumes / "retinue_lane-private-state" / "_data" / "journal.jsonl").write_text("a turn's lines\n")
    docker = bin_ / "docker"
    docker.write_text(FAKE_DOCKER.replace("{python}", sys.executable))
    docker.chmod(0o755)
    (tmp_path / "state.json").write_text(json.dumps({"images": {}, "containers": {}, "next": 0}))
    env = {**fake_env(tmp_path, FAKE_PUBLISHED=f"{IMAGE}:v1.0.0,{IMAGE}-face:v1.0.0,{SQUID},travel-ops:local"),
           "RETINUE_IMAGE": IMAGE, "RETINUE_VERSION": "v1.0.0"}
    for ref in (f"{IMAGE}:v1.0.0", f"{IMAGE}-face:v1.0.0", SQUID, "travel-ops:local"):
        subprocess.run([str(docker), "pull", ref], env=env, check=True)
    up = [str(docker), "compose", "-p", "retinue", "-f", "compose.yml"]
    subprocess.run([*up, "build", "lane-private"], env=env, check=True)
    subprocess.run([*up, "up", "-d"], env=env, check=True)
    if noted:
        config, plan = tmp_path / "config.json", tmp_path / "plan.json"
        config.write_text(subprocess.run([*up, "config", "--format", "json"], env=env, check=True,
                                         capture_output=True, text=True).stdout)
        plan.write_text(json.dumps({"start": OWN + PINNED, "stop": {}}))
        subprocess.run([sys.executable, str(CHANGED), "note", "v1.0.0", str(config), str(plan)], env=env, check=True,
                       capture_output=True)
        config.unlink()
        plan.unlink()
    (tmp_path / "docker.log").unlink()
    return srv, volumes


def update(tmp_path, tag, **fake):
    run = subprocess.run(["bash", str(UPDATE), tag], capture_output=True, text=True, env=fake_env(tmp_path, **fake),
                         timeout=120)
    assert TOKEN not in run.stdout + run.stderr, "stack.env's values are never printed"
    log = tmp_path / "docker.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return run, calls, json.loads((tmp_path / "state.json").read_text())


def same(*refs: str, as_tag: str = "v1.0.0") -> str:
    """FAKE_SAME: these images of the new tag hold what as_tag's held — CI re-tagged them, or the lane's build was
    all cache."""
    return json.dumps({ref: as_tag for ref in refs})


UNCHANGED = (f"{IMAGE}:v1.1.0", f"{IMAGE}-face:v1.1.0", "retinue-lane-private:v1.1.0")


def ids(state: dict) -> dict:
    return {name: c["id"] for name, c in state["containers"].items()}


def versions(state: dict) -> dict:
    """The version each service's container started under."""
    return {name: c["version"] for name, c in state["containers"].items()}


def healthy(state: dict) -> bool:
    return sorted(state["containers"]) == sorted(OWN + PINNED) and all(
        c["healthy"] and c["state"] == "running" for c in state["containers"].values())


def stack_env(srv: Path) -> dict:
    assert oct((srv / "stack.env").stat().st_mode & 0o777) == "0o600"
    return dict(line.split("=", 1) for line in (srv / "stack.env").read_text().splitlines())


def compose(calls: list[dict], command: str) -> list[int]:
    """The positions of the compose calls whose subcommand is `command`."""
    return [n for n, call in enumerate(calls) if call["args"][0] == "compose"
            and call["args"][call["args"].index("-f") + 2] == command]


def stopped(calls: list[dict]) -> list[int]:
    """The positions of every call that stops or removes a container."""
    return compose(calls, "stop") + compose(calls, "rm") + compose(calls, "down") + [
        n for n, call in enumerate(calls) if call["args"][0] in ("stop", "rm")]


def named(call: dict) -> list[str]:
    """The services a compose call names."""
    return [a for a in call["args"][call["args"].index("-f") + 3:] if not a.startswith("-")]


def test_an_update_to_a_tag_ends_healthy_with_that_version(tmp_path):
    srv, volumes = server(tmp_path)
    state = json.loads((tmp_path / "state.json").read_text())
    state["containers"]["egress"]["restarts"] = 3      # restarted long before: not this update's crash
    (tmp_path / "state.json").write_text(json.dumps(state))
    before = ids(state)
    run, calls, state = update(tmp_path, "v1.1.0")
    assert run.returncode == 0, run.stdout + run.stderr
    assert healthy(state) and versions(state) == {**dict.fromkeys(OWN, "v1.1.0"), **dict.fromkeys(PINNED, "v1.0.0")}
    assert {name: ids(state)[name] == before[name] for name in before} == {
        **dict.fromkeys(OWN, False), **dict.fromkeys(PINNED, True)}, \
        "every service of Retinue's images starts anew; the pinned ones, which nothing changed, keep running"
    assert stack_env(srv) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "RETINUE_IMAGE": IMAGE, "RETINUE_VERSION": "v1.1.0"}, \
        "the version is recorded where compose and the next update read it; the owner's lines are kept"
    assert (volumes / "retinue_router-data" / "_data" / "schema.txt").read_text() == "v1.1.0", "migrated"
    pulls = [call["args"][1] for call in calls if call["args"][0] == "pull"]
    assert pulls == [f"{IMAGE}:v1.1.0", f"{IMAGE}-face:v1.1.0"], "Retinue's own images of the tag, nothing pinned"
    stop, = compose(calls, "stop")
    up, = compose(calls, "up")
    build, = compose(calls, "build")
    assert named(calls[build]) == ["lane-private"] and calls[build]["version"] == "v1.1.0", \
        "the image no registry has is built under the tag, and only it: the pulled ones are not rebuilt"
    assert "retinue-lane-private:v1.1.0" in state["images"]
    assert max(n for n, call in enumerate(calls) if call["args"][0] == "pull") < build < stop < up, \
        "every image is in before anything is stopped"
    assert sorted(named(calls[stop])) == OWN and calls[stop]["version"] == "v1.0.0", "stopped as the version before"
    assert calls[up]["version"] == "v1.1.0" and calls[up]["image"] == IMAGE
    assert {"--no-build", "--no-deps"} <= set(calls[up]["args"]) and sorted(named(calls[up])) == OWN
    snapshot, = (tmp_path / "snapshots").iterdir()
    assert snapshot.name.endswith("v1.0.0-to-v1.1.0"), "named for what it is"
    assert (snapshot / "stack.env").read_text().endswith("RETINUE_VERSION=v1.0.0\n")
    names = sorted(path.name for path in (snapshot / "volumes").iterdir())
    assert names == ["retinue_lane-private-state.tar", "retinue_router-data.tar"], \
        "the stopped services' volumes; the lanes' working trees are the owner's checkouts, not state"
    assert oct((srv / "started.json").stat().st_mode & 0o777) == "0o600"
    out = run.stdout
    for step in ("health before", "pull", "what v1.1.0 changes", "stop", "snapshot", "start v1.1.0", "health of v1.1.0"):
        assert step in out, f"each step is printed: {step}"
    for name in OWN:
        assert f"{name}: {before[name][:12]} -> {ids(state)[name][:12]}" in out, "each container's id, before and after"


def test_a_version_that_changes_only_the_face_leaves_the_room_running(tmp_path):
    srv, volumes = server(tmp_path)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_SAME=same(f"{IMAGE}:v1.1.0", "retinue-lane-private:v1.1.0"))
    assert run.returncode == 0, run.stdout + run.stderr
    assert healthy(state) and stack_env(srv)["RETINUE_VERSION"] == "v1.1.0"
    assert {name for name in before if ids(state)[name] != before[name]} == {"face"}, \
        "the room, its guard, the platform and the router keep their containers"
    assert f"keeps running: lane-private {before['lane-private'][:12]}" in run.stdout
    up, = compose(calls, "up")
    assert named(calls[up]) == ["face"]
    assert not list((tmp_path / "snapshots").glob("*/volumes/*")), "the face holds no data: nothing to snapshot"


def test_a_change_to_the_platform_takes_the_room_along_and_nothing_else(tmp_path):
    """The platform confirms the room's journal, which then forgets those lines: the two are stopped, kept and put
    back together, by the volume they share."""
    srv, volumes = server(tmp_path)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    (srv / "platform.yaml").write_text("lanes: [private, code]\n")   # what setup.sh writes for the new version
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_SAME=same(*UNCHANGED))
    assert run.returncode == 0, run.stdout + run.stderr
    assert healthy(state)
    assert {name for name in before if ids(state)[name] != before[name]} == {"platform", "lane-private"}
    snapshot, = (tmp_path / "snapshots").iterdir()
    assert sorted(path.name for path in (snapshot / "volumes").iterdir()) == ["retinue_lane-private-state.tar"]


def test_a_version_that_changes_nothing_that_runs_restarts_nothing(tmp_path):
    srv, volumes = server(tmp_path)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    (srv / "status" / "health.json").write_text('{"containers": []}')   # read as it goes, never only at a start
    (srv / "agents" / "assistant" / "agent.yaml").write_text("model: one\n")   # setup.sh's same bytes again
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_SAME=same(*UNCHANGED))
    assert run.returncode == 0, run.stdout + run.stderr
    assert ids(state) == before and not stopped(calls) and not compose(calls, "up")
    assert stack_env(srv)["RETINUE_VERSION"] == "v1.1.0", "the version is still the one recorded"
    assert "nothing was restarted" in run.stdout


def test_a_container_that_update_sh_did_not_start_is_restarted(tmp_path):
    """The first update with changed.py, or a container someone recreated by hand: what it started with is not
    known, so it starts anew."""
    srv, volumes = server(tmp_path, noted=False)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_SAME=same(*UNCHANGED))
    assert run.returncode == 0, run.stdout + run.stderr
    assert healthy(state) and all(ids(state)[name] != before[name] for name in before)


def test_a_changed_read_only_file_restarts_the_service_that_reads_it(tmp_path):
    srv, volumes = server(tmp_path)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    (srv / "agents" / "assistant" / "agent.yaml").write_text("model: two\n")
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_SAME=same(*UNCHANGED))
    assert run.returncode == 0, run.stdout + run.stderr
    assert {name for name in before if ids(state)[name] != before[name]} == {"assistant"}


def test_an_update_that_fails_health_goes_back_to_the_previous_tag(tmp_path):
    srv, volumes = server(tmp_path)
    before = ids(json.loads((tmp_path / "state.json").read_text()))
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_BROKEN="v1.1.0")
    assert run.returncode == 1, run.stdout + run.stderr
    ups = compose(calls, "up")
    assert [calls[n]["version"] for n in ups] == ["v1.1.0", "v1.0.0"], "the tag, then the one before"
    assert sorted(named(calls[ups[1]])) == OWN, "only what was stopped is started again"
    assert healthy(state) and versions(state) == dict.fromkeys(OWN + PINNED, "v1.0.0"), \
        "healthy again: the snapshot was put back before the old version opened its database"
    assert all(ids(state)[name] == before[name] for name in PINNED), "what was never stopped is left alone"
    data = volumes / "retinue_router-data" / "_data"
    assert (data / "schema.txt").read_text() == "v1.0.0" and (data / "router.sqlite").read_text() == "the owner's rows"
    assert stack_env(srv)["RETINUE_VERSION"] == "v1.0.0"
    assert sorted((srv / "update-touched").read_text().split()) == OWN, "deploy.sh is told what was started again"
    assert "v1.0.0" in run.stdout and "router: running, unhealthy" in run.stdout, "what was not healthy"
    assert (tmp_path / "snapshots").is_dir() and any((tmp_path / "snapshots").iterdir()), "the snapshot stays"


def test_an_update_to_a_tag_that_was_not_published_changes_nothing(tmp_path):
    srv, volumes = server(tmp_path)
    before = (srv / "stack.env").read_text()
    run, calls, state = update(tmp_path, "v1.2.0")
    assert run.returncode != 0 and "v1.2.0" in run.stderr
    assert not stopped(calls) and not compose(calls, "up"), "nothing stopped"
    assert (srv / "stack.env").read_text() == before and set(versions(state).values()) == {"v1.0.0"}


def test_an_update_whose_image_does_not_build_changes_nothing(tmp_path):
    srv, volumes = server(tmp_path)
    before = (srv / "stack.env").read_text()
    run, calls, state = update(tmp_path, "v1.1.0", FAKE_BUILD_FAILS="1")
    assert run.returncode != 0
    assert not stopped(calls) and not compose(calls, "up"), "nothing stopped"
    assert (srv / "stack.env").read_text() == before and set(versions(state).values()) == {"v1.0.0"}


def workflow_steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_the_release_workflow_parses_and_pins_every_action_by_commit():
    workflow = yaml.safe_load(RELEASE.read_text())
    trigger = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    assert trigger["push"] == {"tags": ["v*.*.*"]}, "a release is a tag"
    inputs = trigger["workflow_call"]["inputs"]
    assert list(inputs) == ["commit"] and {k: v for k, v in inputs["commit"].items() if k != "description"} == {
        "type": "string", "required": True}, "or a commit of develop, when CI calls it (tests/test_follow_develop.py)"
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
    here = []
    for name, service in SERVICES.items():
        image = service.get("image", "")
        if re.fullmatch(r"retinue(-[a-z-]+)?:\$\{RETINUE_VERSION:-local\}", image):
            assert "build" in service, f"{name}: an image no registry has is built here (update.sh)"
            here.append(image.split(":")[0])
            continue
        assert not re.match(r"retinue(-[a-z-]+)?:", image), \
            f"{name}: a Retinue image by a fixed name stays behind when the stack moves to a release or a commit"
    assert here == ["retinue-lane-private"], "only the lane's, which carries Claude Code, is kept out of the registry"
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
    assert sorted(own) == ["retinue", "retinue-face", "retinue-pages-build"]
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


def test_an_image_ci_may_take_from_an_earlier_commit_names_every_path_its_build_reads():
    """A commit of develop gets an earlier commit's image when its inputs are the same (release.yml): a path the build
    reads and the inputs leave out would ship the old image after a change to it."""
    for entry in yaml.safe_load(RELEASE.read_text())["jobs"]["images"]["strategy"]["matrix"]["include"]:
        inputs = entry["inputs"].split()

        def covered(path: str) -> bool:
            return any(path == given or path.startswith(given + "/") for given in inputs)

        context, dockerfile = entry["context"], entry["dockerfile"]
        assert covered(dockerfile), f"{entry['image']}: its Dockerfile"
        # BuildKit reads <Dockerfile>.dockerignore when there is one, the context's .dockerignore otherwise
        ignore = next((path for path in (f"{dockerfile}.dockerignore", f"{context}/.dockerignore")
                       if (ROOT / path).exists()), None)
        if ignore:
            assert covered(posixpath.normpath(ignore)), f"{entry['image']}: {ignore}, which says what is sent"
        for line in (ROOT / dockerfile).read_text().splitlines():
            words = line.split()
            if not words or words[0].upper() not in ("COPY", "ADD") or any(w.startswith("--from=") for w in words):
                continue
            for source in (w for w in words[1:-1] if not w.startswith("--")):
                path = posixpath.normpath(posixpath.join(context, source))
                assert covered(path), f"{entry['image']}: copies {source}, which is not among its inputs"
