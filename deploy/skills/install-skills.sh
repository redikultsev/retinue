#!/usr/bin/env bash
# The owner's skills for the lanes: each folder of <source> that holds a SKILL.md (a folder of skills, as
# ~/.claude/skills on the Mac) is copied into /srv/retinue/skills/skills, checked as the lane host checks it
# (retinue/instructions.py: no link, no hook, no inline shell, nothing but skills) and swapped in whole. The room
# (lane-private) mounts /srv/retinue/skills read-only; a session's next run sees the new set, no restart.
#
#   sudo bash deploy/skills/install-skills.sh ~/retinue-skills    # from the Mac: rsync -rL (links as files)
#   sudo bash deploy/skills/install-skills.sh --empty             # no skills
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DEST=${RETINUE_SKILLS:-/srv/retinue/skills}
src=${1:?usage: install-skills.sh <folder of skills> | --empty}
[[ $src == --empty || -d $src ]] || { echo "$src: not a folder" >&2; exit 1; }

install -d -m 755 "$DEST"
parent=$(dirname "$DEST")
new=$(mktemp -d "$parent/.skills-new.XXXXXX")   # beside it: one filesystem, so the swap is a rename
old="$parent/.skills-old.$$"
trap 'rm -rf "$new" "$old"' EXIT
install -d -m 755 "$new/skills"
if [[ $src != --empty ]]; then
  for dir in "$src"/*/; do
    name=$(basename "$dir")
    if [[ -f $dir/SKILL.md ]]; then
      cp -R -P "${dir%/}" "$new/skills/$name"        # a link stays a link, and the check refuses it
    else
      echo "skipped $name: no SKILL.md"
    fi
  done
fi
chmod -R u=rwX,go=rX "$new"
if [[ $(id -u) == 0 ]]; then chown -R root:root "$new"; fi
python3 -I "$REPO/retinue/instructions.py" check-skills "$new"

# The mount is $DEST itself, so it stays; what is swapped is its one entry. A run that starts in between finds no
# skills/ and runs without them, as on a lane with none.
if [[ -e $DEST/skills ]]; then mv "$DEST/skills" "$old"; fi
mv "$new/skills" "$DEST/skills"
echo "installed in $DEST/skills"
