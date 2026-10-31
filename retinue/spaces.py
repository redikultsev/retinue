"""Spaces: the Base read in the owner's app — its Spaces, a Space's Showcase and Records, one Record — through the
face's API and its login (docs/base.md, «Reading the Base in the app»).

Read through git only, from the platform's own repository of the clone (`/platform/base.git`) at its `main`: the
commit the clone is on, the hub's as of the last refresh. The clone's files are the lane's, and a session may have
put anything there — a link out of the tree, a FIFO, a file the size of the disk — so the tree is never opened here:
what the app shows is what the Base committed, never a session's draft. Git is called with the clone's own settings
(`BaseRepo.repo`, `BaseRepo.env`): no hook, no attributes, no config of the tree.

Path rules, the same for a Record asked for and a link resolved: a `*.md` file at most two folders below its Space
(`knowledge/acme/x.md`), no segment starting with a dot (so no `.cache`, no `.claude`), no `..`, no control
character; only a regular file of the commit (never a link: mode 120000, nor a submodule), at most `RECORD_MAX`.
Everything is cached by the commit: a new one drops the cache.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import subprocess
import textwrap
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

import yaml
from aiohttp import web

from .base import BRANCH_REF, clean_path

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.spaces")

SHOWCASE = "AGENTS.md"
MARKER = "<!-- Витрина:"
NATURES = ("knowledge", "profile", "journal", "artifacts")
SPACE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_SEGMENTS = 3                # nature/subfolder/file.md: at most two folders below the Space
PATH_MAX = 512
RECORD_MAX = 512 * 2**10        # a Record bigger than this is listed, never sent
SHOWCASE_MAX = 256 * 2**10
HEAD_MAX = 16 * 2**10           # the YAML head of a Record: past this it is not parsed
YAML_NODES_MAX = 2000           # a head with more values (an alias bomb) is not shown
VALUE_MAX = 2000                # one value of a head, in characters
TREE_MAX = 100_000              # entries of the commit looked at
SPACE_READ_MAX = 8 * 2**20      # bytes of Records one Space's listing reads; past it, names only
SMALL = 64 * 2**10              # a Record up to this is read whole for its head; a bigger one only its start
STARTS_MAX = 200                # bigger Records whose start one listing reads; past it, names only
RECORDS_MAX = 3000              # Records one Space lists
RECORD_CACHE = 64
GIT_TIMEOUT_S = 30
SHOWN = ("id", "title", "type", "status", "date", "verified", "stale_after", "updated", "from", "until", "practice")
REGULAR = ("100644", "100755")
LIST_ITEM = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
NONE_YET = re.compile(r"^пока\b[^.\n]*\bнет\b", re.IGNORECASE)   # «Пока открытых нет.»: a placeholder, not a line
# [text](target "title"), not an image; the text may hold one level of brackets.
LINK = re.compile(r"(?<!!)\[((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# A reference-style definition, `[label]: target "title"`, at the start of a line.
DEFINITION = re.compile(r"\A( {0,3})\[([^\]\n]+)\]:[ \t]*(<[^>\n]*>|\S+)[^\n]*")

NO_BASE = "The Base is not connected."
NOT_READY = "The Base is unavailable now — try again in a minute."


class Refused(Exception):
    """What the owner asked for cannot be shown: `status` and the reason in his words."""

    def __init__(self, status: int, why: str) -> None:
        super().__init__(why)
        self.status, self.why = status, why


class Unreadable(OSError):
    """The repository cannot be read now: not cloned yet, or git failed."""


def record_path(rel: object) -> str | None:
    """A Record's path inside its Space when the rules allow it, else None: `*.md`, at most two folders deep, no
    dot segment, nothing `clean_path` refuses."""
    path = clean_path(rel)
    if path is None or len(path) > PATH_MAX or "\\" in path:
        return None
    parts = path.split("/")
    if len(parts) > MAX_SEGMENTS or any(p.startswith(".") for p in parts):
        return None
    if not parts[-1].endswith(".md") or parts[-1] == ".md":
        return None
    return path


def space_name(name: object) -> str | None:
    return name if isinstance(name, str) and SPACE_NAME.fullmatch(name) else None


# --- parsers: pure, tested on their own ----------------------------------------------------------------------------

def _plain(value, budget: list[int]):
    """A YAML value as JSON can carry it: dates as ISO strings, anything else as text, each string cut to VALUE_MAX;
    `budget` counts the values, so a head that expands into thousands (aliases) stops being one."""
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("too many values")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:VALUE_MAX]
    if isinstance(value, (list, tuple)):
        return [_plain(v, budget) for v in value]
    if isinstance(value, dict):
        return {str(k)[:200]: _plain(v, budget) for k, v in value.items()}
    return str(value)[:VALUE_MAX]


def front_matter(text: str) -> tuple[dict | None, str]:
    """A Record's YAML head (safe YAML, as JSON can carry it) and its body. No head, or one that does not parse, is
    too long or too big: None, and the whole text is the body."""
    text = text.removeprefix("﻿")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, text
    found = re.search(r"\r?\n---[ \t]*(?:\r?\n|$)", text[3:HEAD_MAX + 3])
    if found is None:
        return None, text
    raw, body = text[3:3 + found.start()], text[3 + found.end():]
    try:
        head = yaml.safe_load(raw)
        if not isinstance(head, dict):
            return None, text
        return _plain(head, [YAML_NODES_MAX]), body
    except (yaml.YAMLError, ValueError, RecursionError):  # ValueError: a date that is no date, or too many values
        return None, text


def _outside_code(text: str, change) -> str:
    """`change` applied to the text outside fenced code blocks and inline code spans."""
    out, fence = [], None
    for line in text.splitlines(keepends=True):
        opened = FENCE.match(line)
        if fence is not None:
            out.append(line)
            if opened and opened.group(1) == fence:
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            out.append(line)
            continue
        parts = re.split(r"(`+[^`\n]*`+)", line)
        out.append("".join(p if i % 2 else change(p) for i, p in enumerate(parts)))
    return "".join(out)


def _link(href: str, here: str, target) -> str | None:
    """Where one link of a Record at Base folder `here` may go: an app route (`target` of the Base path), the web or
    mail address itself, or None — the link is dropped."""
    href = href.strip("<>")
    if SCHEME.match(href):
        return href if href.lower().startswith(("http://", "https://", "mailto:")) else None
    if href.startswith(("/", "#")):
        return None
    path = unquote(href.split("#", 1)[0].split("?", 1)[0])
    joined = posixpath.normpath(posixpath.join(here, path)) if path else here
    return None if joined.startswith("..") or joined.startswith("/") else target(joined)


def resolve_links(text: str, here: str, target) -> str:
    """Relative links of a Record (or a Showcase) at Base folder `here` turned into the app's routes: `target(path)`
    gives the route of a Base path, or None — then the link is dropped and its text stays. Web and mail links stay;
    anything else (`/abs`, `#anchor`, `file:`, `javascript:`, `data:`) is dropped. A reference-style definition
    (`[x]: path`) goes the same way; one dropped is removed, so its uses stay as text. Images are left as they are:
    the app shows them as text."""
    def one(match: re.Match) -> str:
        label, route = match.group(1), _link(match.group(2), here, target)
        return f"[{label}]({route})" if route else label

    def define(match: re.Match) -> str:
        route = _link(match.group(3), here, target)
        return f"{match.group(1)}[{match.group(2)}]: {route}" if route else ""

    return _outside_code(text, lambda chunk: LINK.sub(one, DEFINITION.sub(define, chunk)))


def _items(lines: list[str]) -> list[dict]:
    """A section's lines as items: a top-level list item with what is indented under it, or a paragraph."""
    out: list[dict] = []
    current: dict | None = None
    rest: list[str] = []
    blank = False

    def close() -> None:
        if current is not None and rest:
            current["text"] += "\n" + textwrap.dedent("\n".join(rest))
        rest.clear()

    for line in lines:
        if LIST_ITEM.match(line):
            close()
            current = {"text": LIST_ITEM.sub("", line, count=1).rstrip(), "list": True}
            out.append(current)
            blank = False
        elif not line.strip():
            blank = True
        elif current is not None and (line[:1] in (" ", "\t") or not blank):
            rest.append(line.rstrip())
            blank = False
        else:
            close()
            current = {"text": line.strip(), "list": False}
            out.append(current)
            blank = False
    close()
    return out


def section_kind(heading: str) -> str:
    words = heading.lower()
    if "открыт" in words and "вопрос" in words:
        return "questions"
    if words.startswith("задач"):
        return "tasks"
    if words.startswith("запис"):
        return "records"
    return "other"


def showcase(text: str, resolve=lambda t: t) -> dict:
    """A Showcase (a Space's AGENTS.md): its title (the first heading), the rules above the marker, and below it the
    lines by heading. `kind` names the sections the app knows: questions, tasks, records; the rest are `other`.
    «Пока … нет» in a list of questions or tasks is a placeholder and is left out. No marker: all of it is rules."""
    lines = text.splitlines()
    title = next((m.group(2) for line in lines if (m := HEADING.match(line)) and m.group(1) == "#"), "")
    at = next((i for i, line in enumerate(lines) if line.lstrip().startswith(MARKER)), None)
    above = lines if at is None else lines[:at]
    below = [] if at is None else lines[at + 1:]
    first = next((i for i, line in enumerate(above) if (m := HEADING.match(line)) and m.group(1) == "#"), None)
    rules = "\n".join(line for i, line in enumerate(above) if i != first).strip()
    sections: list[dict] = []
    heading, chunk, fence = "", [], None

    def flush() -> None:
        kind = section_kind(heading)
        items = _items(chunk)
        if kind in ("questions", "tasks"):
            items = [i for i in items if not (i["list"] and NONE_YET.match(i["text"]))]
        for item in items:
            item["text"] = resolve(item["text"])
        if heading or items:
            sections.append({"heading": heading, "kind": kind, "items": items})

    for line in below:
        opened = FENCE.match(line)
        if fence is None and (m := HEADING.match(line)) and len(m.group(1)) >= 2:
            flush()
            heading, chunk = m.group(2), []
            continue
        if opened:
            fence = None if fence == opened.group(1) else fence or opened.group(1)
        chunk.append(line)
    flush()
    return {"title": title, "rules": resolve(rules), "marker": at is not None, "sections": sections}


def counts(sections: list[dict]) -> dict:
    """Open questions and Tasks of a Showcase: its list items under those headings."""
    def n(kind: str) -> int:
        return sum(1 for s in sections if s["kind"] == kind for i in s["items"] if i["list"])
    return {"questions": n("questions"), "tasks": n("tasks")}


def _short(head: dict | None, rel: str) -> dict:
    """A Record's line in a listing: the fields of its head that the list shows, as text."""
    out: dict = {}
    for key in SHOWN:
        value = (head or {}).get(key)
        if value is not None and not isinstance(value, (dict, list)):
            out[key] = str(value)[:300]
    out["title"] = out.get("title") or posixpath.basename(rel)[:-3]
    return out


# --- the reader ------------------------------------------------------------------------------------------------------

@dataclass
class _Commit:
    head: str
    files: dict[str, tuple[str, str, int]]          # path -> (mode, blob, size); regular files only
    links: frozenset[str]                           # paths git keeps as something else (a link, a submodule)
    spaces: list[str]
    views: dict[str, dict] = field(default_factory=dict)
    records: OrderedDict = field(default_factory=OrderedDict)


class BaseTree:
    """The commit the clone is on, read through git: `git` is the command that names the repository (`--git-dir`
    and the clone's `-c` settings), `env` what git gets. Thread-safe; the cache follows the commit."""

    def __init__(self, git: list[str], env: dict) -> None:
        self.git, self.env = list(git), dict(env)
        self._lock = threading.Lock()
        self._commit: _Commit | None = None

    def _run(self, *args: str, data: bytes | None = None) -> bytes:
        try:
            run = subprocess.run([*self.git, *args], input=data, capture_output=True, env=self.env,
                                 timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            raise Unreadable(str(e)) from None
        if run.returncode != 0:
            raise Unreadable(run.stderr.decode("utf-8", "replace").strip()[:500])
        return run.stdout

    def commit(self) -> _Commit:
        """The commit `main` names now, listed once."""
        head = self._run("rev-parse", "--verify", "-q", f"{BRANCH_REF}^{{commit}}").decode().strip()
        with self._lock:
            if self._commit is not None and self._commit.head == head:
                return self._commit
        raw = self._run("ls-tree", "-r", "-l", "-z", "--full-tree", head).decode("utf-8", "surrogateescape")
        files: dict[str, tuple[str, str, int]] = {}
        links: set[str] = set()
        for n, entry in enumerate(filter(None, raw.split("\0"))):
            if n >= TREE_MAX:
                log.warning("spaces: the Base's commit %s has more than %d entries; the rest are not shown", head[:12],
                            TREE_MAX)
                break
            meta, _, path = entry.partition("\t")
            mode, kind, sha, size = meta.split()
            if kind == "blob" and mode in REGULAR and size.isdigit():
                files[path] = (mode, sha, int(size))
            else:
                links.add(path)
        spaces = sorted(name for path in files if path.endswith("/" + SHOWCASE) and path.count("/") == 1
                        and space_name(name := path.split("/")[0]))
        made = _Commit(head, files, frozenset(links), spaces)
        with self._lock:
            self._commit = made
        return made

    def blobs(self, shas: list[str]) -> dict[str, bytes]:
        """Several blobs in one call (`cat-file --batch`)."""
        if not shas:
            return {}
        out = self._run("cat-file", "--batch", data="".join(f"{s}\n" for s in dict.fromkeys(shas)).encode())
        found, at = {}, 0
        while at < len(out):
            end = out.index(b"\n", at)
            header = out[at:end].decode().split()
            if len(header) != 3:            # «<sha> missing»
                at = end + 1
                continue
            size = int(header[2])
            found[header[0]] = out[end + 1:end + 1 + size]
            at = end + 1 + size + 1
        return found

    def _start(self, sha: str, limit: int) -> bytes:
        """The first `limit` bytes of a blob, without holding the rest: git is stopped once they are read."""
        try:
            with subprocess.Popen([*self.git, "cat-file", "blob", sha], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=self.env) as run:
                data = run.stdout.read(limit)
                run.kill()
        except OSError as e:
            raise Unreadable(str(e)) from None
        return data

    # --- what the API answers ---------------------------------------------------------------------------------------

    def route(self, made: _Commit, path: str) -> str | None:
        """The app's route of a Base path, or None when the app does not show it (or the commit has no such file)."""
        parts = path.rstrip("/").split("/")
        name = parts[0]
        if name not in made.spaces:
            return None
        rel = "/".join(parts[1:])
        if rel in ("", SHOWCASE):
            return f"/spaces/{name}"
        rel = record_path(rel)
        if rel is None or f"{name}/{rel}" not in made.files:
            return None
        return f"/spaces/{name}/r/{quote(rel, safe='/')}"

    def _resolver(self, made: _Commit, here: str):
        return lambda text: resolve_links(text, here, lambda path: self.route(made, path))

    def spaces(self) -> tuple[str, list[dict]]:
        """The grid: each Space's Showcase only — its title and counts; no Record is read."""
        made = self.commit()
        return made.head, [{"name": name, "title": shown["title"] or name, **shown["counts"]}
                           for name in made.spaces for shown in [self._showcase(made, name)]]

    def space(self, name: str) -> tuple[str, dict]:
        made = self.commit()
        if space_name(name) not in made.spaces:
            raise Refused(404, "No such Space.")
        return made.head, self._view(made, name)

    def record(self, name: str, rel: str) -> tuple[str, dict]:
        made = self.commit()
        if space_name(name) not in made.spaces:
            raise Refused(404, "No such Space.")
        if (rel := record_path(rel)) is None:
            raise Refused(404, "The app does not show this kind of Record.")
        key = f"{name}/{rel}"
        with self._lock:
            if key in made.records:
                made.records.move_to_end(key)
                return made.head, made.records[key]
        if key not in made.files:
            raise Refused(404, "No such Record.")
        _, sha, size = made.files[key]
        if size > RECORD_MAX:
            raise Refused(413, f"The Record is large ({size // 1024} KB) — open it on a computer.")
        text = self.blobs([sha]).get(sha, b"").decode("utf-8", "replace")
        head, body = front_matter(text)
        parts = rel.split("/")
        view = {"space": name, "path": rel, "nature": parts[0] if len(parts) > 1 else "",
                "title": _short(head, rel)["title"], "head": head or {}, "bad_head": head is None and
                text.lstrip("﻿").startswith("---"), "body": self._resolver(made, posixpath.dirname(key))(body)}
        with self._lock:
            made.records[key] = view
            while len(made.records) > RECORD_CACHE:
                made.records.popitem(last=False)
        return made.head, view

    def _showcase(self, made: _Commit, name: str) -> dict:
        """A Space's Showcase parsed, cached by the commit apart from its Records: the grid needs only this."""
        key = f"showcase:{name}"
        with self._lock:
            if key in made.views:
                return made.views[key]
        _, sha, size = made.files[f"{name}/{SHOWCASE}"]
        raw = self.blobs([sha]).get(sha, b"") if size <= SHOWCASE_MAX else self._start(sha, SHOWCASE_MAX)
        parsed = showcase(raw.decode("utf-8", "replace"), self._resolver(made, name))
        shown = {"name": name, **parsed, "counts": counts(parsed["sections"]), "big": size > SHOWCASE_MAX}
        with self._lock:
            made.views[key] = shown
        return shown

    def _view(self, made: _Commit, name: str) -> dict:
        """One Space whole — its Showcase and its Records —, built only when that Space is opened."""
        with self._lock:
            if name in made.views:
                return made.views[name]
        view = {**self._showcase(made, name), "records": self._records(made, name)}
        with self._lock:
            made.views[name] = view
        return view

    def _records(self, made: _Commit, name: str) -> dict[str, list[dict]]:
        """The Space's Records by nature, one level of subfolders, each with what its head says."""
        found: list[tuple[str, str, int]] = []
        for path, (_, sha, size) in made.files.items():
            if not path.startswith(name + "/"):
                continue
            rel = record_path(path[len(name) + 1:])
            if rel is None or rel.split("/")[0] not in NATURES or rel.count("/") < 1:
                continue
            found.append((rel, sha, size))
        found.sort()
        found = found[:RECORDS_MAX]
        # Only a head is wanted: a small Record is read whole in one batch, a bigger one only its start (ls-tree gave
        # the sizes), within a budget of bytes and of starts.
        budget, starts, wanted, big = SPACE_READ_MAX, STARTS_MAX, [], []
        for _, sha, size in found:
            if size > RECORD_MAX:
                continue
            if size <= SMALL and size <= budget:
                budget -= size
                wanted.append(sha)
            elif size > SMALL and starts > 0 and budget >= HEAD_MAX:
                starts, budget = starts - 1, budget - HEAD_MAX
                big.append(sha)
        texts = self.blobs(wanted)
        texts.update({sha: self._start(sha, HEAD_MAX + 64) for sha in big})
        out: dict[str, list[dict]] = {nature: [] for nature in NATURES}
        for rel, sha, size in found:
            head = front_matter(texts[sha].decode("utf-8", "replace"))[0] if sha in texts else None
            parts = rel.split("/")
            out[parts[0]].append({"path": rel, "folder": parts[1] if len(parts) == 3 else "", **_short(head, rel),
                                  **({"big": True} if size > RECORD_MAX else {})})
        for nature, rows in out.items():
            if nature == "journal":   # newest first within a folder
                rows.sort(key=lambda r: r.get("date") or posixpath.basename(r["path"]), reverse=True)
            else:
                rows.sort(key=lambda r: r["title"].lower())
            rows.sort(key=lambda r: r["folder"])
        return out


# --- the face's routes -----------------------------------------------------------------------------------------------

def _badges(sessions: list[dict]) -> dict[str, dict]:
    """Per sphere: sessions waiting on the owner and sessions working, as the app's list counts them."""
    out: dict[str, dict] = {}
    for s in sessions:
        if not s.get("sphere") or s.get("state") == "closed" and not s.get("waiting"):
            continue
        row = out.setdefault(s["sphere"], {"waiting": 0, "working": 0})
        if s.get("state") == "waiting" or s.get("waiting"):
            row["waiting"] += 1
        elif s.get("state") == "active":
            row["working"] += 1
    return out


class Spaces:
    """`/api/spaces…` for the face; the face's guard (its login) is in front of every route."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self._tree: BaseTree | None = None

    def routes(self) -> list:
        return [web.get("/api/spaces", self.list), web.get("/api/spaces/{space}", self.one),
                web.get("/api/spaces/{space}/r/{path:.+}", self.record)]

    def tree(self) -> BaseTree:
        base = self.platform.base
        git, env = getattr(base, "repo", None), getattr(base, "env", None)
        if not git or env is None:
            raise Refused(404, NO_BASE)
        if self._tree is None or self._tree.git != git:
            self._tree = BaseTree(git, env)
        return self._tree

    async def _answer(self, work) -> web.Response:
        """`work()` in a worker thread (git runs there), its `(head, data)` as the answer."""
        try:
            head, data = await asyncio.to_thread(work)
        except Refused as e:
            return web.json_response({"error": e.why}, status=e.status)
        except Unreadable as e:
            log.warning("spaces: the Base cannot be read: %s", e)
            return web.json_response({"error": NOT_READY}, status=503)
        return web.json_response({"head": head, **data})

    async def list(self, request: web.Request) -> web.Response:
        badges = _badges(self.platform.store.face_sessions(self.platform.clock()))

        def work() -> tuple[str, dict]:
            head, rows = self.tree().spaces()
            return head, {"spaces": [{**row, **badges.get(row["name"], {"waiting": 0, "working": 0})}
                                     for row in rows]}
        return await self._answer(work)

    async def one(self, request: web.Request) -> web.Response:
        def work() -> tuple[str, dict]:
            head, view = self.tree().space(request.match_info["space"])
            return head, {"space": view}
        return await self._answer(work)

    async def record(self, request: web.Request) -> web.Response:
        def work() -> tuple[str, dict]:
            head, view = self.tree().record(request.match_info["space"], request.match_info["path"])
            return head, {"record": view}
        return await self._answer(work)
