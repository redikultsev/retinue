#!/usr/bin/env python3
"""What a move to another version restarts (deploy/update.sh, docs/install.md «Updating»).

A service is restarted when what it would start with differs from what its running container started with: its
definition in compose.yml (the version itself left out), its image (by id: an image CI re-tagged because nothing in it
changed, or the lane's image rebuilt here from cache, is the same image) or a read-only file the host gives it
(setup.sh writes them, the same bytes on every run). Each start is noted in $RETINUE_ROOT/started.json; a container
started some other way, or a service never noted, is restarted. So is every service that shares a volume with a
restarted one (the platform and the room: the platform confirms the room's journal, which then forgets those lines, so
the two are stopped, kept and put back together) or depends on one. The rest keep running, their turns uncut.

  changed.py plan <tag> <config.json>                what to stop and start, as JSON
  changed.py note <tag> <config.json> <plan.json>    the plan's services started now under <tag>; old -> new ids
  changed.py forget <service>...                     these were started some other way (the way back)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("RETINUE_ROOT", "/srv/retinue"))
DOCKER = os.environ.get("DOCKER", "docker")
PROJECT = "retinue"
NOTED = ROOT / "started.json"
# Read-only folders a service reads as it goes, never only at its start: a change in them restarts nothing. A folder
# missing here costs a restart, never a stale service.
LIVE = ("status", "owner-memory", "skills", "memory/hub.git", "pages", "settings/status")
WORK = re.compile(r"_lane-[a-z]+-work$")  # the lanes' checkouts: no state, never in a snapshot (update.sh)
# Only these fields of a container, never `{{json .}}`: the full record carries the environment, secrets included.
FIELDS = '{{.Id}}\t{{index .Config.Labels "com.docker.compose.service"}}\t{{json .Mounts}}'


def docker(*args: str) -> str:
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, check=True).stdout


def running() -> dict[str, dict]:
    """The stack's containers by service: their id and their volumes and folders."""
    ids = docker("ps", "-a", "-q", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                 "--filter", "label=com.docker.compose.oneoff=False").split()
    out = {}
    for container in ids:
        cid, service, mounts = docker("inspect", "--format", FIELDS, container).strip().split("\t", 2)
        mounts = json.loads(mounts) or []
        out[service] = {"id": cid, "volumes": sorted(m["Name"] for m in mounts if m.get("Type") == "volume"),
                        "binds": sorted(m["Source"] for m in mounts if m.get("Type") == "bind")}
    return out


def image_id(ref: str) -> str:
    try:
        return docker("image", "inspect", "--format", "{{.Id}}", ref).strip()
    except subprocess.CalledProcessError:
        return ""  # not here: never the same as what runs


def content(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    elif path.is_dir():
        for file in sorted(path.rglob("*")):
            if file.is_file():
                digest.update(file.relative_to(path).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    else:
        digest.update(b"none")
    return digest.hexdigest()


def live(source: str) -> bool:
    try:
        rel = Path(source).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return False
    return any(rel == folder or rel.startswith(folder + "/") for folder in LIVE)


def fingerprint(service: dict, tag: str) -> str:
    """What the service starts with: its definition with the version left out, its image, its read-only files."""
    files = {v["source"]: content(Path(v["source"])) for v in service.get("volumes", [])
             if v.get("type") == "bind" and v.get("read_only") and not live(v["source"])}
    text = json.dumps(service, sort_keys=True).replace(tag, "@version")
    return hashlib.sha256(json.dumps([text, image_id(service.get("image", "")), files]).encode()).hexdigest()


def volumes(config: dict, name: str, containers: dict) -> set[str]:
    named = config.get("volumes") or {}
    own = {(named.get(v["source"]) or {}).get("name", f"{PROJECT}_{v['source']}")
           for v in (config["services"].get(name) or {}).get("volumes", []) if v.get("type") == "volume" and v.get("source")}
    return own | set((containers.get(name) or {}).get("volumes", []))


def noted() -> dict:
    return json.loads(NOTED.read_text()) if NOTED.exists() else {}


def save(seen: dict) -> None:
    tmp = NOTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, NOTED)


def plan(tag: str, config: dict) -> dict:
    services, containers, seen = config["services"], running(), noted()
    changed = {name for name in containers if name not in services}  # gone from the file, or its profile turned off
    for name, service in services.items():
        was = seen.get(name) or {}
        if (name not in containers or was.get("container") != containers[name]["id"]
                or was.get("fingerprint") != fingerprint(service, tag)):
            changed.add(name)
    while True:  # what goes along: a shared volume, or a dependency
        shared = set().union(*(volumes(config, name, containers) for name in changed))
        more = {name for name, service in services.items() if name not in changed
                and (set(service.get("depends_on") or {}) & changed or volumes(config, name, containers) & shared)}
        if not more:
            break
        changed |= more
    stop = {name: containers[name] for name in sorted(changed) if name in containers}
    return {"start": sorted(changed & set(services)),
            "stop": {name: c["id"] for name, c in stop.items()},
            "keep": {name: c["id"] for name, c in sorted(containers.items()) if name not in changed},
            "volumes": sorted({v for c in stop.values() for v in c["volumes"] if not WORK.search(v)}),
            "binds": sorted({b for c in stop.values() for b in c["binds"]})}


def main(argv: list[str]) -> int:
    if argv[:1] == ["plan"] and len(argv) == 3:
        print(json.dumps(plan(argv[1], json.loads(Path(argv[2]).read_text()))))
    elif argv[:1] == ["note"] and len(argv) == 4:
        tag, config, was = argv[1], json.loads(Path(argv[2]).read_text()), json.loads(Path(argv[3]).read_text())
        containers, seen = running(), noted()
        for name in was["start"]:
            if name in containers:
                new = containers[name]["id"]
                seen[name] = {"container": new, "fingerprint": fingerprint(config["services"][name], tag)}
                print(f"   {name}: {was['stop'].get(name, 'none')[:12]} -> {new[:12]}")
        save(seen)
    elif argv[:1] == ["forget"]:
        seen = noted()
        for name in argv[1:]:
            seen.pop(name, None)
        save(seen)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
