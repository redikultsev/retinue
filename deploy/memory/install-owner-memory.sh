#!/usr/bin/env bash
# The owner's memory for the room: each top-level *.md of <source> (his Claude Code memory folder on the Mac,
# ~/.claude/projects/<project>/memory: MEMORY.md and one file per memory) is copied into
# /srv/retinue/owner-memory/memory, checked as the lane host checks it (retinue/instructions.py: plain Markdown files,
# MEMORY.md among them, no link, sizes bounded, nothing that looks like a credential) and swapped in whole.
# lane-private mounts /srv/retinue/owner-memory read-only; a session's next run sees the new snapshot, no restart.
#
#   sudo bash deploy/memory/install-owner-memory.sh ~/retinue-owner-memory    # from the Mac: rsync of the folder
#   sudo bash deploy/memory/install-owner-memory.sh --empty                   # no memory
#
# The source is a folder of the user who ran sudo, so it is read as that user (tar under `sudo -u $SUDO_USER`), never
# as root: a link or a name it planted reaches nothing root could read and he could not. Only regular files with a
# plain *.md name are taken; anything else — a link, a FIFO, a folder, another name — is skipped and named, and a FIFO
# is never opened. A refused or broken install leaves the installed snapshot as it was.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DEST=${RETINUE_OWNER_MEMORY:-/srv/retinue/owner-memory}
src=${1:?usage: install-owner-memory.sh <memory folder> | --empty}
NAME='^[A-Za-z0-9][A-Za-z0-9._-]{0,120}[.]md$'   # instructions.MEMORY_NAME

as_owner() {  # read the source as the user who ran sudo; as yourself when not under sudo
  if [[ $(id -u) == 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then sudo -u "$SUDO_USER" -- "$@"; else "$@"; fi
}

if [[ $src != --empty ]]; then
  as_owner test -d "$src" || { echo "$src: not a folder you can read" >&2; exit 1; }
fi

install -d -m 755 "$DEST"
parent=$(dirname "$DEST")
new=$(mktemp -d "$parent/.owner-memory-new.XXXXXX")   # beside it: one filesystem, so the swap is a rename
old="$parent/.owner-memory-old.$$"
restore() {  # whatever failed, the snapshot that was installed is installed still
  if [[ -e $old && ! -e $DEST/memory ]]; then mv "$old" "$DEST/memory"; fi
  rm -rf "$new" "$old"
}
trap restore EXIT
install -d -m 755 "$new/memory"
if [[ $src != --empty ]]; then
  names=()
  while IFS= read -r -d '' path; do
    name=${path##*/}
    if [[ -L $path || ! -f $path ]]; then
      echo "skipped $name: not a plain file"
    elif [[ ! $name =~ $NAME ]]; then
      echo "skipped $name: not a *.md name of A-Z a-z 0-9 . _ -"
    else
      names+=("$name")
    fi
  done < <(as_owner find "$src" -mindepth 1 -maxdepth 1 -print0)
  if ((${#names[@]})); then
    # The names are plain basenames: extraction lands in the new folder only. A file swapped for a link or a FIFO
    # after the look is archived as one, never read, and the check refuses it.
    as_owner tar -C "$src" -cf - --no-recursion -- "${names[@]}" | tar -C "$new/memory" -xf - --no-same-owner
  fi
fi
chmod -R u=rwX,go=rX "$new"
if [[ $(id -u) == 0 ]]; then chown -R root:root "$new"; fi
python3 -I "$REPO/retinue/instructions.py" check-memory "$new"

if [[ -e $DEST/memory ]]; then mv "$DEST/memory" "$old"; fi
mv "$new/memory" "$DEST/memory"
echo "installed in $DEST/memory"
