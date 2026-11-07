"""The owner's computer has one word (#66): it is any of macOS, Windows and Linux, so nothing calls it «the Mac». Held
by a test, as the face's look is (face/src/test/design-guard.test.ts), so a new one fails the build:

- «Mac» or «Macs» nowhere — the docs, the code, the instructions, the app's strings, the tests. What is really one
  system's is said as that system: macOS, its Keychain, launchd.
- `mac` as a word only where the old name is read on purpose — the endpoint's old address, the history's old
  writers, rights kept before the rename —, and such a line, or the one above it, names #66. The tests are free to
  use it: they are what proves the old name still works.

The CHANGELOG keeps its history as it was written."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "test-results"}
SUFFIXES = {".py", ".md", ".ts", ".tsx", ".css", ".html", ".sh", ".yml", ".yaml", ".json", ".toml", ".conf",
            ".template", ".txt", ".service", ".timer"}
SKIP_FILES = {"CHANGELOG.md", "package-lock.json"}
CAPITAL = re.compile(r"(?:\b|\\[nrt])Macs?\b")  # after an escape too: "\nMac." in a string is a «Mac»
WORD = re.compile(r"(?:\b|\\[nrt])mac\b")


def texts() -> list[Path]:
    out = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        out += [Path(folder) / f for f in files if Path(f).suffix in SUFFIXES and f not in SKIP_FILES]
    return out


def test_the_owners_computer_is_never_the_mac():
    said = []
    for path in texts():
        if path == Path(__file__).resolve():
            continue  # this file names what it looks for
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        code = not rel.startswith(("tests/", "face/src/test/"))
        for n, line in enumerate(lines):
            if CAPITAL.search(line):
                said.append(f"{rel}:{n + 1}: «Mac» — the owner's computer, or macOS where only macOS is meant")
            elif code and WORD.search(line) and "#66" not in line and (n == 0 or "#66" not in lines[n - 1]):
                said.append(f"{rel}:{n + 1}: `mac` — `device`, or name #66 where the old name is read on purpose")
    assert said == []
