#!/usr/bin/env bash
# Care of the Retinue backup from the owner's own computer, with the key that may delete (the server's may not).
#   mac.sh monthly   check every byte, restore the latest snapshot and open its databases, copy to a local
#                    repository, and only then forget old snapshots and prune. Stops at the first failure.
#   mac.sh check | restore | copy | forget   one step
# A monthly run that went through tells the server when and which snapshot it proved (status/restore.json, over ssh
# to $RETINUE_SSH_HOST), and keeps that snapshot's id here too (verified-snapshot beside backup.env): a restore onto a
# new server starts from it, not from `latest`, which a compromised server could have written. The morning's health
# line says so once the check is 35 days old.
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
SSH=${SSH:-ssh}
HOST=${RETINUE_BACKUP_HOST:-retinue}
SNAPSHOTS=(--host "$HOST" --tag retinue)
# Time-based, so that many fake snapshots made at once cannot push the real ones out by count. The durations count
# back from the newest snapshot, not from now: one snapshot dated in the future would put every real one out of
# range — `trusted` refuses to go on while there is one.
KEEP=(--keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 6m)
# The copy on this computer outlives the remote one: two years.
LOCAL_KEEP=(--keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 24m)
VERIFIED=$(dirname "$ENV_FILE")/verified-snapshot
RESTIC_SERIES=0.19   # the series whose output this script reads (snapshots --json)
SNAPSHOT=
# The local repository has the same password as this computer's key to the remote one.
[[ -n ${RESTIC_PASSWORD:-} ]] && export RESTIC_FROM_PASSWORD=$RESTIC_PASSWORD
[[ -n ${RESTIC_PASSWORD_COMMAND:-} ]] && export RESTIC_FROM_PASSWORD_COMMAND=$RESTIC_PASSWORD_COMMAND
RESTORED=""
trap 'rm -rf ${RESTORED:+"$RESTORED"}' EXIT   # the restored copy holds the archive: never left behind

check() { "$RESTIC" check --read-data; }

# The newest snapshot, as long as none is dated more than an hour ahead of this computer's clock. The server writes
# the time it likes: a snapshot from the future is a forged one or a broken clock, and either way nothing may be
# deleted on its account. Sets SNAPSHOT.
trusted() {
  local version
  version=$("$RESTIC" version)
  [[ $version == "restic $RESTIC_SERIES."* ]] ||
    echo "warning: $version; this script reads the output of restic $RESTIC_SERIES" >&2
  SNAPSHOT=$("$RESTIC" snapshots --json "${SNAPSHOTS[@]}" | "$PYTHON" -c '
import json, re, sys
from datetime import datetime, timedelta, timezone

def when(text):  # restic: 2026-11-04T03:30:01.123456789+01:00 or …Z; Python before 3.11 takes neither as is
    return datetime.fromisoformat(re.sub(r"\.\d+", "", text).replace("Z", "+00:00"))

snapshots = json.load(sys.stdin) or []
ahead = datetime.now(timezone.utc) + timedelta(hours=1)
future = [s for s in snapshots if when(s["time"]) > ahead]
if future:
    short, time = future[0]["short_id"], future[0]["time"]
    sys.exit(f"snapshot {short} is dated {time}, in the future: forged or a broken clock on the server. "
             f"Nothing is deleted while it is there; look at it (restic snapshots) before removing it by hand")
if not snapshots:
    sys.exit("no snapshots")
print(max(snapshots, key=lambda s: when(s["time"]))["id"])
')
}

restore() {
  RESTORED=$(mktemp -d "${TMPDIR:-/tmp}/retinue-restore.XXXXXX")
  "$RESTIC" restore "$SNAPSHOT" --target "$RESTORED"
  # The restore counts when every database opens whole and the archive has last night's events.
  "$PYTHON" - "$RESTORED" <<'PY'
import os, sqlite3, sys, time
from pathlib import Path

root = Path(sys.argv[1])
found = {p.name: p for p in root.rglob("*.sqlite") if "staging" in p.parts}
# With the platform on (RETINUE_PLATFORM=1 in backup.env, or its database in the snapshot), its collections too:
# the platform makes the file at its start, uploads or not.
platform = os.environ.get("RETINUE_PLATFORM") == "1" or "platform.sqlite" in found
wanted = {"router.sqlite", "archive.sqlite", "agent.sqlite"} | ({"platform.sqlite", "collections.sqlite"} if platform else set())
missing = wanted - set(found)
if missing:
    sys.exit(f"restore: not in the snapshot: {', '.join(sorted(missing))}")
for name, path in sorted(found.items()):
    try:
        result = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        result = str(exc)
    if result != "ok":
        sys.exit(f"restore: {name}: {result}")
if "collections.sqlite" in found:  # every uploaded file its row names, whole, in its own volume (by its SHA-256)
    staged = found["collections.sqlite"]
    db = sqlite3.connect(f"{staged.as_uri()}?mode=ro", uri=True)
    rows = db.execute("SELECT sha256, size FROM files").fetchall()
    volume = staged.parent.name
    kept = {p.name: p.stat().st_size for p in root.rglob("attachments/*")
            if "staging" not in p.parts and volume in p.parts and p.is_file()}
    if lost := [sha for sha, size in rows if kept.get(sha) != size]:
        sys.exit(f"restore: {len(lost)} uploaded files not in the snapshot or not whole, e.g. {lost[0][:12]}")
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

# Only after every step went through: the server learns when its copies were last proven whole, and this computer
# keeps which snapshot that was.
report() {
  echo "$SNAPSHOT" > "$VERIFIED"
  echo "verified snapshot $SNAPSHOT (kept in $VERIFIED)"
  if [[ -z ${RETINUE_SSH_HOST:-} ]]; then
    echo "restore check done; set RETINUE_SSH_HOST in $ENV_FILE so the server knows (docs/backup.md)" >&2
    return 0
  fi
  printf '{"checked": %s, "snapshot": "%s"}\n' "$(date +%s)" "$SNAPSHOT" |
    "$SSH" "$RETINUE_SSH_HOST" 'sudo tee /srv/retinue/status/restore.json >/dev/null' ||
    echo "restore check done, but the server was not told: run it again or write status/restore.json" >&2
}

# The server's key may not delete, its own locks included: each night leaves one behind. Only stale ones go.
forget() {
  [[ -n $SNAPSHOT ]] || trusted
  "$RESTIC" unlock
  "$RESTIC" forget "${SNAPSHOTS[@]}" "${KEEP[@]}" --prune
  [[ ! -f $RETINUE_LOCAL_REPO/config ]] || "$RESTIC" -r "$RETINUE_LOCAL_REPO" forget "${SNAPSHOTS[@]}" "${LOCAL_KEEP[@]}" --prune
}

case ${1:-} in
  # Each night leaves the server's lock behind, and `check` wants the repository to itself: stale ones go first.
  monthly) "$RESTIC" unlock; trusted; check; restore; copy; forget; "$RESTIC" -r "$RETINUE_LOCAL_REPO" check; report ;;
  restore) trusted; restore ;;
  check | copy | forget) "$1" ;;
  *) echo "usage: $0 monthly | check | restore | copy | forget" >&2; exit 2 ;;
esac
