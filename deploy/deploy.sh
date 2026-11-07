#!/usr/bin/env bash
# Deploy one commit of develop on the server that follows it (docs/install.md, «Following develop»).
# GitHub Actions runs it over ssh after CI passed (.github/workflows/deploy.yml), with a key whose authorized_keys line
# allows nothing else: the commit's sha arrives as SSH_ORIGINAL_COMMAND. By hand, as root: deploy.sh <sha>.
#
# The commit is built while the stack runs; the stack is stopped, its data copied, the commit started with
# `docker compose up --wait` (compose waits for every healthcheck). Not healthy: the data is put back and the commit
# before is started again.
set -euo pipefail

REPO=${RETINUE_REPO:-/opt/retinue}
ROOT=${RETINUE_ROOT:-/srv/retinue}
VOLUME_ROOT=${RETINUE_VOLUME_ROOT:-/var/lib/docker/volumes}
SNAPSHOTS=${RETINUE_SNAPSHOTS:-/var/backups/retinue/deploys}
WAIT=${RETINUE_DEPLOY_WAIT:-300}
STATE_DIRS=(mail/state send/state telegram/state)   # the code services' databases, on the host

compose() { docker compose -p retinue --env-file "$ROOT/stack.env" -f "$REPO/deploy/compose.yml" "$@"; }
say() { printf 'deploy: %s\n' "$*"; }

copy_data() {   # the stack's volumes but the lanes' checkouts, and the state folders, into $1
  local volume dir
  install -d -m 700 "$1/volumes" "$1/srv"
  for volume in $(docker volume ls -q --filter label=com.docker.compose.project=retinue | grep -vE '_lane-[a-z]+-work$'); do
    tar -C "$VOLUME_ROOT/$volume/_data" --numeric-owner -cf "$1/volumes/$volume.tar" .
  done
  for dir in "${STATE_DIRS[@]}"; do
    [[ ! -d $ROOT/$dir ]] || tar -C "$ROOT/$dir" --numeric-owner -cf "$1/srv/${dir//\//_}.tar" .
  done
}
put_back() {
  local tar dir
  for tar in "$1"/volumes/*.tar "$1"/srv/*.tar; do
    [[ -f $tar ]] || continue
    if [[ $tar == */volumes/* ]]; then dir=$VOLUME_ROOT/$(basename "$tar" .tar)/_data
    else dir=$ROOT/$(basename "$tar" .tar | tr _ /); fi
    find "$dir" -mindepth 1 -delete && tar -C "$dir" --numeric-owner -xpf "$tar"
  done
}

# All in a function: bash reads a script as it runs, and this one checks out another version of itself.
main() {
  local sha=${SSH_ORIGINAL_COMMAND:-${1:-}} was snap
  [[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy.sh <a commit's full sha>" >&2; exit 2; }
  exec 9>"$ROOT/deploy.lock"
  flock 9
  was=$(git -C "$REPO" rev-parse HEAD)
  [[ $was != "$sha" ]] || { say "already on ${sha:0:12}"; exit 0; }

  say "building ${sha:0:12} (running ${was:0:12})"
  git -C "$REPO" fetch -q origin "$sha"
  git -C "$REPO" checkout -q --detach "$sha"
  if ! compose build -q; then
    git -C "$REPO" checkout -q --detach "$was"
    say "${sha:0:12} did not build; ${was:0:12} goes on running"
    exit 1
  fi

  snap=$SNAPSHOTS/$(date -u +%Y%m%dT%H%M%SZ)-${was:0:12}-to-${sha:0:12}
  compose down
  copy_data "$snap"
  if compose up -d --wait --wait-timeout "$WAIT"; then
    ls -1d "$SNAPSHOTS"/*/ | head -n -5 | xargs -r rm -rf   # the last five deploys' data are kept
    say "now on ${sha:0:12}"
    exit 0
  fi

  say "${sha:0:12} is not healthy: back to ${was:0:12} with the data from before it"
  compose ps -a --format '{{.Service}}: {{.State}} {{.Health}}' >&2 || true
  compose down || true
  put_back "$snap"
  git -C "$REPO" checkout -q --detach "$was"
  compose build -q
  compose up -d --wait --wait-timeout "$WAIT" || { say "${was:0:12} is not healthy either; its data: $snap"; exit 3; }
  say "back on ${was:0:12}; ${sha:0:12} was not deployed"
  exit 1
}

main "$@"
