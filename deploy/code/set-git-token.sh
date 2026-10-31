#!/usr/bin/env bash
# Put one git token where the platform reads it, without it ever passing through a chat, an argument or the screen:
#
#   sudo bash /opt/retinue/deploy/code/set-git-token.sh <key>       # e.g. you, acme_corp, example_gitlab
#
# <key> is a repository's `token:` in platform.yaml; the token becomes RETINUE_GIT_TOKEN_<KEY> in
# /srv/retinue/code/tokens.env (root's, mode 600), which compose hands to the platform alone. It is read from the
# terminal with echo off, checked for shape, and written by replacing that one line. Nothing of it is printed: only
# the variable's name and the token's length. Then restart the platform (the command is printed). docs/code.md.
set -euo pipefail

KEY=${1:-}
ROOT=${RETINUE_ROOT:-/srv/retinue}
FILE="$ROOT/code/tokens.env"
if [[ ! $KEY =~ ^[a-z0-9][a-z0-9_]{0,39}$ ]]; then
  echo "usage: $0 <key>   — the repository's token: key in platform.yaml (a-z, 0-9, _)" >&2
  exit 2
fi
NAME="RETINUE_GIT_TOKEN_$(printf %s "$KEY" | tr a-z A-Z)"
if [[ -t 0 ]]; then
  read -r -s -p "Token for $NAME (input hidden): " TOKEN
  echo
else
  read -r TOKEN                 # piped in: `pass show … | sudo bash set-git-token.sh key`
fi
if [[ -z $TOKEN || $TOKEN =~ [[:space:]] || $TOKEN == *=* || ${#TOKEN} -gt 512 ]]; then
  echo "this does not look like a token: empty, has spaces or too long — nothing written" >&2
  exit 1
fi
if [[ ! $TOKEN =~ ^(github_pat_|glpat-|ghp_) ]]; then
  echo "warning: the token does not start with github_pat_ (GitHub fine-grained) or glpat- (GitLab) — check it is the right one" >&2
fi
install -d -m 755 "$ROOT/code"
(umask 077; touch "$FILE")
chmod 600 "$FILE"
TMP=$(mktemp "$FILE.XXXXXX")
grep -v "^$NAME=" "$FILE" > "$TMP" || true
printf '%s=%s\n' "$NAME" "$TOKEN" >> "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$FILE"
LENGTH=${#TOKEN}
unset TOKEN
echo "Wrote $NAME ($LENGTH characters) to $FILE."
echo "Restart the platform: cd /opt/retinue && sudo docker compose -p retinue --env-file $ROOT/stack.env" \
  "-f deploy/compose.yml up -d platform"
