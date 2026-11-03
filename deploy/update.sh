#!/usr/bin/env bash
# Move the stack to a release, and back to the version before when it is not healthy on it (docs/install.md,
# «Updating»). As root, from the checkout, for a stack run with `docker compose -p retinue`.
#
#   sudo RETINUE_IMAGE=ghcr.io/acme/retinue bash deploy/update.sh v1.2.0   # RETINUE_IMAGE once; stack.env keeps it
#
# The version running is RETINUE_VERSION in stack.env: compose reads it and hands it to the router and the platform.
set -euo pipefail

TAG=${1:-}
if [[ $# != 1 || ! $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: update.sh vX.Y.Z — a release tag" >&2
  exit 2
fi

REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${RETINUE_ROOT:-/srv/retinue}                       # tests point these at temporary folders
VOLUME_ROOT=${RETINUE_VOLUME_ROOT:-/var/lib/docker/volumes}
SNAPSHOTS=${RETINUE_SNAPSHOTS:-/var/backups/retinue/updates}
TIMEOUT=${RETINUE_UPDATE_TIMEOUT:-300}
POLL=${RETINUE_UPDATE_POLL:-10}
PYTHON=${PYTHON:-python3}
STACK=$ROOT/stack.env
STATE_DIRS=(mail/state send/state telegram/state)        # the code services' databases, on the host

step() { printf '\n== %s\n' "$*"; }
die() { echo "update: $*" >&2; exit 1; }
value() { sed -n "s/^$1=//p" "$STACK" | tail -n 1; }      # never printed: stack.env holds secrets
set_value() {
  local tmp
  tmp=$(mktemp "$STACK.XXXXXX")
  { grep -v "^$1=" "$STACK" || true; printf '%s=%s\n' "$1" "$2"; } > "$tmp"
  chmod 600 "$tmp" && mv -f "$tmp" "$STACK"
}
compose() {   # $1 image, $2 version, then compose's arguments
  RETINUE_IMAGE=$1 RETINUE_VERSION=$2 docker compose -p retinue --env-file "$STACK" -f "$REPO/deploy/compose.yml" "${@:3}"
}

# green, wait: … or red: …, from the report deploy/health writes. $1 fresh: the containers are new, a restart is a crash.
verdict() {
  RETINUE_ROOT=$ROOT "$PYTHON" "$REPO/deploy/health/retinue-health.py" >/dev/null 2>&1 || true
  "$PYTHON" - "$ROOT/status/health.json" "$1" "${SERVICES[@]}" <<'PY'
import json, sys

report = json.load(open(sys.argv[1]))
seen = {c["name"]: c for c in report.get("containers", [])}
bad, waiting = [], []
for name in sorted(set(sys.argv[3:])):
    c = seen.get(name)
    if c is None:
        waiting.append(f"{name}: no container")
    elif c["state"] in ("exited", "dead") or c["health"] == "unhealthy" or (sys.argv[2] == "fresh" and c["restarts"]):
        bad.append(f"{name}: {c['state']}, {c['health'] or 'no healthcheck'}, {c['restarts']} restarts")
    elif c["state"] != "running" or c["health"] not in ("healthy", ""):
        waiting.append(f"{name}: {c['state']}, {c['health']}")
print(f"red: {'; '.join(bad)}" if bad else f"wait: {'; '.join(waiting)}" if waiting else "green")
PY
}
wait_healthy() {   # 0 when green; 1 when red or out of time, the reason in LAST
  local deadline=$(( $(date +%s) + TIMEOUT )) said
  while said=$(verdict "$1"); do
    case $said in green) return 0 ;; red:*) LAST=${said#red: }; return 1 ;; esac
    (( $(date +%s) < deadline )) || { LAST="not healthy within ${TIMEOUT}s — ${said#wait: }"; return 1; }
    sleep "$POLL"
  done
  LAST="no health report"
  return 1
}

snapshot() {   # called in an `if`, where set -e is off: each step returns on its own failure
  install -d -m 700 "$SNAP/volumes" "$SNAP/srv" && cp -p "$STACK" "$SNAP/stack.env" || return 1
  local volume dir
  for volume in "${VOLUMES[@]}"; do
    tar -C "$VOLUME_ROOT/$volume/_data" --numeric-owner -cf "$SNAP/volumes/$volume.tar" . || return 1
  done
  for dir in "${STATE_DIRS[@]}"; do
    [[ ! -d $ROOT/$dir ]] || tar -C "$ROOT/$dir" --numeric-owner -cf "$SNAP/srv/${dir//\//_}.tar" . || return 1
  done
}
restore() {
  local tar dir
  for tar in "$SNAP"/volumes/*.tar "$SNAP"/srv/*.tar; do
    [[ -f $tar ]] || continue
    if [[ $tar == */volumes/* ]]; then dir=$VOLUME_ROOT/$(basename "$tar" .tar)/_data
    else dir=$ROOT/$(basename "$tar" .tar | tr _ /); fi
    find "$dir" -mindepth 1 -delete && tar -C "$dir" --numeric-owner -xpf "$tar" || return 1
  done
  cp -p "$SNAP/stack.env" "$STACK"
}
rollback() {
  echo "update: $TAG is not healthy: $1" >&2
  step "back to $FROM"
  compose "$IMAGE" "$TAG" down || true
  restore || die "the snapshot $SNAP could not be put back: the stack is down — put it back by hand"
  if compose "$FROM_IMAGE" "$FROM" up -d --no-build && wait_healthy fresh; then
    printf '\nback to %s, healthy; %s was not: %s\n' "$FROM" "$TAG" "$1"
    exit 1
  fi
  echo "update: back to $FROM, and it is not healthy either: $LAST — the snapshot stays in $SNAP" >&2
  exit 3
}

[[ -f $STACK ]] || die "no $STACK: run deploy/setup.sh first"
IMAGE=${RETINUE_IMAGE:-$(value RETINUE_IMAGE)}
[[ $IMAGE =~ ^[a-z0-9.:-]+(/[a-z0-9._-]+)+$ ]] || die "RETINUE_IMAGE: the release's images, e.g. ghcr.io/acme/retinue"
FROM_IMAGE=$(value RETINUE_IMAGE) FROM=$(value RETINUE_VERSION)
FROM_IMAGE=${FROM_IMAGE:-retinue} FROM=${FROM:-local}    # neither set: the images built here, retinue*:local
services=$(compose "$FROM_IMAGE" "$FROM" config --services)
# Arrays by `while read`, not mapfile: bash 3.2 (a Mac's, where the tests run too) has none.
SERVICES=(); while IFS= read -r line; do SERVICES+=("$line"); done <<< "$services"

step "1/6 health before the update ($FROM)"
wait_healthy running || die "the stack is not healthy before the update: $LAST — nothing was changed"

step "2/6 pull $TAG"
images=$(compose "$IMAGE" "$TAG" config --images)
IMAGES=(); while IFS= read -r line; do IMAGES+=("$line"); done < <(grep -E "^${IMAGE//./\\.}(-[a-z-]+)?:${TAG//./\\.}\$" <<< "$images" | awk '!seen[$0]++')
for image in "${IMAGES[@]}"; do
  echo "   $image"
  docker pull "$image" > /dev/null || die "$image could not be pulled (is $TAG published?); nothing was changed"
done

step "3/6 stop"
volumes=$(docker volume ls -q --filter label=com.docker.compose.project=retinue)
VOLUMES=(); while IFS= read -r line; do VOLUMES+=("$line"); done < <(grep -vE '_lane-[a-z]+-work$' <<< "$volumes" | sed '/^$/d')   # the lanes' checkouts: no state
SNAP=$SNAPSHOTS/$(date -u +%Y%m%dT%H%M%SZ)-$FROM-to-$TAG
compose "$FROM_IMAGE" "$FROM" down

step "4/6 snapshot into $SNAP"
if ! snapshot; then
  compose "$FROM_IMAGE" "$FROM" up -d --no-build || true
  die "the snapshot failed: $FROM was started again, nothing else changed"
fi

step "5/6 start $TAG"
set_value RETINUE_IMAGE "$IMAGE"
set_value RETINUE_VERSION "$TAG"
compose "$IMAGE" "$TAG" up -d --no-build || rollback "the stack did not start"

step "6/6 health of $TAG"
wait_healthy fresh || rollback "$LAST"
printf '\nupdated: %s -> %s, healthy; the snapshot of before: %s\n' "$FROM" "$TAG" "$SNAP"
