#!/usr/bin/env bash
# Prepare a host for the Retinue pilot. Run on the server from a checkout of the repo:
#   sudo bash deploy/setup.sh <matrix server name> <owner localpart>
# Idempotent: existing tokens and workspaces are kept.
set -euo pipefail

SERVER_NAME=${1:?matrix server name, e.g. matrix.example.com}
OWNER=${2:?owner localpart, e.g. alice}
PRIVATE_SUFFIX=${3:-in.${SERVER_NAME#*.}}   # e.g. in.example.com for matrix.example.com
REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=/srv/retinue
AGENT_UID=10001
token() { openssl rand -hex 32; }

install -d -m 755 "$ROOT" "$ROOT/tuwunel/appservices"
SECRETS="$ROOT/secrets.env"
if [[ ! -f $SECRETS ]]; then
  (umask 077; cat > "$SECRETS" <<ENV
RETINUE_AS_TOKEN=$(token)
RETINUE_HS_TOKEN=$(token)
MATRIX_REGISTRATION_TOKEN=$(token)
ENV
  )
fi
# shellcheck disable=SC1090
source "$SECRETS"

ESCAPED=${SERVER_NAME//./\\.}
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

cat > "$ROOT/router.yaml" <<YAML
homeserver: http://tuwunel:6167
server_name: $SERVER_NAME
owner: "@$OWNER:$SERVER_NAME"
agents:
  - id: study
    name: Учёба
    url: http://agent-study:9000
    topic: Учебные планы, конспекты, материалы
  - id: travel
    name: Путешествия
    url: http://agent-travel:9000
    topic: Билеты и отели — поиск и ссылки, без покупок
YAML
chmod 644 "$ROOT/router.yaml"

for agent in study travel; do
  install -d -m 755 "$ROOT/agents/$agent"
  install -m 644 "$REPO/agents/$agent/agent.yaml" "$ROOT/agents/$agent/agent.yaml"
  if [[ ! -d $ROOT/agents/$agent/workspace ]]; then
    cp -r "$REPO/agents/$agent/workspace" "$ROOT/agents/$agent/workspace"
  fi
  chown -R "$AGENT_UID:$AGENT_UID" "$ROOT/agents/$agent/workspace"
done

install -m 644 "$REPO/deploy/traefik-retinue.yml" /etc/dokploy/traefik/dynamic/retinue.yml

install -d -m 755 /opt/split-dns
install -m 644 "$REPO/deploy/split-dns/compose.yml" /opt/split-dns/compose.yml
sed -e "s/\${MATRIX_SERVER_NAME}/$SERVER_NAME/" -e "s/\${PRIVATE_SUFFIX}/$PRIVATE_SUFFIX/" "$REPO/deploy/split-dns/dnsmasq.conf.template" > /opt/split-dns/dnsmasq.conf
docker compose -f /opt/split-dns/compose.yml up -d --force-recreate

cat <<OUT

Done. Paste into Dokploy → retinue → Environment (CLAUDE_CODE_OAUTH_TOKEN from \`claude setup-token\`):

MATRIX_SERVER_NAME=$SERVER_NAME
MATRIX_ALLOW_REGISTRATION=true
MATRIX_REGISTRATION_TOKEN=$MATRIX_REGISTRATION_TOKEN
RETINUE_AS_TOKEN=$RETINUE_AS_TOKEN
RETINUE_HS_TOKEN=$RETINUE_HS_TOKEN
CLAUDE_CODE_OAUTH_TOKEN=<paste>

Split DNS check (from a VPN client): dig @10.8.0.1 $SERVER_NAME  → 10.8.0.1
OUT
