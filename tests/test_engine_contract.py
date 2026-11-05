"""The contract with the session engine, held by tests rather than by words: a skipped probe is red where it must run,
and the words of Claude's own format stay in the modules that already have them (docs/platform.md, «Probes»)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from conftest import REQUIRED

ROOT = Path(__file__).resolve().parents[1]

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
