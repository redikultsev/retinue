"""The open proxy as it runs (the room's, lane-private-egress): the pinned Squid image with deploy/egress's files,
asked by a client on its network.
It answers what reading the config cannot: that Squid starts with the operator's lists still empty, refuses a name
that resolves inside and an IPv4-mapped IPv6 literal, and takes `dstdomain -n`. No internet is needed: every case is
a refusal, or an address Squid lets through and then cannot reach. Skipped without Docker.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EGRESS = ROOT / "deploy" / "egress"
IMAGE = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())["services"]["lane-private-egress"]["image"]


def _docker() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason="needs Docker")

# One CONNECT per target over bash's /dev/tcp: the proxy's status line, or `none` when Squid let it through and is
# still trying to reach it (an address nobody answers on); `unreachable-proxy` when the client never got to Squid.
CLIENT = r'''
for target in "$@"; do
  exec 3<>/dev/tcp/$PROXY/3128 || { echo "$target unreachable-proxy"; continue; }
  printf 'CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' "$target" "$target" >&3
  line=$(timeout 4 head -1 <&3 | tr -d '\r')
  echo "$target ${line:-none}"
  exec 3>&-
done
'''


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=600)
    assert not check or done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    """Two proxies on one network: `bare` with the repository's files as setup.sh writes them first (the operator's
    two lists still without an entry), `filled` with a zone and an address in them."""
    tag = uuid.uuid4().hex[:8]
    net = f"lwp-{tag}"
    filled = tmp_path_factory.mktemp("filled")
    (filled / "ips").write_text((EGRESS / "open-deny-ips.txt").read_text() + "203.0.113.7/32\n")
    (filled / "domains").write_text((EGRESS / "open-deny-domains.txt").read_text() + ".example.test\n")
    names = {"bare": f"lwp-bare-{tag}", "filled": f"lwp-filled-{tag}"}
    docker("network", "create", net)
    try:
        for kind, name in names.items():
            ips = filled / "ips" if kind == "filled" else EGRESS / "open-deny-ips.txt"
            domains = filled / "domains" if kind == "filled" else EGRESS / "open-deny-domains.txt"
            docker("run", "-d", "--name", name, "--network", net,
                   "-v", f"{EGRESS / 'open-squid.conf'}:/etc/squid/squid.conf:ro",
                   "-v", f"{EGRESS / 'open-deny-nets.txt'}:/etc/squid/deny-nets.txt:ro",
                   "-v", f"{ips}:/etc/squid/deny-ips.txt:ro", "-v", f"{domains}:/etc/squid/deny-domains.txt:ro", IMAGE)
        for name in names.values():
            for _ in range(60):
                if subprocess.run(["docker", "exec", name, "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/3128"],
                                  capture_output=True).returncode == 0:
                    break
                time.sleep(0.5)
            else:
                pytest.fail(f"{name} never listened: {docker('logs', name, check=False)}")

        def ask(kind: str, *targets: str) -> dict[str, str]:
            out = docker("run", "--rm", "--network", net, "-e", f"PROXY={names[kind]}", "--entrypoint", "bash", IMAGE,
                         "-c", CLIENT, "client", *targets)
            return dict(line.split(" ", 1) for line in out.splitlines())

        yield ask, names
    finally:
        for name in names.values():
            docker("rm", "-f", name, check=False)
        docker("network", "rm", net, check=False)


def refused(answer: str) -> bool:
    return answer.startswith("HTTP/1.1 403")


def let_through(answer: str) -> bool:
    """Squid accepted the CONNECT and is still reaching for the address (or says it could not): not a refusal."""
    return answer == "none" or answer.startswith(("HTTP/1.1 200", "HTTP/1.1 503"))


def test_squid_starts_with_the_operators_lists_still_empty_and_refuses_everything_inside(proxy):
    ask, names = proxy
    answers = ask("bare", "10.8.0.1:443", "172.17.0.1:443", "192.168.1.1:443", "127.0.0.1:443", "169.254.169.254:443",
                  "100.64.0.1:443", "[::1]:443", "[fd00::1]:443", "[fe80::1]:443", "[64:ff9b::a08:1]:443",
                  "example.com:80", "example.com:8443")
    assert all(refused(answer) for answer in answers.values()), answers


def test_a_name_that_resolves_inside_and_an_ipv4_mapped_literal_are_refused(proxy):
    """The proxy's own container name resolves to its address on the Docker network — a public-looking name in an
    attack, a private address all the same. `::ffff:10.8.0.1` is 10.8.0.1 to Squid, which keeps IPv4 as mapped."""
    ask, names = proxy
    answers = ask("bare", f"{names['filled']}:443", "[::ffff:10.8.0.1]:443", "[::ffff:a08:1]:443",
                  "[::ffff:127.0.0.1]:443")
    assert all(refused(answer) for answer in answers.values()), answers


def test_a_public_address_is_let_through_and_the_operators_entries_are_refused(proxy):
    """192.0.2.1 (documentation space, nobody answers) is in no list: Squid goes to reach it rather than refusing. The
    filled lists refuse the zone and the address; `dstdomain -n` never looks an address literal up in reverse."""
    ask, _ = proxy
    assert let_through(ask("bare", "192.0.2.1:443")["192.0.2.1:443"])
    answers = ask("filled", "example.test:443", "hub.in.example.test:443", "203.0.113.7:443", "192.0.2.1:443")
    assert refused(answers["example.test:443"]) and refused(answers["hub.in.example.test:443"]), answers
    assert refused(answers["203.0.113.7:443"]) and let_through(answers["192.0.2.1:443"]), answers
