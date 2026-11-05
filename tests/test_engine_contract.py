"""The contract with the session engine, held by tests rather than by words: which probe holds each rule for each
engine, a skipped probe is red where it must run, and the words of Claude's own format stay in the modules that already
have them (docs/platform.md, «Probes»)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import yaml

from conftest import MAY_SKIP, REQUIRED
from retinue.config import SESSION_ENGINES

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# The rules of the contract (docs/platform.md, «The engine's contract»).
RULES = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9")
# The probes CI's `probes` job runs, and those only the server runs: they need the owner's login.
IN_CI, ON_SERVER = ("test_probes.py", "test_inbox_probe.py"), ("test_lane_container.py", "test_probes.py")
# What holds each rule for each engine of `SESSION_ENGINES`. Silence means the engine does not give the rule. A name with
# `server:` runs only on the server: CI does not hold it, and a rule held by nothing else is one CI does not hold at all.
HELD = {"claude-code": {
    "D1": ["test_marker_hooks_let_the_clis_rules_decide_and_defer_the_rest",
           "test_subagents_run_in_the_foreground_side_by_side_and_their_calls_carry_their_agent_id",
           "test_the_gate_keeps_a_session_from_sending_to_another_and_lets_it_send_to_its_subagent",
           "test_e6_a_push_is_a_courier_card_never_a_grant_and_nothing_runs"],
    "D2": ["test_a_call_is_blocked_when_the_hook_endpoint_is_down_even_under_an_allow_rule",
           "test_e12_with_the_platform_away_the_call_is_blocked_and_the_journal_waits_for_it"],
    "D3": ["test_a_deferred_call_runs_exactly_once_on_resume_and_never_again",
           "test_e4_a_deferred_call_granted_after_both_processes_restarted_runs_exactly_once",
           "test_e_question_answered_by_buttons_after_both_restarts_reaches_the_model_once"],
    "D4": ["test_a_deferred_call_runs_exactly_once_on_resume_and_never_again",
           "test_d4_a_resume_with_no_transcript_is_its_own_error_and_reaches_no_model"],
    "D5": ["test_settings_the_agent_writes_have_no_effect_on_the_next_run",
           "test_a_repositorys_rules_reach_the_model_only_through_the_lane_hosts_file",
           "test_a_nested_claude_md_is_not_loaded_when_a_file_below_is_read_under_no_setting_sources"],
    "D6": ["test_the_cli_takes_its_token_from_a_pipe_for_every_request",
           "server:test_bash_runs_sandboxed_without_the_token_and_the_web_is_the_clis_alone"],
    "D7": ["server:test_bash_runs_sandboxed_without_the_token_and_the_web_is_the_clis_alone"],
    "D8": ["test_d8_the_init_line_names_the_session_the_model_and_the_mode_and_an_answer_names_its_own_model"],
    "D9": ["test_d9_a_successful_calls_additional_context_reaches_the_model",
           "test_a_failures_additional_context_reaches_the_model"],
}}

# The words only this engine's format has: its hooks' answers, its lines, its tool for questions.
FORMAT_WORDS = ("hookSpecificOutput", "permissionDecision", "deferred_tool_use", "updatedInput", "updatedPermissions",
                "claudecode/", "stream_event", "AskUserQuestion", "additionalContext", "PreToolUse", "PostToolUse",
                "PermissionDenied", "PermissionRequest", "stop_hook_active", "content_block", "parent_tool_use_id",
                "permissionMode")
# The ratchet: the modules that may hold them. This list only shrinks. A new module speaks the platform's own words
# and leaves the format to these; one that stops holding any word comes off the list.
HOLDERS = {"approvals.py", "cliengine.py", "engine.py", "feed.py", "finish.py", "gate.py", "lane_host.py", "mac.py",
           "pings.py", "platformd.py", "questions.py", "sessions.py", "signals.py", "subs.py", "transcript.py",
           "travel.py", "voice.py"}


def _tests_in(names: tuple[str, ...]) -> set[str]:
    trees = (ast.parse((TESTS / name).read_text()) for name in names)
    return {node.name for tree in trees for node in tree.body if isinstance(node, ast.FunctionDef)}


def gaps(held: dict, engines: tuple[str, ...]) -> list[str]:
    """What the registry leaves open: an engine with a rule nothing holds, a name that is no such probe, a rule named
    that is none, or a name that CI would not run without saying `server:`."""
    in_ci, on_server = _tests_in(IN_CI) - MAY_SKIP, _tests_in(ON_SERVER)
    found = []
    for engine in engines:
        rules = held.get(engine, {})
        found += [f"{engine}: {rule} is held by nothing" for rule in RULES if not rules.get(rule)]
        found += [f"{engine}: {rule} is no rule of the contract" for rule in rules if rule not in RULES]
        for rule, probes in rules.items():
            for probe in probes:
                server, name = probe.startswith("server:"), probe.removeprefix("server:")
                if name not in (on_server if server else in_ci):
                    found.append(f"{engine}: {rule}: no such probe {'on the server' if server else 'in CI'}: {name}")
    return found


def test_each_engine_has_a_probe_for_each_rule_of_the_contract():
    assert gaps(HELD, SESSION_ENGINES) == []


def test_an_engine_with_no_entry_or_an_entry_with_no_probe_fails_the_registry():
    assert gaps(HELD, ("claude-code", "other")) == [f"other: {rule} is held by nothing" for rule in RULES]
    broken = {"claude-code": {**HELD["claude-code"], "D2": ["test_no_such_probe"]}}
    assert gaps(broken, ("claude-code",)) == ["claude-code: D2: no such probe in CI: test_no_such_probe"]
    silent = {"claude-code": {**HELD["claude-code"], "D2": []}}
    assert gaps(silent, ("claude-code",)) == ["claude-code: D2 is held by nothing"]


def test_a_probe_only_the_server_runs_cannot_pass_for_one_ci_runs():
    moved = {"claude-code": {**HELD["claude-code"], "D7": ["test_bash_runs_sandboxed_without_the_token_and_the_web_is_the_clis_alone"]}}
    assert gaps(moved, ("claude-code",)) == ["claude-code: D7: no such probe in CI: "
                                             "test_bash_runs_sandboxed_without_the_token_and_the_web_is_the_clis_alone"]


def holders(package: Path) -> dict[str, list[str]]:
    """Each module of `package` that holds a format word, by its path inside it, with the words."""
    found = {}
    for path in sorted(package.rglob("*.py")):
        text = path.read_text()
        if words := [word for word in FORMAT_WORDS if word in text]:
            found[path.relative_to(package).as_posix()] = words
    return found


def test_the_words_of_claudes_format_stay_in_the_listed_modules():
    found = holders(ROOT / "retinue")
    assert not {module: words for module, words in found.items() if module not in HOLDERS}, \
        "a new module holds Claude's format: use the module that already speaks it"
    assert not HOLDERS - set(found), "no format word left there: take it off the list, the list only shrinks"


def test_the_ratchet_catches_a_new_module_with_a_format_word(tmp_path):
    (tmp_path / "graph.py").write_text('answer = {"hookSpecificOutput": {}}\n')
    (tmp_path / "plain.py").write_text('answer = {"decision": "allow"}\n')
    assert holders(tmp_path) == {"graph.py": ["hookSpecificOutput"]}


def _probes(*args: str, required: bool) -> subprocess.CompletedProcess:
    """pytest over a probe with no pinned CLI at hand, with the required mode or without it."""
    env = {key: value for key, value in os.environ.items() if key not in ("RETINUE_LANE_CLI", REQUIRED)}
    if required:
        env[REQUIRED] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "probe", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_with_no_pinned_cli_a_probe_is_skipped_on_a_laptop_and_red_in_ci():
    assert _probes("tests/test_inbox_probe.py", required=False).returncode == 0
    red = _probes("tests/test_inbox_probe.py", required=True)
    assert red.returncode == 1 and "a probe may not be skipped" in red.stdout, red.stdout


def test_the_probe_that_needs_the_owners_login_alone_may_skip_in_ci():
    named = "tests/test_probes.py::test_the_lane_cli_is_logged_in_with_the_owners_token"
    assert _probes(named, required=True).returncode == 0


def test_the_ci_job_runs_the_probes_in_the_required_mode():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    run = "\n".join(step.get("run", "") for step in jobs["probes"]["steps"])
    assert f"{REQUIRED}=1" in run and "-m probe tests --ignore=tests/test_lane_container.py" in run
    assert "fetch-cli.sh" in run and "if" not in jobs["probes"], "the pinned CLI, on every push and pull request"
