#!/usr/bin/env bash
# Care of the Retinue backup from the owner's own computer, with the key that may delete (the server's may not).
#   mac.sh monthly   check every byte, restore the latest snapshot and open its databases, copy to a local
#                    repository, and only then forget old snapshots and prune. Stops at the first failure.
#   mac.sh check | restore | copy | forget   one step
# Settings: $RETINUE_BACKUP_ENV (default ~/.config/retinue/backup.env, mode 600) — see docs/backup.md.
set -euo pipefail

ENV_FILE=${RETINUE_BACKUP_ENV:-$HOME/.config/retinue/backup.env}
[[ -f $ENV_FILE ]] || { echo "no $ENV_FILE: see docs/backup.md" >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a
: "${RESTIC_REPOSITORY:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}" "${RETINUE_LOCAL_REPO:?}"
RESTIC=${RESTIC:-restic}
PYTHON=${PYTHON:-python3}
HOST=${RETINUE_BACKUP_HOST:-retinue}
SNAPSHOTS=(--host "$HOST" --tag retinue)
# Time-based, so that a compromised server making many fake snapshots cannot push the real ones out.
KEEP=(--keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 6m)
# The local repository has the same password as this computer's key to the remote one.
[[ -n ${RESTIC_PASSWORD:-} ]] && export RESTIC_FROM_PASSWORD=$RESTIC_PASSWORD
[[ -n ${RESTIC_PASSWORD_COMMAND:-} ]] && export RESTIC_FROM_PASSWORD_COMMAND=$RESTIC_PASSWORD_COMMAND
RESTORED=""
trap 'rm -rf ${RESTORED:+"$RESTORED"}' EXIT   # the restored copy holds the archive: never left behind

check() { "$RESTIC" check --read-data; }

restore() {
  RESTORED=$(mktemp -d "${TMPDIR:-/tmp}/retinue-restore.XXXXXX")
  "$RESTIC" restore latest "${SNAPSHOTS[@]}" --target "$RESTORED"
  # The restore counts when every database opens whole and the archive has last night's events.
  "$PYTHON" - "$RESTORED" <<'PY'
import sqlite3, sys, time
from pathlib import Path

found = {p.name: p for p in Path(sys.argv[1]).rglob("*.sqlite") if "staging" in p.parts}
missing = {"router.sqlite", "archive.sqlite", "agent.sqlite"} - set(found)
if missing:
    sys.exit(f"restore: not in the snapshot: {', '.join(sorted(missing))}")
for name, path in sorted(found.items()):
    try:
        result = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        result = str(exc)
    if result != "ok":
        sys.exit(f"restore: {name}: {result}")
last = sqlite3.connect(f"{found['archive.sqlite'].as_uri()}?mode=ro", uri=True).execute(
    "SELECT max(ts) FROM events").fetchone()[0]
if not last or time.time() - last > 2 * 86400:
    sys.exit("restore: archive.sqlite has no events from the last two days")
print(f"integrity ok: {', '.join(sorted(found))}; last archive event {round((time.time() - last) / 3600)} h ago")
PY
}

copy() {
  [[ -f $RETINUE_LOCAL_REPO/config ]] ||
    "$RESTIC" -r "$RETINUE_LOCAL_REPO" init --from-repo "$RESTIC_REPOSITORY" --copy-chunker-params
  "$RESTIC" -r "$RETINUE_LOCAL_REPO" copy --from-repo "$RESTIC_REPOSITORY" "${SNAPSHOTS[@]}"
}

# The server's key may not delete, its own locks included: each night leaves one behind. Only stale ones go.
forget() { "$RESTIC" unlock; "$RESTIC" forget "${SNAPSHOTS[@]}" "${KEEP[@]}" --prune; }

case ${1:-} in
  monthly) check; restore; copy; forget; "$RESTIC" -r "$RETINUE_LOCAL_REPO" check ;;
  check | restore | copy | forget) "$1" ;;
  *) echo "usage: $0 monthly | check | restore | copy | forget" >&2; exit 2 ;;
esac
