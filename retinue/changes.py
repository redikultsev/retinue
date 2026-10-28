"""A session's changes as a unified diff, built from its own transcript: the Edit, Write, MultiEdit and NotebookEdit
calls that ran and did not fail, in the order their results came. Pure — the same lines give the same diff.

Nothing runs in the lane for it, git least of all: `git diff` in a work tree the session controls would run that
repository's own configuration (`core.fsmonitor`, a filter's clean driver, a textconv) outside the sandbox, with the
lane host's rights. The transcript already says what each call did.

Line numbers come from the CLI itself: the user line that carries a call's result carries `tool_use_result`, and for
an edit its `structuredPatch` — hunks with the file's real `oldStart/oldLines/newStart/newLines` and their lines. A
Write of a new file (`type: create`) is its whole content added. Where the patch is missing — a CLI that gives none,
a Write over a file without one, a turn whose raw lines were pruned and only the projection is left — the hunk is
rebuilt with difflib from what the call itself says (`old_string`/`new_string`, the content written): its numbers are
then relative to the snippet, not the file, and the file is named in `approximate_files`.

Several changes of one file make one file section, their hunks in the order they ran — each hunk's numbers are the
file's as it was at that call. A path under the session's directory is shown relative to it; any other, absolute.
"""

from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass, field

CAP = 1024 * 1024        # bytes of diff text; past it whole hunks are left out and `truncated` is set
CONTEXT = 3
STAT_BAR = 40
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
NO_NEWLINE = "\\ No newline at end of file"
_LINES = re.compile(r"[^\n]*\n|[^\n]+$")


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[str]     # each ' ', '-', '+' or '\\' and the line, no newline

    def counts(self) -> tuple[int, int]:
        old = sum(1 for line in self.lines if line[:1] in (" ", "-"))
        new = sum(1 for line in self.lines if line[:1] in (" ", "+"))
        return old, new

    def text(self) -> str:
        old, new = self.counts()
        return f"@@ -{_range(self.old_start, old)} +{_range(self.new_start, new)} @@\n" + \
            "".join(line + "\n" for line in self.lines)


@dataclass
class Change:
    """What one call did to one file."""
    path: str
    hunks: list[Hunk]
    created: bool = False      # a new file: its section says so
    approximate: bool = False  # hunks rebuilt from the call, not the CLI's patch


@dataclass
class _File:
    path: str
    created: bool
    approximate: bool = False
    hunks: list[Hunk] = field(default_factory=list)


def _range(start: int, count: int) -> str:
    return str(start) if count == 1 else f"{start},{count}"


def _split(text: str) -> list[str]:
    """Lines with their own newline kept, split on '\\n' only — so 'x' and 'x\\n' differ."""
    return _LINES.findall(text) if isinstance(text, str) else []


def _diff_lines(a: list[str], b: list[str], whole_file: bool) -> list[Hunk]:
    """Hunks between two lists of lines (each with its newline, if it has one). For a whole file, a last line
    without its newline gets git's marker after it."""
    if not whole_file:  # a snippet's last line is not the file's: whether it ends in a newline says nothing
        a, b = [line.removesuffix("\n") + "\n" for line in a], [line.removesuffix("\n") + "\n" for line in b]
    hunks = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(CONTEXT):
        signed = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                signed += [(" ", line) for line in a[i1:i2]]
            else:
                signed += [("-", line) for line in a[i1:i2]] + [("+", line) for line in b[j1:j2]]
        lines: list[str] = []
        for sign, line in signed:
            lines.append(sign + line.removesuffix("\n"))
            if whole_file and not line.endswith("\n"):
                lines.append(NO_NEWLINE)
        first = group[0]
        old_start = first[1] + 1 if any(op[2] > op[1] for op in group) else first[1]
        new_start = first[3] + 1 if any(op[4] > op[3] for op in group) else first[3]
        hunks.append(Hunk(old_start, new_start, lines))
    return hunks


def _patch(patch: object) -> list[Hunk] | None:
    """The CLI's `structuredPatch` as hunks, or None for one that is not exactly its shape — a line that would break
    the diff's own structure (a newline inside, no sign) is never passed on."""
    if not isinstance(patch, list):
        return None
    hunks = []
    for hunk in patch:
        if not isinstance(hunk, dict):
            return None
        old_start, new_start, lines = hunk.get("oldStart"), hunk.get("newStart"), hunk.get("lines")
        if type(old_start) is not int or type(new_start) is not int or old_start < 0 or new_start < 0 or \
                not isinstance(lines, list) or not lines:
            return None
        for line in lines:
            if not isinstance(line, str) or line[:1] not in (" ", "-", "+", "\\") or "\n" in line:
                return None
        hunks.append(Hunk(old_start, new_start, list(lines)))
    return hunks


def _added(content: object, whole_file: bool) -> list[Hunk]:
    lines = _split(content if isinstance(content, str) else "")
    return _diff_lines([], lines, whole_file) if lines else []


def change_of(tool: str, args: dict, result: dict | None) -> list[Change]:
    """What one call that ran without an error did. `result`: the CLI's `tool_use_result` for it, when there is one."""
    result = result if isinstance(result, dict) else None
    path = (result or {}).get("filePath") or args.get(EDIT_TOOLS.get(tool, "file_path")) or ""
    if not isinstance(path, str) or not path:
        return []
    if result is not None and result.get("type") == "create" and tool in ("Write", ""):
        return [Change(path, _added(result.get("content", args.get("content")), True), created=True)]
    patch = _patch(result.get("structuredPatch")) if result is not None else None
    if patch:
        return [Change(path, patch)]
    if tool == "Write" and result is not None and isinstance(result.get("originalFile"), str) and \
            isinstance(args.get("content"), str):
        return [Change(path, _diff_lines(_split(result["originalFile"]), _split(args["content"]), True))]
    if patch == [] and not _changes_text(tool, args):
        return []  # the CLI says nothing changed, and the call agrees
    return [Change(path, hunks, approximate=True) for hunks in [_rebuilt(tool, args)] if hunks]


def _changes_text(tool: str, args: dict) -> bool:
    if tool == "Edit":
        return args.get("old_string") != args.get("new_string")
    if tool == "MultiEdit":
        return any(isinstance(e, dict) and e.get("old_string") != e.get("new_string") for e in args.get("edits") or [])
    return True


def _rebuilt(tool: str, args: dict) -> list[Hunk]:
    """Hunks from the call alone; their numbers count from the snippet, not the file."""
    if tool == "Edit":
        return _diff_lines(_split(args.get("old_string", "")), _split(args.get("new_string", "")), False)
    if tool == "MultiEdit":
        return [hunk for e in args.get("edits") or [] if isinstance(e, dict)
                for hunk in _diff_lines(_split(e.get("old_string", "")), _split(e.get("new_string", "")), False)]
    if tool == "Write":
        return _added(args.get("content"), True)
    if tool == "NotebookEdit":
        return _added(args.get("new_source"), False)
    return []


class Scan:
    """The changes in raw CLI lines, fed turn by turn: a call and its result may lie in different turns (a deferred
    call is finished by the resume), so what was called is kept across them."""

    def __init__(self) -> None:
        self.called: dict[str, tuple[str, dict]] = {}
        self.changes: list[Change] = []

    def line(self, line: dict) -> None:
        content = (line.get("message") or {}).get("content") if isinstance(line.get("message"), dict) else None
        if not isinstance(content, list):
            return
        if line.get("type") == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                    args = block.get("input")
                    self.called[str(block.get("id") or "")] = (block["name"], args if isinstance(args, dict) else {})
            return
        if line.get("type") != "user":
            return
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        meta = line.get("tool_use_result") if len(results) == 1 else None  # the line's result is that one block's
        for block in results:
            if block.get("is_error"):
                continue
            call_id = str(block.get("tool_use_id") or "")
            if call_id in self.called:
                tool, args = self.called.pop(call_id)
            elif _is_edit_result(meta):
                tool, args = "", {}  # called in a turn whose lines were pruned: the CLI's result alone says enough
            else:
                continue
            given = args.get(EDIT_TOOLS.get(tool, "")) if args else None
            if isinstance(meta, dict) and isinstance(meta.get("filePath"), str) and isinstance(given, str) and \
                    posixpath.normpath(meta["filePath"]) != posixpath.normpath(given):
                meta = None  # not this call's
            self.changes += change_of(tool, args, meta)


def _is_edit_result(meta: object) -> bool:
    return isinstance(meta, dict) and isinstance(meta.get("filePath"), str) and \
        (meta.get("type") == "create" or isinstance(meta.get("structuredPatch"), list))


def from_items(items: list[dict]) -> list[Change]:
    """The changes in a turn's projection, for a turn whose raw lines were pruned: every edit that ran, rebuilt from
    the call (the projection keeps no patch), a subagent's own edits with them."""
    out: list[Change] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("kind") == "subagent":
            out += from_items(data.get("items") or [])
        if item.get("kind") != "edit" or data.get("ok") is not True:
            continue
        tool, path = data.get("tool", ""), data.get("path", "")
        args = {EDIT_TOOLS.get(tool, "file_path"): path, "old_string": data.get("old", ""),
                "new_string": data.get("new", ""), "content": data.get("content", ""),
                "new_source": data.get("content", ""),
                "edits": [{"old_string": e.get("old", ""), "new_string": e.get("new", "")}
                          for e in data.get("edits") or [] if isinstance(e, dict)]}
        out += [Change(path, hunks, approximate=True) for hunks in [_rebuilt(tool, args)] if hunks and path]
    return out


# --- the answer ---------------------------------------------------------------------------------------------------

def shown(path: str, cwd: str) -> str:
    """A path under `cwd` relative to it; any other as it is."""
    if not posixpath.isabs(path):
        return posixpath.normpath(path)
    path = posixpath.normpath(path)
    base = posixpath.normpath(cwd) if cwd and posixpath.isabs(cwd) else ""
    if base and (path.startswith(base.rstrip("/") + "/")):
        return posixpath.relpath(path, base)
    return path


def _quoted(name: str) -> str:
    """A name as git writes it in a header: C-quoted when it holds a control character, a quote or a backslash — a
    file name never becomes a line of the diff."""
    if not re.search(r'[\x00-\x1f\x7f"\\]', name):
        return name
    escaped = "".join({"\n": "\\n", "\t": "\\t", "\r": "\\r", '"': '\\"', "\\": "\\\\"}.get(c) or
                      (f"\\x{ord(c):02x}" if ord(c) < 0x20 or ord(c) == 0x7f else c) for c in name)
    return f'"{escaped}"'


def render(changes: list[Change], cwd: str, cap: int = CAP) -> dict:
    """`{cwd, stat, diff, truncated, approximate_files}`: one section per file, in the order each was first changed."""
    files: dict[str, _File] = {}
    for change in changes:
        if not change.hunks:
            continue
        name = shown(change.path, cwd)
        entry = files.setdefault(name, _File(name, change.created))
        entry.hunks += change.hunks
        entry.approximate |= change.approximate
    out, size, truncated = [], 0, False
    for entry in files.values():
        a, b = _quoted("a/" + entry.path), _quoted("b/" + entry.path)
        head = f"diff --git {a} {b}\n" + ("new file mode 100644\n--- /dev/null\n" if entry.created else f"--- {a}\n") \
            + f"+++ {b}\n"
        for n, hunk in enumerate(entry.hunks):
            text = (head if n == 0 else "") + hunk.text()
            if size + len(text.encode()) > cap:
                truncated = True
                break
            out.append(text)
            size += len(text.encode())
        if truncated:
            break
    return {"cwd": cwd, "stat": _stat(list(files.values())), "diff": "".join(out), "truncated": truncated,
            "approximate_files": [entry.path for entry in files.values() if entry.approximate]}


def _stat(files: list[_File]) -> str:
    """As `git diff --stat` writes it: a line per file, then the totals — of every change, shown or cut."""
    if not files:
        return ""
    rows = []
    for entry in files:
        added = sum(1 for h in entry.hunks for line in h.lines if line[:1] == "+")
        removed = sum(1 for h in entry.hunks for line in h.lines if line[:1] == "-")
        rows.append((_quoted(entry.path), added, removed))
    width, most = max(len(r[0]) for r in rows), max(r[1] + r[2] for r in rows)
    digits = len(str(most))
    lines = []
    for name, added, removed in rows:
        plus, minus = added, removed
        if most > STAT_BAR:
            plus, minus = round(added * STAT_BAR / most), round(removed * STAT_BAR / most)
            plus, minus = max(plus, 1 if added else 0), max(minus, 1 if removed else 0)
        lines.append(f" {name.ljust(width)} | {str(added + removed).rjust(digits)} {'+' * plus}{'-' * minus}")
    added, removed = sum(r[1] for r in rows), sum(r[2] for r in rows)
    total = f" {len(rows)} file{'s' if len(rows) != 1 else ''} changed"
    if added:
        total += f", {added} insertion{'s' if added != 1 else ''}(+)"
    if removed:
        total += f", {removed} deletion{'s' if removed != 1 else ''}(-)"
    return "\n".join(lines + [total]) + "\n"
