#!/usr/bin/env bash
# Prepare the host for the Retinue stack: /srv/retinue with the router config, agent files and secrets.
# Safe to run again after `git pull`.
#
#   sudo TELEGRAM_OWNER_ID=123456789 bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 OWNER_TZ=Europe/Belgrade bash deploy/setup.sh   # default: Europe/Moscow
#   sudo TELEGRAM_OWNER_ID=123456789 MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=alice bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 ROTATE_BUS_SECRET=1 bash deploy/setup.sh   # a new bus secret and agent tokens
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
AGENTS=(assistant)
token() { openssl rand -hex 32; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

install -d -m 755 "$ROOT"
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
    can_call: []      # no other agents at this stage
YAML
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
keep() { grep -q "^$1=" "$STACK" || printf '%s=%s\n' "$1" "$2" >> "$STACK"; }
fill() { keep "$1" ""; grep -q "^$1=." "$STACK" || MISSING+=("$1 ($2)"); }
put RETINUE_BUS_SECRET "$RETINUE_BUS_SECRET"
for a in "${AGENTS[@]}"; do put "RETINUE_BUS_TOKEN_$(upper "$a")" "$(bus_token "$a")"; done
fill CLAUDE_CODE_OAUTH_TOKEN "claude setup-token"
[[ -n $TELEGRAM_OWNER_ID ]] && fill TELEGRAM_BOT_TOKEN "@BotFather"
if [[ -n $MATRIX_SERVER_NAME ]]; then
  put COMPOSE_PROFILES matrix
  put MATRIX_SERVER_NAME "$MATRIX_SERVER_NAME"
  keep MATRIX_ALLOW_REGISTRATION true   # the owner turns it off after creating the account
  put MATRIX_REGISTRATION_TOKEN "$MATRIX_REGISTRATION_TOKEN"
  put RETINUE_AS_TOKEN "$RETINUE_AS_TOKEN"
  put RETINUE_HS_TOKEN "$RETINUE_HS_TOKEN"
fi
chmod 600 "$STACK"

echo
echo "Done. Stack environment: $STACK (mode 600; values are not printed)."
echo "Written: ${WRITTEN[*]}"
if (( ${#MISSING[@]} )); then
  printf 'Fill in there: %s\n' "${MISSING[@]}"
fi
echo "Start: docker compose -p retinue --env-file $STACK -f deploy/compose.yml up -d  (Dokploy: copy the file into Environment)"
if [[ -n $MATRIX_SERVER_NAME ]]; then
  echo "Split DNS check (from a VPN client): dig @10.8.0.1 $MATRIX_SERVER_NAME  → 10.8.0.1"
fi
