"""The transcript projection over stream-json lines the pinned CLI (2.1.304) really printed. The fixtures in
`fixtures/transcript/` were captured with the probe harness (`fakeapi.FakeApi` + `test_probes.Lane`), the scratch
directory's path cut out: a deferred turn and its resume, two parallel reads, a write and an edit, a subagent."""

from __future__ import annotations

import json
from pathlib import Path

from retinue import transcript

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"


def lines(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def kinds(items: list[transcript.Item]) -> list[str]:
    return [item.kind for item in items]


def test_a_deferred_turn_ends_on_the_call_and_its_resume_finishes_it():
    deferred, resumed = lines("deferred"), lines("resumed")
    first = transcript.project(deferred)
    assert kinds(first) == ["tool", "result"]
    call = first[-1].data["deferred"]
    assert first[-1].data["stop_reason"] == "tool_deferred" and call["name"] == "Bash"
    assert first[0].data["id"] == call["id"] and first[0].data["result"] is None, "deferred: nothing ran"
    assert transcript.deferred_of(deferred) == call

    assert next(i for i, line in enumerate(resumed) if line.get("subtype") == "init") > \
        next(i for i, line in enumerate(resumed) if line["type"] == "user"), "the deferred call's result precedes init"
    second = transcript.project(resumed, resumed=call)
    assert kinds(second) == ["tool", "message", "result"]
    assert second[0].data | {"result": None} == first[0].data | {"result": None}
    assert second[0].data["result"] == "(Bash completed with no output)"
    assert second[-1].data == {"stop_reason": "end_turn", "is_error": False, "text": "done", "deferred": None}

    orphan = transcript.project(resumed)
    assert orphan[0].kind == "tool" and orphan[0].data["tool"] == "", "without the previous turn: a result, unnamed"


def test_turns_are_cut_after_each_result():
    deferred, resumed = lines("deferred"), lines("resumed")
    cut = transcript.turns(deferred + resumed)
    assert cut[0] == deferred and cut[1] == resumed[:-1]
    assert [line["type"] for line in cut[2]] == ["command_lifecycle"], "what follows the last result is still open"
    assert transcript.project(cut[2]) == []


def test_parallel_reads_are_one_item_with_every_result():
    items = transcript.project(lines("reads"))
    assert kinds(items) == ["reads", "message", "result"]
    calls = items[0].data["calls"]
    assert [(c["tool"], c["input"]["file_path"]) for c in calls] == [("Read", "/work/notes.md"),
                                                                      ("Read", "/work/todo.md")]
    assert "alpha" in calls[0]["result"] and "milk" in calls[1]["result"] and not any(c["error"] for c in calls)
    assert items[0].last > items[0].first, "the item spans the lines of its results"


def test_an_edit_shows_the_path_and_the_change():
    write, edit = transcript.project(lines("edit"))[:2]
    assert write.data == {"id": write.data["id"], "tool": "Write", "path": "/work/new.txt", "ok": True, "error": "",
                          "content": "hello\n"}
    assert (edit.data["tool"], edit.data["path"], edit.data["old"], edit.data["new"], edit.data["ok"]) == \
        ("Edit", "/work/old.txt", "one", "two", True)


def test_a_subagent_holds_its_own_lines():
    items = transcript.project(lines("subagent"))
    assert kinds(items) == ["subagent", "message", "result"], "the subagent's lines are not the turn's"
    agent = items[0].data
    assert (agent["tool"], agent["agent"], agent["description"]) == ("Agent", "Explore", "look around")
    assert agent["items"] == [{"kind": "message", "data": {"role": "user", "text": "List the files"}}]
    assert agent["result"].startswith("[Subagent hand-back]")


def test_plans_questions_errors_and_other_tools():
    turn = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "План такой."},
            {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [{"content": "a"}]}},
            {"type": "tool_use", "id": "t2", "name": "ExitPlanMode", "input": {"plan": "1. a"}},
            {"type": "tool_use", "id": "t3", "name": "AskUserQuestion", "input": {"questions": [{"question": "?"}]}},
            {"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "false"}},
            {"type": "tool_use", "id": "t5", "name": "Edit", "input": {"file_path": "/work/a", "old_string": "x",
                                                                         "new_string": "y"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "да"},
            {"type": "tool_result", "tool_use_id": "t4", "content": "x" * 5000, "is_error": True},
            {"type": "tool_result", "tool_use_id": "t5", "content": "String not found", "is_error": True}]}},
        {"type": "system", "subtype": "hook_started", "hook_event": "PostToolUse"},
        {"type": "lane", "event": "exited", "code": 0},
    ]
    items = transcript.project(turn)
    assert kinds(items) == ["message", "plan", "plan", "question", "tool", "edit"]
    assert items[1].data["todos"] == [{"content": "a"}] and items[2].data["plan"] == "1. a"
    assert items[3].data["answer"] == "да"
    assert items[4].data["error"] is True and len(items[4].data["result"]) == transcript.RESULT_HEAD
    assert (items[5].data["ok"], items[5].data["error"]) == (False, "String not found")

