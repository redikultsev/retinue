"""A turn's end and its loops, seen from the gate's hooks: a turn that says «done» over files changed after their last
passing check is sent on once (`Checks`), and a turn that makes the same call and gets the same outcome again and
again is warned, then denied, then cut (`Loops`). Both read only what the CLI's hooks carry — the tool, its input, its
cwd, its result or its error — and both are memory only: a restart forgets them, and the turn goes on as if they were
not there. A turn's end is the Stop that passes, or any result but a deferral (`Gate.turn_over`): the owner's «Stop»,
an error, a cut, a lane that went away.

`Checks`: an Edit, Write, MultiEdit or NotebookEdit that ran, on a file in one of the trees the platform knows — the
Base (`/work/<base.tree>`: any file, its check `scripts/lint.py` or `just check`) and the owner's repositories
(`/work/<code.root>/<name>`: any file but prose, its check any test, lint, type or build command) — stays pending for
its tree until a check of that tree exits 0. A check is a command one of whose parts (between `&&`, `;`, `|`) starts
with one, past env assignments and runners (`uv run`, `npx`, `python -m`…); it is for the tree its cwd is in, any
whose folder its command names, or else every pending one. A Stop with something pending is blocked once a turn, with
a reason the platform writes; the next Stop passes and forgets it. A command that writes files (`sed -i`, a generator)
is not seen. A deferred call ends a turn with no Stop (probed with 2.1.304), so a turn that waits for the owner is
never sent on.

`Loops`: within a turn, per agent, the same call — a fetch's address without its fragment, any other tool's whole
input; each call id once, however often a card's resume fires it — is counted while its outcome stays the same (its
result or error, numbers aside): an outcome that changes, or an edit by the same agent, starts the count again; a
command that waits (`sleep`) is not counted. `WARN` same calls or `FAILS_WARN` same failures in a row: a warning in the
answer to its PostToolUse or PostToolUseFailure. `DENY` or `FAILS_DENY`: the call is denied with «end your turn and say
what blocks you». `CUT`: the main agent's turn is ended by `continue: false` (probed with 2.1.304: the call is not run
and no request follows); a subagent's call is only ever denied.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
PROSE = (".md", ".mdx", ".txt", ".rst")  # a repository's prose: changing it needs no check
# What may come before a check in one part of a command: `cd x`-free env assignments, and runners.
_LEAD = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+|(?:uv\s+run|uvx|npx|bunx|pnpm\s+exec|poetry\s+run|"
                   r"python3?(?:\s+-[IuqbB])*\s+-m|timeout\s+\d+[smh]?|time|env)(?:\s+-{1,2}[\w-]+(?:=\S+)?)*\s+)+")
_FLAGS = r"(?:\s+-{1,2}[\w-]+(?:=\S+)?)*"
# A part of a command that checks code: tests, lints, type checks, builds — matched at the part's start.
CODE_CHECK = re.compile(
    r"(?:pytest|tox|nox|ruff|mypy|pyright|flake8|pylint|unittest|tsc|vitest|jest|mocha|eslint|biome|playwright"
    rf"{_FLAGS}\s+test|go{_FLAGS}\s+(?:test|vet|build)|cargo{_FLAGS}\s+(?:test|check|clippy|build)|"
    rf"(?:\./)?gradlew?{_FLAGS}(?:\s+\S+)*\s+(?:test|check|build)|(?:\./)?mvnw?{_FLAGS}(?:\s+\S+)*\s+(?:test|verify)|"
    rf"(?:make|just)(?:\s+-C\s+\S+)?{_FLAGS}\s+(?:test|check|lint)|(?:pnpm|npm|yarn|bun){_FLAGS}(?:\s+run)?{_FLAGS}"
    r"\s+(?:test|typecheck|lint|check|build)|python3?(?:\s+-I)?\s+(?:\S*/)?scripts/lint\.py)(?![\w.-])")
BASE_CHECK = re.compile(r"(?:python3?(?:\s+-I)?\s+(?:\S*/)?scripts/lint\.py|just\s+check)(?![\w.-])")
_PARTS = re.compile(r"&&|\|\||;|\||\n")
SHOWN_FILES = 3
PATH_CHARS = 120

WARN, DENY, CUT = 3, 5, 8      # the same call with the same outcome, in a turn
FAILS_WARN, FAILS_DENY = 2, 3  # the same failure in a row, in a turn
WAITS = re.compile(r"(^|[;&|\s])sleep\s")  # a command that waits on purpose: polling is not a loop
KEEP_SESSIONS = 500

BLOCK = ("Not done yet: files changed after their last passing check — {what}. Run the check ({how}) and fix what "
         "fails. If it cannot run here, add a `Not checked: …` line saying what is unchecked. Then write your whole "
         "summary again as your last message: the owner reads only that one.")
HOW = {"base": "`python3 scripts/lint.py` in {root}", "code": "the repository's tests or its own check in {root}"}
WARNING = ("This same call came {n} times this turn{failed}. Repeating it will not change the outcome: change the "
           "approach, or end your turn and say what blocks you.")
DENIED = ("Not run: the same call {n} times this turn{failed}. Stop repeating it — try another way, or end your turn "
          "and say in your summary what blocks you.")
CUT_REASON = "The platform ended the turn: the session made the same call {n} times."


def checks(command: str, pattern: re.Pattern) -> bool:
    """Whether one part of the command, past its env assignments and runners, starts with a check."""
    for part in _PARTS.split(command or ""):
        part = part.strip().lstrip("(").strip()
        part = _LEAD.sub("", part, count=1)
        if pattern.match(part):
            return True
    return False


@dataclass(frozen=True)
class Tree:
    """A folder the platform checks under: the lane, its path there (`/work/base/`, `/work/code/`), and whether
    each repository is a folder under it (`code`) or it is one itself (`base`)."""
    lane: str
    root: str
    kind: str  # base | code


def _clean(text: str, limit: int = PATH_CHARS) -> str:
    """A path or a name as the platform writes it to the model: one line, no control characters, cut."""
    text = re.sub(r"[\x00-\x1f\x7f`]", "", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _path(tool_input: dict, cwd: str) -> str:
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return ""
    return posixpath.normpath(posixpath.join(cwd or "/", path))


class Checks:
    def __init__(self, trees: list[Tree] | None = None) -> None:
        self.trees = list(trees or [])
        self._pending: dict[str, dict[tuple[str, str], list[str]]] = {}  # session -> (kind, root) -> files
        self._blocked: set[str] = set()  # sessions whose turn was sent on once already
        self.ended_unchecked = False     # the last Stop let through ended a turn sent on and still unchecked

    def _scope(self, lane: str, path: str) -> tuple[str, str] | None:
        for tree in self.trees:
            root = tree.root.rstrip("/") + "/"
            if tree.lane != lane or not path.startswith(root):
                continue
            if tree.kind == "base":
                return ("base", root.rstrip("/"))
            name = path[len(root):].split("/", 1)
            if len(name) < 2 or not name[0]:
                return None
            return ("code", root + name[0]) if not path.lower().endswith(PROSE) else None
        return None

    def edited(self, session: str, lane: str, tool: str, tool_input: dict, cwd: str) -> bool:
        """A file edit that ran. True when it is one a check must follow."""
        if tool not in EDITS or not (path := _path(tool_input, cwd)) or (scope := self._scope(lane, path)) is None:
            return False
        if len(self._pending) > KEEP_SESSIONS and session not in self._pending:
            self._pending.pop(next(iter(self._pending)))
        files = self._pending.setdefault(session, {}).setdefault(scope, [])
        if path not in files:
            files.append(path)
        return True

    def ran(self, session: str, lane: str, command: str, cwd: str) -> None:
        """A command that ran and exited 0: a check of the trees it is for."""
        pending = self._pending.get(session)
        if not pending or not isinstance(command, str):
            return
        found = [s for s in pending if checks(command, BASE_CHECK if s[0] == "base" else CODE_CHECK)]
        if not found:
            return
        where = posixpath.normpath(cwd or "/")
        named = [s for s in found if where == s[1] or where.startswith(s[1] + "/")
                 or re.search(re.escape(s[1]) + r"(?![\w.-])", command)]
        for scope in named or found:
            pending.pop(scope, None)

    def stop(self, session: str, body: dict) -> str:
        """The reason a Stop is blocked, or '' to let it end the turn. Blocked once a turn: the Stop after a block —
        `stop_hook_active`, or the next one after a card's resume — passes and forgets what was pending."""
        pending = self._pending.get(session)
        if body.get("stop_hook_active") or not pending or session in self._blocked:
            # Sent on once and still unchecked: the turn ends so all the same (`episodes`, `unchecked_end`).
            self.ended_unchecked = bool(pending) and (session in self._blocked or bool(body.get("stop_hook_active")))
            self.forget(session)
            return ""
        self.ended_unchecked = False
        self._blocked.add(session)
        what, how = [], []
        for (kind, root), files in pending.items():
            names = [_clean(posixpath.relpath(f, root)) for f in files[:SHOWN_FILES]]
            more = f" (+{len(files) - SHOWN_FILES})" if len(files) > SHOWN_FILES else ""
            what.append(f"{_clean(root)}: {', '.join(names)}{more}")
            how.append(HOW[kind].format(root=_clean(root)))
        return BLOCK.format(what="; ".join(what), how="; ".join(dict.fromkeys(how)))

    def forget(self, session: str) -> None:
        """The turn is over: what it left unchecked is not the next one's."""
        self._pending.pop(session, None)
        self._blocked.discard(session)


def call_key(tool: str, tool_input: dict) -> str:
    """The same call: a fetch's address without its fragment (its prompt is reworded every time), any other tool's
    whole input."""
    if tool == "WebFetch" and isinstance(url := tool_input.get("url"), str):
        try:
            parts = urlsplit(url.strip())
            what = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
        except ValueError:
            what = url
    else:
        what = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    return tool + ":" + hashlib.sha256(what.encode()).hexdigest()[:24]


def outcome(result: object) -> str:
    """A call's outcome as compared: its result or error, numbers aside (a duration, a timestamp), cut."""
    text = result if isinstance(result, str) else json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(re.sub(r"\d+", "#", text[:20000]).encode()).hexdigest()[:16]


@dataclass
class _Turn:
    calls: dict[str, set[str]] = field(default_factory=dict)          # the same call -> the ids it came with
    outcomes: dict[str, str] = field(default_factory=dict)            # -> its last outcome
    fails: dict[str, int] = field(default_factory=dict)               # -> the same failure, in a row


class Loops:
    def __init__(self) -> None:
        self._turns: dict[str, _Turn] = {}

    def _turn(self, session: str) -> _Turn:
        if len(self._turns) > KEEP_SESSIONS and session not in self._turns:
            self._turns.pop(next(iter(self._turns)))
        return self._turns.setdefault(session, _Turn())

    def before(self, session: str, agent: str, tool: str, tool_input: dict, call_id: str = "") -> tuple[str, str]:
        """A call about to be decided: ('', '') to go on, ('deny', reason) or ('cut', reason)."""
        if tool == "Bash" and WAITS.search(str(tool_input.get("command") or "")):
            return "", ""
        turn, key = self._turn(session), agent + "|" + call_key(tool, tool_input)
        ids = turn.calls.setdefault(key, set())
        ids.add(call_id or f"#{len(ids)}")
        n, fails = len(ids), turn.fails.get(key, 0)
        if n >= CUT and not agent:
            return "cut", CUT_REASON.format(n=n)
        if n >= DENY or fails >= FAILS_DENY:
            failed = f", and it failed {fails} times in a row the same way" if fails >= FAILS_DENY else ""
            return "deny", DENIED.format(n=n, failed=failed)
        return "", ""

    def after(self, session: str, agent: str, tool: str, tool_input: dict, result: object = None,
              error: object = None, call_id: str = "") -> str:
        """A call that ran (`error` None, its `result`) or failed: the warning to add to the model's context, or ''."""
        turn, key = self._turn(session), agent + "|" + call_key(tool, tool_input)
        if error is None and tool in EDITS:
            for k in [k for k in turn.calls if k.startswith(agent + "|")]:  # this agent's progress
                turn.calls.pop(k, None)
                turn.outcomes.pop(k, None)
                turn.fails.pop(k, None)
        was = turn.outcomes.get(key)
        now = turn.outcomes[key] = ("err:" if error is not None else "ok:") + outcome(error if error is not None
                                                                                     else result)
        if was is not None and was != now:  # something changed: the count starts again from this call
            turn.calls[key] = {call_id} if call_id else set(list(turn.calls.get(key, ()))[-1:])
            turn.fails.pop(key, None)
        n = len(turn.calls.get(key, ()))
        if error is None:
            turn.fails.pop(key, None)
            return WARNING.format(n=n, failed="") if n == WARN else ""
        f = turn.fails[key] = turn.fails.get(key, 0) + 1
        if f == FAILS_WARN or n == WARN:
            return WARNING.format(n=max(n, f), failed=", failing the same way" if f >= FAILS_WARN else "")
        return ""

    def end(self, session: str) -> None:
        """The turn ended: its counts go."""
        self._turns.pop(session, None)
