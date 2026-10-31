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
SETTINGS_DIR = "/lane/run"  # a lane's tmpfs: one settings file per run (and its instructions, `<run>.md`)
SKILLS_DIR = "/lane/skills"  # the owner's skills, read-only, as a plugin (`instructions`); RETINUE_LANE_SKILLS elsewhere
OWNER_MEMORY_DIR = "/lane/owner-memory"  # the owner's memory snapshot, read-only (`instructions.owner_memory`)
HOOK_TIMEOUT_S = 15
# PermissionRequest fires when the CLI asks: in a run that fell back from auto, and in auto for a tool that needs a
# person (AskUserQuestion allowed without its answers); the gate denies every ask. PermissionDenied fires only in
# auto, when its classifier blocks a call (`gate.Gate.permission_denied`).
EVENTS = ("PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse", "Stop")

# A permission rule in the CLI's own syntax: a tool name, optionally `(specifier)`; the specifier holds no
# parenthesis and no control character, so it can neither nest nor break a line.
RULE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\([^\x00-\x1f()]{1,200}\))?")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # sessions.PlatformStore.add_run; it names the run's settings file
MARK_N = r"\d{1,4}"  # the n of a marker hook's address `<hook_url>/mark/<n>`; the platform routes the same
SETTINGS_PATH = re.compile(re.escape(SETTINGS_DIR) + "/" + RUN_ID.pattern + r"\.json")
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# `--permission-mode auto`: a call no rule and no hook decides goes to the CLI's own classifier, as on the owner's Mac;
# the gate's PreToolUse still comes first (probed with 2.1.304: hook, then rules, then the classifier). A run where auto
# is unavailable starts in manual without saying so but in its init line, and the gate falls back (docs/platform.md).
# Every run passes it, a `--resume` too: the CLI does not carry the mode over.
# `--include-partial-messages`: the text of a message while it is written, as `stream_event` lines the platform passes
# to the face and never stores. `--forward-subagent-text` (2.1.211+): a foreground subagent's own text and thinking as
# lines with its `parent_tool_use_id`, which the projection folds into the subagent's item.
# `--model` and `--effort`: every run is the owner's own setup on his Mac — the latest Opus by its full id (an alias
# names another model with another CLI release) with its million-token context, at extra-high effort. Without them a
# headless CLI takes its own default, a smaller model.
MODEL, EFFORT = "claude-opus-5-5[1m]", "xhigh"
PREFIX = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
          "--include-hook-events", "--include-partial-messages", "--forward-subagent-text", "--setting-sources", "",
          "--model", MODEL, "--effort", EFFORT,
          "--permission-mode", "auto", "--permission-prompt-tool", "stdio", "--settings")
# `--permission-prompt-tool stdio`, not `--permission-prompts none`: a headless CLI offers AskUserQuestion (and the plan
# tools, which the platform denies) only when a host can answer it (probed with 2.1.304). Its question is deferred by
# the gate and answered on resume (`allow` + `updatedInput.answers`); every other ask is denied by the gate's
# PermissionRequest hook, and one that still reaches stdio is denied by the lane host (`deny_control`) — never granted.
STDIO_TOOL = ("--permission-prompt-tool", "stdio")
# A fork (the owner's «Fork»): `--resume <parent> --fork-session` starts a new CLI session from a copy of the
# parent's conversation and leaves the parent's own transcript untouched (probed with 2.1.304, tests/test_probes.py).
# Allowed only right after `--resume <id>`: a flag alone, or anywhere else, is refused like any unknown one.
FORK_SESSION = "--fork-session"
MCP_CONFIG, MCP_STRICT = "--mcp-config", "--strict-mcp-config"
MCP_SERVER = "retinue"  # `subs.SERVER`: the platform's sub-sessions server, the only one a run may have
FORBIDDEN = {"bypassPermissions": "the bypassPermissions mode",
             "--dangerously-skip-permissions": "the flag --dangerously-skip-permissions",
             "--allow-dangerously-skip-permissions": "the flag --allow-dangerously-skip-permissions",
             "--permission-prompt-tool": "the flag --permission-prompt-tool (except the stdio prefix)",
             "--permission-prompts": "the flag --permission-prompts (it hides AskUserQuestion)"}

# Off, and so set: the scrub silently forces the manual mode (probed with 2.1.304), and unset is the same as off only
# until a default changes. What it did — keep the CLI's secrets out of the commands' environment — the managed
# settings of every lane do instead: `sandbox.credentials.envVars` denies each name in SECRET_ENV to sandboxed Bash,
# and no command runs unsandboxed there (`allowUnsandboxedCommands: false`).
SCRUB = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
# Fork subagents are on by default in `-p` since 2.1.302, and a fork's calls run beside the parent's: a turn makes
# parallel calls, of which a deferral keeps only one. Off, as the changelog of 2.1.302 says to turn them off.
FORK = "CLAUDE_CODE_FORK_SUBAGENT"
# Auto memory is read whatever --setting-sources says, from <config>/projects/<cwd>/memory, and the model is told to
# write it there — where neither the gate nor the sandbox lets it write. Off: what outlives a session is a file in
# its repository, as the repository's rules say (docs/instructions.md).
NO_MEMORY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
# Background tasks off. In `-p` 2.1.304 launches every subagent in the background by default, and a backgrounded
# subagent or `run_in_background` command outlives the turn: the CLI holds the turn's `result` until it ends, then
# runs one more turn on its notification and writes a `result` for each — a run of one turn says two or three, a
# deferred call is followed by a turn after it, and a deferral inside such a subagent is lost (probed with a fake API
# and a real one). Off, the parameter is gone from the tools' schemas and subagents run in the foreground, in
# parallel when the model asks for several at once (docs/platform.md, «Subagents»).
NO_BACKGROUND = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
# With no background, a build or a test suite runs as one foreground Bash call: 10 minutes unless the model asks for
# more, an hour at most (the CLI's own defaults are 2 and 10 minutes). A server or a watch mode never ends: the lane's
# instructions say not to start one, and the timeout ends it.
BASH_TIMEOUTS = {"BASH_DEFAULT_TIMEOUT_MS": str(10 * 60 * 1000), "BASH_MAX_TIMEOUT_MS": str(60 * 60 * 1000)}
# A run that hits the subscription's limit waits for it to reset instead of failing: the retry watchdog (2.1.186+)
# waits out a 429 to the reset time the API gives. A reset more than 5 hours away — a weekly limit — fails the request
# at once (2.1.301+), and no request waits more than 6 hours in all (2.1.295+; the CLI's own cap on one wait is 6 h).
# The watchdog also retries other transient errors longer (300 tries, about 3 hours). `…_USAGE_LIMIT_RECHECK_MS`
# (2.1.305) is not in the pinned CLI, and it makes no checks under CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC anyway.
WATCHDOG = {"CLAUDE_CODE_RETRY_WATCHDOG": "1",
            "CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS": str(5 * 3600 * 1000),
            "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS": str(6 * 3600 * 1000)}
# The subscription token reaches the CLI through a pipe, never its environment: a process's environment is readable at
# /proc/<pid>/environ for as long as it runs, a pipe once (the CLI reads it at start; probed with 2.1.304, which reads
# CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR for every request and for `auth status` alike).
TOKEN_FD = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"
ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", TOKEN_FD, "HTTPS_PROXY",
                      "NO_PROXY", "RETINUE_HOOK_RUN", SCRUB, FORK, NO_MEMORY, NO_BACKGROUND, *BASH_TIMEOUTS,
                      "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                      "DISABLE_AUTOUPDATER", "ENABLE_CLAUDEAI_MCP_SERVERS", *WATCHDOG})
# The run's hook token, by a name the scrub would keep (it drops secret-looking names, …TOKEN, from the hooks' header
# interpolation as well as from commands, and a hook without it is an unknown run). A command does not see it: the
# managed settings deny it to sandboxed Bash. Were it seen, a command could only call its own run's hooks, which decide
# nothing it could not ask for anyway: a grant is bound to the CLI's own tool_use_id and the call a resume finishes
# comes from the CLI's stream.
HOOK_TOKEN = "RETINUE_HOOK_RUN"
# Every name in the CLI's environment that holds or leads to a secret, and the two its environment must never hold
# (`check_env`), denied to sandboxed Bash by every lane's managed settings (`sandbox.credentials.envVars`, pinned by
# tests/test_deploy.py). The pipe the token descriptor names is empty once the CLI has read it; its name goes anyway.
SECRET_ENV = (HOOK_TOKEN, TOKEN_FD, "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
HOOK_FIELDS = {"type": "http", "timeout": HOOK_TIMEOUT_S, "onFailure": "block",
               "headers": {"Authorization": f"Bearer ${HOOK_TOKEN}"}, "allowedEnvVars": [HOOK_TOKEN]}
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class Refused(ValueError):
    """An argv, environment or settings file the lane will not run."""


def cli_path() -> str:
    return os.environ.get("RETINUE_LANE_CLI") or CLI_PATH


def skills_dir() -> str:
    return os.environ.get("RETINUE_LANE_SKILLS") or SKILLS_DIR


def owner_memory_dir() -> str:
    return os.environ.get("RETINUE_LANE_OWNER_MEMORY") or OWNER_MEMORY_DIR


def rule_ok(rule: object) -> bool:
    return isinstance(rule, str) and RULE.fullmatch(rule) is not None


def mcp_config(hook_url: str) -> str:
    """The run's one MCP server: the platform's sub-sessions (`subs`), beside its hooks and under the same run token,
    which the CLI puts in the header from its environment (probed with 2.1.304)."""
    return json.dumps({"mcpServers": {MCP_SERVER: {"type": "http", "url": f"{hook_url}/mcp",
                                                   "headers": {"Authorization": f"Bearer ${{{HOOK_TOKEN}}}"}}}},
                      separators=(",", ":"))

def argv(settings_path: str, allowed: Sequence[str], disallowed: Sequence[str], resume: str | None, *,
         instructions: bool = False, skills: bool = False, fork: bool = False, mcp: str | None = None) -> list[str]:
    """The whole command line of one run. The prompt never goes here: it is written to stdin (`user_line`).
    `instructions`: the repository's rules, in the run's own `<run>.md` beside its settings; `skills`: the owner's
    skills folder (both `instructions`). `fork`: the first run of a fork — `resume` is the parent's CLI session, which
    the CLI copies into a new one and leaves as it was (`FORK_SESSION`)."""
    out = [cli_path(), *PREFIX, settings_path]
    if allowed:
        out += ["--allowedTools", *allowed]
    if disallowed:
        out += ["--disallowedTools", *disallowed]
    if fork and not resume:
        raise Refused("argv: a fork without a session to fork from")
    if resume:
        out += ["--resume", resume, *((FORK_SESSION,) if fork else ())]
    if instructions:
        out += ["--append-system-prompt-file", instructions_path(settings_path)]
    if skills:
        out += ["--plugin-dir", skills_dir()]
    if mcp:  # the lane's own hook address: never one the platform sent
        out += [MCP_CONFIG, mcp_config(mcp), MCP_STRICT]
    return out


def instructions_path(settings_path: str) -> str:
    return settings_path.removesuffix(".json") + ".md"


def check_argv(args: Sequence[str], hook_url: str = "") -> list[str]:
    """`hook_url`: the lane's own — the only MCP server a run may name is the platform's at it (`mcp_config`)."""
    args = list(args)
    for i, token in enumerate(args):
        if token == STDIO_TOOL[0] and args[i + 1:i + 2] == [STDIO_TOOL[1]]:
            continue  # the prefix's own: an ask comes to the lane host, which denies it; twice is an unknown flag
        if (name := token.split("=", 1)[0]) in FORBIDDEN:
            raise Refused(f"argv: forbidden — {FORBIDDEN[name]}")
    prefix = [cli_path(), *PREFIX]
    if args[:len(prefix)] != prefix:
        raise Refused("argv: not the fixed prefix")
    rest = args[len(prefix):]
    if not rest or not SETTINGS_PATH.fullmatch(rest[0]):
        raise Refused(f"argv: the settings file is not in {SETTINGS_DIR}/*.json")
    seen: set[str] = set()
    i = 1
    while i < len(rest):
        flag = rest[i]
        if flag in seen:
            raise Refused(f"argv: {flag} twice")
        seen.add(flag)
        if flag in ("--allowedTools", "--disallowedTools"):
            j = i + 1
            while j < len(rest) and not rest[j].startswith("-"):
                if not rule_ok(rest[j]):
                    raise Refused(f"argv: not a rule — {rest[j]!r}")
                j += 1
            if j == i + 1:
                raise Refused(f"argv: {flag} without rules")
            i = j
        elif flag == "--resume":
            if i + 1 >= len(rest) or not UUID4.fullmatch(rest[i + 1]):
                raise Refused("argv: --resume without a session id")
            i += 2
            if rest[i:i + 1] == [FORK_SESSION]:  # only right after the session it copies: nowhere else
                seen.add(FORK_SESSION)
                i += 1
        elif flag == MCP_CONFIG:
            if not hook_url or rest[i + 1:i + 3] != [mcp_config(hook_url), MCP_STRICT]:
                raise Refused(f"argv: {MCP_CONFIG} is not the platform's server with {MCP_STRICT}")
            seen.add(MCP_STRICT)
            i += 3
        elif flag in ("--append-system-prompt-file", "--plugin-dir"):
            want = instructions_path(rest[0]) if flag == "--append-system-prompt-file" else skills_dir()
            if i + 1 >= len(rest) or rest[i + 1] != want:
                raise Refused(f"argv: {flag} is not {want}")
            i += 2
        elif flag.startswith("-"):
            raise Refused(f"argv: unknown flag {flag!r}")
        else:
            raise Refused(f"argv: positional argument {flag!r}")
    return args


def env(home: str, config_dir: str, proxy: str, no_proxy: str, hook_token: str, token_fd: int) -> dict[str, str]:
    """The CLI's whole environment, built from scratch: nothing of the lane host's own is inherited. The token is not
    in it: `token_fd` is the read end of a pipe holding it (`lane_host.token_pipe`)."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "CLAUDE_CONFIG_DIR": config_dir,
            TOKEN_FD: str(token_fd), "HTTPS_PROXY": proxy, "NO_PROXY": no_proxy,
            HOOK_TOKEN: hook_token, SCRUB: "0", FORK: "0", NO_MEMORY: "1", NO_BACKGROUND: "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false", **WATCHDOG, **BASH_TIMEOUTS}


def check_env(data: dict[str, str]) -> dict[str, str]:
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
        if key in data:
            raise Refused(f"env: {key} in the lane's environment")
    if not re.fullmatch(r"[0-9]{1,5}", str(data.get(TOKEN_FD, ""))):
        raise Refused(f"env: without {TOKEN_FD}")
    if unknown := sorted(set(data) - ENV_KEYS):
        raise Refused(f"env: extra variables {', '.join(unknown)}")
    if data.get(SCRUB) != "0":  # "1" forces manual; unset is "0" only by today's default
        raise Refused(f"env: without {SCRUB}=0")
    if data.get(FORK) != "0":
        raise Refused(f"env: without {FORK}=0")
    if data.get(NO_MEMORY) != "1":
        raise Refused(f"env: without {NO_MEMORY}=1")
    if data.get(NO_BACKGROUND) != "1":
        raise Refused(f"env: without {NO_BACKGROUND}=1")
    if not all(isinstance(v, str) for v in data.values()):
        raise Refused("env: a value is not a string")
    for key in (*WATCHDOG, *BASH_TIMEOUTS):
        if key in data and not re.fullmatch(r"[1-9][0-9]{0,9}", data[key]):
            raise Refused(f"env: {key} is not an integer")
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
        raise Refused("settings: the top level is only hooks and cleanupPeriodDays")
    days = data["cleanupPeriodDays"]
    if type(days) is not int or days < 1:
        raise Refused("settings: cleanupPeriodDays is not a whole number of days")
    hooks = data["hooks"]
    if not isinstance(hooks, dict) or not set(hooks) <= set(EVENTS):
        raise Refused("settings: events not among " + ", ".join(EVENTS))
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise Refused(f"settings: {event} is not a list")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"matcher", "hooks"} or \
                    not isinstance(entry["hooks"], list) or not isinstance(entry["matcher"], str) or \
                    not (entry["matcher"] == "*" or TOOL_NAME.fullmatch(entry["matcher"])):
                raise Refused(f"settings: {event} — not the expected entry")
            for hook in entry["hooks"]:
                _check_hook(event, hook, hook_url)
    return data


def _check_hook(event: str, hook: object, hook_url: str) -> None:
    if not isinstance(hook, dict) or not set(HOOK_FIELDS) | {"url"} <= set(hook) <= set(HOOK_FIELDS) | {"url", "if"}:
        raise Refused(f"settings: {event} — not the expected hook fields")
    if any(hook[k] != v for k, v in HOOK_FIELDS.items()):
        raise Refused(f"settings: {event} — the hook is not http, does not block on failure or has the wrong header")
    url = hook["url"]
    mark = event == "PreToolUse" and isinstance(url, str) and \
        re.fullmatch(re.escape(hook_url) + rf"/mark/{MARK_N}", url)
    if url != f"{hook_url}/{event}" and not mark:
        raise Refused(f"settings: {event} — a foreign address {url!r}")
    if "if" in hook and not rule_ok(hook["if"]):
        raise Refused(f"settings: {event} — if is not a rule")


def user_line(content: str | list[dict]) -> bytes:
    """One owner message as a stream-json input line: text, or content blocks (`check_content`) — the pinned CLI
    passes `image` and `document` blocks to the model as they are (`tests/test_inbox_probe.py`)."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                       ensure_ascii=False, separators=(",", ":"))
            + "\n").encode()

# --- what the owner sends besides text: content blocks and files for the session's inbox ----------------------------

BLOCK_MEDIA = {"image": ("image/jpeg",), "document": ("application/pdf",)}  # the platform re-encodes every picture
CONTENT_BLOCKS = 24              # an album of ten and its texts, with room
CONTENT_B64 = 24 * 1024 * 1024   # base64 characters of every block together: a dozen pictures and a 5 MB PDF
INBOX_FILES = 10
INBOX_BYTES = 30 * 1024 * 1024   # the files of one message together, decoded
INBOX_DIR = "inbox"              # under the run's directory: `inbox/<date>-<name>`
UNSHOWN = r"\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069"  # control characters and the bidi overrides
INBOX_NAME = re.compile(f"[^{UNSHOWN}/\\\\.][^{UNSHOWN}/\\\\]{{0,119}}")
INBOX_NAME_BYTES = 160           # with the date and a «-NN» it stays far below a file system's 255 bytes
INBOX_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TITLE = re.compile(f"[^{UNSHOWN}]{{1,200}}")

def check_content(content: object) -> str | list[dict]:
    """A prompt the lane will write to the CLI: text, or a list of `text`, `image` (JPEG) and `document` (PDF)
    blocks, each base64 and bounded. Anything else is refused: a block the API rejects would sit in the session's
    history and break every later turn."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not 0 < len(content) <= CONTENT_BLOCKS:
        raise Refused("prompt: neither text nor a list of blocks")
    size = 0
    for block in content:
        if not isinstance(block, dict):
            raise Refused("prompt: a block is not an object")
        kind = block.get("type")
        if kind == "text":
            if set(block) != {"type", "text"} or not isinstance(block["text"], str) or not block["text"]:
                raise Refused("prompt: a text block is not as expected")
            continue
        if kind not in BLOCK_MEDIA or not set(block) <= {"type", "source", "title"} or \
                ("title" in block and not (isinstance(block["title"], str) and TITLE.fullmatch(block["title"]))):
            raise Refused(f"prompt: a block {kind!r} is not as expected")
        source = block.get("source")
        if not isinstance(source, dict) or set(source) != {"type", "media_type", "data"} or \
                source["type"] != "base64" or source["media_type"] not in BLOCK_MEDIA[kind] or \
                not isinstance(source["data"], str) or not source["data"] or len(source["data"]) % 4 or \
                not BASE64.fullmatch(source["data"]):
            raise Refused(f"prompt: the source of the {kind} block is not base64 of an accepted type")
        size += len(source["data"])
    if size > CONTENT_B64:
        raise Refused(f"prompt: attachments over {CONTENT_B64 // 2**20} MB in base64")
    return content

def check_files(files: object, date: object) -> list[dict]:
    """Files for the session's inbox: `[{name, data (base64), sha256}]`, a plain name each (no path, no leading dot),
    the digest of the bytes as given, and the date of the message that brought them."""
    if not isinstance(files, list) or not 0 < len(files) <= INBOX_FILES:
        raise Refused(f"files: not a list of up to {INBOX_FILES} files")
    if not isinstance(date, str) or not INBOX_DATE.fullmatch(date):
        raise Refused("files: without a YYYY-MM-DD date")
    size = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"name", "data", "sha256"} or \
                not isinstance(item["name"], str) or not INBOX_NAME.fullmatch(item["name"]) or \
                len(item["name"].encode()) > INBOX_NAME_BYTES or \
                not isinstance(item["data"], str) or len(item["data"]) % 4 or not BASE64.fullmatch(item["data"]) or \
                not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
            raise Refused("files: a file of the wrong shape")
        size += len(item["data"]) // 4 * 3
    if size > INBOX_BYTES + 2 * INBOX_FILES:
        raise Refused(f"files: over {INBOX_BYTES // 2**20} MB")
    return files


def deny_control(line: str) -> bytes | None:
    """The lane host's answer to a `control_request` the CLI writes to stdout (`--permission-prompt-tool stdio`):
    a permission ask denied, anything else refused. None for any other line. The gate's PermissionRequest hook denies
    an ask first; this is what holds when it did not decide, so a run never waits on a host that will not answer."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "control_request":
        return None
    request_id = str(data.get("request_id") or "")
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    if request.get("subtype") == "can_use_tool":
        response = {"subtype": "success", "request_id": request_id,
                    "response": {"behavior": "deny", "message": "This call asks for permission, and nobody "
                                                                "here can answer it — it was not run."}}
    else:
        response = {"subtype": "error", "request_id": request_id, "error": "the lane host answers no such request"}
    return (json.dumps({"type": "control_response", "response": response}, ensure_ascii=False) + "\n").encode()

def version_ok(output: str) -> bool:
    """`claude --version` says the pinned version."""
    return output.strip() == f"{CLI_VERSION} (Claude Code)"
