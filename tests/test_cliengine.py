"""The lanes' engine: the one argv, environment and settings file a lane runs the CLI with, and every other one
refused before anything is spawned."""

import json
import re

import pytest

from retinue import cliengine, courier
from retinue.cliengine import Refused
from test_courier import LETTER

SETTINGS = "/lane/run/r1.json"
HOOK_URL = "http://platform:9500/hook"
SESSION = "68c88c62-f9c6-44e3-bbba-6e45dfa967a2"


def refused(call, *args):
    with pytest.raises(Refused) as caught:
        call(*args)
    return str(caught.value)


@pytest.fixture(autouse=True)
def pinned_path(monkeypatch):
    monkeypatch.delenv("RETINUE_LANE_CLI", raising=False)


def test_the_argv_is_the_fixed_prefix_the_rules_and_the_resume_and_never_a_prompt():
    prefix = ["/opt/claude/claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
              "--verbose", "--include-hook-events", "--include-partial-messages", "--forward-subagent-text",
              "--setting-sources", "", "--permission-mode", "auto",
              "--permission-prompt-tool", "stdio", "--settings", SETTINGS]
    assert cliengine.argv(SETTINGS, [], [], None) == prefix, "auto on every run and every resume alike"
    full = cliengine.argv(SETTINGS, ["Read", "Bash(npm test:*)"], ["WebFetch"], SESSION)
    assert full == [*prefix, "--allowedTools", "Read", "Bash(npm test:*)", "--disallowedTools", "WebFetch",
                    "--resume", SESSION]
    assert cliengine.check_argv(full) == full and cliengine.check_argv(prefix) == prefix


@pytest.mark.parametrize("mode", ["manual", "default", "acceptEdits", "dontAsk", "plan"])
def test_another_mode_than_auto_is_off_the_fixed_prefix(mode):
    argv = cliengine.argv(SETTINGS, [], [], None)
    at = argv.index("--permission-mode") + 1
    assert refused(cliengine.check_argv, [*argv[:at], mode, *argv[at + 1:]]) == "argv: не тот фиксированный префикс"


def test_the_cli_path_is_the_images_unless_the_environment_names_another(monkeypatch):
    monkeypatch.setenv("RETINUE_LANE_CLI", "/tmp/claude")
    argv = cliengine.argv(SETTINGS, [], [], None)
    assert argv[0] == "/tmp/claude" and cliengine.check_argv(argv) == argv
    monkeypatch.delenv("RETINUE_LANE_CLI")
    assert refused(cliengine.check_argv, argv) == "argv: не тот фиксированный префикс"


@pytest.mark.parametrize("extra, why", [
    (["--permission-mode", "bypassPermissions"], "режим bypassPermissions"),
    (["--dangerously-skip-permissions"], "флаг --dangerously-skip-permissions"),
    (["--allow-dangerously-skip-permissions"], "флаг --allow-dangerously-skip-permissions"),
    (["--permission-prompt-tool", "mcp__x__y"], "флаг --permission-prompt-tool (кроме stdio префикса)"),
    (["--permission-prompt-tool=mcp__x__y"], "флаг --permission-prompt-tool (кроме stdio префикса)"),
    (["--permission-prompt-tool=stdio"], "флаг --permission-prompt-tool (кроме stdio префикса)"),
    (["--permission-prompts", "none"], "флаг --permission-prompts (он прячет AskUserQuestion)"),
])
def test_a_forbidden_mode_or_flag_is_refused_by_name_wherever_it_stands(extra, why):
    argv = cliengine.argv(SETTINGS, ["Read"], [], None)
    for at in (1, len(argv)):
        assert refused(cliengine.check_argv, [*argv[:at], *extra, *argv[at:]]) == f"argv: запрещено — {why}"


def test_anything_else_off_the_strict_shape_is_refused():
    argv = cliengine.argv(SETTINGS, [], [], None)
    check = cliengine.check_argv
    assert refused(check, [*argv, "--permission-prompt-tool", "stdio"]) == "argv: неизвестный флаг '--permission-prompt-tool'"
    assert refused(check, [*argv, "do it"]) == "argv: позиционный аргумент 'do it'"
    assert refused(check, [*argv[:-1], "do it", *argv[-1:]]) == "argv: файл настроек не в /lane/run/*.json"
    assert refused(check, [*argv, "--model", "x"]) == "argv: неизвестный флаг '--model'"
    assert refused(check, [*argv[:-1]]) == "argv: файл настроек не в /lane/run/*.json"
    for path in ("/tmp/r1.json", "/lane/run/../config/r1.json", "/lane/run/r1.yaml", "/lane/run/a/r1.json"):
        assert refused(check, [*argv[:-1], path]) == "argv: файл настроек не в /lane/run/*.json", path
    assert refused(check, cliengine.argv(SETTINGS, ["Bash(ls\n)"], [], None)) == "argv: не правило — 'Bash(ls\\n)'"
    for rule in ("Bash(ls (x))", "Bash(ls\x00)", "1Bash", "Bash()", "Bash(" + "x" * 201 + ")"):
        assert refused(check, cliengine.argv(SETTINGS, [rule], [], None)).startswith("argv: не правило"), rule
    assert refused(check, [*argv, "--allowedTools"]) == "argv: --allowedTools без правил"
    assert refused(check, [*argv, "--allowedTools", "Read", "--allowedTools", "Grep"]) == "argv: --allowedTools дважды"
    for session in (None, "not-a-uuid", SESSION.upper(), SESSION.replace("-44e3-", "-14e3-")):
        assert refused(check, [*argv, "--resume", *([session] if session else [])]) == "argv: --resume без id сессии"


def test_a_fork_is_the_resume_of_its_parent_with_fork_session_right_after_it_and_nowhere_else():
    """The first run of a fork (`Platform.fork`): `--resume <parent> --fork-session`, pinned by the probes. The flag
    alone, before the resume, or twice is refused like any unknown one; a fork needs the session it copies."""
    check = cliengine.check_argv
    fork = cliengine.argv(SETTINGS, ["Read"], ["WebFetch"], SESSION, fork=True, instructions=True)
    assert fork[-6:] == ["WebFetch", "--resume", SESSION, "--fork-session", "--append-system-prompt-file",
                         "/lane/run/r1.md"]
    assert check(fork) == fork
    assert cliengine.argv(SETTINGS, [], [], SESSION) == cliengine.argv(SETTINGS, [], [], SESSION, fork=False)
    assert refused(lambda: cliengine.argv(SETTINGS, [], [], None, fork=True)) == \
        "argv: ответвление без сессии, от которой ответвляться"
    base = cliengine.argv(SETTINGS, [], [], None)
    assert refused(check, [*base, "--fork-session"]) == "argv: неизвестный флаг '--fork-session'"
    assert refused(check, [*base, "--fork-session", "--resume", SESSION]) == "argv: неизвестный флаг '--fork-session'"
    assert refused(check, [*base, "--resume", SESSION, "--fork-session", "--fork-session"]) == \
        "argv: --fork-session дважды"
    assert refused(check, [*base, "--resume", SESSION, "--allowedTools", "Read", "--fork-session"]) == \
        "argv: неизвестный флаг '--fork-session'"


def test_the_repositorys_rules_and_the_owners_skills_go_only_where_the_run_keeps_them(monkeypatch):
    """`--append-system-prompt-file` names the run's own `<run>.md` beside its settings, `--plugin-dir` the lane's
    skills folder; any other file or folder is refused, so a run cannot be pointed at a plugin that brings hooks."""
    monkeypatch.delenv("RETINUE_LANE_SKILLS", raising=False)
    check = cliengine.check_argv
    full = cliengine.argv(SETTINGS, ["Read"], [], SESSION, instructions=True, skills=True)
    assert full[-4:] == ["--append-system-prompt-file", "/lane/run/r1.md", "--plugin-dir", "/lane/skills"]
    assert check(full) == full
    assert cliengine.argv(SETTINGS, [], [], None) == cliengine.argv(SETTINGS, [], [], None, instructions=False)
    base = cliengine.argv(SETTINGS, [], [], None)
    for path in ("/lane/run/r2.md", "/work/repo/CLAUDE.md", "/lane/run/r1.json", "/lane/run/../run/r1.md"):
        assert refused(check, [*base, "--append-system-prompt-file", path]) == \
            "argv: --append-system-prompt-file не /lane/run/r1.md", path
    assert refused(check, [*base, "--append-system-prompt-file"]) == \
        "argv: --append-system-prompt-file не /lane/run/r1.md"
    for folder in ("/work/repo", "/lane/skills/", "/lane/skills/skills"):
        assert refused(check, [*base, "--plugin-dir", folder]) == "argv: --plugin-dir не /lane/skills", folder
    assert refused(check, [*base, "--plugin-dir", "/lane/skills", "--plugin-dir", "/lane/skills"]) == \
        "argv: --plugin-dir дважды"
    for flag in ("--append-system-prompt", "--system-prompt-file", "--add-dir", "--agents", "--strict-mcp-config"):
        assert refused(check, [*base, flag, "x"]) == f"argv: неизвестный флаг {flag!r}"
    assert refused(check, [*base, "--mcp-config", "x"]) == \
        "argv: --mcp-config не сервер платформы с --strict-mcp-config"
    monkeypatch.setenv("RETINUE_LANE_SKILLS", "/tmp/skills")
    assert cliengine.argv(SETTINGS, [], [], None, skills=True)[-1] == "/tmp/skills"


def test_the_only_mcp_server_a_run_may_have_is_the_platforms_at_the_lanes_own_hook_address():
    """Sub-sessions (`subs`): `--mcp-config` exactly the platform's server at the lane's hook address, the run token
    in its header by the CLI's own expansion, and `--strict-mcp-config` right after it — no other server loads."""
    hook = "http://platform:9500/hook"
    full = cliengine.argv(SETTINGS, [], [], SESSION, mcp=hook)
    assert full[-3:] == ["--mcp-config", cliengine.mcp_config(hook), "--strict-mcp-config"]
    assert json.loads(full[-2]) == {"mcpServers": {"retinue": {
        "type": "http", "url": "http://platform:9500/hook/mcp",
        "headers": {"Authorization": "Bearer ${RETINUE_HOOK_RUN}"}}}}
    assert cliengine.check_argv(full, hook) == full
    base = cliengine.argv(SETTINGS, [], [], None)
    other = json.dumps({"mcpServers": {"x": {"type": "stdio", "command": "sh"}}})
    for tail in ([cliengine.mcp_config(hook)], [cliengine.mcp_config("http://evil:1/hook"), "--strict-mcp-config"],
                 [other, "--strict-mcp-config"], ["--strict-mcp-config", cliengine.mcp_config(hook)]):
        assert refused(lambda a: cliengine.check_argv(a, hook), [*base, "--mcp-config", *tail]) == \
            "argv: --mcp-config не сервер платформы с --strict-mcp-config", tail
    assert refused(cliengine.check_argv, full) == "argv: --mcp-config не сервер платформы с --strict-mcp-config", \
        "a lane host that names no hook address takes no server"


def test_auto_memory_is_off_on_every_run():
    """Read whatever --setting-sources says, and written where nothing lets the model write: off, required."""
    env = cliengine.env("/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook", 5)
    assert env[cliengine.NO_MEMORY] == "1" and cliengine.NO_MEMORY == "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
    for value in ({**env, cliengine.NO_MEMORY: "0"}, {k: v for k, v in env.items() if k != cliengine.NO_MEMORY}):
        assert refused(cliengine.check_env, value) == "env: без CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"


def test_background_tasks_are_off_on_every_run():
    """A backgrounded subagent or command outlives its turn and makes the CLI write a result per turn after it, and a
    deferral inside such a subagent is lost (probed): off, required."""
    env = cliengine.env("/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook", 5)
    assert env[cliengine.NO_BACKGROUND] == "1" and cliengine.NO_BACKGROUND == "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
    for value in ({**env, cliengine.NO_BACKGROUND: "0"},
                  {k: v for k, v in env.items() if k != cliengine.NO_BACKGROUND}):
        assert refused(cliengine.check_env, value) == "env: без CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1"
    # With no background, a build runs as one foreground call: 10 minutes by default, an hour at most.
    assert (env["BASH_DEFAULT_TIMEOUT_MS"], env["BASH_MAX_TIMEOUT_MS"]) == ("600000", "3600000")
    assert refused(cliengine.check_env, {**env, "BASH_MAX_TIMEOUT_MS": "1h"}) == "env: BASH_MAX_TIMEOUT_MS не целое число"


def test_the_environment_is_built_from_scratch_with_the_scrub_off_as_auto_needs():
    """The scrub forces manual (2.1.304); the managed settings keep the secrets from commands instead."""
    env = cliengine.env("/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook", 5)
    assert cliengine.check_env(env) == env
    assert set(env) == cliengine.ENV_KEYS and env[cliengine.SCRUB] == "0"
    assert env[cliengine.TOKEN_FD] == "5" and env["RETINUE_HOOK_RUN"] == "hook"
    assert refused(cliengine.check_env, {**env, "CLAUDE_CODE_OAUTH_TOKEN": "t"}) == \
        "env: CLAUDE_CODE_OAUTH_TOKEN в среде линии", "the token goes through a pipe, never the environment"
    for fd in ("", "x", "-1"):
        assert refused(cliengine.check_env, {**env, cliengine.TOKEN_FD: fd}) == f"env: без {cliengine.TOKEN_FD}"
    assert refused(cliengine.check_env, {**env, "ANTHROPIC_API_KEY": "sk"}) == "env: ANTHROPIC_API_KEY в среде линии"
    assert refused(cliengine.check_env, {**env, "ANTHROPIC_BASE_URL": "x"}) == "env: ANTHROPIC_BASE_URL в среде линии"
    for scrub in ("1", "", "true"):
        assert refused(cliengine.check_env, {**env, cliengine.SCRUB: scrub}) == f"env: без {cliengine.SCRUB}=0"
    without = {k: v for k, v in env.items() if k != cliengine.SCRUB}
    assert refused(cliengine.check_env, without) == f"env: без {cliengine.SCRUB}=0", "set, not left to a default"
    assert env["CLAUDE_CODE_FORK_SUBAGENT"] == "0", "no fork subagents: a turn defers one call at a time"
    for forks in ({**env, cliengine.FORK: "1"}, {k: v for k, v in env.items() if k != cliengine.FORK}):
        assert refused(cliengine.check_env, forks) == "env: без CLAUDE_CODE_FORK_SUBAGENT=0"
    assert refused(cliengine.check_env, {**env, "LD_PRELOAD": "/x.so"}) == "env: лишние переменные LD_PRELOAD"


def test_a_run_waits_out_the_subscription_limit_within_bounds():
    """The watchdog is on, a weekly limit (a reset past 5 h) fails at once, no request waits past 6 h; the CLI reads
    each value as plain digits, so the lane refuses anything else."""
    env = cliengine.env("/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook", 5)
    assert env["CLAUDE_CODE_RETRY_WATCHDOG"] == "1"
    assert env["CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS"] == "18000000"
    assert env["CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS"] == "21600000"
    assert "CLAUDE_CODE_RETRY_WATCHDOG_USAGE_LIMIT_RECHECK_MS" not in env, "2.1.305: not in the pinned CLI"
    for bad in ("5h", "-1", "0", "1e9", " 60000"):
        assert refused(cliengine.check_env, {**env, "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS": bad}) == \
            "env: CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS не целое число", bad


def test_the_settings_hold_only_blocking_http_hooks_back_to_the_platform():
    data = cliengine.settings(HOOK_URL, 30, ["Bash(npm test:*)", "Read"])
    assert cliengine.check_settings(data, HOOK_URL) is data and data["cleanupPeriodDays"] == 30
    pre = data["hooks"]["PreToolUse"]
    assert [(e["matcher"], e["hooks"][0]["url"], e["hooks"][0].get("if")) for e in pre] == [
        ("Bash", f"{HOOK_URL}/mark/0", "Bash(npm test:*)"), ("Read", f"{HOOK_URL}/mark/1", None),
        ("*", f"{HOOK_URL}/PreToolUse", None)]
    assert {event: [h["url"] for e in entries for h in e["hooks"]] for event, entries in data["hooks"].items()
            if event != "PreToolUse"} == {e: [f"{HOOK_URL}/{e}"] for e in ("PermissionRequest", "PermissionDenied",
                                                                           "PostToolUse", "Stop")}
    assert pre[2]["hooks"][0] == {"type": "http", "url": f"{HOOK_URL}/PreToolUse", "timeout": 15, "onFailure": "block",
                                  "headers": {"Authorization": "Bearer $RETINUE_HOOK_RUN"},
                                  "allowedEnvVars": ["RETINUE_HOOK_RUN"]}


def test_every_secret_the_clis_environment_could_carry_is_named_for_the_managed_settings_to_deny():
    env = cliengine.env("/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook", 5)
    assert {cliengine.HOOK_TOKEN, cliengine.TOKEN_FD} <= set(cliengine.SECRET_ENV) & set(env)
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"} <= set(cliengine.SECRET_ENV), "never there: denied anyway"
    assert not {"PATH", "HOME", "HTTPS_PROXY", "NO_PROXY", cliengine.SCRUB} & set(cliengine.SECRET_ENV)


def test_the_hook_token_has_a_name_the_scrub_keeps():
    # The scrub blanks secret-looking names in the hooks' headers too (CLI 2.1.304, seen on the server): the hook
    # then arrives with an empty token and the platform answers «Неизвестный запуск» to every call.
    assert not re.search(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|AUTH", cliengine.HOOK_TOKEN)


def _with_hook(**changes):
    data = cliengine.settings(HOOK_URL, 30, ["Bash(ls:*)"])
    data["hooks"]["PreToolUse"][0]["hooks"][0].update(changes)
    return data


def test_settings_off_that_shape_are_refused():
    check = cliengine.check_settings
    assert refused(check, cliengine.settings("http://evil.example/hook", 30, []), HOOK_URL).startswith(
        "settings: PreToolUse — чужой адрес")
    assert refused(check, _with_hook(url="http://platform:9500/hook/mark/x"), HOOK_URL).startswith(
        "settings: PreToolUse — чужой адрес")
    stop_mark = cliengine.settings(HOOK_URL, 30, [])
    stop_mark["hooks"]["Stop"][0]["hooks"][0]["url"] = f"{HOOK_URL}/mark/0"
    assert refused(check, stop_mark, HOOK_URL) == "settings: Stop — чужой адрес 'http://platform:9500/hook/mark/0'"
    assert refused(check, _with_hook(onFailure="continue"), HOOK_URL) == \
        "settings: PreToolUse — хук не http, не блокирует при сбое или не тот заголовок"
    assert refused(check, _with_hook(type="command", command="sh"), HOOK_URL) == "settings: PreToolUse — не те поля хука"
    assert refused(check, _with_hook(headers={"Authorization": "Bearer $CLAUDE_CODE_OAUTH_TOKEN"}), HOOK_URL) == \
        "settings: PreToolUse — хук не http, не блокирует при сбое или не тот заголовок"
    assert refused(check, _with_hook(**{"if": "Bash(ls\n)"}), HOOK_URL) == "settings: PreToolUse — if не правило"
    for extra in ({"permissions": {"allow": ["Bash"]}}, {"env": {"X": "1"}}):
        assert refused(check, {**cliengine.settings(HOOK_URL, 30, []), **extra}, HOOK_URL) == \
            "settings: верхний уровень — только hooks и cleanupPeriodDays"
    assert refused(check, {"hooks": {"SessionStart": []}, "cleanupPeriodDays": 30}, HOOK_URL) == \
        "settings: события не из PreToolUse, PermissionRequest, PermissionDenied, PostToolUse, Stop"
    regex = cliengine.settings(HOOK_URL, 30, [])
    regex["hooks"]["PreToolUse"][0]["matcher"] = "Bash|Write"
    assert refused(check, regex, HOOK_URL) == "settings: PreToolUse — не та запись"


def test_a_user_line_is_one_stream_json_message_and_the_version_is_the_pin():
    assert cliengine.user_line("Привет") == \
        '{"type":"user","message":{"role":"user","content":"Привет"}}\n'.encode()
    assert cliengine.version_ok("2.1.304 (Claude Code)\n")
    assert not cliengine.version_ok("2.1.306 (Claude Code)") and not cliengine.version_ok("")


def test_sha256_of_is_the_couriers_canonical_digest():
    assert courier.sha256_of(LETTER.as_dict()) == courier.digest(LETTER)
    assert courier.sha256_of({"b": 1, "a": "ё"}) == courier.sha256_of({"a": "ё", "b": 1})
