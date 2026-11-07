#!/usr/bin/env bash
# Deploy one commit of develop on the server that follows it (docs/install.md, «Following develop»).
# CI's last job runs it over ssh once the commit passed every check and its images are in the registry
# (.github/workflows/ci.yml: images, deploy), with a key whose authorized_keys line allows nothing else: the commit's
# sha arrives as SSH_ORIGINAL_COMMAND. By hand, as root: deploy.sh <sha>.
#
# Nothing is built here. The checkout goes to the commit (its compose file names its images), and deploy/update.sh
# switches to them as to a release — health, pull, stop, snapshot of the data, start, health, back to what ran before
# with its data when it is not healthy. Then the checkout follows: the commit's, or back to the one before.
set -euo pipefail

REPO=${RETINUE_REPO:-/opt/retinue}
ROOT=${RETINUE_ROOT:-/srv/retinue}
SNAPSHOTS=${RETINUE_SNAPSHOTS:-/var/backups/retinue/updates}
KEEP=${RETINUE_DEPLOY_KEEP:-10}   # the snapshots of the last ten switches stay

say() { printf 'deploy: %s\n' "$*"; }
value() { sed -n "s/^$1=//p" "$ROOT/stack.env" | tail -n 1; }   # never printed: stack.env holds secrets

# All in a function: bash reads a script as it runs, and this one checks out another version of itself.
main() {
  local sha=${SSH_ORIGINAL_COMMAND:-${1:-}} was origin image code
  [[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy.sh <a commit's full sha>" >&2; exit 2; }
  exec 9>"$ROOT/deploy.lock"
  flock 9
  trap '' HUP                                     # a dropped connection does not stop it halfway
  exec > >(tee -p -a "$ROOT/deploy.log") 2>&1     # -p: tee goes on into the log when the ssh side is gone
  was=$(git -C "$REPO" rev-parse HEAD)
  [[ $was != "$sha" ]] || { say "already on ${sha:0:12}"; exit 0; }

  # The images are in the registry of the repository the checkout comes from: github.com/<owner>/<repo>.
  image=${RETINUE_DEPLOY_IMAGE:-}
  if [[ -z $image ]]; then
    origin=$(git -C "$REPO" remote get-url origin)
    [[ $origin =~ github\.com[:/]([A-Za-z0-9_.-]+)/ ]] || { say "origin is not on GitHub: $origin"; exit 1; }
    image=ghcr.io/$(tr '[:upper:]' '[:lower:]' <<< "${BASH_REMATCH[1]}")/retinue
  fi

  say "${sha:0:12} (running ${was:0:12})"
  git -C "$REPO" fetch -q origin "$sha"
  git -C "$REPO" checkout -q --detach "$sha"
  if RETINUE_IMAGE=$image bash "$REPO/deploy/update.sh" "sha-${sha:0:12}"; then
    ls -1d "$SNAPSHOTS"/*/ 2>/dev/null | head -n "-$KEEP" | xargs -r rm -rf
    say "now on ${sha:0:12}"
    exit 0
  else
    code=$?
  fi
  git -C "$REPO" checkout -q --detach "$was"
  if (( code == 1 )); then   # update.sh went back, with the commit's compose file: the one before's, once more
    RETINUE_IMAGE=$(value RETINUE_IMAGE) RETINUE_VERSION=$(value RETINUE_VERSION) \
      docker compose -p retinue --env-file "$ROOT/stack.env" -f "$REPO/deploy/compose.yml" up -d --no-build || code=3
  fi
  say "${sha:0:12} was not deployed (update.sh exit $code); the checkout is back on ${was:0:12}"
  exit "$code"
}

main "$@"
