"""Probes: the pinned lane CLI, unmodified, against a local fake Messages API. They pin the CLI behaviour the platform
rests on — defer, resume, rules, marker hooks, settings that do not load — so a CLI bump that changes it fails here.

Run them with the pinned binary (`bash deploy/lanes/fetch-cli.sh <dir>`, then `RETINUE_LANE_CLI=<dir>/claude
pytest -m probe`); without it they are skipped. No credential is used except by the login probe, which needs
`CLAUDE_CODE_OAUTH_TOKEN`. The CLI's environment is `cliengine.env` with two test-only substitutions that never go
through `check_env`: the fake API's key and address instead of the token, and the env scrub off — it needs
bubblewrap, which the lane image has and a developer's machine may not.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from fakeapi import FakeApi
from retinue import cliengine, gate, instructions, lane_host, subs
from retinue.config import LaneHostConfig, LaneRef, PlatformConfig, Track
from retinue.lane_host import LaneHost
from retinue.platformd import Platform
from retinue.sessions import PlatformStore


def _pinned() -> bool:
    path = os.environ.get("RETINUE_LANE_CLI")
    if not path or not os.access(path, os.X_OK):
        return False
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return cliengine.version_ok(done.stdout)


pytestmark = [pytest.mark.probe,
              pytest.mark.skipif(not _pinned(), reason=f"RETINUE_LANE_CLI is not the pinned {cliengine.CLI_VERSION}")]

HOOK_TOKEN = "probe-hook-token"
TIMEOUT_S = 90


@dataclass
class Run:
    rc: int
    lines: list[dict]
    stderr: str

    @property
    def result(self) -> dict:
        return next(line for line in self.lines if line.get("type") == "result")

    @property
    def init(self) -> dict:
        return next(line for line in self.lines if line.get("type") == "system" and line.get("subtype") == "init")

    def tool_use_ids(self) -> list[str]:
        return [block["id"] for line in self.lines if line.get("type") == "assistant"
                for block in line["message"]["content"] if block.get("type") == "tool_use"]


class Lane:
    """One working directory, home and config directory, run again and again like a lane runs one session."""

    def __init__(self, root: Path, api: FakeApi) -> None:
        self.api = api
        self.home, self.cwd, self.config = root / "home", root / "work", root / "home" / "config"
        for path in (self.home, self.cwd, self.config):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = root / "settings.json"

    def env(self) -> dict[str, str]:
        env = cliengine.env(str(self.home), str(self.config), "", "", HOOK_TOKEN, 0)
        for key in (cliengine.TOKEN_FD, "HTTPS_PROXY", "NO_PROXY"):
            del env[key]
        return {**env, cliengine.SCRUB: "0", "ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": self.api.url}

    def run(self, prompt: str | None, *, resume: str | None = None, allowed=(), disallowed=(), marks=(),
            hook_url: str | None = None, notes: str = "", skills: bool = False, env: dict | None = None,
            fork: bool = False, mcp: bool = False) -> Run:
        self.settings.write_text(json.dumps(cliengine.settings(hook_url or self.api.hook_url, 30, marks)))
        if notes:
            Path(cliengine.instructions_path(str(self.settings))).write_text(notes)
        argv = cliengine.argv(str(self.settings), list(allowed), list(disallowed), resume, instructions=bool(notes),
                              skills=skills, fork=fork, mcp=(hook_url or self.api.hook_url) if mcp else None)
        done = subprocess.run(argv, cwd=self.cwd, env=env or self.env(), capture_output=True, timeout=TIMEOUT_S,
                              input=cliengine.user_line(prompt) if prompt is not None else b"")
        lines = [json.loads(line) for line in done.stdout.decode().splitlines() if line.startswith("{")]
        return Run(done.returncode, lines, done.stderr.decode())


@pytest.fixture
def api():
    with FakeApi() as fake:
        yield fake


@pytest.fixture
def lane(tmp_path, api):
    return Lane(tmp_path, api)


def bash(command: str) -> tuple[str, dict]:
    return ("Bash", {"command": command, "description": "probe"})


def test_the_argv_cliengine_builds_is_the_one_the_pinned_cli_runs(lane, api):
    """The init line names the pinned version; the hook token reaches the hook through `$RETINUE_HOOK_RUN`."""
    api.tools, api.mode = [bash("true")], "defer"
    run = lane.run("go")
    assert run.rc == 0, run.stderr
    assert run.init["claude_code_version"] == cliengine.CLI_VERSION
    assert run.init["permissionMode"] == "auto", "the scrub off: with it on, the CLI starts in manual (default)"
    assert cliengine.UUID4.fullmatch(run.result["session_id"]), "a session id --resume accepts in check_argv"
    assert {h.authorization for h in api.hooks} == {f"Bearer {HOOK_TOKEN}"}


def test_a_deferred_call_runs_exactly_once_on_resume_and_never_again(lane, api):
    marker = lane.cwd.parent / "ran"
    api.tools, api.mode = [bash(f"echo ran >> {marker}")], "defer"
    first = lane.run("go")
    assert first.rc == 0, first.stderr
    assert first.result["stop_reason"] == "tool_deferred"
    deferred = first.result["deferred_tool_use"]
    assert deferred["name"] == "Bash" and deferred["id"] == first.tool_use_ids()[-1]
    assert not marker.exists()

    api.mode, api.hooks = "allow", []
    session = first.result["session_id"]
    resumed = lane.run(None, resume=session)
    assert [h.tool_use_id for h in api.events("PreToolUse")] == [deferred["id"]], "the same call is asked again"
    assert resumed.result["stop_reason"] != "tool_deferred"
    assert marker.read_text() == "ran\n"

    lane.run(None, resume=session)
    assert marker.read_text() == "ran\n", "a further resume does not run it again"


def test_a_call_is_blocked_when_the_hook_endpoint_is_down_even_under_an_allow_rule(lane, api):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    api.tools = [bash("touch marker")]
    run = lane.run("go", allowed=["Bash(touch:*)"], hook_url=f"http://127.0.0.1:{closed}/hook")
    assert run.tool_use_ids() and not (lane.cwd / "marker").exists(), run.lines


def test_settings_the_agent_writes_have_no_effect_on_the_next_run(lane, api):
    evil = {"permissions": {"allow": ["Bash(echo:*)"], "defaultMode": "bypassPermissions"},
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f"touch {lane.cwd.parent / 'evil-hook'}"}]}]}}
    for path in (lane.cwd / ".claude" / "settings.json", lane.cwd / ".claude" / "settings.local.json",
                 lane.home / ".claude" / "settings.json", lane.config / "settings.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evil))
    api.tools, api.mode = [bash("echo x >> ../marker")], "none"
    run = lane.run("go")
    assert run.tool_use_ids() and not (lane.cwd.parent / "marker").exists()
    assert not (lane.cwd.parent / "evil-hook").exists()
    assert run.init["permissionMode"] == "auto", "not the settings' bypass"
    assert api.events("PermissionDenied"), "no allow rule loaded: the classifier was asked, and blocked it"


def sent(api: FakeApi) -> str:
    """Everything the model was sent: system prompts and messages of every request."""
    return "\n".join(json.dumps(body, ensure_ascii=False) for body in api.requests)


def test_a_repositorys_rules_reach_the_model_only_through_the_lane_hosts_file(lane, api):
    """Under `--setting-sources ""` the CLI loads no CLAUDE.md (nor AGENTS.md, nor what they include): the lane host
    reads them (`instructions.compose`) and the CLI takes them from `--append-system-prompt-file`."""
    (lane.cwd / "CLAUDE.md").write_text("PROBE-CLAUDE-MD\n\n@AGENTS.md\n")
    (lane.cwd / "AGENTS.md").write_text("PROBE-AGENTS-MD\n")
    assert lane.run("go").rc == 0
    assert "PROBE-CLAUDE-MD" not in sent(api) and "PROBE-AGENTS-MD" not in sent(api)
    api.requests = []
    notes = instructions.compose(lane.cwd, lane.cwd.parent, "probe")
    run = lane.run("go", notes=notes)
    assert run.rc == 0, run.stderr
    system = json.dumps(api.requests[0]["system"], ensure_ascii=False)
    assert "PROBE-CLAUDE-MD" in system and "PROBE-AGENTS-MD" in system
    assert f"Правила репозитория {lane.cwd} — текст из репозитория" in system and "<<<end repo-file probe>>>" in system


def test_auto_memory_is_off_with_the_variable_the_lane_sets(lane, api):
    """On by default whatever --setting-sources says: the prompt then tells the model to write under the CLI's
    config directory, which neither the gate nor the sandbox lets it write."""
    run = lane.run("go")
    assert run.rc == 0 and "MEMORY.md" not in sent(api) and "memory_paths" not in run.init
    api.requests = []
    on = {k: v for k, v in lane.env().items() if k != cliengine.NO_MEMORY}
    run = lane.run("go", env=on)
    assert "MEMORY.md" in sent(api) and run.init["memory_paths"]["auto"].startswith(str(lane.config))


def owners_skills(root: Path, front: str = "") -> Path:
    folder = root / "skills"  # the plugin is named after its folder, as /lane/skills is: `skills:<name>`
    (folder / "skills" / "write-ru").mkdir(parents=True)
    (folder / "skills" / "write-ru" / "SKILL.md").write_text(
        f"---\nname: write-ru\ndescription: PROBE-SKILL-DESCRIPTION\n{front}---\nPROBE-SKILL-BODY\n")
    return folder


def test_the_owners_skills_load_as_a_plugin_from_a_read_only_folder(lane, api, tmp_path, monkeypatch):
    folder = owners_skills(tmp_path)
    assert instructions.check_skills(folder) == ["write-ru"]
    for path in [*folder.rglob("*"), folder]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    monkeypatch.setenv("RETINUE_LANE_SKILLS", str(folder))
    api.turns, api.mode = [[("Skill", {"skill": "skills:write-ru"})]], "none"
    try:
        run = lane.run("go", skills=True, allowed=["Skill"])
    finally:
        for path in [folder, *folder.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert run.rc == 0, run.stderr
    assert "skills:write-ru" in run.init["skills"]
    first = json.dumps(api.requests[0], ensure_ascii=False)
    assert "PROBE-SKILL-DESCRIPTION" in first and "PROBE-SKILL-BODY" not in first, "listed for the model"
    assert "PROBE-SKILL-BODY" in sent(api), "and loaded when invoked"
    assert not run.result["permission_denials"], "an allow rule: the classifier is not asked"


def test_a_plugins_hooks_and_a_skills_own_hooks_run_so_such_a_folder_is_never_passed(lane, api, tmp_path,
                                                                                     monkeypatch):
    """Command hooks, outside the sandbox and the platform's gate: `check_skills` refuses both before a run."""
    hook = {"type": "command", "command": f"touch {tmp_path / 'skill-hook'}"}
    folder = owners_skills(tmp_path, front="hooks:\n  PreToolUse:\n    - matcher: \"*\"\n      hooks:\n"
                                           f"        - type: command\n          command: {hook['command']}\n")
    (folder / "hooks").mkdir()
    (folder / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"touch {tmp_path / 'plugin-hook'}"}]}]}}))
    monkeypatch.setenv("RETINUE_LANE_SKILLS", str(folder))
    api.turns, api.mode = [[("Skill", {"skill": "skills:write-ru"})], [bash("true")]], "none"
    run = lane.run("go", skills=True, allowed=["Skill"])
    assert run.rc == 0, run.stderr
    assert (tmp_path / "plugin-hook").exists() and (tmp_path / "skill-hook").exists()
    with pytest.raises(instructions.SkillsRefused):
        instructions.check_skills(folder)


SMUGGLED = {  # how Bun's YAML names a key a line-by-line look misses; `{hook}` is a hooks mapping in flow style
    "double-quoted key": "name: write-ru\ndescription: d\n\"hooks\": {hook}\n",
    "single-quoted key": "name: write-ru\ndescription: d\n'hooks': {hook}\n",
    "flow mapping": "{{name: write-ru, description: d, hooks: {hook}}}\n",
    "merge key": "name: write-ru\ndescription: d\n<<: {{hooks: {hook}}}\n",
    "complex key": "name: write-ru\ndescription: d\n? hooks\n: {hook}\n",
}


@pytest.mark.parametrize("form", SMUGGLED)
def test_a_skills_hook_in_any_yaml_form_runs_and_the_check_refuses_every_one(lane, api, tmp_path, monkeypatch, form):
    marker = tmp_path / "skill-hook"
    hook = f"{{PreToolUse: [{{matcher: '*', hooks: [{{type: command, command: 'touch {marker}'}}]}}]}}"
    folder = tmp_path / "skills"
    (folder / "skills" / "write-ru").mkdir(parents=True)
    (folder / "skills" / "write-ru" / "SKILL.md").write_text(f"---\n{SMUGGLED[form].format(hook=hook)}---\nbody\n")
    monkeypatch.setenv("RETINUE_LANE_SKILLS", str(folder))
    api.turns, api.mode = [[("Skill", {"skill": "skills:write-ru"})], [bash("true")]], "none"
    run = lane.run("go", skills=True, allowed=["Skill"])
    assert run.rc == 0, run.stderr
    assert marker.exists(), f"{form}: the CLI reads it as a hook and runs it, outside the sandbox"
    with pytest.raises(instructions.SkillsRefused, match="frontmatter"):
        instructions.check_skills(folder)


def test_parallel_calls_under_defer_only_the_last_survives(lane, api):
    """Both calls are deferred, `deferred_tool_use` names the last, and on resume only it runs: the earlier one is
    gone from the conversation. The platform records the other approvals of such a run as dropped."""
    api.tools, api.mode = [bash("touch one"), bash("touch two")], "defer"
    first = lane.run("go")
    ids = first.tool_use_ids()
    assert len(ids) == 2 and first.result["deferred_tool_use"]["id"] == ids[1]
    api.mode = "allow"
    lane.run(None, resume=first.result["session_id"])
    assert sorted(p.name for p in lane.cwd.iterdir()) == ["two"]


@pytest.mark.parametrize("command, ran, deferred, blocked", [
    ("touch marker", True, False, False),                  # allow-rule marker: the CLI's rule runs it
    ("mkdir marker", False, True, False),                  # no marker: deferred by the hook
    ("rm -f victim", False, False, False),                 # deny-rule marker: the CLI's rule denies it
    # The marker fired but the rule does not cover the whole command: in auto the classifier decides, not an ask.
    ("touch marker && echo x >> ../other", False, False, True),
])
def test_marker_hooks_let_the_clis_rules_decide_and_defer_the_rest(lane, api, command, ran, deferred, blocked):
    (lane.cwd / "victim").touch()
    api.tools, api.mode = [bash(command)], "marker"
    rules = ["Bash(touch:*)", "Bash(rm:*)"]
    run = lane.run("go", allowed=rules[:1], disallowed=rules[1:], marks=rules)
    assert (lane.cwd / "marker").exists() == ran
    assert (lane.cwd / "victim").exists() and not (lane.cwd.parent / "other").exists()
    assert (run.result["stop_reason"] == "tool_deferred") == deferred
    assert bool(api.events("PermissionDenied")) == blocked
    assert not api.events("PermissionRequest"), "auto never asks"
    if not ran and not deferred:
        assert [d["tool_name"] for d in run.result["permission_denials"]] == ["Bash"]


def test_a_deferred_call_whose_tool_is_gone_at_resume_ends_unavailable(lane, api):
    api.tools, api.mode = [bash("touch marker")], "defer"
    first = lane.run("go")
    api.mode, api.hooks = "allow", []
    resumed = lane.run(None, resume=first.result["session_id"], disallowed=["Bash"])
    assert resumed.result["stop_reason"] == "tool_deferred_unavailable" and resumed.result["is_error"] is True
    assert not api.events("PreToolUse") and not (lane.cwd / "marker").exists()


def agent(prompt: str) -> tuple[str, dict]:
    return ("Agent", {"description": "probe", "prompt": prompt, "subagent_type": "general-purpose"})


def tasks(run: Run) -> list[dict]:
    return [line for line in run.lines if line.get("type") == "system" and line.get("subtype") == "task_started"]


def results(run: Run) -> list[dict]:
    return [line for line in run.lines if line.get("type") == "result"]


def test_subagents_run_in_the_foreground_side_by_side_and_their_calls_carry_their_agent_id(lane, api):
    """With background tasks off (`cliengine.NO_BACKGROUND`): two subagents asked for at once run in parallel, each
    to its end inside the turn, one `result`; every call of theirs passes the hooks with the subagent's `agent_id`
    (the gate's `_subagent`), the main agent's with none; `run_in_background` is not in the tools' schemas."""
    api.scripts = {"go": [[agent("SUB-ONE"), agent("SUB-TWO")]], "SUB-ONE": [[bash("sleep 2; touch one")]],
                   "SUB-TWO": [[bash("sleep 2; touch two")]]}
    api.mode = "allow"
    run = lane.run("go")
    assert run.rc == 0, run.stderr
    assert sorted(p.name for p in lane.cwd.iterdir()) == ["one", "two"]
    assert [t["is_backgrounded"] for t in tasks(run)] == [False, False] and len(results(run)) == 1
    calls = {(h.tool, bool(h.agent_id)) for h in api.events("PreToolUse")}
    assert calls == {("Agent", False), ("Bash", True)}
    schemas = {t["name"]: t["input_schema"]["properties"] for t in api.requests[0]["tools"]}
    assert "run_in_background" not in schemas["Agent"] and "run_in_background" not in schemas["Bash"]
    bash_tool = next(t for t in api.requests[0]["tools"] if t["name"] == "Bash")
    assert "default 600000, max 3600000" in bash_tool["description"], "the lane's Bash timeouts (BASH_TIMEOUTS)"


def test_with_background_tasks_on_a_subagent_outlives_the_turn_and_each_turn_writes_a_result(lane, api):
    """Why they are off: 2.1.304 backgrounds a subagent by default in `-p`; the CLI holds the turn's `result` until
    it ends, then runs a turn on its notification — two results for one run, the platform's one turn per run broken."""
    api.scripts = {"go": [[agent("SUB-ONE")]], "SUB-ONE": [[bash("touch one")]]}
    api.mode = "allow"
    on = {k: v for k, v in lane.env().items() if k != cliengine.NO_BACKGROUND}
    run = lane.run("go", env=on)
    assert run.rc == 0 and (lane.cwd / "one").exists()
    assert [t["is_backgrounded"] for t in tasks(run)] == [True] and len(results(run)) >= 2


def test_a_deferral_inside_a_subagent_is_lost_so_the_gate_denies_there_and_the_subagent_hears_why(lane, api):
    """Deferred, the subagent's call ends the subagent without its report and the run ends as if nothing were
    pending — no `deferred_tool_use`, nothing a resume could run. Denied with the gate's reason, the subagent's model
    reads it as the call's result."""
    api.scripts = {"go": [[agent("SUB-ONE")]], "SUB-ONE": [[bash("touch one")]]}
    api.decide = lambda body: "defer" if body.get("agent_id") else "allow"
    run = lane.run("go")
    assert run.rc == 0 and not (lane.cwd / "one").exists()
    assert run.result["stop_reason"] == "end_turn" and not run.result.get("deferred_tool_use")
    api.requests, api.decide = [], lambda body: f"deny:{gate.SUBAGENT}" if body.get("agent_id") else "allow"
    run = lane.run("go")
    assert run.rc == 0 and not (lane.cwd / "one").exists()
    assert any(gate.SUBAGENT in json.dumps(r, ensure_ascii=False) for r in api.tool_results())
    api.hooks, api.decide = [], lambda body: "none" if body.get("agent_id") else "allow"
    api.scripts["SUB-ONE"] = [[bash("make probe")]]  # the classifier's (here: blocked), as a subagent's call
    assert lane.run("go").rc == 0
    assert [h.agent_id is not None for h in api.events("PermissionDenied")] == [True], "a block names it too"


@pytest.mark.parametrize("sources, loaded", [("", False), ("project", True)])
def test_a_nested_claude_md_is_not_loaded_when_a_file_below_is_read_under_no_setting_sources(lane, api, sources,
                                                                                           loaded):
    """On the Mac a CLAUDE.md of a subfolder joins the context when the model reads a file there; under
    `--setting-sources ""` it never does — a session in the Base's root that works in /work/code/<repo> reads that
    repository's rules only because its instructions tell it to. With `project` both load: a folder's CLAUDE.md, and
    the AGENTS.md of a folder that has none."""
    (lane.cwd / "code" / "x" / "src").mkdir(parents=True)
    (lane.cwd / "code" / "x" / "CLAUDE.md").write_text("PROBE-NESTED-CLAUDE\n")
    (lane.cwd / "code" / "x" / "src" / "f.txt").write_text("PROBE-FILE\n")
    (lane.cwd / "code" / "y").mkdir()
    (lane.cwd / "code" / "y" / "AGENTS.md").write_text("PROBE-NESTED-AGENTS\n")
    (lane.cwd / "code" / "y" / "g.txt").write_text("x\n")
    api.turns = [[("Read", {"file_path": str(lane.cwd / "code" / "x" / "src" / "f.txt")})],
                 [("Read", {"file_path": str(lane.cwd / "code" / "y" / "g.txt")})]]
    api.mode = "allow"
    argv = cliengine.argv(str(lane.settings), [], [], None)
    argv[argv.index("--setting-sources") + 1] = sources
    lane.settings.write_text(json.dumps(cliengine.settings(api.hook_url, 30, [])))
    done = subprocess.run(argv, cwd=lane.cwd, env=lane.env(), capture_output=True, timeout=TIMEOUT_S,
                          input=cliengine.user_line("go"))
    assert done.returncode == 0, done.stderr.decode()
    assert "PROBE-FILE" in sent(api) and ("PROBE-NESTED-CLAUDE" in sent(api)) is loaded
    assert ("PROBE-NESTED-AGENTS" in sent(api)) is loaded


def test_the_owners_memory_index_reaches_the_model_through_the_lane_hosts_file(lane, api, tmp_path):
    (tmp_path / "owner-memory" / "memory").mkdir(parents=True)
    (tmp_path / "owner-memory" / "memory" / "MEMORY.md").write_text("- [PROBE-MEMORY-LINE](x.md)\n")
    (tmp_path / "owner-memory" / "memory" / "x.md").write_text("PROBE-MEMORY-BODY\n")
    notes = instructions.owner_memory(str(tmp_path / "owner-memory"), "probe")
    run = lane.run("go", notes=notes)
    assert run.rc == 0, run.stderr
    system = json.dumps(api.requests[0]["system"], ensure_ascii=False)
    assert "PROBE-MEMORY-LINE" in system and "<<<end owner-memory probe>>>" in system
    assert "PROBE-MEMORY-BODY" not in sent(api)


def _said(body: dict) -> list[str]:
    """The owner's and the model's plain texts of one Messages request, in order."""
    return [block["text"] if isinstance(block, dict) else block for message in body.get("messages", [])
            if message.get("role") in ("user", "assistant")
            for block in (message["content"] if isinstance(message["content"], list) else [message["content"]])
            if isinstance(block, str) or block.get("type") == "text"]


def test_a_fork_starts_from_the_parents_context_and_leaves_the_parent_as_it_was(lane, api):
    """`--resume <parent> --fork-session` (the face's «Ответвить», `cliengine.FORK_SESSION`): with our whole argv the
    fork gets a new session id, its first request carries the parent's conversation, its stream holds only its own
    turn, and the parent's transcript is not touched — the parent's next turn never sees the fork's words."""
    parent = lane.run("запомни 4711")
    assert parent.rc == 0, parent.stderr
    first = parent.result["session_id"]
    transcript = next(lane.config.rglob(f"{first}.jsonl"))
    before = transcript.read_bytes()

    fork = lane.run("какое число?", resume=first, fork=True)
    assert fork.rc == 0, fork.stderr
    assert fork.init["session_id"] == fork.result["session_id"] != first
    assert fork.init["permissionMode"] == "auto"
    assert "запомни 4711" in _said(api.requests[-1]) and _said(api.requests[-1])[-1] == "какое число?"
    assert [line["type"] for line in fork.lines].count("assistant") == 1, "the parent's history is not replayed"
    assert transcript.read_bytes() == before

    again = lane.run("дальше", resume=first)
    assert again.result["session_id"] == first
    assert "какое число?" not in _said(api.requests[-1])
    later = lane.run("ещё", resume=fork.result["session_id"])
    assert later.result["session_id"] == fork.result["session_id"]
    assert {"запомни 4711", "какое число?"} <= set(_said(api.requests[-1]))


def test_a_fork_of_a_parent_with_a_deferred_call_runs_that_call_so_the_platform_refuses_it(lane, api):
    """Why `Platform.fork` refuses a parent waiting on a card: the fork's resume finishes the parent's deferred call
    itself — under the parent's tool_use id, in another session — and the parent's own resume runs it again."""
    api.tools, api.mode = [bash("echo ran")], "defer"
    parent = lane.run("go")
    assert parent.result["stop_reason"] == "tool_deferred"
    call = parent.result["deferred_tool_use"]["id"]
    api.mode, api.hooks = "allow", []
    lane.run("ответвись", resume=parent.result["session_id"], fork=True)
    lane.run(None, resume=parent.result["session_id"])
    assert [h.tool_use_id for h in api.events("PreToolUse")] == [call, call]


ASK = {"questions": [
    {"question": "Где жить?", "header": "Жильё", "multiSelect": False,
     "options": [{"label": "Центр", "description": "старый город"}, {"label": "Окраина", "description": "тихо"}]},
    {"question": "Что взять?", "header": "Вещи", "multiSelect": True,
     "options": [{"label": "Зонт", "description": ""}, {"label": "Ноутбук", "description": ""}]}]}
ANSWERS = {"Где жить?": "Окраина", "Что взять?": "Зонт, Ноутбук"}


def test_a_question_is_offered_only_with_a_host_and_the_plan_tools_are_denied(lane, api):
    """`--permission-prompt-tool stdio` is what offers AskUserQuestion to a headless CLI (with `--permission-prompts
    none`, or with neither, it is gone); it brings EnterPlanMode and ExitPlanMode too, which BASE_DENY takes away."""
    api.tools, api.mode = [bash("true")], "deny"
    run = lane.run("go", disallowed=["EnterPlanMode", "ExitPlanMode"])
    tools = set(run.init["tools"])
    assert "AskUserQuestion" in tools and not {"EnterPlanMode", "ExitPlanMode"} & tools
    assert "AskUserQuestion" in {t["name"] for t in api.requests[-1]["tools"]}


def test_a_question_is_deferred_and_answered_on_resume(lane, api):
    """The gate's loop for AskUserQuestion: deferred, then on a bare resume `allow` with `updatedInput` = the input
    plus `answers` keyed by each question's text; the model is told the answers. An `allow` without them is not
    enough: the tool asks again (PermissionRequest), and that ask is denied — nothing is answered for the owner."""
    api.tools, api.mode = [("AskUserQuestion", ASK)], "defer"
    first = lane.run("go")
    assert first.result["stop_reason"] == "tool_deferred"
    assert first.result["deferred_tool_use"]["name"] == "AskUserQuestion"
    assert first.result["deferred_tool_use"]["input"]["questions"][1]["multiSelect"] is True

    api.mode, api.updated, api.hooks = "allow", {**ASK, "answers": ANSWERS}, []
    resumed = lane.run(None, resume=first.result["session_id"])
    assert resumed.result["stop_reason"] == "end_turn" and not api.events("PermissionRequest")
    told = api.tool_results()[-1]
    assert told["content"] == ('Your questions have been answered: "Где жить?"="Окраина", "Что взять?"="Зонт, Ноутбук". '
                               "You can now continue with these answers in mind.")

    api.mode, api.updated, api.hooks = "defer", None, []
    again = lane.run("ещё раз")
    api.mode, api.hooks = "allow", []
    lane.run(None, resume=again.result["session_id"])
    assert [h.tool for h in api.events("PermissionRequest")] == ["AskUserQuestion"]
    assert api.tool_results()[-1]["is_error"] is True


def test_the_cli_takes_its_token_from_a_pipe_for_every_request(lane, api):
    """The lane host hands the token through a pipe (`lane_host.token_pipe`), never the environment: the CLI reads it
    once and sends it with every request of the run — the first and the one after a tool call."""
    token = "sk-ant-oat01-probe-" + "x" * 40
    token_fd = lane_host.token_pipe(token)
    env = {k: v for k, v in lane.env().items() if k != "ANTHROPIC_API_KEY"}
    env[cliengine.TOKEN_FD] = str(token_fd)
    lane.settings.write_text(json.dumps(cliengine.settings(api.hook_url, 30, [])))
    api.tools, api.mode = [bash("true")], "allow"
    try:
        done = subprocess.run(cliengine.argv(str(lane.settings), [], [], None), cwd=lane.cwd, env=env,
                              pass_fds=(token_fd,), input=cliengine.user_line("go"), capture_output=True,
                              timeout=TIMEOUT_S)
    finally:
        os.close(token_fd)
    assert done.returncode == 0, done.stderr.decode()
    assert len(api.credentials) >= 2 and set(api.credentials) == {f"Bearer {token}"}


@pytest.mark.skipif(not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), reason="needs the owner's CLAUDE_CODE_OAUTH_TOKEN")
def test_the_lane_cli_is_logged_in_with_the_owners_token(tmp_path):
    token_fd = lane_host.token_pipe(os.environ["CLAUDE_CODE_OAUTH_TOKEN"])
    env = cliengine.env(str(tmp_path), str(tmp_path / "config"), "", "", HOOK_TOKEN, token_fd)
    del env["HTTPS_PROXY"], env["NO_PROXY"]
    try:  # the token as the lane host gives it: through a pipe
        done = subprocess.run([cliengine.cli_path(), "auth", "status", "--json"], env={**env, cliengine.SCRUB: "0"},
                              pass_fds=(token_fd,), capture_output=True, text=True, timeout=60)
    finally:
        os.close(token_fd)
    status = json.loads(done.stdout)
    assert status["loggedIn"] is True and status["authMethod"] == "oauth_token"


# --- the platform end to end: the pinned CLI under a real lane host, its hooks answered by the platform's gate ------


class Stack:
    """A lane host and a platform in this process, the CLI between them; either can be torn down and built again
    from its own files. The platform listens on one port throughout, so the hook address in a run's settings stays
    the same across a rebuild — and nothing answers there while it is down."""

    def __init__(self, root: Path, api: FakeApi) -> None:
        self.root, self.api = root, api
        (root / "work" / "repo").mkdir(parents=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        hook_url = f"http://127.0.0.1:{self.port}/hook"
        self.lane_cfg = LaneHostConfig(token="lane-token", hook_url=hook_url, max_running=2, work=str(root / "work"),
                                       home=str(root / "home"), config_dir=str(root / "home" / "config"),
                                       state=str(root / "state"), run_dir=str(root / "run"))
        (root / "run").mkdir()
        self.cfg = PlatformConfig(
            lanes={"code": LaneRef("code", "", hook_url, str(root / "work"), token="lane-token")},
            tracks=[Track("retinue", "code", "code", "repo")], state_db=str(root / "platform.sqlite"),
            router_token="router-token", hook_secret="hook-secret", oauth_token="probe-login")
        self.host = self.lane_server = self.platform = self.runner = None

    def env(self, host: LaneHost, hook_token: str) -> dict[str, str]:
        env = cliengine.env(self.lane_cfg.home, self.lane_cfg.config_dir, "", "", hook_token, 0)
        for key in (cliengine.TOKEN_FD, "HTTPS_PROXY", "NO_PROXY"):
            del env[key]
        return {**env, cliengine.SCRUB: "0", "ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": self.api.url}

    async def lane_up(self) -> None:
        self.host = LaneHost(self.lane_cfg, lane_host.Journal(f"{self.lane_cfg.state}/journal.sqlite"))
        await self.host.start()
        assert self.host.pinned
        self.lane_server = TestServer(self.host.app())
        await self.lane_server.start_server()
        self.cfg.lanes["code"].url = str(self.lane_server.make_url("")).rstrip("/")

    async def lane_down(self) -> None:
        await self.lane_server.close()
        self.host.journal.db.close()

    def platform_object(self) -> None:
        self.store = PlatformStore(self.cfg.state_db)
        self.platform = Platform(self.cfg, self.store)

    async def platform_listening(self) -> None:
        self.runner = web.AppRunner(self.platform.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def platform_down(self) -> None:
        await self.runner.cleanup()
        self.store.db.close()

    async def owner(self, path: str, body: dict) -> dict:
        async with ClientSession() as http:
            async with http.post(f"http://127.0.0.1:{self.port}/owner/{path}", json=body,
                                 headers={"Authorization": "Bearer router-token"}) as response:
                return await response.json()

    async def settled(self) -> None:
        """The lane's CLI processes have all ended and the platform has taken every line."""
        for _ in range(TIMEOUT_S * 10):
            if not self.host.live:
                break
            await asyncio.sleep(0.1)
        assert not self.host.live, "the CLI did not end"
        while await self.platform.poll("code"):
            pass

    def notes(self, kind: str) -> list[dict]:
        return [n for n in self.store.look(200) if n["kind"] == kind]

    def lines(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.store.db.execute("SELECT line FROM raw ORDER BY lane_seq")]

    def results(self) -> list[dict]:
        return [line for line in self.lines() if line.get("type") == "result"]


@pytest.fixture
def stack(tmp_path, api, monkeypatch):
    stack = Stack(tmp_path, api)
    monkeypatch.setattr(LaneHost, "_env", lambda host, hook_token, token_fd: stack.env(host, hook_token))
    monkeypatch.setattr(cliengine, "SETTINGS_PATH",
                        re.compile(re.escape(stack.lane_cfg.run_dir) + "/" + cliengine.RUN_ID.pattern + r"\.json"))
    return stack


def test_e4_a_deferred_call_granted_after_both_processes_restarted_runs_exactly_once(stack, api):
    """In auto: the classifier blocks the call, the gate lets the model retry it, and the same call made again is the
    owner's card with the classifier's reason; granted after both restarts, it runs once, past the classifier."""
    marker = stack.root / "ran"
    api.tools, api.rounds = [bash(f"echo ran >> {marker}")], 2

    async def go():
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        session = (await stack.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "go"}))["session"]
        await stack.settled()
        [card] = stack.notes("card")
        assert stack.store.session(session)["state"] == "waiting" and not marker.exists()
        assert any(line.startswith("Классификатор остановил: Auto mode could not evaluate")
                   for line in card["data"]["view"]["head"])
        [blocked] = [line for line in stack.lines() if line.get("subtype") == "permission_denied"]
        assert stack.store.approval(card["approval"])["tool_use_id"] != blocked["tool_use_id"], "the repeat is the card"

        await stack.platform_down()
        await stack.lane_down()
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        pressed = await stack.owner("press", {"button": card["data"]["buttons"][0][1]})
        assert pressed["toast"] == "Разрешено."
        await stack.settled()
        assert marker.read_text() == "ran\n"
        assert stack.store.approval(card["approval"])["state"] == "executed"
        assert [n["data"]["text"] for n in stack.notes("summary")] == ["done"]
        assert stack.store.session(session)["state"] == "closed"
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_e_question_answered_by_buttons_after_both_restarts_reaches_the_model_once(stack, api):
    """AskUserQuestion end to end: the gate defers it, one card per question; a press, both processes restarted, a
    tick and «Готово» — the resume hands the CLI the answers and the model is told them."""
    api.tools = [("AskUserQuestion", ASK)]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        session = (await stack.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "go"}))["session"]
        await stack.settled()
        first, second = stack.notes("card")
        assert stack.store.session(session)["state"] == "waiting"
        assert [w for w, _ in first["data"]["buttons"]] == ["Центр", "Окраина"]
        assert (await stack.owner("press", {"button": first["data"]["buttons"][1][1]}))["toast"] == \
            "Принято. Осталось ответить: 1."

        await stack.platform_down()
        await stack.lane_down()
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        keep = (await stack.owner("press", {"button": second["data"]["buttons"][0][1]}))["keep"]
        keep = (await stack.owner("press", {"button": keep[1][1]}))["keep"]
        done = await stack.owner("press", {"button": keep[2][1]})
        assert done["toast"] == "Ответ передан в сессию."
        await stack.settled()
        [approval] = stack.store.approvals(session=session, kind="question")
        assert approval["state"] == "executed"
        assert api.tool_results()[-1]["content"].startswith(
            'Your questions have been answered: "Где жить?"="Окраина", "Что взять?"="Зонт, Ноутбук".')
        assert [n["data"]["text"] for n in stack.notes("summary")] == ["done"]
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_an_ask_no_hook_decides_is_denied_by_the_lane_host_and_the_run_ends(stack, api):
    """Hooks that decide nothing (not the gate's: it defers a question and denies every ask): the CLI puts the ask
    to its host over stdio, the lane host denies it at once, and the run ends instead of waiting for an answer."""
    api.tools, api.mode, api.ask = [("AskUserQuestion", ASK)], "none", "none"
    stack.lane_cfg.hook_url = api.hook_url

    async def go():
        await stack.lane_up()
        async with ClientSession() as http:
            body = {"session": "0a1b2c3d", "run": "r1", "cli_session": None, "cwd": "repo",
                    "settings": cliengine.settings(api.hook_url, 30, []), "allowed": [], "disallowed": [],
                    "prompt": "go", "hook_token": "probe-hook-token", "oauth_token": "probe-login"}
            async with http.post(f"{stack.cfg.lanes['code'].url}/run", json=body,
                                 headers={"Authorization": "Bearer lane-token"}) as response:
                assert (await response.json())["status"] == "started"
        for _ in range(TIMEOUT_S * 10):
            if not stack.host.live:
                break
            await asyncio.sleep(0.1)
        assert not stack.host.live, "the run waited on an ask nobody answered"
        lines = [json.loads(i["line"]) for i in stack.host.journal.look(200)]
        [ask] = [line for line in lines if line.get("type") == "control_request"]
        assert ask["request"]["subtype"] == "can_use_tool" and ask["request"]["tool_name"] == "AskUserQuestion"
        assert api.tool_results()[-1]["content"].startswith("Этот вызов спрашивает разрешения")
        await stack.lane_down()

    asyncio.run(go())


def test_e6_a_push_is_a_courier_card_never_a_grant_and_nothing_runs(stack, api):
    api.tools = [bash("git push origin main")]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        await stack.platform_listening()
        session = (await stack.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "go"}))["session"]
        await stack.settled()
        [card] = stack.notes("card")
        assert card["data"]["view"]["body"] == "git push origin main" and card["data"]["buttons"] == []
        assert stack.store.approvals(session=session, states=("pending", "shown", "granted", "executing",
                                                              "executed")) == []
        [result] = stack.results()
        assert result["stop_reason"] != "tool_deferred"
        assert [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_e12_with_the_platform_away_the_call_is_blocked_and_the_journal_waits_for_it(stack, api):
    api.tools = [bash("touch marker")]

    async def go():
        await stack.lane_up()
        stack.platform_object()
        session = stack.store.new_session("retinue", "code", "code", 0.0)
        stack.store.enqueue(session, "go", 0.0)
        await stack.platform.drain()
        for _ in range(TIMEOUT_S * 10):
            if not stack.host.live:
                break
            await asyncio.sleep(0.1)
        assert not (stack.root / "work" / "repo" / "marker").exists(), "no hook answer: blocked"
        # Every line but the partial ones, which the platform hands to the face and never stores.
        journal = [i["line"] for i in stack.host.journal.look(200) if '"type":"stream_event"' not in i["line"]]

        await stack.platform_listening()
        await stack.settled()
        stored = [row[0] for row in stack.store.db.execute("SELECT line FROM raw ORDER BY lane_seq")]
        assert stored == journal and stack.host.journal.look(200) == []
        assert any(i["kind"] == "tool" for i in stack.store.items(session))
        await stack.platform_down()
        await stack.lane_down()

    asyncio.run(go())


def test_the_platforms_mcp_server_connects_its_tools_pass_the_gate_and_the_call_names_the_gated_id(lane, api):
    """Sub-sessions (`subs`): the run's one MCP server, at the hook address under the run's token. Its tools are
    deferred — the model loads them by ToolSearch — every call fires PreToolUse like any other, no decision leaves it
    to auto's classifier, and a call let through reaches the server with the very id the gate saw."""
    tool = subs.PREFIX + subs.START
    api.turns = [[("ToolSearch", {"query": f"select:{tool}", "max_results": 3})],
                 [(tool, {"title": "жильё", "brief": "найди жильё"})]]
    api.decide = lambda body: "none"
    run = lane.run("go", mcp=True)
    assert run.rc == 0, run.stderr
    assert run.init["mcp_servers"] == [{"name": "retinue", "status": "connected", "source": "dynamic"}]
    assert subs.server_failed(run.init) == ""
    assert {a for _, a in api.mcp} == {f"Bearer {HOOK_TOKEN}"}, "the run's token, expanded by the CLI"
    methods = [body.get("method") for body, _ in api.mcp]
    assert methods[:4] == ["server/discover", "initialize", "notifications/initialized", "tools/list"]
    assert [t.get("name") for t in api.requests[0]["tools"] if "mcp" in t.get("name", "")] == [], "deferred"
    assert "PROBE-MCP-INSTRUCTIONS" in sent(api), "the server's instructions reach the model"
    pre = [h for h in api.events("PreToolUse") if h.tool == tool]
    assert pre and pre[0].input == {"title": "жильё", "brief": "найди жильё"}
    assert [h.tool for h in api.events("PermissionDenied")] == [tool], "no decision: the classifier was asked"
    assert "tools/call" not in methods, "blocked: nothing reached the server"

    api.requests, api.hooks, api.mcp, api.decide = [], [], [], (lambda body: "allow")
    run = lane.run("go", mcp=True)
    pre = [h for h in api.events("PreToolUse") if h.tool == tool]
    [called] = [body for body, _ in api.mcp if body.get("method") == "tools/call"]
    assert called["params"]["name"] == subs.START and subs.call_id_of(called["params"]) == pre[0].tool_use_id
    assert any(f"probe {subs.START} {pre[0].tool_use_id}" in json.dumps(r, ensure_ascii=False)
               for r in api.tool_results()), "the server's answer is the tool's result"
    assert list(lane.config.rglob("*.jsonl")), "the scan below covers the CLI's transcripts"
    leaked = [str(path) for path in lane.cwd.parent.rglob("*") if path.is_file() and
              HOOK_TOKEN.encode() in path.read_bytes()]
    assert leaked == [], "the expanded header is written nowhere: not the config, a transcript, a log or a cache"
