"""Spaces: the Base read in the owner's app — its projects, a project's Showcase, Records, files and sub-projects, one
Record, one file — through the face's API and its login (docs/base.md, «Reading the Base in the app»).

A project is any folder of the Base that holds an `AGENTS.md` (its Showcase), at any depth: projects nest, and a
project's sub-projects are the project folders below it with no other project between. A Space is a top-level
project. A file, a Record and a folder belong to the nearest project above them (`project_of`). A session started in a
project runs in its folder (`project_cwd`), so it gets the rules of every folder from the Base's root down to it.

Read through git only, from the platform's own repository of the clone (`/platform/base.git`) at its `main`: the
commit the clone is on, the hub's as of the last refresh. The clone's files are the lane's, and a session may have
put anything there — a link out of the tree, a FIFO, a file the size of the disk — so the tree is never opened here:
what the app shows is what the Base committed, never a session's draft. Git is called with the clone's own settings
(`BaseRepo.repo`, `BaseRepo.env`): no hook, no attributes, no config of the tree.

Path rules, the same for a Record asked for and a link resolved: a `*.md` file at most two folders below its project
(`knowledge/acme/x.md`), no segment starting with a dot (so no `.cache`, no `.claude`), no `..`, no control
character; only a regular file of the commit (never a link: mode 120000, nor a submodule), at most `RECORD_MAX`. A
file asked for by itself (`file`): any regular file of the commit inside a project, no dot segment, at most
`FILE_MAX`. A project's folder: segments of `SPACE_NAME`, at most `PROJECT_DEPTH`. Everything is cached by the commit:
a new one drops the cache.
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

from . import passport as passports
from .base import BRANCH_REF, clean_path

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.spaces")

SHOWCASE = passports.SHOWCASE
MARKER = passports.MARKER
NATURES = ("knowledge", "profile", "journal", "artifacts")
SPACE_NAME = passports.FOLDER
MAX_SEGMENTS = 3                # nature/subfolder/file.md: at most two folders below the project
PROJECT_DEPTH = passports.DEPTH  # folders of a project's path below the Base's root
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
FILES_MAX = 3000                # files one project lists
FILE_MAX = 25 * 2**20           # a file bigger than this is listed, never sent
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

SESSIONS_MAX = 200             # sessions one project's answer names
# A project's file served by itself: a document of no origin that loads nothing (as a session's, `face.FILE_CSP`).
FILE_CSP = "default-src 'none'; sandbox"

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
    """A Record's path inside its project when the rules allow it, else None: `*.md`, at most two folders deep, no
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


# --- projects: pure, on the set of a commit's project folders ------------------------------------------------------

def project_path(path: object) -> str | None:
    """A project's folder as the app names it: '' (the Base's root) or `/`-separated segments of `SPACE_NAME`, at
    most `PROJECT_DEPTH` of them; else None."""
    if path == "":
        return ""
    if not isinstance(path, str) or len(path) > PATH_MAX:
        return None
    parts = path.split("/")
    return path if len(parts) <= PROJECT_DEPTH and all(SPACE_NAME.fullmatch(p) for p in parts) else None


def project_of(projects: frozenset[str] | set[str], folder: str) -> str | None:
    """The project a Base folder belongs to: the folder itself when it is a project, else the nearest project above
    it; None when no project holds it (the Base's root is a project only with an `AGENTS.md` of its own)."""
    parts = folder.split("/") if folder else []
    for n in range(len(parts), -1, -1):
        if (above := "/".join(parts[:n])) in projects:
            return above
    return None


def parent_project(projects: frozenset[str] | set[str], project: str) -> str | None:
    """The project a project is a sub-project of; None for the root, and for a project no other one holds."""
    return project_of(projects, posixpath.dirname(project)) if project else None


def sub_projects(projects: frozenset[str] | set[str], project: str | None) -> list[str]:
    """A project's sub-projects: the project folders below it with no other project between, sorted. `None`: the
    projects no project holds — the Base's top ones when its root has no `AGENTS.md`."""
    return sorted(p for p in projects if p != "" and parent_project(projects, p) == project)


def project_cwd(tree: str, project: str) -> str:
    """A project's folder as a session's directory under the lane's /work: the clone's folder (`base.tree`), then
    the project's path below it."""
    tree = tree.strip("/")
    return f"{tree}/{project}" if project and tree else project or tree


def cwd_project(tree: str, cwd: str | None) -> str | None:
    """The Base folder a session's directory is — its project, when the folder holds an `AGENTS.md`: the path below
    the clone's folder, '' for the clone itself; None for a directory outside the Base."""
    tree, cwd = tree.strip("/"), (cwd or "").strip("/")
    if cwd == tree:
        return ""
    return cwd[len(tree) + 1:] if tree and cwd.startswith(tree + "/") else None


def file_path(rel: object) -> str | None:
    """A file of the Base the app may serve by itself, when the rules allow it: no dot segment, nothing `clean_path`
    refuses, no backslash, at most `PATH_MAX`."""
    path = clean_path(rel)
    if path is None or len(path) > PATH_MAX or "\\" in path or any(p.startswith(".") for p in path.split("/")):
        return None
    return path


# The type a file of the Base is served as, by its extension; any other is a download (`OTHER_MEDIA`).
MEDIA = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "avif": "image/avif", "svg": "image/svg+xml", "pdf": "application/pdf",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg", "oga": "audio/ogg",
    "opus": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "ogv": "video/ogg",
    "md": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "json": "application/json", "yaml": "text/plain; charset=utf-8", "yml": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8",
}
OTHER_MEDIA = "application/octet-stream"


def media_of(path: str) -> str:
    """The type a file is served as: by its extension (`MEDIA`), else a download."""
    name = posixpath.basename(path)
    return MEDIA.get(name.rsplit(".", 1)[-1].lower(), OTHER_MEDIA) if "." in name else OTHER_MEDIA


def kind_of(path: str) -> str:
    """What the app does with a file: image, pdf, audio, video, markdown, text, html — or other, a download."""
    mime = media_of(path).split(";")[0]
    if mime == "application/pdf":
        return "pdf"
    if mime in ("text/markdown", "text/html"):
        return mime.split("/")[1]
    if mime.startswith(("image/", "audio/", "video/")):
        return mime.split("/")[0]
    return "text" if mime.startswith("text/") or mime == "application/json" else "other"


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


def passport_view(told: passports.Passport, resolve=lambda t: t) -> dict:
    """A parsed passport as the app reads it: its parts with their links resolved, its `problems` against the schema
    (`passport.SCHEMA`), and `in_scope` and `not_in_scope` joined from its «Scope» lines."""
    scope = [{**line, "text": resolve(line["text"]), "where": resolve(line["where"])} for line in told.scope]
    return {"why": resolve(told.why),
            "in_scope": "; ".join(line["text"] for line in scope if line["key"] == "in"),
            "not_in_scope": "; ".join(f"{line['text']} → {line['where']}" if line["where"] else line["text"]
                                      for line in scope if line["key"] == "out"),
            "scope": scope,
            "files": [{**f, "about": resolve(f["about"])} for f in told.files],
            "rules": [resolve(rule) for rule in told.rules],
            "checks": [resolve(check) for check in told.checks],
            "imports": list(told.imports),
            "problems": [problem.as_dict() for problem in told.problems]}


def showcase(text: str, resolve=lambda t: t, exists=None) -> dict:
    """A Showcase (a project's AGENTS.md): its title (the first heading), its passport — everything above the marker,
    read by the schema (`passport.parse`; `exists` looks a path up in the project) — and below the marker the lines
    by heading. `rules` is the passport's «Rules» as a list, then whatever above the marker the schema does not name,
    so nothing written there goes unseen. `kind` names the sections the app knows: questions, tasks, records; the rest
    are `other`. «Пока … нет» in a list of questions or tasks is a placeholder and is left out. No marker: all of it
    is the passport."""
    lines = text.splitlines()
    title = next((m.group(2) for line in lines if (m := HEADING.match(line)) and m.group(1) == "#"), "")
    at = next((i for i, line in enumerate(lines) if line.lstrip().startswith(MARKER)), None)
    told = passports.parse("\n".join(lines if at is None else lines[:at]), exists)
    below = [] if at is None else lines[at + 1:]
    rules = "\n".join([*(f"- {rule}" for rule in told.rules), *told.other]).strip()
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
    shown = passport_view(told, resolve)
    return {"title": title, "passport": shown, "rules": resolve(rules), "marker": at is not None, "sections": sections}


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
    projects: frozenset[str]                        # every project's folder, '' for the root when it is one
    spaces: list[str]                               # the top-level projects
    folders: frozenset[str] = frozenset()           # every folder that holds a file of the commit
    views: dict[str, dict] = field(default_factory=dict)
    records: OrderedDict = field(default_factory=OrderedDict)


def _join(project: str, rel: str) -> str:
    return f"{project}/{rel}" if project else rel


def _parents(path: str) -> list[str]:
    """`a/b/c.md` → `a/b`, `a`."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]


class BaseTree:
    """The commit the clone is on, read through git: `git` is the command that names the repository (`--git-dir`
    and the clone's `-c` settings), `env` what git gets. Thread-safe; the cache follows the commit."""

    def __init__(self, git: list[str], env: dict) -> None:
        self.git, self.env = list(git), dict(env)
        self._lock = threading.Lock()
        self._commit: _Commit | None = None
        self._blob: tuple[str, bytes] | None = None      # the last file served whole: a video asked for in ranges

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
        projects = frozenset(folder for path in files if posixpath.basename(path) == SHOWCASE
                             and project_path(folder := posixpath.dirname(path)) is not None)
        folders = {parent for path in files for parent in _parents(path)}
        made = _Commit(head, files, frozenset(links), projects, sorted(p for p in projects if p and "/" not in p),
                       frozenset(folders))
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

    @staticmethod
    def project_route(project: str) -> str:
        """The app's route of a project: `/spaces/<space>` for a top-level one, `/spaces/<top>/p/<the rest>` below."""
        top, _, below = project.partition("/")
        return f"/spaces/{top}/p/{quote(below, safe='/')}" if below else f"/spaces/{top}"

    def route(self, made: _Commit, path: str) -> str | None:
        """The app's route of a Base path, or None when the app does not show it (or the commit has no such file): a
        project's folder or Showcase is the project, a Record is its project's Record."""
        path = path.rstrip("/")
        owner = project_of(made.projects, path if path in made.projects else posixpath.dirname(path))
        if not owner:                   # in no project, or in the root's: the app has no route there
            return None
        rel = path[len(owner) + 1:]
        if rel in ("", SHOWCASE):
            return self.project_route(owner)
        if record_path(rel) is None or path not in made.files:
            return None
        top, _, below = owner.partition("/")
        return f"/spaces/{top}/r/{quote(_join(below, rel), safe='/')}"

    def _resolver(self, made: _Commit, here: str):
        return lambda text: resolve_links(text, here, lambda path: self.route(made, path))

    def _row(self, made: _Commit, project: str) -> dict:
        shown = self._showcase(made, project)
        return {"name": project, "title": shown["title"] or posixpath.basename(project), **shown["counts"]}

    def spaces(self) -> tuple[str, list[dict]]:
        """The grid: the projects right below the Base's root — the Spaces, and a deeper project no project holds —,
        each by its Showcase only — its title and counts; no Record is read."""
        made = self.commit()
        top = sub_projects(made.projects, "" if "" in made.projects else None)
        return made.head, [self._row(made, project) for project in top]

    def projects(self) -> tuple[str, frozenset[str]]:
        """Every project's folder in the commit, '' for the root when it holds an `AGENTS.md`."""
        made = self.commit()
        return made.head, made.projects

    def title(self, project: str) -> str | None:
        """A project's title as the app lists it (its Showcase's first heading, else its folder's name); None for a
        folder that is no project."""
        made = self.commit()
        return self._row(made, project)["title"] if project and project in made.projects else None

    def space(self, name: str) -> tuple[str, dict]:
        made = self.commit()
        if space_name(name) not in made.spaces:
            raise Refused(404, "No such Space.")
        return made.head, self._view(made, name)

    def project(self, path: str) -> tuple[str, dict]:
        """One project at any depth ('' : the root), as a Space is shown: its Showcase, Records, files, sub-projects."""
        made = self.commit()
        if (path := project_path(path)) is None or path not in made.projects:
            raise Refused(404, "No such project.")
        return made.head, self._view(made, path)

    def record(self, name: str, rel: str) -> tuple[str, dict]:
        """A Record by its path below a Space's folder (`name`): it belongs to its nearest project, which may be a
        sub-project of that Space, and the path rules hold below that project."""
        made = self.commit()
        if space_name(name) is None:
            raise Refused(404, "No such Space.")
        if (whole := file_path(f"{name}/{rel}" if isinstance(rel, str) else None)) is None:
            raise Refused(404, "The app does not show this kind of Record.")
        owner = project_of(made.projects, posixpath.dirname(whole))
        if owner is None or owner.split("/")[0] != name:
            raise Refused(404, "No such Space.")
        inner = record_path(whole[len(owner) + 1:])
        if inner is None:
            raise Refused(404, "The app does not show this kind of Record.")
        key = whole
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
        parts = inner.split("/")
        view = {"space": name, "project": owner, "path": whole[len(name) + 1:],
                "nature": parts[0] if len(parts) > 1 else "", "title": _short(head, inner)["title"],
                "head": head or {}, "bad_head": head is None and text.lstrip("﻿").startswith("---"),
                "body": self._resolver(made, posixpath.dirname(key))(body)}
        with self._lock:
            made.records[key] = view
            while len(made.records) > RECORD_CACHE:
                made.records.popitem(last=False)
        return made.head, view

    def file(self, path: str) -> tuple[str, str, bytes]:
        """One file of a project by its Base path, whole, and the type it is served as: a regular file of the commit
        (never a link), no dot segment, at most `FILE_MAX`."""
        made = self.commit()
        if (path := file_path(path)) is None:
            raise Refused(404, "The app does not show this file.")
        if path not in made.files or project_of(made.projects, posixpath.dirname(path)) is None:
            raise Refused(404, "No such file.")
        _, sha, size = made.files[path]
        if size > FILE_MAX:
            raise Refused(413, f"The file is large ({size // 2**20} MB) — open it on a computer.")
        with self._lock:
            if self._blob is not None and self._blob[0] == sha:
                return made.head, media_of(path), self._blob[1]
        data = self.blobs([sha]).get(sha, b"")
        with self._lock:
            self._blob = (sha, data)
        return made.head, media_of(path), data

    def every(self) -> tuple[str, list[dict]]:
        """Every project below the root at any depth, by its Showcase only — its title, the project it is in, its
        counts —, the Showcases read in one call: the app's list of projects and the sessions under them."""
        made = self.commit()
        found = sorted(p for p in made.projects if p)
        wanted = [made.files[_join(p, SHOWCASE)][1] for p in found if made.files[_join(p, SHOWCASE)][2] <= SHOWCASE_MAX]
        texts = self.blobs(wanted)
        rows = []
        for project in found:
            shown = self._showcase(made, project, texts.get(made.files[_join(project, SHOWCASE)][1]))
            rows.append({"name": project, "title": shown["title"] or posixpath.basename(project),
                         "parent": parent_project(made.projects, project), **shown["counts"]})
        return made.head, rows

    def _showcase(self, made: _Commit, project: str, read: bytes | None = None) -> dict:
        """A project's Showcase parsed, cached by the commit apart from its Records: the grid needs only this. `read`:
        its bytes, when a batch has them already."""
        key = f"showcase:{project}"
        with self._lock:
            if key in made.views:
                return made.views[key]
        _, sha, size = made.files[_join(project, SHOWCASE)]
        raw = read if read is not None and size <= SHOWCASE_MAX else \
            self.blobs([sha]).get(sha, b"") if size <= SHOWCASE_MAX else self._start(sha, SHOWCASE_MAX)
        def exists(rel: str) -> bool:
            path = _join(project, rel.rstrip("/"))
            return path in made.files or path in made.folders

        parsed = showcase(raw.decode("utf-8", "replace"), self._resolver(made, project), exists)
        shown = {"name": project, **parsed, "counts": counts(parsed["sections"]), "big": size > SHOWCASE_MAX}
        with self._lock:
            made.views[key] = shown
        return shown

    def _view(self, made: _Commit, project: str) -> dict:
        """One project whole — its Showcase, Records, files and sub-projects —, built only when it is opened."""
        with self._lock:
            if project in made.views:
                return made.views[project]
        view = {**self._showcase(made, project), "path": project,
                "parent": parent_project(made.projects, project), "records": self._records(made, project),
                **self._files(made, project),
                "projects": [self._row(made, p) for p in sub_projects(made.projects, project)]}
        with self._lock:
            made.views[project] = view
        return view

    def _own(self, made: _Commit, project: str):
        """(path inside the project, Base path, blob, size) of each regular file that is the project's own: below its
        folder, in no sub-project, no dot segment."""
        prefix = _join(project, "")
        for path, (_, sha, size) in made.files.items():
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if any(p.startswith(".") for p in rel.split("/")):
                continue
            if project_of(made.projects, posixpath.dirname(path)) != project:
                continue
            yield rel, path, sha, size

    def _files(self, made: _Commit, project: str) -> dict:
        """Every file of the project — its Records too — with its size and what the app does with it, by path; at
        most `FILES_MAX`, the rest counted."""
        found = sorted(self._own(made, project))
        rows = [{"path": rel, "size": size, "kind": kind_of(rel), **({"big": True} if size > FILE_MAX else {})}
                for rel, _, _, size in found[:FILES_MAX]]
        return {"files": rows, "more_files": max(0, len(found) - FILES_MAX)}

    def _records(self, made: _Commit, project: str) -> dict[str, list[dict]]:
        """The project's Records by nature, one level of subfolders, each with what its head says."""
        found: list[tuple[str, str, int]] = []
        for rel, _, sha, size in self._own(made, project):
            rel = record_path(rel)
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
    """`/api/spaces…`, `/api/projects…` and a project's files (`/api/base/…`) for the face; the face's guard (its
    login) is in front of every route."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self._tree: BaseTree | None = None

    def routes(self) -> list:
        return [web.get("/api/spaces", self.list), web.get("/api/spaces/{space}", self.one),
                web.get("/api/spaces/{space}/r/{path:.+}", self.record),
                web.get("/api/projects", self.project), web.get("/api/projects/{path:.+}", self.project),
                web.get("/api/base/{path:.+}", self.file), web.get("/api/base-view/{path:.+}", self.view)]

    def tree(self) -> BaseTree:
        base = self.platform.base
        git, env = getattr(base, "repo", None), getattr(base, "env", None)
        if not git or env is None:
            raise Refused(404, NO_BASE)
        if self._tree is None or self._tree.git != git:
            self._tree = BaseTree(git, env)
        return self._tree

    def sessions_in(self, project: str) -> list[str]:
        """The sessions started in a project's folder (`sessions.cwd`), the latest first; archived ones left out."""
        tree = getattr(self.platform.base, "tree_rel", None)
        if not isinstance(tree, str):
            return []
        rows = self.platform.store.db.execute(
            "SELECT id FROM sessions WHERE cwd = ? AND archived IS NULL ORDER BY at DESC LIMIT ?",
            (project_cwd(tree, project), SESSIONS_MAX)).fetchall()
        return [row[0] for row in rows]

    async def _answer(self, work, sessions=None) -> web.Response:
        """`work()` in a worker thread (git runs there), its `(head, data)` as the answer; `sessions(data)`, in the
        loop (the store is the loop's), adds the sessions of what it read."""
        try:
            head, data = await asyncio.to_thread(work)
        except Refused as e:
            return web.json_response({"error": e.why}, status=e.status)
        except Unreadable as e:
            log.warning("spaces: the Base cannot be read: %s", e)
            return web.json_response({"error": NOT_READY}, status=503)
        return web.json_response({"head": head, **data, **({"sessions": sessions(data)} if sessions else {})})

    async def list(self, request: web.Request) -> web.Response:
        badges = _badges(self.platform.store.face_sessions(self.platform.clock()))

        def work() -> tuple[str, dict]:
            tree = self.tree()
            head, rows = tree.spaces()
            # every project at any depth beside the grid: the app's list puts the sessions under them
            return head, {"spaces": [{**row, **badges.get(row["name"], {"waiting": 0, "working": 0})}
                                     for row in rows], "projects": tree.every()[1]}
        return await self._answer(work)

    async def one(self, request: web.Request) -> web.Response:
        def work() -> tuple[str, dict]:
            head, view = self.tree().space(request.match_info["space"])
            return head, {"space": view}
        return await self._answer(work, lambda data: self.sessions_in(data["space"]["path"]))

    async def project(self, request: web.Request) -> web.Response:
        """One project at any depth, `/api/projects` the root: as a Space is, and the sessions started in its
        folder."""
        def work() -> tuple[str, dict]:
            head, view = self.tree().project(request.match_info.get("path", ""))
            return head, {"project": view}
        return await self._answer(work, lambda data: self.sessions_in(data["project"]["path"]))

    async def record(self, request: web.Request) -> web.Response:
        def work() -> tuple[str, dict]:
            head, view = self.tree().record(request.match_info["space"], request.match_info["path"])
            return head, {"record": view}
        return await self._answer(work)

    async def _bytes(self, path: str) -> tuple[str, bytes] | web.Response:
        try:
            _, mime, data = await asyncio.to_thread(self.tree().file, path)
        except Refused as e:
            return web.json_response({"error": e.why}, status=e.status)
        except Unreadable as e:
            log.warning("spaces: the Base cannot be read: %s", e)
            return web.json_response({"error": NOT_READY}, status=503)
        return mime, data

    async def file(self, request: web.Request) -> web.StreamResponse:
        """A project's file as the commit has it, under the sandbox policy and never sniffed, as the type its
        extension is on the list with (`MEDIA`), else a download. A picture, a recording or a film inline — the app
        shows them in place —, in a range when one is asked for (a phone's player asks so); anything else as a
        download."""
        path = request.match_info["path"]
        found = await self._bytes(path)
        if isinstance(found, web.Response):
            return found
        mime, data = found
        shown = kind_of(path) in ("image", "audio", "video")
        return ranged(request, data, {"Content-Type": mime, "Content-Security-Policy": FILE_CSP,
                                      "Content-Disposition": "inline" if shown else _attachment(path)})

    async def view(self, request: web.Request) -> web.StreamResponse:
        """A project's PDF — only a file that starts as one — for the face's frame, in the browser's own viewer, which
        refuses a PDF under `sandbox`: its policy names only who may frame it, the face."""
        from .face import NO_FRAME   # the face imports this module
        path = request.match_info["path"]
        if kind_of(path) != "pdf":
            return web.json_response({"error": "No such PDF."}, status=404)
        found = await self._bytes(path)
        if isinstance(found, web.Response):
            return found
        mime, data = found
        if not data.startswith(b"%PDF-"):
            return web.json_response({"error": "The file is not a PDF."}, status=404)
        return web.Response(body=data, headers={
            "Content-Type": mime, "Content-Disposition": "inline",
            "Content-Security-Policy": f"frame-ancestors {self.platform.cfg.face_origin or NO_FRAME}"})


RANGE = re.compile(r"bytes=(\d{0,15})-(\d{0,15})")


def ranged(request: web.Request, data: bytes, headers: dict) -> web.Response:
    """The whole body, or the one range `Range: bytes=a-b` asks for (206; 416 past the end). Several ranges in one
    header are answered whole."""
    headers = {**headers, "Accept-Ranges": "bytes"}
    asked = RANGE.fullmatch(request.headers.get("Range", "").strip())
    if asked is None or asked[1] == asked[2] == "":
        return web.Response(body=data, headers=headers)
    size = len(data)
    if asked[1]:
        start, end = int(asked[1]), min(int(asked[2]), size - 1) if asked[2] else size - 1
    else:
        start, end = max(0, size - int(asked[2])), size - 1
    if start >= size or start > end:
        return web.Response(status=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    return web.Response(status=206, body=data[start:end + 1],
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})


def _attachment(path: str) -> str:
    """`Content-Disposition` for a download: an ASCII stand-in for old clients, the name itself as RFC 5987."""
    name = posixpath.basename(path)
    plain = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in name) or "file"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"
