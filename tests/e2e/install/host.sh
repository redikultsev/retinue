#!/usr/bin/env bash
# A clean install rehearsed on a fresh host, the way docs/install.md tells a person to do it, with test values only:
# the host's packages, Docker, deploy/setup.sh with every switch that runs without a real outside secret, the images
# built here, the stack started, and every service it starts waited on until deploy/health calls it healthy.
# As root, on the host itself (.github/workflows/install.yml runs it in a Debian 13 VM and on an arm64 runner):
#
#   sudo bash tests/e2e/install/host.sh /opt/retinue
#
# Nothing here is a real secret: the Telegram id is the doc's example, the tokens are made-up strings. What cannot be
# healthy without a real outside secret is named in SKIP with the reason, and not waited on.
set -euo pipefail

REPO=$(cd "${1:?usage: host.sh <checkout>}" && pwd)
ROOT=/srv/retinue
STACK=$ROOT/stack.env
TIMEOUT=${RETINUE_REHEARSAL_TIMEOUT:-900}
export DEBIAN_FRONTEND=noninteractive
step() { printf '\n== %s\n' "$*"; }
compose() { docker compose -p retinue --env-file "$STACK" -f "$REPO/deploy/compose.yml" "$@"; }

# Services of the profiles turned on below that cannot be healthy here, and why. Not waited on; their state is printed.
# None now: every one of them comes up healthy on test values.
declare -A SKIP=()
# Healthy here, but the half of their work that needs the outside secret is not exercised (printed with the result).
UNTRIED=(
  "router: Telegram — getMe refuses the made-up bot token, so only its Matrix channel starts (logged below)"
  "tg-business: polls the Bot API with a made-up Business bot token; refused, retried"
  "assistant, platform, lane-private: no model call — no real CLAUDE_CODE_OAUTH_TOKEN (the container probe needs it)"
  "collector, mail-send: no Google keys (deploy/mail/login.py on the owner's computer)"
  "speech: no ELEVENLABS_API_KEY; backup: backup.env unfilled, so restic is installed but its timer is off"
)
# Profiles not turned on at all, and why (printed, so the gap is in the log, not only in this file).
NOT_ON=(
  "travel (travel-ops, travel-watch): built from the owner's own travel-ops checkout (TRAVEL_OPS_GIT), about 5 GB;"
  "  its Dockerfile installs Google Chrome's amd64 package, so it does not build on arm64 at all"
  "legacy-lanes (lane-code, lane-web and their proxies): retired, never started (docs/platform.md)"
)

. /etc/os-release
step "host: $PRETTY_NAME, $(uname -m), kernel $(uname -r), $(nproc) CPUs, $(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo) MiB"

step "1. packages (docs/install.md, «Prepare the host»)"
apt-get update -q
apt-get install -y -q --no-install-recommends git curl ca-certificates python3 openssl iproute2 apparmor
if ! command -v docker >/dev/null; then
  # Docker Engine and the compose plugin from Docker's own repository for Debian (what Dokploy's installer sets up).
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker version --format 'docker {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})'
docker compose version
aa-enabled 2>/dev/null || echo "AppArmor: $(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || echo absent)"

# What Dokploy gives the host and the stack expects: Traefik's folder of dynamic files and its network. Dokploy itself
# (Traefik, its UI) is not installed here: nothing of it can be reached from a runner, and the stack never calls it.
# And the WireGuard address the split DNS listens on (10.8.0.1), as a dummy interface: no VPN here either.
step "2. what Dokploy and WireGuard would have made"
install -d -m 755 /etc/dokploy/traefik/dynamic
docker network inspect dokploy-network >/dev/null 2>&1 || docker network create dokploy-network >/dev/null
if ! ip -o addr show | grep -q ' 10\.8\.0\.1/'; then
  ip link add wg0 type dummy && ip addr add 10.8.0.1/24 dev wg0 && ip link set wg0 up
fi

# The switches: everything setup.sh offers that runs without an outside secret. Matrix too: its homeserver runs in the
# stack, so the router has a channel that starts without Telegram — a made-up bot token makes Telegram's own start
# fail (getMe), and a router with no channel exits by design.
SWITCHES=(TELEGRAM_OWNER_ID=123456789 OWNER_TZ=Europe/Berlin MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=anna
          MEMORY=1 MAIL=1 SEND=1 TGBUSINESS=1 PAGES=1 PAGES_HOST=pages.in.example.com DEVICES=10.8.0.2/32
          PAGES_PUBLIC_HOSTS=pages.example.com PLATFORM=1 FACE=1 FACE_HOST=app.in.example.com BACKUP=1)
step "3. deploy/setup.sh ${SWITCHES[*]}"
env "${SWITCHES[@]}" bash "$REPO/deploy/setup.sh"

step "4. stack.env filled with made-up values, setup.sh again (as the doc says after filling in)"
fake() { sed -i "s|^$1=.*|$1=$2|" "$STACK"; grep -q "^$1=$2\$" "$STACK"; }
fake TELEGRAM_BOT_TOKEN 123456789:not-a-real-bot-token-rehearsal-only
fake TELEGRAM_BUSINESS_TOKEN 123456789:not-a-real-business-token-rehearsal-only
fake CLAUDE_CODE_OAUTH_TOKEN not-a-real-oauth-token-rehearsal-only
env "${SWITCHES[@]}" bash "$REPO/deploy/setup.sh" > /dev/null
grep -q '^COMPOSE_PROFILES=matrix,mail,pages,tgbusiness,send,platform,face$' "$STACK"
systemctl is-enabled retinue-health.timer
if systemctl is-enabled retinue-backup.timer 2>/dev/null; then   # backup.env unfilled: the timer stays off
  echo "rehearsal: the backup timer is on with backup.env unfilled" >&2; exit 1
fi
/usr/local/bin/restic version                         # the pinned binary for this architecture, its sha256 checked
grep -q '^retinue-lane ' /sys/kernel/security/apparmor/profiles   # the lanes' profile, loaded by setup.sh

step "4b. setup.sh with no switches, as deploy.sh runs it for each version: the same set-up"
router_before=$(cksum < "$ROOT/router.yaml") stack_before=$(sort "$STACK" | cksum)
hub_owner=$(stat -c %U "$ROOT/memory/hub.git")
env -u SUDO_USER bash "$REPO/deploy/setup.sh" > /dev/null   # root without sudo, as over deploy.sh's ssh key
if [[ $(cksum < "$ROOT/router.yaml") != "$router_before" || $(sort "$STACK" | cksum) != "$stack_before" ]]; then
  echo "rehearsal: setup.sh with no switches changed router.yaml or stack.env" >&2; exit 1
fi
if [[ $(stat -c %U "$ROOT/memory/hub.git") != "$hub_owner" ]]; then
  echo "rehearsal: setup.sh without sudo gave the hub to another owner" >&2; exit 1
fi
systemctl is-active retinue-settings.path

step "5. build"
compose build

# The owner's Matrix account first: the router invites him into its rooms when it starts (docs/install.md, «Matrix»).
step "6. start: the homeserver, the owner's account, then the stack"
compose up -d tuwunel
token=$(sed -n 's/^MATRIX_REGISTRATION_TOKEN=//p' "$STACK")
for _ in $(seq 30); do
  if compose run --rm --no-deps -T router python -c 'import sys; from retinue.admin import register
print(register("http://tuwunel:6167", "anna", sys.argv[1], "rehearsal-password"))' "$token"; then break; fi
  sleep 2
done
compose up -d

# The same verdict as deploy/update.sh: each service running and, where it has a healthcheck, healthy; a restart of a
# fresh container is a crash. The report comes from the health unit setup.sh installed, run as systemd runs it.
verdict() {
  systemctl start retinue-health.service
  python3 - "$ROOT/status/health.json" "$@" <<'PY'
import json, sys

report = json.load(open(sys.argv[1]))
if "error" in report:
    print(f"wait: health check failed: {report['error']}")
    sys.exit()
seen = {c["name"]: c for c in report["containers"]}
bad, waiting = [], []
for name in sorted(set(sys.argv[2:])):
    c = seen.get(name)
    if c is None:
        waiting.append(f"{name}: no container")
    elif c["state"] in ("exited", "dead") or c["health"] == "unhealthy" or c["restarts"]:
        bad.append(f"{name}: {c['state']}, {c['health'] or 'no healthcheck'}, {c['restarts']} restarts")
    elif c["state"] != "running" or c["health"] not in ("healthy", ""):
        waiting.append(f"{name}: {c['state']}, {c['health']}")
print(f"red: {'; '.join(bad)}" if bad else f"wait: {'; '.join(waiting)}" if waiting else "green")
PY
}
mapfile -t SERVICES < <(compose config --services)
WAIT=()
for name in "${SERVICES[@]}"; do [[ -n ${SKIP[$name]:-} ]] || WAIT+=("$name"); done
step "7. health of ${WAIT[*]} (up to ${TIMEOUT}s)"
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  said=$(verdict "${WAIT[@]}")
  [[ $said == green || $said == red:* ]] && break
  (( $(date +%s) < deadline )) || break
  echo "   $said"
  sleep 15
done
python3 - "$ROOT/status/health.json" <<'PY'
import json, sys
for c in json.load(open(sys.argv[1])).get("containers", []):
    print(f"   {c['name']:<22} {c['state']:<10} {c['health'] or '-':<10} restarts {c['restarts']}")
PY
for name in "${!SKIP[@]}"; do echo "   skipped: $name — ${SKIP[$name]}"; done
printf '   not on: %s\n' "${NOT_ON[@]}"
printf '   not tried: %s\n' "${UNTRIED[@]}"
if [[ $said != green ]]; then
  echo "rehearsal: not healthy — ${said}" >&2
  compose ps -a >&2
  for name in "${WAIT[@]}"; do
    echo "--- logs: $name" >&2
    compose logs --no-color --tail 60 "$name" >&2 || true
  done
  exit 1
fi

step "8. what the doc's «Check» and the lanes need, without a model"
# The assistant has no way out but the proxy (docs/install.md, «Check»).
if compose exec -T assistant python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)" 2>/tmp/out; then
  echo "rehearsal: the assistant reached 1.1.1.1 directly" >&2; exit 1
fi
grep -q 'Network is unreachable' /tmp/out && echo "   assistant: no route out (Network is unreachable)"
# The lane's sandbox can start on this kernel: a user namespace and a fresh /proc under the lane's AppArmor profile and
# seccomp (what Claude Code's Bash sandbox needs; without it no session starts).
compose exec -T lane-private bwrap --unshare-all --die-with-parent --ro-bind / / --dev /dev --proc /proc -- /bin/true
echo "   lane-private: bubblewrap runs under retinue-lane"
compose exec -T lane-private /opt/claude/claude --version
compose exec -T router python -c "import retinue, claude_agent_sdk; print('router: python', __import__('sys').version.split()[0])"
# Which channels the router started: Matrix; Telegram refused the made-up token (its error names no token).
compose logs --no-color router | grep -E 'router ready|channel [a-z]+ failed to start' | sed 's/^/   /'
step "rehearsal: green on $(uname -m)"
