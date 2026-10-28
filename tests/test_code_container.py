"""The code path as it runs on the server, in a Linux container with the lanes' git: the platform (uid 10007) owns the
mirrors and its outbox, the room (uid 10005) owns `/work`; the mirrors are one folder mounted twice — read-write at
`/code/mirrors`, read-only at `/lane/mirrors` — and `/etc/gitconfig` trusts `/lane/mirrors/*` as the lane image's
does, on the lanes' base image. What a Mac cannot show: the room's objects copied across the two users, git's
ownership checks between them, the lane's git new enough for `/lane/mirrors/*`, and the room's `git pull` from a mirror it does not own. Skipped without
Docker."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = """FROM python:3.12-slim-trixie
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null && rm -rf /var/lib/apt/lists/*
RUN pip install -q aiohttp pyyaml
RUN useradd -u 10007 platform && useradd -u 10005 lane \\
 && printf '[safe]\\n\\tdirectory = /lane/mirrors/*\\n' > /etc/gitconfig
"""

SCRIPT = r'''
set -eu
cd /tmp
git version | awk '{split($3, v, "."); exit !(v[1] > 2 || (v[1] == 2 && v[2] >= 46))}'
export PYTHONPATH=/src
install -d -o platform -g platform /code/mirrors /platform /remotes
install -d -o lane -g lane /work
# The remote: a bare repository the platform reaches over file:// (the server's is GitHub over https).
su platform -s /bin/sh -c '
  git init -q --bare -b main /remotes/r.git && git clone -q /remotes/r.git /tmp/seed && cd /tmp/seed \
  && echo hi > README.md && git add . && git -c user.name=t -c user.email=t@e commit -qm init \
  && git push -q origin HEAD:main'
su platform -s /bin/sh -c 'python3 - <<EOF
from retinue.code import CodeRepos
from retinue.config import CodeConfig, CodeRepo
c = CodeRepos(CodeConfig(repos=[CodeRepo("r", "file:///remotes/r.git", "k", secret="")], mirrors="/code/mirrors",
                         outbox="/platform/code", author="O <o@e.x>"), "/work")
assert c.fetch("r") and c.ready("r")
print("mirror", "ok")
EOF'
# The room clones the mirror it does not own, as the lane host does (umask 002), commits, pulls.
su lane -s /bin/sh -c 'umask 002; python3 - <<EOF
from pathlib import Path
from retinue.lane_host import ensure_clone
assert ensure_clone(Path("/work"), Path("/lane/mirrors"), "code", "r", "O <o@e.x>")
EOF
cd /work/code/r && git pull -q --ff-only && git checkout -qb feature && echo a > a.txt && git add a.txt \
  && git commit -qm A && echo cloned-and-committed
# Then hostile: a hook and a config that try to run and redirect.
printf "#!/bin/sh\ntouch /tmp/ran-hook\n" > .git/hooks/reference-transaction && chmod +x .git/hooks/reference-transaction
printf "[core]\n\tfsmonitor = touch /tmp/ran-fsmonitor\n[url \"/tmp/evil.git\"]\n\tinsteadOf = file:///remotes/r.git\n" >> .git/config'
su platform -s /bin/sh -c 'python3 - <<EOF
from retinue.code import CodeRepos, Refused
from retinue.config import CodeConfig, CodeRepo
from pathlib import Path
c = CodeRepos(CodeConfig(repos=[CodeRepo("r", "file:///remotes/r.git", "k", secret="")], mirrors="/code/mirrors",
                         outbox="/platform/code", author="O <o@e.x>"), "/work")
look = c.look("r", "refs/retinue/cards/c1")
assert (look.branch, look.ahead, [f["path"] for f in look.files]) == ("feature", 1, ["a.txt"]), look
done = c.push("r", "refs/retinue/cards/c1", look.sha, "retinue/c1-feature")
assert done.ok, done
print("pushed", look.sha)
EOF'
test "$(git --git-dir=/remotes/r.git rev-parse refs/heads/retinue/c1-feature)" = "$(su lane -s /bin/sh -c 'git -C /work/code/r rev-parse HEAD')"
ls /tmp/ran-* 2>/dev/null && { echo "something of the room ran"; exit 1; } || true
# Alternates added by the room: refused before git reads them.
su lane -s /bin/sh -c 'echo /platform/code/r.git/objects > /work/code/r/.git/objects/info/alternates'
su platform -s /bin/sh -c 'python3 - <<EOF
from retinue.code import CodeRepos, Refused
from retinue.config import CodeConfig, CodeRepo
c = CodeRepos(CodeConfig(repos=[CodeRepo("r", "file:///remotes/r.git", "k", secret="")], mirrors="/code/mirrors",
                         outbox="/platform/code", author="O <o@e.x>"), "/work")
try:
    c.look("r", "refs/retinue/cards/c2")
except Refused as exc:
    print("refused", exc)
else:
    raise SystemExit("alternates were read")
EOF'
echo ALL-OK
'''


def _docker() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason="needs Docker")


def test_the_code_path_across_the_platforms_and_the_rooms_users(tmp_path):
    tag = "retinue-code-probe:" + hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        built = subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=DOCKERFILE, text=True,
                               capture_output=True, timeout=900)
        assert built.returncode == 0, built.stderr
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    done = subprocess.run(
        ["docker", "run", "--rm", "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined", "-v", f"{ROOT}:/src:ro", "--tmpfs", "/code:exec,mode=0755",
         "--tmpfs", "/lane:exec,mode=0755", tag, "sh", "-c",
         # One folder, two mounts: the platform's read-write, the room's read-only.
         "install -d -m 755 /code/mirrors /lane/mirrors && mount --bind /code/mirrors /lane/mirrors && "
         "mount -o remount,bind,ro /lane/mirrors && " + SCRIPT],
        capture_output=True, text=True, timeout=600)
    assert done.returncode == 0 and "ALL-OK" in done.stdout, done.stdout[-3000:] + done.stderr[-3000:]
    assert "mirror ok" in done.stdout and "cloned-and-committed" in done.stdout and "refused" in done.stdout
