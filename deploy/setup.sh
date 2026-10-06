#!/usr/bin/env bash
# Prepare the host for the Retinue stack: /srv/retinue with the router config, agent files and secrets.
# Safe to run again after `git pull`.
#
#   sudo TELEGRAM_OWNER_ID=123456789 bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=alice bash deploy/setup.sh
#
# At least one channel is required. Everything Matrix needs (appservice registration, Traefik middleware,
# split DNS) is made only when MATRIX_SERVER_NAME is set.
set -euo pipefail

TELEGRAM_OWNER_ID=${TELEGRAM_OWNER_ID:-}
MATRIX_SERVER_NAME=${MATRIX_SERVER_NAME:-}
MATRIX_OWNER=${MATRIX_OWNER:-}
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
AGENTS=(assistant)
token() { openssl rand -hex 32; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

install -d -m 755 "$ROOT"
SECRETS="$ROOT/secrets.env"
[[ -f $SECRETS ]] || (umask 077; : > "$SECRETS")
secret() { grep -q "^$1=" "$SECRETS" || echo "$1=$(token)" >> "$SECRETS"; }   # made once, then kept
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
default_agent: assistant
agents:
  - id: assistant
    name: Ассистентка
    url: http://assistant:9000
    description: Единственное лицо системы
    trust_class: private
    archive: true     # may search the raw archive through the bus
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

cat <<OUT

Done. Paste into Dokploy → retinue → Environment (CLAUDE_CODE_OAUTH_TOKEN from \`claude setup-token\`):

RETINUE_BUS_SECRET=$RETINUE_BUS_SECRET
$(for a in "${AGENTS[@]}"; do echo "RETINUE_BUS_TOKEN_$(upper "$a")=$(bus_token "$a")"; done)
CLAUDE_CODE_OAUTH_TOKEN=<paste>
OUT
if [[ -n $TELEGRAM_OWNER_ID ]]; then
  echo "TELEGRAM_BOT_TOKEN=<paste: from @BotFather>"
fi
if [[ -n $MATRIX_SERVER_NAME ]]; then
  cat <<OUT
COMPOSE_PROFILES=matrix
MATRIX_SERVER_NAME=$MATRIX_SERVER_NAME
MATRIX_ALLOW_REGISTRATION=true
MATRIX_REGISTRATION_TOKEN=$MATRIX_REGISTRATION_TOKEN
RETINUE_AS_TOKEN=$RETINUE_AS_TOKEN
RETINUE_HS_TOKEN=$RETINUE_HS_TOKEN

Split DNS check (from a VPN client): dig @10.8.0.1 $MATRIX_SERVER_NAME  → 10.8.0.1
OUT
fi
