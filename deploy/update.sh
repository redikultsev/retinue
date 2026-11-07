#!/usr/bin/env bash
# Move the stack to a release, and back to the version before when it is not healthy on it (docs/install.md,
# «Updating»). As root, from the checkout, for a stack run with `docker compose -p retinue`.
#
#   sudo RETINUE_IMAGE=ghcr.io/acme/retinue bash deploy/update.sh v1.2.0   # RETINUE_IMAGE once; stack.env keeps it
#
# Or to a commit of develop, sha-<12 hex>, whose images CI built the same way (deploy/deploy.sh runs this for it).
#
# The version running is RETINUE_VERSION in stack.env: compose reads it and hands it to the router and the platform.
# Only the services the version changes are stopped and started again (deploy/changed.py says which); the rest keep
# running, their sessions with them.
set -euo pipefail

TAG=${1:-}
if [[ $# != 1 || ! $TAG =~ ^(v[0-9]+\.[0-9]+\.[0-9]+|sha-[0-9a-f]{12})$ ]]; then
  echo "usage: update.sh vX.Y.Z — a release tag, or sha-<12 hex>, a commit of develop CI built" >&2
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
TOUCHED=$ROOT/update-touched                             # what a way back started again, for deploy.sh
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
changed() { RETINUE_ROOT=$ROOT "$PYTHON" "$REPO/deploy/changed.py" "$@"; }
lines() {   # the plan's $1, one item a line: a list's items, or a map's "name id"
  "$PYTHON" -c 'import json, sys
v = json.load(open(sys.argv[1]))[sys.argv[2]]
print("\n".join(f"{k} {i}" for k, i in v.items()) if isinstance(v, dict) else "\n".join(v))' "$PLAN" "$1"
}
csv() { local IFS=,; echo "$*"; }

# green, wait: … or red: …, from the report deploy/health writes. $1: the services just started, comma-separated, where
# a restart is a crash; the others may have restarted long before.
verdict() {
  RETINUE_ROOT=$ROOT "$PYTHON" "$REPO/deploy/health/retinue-health.py" >/dev/null 2>&1 || true
  "$PYTHON" - "$ROOT/status/health.json" "$1" "${SERVICES[@]}" <<'PY'
import json, sys

report = json.load(open(sys.argv[1]))
seen = {c["name"]: c for c in report.get("containers", [])}
fresh = set(filter(None, sys.argv[2].split(",")))
bad, waiting = [], []
for name in sorted(set(sys.argv[3:])):
    c = seen.get(name)
    if c is None:
        waiting.append(f"{name}: no container")
    elif c["state"] in ("exited", "dead") or c["health"] == "unhealthy" or (name in fresh and c["restarts"]):
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
  local volume dir bind
  for volume in ${VOLUMES[@]+"${VOLUMES[@]}"}; do
    tar -C "$VOLUME_ROOT/$volume/_data" --numeric-owner -cf "$SNAP/volumes/$volume.tar" . || return 1
  done
  for dir in "${STATE_DIRS[@]}"; do   # only a stopped service's: a running one writes on
    for bind in ${BINDS[@]+"${BINDS[@]}"}; do
      [[ $bind == "$ROOT/$dir" && -d $ROOT/$dir ]] || continue
      tar -C "$ROOT/$dir" --numeric-owner -cf "$SNAP/srv/${dir//\//_}.tar" . || return 1
    done
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
  local name back=()
  for name in ${STOPPED[@]+"${STOPPED[@]}"}; do   # what ran before, of what the file still has
    if printf '%s\n' "${FROM_SERVICES[@]}" | grep -qxF "$name"; then back+=("$name"); fi
  done
  printf '%s\n' ${STOPPED[@]+"${STOPPED[@]}"} > "$TOUCHED"
  changed forget ${START[@]+"${START[@]}"} ${STOPPED[@]+"${STOPPED[@]}"}
  # Never without names: compose's rm, like its up, then takes every service.
  (( ! ${#START[@]} )) || compose "$IMAGE" "$TAG" rm --stop --force "${START[@]}" || true
  restore || die "the snapshot $SNAP could not be put back: ${STOPPED[*]:-nothing} are down — put them back by hand"
  SERVICES=("${FROM_SERVICES[@]}")
  if (( ! ${#back[@]} )) \
      || { compose "$FROM_IMAGE" "$FROM" up -d --no-build --no-deps "${back[@]}" && wait_healthy "$(csv "${back[@]}")"; }; then
    printf '\nback to %s, healthy; %s was not: %s\n' "$FROM" "$TAG" "$1"
    exit 1
  fi
  echo "update: back to $FROM, and it is not healthy either: $LAST — the snapshot stays in $SNAP" >&2
  exit 3
}

[[ -f $STACK ]] || die "no $STACK: run deploy/setup.sh first"
: > "$TOUCHED"
IMAGE=${RETINUE_IMAGE:-$(value RETINUE_IMAGE)}
[[ $IMAGE =~ ^[a-z0-9.:-]+(/[a-z0-9._-]+)+$ ]] || die "RETINUE_IMAGE: the release's images, e.g. ghcr.io/acme/retinue"
FROM_IMAGE=$(value RETINUE_IMAGE) FROM=$(value RETINUE_VERSION)
FROM_IMAGE=${FROM_IMAGE:-retinue} FROM=${FROM:-local}    # neither set: the images built here, retinue*:local
services=$(compose "$FROM_IMAGE" "$FROM" config --services)
# Arrays by `while read`, not mapfile: bash 3.2 (a computer's, where the tests run too) has none.
FROM_SERVICES=(); while IFS= read -r line; do FROM_SERVICES+=("$line"); done <<< "$services"
SERVICES=("${FROM_SERVICES[@]}")
WORKDIR=$(mktemp -d)                                     # the config holds stack.env's secrets: never kept, never printed
trap 'rm -rf "$WORKDIR"' EXIT
CONFIG=$WORKDIR/config.json PLAN=$WORKDIR/plan.json

step "1/7 health before the update ($FROM)"
wait_healthy "" || die "the stack is not healthy before the update: $LAST — nothing was changed"

step "2/7 pull $TAG, build what no registry has"
images=$(compose "$IMAGE" "$TAG" config --images)
IMAGES=(); while IFS= read -r line; do IMAGES+=("$line"); done < <(grep -E "^${IMAGE//./\\.}(-[a-z-]+)?:${TAG//./\\.}\$" <<< "$images" | awk '!seen[$0]++')
for image in "${IMAGES[@]}"; do
  echo "   $image"
  docker pull "$image" > /dev/null || die "$image could not be pulled (is $TAG published?); nothing was changed"
done
# The images no registry has (the lane's: it carries Claude Code, see compose.yml), built from the checkout under the
# tag, still before the stop: a failed build leaves the version running as it was.
compose "$IMAGE" "$TAG" config --format json > "$CONFIG"
built=$("$PYTHON" -c '
import json, sys
image, tag = sys.argv[1], sys.argv[2]
for name, service in json.load(open(sys.argv[3]))["services"].items():
    own = service.get("image", "")
    if "build" in service and own.endswith(":" + tag) and not own.startswith(image):
        print(name)' "$IMAGE" "$TAG" "$CONFIG")
BUILT=(); while IFS= read -r line; do BUILT+=("$line"); done < <(sed '/^$/d' <<< "$built")
if (( ${#BUILT[@]} )); then
  echo "   built here: ${BUILT[*]}"
  compose "$IMAGE" "$TAG" build "${BUILT[@]}" || die "${BUILT[*]} could not be built from the checkout; nothing was changed"
fi

step "3/7 what $TAG changes"
changed plan "$TAG" "$CONFIG" > "$PLAN" || die "could not tell what $TAG changes; nothing was changed"
START=(); while IFS= read -r line; do START+=("$line"); done < <(lines start | sed '/^$/d')
STOPPED=() STOP_IDS=(); while read -r name id; do STOPPED+=("$name"); STOP_IDS+=("$id"); done < <(lines stop | sed '/^$/d')
VOLUMES=(); while IFS= read -r line; do VOLUMES+=("$line"); done < <(lines volumes | sed '/^$/d')
BINDS=(); while IFS= read -r line; do BINDS+=("$line"); done < <(lines binds | sed '/^$/d')
while read -r name id; do echo "   keeps running: $name ${id:0:12}"; done < <(lines keep | sed '/^$/d')
for name in ${START[@]+"${START[@]}"}; do echo "   restarts: $name"; done
SERVICES=(); while IFS= read -r line; do SERVICES+=("$line"); done < <("$PYTHON" -c '
import json, sys
print("\n".join(json.load(open(sys.argv[1]))["services"]))' "$CONFIG")
if (( ! ${#START[@]} && ! ${#STOPPED[@]} )); then
  set_value RETINUE_IMAGE "$IMAGE"
  set_value RETINUE_VERSION "$TAG"
  printf '\nupdated: %s -> %s, nothing in it changed what runs: nothing was restarted\n' "$FROM" "$TAG"
  exit 0
fi

step "4/7 stop ${STOPPED[*]:-nothing}"
SNAP=$SNAPSHOTS/$(date -u +%Y%m%dT%H%M%SZ)-$FROM-to-$TAG
if (( ${#STOPPED[@]} )); then
  # By compose, in the order of their dependencies and each within its stop_grace_period; what the file no longer
  # has, by docker.
  ours=(); for name in ${STOPPED[@]+"${STOPPED[@]}"}; do
    if printf '%s\n' "${FROM_SERVICES[@]}" | grep -qxF "$name"; then ours+=("$name"); fi
  done
  (( ! ${#ours[@]} )) || compose "$FROM_IMAGE" "$FROM" stop "${ours[@]}"
  docker stop "${STOP_IDS[@]}" > /dev/null
fi

step "5/7 snapshot into $SNAP"
if ! snapshot; then
  (( ! ${#STOP_IDS[@]} )) || docker start "${STOP_IDS[@]}" > /dev/null || true
  die "the snapshot failed: ${STOPPED[*]:-nothing} were started again, nothing else changed"
fi

step "6/7 start $TAG: ${START[*]:-nothing}"
(( ! ${#STOP_IDS[@]} )) || docker rm "${STOP_IDS[@]}" > /dev/null
set_value RETINUE_IMAGE "$IMAGE"
set_value RETINUE_VERSION "$TAG"
if (( ${#START[@]} )); then   # none when the version only drops services
  compose "$IMAGE" "$TAG" up -d --no-build --no-deps "${START[@]}" || rollback "${START[*]} did not start"
  changed note "$TAG" "$CONFIG" "$PLAN" || rollback "what started could not be noted"
fi

step "7/7 health of $TAG"
wait_healthy "$(csv ${START[@]+"${START[@]}"})" || rollback "$LAST"
printf '\nupdated: %s -> %s, healthy; restarted %s; the snapshot of before: %s\n' "$FROM" "$TAG" "${START[*]:-nothing}" "$SNAP"
