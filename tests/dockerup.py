"""Whether Docker answers, for the tests that need it to skip without it.

A daemon that does not answer — Docker Desktop stuck on a Mac — counts as none: `docker info` waits for it without
end, and the whole run with it, before a single test starts.
"""

from __future__ import annotations

import shutil
import subprocess

TIMEOUT_S = 20  # a cold Docker Desktop answers in a few seconds; a stuck one never does


def docker_up() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=TIMEOUT_S).returncode == 0
    except subprocess.TimeoutExpired:
        return False
