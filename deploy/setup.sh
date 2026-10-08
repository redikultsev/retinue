#!/usr/bin/env bash
# Prepare the host for the Retinue stack: /srv/retinue with the router config, agent files and secrets.
# Safe to run again after `git pull`.
#
#   sudo TELEGRAM_OWNER_ID=123456789 bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 OWNER_TZ=Europe/Belgrade bash deploy/setup.sh   # default: Europe/Moscow
#   sudo TELEGRAM_OWNER_ID=123456789 MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=alice bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 ROTATE_BUS_SECRET=1 bash deploy/setup.sh   # a new bus secret and agent tokens
#   sudo TELEGRAM_OWNER_ID=123456789 TRAVEL=1 bash deploy/setup.sh   # travel-ops: trip search and price watches
#   sudo TELEGRAM_OWNER_ID=123456789 MEMORY=1 bash deploy/setup.sh   # the knowledge base she writes, and its hub
#
# The stack's environment goes to /srv/retinue/stack.env (mode 600). The script prints names, never values:
# a terminal ends up in logs and transcripts.
# At least one channel is required. Everything Matrix needs (appservice registration, Traefik middleware,
# split DNS) is made only when MATRIX_SERVER_NAME is set.
set -euo pipefail

TELEGRAM_OWNER_ID=${TELEGRAM_OWNER_ID:-}
MATRIX_SERVER_NAME=${MATRIX_SERVER_NAME:-}
MATRIX_OWNER=${MATRIX_OWNER:-}
OWNER_TZ=${OWNER_TZ:-Europe/Moscow}     # the owner's time zone: reminders, the morning summary, every time shown
if [[ -z $TELEGRAM_OWNER_ID && -z $MATRIX_SERVER_NAME ]]; then
  echo "no channel: set TELEGRAM_OWNER_ID (your Telegram user id) or MATRIX_SERVER_NAME and MATRIX_OWNER" >&2
  exit 1
fi
if [[ -n $MATRIX_SERVER_NAME && -z $MATRIX_OWNER ]]; then
  echo "MATRIX_OWNER is required with MATRIX_SERVER_NAME: the owner's localpart, e.g. alice" >&2
  exit 1
fi

REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${RETINUE_ROOT:-/srv/retinue}      # tests point this at a temporary folder
HOST_SETUP=${RETINUE_HOST_SETUP:-1}     # 0: write files under ROOT only; no Traefik or split DNS (tests)
ROTATE_BUS_SECRET=${ROTATE_BUS_SECRET:-0}
BACKUP=${BACKUP:-0}                     # 1: nightly restic backup to S3; on for good once backup.env exists
TRAVEL=${TRAVEL:-0}                     # 1: travel-ops; on for good once travel/profile.yml exists
TRAVEL_ON=0
[[ $TRAVEL == 1 || -f $ROOT/travel/profile.yml ]] && TRAVEL_ON=1
TRAVEL_URL=http://travel-ops:8765/mcp
MEMORY=${MEMORY:-0}                     # 1: the knowledge base and its hub; on for good once the hub exists
MEMORY_ON=0
MEMORY_DIR=$ROOT/memory
HUB=$MEMORY_DIR/hub.git
[[ $MEMORY == 1 || -d $HUB ]] && MEMORY_ON=1
MEMORY_UID=10001                        # the containers' user (Dockerfile): the router writes the hub as it
AGENTS=(assistant)
token() { openssl rand -hex 32; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

install -d -m 755 "$ROOT"
# The knowledge base. The mounts exist whether it is on or not, so the folders and the policy always do; what she
# may write and see is the owner's (written once from the examples, then edited by hand).
install -d -m 755 "$MEMORY_DIR" "$MEMORY_DIR/tree" "$MEMORY_DIR/git"
[[ -f $MEMORY_DIR/policy.json ]] || install -m 644 "$REPO/deploy/memory/policy.example.json" "$MEMORY_DIR/policy.json"
[[ -f $MEMORY_DIR/checkout.txt ]] || install -m 644 "$REPO/deploy/memory/checkout.example.txt" "$MEMORY_DIR/checkout.txt"
SECRETS="$ROOT/secrets.env"
[[ -f $SECRETS ]] || (umask 077; : > "$SECRETS")
secret() { grep -q "^$1=" "$SECRETS" || echo "$1=$(token)" >> "$SECRETS"; }   # made once, then kept
drop() { local rest; rest=$(grep -v "^$1=" "$2" || true); printf '%s\n' "$rest" | sed '/^$/d' > "$2"; }
[[ $ROTATE_BUS_SECRET == 1 ]] && drop RETINUE_BUS_SECRET "$SECRETS"   # the agents' tokens follow from it
secret RETINUE_BUS_SECRET
if [[ -n $MATRIX_SERVER_NAME ]]; then
  secret RETINUE_AS_TOKEN
  secret RETINUE_HS_TOKEN
  secret MATRIX_REGISTRATION_TOKEN
fi
source "$SECRETS"
bus_token() { printf 'retinue-bus:%s' "$1" | openssl dgst -sha256 -hmac "$RETINUE_BUS_SECRET" -r | cut -d' ' -f1; }

{
  cat <<YAML
owner: owner
owner_tz: $OWNER_TZ
default_agent: assistant
agents:
  - id: assistant
    name: Ассистентка
    url: http://assistant:9000
    description: Единственное лицо системы
    trust_class: private
    archive: true     # may search the raw archive through the bus
    reminders: true   # may set, list, move and cancel the owner's reminders through the bus
    attachments: true # may fetch what the owner sent (#N) again through the bus
    can_call: []      # no other agents at this stage
YAML
  if [[ $TRAVEL_ON == 1 ]]; then
    printf 'travel_url: %s\n' "$TRAVEL_URL"      # the router collects price alerts there
    printf 'link_hosts:\n'                       # travel-ops' sites: a link there is clickable in Telegram
    sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e "s/^[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/  - '\1'/" \
      "$REPO/deploy/travel/link-hosts.txt"
  fi
  if [[ $MEMORY_ON == 1 ]]; then
    printf 'memory:\n  hub: /hub\n  checkout:\n'   # the rest: defaults in config.py (MemoryConfig)
    sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e "s/^[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/    - '\1'/" \
      "$MEMORY_DIR/checkout.txt"
  fi
  if [[ -n $TELEGRAM_OWNER_ID ]]; then
    printf 'telegram:\n  owner_id: %s\n' "$TELEGRAM_OWNER_ID"
  fi
  if [[ -n $MATRIX_SERVER_NAME ]]; then
    cat <<YAML
matrix:
  homeserver: http://tuwunel:6167
  server_name: $MATRIX_SERVER_NAME
  owner: "@$MATRIX_OWNER:$MATRIX_SERVER_NAME"
YAML
  fi
} > "$ROOT/router.yaml"
chmod 644 "$ROOT/router.yaml"

for agent in "${AGENTS[@]}"; do
  # Config and instructions, always as they are in the repo: the container mounts this folder read-only.
  install -d -m 755 "$ROOT/agents/$agent"
  install -m 644 "$REPO/agents/$agent/agent.yaml" "$REPO/agents/$agent/CLAUDE.md" "$ROOT/agents/$agent/"
done

install -d -m 755 "$ROOT/status"       # the host writes, the router reads (mounted read-only)

PROFILE="$ROOT/travel/profile.yml"
if [[ $TRAVEL_ON == 1 ]]; then
  install -d -m 755 "$ROOT/travel"
  # The owner's home and party: written by the owner, never by this script and never in git.
  [[ -f $PROFILE ]] || printf '%s\n' "# travel-ops profile: home_airports, travellers, currency, stays." \
    "# The fields are in profile.example.yml of travel-ops. Without home_airports the assistant asks." > "$PROFILE"
  chmod 644 "$PROFILE"   # read by travel-ops' own user in its container
fi

if [[ $MEMORY_ON == 1 ]]; then
  # The hub: every copy pushes here, and its pre-receive checks every push with the same rules (kbcheck.py).
  # A push writes objects/ and refs/ and nothing else: no gc after it (it would write packed-refs and gc.* in the
  # hub's own folder), no reflogs. So only objects/ and refs/ are the group's. The hub's folder and its config are
  # the owner's (git refuses a repository its user does not own, and he pushes over ssh) and not the group's;
  # hooks/ is root's. The containers' user can push, but cannot change what checks a push (core.hooksPath,
  # receive.*) or move hooks/ away.
  [[ -f $HUB/HEAD ]] || git init -q --bare -b main "$HUB"
  for setting in "core.sharedRepository group" "receive.denyNonFastForwards true" "receive.denyDeletes true" \
                 "receive.autogc false" "gc.auto 0" "core.logAllRefUpdates false"; do
    git -C "$HUB" config ${setting}
  done
  # HEAD names no branch: a push to the branch HEAD names also locks HEAD here, in the folder the group cannot
  # write (seen on the server: «cannot lock ref 'HEAD'»). Clone with -b main.
  git -C "$HUB" symbolic-ref HEAD refs/heads/none
  rm -rf "$HUB/kbcheck"                 # an earlier version kept the lint's copies here, writable by the group
  install -m 755 "$REPO/retinue/kbcheck.py" "$HUB/hooks/pre-receive"
  install -m 644 "$MEMORY_DIR/policy.json" "$HUB/hooks/kb-policy.json"
  chmod -R go-w "$HUB"
  chmod -R g+rwX "$HUB/objects" "$HUB/refs"
  find "$HUB/objects" "$HUB/refs" -type d -exec chmod g+s {} +
fi
if [[ $HOST_SETUP == 1 ]]; then
  # The containers' user owns the working copy; the hub's objects/ and refs/ are shared with the owner through
  # the group of that uid; the rest of the hub is the owner's, its hooks root's.
  chown -R "$MEMORY_UID:$MEMORY_UID" "$MEMORY_DIR/tree" "$MEMORY_DIR/git"
  chmod 700 "$MEMORY_DIR/git"
  if [[ $MEMORY_ON == 1 ]]; then
    getent group "$MEMORY_UID" >/dev/null || groupadd -g "$MEMORY_UID" retinue-memory
    [[ -n ${SUDO_USER:-} ]] && usermod -aG "$(getent group "$MEMORY_UID" | cut -d: -f1)" "$SUDO_USER"
    chown -R "${SUDO_USER:-root}:$MEMORY_UID" "$HUB"
    chown -R root:root "$HUB/hooks"
  fi
fi

install -d -m 755 "$ROOT/egress"
install -m 644 "$REPO/deploy/egress/squid.conf" "$ROOT/egress/squid.conf"
# The list of allowed hosts belongs to the owner: written once, then edited only by hand.
[[ -f $ROOT/egress/allowed-hosts.txt ]] || install -m 644 "$REPO/deploy/egress/allowed-hosts.txt" "$ROOT/egress/allowed-hosts.txt"

if [[ -n $MATRIX_SERVER_NAME ]]; then
  PRIVATE_SUFFIX=${PRIVATE_SUFFIX:-in.${MATRIX_SERVER_NAME#*.}}   # e.g. in.example.com for matrix.example.com
  ESCAPED=${MATRIX_SERVER_NAME//./\\.}
  install -d -m 755 "$ROOT/tuwunel/appservices"
  cat > "$ROOT/tuwunel/appservices/retinue.yaml" <<YAML
id: retinue
url: http://router:29400
as_token: $RETINUE_AS_TOKEN
hs_token: $RETINUE_HS_TOKEN
sender_localpart: retinue
rate_limited: false
namespaces:
  users:
    - exclusive: true
      regex: '^@agent_[a-z0-9_]+:$ESCAPED\$'
YAML
  chmod 644 "$ROOT/tuwunel/appservices/retinue.yaml"   # read by the homeserver container
  if [[ $HOST_SETUP == 1 ]]; then
    install -m 644 "$REPO/deploy/traefik-retinue.yml" /etc/dokploy/traefik/dynamic/retinue.yml
    install -d -m 755 /opt/split-dns
    install -m 644 "$REPO/deploy/split-dns/compose.yml" /opt/split-dns/compose.yml
    sed -e "s/\${MATRIX_SERVER_NAME}/$MATRIX_SERVER_NAME/" -e "s/\${PRIVATE_SUFFIX}/$PRIVATE_SUFFIX/" \
      "$REPO/deploy/split-dns/dnsmasq.conf.template" > /opt/split-dns/dnsmasq.conf
    docker compose -f /opt/split-dns/compose.yml up -d --force-recreate
  fi
fi

# The stack's environment: generated values are written (and replaced after a rotation), what the owner pastes is
# kept as it is. Nothing here is printed.
STACK="$ROOT/stack.env"
[[ -f $STACK ]] || (umask 077; : > "$STACK")
chmod 600 "$STACK"
WRITTEN=() MISSING=()
put() { drop "$1" "$STACK"; printf '%s=%s\n' "$1" "$2" >> "$STACK"; WRITTEN+=("$1"); }
keep() { local file=${3:-$STACK}; grep -q "^$1=" "$file" || printf '%s=%s\n' "$1" "$2" >> "$file"; }
fill() { local file=${3:-$STACK}; keep "$1" "" "$file"; grep -q "^$1=." "$file" || MISSING+=("$file: $1 ($2)"); }
put RETINUE_BUS_SECRET "$RETINUE_BUS_SECRET"
for a in "${AGENTS[@]}"; do put "RETINUE_BUS_TOKEN_$(upper "$a")" "$(bus_token "$a")"; done
fill CLAUDE_CODE_OAUTH_TOKEN "claude setup-token"
fill ELEVENLABS_API_KEY "elevenlabs.io: a key with speech_to_text only and a credit limit; empty = voice refused"
[[ -n $TELEGRAM_OWNER_ID ]] && fill TELEGRAM_BOT_TOKEN "@BotFather"
PROFILES=()
[[ -n $MATRIX_SERVER_NAME ]] && PROFILES+=(matrix)
[[ $TRAVEL_ON == 1 ]] && PROFILES+=(travel)
if (( ${#PROFILES[@]} )); then
  put COMPOSE_PROFILES "$(IFS=,; echo "${PROFILES[*]}")"
else
  drop COMPOSE_PROFILES "$STACK"
fi
if [[ $TRAVEL_ON == 1 ]]; then put RETINUE_TRAVEL_URL "$TRAVEL_URL"; else put RETINUE_TRAVEL_URL ""; fi
if [[ $MEMORY_ON == 1 ]]; then put RETINUE_MEMORY /kb; else put RETINUE_MEMORY ""; fi
if [[ $TRAVEL_ON == 1 ]] && ! grep -q '^home_airports:' "$PROFILE"; then
  MISSING+=("$PROFILE: home_airports (your airports, IATA)")
fi
if [[ -n $MATRIX_SERVER_NAME ]]; then
  put MATRIX_SERVER_NAME "$MATRIX_SERVER_NAME"
  keep MATRIX_ALLOW_REGISTRATION true   # the owner turns it off after creating the account
  put MATRIX_REGISTRATION_TOKEN "$MATRIX_REGISTRATION_TOKEN"
  put RETINUE_AS_TOKEN "$RETINUE_AS_TOKEN"
  put RETINUE_HS_TOKEN "$RETINUE_HS_TOKEN"
fi
chmod 600 "$STACK"

# Backup: restic on the host, not in the stack — it must outlive a broken deploy, and its keys stay out of Dokploy.
BACKUP_ENV="$ROOT/backup.env"
RESTIC_VERSION=0.19.1
# restic_0.19.1_linux_amd64.bz2 in the release's SHA256SUMS, signed by CF8F18F2844575973F79D4E191A6868BD3F7A907
RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
install_restic() {
  [[ $(/usr/local/bin/restic version 2>/dev/null) == "restic $RESTIC_VERSION "* ]] && return 0
  [[ $(uname -m) == x86_64 ]] || { echo "restic: only linux amd64 is pinned here; install restic $RESTIC_VERSION" >&2; return 1; }
  local tmp; tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/restic.bz2" \
    "https://github.com/restic/restic/releases/download/v$RESTIC_VERSION/restic_${RESTIC_VERSION}_linux_amd64.bz2"
  echo "$RESTIC_SHA256  $tmp/restic.bz2" | sha256sum -c --quiet -
  python3 -c 'import bz2, sys; sys.stdout.buffer.write(bz2.open(sys.argv[1]).read())' "$tmp/restic.bz2" > "$tmp/restic"
  install -m 755 "$tmp/restic" /usr/local/bin/restic
  rm -rf "$tmp"
}
BACKUP_STATE=""
if [[ $BACKUP == 1 || -f $BACKUP_ENV ]]; then
  [[ -f $BACKUP_ENV ]] || (umask 077; : > "$BACKUP_ENV")
  chmod 600 "$BACKUP_ENV"
  before=${#MISSING[@]}
  fill RESTIC_REPOSITORY "s3:https://<endpoint>/<bucket>/retinue" "$BACKUP_ENV"
  fill RESTIC_PASSWORD "from your password manager; keep a copy off this server" "$BACKUP_ENV"
  fill AWS_ACCESS_KEY_ID "the server's S3 key: no delete except locks/" "$BACKUP_ENV"
  fill AWS_SECRET_ACCESS_KEY "the same key's secret" "$BACKUP_ENV"
  fill AWS_DEFAULT_REGION "the endpoint's region, e.g. eu-luxembourg-1" "$BACKUP_ENV"
  if (( ${#MISSING[@]} == before )); then BACKUP_STATE=on; else BACKUP_STATE=unfilled; fi
  # With travel on, travel-ops' searches and the owner's watches are backed up too; without it, the script's list.
  drop RETINUE_VOLUMES "$BACKUP_ENV"
  if [[ $TRAVEL_ON == 1 ]]; then
    echo 'RETINUE_VOLUMES="retinue_router-data retinue_assistant-data retinue_travel-data"' >> "$BACKUP_ENV"
  fi
  if [[ $HOST_SETUP == 1 ]]; then
    install_restic
    install -d -m 755 /usr/local/lib/retinue
    install -m 755 "$REPO/deploy/backup/retinue-backup.py" /usr/local/lib/retinue/retinue-backup.py
    for unit in retinue-backup.service retinue-backup.timer; do
      sed "s|/srv/retinue|$ROOT|g" "$REPO/deploy/backup/$unit" > "/etc/systemd/system/$unit"
      chmod 644 "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    if [[ $BACKUP_STATE == on ]]; then systemctl enable --now retinue-backup.timer; fi
  fi
fi

echo
echo "Done. Stack environment: $STACK (mode 600; values are not printed)."
echo "Written: ${WRITTEN[*]}"
for missing in ${MISSING[@]+"${MISSING[@]}"}; do
  echo "Fill in $missing"
done
[[ $TRAVEL_ON == 1 ]] && echo "Travel: on — the travel-ops image (about 5 GB) is built from GitHub by \`up -d --build\`"
[[ $MEMORY_ON == 1 ]] && echo "Memory: on — hub $HUB; rules $MEMORY_DIR/policy.json and checkout.txt (see docs/memory.md)"
case $BACKUP_STATE in
  on) echo "Backup: on, every night at 03:30 Europe/Moscow; the result goes to $ROOT/status/backup.json" ;;
  unfilled) echo "Backup: off until $BACKUP_ENV is filled; then run this script again, and see docs/backup.md" ;;
esac
echo "Start: docker compose -p retinue --env-file $STACK -f deploy/compose.yml up -d  (Dokploy: copy the file into Environment)"
if [[ -n $MATRIX_SERVER_NAME ]]; then
  echo "Split DNS check (from a VPN client): dig @10.8.0.1 $MATRIX_SERVER_NAME  → 10.8.0.1"
fi
