"""A turn's end and its loops (`finish`): files changed after their last passing check send the turn on once, and the
same call or failure again and again is warned, denied, then cut — counted per turn, per agent."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from retinue import finish, signals
from retinue.config import LaneRef
from retinue.finish import Checks, Loops, Tree
from retinue.gate import Gate
from retinue.sessions import PlatformStore

TREES = [Tree("private", "/work/base", "base"), Tree("private", "/work/code", "code")]


def edit(checks: Checks, path: str, tool: str = "Edit", session: str = "s") -> bool:
    return checks.edited(session, "private", tool, {"file_path": path}, "/work")


def test_code_changed_after_its_check_blocks_the_end_once_with_the_files_and_the_way_to_check():
    checks = Checks(TREES)
    assert edit(checks, "/work/code/acme/app.py")
    assert edit(checks, "/work/code/acme/web/page.ts", "Write")
    reason = checks.stop("s", {})
    assert "/work/code/acme: app.py, web/page.ts" in reason and "Not checked:" in reason
    assert reason
    assert checks.stop("s", {"stop_hook_active": True}) == "", "the Stop after a block passes"
    assert checks.stop("s", {}) == "", "and what was pending is forgotten"


def test_a_passing_check_after_the_edit_lets_the_turn_end():
    checks = Checks(TREES)
    edit(checks, "/work/code/acme/app.py")
    checks.ran("s", "private", "uv run pytest -q", "/work/code/acme")
    assert checks.stop("s", {}) == ""


def test_a_check_counts_for_the_tree_it_runs_in_and_not_for_another():
    checks = Checks(TREES)
    edit(checks, "/work/code/acme/app.py")
    edit(checks, "/work/code/demo/main.go")
    checks.ran("s", "private", "go test ./...", "/work/code/demo")
    reason = checks.stop("s", {})
    assert "/work/code/acme" in reason and "demo" not in reason


def test_a_check_named_by_path_counts_for_that_tree_and_one_from_elsewhere_for_every_pending_one():
    checks = Checks(TREES)
    edit(checks, "/work/code/acme/app.py")
    edit(checks, "/work/code/demo/app.py")
    checks.ran("s", "private", "cd /work/code/acme && pytest", "/work")
    reason = checks.stop("s", {})
    assert "/work/code/demo" in reason and "acme:" not in reason
    checks.forget("s")
    edit(checks, "/work/code/acme/app.py")
    edit(checks, "/work/code/demo/app.py")
    checks.ran("s", "private", "pytest", "/tmp")
    assert checks.stop("s", {}) == ""


def test_the_base_needs_its_own_lint_and_any_of_its_files_counts():
    checks = Checks(TREES)
    assert edit(checks, "/work/base/travel/knowledge/x.md")
    checks.ran("s", "private", "pytest", "/work/base")
    assert "scripts/lint.py" in checks.stop("s", {})
    checks.forget("s")
    edit(checks, "/work/base/travel/knowledge/x.md")
    checks.ran("s", "private", "python3 scripts/lint.py", "/work/base")
    assert checks.stop("s", {}) == ""


def test_prose_in_a_repository_a_file_outside_the_trees_and_a_read_need_no_check():
    checks = Checks(TREES)
    assert not edit(checks, "/work/code/acme/README.md")
    assert not edit(checks, "/work/scratch/notes.py")
    assert not edit(checks, "/work/code/acme/app.py", "Read")
    assert not checks.edited("s", "other", "Edit", {"file_path": "/work/code/acme/app.py"}, "/work")
    assert checks.stop("s", {}) == ""


def test_a_relative_path_resolves_against_the_cwd_and_names_written_to_the_model_are_cleaned():
    checks = Checks(TREES)
    assert checks.edited("s", "private", "Edit", {"file_path": "a`b\nc.py"}, "/work/code/acme")
    reason = checks.stop("s", {})
    assert "abc.py" in reason and "\n" not in reason


def test_sessions_are_apart():
    checks = Checks(TREES)
    edit(checks, "/work/code/acme/app.py", session="a")
    assert checks.stop("b", {}) == "" and checks.stop("a", {}) != ""


def test_the_same_fetch_reworded_with_the_same_outcome_is_warned_denied_then_cut():
    loops = Loops()
    url = "https://example.com/page#top"
    seen = []
    for n in range(1, finish.CUT + 1):
        tool_input = {"url": url if n % 2 else "https://EXAMPLE.com/page", "prompt": f"look {n}"}
        seen.append(loops.before("s", "", "WebFetch", tool_input, f"t{n}")[0])
        if not seen[-1]:
            warning = loops.after("s", "", "WebFetch", tool_input, f"Page text, read at 12:0{n}", call_id=f"t{n}")
            assert bool(warning) == (n == finish.WARN)
    assert seen == [""] * (finish.DENY - 1) + ["deny"] * (finish.CUT - finish.DENY) + ["cut"]


def test_a_call_whose_outcome_changes_is_not_a_loop():
    loops = Loops()
    for n in range(finish.CUT + 2):
        call = {"url": "https://example.com/long", "prompt": f"part {n}"}
        assert loops.before("s", "", "WebFetch", call, f"t{n}") == ("", "")
        loops.after("s", "", "WebFetch", call, f"section {'abcdefghijkl'[n]}", call_id=f"t{n}")


def test_a_resume_firing_the_same_call_id_again_counts_it_once():
    loops = Loops()
    for _ in range(finish.CUT + 2):
        assert loops.before("s", "", "Bash", {"command": "make deploy"}, "t1") == ("", "")


def test_waiting_on_purpose_is_not_counted():
    loops = Loops()
    for n in range(finish.CUT + 2):
        assert loops.before("s", "", "Bash", {"command": "sleep 30; gh run view 1 --json status"}, f"t{n}") == ("", "")


def test_a_subagent_is_denied_never_cut_and_counted_apart_from_the_main_agent():
    loops = Loops()
    for n in range(finish.CUT + 2):
        how, _ = loops.before("s", "agent-1", "Bash", {"command": "curl x"}, f"t{n}")
    assert how == "deny"
    assert loops.before("s", "", "Bash", {"command": "curl x"}, "m1") == ("", "")


def test_the_same_failure_in_a_row_is_warned_on_the_second_and_denied_on_the_third():
    loops = Loops()
    call = {"command": "make deploy"}
    for n, warned in enumerate([False, True, False]):
        assert loops.before("s", "", "Bash", call, f"t{n}") == ("", "")
        assert bool(loops.after("s", "", "Bash", call, error=f"Exit code 2\nboom after {n}s", call_id=f"t{n}")) == warned
    how, reason = loops.before("s", "", "Bash", call, "t3")
    assert how == "deny"


def test_a_failure_that_changes_after_a_fix_is_progress():
    loops = Loops()
    call = {"command": "pytest"}
    for n in range(6):
        assert loops.before("s", "", "Bash", call, f"t{n}") == ("", "")
        loops.after("s", "", "Bash", call, error=f"FAILED test_{'abcdef'[n]}", call_id=f"t{n}")


def test_an_edit_is_progress_for_its_own_agent_only_and_the_end_of_a_turn_forgets_the_counts():
    loops = Loops()
    call = {"command": "pytest"}
    for n in range(finish.FAILS_DENY):
        loops.before("s", "", "Bash", call, f"m{n}")
        loops.after("s", "", "Bash", call, error="Exit code 1", call_id=f"m{n}")
        loops.before("s", "agent-1", "Bash", call, f"a{n}")
        loops.after("s", "agent-1", "Bash", call, error="Exit code 1", call_id=f"a{n}")
    loops.after("s", "", "Edit", {"file_path": "/work/code/acme/app.py"}, "ok", call_id="e1")
    assert loops.before("s", "", "Bash", call, "m9") == ("", ""), "the main agent edited: its count starts again"
    assert loops.before("s", "agent-1", "Bash", call, "a9")[0] == "deny", "the subagent's did not"
    loops.end("s")
    assert loops.before("s", "agent-1", "Bash", call, "a3") == ("", "")


@pytest.mark.parametrize("command, counts", [
    ("uv run --with pytest pytest -q", True), ("pnpm -s typecheck && pnpm -s lint", True), ("python3 -m pytest", True),
    ("mvn -q clean verify", True), ("make -C web test", True), ("FOO=1 npm run test", True),
    ("cat scripts/lint.py", False), ("echo pytest", False), ("git log | grep pytest", False),
])
def test_a_check_is_a_part_of_the_command_that_starts_with_one(command, counts):
    assert finish.checks(command, finish.CODE_CHECK) is counts


def test_a_check_naming_one_repository_does_not_count_for_another_with_a_longer_name():
    checks = Checks(TREES)
    edit(checks, "/work/code/app/a.py")
    edit(checks, "/work/code/app2/a.py")
    checks.ran("s", "private", "cd /work/code/app2 && pytest", "/work")
    assert "/work/code/app:" in checks.stop("s", {})


def test_after_a_cards_resume_the_turn_is_not_sent_on_twice():
    checks = Checks(TREES)
    edit(checks, "/work/code/acme/app.py")
    assert checks.stop("s", {})
    edit(checks, "/work/code/acme/app.py")
    assert checks.stop("s", {}) == "", "a new process: no stop_hook_active, but this turn was sent on already"


# --- through the gate's hooks ---------------------------------------------------------------------------------------

TOKEN = "hook-token"


@pytest.fixture
def setup(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    (tmp_path / "work").mkdir()
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    the_gate = Gate(store, {"private": LaneRef("private", "http://lane", "http://platform/hook",
                                               str(tmp_path / "work"))}, "secret")
    the_gate.checks.trees = TREES
    session = store.new_session("retinue", "private", "code", 0.0)
    store.add_run(session, the_gate.token_hash(TOKEN), [dataclasses.asdict(m) for m in []], 0.0)
    yield store, the_gate, session
    asyncio.set_event_loop(None)
    loop.close()


def hook(the_gate: Gate, event: str, body: dict) -> dict:
    return asyncio.get_event_loop().run_until_complete(the_gate.hook(event, TOKEN, {"hook_event_name": event, **body}))


def kinds(store: PlatformStore) -> list[tuple[str, str]]:
    return [tuple(r) for r in store.db.execute("SELECT kind, what FROM signals WHERE kind IN ('finish', 'loop') "
                                               "ORDER BY id")]


def test_the_gate_blocks_a_stop_over_an_unchecked_edit_once_and_journals_it(setup):
    store, the_gate, _ = setup
    hook(the_gate, "PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "/work/code/acme/a.py"},
                                   "cwd": "/work/code/acme", "tool_use_id": "t1"})
    answer = hook(the_gate, "Stop", {"stop_hook_active": False, "last_assistant_message": "Done."})
    assert answer["decision"] == "block" and "/work/code/acme: a.py" in answer["reason"]
    assert hook(the_gate, "Stop", {"stop_hook_active": True}) == {}
    assert kinds(store) == [(signals.FINISH, "stop_block")]


def test_a_passing_check_through_the_gate_clears_it_and_a_failed_one_does_not(setup):
    _, the_gate, _ = setup
    hook(the_gate, "PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "/work/code/acme/a.py"},
                                   "cwd": "/work/code/acme"})
    hook(the_gate, "PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": "pytest"},
                                          "cwd": "/work/code/acme", "error": "Exit code 1"})
    assert hook(the_gate, "Stop", {}).get("decision") == "block"
    hook(the_gate, "Stop", {"stop_hook_active": True})
    hook(the_gate, "PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "/work/code/acme/a.py"},
                                   "cwd": "/work/code/acme"})
    hook(the_gate, "PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest"}, "cwd": "/work/code/acme"})
    assert hook(the_gate, "Stop", {}) == {}


def test_the_gate_warns_on_a_repeated_failure_and_cuts_a_main_agents_loop(setup):
    store, the_gate, _ = setup
    body = {"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}, "cwd": "/work"}
    answers = []
    for n in range(finish.CUT):
        answers.append(hook(the_gate, "PreToolUse", {**body, "tool_use_id": f"t{n}", "permission_mode": "auto"}))
        if n == 0:
            warn = hook(the_gate, "PostToolUseFailure", {**body, "error": "Exit code 7", "tool_use_id": "t0"})
            assert warn == {}
        if n == 1:
            warn = hook(the_gate, "PostToolUseFailure", {**body, "error": "Exit code 7", "tool_use_id": "t1"})
            assert warn["hookSpecificOutput"]["additionalContext"]
            assert warn["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert answers[-1]["continue"] is False and answers[-1]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert answers[finish.DENY - 1]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "continue" not in answers[finish.DENY - 1]
    assert ("loop", "warn") in kinds(store) and kinds(store)[-1] == ("loop", "cut")


def test_a_turn_that_ended_without_a_passing_stop_leaves_nothing_to_the_next(setup):
    _, the_gate, session = setup
    hook(the_gate, "PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "/work/code/acme/a.py"},
                                   "cwd": "/work/code/acme"})
    for n in range(finish.DENY):
        hook(the_gate, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "curl x"}, "cwd": "/work",
                                      "tool_use_id": f"t{n}", "permission_mode": "auto"})
    the_gate.turn_over(session)  # the owner's «Stop», an error, a lane gone
    assert hook(the_gate, "Stop", {}) == {}
    answer = hook(the_gate, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "curl x"}, "cwd": "/work",
                                           "tool_use_id": "t9", "permission_mode": "auto"})
    assert answer.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
