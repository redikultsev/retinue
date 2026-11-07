#!/usr/bin/env python3
"""A project's passport: the part of its `AGENTS.md` above the Showcase marker, written for the agent that opens the
project — the file an agent CLI reads first in a folder (docs/base.md, «A project's passport»).

`SCHEMA` names every part a passport holds, in order, and its limits. `parse` reads one passport and says what is
wrong with it by structure only — headings, items, keys, source marks, sizes, whether a path exists —, never by what
a sentence says. `main` lists every project of a Base folder with `ok` or its problems.

Standard library only: the hub's pre-receive hook (kbcheck.py) imports it with the host's python3.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

SHOWCASE = "AGENTS.md"
MARKER = "<!-- Витрина:"                        # the Showcase's marker: the passport is what is above it
FOLDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # a project folder's name (spaces.SPACE_NAME)
DEPTH = 8                                         # folders of a project's path below the Base's root


@dataclass(frozen=True)
class Part:
    """One `##` section of a passport. `form`: `paragraph` (prose, one paragraph), `items` (a list, one thought per
    item) or `files` (``- `path` — what it holds, when to read it``). `keys`: an item starts with `Key:`, each key
    with its (least, most) count. `marked`: every item (a paragraph: the text) carries a source mark."""
    name: str
    required: bool
    form: str
    least: int = 1
    most: int = 1
    chars: int = 300                              # a paragraph's or an item's length, in characters
    keys: tuple[tuple[str, int, int], ...] = ()
    marked: bool = False


SCHEMA = {
    "bytes": 5 * 1024,          # the whole passport: a root-to-project chain of six stays under Codex's 32 KiB
    "title": 80,                # characters of the `# title`
    "head": ("id", "icon", "code"),                 # the head's keys (#21, #22)
    "imports": 3,               # `@path` lines under the title (Claude Code loads them)
    "parts": (
        Part("Why", True, "paragraph", chars=800, marked=True),
        Part("Scope", True, "items", least=1, most=8, keys=(("In", 0, 3), ("Out", 1, 3), ("Size", 0, 1),
                                                           ("Ceiling", 0, 1)), marked=True),
        Part("Files", True, "files", least=1, most=20, chars=240),
        Part("Rules", False, "items", least=1, most=12),
        Part("Checks", False, "items", least=1, most=6, chars=240),
    ),
}

# `(owner, 2026-11-01)` — the owner said it that day; `(assumption)` — an agent's guess he has not confirmed.
MARK = re.compile(r"\((owner), (\d{4}-\d{2}-\d{2})\)|\((assumption)\)")
END_MARK = re.compile(r"\s*(?:\(owner, \d{4}-\d{2}-\d{2}\)|\(assumption\))\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
ITEM = re.compile(r"^[-*+]\s+(.*)$")
NESTED = re.compile(r"^\s+(?:[-*+]|\d{1,3}[.)])\s+")
KEY = re.compile(r"^([A-Z][a-z]+):\s*(.*)$")
FILE_ITEM = re.compile(r"^`([^`\n]+)`\s+—\s+(\S.*)$")
PATH_IN = re.compile(r"`([^`\n]+)`")
IMPORT = re.compile(r"^@(\S+)$")
HEAD_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


@dataclass(frozen=True)
class Problem:
    """What is wrong, as data: `code` (missing, unknown, stray, nested, order, twice, count, long, mark, where, key,
    path, size, title, head), the `part` it is in, its `subject` (a key, a path, a heading) and a sentence for the
    session that fixes it."""
    code: str
    part: str
    subject: str = ""
    say: str = ""

    def key(self) -> tuple[str, str, str]:
        return self.code, self.part, self.subject

    def as_dict(self) -> dict:
        return {"code": self.code, "part": self.part, "subject": self.subject, "say": self.say}


@dataclass
class Passport:
    title: str = ""
    head: dict[str, str] = field(default_factory=dict)
    imports: list[str] = field(default_factory=list)
    why: str = ""
    scope: list[dict] = field(default_factory=list)    # {key, text, where, mark: {by, date?}}
    files: list[dict] = field(default_factory=list)    # {path, about}
    rules: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)     # lines in no part of the schema, in their order
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def above(text: str) -> str:
    """The passport's text: the Showcase up to its marker line (all of it when there is none)."""
    lines = text.splitlines()
    at = next((i for i, line in enumerate(lines) if line.lstrip().startswith(MARKER)), len(lines))
    return "\n".join(lines[:at])


def mark(text: str) -> dict | None:
    found = MARK.search(text)
    if not found:
        return None
    return {"by": "assumption"} if found.group(3) else {"by": "owner", "date": found.group(2)}


def inside(rel: str) -> bool:
    """A path a passport may point to: relative, below the project's folder, no dot segment."""
    parts = rel.rstrip("/").split("/")
    return bool(rel) and not rel.startswith("/") and "\\" not in rel and all(p and not p.startswith(".")
                                                                             for p in parts)


def _items(name: str, body: list[str], out: Passport) -> list[str]:
    """A list section's items, each with its wrapped lines joined; a nested list or text that is no item is a
    problem."""
    items: list[str] = []
    for line in body:
        if not line.strip():
            continue
        if NESTED.match(line):
            out.problems.append(Problem("nested", name, say=f"«{name}»: one level of items, no nested lists"))
        elif m := ITEM.match(line):
            items.append(m.group(1).strip())
        elif line.startswith((" ", "\t")) and items:
            items[-1] += " " + line.strip()
        else:
            if not any(p.code == "stray" and p.part == name for p in out.problems):
                out.problems.append(Problem("stray", name, line.strip()[:40], f"«{name}»: text that is no item — "
                                            "make it an item or move it to a file the passport points to"))
            out.other.append(line)
    return items


def _section(part: Part, body: list[str], out: Passport, exists) -> None:
    name = part.name
    if part.form == "paragraph":
        text = [line for line in body if line.strip()]
        paragraphs = sum(1 for i, line in enumerate(body) if line.strip() and (i == 0 or not body[i - 1].strip()))
        if any(ITEM.match(line) or NESTED.match(line) for line in text):
            out.problems.append(Problem("stray", name, say=f"«{name}» is prose; a list belongs in another part"))
        if paragraphs > 1:
            out.problems.append(Problem("count", name, say=f"«{name}» is one paragraph, not {paragraphs}"))
        out.why = " ".join(line.strip() for line in text)
        if not out.why:
            out.problems.append(Problem("missing", name, say=f"«{name}» is empty"))
        elif len(out.why) > part.chars:
            out.problems.append(Problem("long", name, say=f"«{name}»: {len(out.why)} characters, {part.chars} at most"))
        if out.why and part.marked and not MARK.search(out.why):
            out.problems.append(Problem("mark", name, say=f"«{name}» says whose it is: (owner, YYYY-MM-DD) or "
                                                          "(assumption)"))
        return
    items = _items(name, body, out)
    if not part.least <= len(items) <= part.most:
        out.problems.append(Problem("count", name, say=f"«{name}»: {len(items)} items, {part.least} to {part.most}"))
    for item in items:
        if len(item) > part.chars:
            out.problems.append(Problem("long", name, item[:40], f"«{name}»: an item of {len(item)} characters, "
                                                                 f"{part.chars} at most — the rest goes to a file"))
    if part.form == "files":
        for item in items:
            m = FILE_ITEM.match(item)
            if not m:
                out.problems.append(Problem("stray", name, item[:40], f"«{name}»: an item is ``- `path` — what it "
                                                                      "holds, when to read it``"))
                continue
            path, about = m.group(1), m.group(2)
            out.files.append({"path": path, "about": about})
            if not inside(path) or (exists is not None and not exists(path)):
                out.problems.append(Problem("path", name, path, f"«{name}»: `{path}` is not in the project"))
        return
    if not part.keys:
        (out.rules if name == "Rules" else out.checks).extend(items)
        return
    counted = {key: 0 for key, _, _ in part.keys}
    for item in items:
        m = KEY.match(item)
        if not m or m.group(1) not in counted:
            out.problems.append(Problem("key", name, item[:40], f"«{name}»: an item starts with one of "
                                                                f"{', '.join(k + ':' for k in counted)}"))
            continue
        key, rest = m.group(1), m.group(2)
        counted[key] += 1
        found = mark(rest)
        if part.marked and not END_MARK.search(rest):
            out.problems.append(Problem("mark", name, key, f"«{name}»: a «{key}» line ends with (owner, YYYY-MM-DD) "
                                                           "or (assumption)"))
        text = END_MARK.sub("", rest).strip()
        where = ""
        if key == "Out":
            if "→" not in text:
                out.problems.append(Problem("where", name, key, f"«{name}»: an «Out» line says where it goes: "
                                                                "… → where"))
            else:
                text, where = (s.strip() for s in text.split("→", 1))
        if key == "Ceiling":
            path = PATH_IN.search(text)
            if not path or not inside(path.group(1)) or (exists is not None and not exists(path.group(1))):
                out.problems.append(Problem("path", name, path.group(1) if path else key,
                                            f"«{name}»: «Ceiling» points to a page of the project in backticks"))
        out.scope.append({"key": key.lower(), "text": text, "where": where, "mark": found})
    for key, least, most in part.keys:
        if not least <= counted[key] <= most:
            out.problems.append(Problem("count", name, key, f"«{name}»: {counted[key]} «{key}» lines, "
                                                            f"{least} to {most}"))


def parse(text: str, exists=None) -> Passport:
    """A passport (the text above the marker, `above`) as data, with every way it departs from `SCHEMA`. `exists`:
    whether a path relative to the project's folder is a file or a folder of it; None: paths are not looked up."""
    out = Passport()
    if len(text.encode("utf-8")) > SCHEMA["bytes"]:
        out.problems.append(Problem("size", "passport", say=f"the passport is {len(text.encode('utf-8'))} bytes, "
                                                            f"{SCHEMA['bytes']} at most — move detail to its files"))
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":                       # the head (#21)
        end = next((j for j in range(1, len(lines)) if lines[j].strip() == "---"), None)
        if end is None:
            out.problems.append(Problem("head", "head", say="the head opened with --- is never closed"))
        else:
            for line in lines[1:end]:
                if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
                    continue
                m = HEAD_KEY.match(line)
                if not m or m.group(1) not in SCHEMA["head"]:
                    out.problems.append(Problem("head", "head", m.group(1) if m else line[:40],
                                                f"the head holds only {', '.join(SCHEMA['head'])}"))
                else:
                    out.head[m.group(1)] = line.split(":", 1)[1].strip()
            i = end + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    m = HEADING.match(lines[i]) if i < len(lines) else None
    if m and len(m.group(1)) == 1:
        out.title = m.group(2)
        if len(out.title) > SCHEMA["title"]:
            out.problems.append(Problem("long", "title", say=f"the title is {len(out.title)} characters, "
                                                             f"{SCHEMA['title']} at most"))
        i += 1
    else:
        out.problems.append(Problem("title", "title", say="the passport starts with `# <title>`"))
    sections: list[tuple[str, list[str]]] = []
    for line in lines[i:]:
        m = HEADING.match(line)
        if m and len(m.group(1)) == 2:
            sections.append((m.group(2), []))
        elif m:
            out.problems.append(Problem("nested", sections[-1][0] if sections else "title", m.group(2)[:40],
                                        "only `##` headings, the schema's"))
            out.other.append(line)
        elif sections:
            sections[-1][1].append(line)
        elif IMPORT.match(line.strip()):
            path = IMPORT.match(line.strip()).group(1)
            out.imports.append(path)
            if not inside(path) or (exists is not None and not exists(path)):
                out.problems.append(Problem("path", "imports", path, f"`@{path}` is not in the project"))
        elif line.strip():
            out.problems.append(Problem("stray", "title", line.strip()[:40], "text under the title belongs in "
                                                                             "«Why» or a file"))
            out.other.append(line)
    if len(out.imports) > SCHEMA["imports"]:
        out.problems.append(Problem("count", "imports", say=f"{len(out.imports)} imports, {SCHEMA['imports']} at most"))
    parts = {part.name: part for part in SCHEMA["parts"]}
    order = [part.name for part in SCHEMA["parts"]]
    seen: list[str] = []
    for heading, body in sections:
        if heading not in parts:
            out.problems.append(Problem("unknown", heading, heading, f"«{heading}» is no part of a passport: "
                                                                     f"{', '.join(order)}"))
            out.other += [f"## {heading}", *body]
            continue
        if heading in seen:
            out.problems.append(Problem("twice", heading, heading, f"«{heading}» comes twice"))
            continue
        if seen and order.index(heading) < order.index(seen[-1]):
            out.problems.append(Problem("order", heading, heading, f"the parts go in this order: {', '.join(order)}"))
        seen.append(heading)
        _section(parts[heading], body, out, exists)
    for part in SCHEMA["parts"]:
        if part.required and part.name not in seen:
            out.problems.append(Problem("missing", part.name, part.name, f"«{part.name}» is missing"))
    return out


def projects(root: str) -> list[str]:
    """Every project of a Base folder on disk, as `docs/base.md` defines one: a folder holding an `AGENTS.md`, each
    folder name a `FOLDER`, at most `DEPTH` deep; '' is the root."""
    found = []
    for here, dirs, files in os.walk(root):
        rel = os.path.relpath(here, root).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        depth = len(rel.split("/")) if rel else 0
        dirs[:] = sorted(d for d in dirs if FOLDER.fullmatch(d) and depth < DEPTH)
        if SHOWCASE in files and os.path.isfile(os.path.join(here, SHOWCASE)):
            found.append(rel)
    return found


def check_folder(root: str, project: str) -> Passport:
    folder = os.path.join(root, *project.split("/")) if project else root
    with open(os.path.join(folder, SHOWCASE), encoding="utf-8", errors="replace") as f:
        text = f.read()

    def exists(rel: str) -> bool:
        path = os.path.join(folder, *rel.rstrip("/").split("/"))
        return os.path.isfile(path) or os.path.isdir(path)

    return parse(above(text), exists)


def main(argv: list[str] | None = None) -> int:
    """`python3 -m retinue.passport <Base folder>`: one line per project, `ok` or `FAIL` with its problems; exits 1
    when one fails."""
    args = sys.argv[1:] if argv is None else argv
    root = args[0] if args else "."
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed = 0
    found = projects(root)
    for project in found:
        result = check_folder(root, project)
        print(f"{'ok  ' if result.ok else 'FAIL'} {project or '.'}")
        for problem in result.problems:
            print(f"       {problem.code} · {problem.part}: {problem.say}")
        failed += not result.ok
    print(f"{len(found)} projects, {len(found) - failed} pass, {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
