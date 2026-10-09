#!/usr/bin/env python3
"""The life hub's builder: the `lifehub-build` container, no network, its own user, standard library only.

Every POLL_S it looks at the router's data folder; when the data changed, it builds the site with Hugo into a new
release, checks every page and picture the build wrote (`gate`), and only then points `current` at it — one rename,
so nginx serves the old release or the new one, never half of one. A build or a check that fails leaves `current`
where it was and says why in build.json, which the router reads into the health line. The last KEEP releases stay.

Paths come from the environment, so the same script runs in the tests with a fake Hugo.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

DATA = Path(os.environ.get("LIFEHUB_DATA", "/data"))        # the router's JSON, charts and photos (read-only)
SITE = Path(os.environ.get("LIFEHUB_SITE", "/site"))        # releases/, current, build.json — nginx serves current
SOURCE = Path(os.environ.get("LIFEHUB_SOURCE", "/app/site"))  # the Hugo site: templates, content, CSS
HUGO = os.environ.get("LIFEHUB_HUGO", "/usr/local/bin/hugo")
WORK = Path(os.environ.get("LIFEHUB_WORK", "/tmp/lifehub"))  # a fresh copy of the site and the data per build
POLL_S = 15
RETRY_S = 300            # a failed build is tried again this late even if the data did not change
KEEP = 3                 # releases kept, the live one among them
HUGO_TIMEOUT_S = 120
# What a release may contain: pages, the stylesheet, the router's charts and re-encoded photos. Nothing that runs.
SUFFIXES = {".html", ".css", ".svg", ".jpg"}
NEVER = {"script", "style", "iframe", "object", "embed", "form", "base", "frame", "frameset", "applet", "link"}
ADDRESSES = {"href", "src", "action", "formaction", "xlink:href", "poster", "srcset", "data"}


class Failure(Exception):
    """The build or its check failed; the text goes to build.json and the owner's health line."""


class Gate(HTMLParser):
    """Everything in a page or an SVG that the site's policy would block or that would run: a script, a style, an
    event handler, an address with a scheme that runs, anything loaded from elsewhere."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bad: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in NEVER and not (tag == "link" and dict(attrs).get("href") == "/css/lifehub.css"):
            self.bad.append(f"<{tag}>")
        for name, value in attrs:
            value = (value or "").strip().lower()
            if name == "style" or name.startswith("on"):
                self.bad.append(f"{tag}[{name}]")
            elif name in ADDRESSES and value.startswith(("javascript:", "data:", "vbscript:", "//")):
                self.bad.append(f"{tag}[{name}={value[:30]}]")
            elif name in ADDRESSES and "://" in value and not (tag == "a" and value.startswith("https://")):
                self.bad.append(f"{tag}[{name}] from elsewhere: {value[:60]}")

    handle_startendtag = handle_starttag


def gate(folder: Path) -> list[str]:
    """What is wrong with a built release, file by file; empty when it may go live."""
    problems = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        where = path.relative_to(folder).as_posix()
        if path.suffix not in SUFFIXES:
            problems.append(f"{where}: такого файла в хабе быть не должно")
        elif path.suffix == ".jpg":
            if not path.read_bytes().startswith(b"\xff\xd8\xff"):
                problems.append(f"{where}: не JPEG")
        elif path.suffix in (".html", ".svg"):
            check = Gate()
            check.feed(path.read_text(errors="replace"))
            problems += [f"{where}: {bad}" for bad in dict.fromkeys(check.bad)]
    return problems


def fingerprint(folder: Path) -> str:
    """The data as it is now: every file's path and bytes. A file being written (.tmp-…) is not there yet."""
    digest = hashlib.sha256()
    if folder.is_dir():
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            relative = path.relative_to(folder).as_posix()
            if any(part.startswith(".") for part in relative.split("/")):
                continue
            digest.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build(now: float, data_hash: str) -> str:
    """One release from the data as it is: Hugo into releases/<stamp>.tmp, the gate, then live. Raises Failure."""
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(SOURCE, WORK)
    shutil.copytree(DATA, WORK / "assets", ignore=shutil.ignore_patterns(".*"), dirs_exist_ok=True)
    releases = SITE / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + f"-{data_hash[:8]}"
    target = releases / f"{name}.tmp"
    shutil.rmtree(target, ignore_errors=True)
    try:
        run = subprocess.run([HUGO, "build", "--source", str(WORK), "--destination", str(target), "--noBuildLock",
                              "--cacheDir", str(WORK.parent / "hugo-cache")], capture_output=True, text=True,
                             timeout=HUGO_TIMEOUT_S, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(WORK)})
        if run.returncode != 0:
            said = (run.stderr or run.stdout).strip().splitlines()
            raise Failure(f"Hugo: {said[-1][:300] if said else f'код {run.returncode}'}")
        problems = gate(target)
        if problems:
            raise Failure(f"проверка страниц: {'; '.join(problems[:5])}" + (f" и ещё {len(problems) - 5}"
                                                                            if len(problems) > 5 else ""))
        os.rename(target, releases / name)
    except subprocess.TimeoutExpired:
        raise Failure(f"Hugo не уложился в {HUGO_TIMEOUT_S} с") from None
    finally:
        shutil.rmtree(target, ignore_errors=True)
    link = SITE / "current.tmp"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(f"releases/{name}", link)  # relative: nginx sees the folder under another path
    os.replace(link, SITE / "current")
    for old in sorted(p for p in releases.iterdir() if p.is_dir())[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return name


def report(**status) -> None:
    """build.json, replaced whole: the router reads it."""
    SITE.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=SITE, prefix=".build-")
    with os.fdopen(handle, "w") as file:
        json.dump(status, file, ensure_ascii=False)
    os.chmod(temporary, 0o644)
    os.replace(temporary, SITE / "build.json")


def step(state: dict, now: float) -> bool:
    """One look: build when the data changed (or a failed build is due again). Returns whether it built."""
    data_hash = fingerprint(DATA)
    due = data_hash != state.get("hash") or (state.get("failed") and now - state["failed"] >= RETRY_S)
    if not due and (SITE / "current").exists():
        return False
    state["hash"] = data_hash
    try:
        release = build(now, data_hash)
    except (Failure, OSError, subprocess.SubprocessError) as exc:
        state["failed"] = now
        report(ok=False, at=now, error=str(exc)[:500], release=os.readlink(SITE / "current")
               if (SITE / "current").is_symlink() else None)
        print(f"lifehub: build failed: {exc}", file=sys.stderr, flush=True)
        return False
    state["failed"] = 0
    report(ok=True, at=now, error="", release=release)
    print(f"lifehub: {release} is live", flush=True)
    return True


def main() -> None:
    state: dict = {}
    print(f"lifehub builder: {DATA} -> {SITE}", flush=True)
    while True:
        step(state, time.time())
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
