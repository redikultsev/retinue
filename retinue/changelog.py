"""«What's new»: the release running and, once after an update, what changed since the one the owner last saw.

The lines are CHANGELOG.md's, written for the owner (CONTRIBUTING.md, «Releases»): a `## vX.Y.Z — date` heading per
release, a `- ` line per change. The image carries the file inside the package (Dockerfile); a checkout reads it at
the repository's root. The release running is RETINUE_VERSION, which deploy/update.sh writes and compose hands the
platform; a stack built from a checkout runs `local`, which is no release and shows nothing.

What the owner last saw is one value in the platform's store (`SEEN`), set by his «Got it»: an install that has seen
none yet is shown the release it runs, an update every release after the one he saw, up to the one running.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SEEN = "whats_new.seen"
RELEASE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
HEADING = re.compile(r"^## +(v[0-9]+\.[0-9]+\.[0-9]+)(?: +[—–-] +(\S+))? *$")
LINE = re.compile(r"^[-*] +(\S.*)$")
FILES = (Path(__file__).with_name("CHANGELOG.md"), Path(__file__).resolve().parents[1] / "CHANGELOG.md")


def running() -> str:
    """The release running: RETINUE_VERSION, `local` when built from a checkout."""
    return os.environ.get("RETINUE_VERSION") or "local"


def _key(version: str) -> tuple[int, int, int] | None:
    found = RELEASE.fullmatch(version or "")
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def later(version: str, than: str | None) -> bool:
    """`version` is a release after `than` — or `than` is none."""
    now, before = _key(version), _key(than or "")
    return now is not None and (before is None or now > before)


def releases(text: str) -> list[dict]:
    """Every released section of the changelog, as written: version, date (or ""), its lines. «Unreleased» and any
    other heading are skipped; an indented line under a change carries it on."""
    out: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            heading = HEADING.match(line)
            current = {"version": heading[1], "date": heading[2] or "", "notes": []} if heading else None
            if current:
                out.append(current)
        elif current is not None and (item := LINE.match(line)):
            current["notes"].append(item[1])
        elif current is not None and current["notes"] and raw.startswith("  ") and line.strip():
            current["notes"][-1] += " " + line.strip()
    return out


def read() -> str:
    for path in FILES:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def unseen(version: str, seen: str | None, text: str) -> list[dict]:
    """What to show: the releases after `seen` up to `version`, the newest first — `version`'s alone when nothing was
    seen yet, none when `version` is no release or the owner saw it already. A release with no lines is left out."""
    now = _key(version)
    if now is None:
        return []
    since = _key(seen or "")
    shown = [r for r in releases(text) if (key := _key(r["version"])) and key <= now and r["notes"]
             and (key > since if since else key == now)]
    return sorted(shown, key=lambda r: _key(r["version"]), reverse=True)
