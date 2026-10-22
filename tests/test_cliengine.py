"""The lanes' engine: the one argv, environment and settings file a lane runs the CLI with, and every other one
refused before anything is spawned."""

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
              "--verbose", "--include-hook-events", "--setting-sources", "", "--permission-mode", "manual",
              "--permission-prompts", "none", "--settings", SETTINGS]
    assert cliengine.argv(SETTINGS, [], [], None) == prefix
    full = cliengine.argv(SETTINGS, ["Read", "Bash(npm test:*)"], ["WebFetch"], SESSION)
    assert full == [*prefix, "--allowedTools", "Read", "Bash(npm test:*)", "--disallowedTools", "WebFetch",
                    "--resume", SESSION]
    assert cliengine.check_argv(full) == full and cliengine.check_argv(prefix) == prefix


def test_the_cli_path_is_the_images_unless_the_environment_names_another(monkeypatch):
    monkeypatch.setenv("RETINUE_LANE_CLI", "/tmp/claude")
    argv = cliengine.argv(SETTINGS, [], [], None)
    assert argv[0] == "/tmp/claude" and cliengine.check_argv(argv) == argv
    monkeypatch.delenv("RETINUE_LANE_CLI")
    assert refused(cliengine.check_argv, argv) == "argv: не тот фиксированный префикс"


@pytest.mark.parametrize("extra, why", [
    (["--permission-mode", "bypassPermissions"], "режим bypassPermissions"),
    (["--permission-mode", "auto"], "режим auto"),
    (["--dangerously-skip-permissions"], "флаг --dangerously-skip-permissions"),
    (["--allow-dangerously-skip-permissions"], "флаг --allow-dangerously-skip-permissions"),
    (["--permission-prompt-tool", "mcp__x__y"], "флаг --permission-prompt-tool"),
    (["--permission-prompt-tool=mcp__x__y"], "флаг --permission-prompt-tool"),
])
def test_a_forbidden_mode_or_flag_is_refused_by_name_wherever_it_stands(extra, why):
    argv = cliengine.argv(SETTINGS, ["Read"], [], None)
    for at in (1, len(argv)):
        assert refused(cliengine.check_argv, [*argv[:at], *extra, *argv[at:]]) == f"argv: запрещено — {why}"


def test_anything_else_off_the_strict_shape_is_refused():
    argv = cliengine.argv(SETTINGS, [], [], None)
    check = cliengine.check_argv
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


def test_the_environment_is_built_from_scratch_with_the_scrub_on():
    env = cliengine.env("tok", "/lane/config", "/lane/config/claude", "http://egress:3128", "platform", "hook")
    assert cliengine.check_env(env) == env
    assert set(env) == cliengine.ENV_KEYS and env[cliengine.SCRUB] == "1"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and env["RETINUE_HOOK_TOKEN"] == "hook"
    assert refused(cliengine.check_env, {**env, "ANTHROPIC_API_KEY": "sk"}) == "env: ANTHROPIC_API_KEY в среде линии"
    assert refused(cliengine.check_env, {**env, "ANTHROPIC_BASE_URL": "x"}) == "env: ANTHROPIC_BASE_URL в среде линии"
    assert refused(cliengine.check_env, {**env, cliengine.SCRUB: "0"}) == f"env: без {cliengine.SCRUB}=1"
    without = {k: v for k, v in env.items() if k != cliengine.SCRUB}
    assert refused(cliengine.check_env, without) == f"env: без {cliengine.SCRUB}=1"
    assert refused(cliengine.check_env, {**env, "LD_PRELOAD": "/x.so"}) == "env: лишние переменные LD_PRELOAD"


def test_the_settings_hold_only_blocking_http_hooks_back_to_the_platform():
    data = cliengine.settings(HOOK_URL, 30, ["Bash(npm test:*)", "Read"])
    assert cliengine.check_settings(data, HOOK_URL) is data and data["cleanupPeriodDays"] == 30
    pre = data["hooks"]["PreToolUse"]
    assert [(e["matcher"], e["hooks"][0]["url"], e["hooks"][0].get("if")) for e in pre] == [
        ("Bash", f"{HOOK_URL}/mark/0", "Bash(npm test:*)"), ("Read", f"{HOOK_URL}/mark/1", None),
        ("*", f"{HOOK_URL}/PreToolUse", None)]
    assert {event: [h["url"] for e in entries for h in e["hooks"]] for event, entries in data["hooks"].items()
            if event != "PreToolUse"} == {e: [f"{HOOK_URL}/{e}"] for e in ("PermissionRequest", "PostToolUse", "Stop")}
    assert pre[2]["hooks"][0] == {"type": "http", "url": f"{HOOK_URL}/PreToolUse", "timeout": 15, "onFailure": "block",
                                  "headers": {"Authorization": "Bearer $RETINUE_HOOK_TOKEN"},
                                  "allowedEnvVars": ["RETINUE_HOOK_TOKEN"]}


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
        "settings: события не из PreToolUse, PermissionRequest, PostToolUse, Stop"
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
