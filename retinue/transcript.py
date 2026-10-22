"""A session's transcript as the owner reads it: the CLI's stream-json lines of one turn folded into items. Pure —
the same lines give the same items, so the projection is rebuilt from the raw lines whenever it is needed.

A turn ends at a `result` line. It does not begin at `system/init`: on a bare `--resume` the deferred call runs
first, and its hook events and tool result come before the init line — so the call they finish is passed in from the
previous turn (`resumed`). Hook lifecycle, task bookkeeping and lane events are not the transcript and are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

READS = ("Read", "Grep", "Glob")
EDITS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
PLANS = ("ExitPlanMode", "TodoWrite")
SUBAGENTS = ("Task", "Agent")
QUESTION = "AskUserQuestion"
RESULT_HEAD = 2000  # what of a tool's output is kept in the projection; the raw line has all of it


@dataclass(frozen=True)
class Item:
    kind: str    # message | reads | edit | plan | subagent | question | tool | result
    data: dict
    first: int   # the turn's lines it was folded from: the first and the last index
    last: int


def ends_turn(line: dict) -> bool:
    return line.get("type") == "result"


def turns(lines: list[dict]) -> list[list[dict]]:
    """A session's lines cut after each `result`; the last turn may still be open."""
    out: list[list[dict]] = [[]]
    for line in lines:
        out[-1].append(line)
        if ends_turn(line):
            out.append([])
    return out if out[-1] else out[:-1]


def deferred_of(result: dict) -> dict | None:
    """The call a turn's `result` line ended deferred on: what the next turn, its resume, finishes first."""
    return result.get("deferred_tool_use") if result.get("stop_reason") == "tool_deferred" else None


def _head(content) -> str:
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return text if len(text) <= RESULT_HEAD else text[:RESULT_HEAD - 1] + "…"


def _call(name: str, call_id: str, args: dict) -> tuple[str, dict]:
    if name in READS:
        return "reads", {"calls": [{"id": call_id, "tool": name, "input": args, "result": None, "error": False}]}
    if name in EDITS:
        data = {"id": call_id, "tool": name, "path": args.get(EDITS[name]) or "",
                "ok": None, "error": ""}
        if name == "Edit":
            data |= {"old": args.get("old_string", ""), "new": args.get("new_string", "")}
        elif name == "MultiEdit":
            data["edits"] = [{"old": e.get("old_string", ""), "new": e.get("new_string", "")}
                             for e in args.get("edits") or [] if isinstance(e, dict)]
        else:
            data["content"] = args.get("content", args.get("new_source", ""))
        return "edit", data
    if name == "ExitPlanMode":
        return "plan", {"id": call_id, "tool": name, "plan": args.get("plan", "")}
    if name == "TodoWrite":
        return "plan", {"id": call_id, "tool": name, "todos": args.get("todos") or []}
    if name in SUBAGENTS:
        return "subagent", {"id": call_id, "tool": name, "description": args.get("description", ""),
                            "agent": args.get("subagent_type", ""), "prompt": args.get("prompt", ""),
                            "items": [], "result": None}
    if name == QUESTION:
        return "question", {"id": call_id, "questions": args.get("questions") or [], "answer": None}
    return "tool", {"id": call_id, "tool": name, "input": args, "result": None, "error": False}


class _Fold:
    def __init__(self) -> None:
        self.items: list[dict] = []        # {"kind", "data", "first", "last"}, mutable while the turn is folded
        self.calls: dict[str, dict] = {}   # tool_use id -> the item it is in
        self.owner: dict[str, dict] = {}   # tool_use id inside a subagent -> that subagent's item
        self.children: dict[int, list[dict]] = {}  # id(subagent item) -> its own lines

    def add(self, kind: str, data: dict, at: int) -> dict:
        item = {"kind": kind, "data": data, "first": at, "last": at}
        self.items.append(item)
        return item

    def call(self, name: str, call_id: str, args: dict, at: int) -> None:
        kind, data = _call(name, call_id, args if isinstance(args, dict) else {})
        last = self.items[-1] if self.items else None
        if kind == "reads" and last and last["kind"] == "reads":
            last["data"]["calls"] += data["calls"]
            last["last"] = at
            item = last
        else:
            item = self.add(kind, data, at)
            if kind == "subagent":
                self.children[id(item)] = []
        self.calls[call_id] = item

    def result(self, block: dict, at: int, resumed: dict | None) -> None:
        call_id = block.get("tool_use_id") or ""
        if call_id not in self.calls and resumed and resumed.get("id") == call_id:
            self.call(resumed.get("name") or "", call_id, resumed.get("input") or {}, at)
        item = self.calls.get(call_id) or self.add("tool", {"id": call_id, "tool": "", "input": {}, "result": None,
                                                            "error": False}, at)
        item["last"] = at
        text, error = _head(block.get("content", "")), bool(block.get("is_error"))
        data = item["data"]
        if item["kind"] == "reads":
            for call in data["calls"]:
                if call["id"] == call_id:
                    call["result"], call["error"] = text, error
        elif item["kind"] == "edit":
            data["ok"], data["error"] = not error, text if error else ""
        elif item["kind"] == "question":
            data["answer"] = text
        else:
            data["result"] = text
            if "error" in data:
                data["error"] = error

    def line(self, line: dict, at: int, resumed: dict | None) -> None:
        kind = line.get("type")
        if kind == "result":
            self.add("result", {"stop_reason": line.get("stop_reason") or "", "is_error": bool(line.get("is_error")),
                                "text": line.get("result") or "", "deferred": deferred_of(line)}, at)
            return
        if kind not in ("assistant", "user"):
            return
        content = (line.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                self.add("message", {"role": kind, "text": block["text"]}, at)
            elif block.get("type") == "tool_use" and kind == "assistant":
                self.call(str(block.get("name") or ""), str(block.get("id") or ""), block.get("input") or {}, at)
            elif block.get("type") == "tool_result":
                self.result(block, at, resumed)


def project(lines: list[dict], resumed: dict | None = None, root: str | None = None) -> list[Item]:
    """One turn's items. `resumed`: the previous turn's deferred call (its result item's `deferred`), whose result may open this
    turn. A line whose `parent_tool_use_id` is a subagent's call goes into that subagent's item — projected the same
    way — not into the turn's own; `root` is that parent while a subagent's lines are folded."""
    fold = _Fold()
    for at, line in enumerate(lines):
        parent = line.get("parent_tool_use_id")
        if parent == root:
            fold.line(line, at, resumed)
            continue
        item = fold.owner.get(parent) or fold.calls.get(parent)
        if item is None or item["kind"] != "subagent":
            continue
        fold.children[id(item)].append(line)
        item["last"] = at
        for block in (line.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                fold.owner[str(block.get("id") or "")] = item
    for item in fold.items:
        if item["kind"] == "subagent":
            inner = project(fold.children[id(item)], root=item["data"]["id"])
            item["data"]["items"] = [{"kind": i.kind, "data": i.data} for i in inner]
    return [Item(**item) for item in fold.items]
