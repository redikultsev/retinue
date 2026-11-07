#!/usr/bin/env bash
# On the owner's computer: the owner's skills, as his Claude Code there has them, put together into one folder for the server —
#   - his own, each folder of ~/.claude/skills (a linked one as its files), under its own name;
#   - each enabled plugin's (settings.json `enabledPlugins`, plugins/installed_plugins.json), under the plugin's name
#     as a prefix (`acme-skills` → `acme-tdd`): the lanes load the folder as one plugin, so two skills of
#     one name would clash;
#   - the desktop app's (`anthropic-skills`: its newest manifest.json, the skills it marks enabled), as `anthropic-…`.
# A plugin's skill whose own name is one of his skills is left out: his copy wins. Each SKILL.md gets its folder's
# name and loses the frontmatter keys the lanes refuse (`allowed-tools`, `model`, `hooks`… — said, never silently),
# then each skill is checked as the server checks it (retinue/instructions.py prune-skills, check-skills): a refused
# one is left out and named. With no host it prints the two commands that send the folder and install it; with a host
# it runs them (a tar over ssh — the server needs no rsync — then install-skills.sh under sudo, which asks).
#
#   bash deploy/skills/sync-skills.sh               # put together, check, print the commands
#   bash deploy/skills/sync-skills.sh <ssh-host>    # and send and install
#
# Where things are (defaults in brackets):
#   CLAUDE_CONFIG_DIR       Claude Code's folder on the owner's computer [~/.claude]
#   RETINUE_DESKTOP_SKILLS  the desktop app's skills [macOS: ~/Library/Application Support/Claude/
#                           local-agent-mode-sessions/skills-plugin]; on Windows and Linux the app keeps them in a
#                           folder of its own data (on Windows under %LOCALAPPDATA%): name its
#                           local-agent-mode-sessions/skills-plugin here, or the desktop app's skills are left out
#   RETINUE_SKILLS_OUT      the folder to put them in [a new temporary one]; its skills/ is replaced
#   RETINUE_SKILLS_EXCLUDE  names to leave out, space-separated (`anthropic-computer-use anthropic-morning`)
#   RETINUE_REMOTE_SKILLS   the folder on the server they are sent to [retinue-skills, in the ssh user's home]
#   RETINUE_REMOTE_REPO     the repository on the server [/opt/retinue]
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CLAUDE=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
DESKTOP=${RETINUE_DESKTOP_SKILLS:-$HOME/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin}
OUT=${RETINUE_SKILLS_OUT:-$(mktemp -d)/retinue-skills}
REMOTE=${RETINUE_REMOTE_SKILLS:-retinue-skills}
REMOTE_REPO=${RETINUE_REMOTE_REPO:-/opt/retinue}
host=${1:-}
[[ $# -le 1 ]] || { echo "usage: sync-skills.sh [ssh-host]" >&2; exit 2; }

if [[ -e $OUT && $(ls -A "$OUT") != "" && $(ls -A "$OUT") != skills ]]; then
  echo "$OUT: holds more than skills/ — not emptied; name a new folder" >&2; exit 1
fi
rm -rf "$OUT/skills"
mkdir -p "$OUT/skills"
python3 -I - "$CLAUDE" "$DESKTOP" "$OUT/skills" "${RETINUE_SKILLS_EXCLUDE:-}" <<'PY'
from __future__ import annotations

import json, shutil, sys
from pathlib import Path

claude, desktop, out, exclude = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), set(sys.argv[4].split())
taken: dict[str, str] = {}
personal: set[str] = set()

def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def skills_in(folder: Path) -> list[Path]:
    """A skill folder itself, or the skill folders in it."""
    if (folder / "SKILL.md").is_file():
        return [folder]
    return sorted(d for d in folder.glob("*/") if (d / "SKILL.md").is_file()) if folder.is_dir() else []

def take(src: Path, name: str, origin: str) -> None:
    if name in exclude:
        print(f"excluded {name} ({origin})")
    elif name in taken:
        print(f"skipped {name} ({origin}): the name is {taken[name]}'s")
    else:
        try:  # a link is copied as what it leads to: the server's check refuses links
            shutil.copytree(src, out / name, symlinks=False, ignore_dangling_symlinks=True)
        except (OSError, shutil.Error) as exc:
            shutil.rmtree(out / name, ignore_errors=True)
            print(f"skipped {name} ({origin}): not copied ({type(exc).__name__})")
            return
        taken[name] = origin

def plugin(root: Path, listed: list, origin: str, only: set[str] | None = None) -> None:
    meta = load(root / ".claude-plugin" / "plugin.json")
    pname = str(meta.get("name") or origin.split("@")[0])
    prefix = pname[: -len("-skills")] if pname.endswith("-skills") else pname
    found = [s for p in listed if isinstance(p, str) for s in skills_in(root / p)] + skills_in(root / "skills")
    for src in dict.fromkeys(found):
        if only is not None and src.name not in only:
            continue
        if src.name in personal:
            print(f"skipped {pname}:{src.name}: the owner's own {src.name} wins")
            continue
        take(src, f"{prefix}-{src.name}", pname)

for src in skills_in(claude / "skills") if (claude / "skills").is_dir() else []:
    personal.add(src.name)
    take(src, src.name, "personal")

enabled = load(claude / "settings.json").get("enabledPlugins") or {}
installed = load(claude / "plugins" / "installed_plugins.json").get("plugins") or {}
for key in sorted(k for k, on in enabled.items() if on is True):
    entries = [e for e in installed.get(key) or [] if isinstance(e, dict) and e.get("installPath")]
    if not entries:
        print(f"plugin {key}: enabled, not installed")
        continue
    entry = next((e for e in entries if e.get("scope") == "user"), entries[0])
    root = Path(entry["installPath"])
    listed = load(root / ".claude-plugin" / "plugin.json").get("skills") or []
    plugin(root, [listed] if isinstance(listed, str) else list(listed), key)

manifests = sorted(desktop.glob("*/*/manifest.json"), key=lambda p: p.stat().st_mtime) if desktop.is_dir() else []
if manifests:  # the app keeps one copy per account it has seen; the newest is the one in use
    on = {s.get("name") for s in load(manifests[-1]).get("skills") or [] if s.get("enabled") is not False}
    plugin(manifests[-1].parent, [], "anthropic-skills", only={n for n in on if isinstance(n, str)})
print(f"gathered {len(taken)} skills")
PY

python3 -I "$REPO/retinue/instructions.py" prune-skills --strip "$OUT"
python3 -I "$REPO/retinue/instructions.py" check-skills "$OUT"

send() { COPYFILE_DISABLE=1 tar --no-xattrs -C "$OUT" -cf - skills | ssh "$host" "rm -rf ~/$REMOTE && mkdir -p ~/$REMOTE && tar -C ~/$REMOTE --strip-components=1 -xf -"; }
install=(ssh -t "$host" "sudo bash $REMOTE_REPO/deploy/skills/install-skills.sh ~/$REMOTE")
if [[ -z $host ]]; then
  echo "checked in $OUT. To send and install (<ssh-host>: the server):"
  echo "  tar -C '$OUT' -cf - skills | ssh <ssh-host> 'rm -rf ~/$REMOTE && mkdir -p ~/$REMOTE && tar -C ~/$REMOTE --strip-components=1 -xf -'"
  echo "  ssh -t <ssh-host> 'sudo bash $REMOTE_REPO/deploy/skills/install-skills.sh ~/$REMOTE'"
  exit 0
fi
send
"${install[@]}"
