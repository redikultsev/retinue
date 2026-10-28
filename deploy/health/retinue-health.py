#!/usr/bin/env python3
"""The stack's containers, looked at every two minutes. Runs as root from retinue-health.service, with the host's
python3 (stdlib only).

For each container of the compose project: its state, its healthcheck's verdict, how many times Docker restarted it
and since when it runs — into <root>/status/health.json, which the router reads through a read-only mount. The router
tells the owner when something goes bad and when it is well again, and the morning summary says how it stands.

This script tells nobody anything itself: the host holds no bot token, by design (the token is the router's, in
Dokploy's environment). So the router down cannot be told by anyone; the owner learns it from the morning summary
that does not come. The other way round is covered: this timer stopped, the router sees the file grow old.

Paths come from the environment, so the same script runs in the tests with a fake `docker`.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("RETINUE_ROOT", "/srv/retinue"))
DOCKER = os.environ.get("DOCKER", "docker")
PROJECT = os.environ.get("RETINUE_PROJECT", "retinue")
STATUS = ROOT / "status" / "health.json"
# Only these fields of each container, never `{{json .}}`: the full record carries the environment, secrets included.
# `index .State "Health"`, not `.State.Health`: a container with no healthcheck has no such key, and the template fails.
FIELDS = "\t".join(["{{.Id}}", "{{.Name}}", '{{index .Config.Labels "com.docker.compose.service"}}',
                    "{{.RestartCount}}", "{{.State.Status}}", '{{with index .State "Health"}}{{.Status}}{{end}}',
                    "{{.State.StartedAt}}", "{{.State.ExitCode}}", "{{.State.OOMKilled}}"])


class Vanished(Exception):
    """The container went between `ps` and `inspect`: a deploy recreating it, not news."""


def docker(*args: str) -> str:
    run = subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=20)
    if run.returncode:
        said = (run.stderr.strip().splitlines() or [f"docker exited {run.returncode}"])[-1][:300]
        raise Vanished(said) if "No such" in said else RuntimeError(said)
    return run.stdout


def epoch(stamp: str) -> float | None:
    """Docker's RFC 3339 time with nanoseconds -> seconds; None for a container never started (year 1)."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", stamp.strip())
    if not match or match.group(1).startswith("0001-"):
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    return datetime.fromisoformat(f"{match.group(1)}.{(match.group(2) or '0')[:6]:0<6}{zone}").timestamp()


def containers() -> list[dict]:
    # The stack's own containers, not `compose run`'s one-offs (a probe, an admin command).
    ids = docker("ps", "-a", "-q", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                 "--filter", "label=com.docker.compose.oneoff=False").split()
    if not ids:
        return []
    out = []
    for container in ids:  # one at a time: a container gone meanwhile is skipped, not the whole report lost
        try:
            line = docker("inspect", "--format", FIELDS, container).strip()
        except Vanished:
            continue
        cid, name, service, restarts, state, health, started, code, oom = line.split("\t")
        out.append({"name": service or name.lstrip("/"), "container": name.lstrip("/"), "id": cid[:12],
                    "state": state, "health": health, "restarts": int(restarts), "since": epoch(started),
                    "exit_code": int(code), "oom": oom == "true"})
    return sorted(out, key=lambda c: c["name"])


def write(report: dict) -> None:
    STATUS.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False) + "\n")
    tmp.chmod(0o644)  # the router runs as an unprivileged user and reads it through a read-only mount
    os.replace(tmp, STATUS)


def main() -> int:
    try:
        report = {"checked": time.time(), "containers": containers()}
    except Exception as exc:  # docker not answering is news too: the router reports it like a container gone bad
        write({"checked": time.time(), "error": str(exc)[:300] or type(exc).__name__})
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
