"""The lanes' engine: the unmodified, pinned `claude -p` and the only argv, environment and settings it is run with.
Standard library only — a lane host imports it to build each run and to check it again before it spawns anything.

What is built here is checked here too, strictly: an argv is the fixed prefix plus the few options a run may vary,
an environment is built from scratch, a settings file holds only HTTP hooks back to the platform. Anything else is
refused, never repaired. The CLI facts this rests on are pinned by `tests/test_probes.py` against `CLI_VERSION`.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Sequence

CLI_VERSION = "2.1.304"  # the lanes' pin, separate from the SDK's; deploy/lanes/cli.lock carries the same version
CLI_PATH = "/opt/claude/claude"  # where the lane image installs it; RETINUE_LANE_CLI points elsewhere
SETTINGS_DIR = "/lane/run"  # a lane's tmpfs: one settings file per run
HOOK_TIMEOUT_S = 15
EVENTS = ("PreToolUse", "PermissionRequest", "PostToolUse", "Stop")

# A permission rule in the CLI's own syntax: a tool name, optionally `(specifier)`; the specifier holds no
# parenthesis and no control character, so it can neither nest nor break a line.
RULE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\([^\x00-\x1f()]{1,200}\))?")
SETTINGS_PATH = re.compile(re.escape(SETTINGS_DIR) + r"/[A-Za-z0-9_-]{1,64}\.json")
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
PREFIX = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
          "--include-hook-events", "--setting-sources", "", "--permission-mode", "manual",
          "--permission-prompts", "none", "--settings")
FORBIDDEN = {"bypassPermissions": "режим bypassPermissions", "auto": "режим auto",
             "--dangerously-skip-permissions": "флаг --dangerously-skip-permissions",
             "--allow-dangerously-skip-permissions": "флаг --allow-dangerously-skip-permissions",
             "--permission-prompt-tool": "флаг --permission-prompt-tool"}

SCRUB = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY",
                      "NO_PROXY", "RETINUE_HOOK_TOKEN", SCRUB, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                      "DISABLE_AUTOUPDATER", "ENABLE_CLAUDEAI_MCP_SERVERS"})
HOOK_TOKEN = "RETINUE_HOOK_TOKEN"
HOOK_FIELDS = {"type": "http", "timeout": HOOK_TIMEOUT_S, "onFailure": "block",
               "headers": {"Authorization": f"Bearer ${HOOK_TOKEN}"}, "allowedEnvVars": [HOOK_TOKEN]}
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class Refused(ValueError):
    """An argv, environment or settings file the lane will not run."""


def cli_path() -> str:
    return os.environ.get("RETINUE_LANE_CLI") or CLI_PATH


def rule_ok(rule: object) -> bool:
    return isinstance(rule, str) and RULE.fullmatch(rule) is not None


def argv(settings_path: str, allowed: Sequence[str], disallowed: Sequence[str], resume: str | None) -> list[str]:
    """The whole command line of one run. The prompt never goes here: it is written to stdin (`user_line`)."""
    out = [cli_path(), *PREFIX, settings_path]
    if allowed:
        out += ["--allowedTools", *allowed]
    if disallowed:
        out += ["--disallowedTools", *disallowed]
    if resume:
        out += ["--resume", resume]
    return out


def check_argv(args: Sequence[str]) -> list[str]:
    args = list(args)
    for token in args:
        if (name := token.split("=", 1)[0]) in FORBIDDEN:
            raise Refused(f"argv: запрещено — {FORBIDDEN[name]}")
    prefix = [cli_path(), *PREFIX]
    if args[:len(prefix)] != prefix:
        raise Refused("argv: не тот фиксированный префикс")
    rest = args[len(prefix):]
    if not rest or not SETTINGS_PATH.fullmatch(rest[0]):
        raise Refused(f"argv: файл настроек не в {SETTINGS_DIR}/*.json")
    seen: set[str] = set()
    i = 1
    while i < len(rest):
        flag = rest[i]
        if flag in seen:
            raise Refused(f"argv: {flag} дважды")
        seen.add(flag)
        if flag in ("--allowedTools", "--disallowedTools"):
            j = i + 1
            while j < len(rest) and not rest[j].startswith("-"):
                if not rule_ok(rest[j]):
                    raise Refused(f"argv: не правило — {rest[j]!r}")
                j += 1
            if j == i + 1:
                raise Refused(f"argv: {flag} без правил")
            i = j
        elif flag == "--resume":
            if i + 1 >= len(rest) or not UUID4.fullmatch(rest[i + 1]):
                raise Refused("argv: --resume без id сессии")
            i += 2
        elif flag.startswith("-"):
            raise Refused(f"argv: неизвестный флаг {flag!r}")
        else:
            raise Refused(f"argv: позиционный аргумент {flag!r}")
    return args


def env(oauth_token: str, home: str, config_dir: str, proxy: str, no_proxy: str, hook_token: str) -> dict[str, str]:
    """The CLI's whole environment, built from scratch: nothing of the lane host's own is inherited."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "CLAUDE_CONFIG_DIR": config_dir,
            "CLAUDE_CODE_OAUTH_TOKEN": oauth_token, "HTTPS_PROXY": proxy, "NO_PROXY": no_proxy,
            HOOK_TOKEN: hook_token, SCRUB: "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def check_env(data: dict[str, str]) -> dict[str, str]:
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        if key in data:
            raise Refused(f"env: {key} в среде линии")
    if unknown := sorted(set(data) - ENV_KEYS):
        raise Refused(f"env: лишние переменные {', '.join(unknown)}")
    if data.get(SCRUB) != "1":
        raise Refused(f"env: без {SCRUB}=1")
    if not all(isinstance(v, str) for v in data.values()):
        raise Refused("env: значение не строка")
    return data


def _hook(url: str, rule: str | None = None) -> dict:
    hook = {**copy.deepcopy(HOOK_FIELDS), "url": url}
    if rule is not None:
        hook["if"] = rule
    return hook


def settings(hook_url: str, cleanup_days: int, marks: Sequence[str]) -> dict:
    """The run's `--settings`: one general hook per event, and before them in PreToolUse one marker hook per rule
    (`<hook_url>/mark/<n>`) that the CLI fires only when its own matcher says the call falls under rule n."""
    def mark(n: int, rule: str) -> dict:
        name, specifier = rule.partition("(")[::2]
        return {"matcher": name, "hooks": [_hook(f"{hook_url}/mark/{n}", rule if specifier else None)]}

    hooks = {event: [{"matcher": "*", "hooks": [_hook(f"{hook_url}/{event}")]}] for event in EVENTS}
    hooks["PreToolUse"] = [*(mark(n, rule) for n, rule in enumerate(marks)), *hooks["PreToolUse"]]
    return {"hooks": hooks, "cleanupPeriodDays": cleanup_days}


def check_settings(data: object, hook_url: str) -> dict:
    if not isinstance(data, dict) or set(data) != {"hooks", "cleanupPeriodDays"}:
        raise Refused("settings: верхний уровень — только hooks и cleanupPeriodDays")
    days = data["cleanupPeriodDays"]
    if type(days) is not int or days < 1:
        raise Refused("settings: cleanupPeriodDays не целое число дней")
    hooks = data["hooks"]
    if not isinstance(hooks, dict) or not set(hooks) <= set(EVENTS):
        raise Refused("settings: события не из " + ", ".join(EVENTS))
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise Refused(f"settings: {event} не список")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"matcher", "hooks"} or \
                    not isinstance(entry["hooks"], list) or not isinstance(entry["matcher"], str) or \
                    not (entry["matcher"] == "*" or TOOL_NAME.fullmatch(entry["matcher"])):
                raise Refused(f"settings: {event} — не та запись")
            for hook in entry["hooks"]:
                _check_hook(event, hook, hook_url)
    return data


def _check_hook(event: str, hook: object, hook_url: str) -> None:
    if not isinstance(hook, dict) or not set(HOOK_FIELDS) | {"url"} <= set(hook) <= set(HOOK_FIELDS) | {"url", "if"}:
        raise Refused(f"settings: {event} — не те поля хука")
    if any(hook[k] != v for k, v in HOOK_FIELDS.items()):
        raise Refused(f"settings: {event} — хук не http, не блокирует при сбое или не тот заголовок")
    url = hook["url"]
    mark = event == "PreToolUse" and isinstance(url, str) and re.fullmatch(re.escape(hook_url) + r"/mark/\d{1,4}", url)
    if url != f"{hook_url}/{event}" and not mark:
        raise Refused(f"settings: {event} — чужой адрес {url!r}")
    if "if" in hook and not rule_ok(hook["if"]):
        raise Refused(f"settings: {event} — if не правило")


def user_line(text: str) -> bytes:
    """One owner message as a stream-json input line."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}},
                       ensure_ascii=False, separators=(",", ":"))
            + "\n").encode()


def version_ok(output: str) -> bool:
    """`claude --version` says the pinned version."""
    return output.strip() == f"{CLI_VERSION} (Claude Code)"
