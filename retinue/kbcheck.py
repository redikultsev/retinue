#!/usr/bin/env python3
"""Checks one change to a knowledge base kept in git: what the agent may touch, what never enters the base, a record's
name that never changes, and the base's own lint. The same file serves twice:

- the router imports it and checks the assistant's turn before it pushes (`check`), so that she hears why at once;
- the hub runs it as its `pre-receive` hook (`main`), for every push — the router's and the owner's laptop's alike,
  with the policy file next to it (`kb-policy.json`). A push by `writer_uid` (the containers' user) is the
  assistant's; any other is the owner's.

Standard library only: on the hub it runs with the host's python3, outside any container. Nothing here executes
what is in the change except the base's lint, and that only from a copy of the new tree outside the working tree,
after every path in the change has passed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field

ASSISTANT, OWNER = "assistant", "owner"
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: the parent of a first commit
POLICY_FILE = "kb-policy.json"
ERROR_WORDS = ("ОШИБКА", "ERROR", "Error")  # a lint line that names an error; the rest of its output is noise
MAX_REASONS = 10


@dataclass
class Policy:
    branch: str = "main"
    writer_uid: int = -1               # a push by this user is the assistant's: the containers' uid
    writable: list[str] = field(default_factory=list)     # what the assistant may create, change, delete
    lines_below: list[str] = field(default_factory=list)  # showcases: she may change them only below `marker`
    marker: str = ""
    never: list[str] = field(default_factory=list)        # never hers, whatever `writable` says
    excluded: list[str] = field(default_factory=list)     # never in the base, whoever pushes (e.g. the laptop's scratch)
    keep: str = "id"                   # a record's name: links depend on it, so it never changes while the record lives
    owner_mark: str = "Владелец"       # `written_by` of a record the owner wrote himself
    check: list[str] = field(default_factory=list)        # the base's lint, run in a copy of the new tree
    check_timeout: int = 120

    @classmethod
    def of(cls, raw: dict) -> Policy:
        known = {name: raw[name] for name in cls.__dataclass_fields__ if name in raw}
        return cls(**known)

    @classmethod
    def load(cls, path: str) -> Policy:
        import json
        with open(path, encoding="utf-8") as f:
            return cls.of(json.load(f))


@dataclass
class Verdict:
    refusals: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (A | M | D | R, path) as the change has them
    owner_records: list[str] = field(default_factory=list)      # the owner's own records it changed or deleted
    deleted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals


def norm(path: str) -> str:
    """A name as the owner's Mac sees it: its file system ignores case and Unicode normalisation."""
    return unicodedata.normalize("NFC", path).casefold()


def hidden(path: str) -> bool:
    """A segment that starts with a dot anywhere: `.git`, `.claude`, `.hidden.md`."""
    return any(segment.startswith(".") for segment in path.split("/") if segment not in ("", "."))


def match(path: str, pattern: str) -> bool:
    """A glob over `/`-separated paths: `*` and `?` stay inside one folder, `**` is any number of folders. Case and
    Unicode normalisation do not count, as on the owner's Mac."""
    path, pattern = norm(path), norm(pattern)
    segments, regex = pattern.split("/"), ""
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == "**":
            regex += ".*" if last else "(?:[^/]+/)*"
            continue
        regex += "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in segment)
        regex += "" if last else "/"
    return re.fullmatch(regex, path) is not None


def any_match(path: str, patterns: list[str]) -> bool:
    return any(match(path, pattern) for pattern in patterns)


def header(text: str) -> tuple[dict, str]:
    """(fields, body): the record's header the way the base's lint reads it, and what follows it."""
    found = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not found:
        return {}, text
    fields = {}
    for line in found.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.split("#")[0].strip()
    return fields, text[found.end():]


def _git(git: list[str], *args: str, text: bool = True) -> str | bytes:
    return subprocess.run([*git, *args], check=True, capture_output=True, text=text).stdout


def _show(git: list[str], rev: str, path: str) -> str:
    return _git(git, "show", f"{rev}:{path}", text=False).decode("utf-8", "replace")


def changes(git: list[str], old: str, new: str) -> list[tuple[str, str, str, str]]:
    """(status, old path, new path, new mode) for every file the change touches, renames found."""
    base = EMPTY_TREE if old == ZERO else old
    raw = _git(git, "diff", "--raw", "-z", "-M", "--no-abbrev", base, new, text=False).decode("utf-8", "replace")
    fields, out, i = raw.split("\0"), [], 0
    while i < len(fields) - 1:
        meta = fields[i].lstrip(":").split()
        status = meta[4][0]
        if status in "RC":
            out.append((status, fields[i + 1], fields[i + 2], meta[1]))
            i += 3
        else:
            out.append((status, fields[i + 1], fields[i + 1], meta[1]))
            i += 2
    return out


def _paths(policy: Policy, status: str, old_path: str, new_path: str, mode: str) -> list[str]:
    """What the assistant may not do with this file."""
    refusals = []
    for path in dict.fromkeys(p for p in (old_path, new_path) if p):
        if hidden(path):
            refusals.append(f"{path}: имя с точки — скрытое, ассистентке сюда нельзя")
        elif any_match(path, policy.lines_below):
            if status != "M":
                refusals.append(f"{path}: Витрину нельзя удалить или создать, только дописать строки ниже маркера")
        elif any_match(path, policy.never) or not any_match(path, policy.writable):
            refusals.append(f"{path}: сюда ассистентке писать нельзя")
    if not refusals and mode == "100755":
        refusals.append(f"{new_path}: исполняемый файл — в базе только обычные файлы")
    return refusals


def _showcase(policy: Policy, path: str, before: str, after: str) -> list[str]:
    marker = policy.marker
    if not marker or before.count(marker) != 1 or after.count(marker) != 1:
        return [f"{path}: маркер Витрины должен стоять ровно один раз — без него правка не принимается"]
    if before[:before.index(marker)] != after[:after.index(marker)]:
        return [f"{path}: выше маркера Витрины — правила; ниже можно только строки"]
    return []


def materialize(git: list[str], rev: str, dest: str) -> None:
    """The tree of `rev` in `dest`, an empty folder outside the working tree, with an index of its own next to it:
    the working tree's index is never touched."""
    os.makedirs(dest)
    env = {**os.environ, "GIT_INDEX_FILE": os.path.abspath(dest.rstrip("/") + ".index")}
    subprocess.run([*git, f"--work-tree={dest}", "checkout", "-f", rev, "--", ":/"], check=True,
                   capture_output=True, env=env)


def lint(git: list[str], rev: str, policy: Policy) -> list[str]:
    """The base's own lint on a copy of `rev`, in a fresh folder only this user can enter (`mkdtemp`, 0700): no copy
    is reused, so nobody can leave a file in it for the next check. Python is told not to put the script's folder
    first on its path, so a `scripts/re.py` in the tree is never what the lint's `import re` loads."""
    if not policy.check:
        return []
    temporary = tempfile.mkdtemp(prefix="kbcheck-")
    dest = os.path.join(temporary, "tree")
    try:
        materialize(git, rev, dest)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": temporary,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1"}
        try:
            run = subprocess.run(policy.check, cwd=dest, env=env, capture_output=True, text=True,
                                 timeout=policy.check_timeout)
        except subprocess.TimeoutExpired:
            return [f"just check: не уложился в {policy.check_timeout} с"]
        if run.returncode == 0:
            return []
        lines = [line.strip() for line in (run.stdout + "\n" + run.stderr).splitlines() if line.strip()]
        errors = [line for line in lines if any(word in line for word in ERROR_WORDS)] or lines[-1:] or ["упал"]
        return [f"just check: {line}" for line in errors[:MAX_REASONS]]
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def check(git: list[str], old: str, new: str, policy: Policy, role: str) -> Verdict:
    """Everything about the change `old` → `new` that is not allowed. `git` is the command prefix that reaches the
    repository (`["git", "--git-dir", …]`).
    Records may be changed and deleted, the journal's included; what changed is named for the evening list."""
    verdict = Verdict()
    for status, old_path, new_path, mode in changes(git, old, new):
        verdict.files.append((status, new_path))
        if any_match(new_path, policy.excluded) and status != "D":
            verdict.refusals.append(f"{new_path}: этого в базе не бывает — путь исключён политикой")
            continue
        if mode == "120000" or mode == "160000":
            verdict.refusals.append(f"{new_path}: {'симлинк' if mode == '120000' else 'подмодуль'} — в базе "
                                    "только обычные файлы")
            continue
        if role == ASSISTANT:
            refused = _paths(policy, status, old_path, new_path, mode)
            verdict.refusals += refused
            if refused:
                continue
        before = _show(git, old, old_path) if status != "A" else ""
        after = _show(git, new, new_path) if status != "D" else ""
        if status != "A" and policy.owner_mark and policy.owner_mark in header(before)[0].get("written_by", ""):
            verdict.owner_records.append(old_path)
        if status == "D":
            verdict.deleted.append(old_path)
        if role == ASSISTANT and status == "M" and any_match(new_path, policy.lines_below):
            verdict.refusals += _showcase(policy, new_path, before, after)
        # The same file under another id is a renamed record; a file gone, or moved under a new name, is a record
        # deleted and another made — allowed.
        kept = header(before)[0].get(policy.keep) if policy.keep and status == "M" else None
        if kept and header(after)[0].get(policy.keep) != kept:
            verdict.refusals.append(f"{new_path}: {policy.keep} изменён: {kept} → "
                                    f"{header(after)[0].get(policy.keep) or 'нет'}; {policy.keep} — имя Записи, на "
                                    "него ссылаются: он не меняется, пока Запись есть")
    verdict.refusals += collisions(git, new, {path for status, path in verdict.files if status != "D"})
    if not verdict.refusals:
        verdict.refusals += lint(git, new, policy)
    return verdict


def collisions(git: list[str], rev: str, changed: set[str]) -> list[str]:
    """Two names in the tree that are one file or folder on the owner's Mac (`A.md` and `a.md`, `é` composed and
    decomposed): one of them would silently take the other's place there. Only those the change brings in."""
    names = [n for n in _git(git, "ls-tree", "-r", "-z", "--name-only", rev, text=False).decode("utf-8", "replace")
             .split("\0") if n]
    seen: dict[str, set[str]] = {}
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            seen.setdefault(norm(prefix), set()).add(prefix)
    out = []
    for group in seen.values():
        if len(group) < 2:
            continue
        for name in sorted(group):
            if any(path == name or path.startswith(name + "/") for path in changed):
                other = sorted(group - {name})[0]
                out.append(f"{name}: на Mac совпадёт с {other} — имена отличаются только регистром или нормализацией "
                           "Unicode")
    return out


def main() -> int:
    """The hub's pre-receive: one line `old new ref` per pushed ref on standard input; a refusal is printed and the
    whole push is refused."""
    here = os.path.dirname(os.path.abspath(__file__))
    policy = Policy.load(os.path.join(here, POLICY_FILE))
    role = ASSISTANT if os.getuid() == policy.writer_uid else OWNER
    refusals = []
    for line in sys.stdin.read().splitlines():
        old, new, ref = line.split()
        if ref != f"refs/heads/{policy.branch}":
            refusals.append(f"отказ: только ветка {policy.branch}, а не {ref}")
            continue
        if new == ZERO:
            refusals.append(f"отказ: {policy.branch} не удаляется")
            continue
        if old != ZERO and subprocess.run(["git", "merge-base", "--is-ancestor", old, new]).returncode != 0:
            refusals.append(f"отказ: история {policy.branch} не переписывается — сначала pull")
            continue
        refusals += check(["git"], old, new, policy, role).refusals
    for refusal in refusals:
        print(refusal, file=sys.stderr)
    if refusals:
        print(f"push не принят ({'ассистентка' if role == ASSISTANT else 'Владелец'}); правила — kb-policy.json "
              "в хабе", file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
