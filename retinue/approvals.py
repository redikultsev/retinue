"""What a session may do without asking, what it may never do, what always comes to the owner, and his cards.

The CLI matches the rules itself: every always rule, card rule and never-list entry becomes a marker hook of the run's
settings (`cliengine.settings`), and its mark tells the platform's gate whether the CLI's own rule decides the call,
the owner does, or nobody may. A call no rule names is the CLI's auto classifier's to decide. An approval is bound to
a digest of the call — the tool, its input, the directory and the hash of the file it would change — so a grant runs
only the call the owner saw, on the file as it was.

The never list is what leaves the system: a push, a pull request, a release, a publish, a deploy, a connection to
another machine, a letter. Such a call is never granted; the owner gets it as a courier card and runs it himself.
Each entry is also in `--disallowedTools`, so even a mistaken grant cannot run it.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import unicodedata
import urllib.parse
from dataclasses import dataclass

from . import cliengine, courier, instructions, transcript
from .config import LaneRef, Track

NEVER: list[tuple[str, str]] = [
    ("Bash(git push:*)", "push commits (git push)"),
    ("Bash(gh pr create:*)", "open a pull request (gh pr create)"),
    ("Bash(gh pr merge:*)", "merge a pull request (gh pr merge)"),
    ("Bash(gh release create:*)", "publish a release (gh release create)"),
    ("Bash(npm publish:*)", "publish an npm package (npm publish)"),
    ("Bash(pnpm publish:*)", "publish an npm package (pnpm publish)"),
    ("Bash(yarn publish:*)", "publish an npm package (yarn publish)"),
    ("Bash(uv publish:*)", "publish a PyPI package (uv publish)"),
    ("Bash(twine upload:*)", "publish a PyPI package (twine upload)"),
    ("Bash(cargo publish:*)", "publish a crate (cargo publish)"),
    ("Bash(docker push:*)", "push an image (docker push)"),
    ("Bash(kubectl apply:*)", "deploy to a cluster (kubectl apply)"),
    ("Bash(ssh:*)", "log in to another machine (ssh)"),
    ("Bash(scp:*)", "copy to another machine (scp)"),
    ("Bash(sftp:*)", "copy to another machine (sftp)"),
    ("Bash(rsync:*)", "sync with another machine (rsync)"),
    ("Bash(sendmail:*)", "send an email (sendmail)"),
    ("Bash(mail:*)", "send an email (mail)"),
    ("Bash(mailx:*)", "send an email (mailx)"),
]
# The never list read the gate's own way too, for what the CLI's matcher misses: `git -C . push`, `env git push`,
# `sh -c 'git push'`, `/usr/bin/ssh`. Each entry's words, in order, anywhere in one command of the text (no `;`, `&`,
# `|` or newline between them), quotes and backslashes taken out first. Built from NEVER itself, so the two never
# drift. A word is whole: not part of a name or a path's last part (`test_mail`, `retinue/mail.py`, `~/.ssh`), though a
# path may lead to it (`/usr/bin/git`). Wide on purpose: a hit with no never mark is a card, not a deny, and `grep
# push` coming to the owner costs one press.
_EDGE_BEFORE, _EDGE_AFTER = r"(?<![\w.-])", r"(?![\w./-])"


def _never_words(rule: str) -> list[str]:
    found = re.fullmatch(r"Bash\(([a-z][a-z ]*):\*\)", rule)
    if not found:
        raise ValueError(f"a never rule the gate cannot read as words: {rule}")
    return found.group(1).split()


NEVER_TEXT = [(re.compile(r"[^;&|\n]*".join(_EDGE_BEFORE + re.escape(w) + _EDGE_AFTER for w in _never_words(rule))),
               label) for rule, label in NEVER]
COMMAND_TOOLS = ("Bash", "Monitor")  # the tools whose `command` is a shell command


def never_in(command: str) -> str:
    """The label of the first never-list action the command's text names anywhere; empty when it names none."""
    text = re.sub(r"[\"'\\]", "", command)
    return next((label for pattern, label in NEVER_TEXT if pattern.search(text)), "")


def never_why(label: str) -> str:
    """The card's line for a command whose text names a never-list action no never rule caught."""
    return f"Looks like an action only you take: {label} — check the whole command"


# Always a card, on every lane and track, whatever the classifier would say. Empty: what leaves the system is the never
# list, and the classifier already stops the rest of what the owner would want to see; the commit to the Base that he
# wants as a card gets its own button in a later part, and a track names its own (`card:` in platform.yaml).
BASE_CARD: list[str] = []
# `Skill` loads a skill's text and runs nothing (inline shell is off in the managed settings; docs/instructions.md).
BASE_ALLOW = ["Read", "Grep", "Glob", "TodoWrite", "Skill"]
# The plan tools came with the stdio prompt tool (`cliengine.PREFIX`), not wanted: a plan's approval is a card of its own,
# not built yet, and EnterPlanMode would take a run out of auto.
BASE_DENY = ["CronCreate", "CronDelete", "ScheduleWakeup", "RemoteTrigger", "EnterPlanMode", "ExitPlanMode"]
# The web tools: denied on every lane but one whose proxy is open to the public web (`LaneRef.web`: the room,
# docs/platform.md). Its managed settings are the lock; this is the second one. There, the gate's exfil guard cards a
# call that looks like it carries data (`exfil`).
WEB = ["WebSearch", "WebFetch"]

WORK = "/work"  # the lane's own view of its work volume; the platform reads it at `LaneRef.work_mount`
OPEN_LIMIT = 4096  # one Telegram message: a longer card offers only «Open»
APPROVE, REJECT, OPEN = "approve", "reject", "open"


@dataclass(frozen=True)
class Mark:
    kind: str        # allow | card | deny | never
    rule: str        # in the CLI's syntax: what its marker hook's `if` matches
    label: str = ""  # never: what the courier card says the session wants


# A side session answers a question about another session; it changes nothing — no file, no command. These are deny
# rules, in --disallowedTools, which hold in auto as in any mode: the classifier never sees such a call.
# AskUserQuestion too: a side session answers from what it reads and never waits on a question card.
READ_ONLY_DENY = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "EnterWorktree", "ExitWorktree", "Workflow",
                  "AskUserQuestion"]


def marks_for(track: Track, lane: LaneRef, read_only: bool = False) -> list[Mark]:
    """Every rule of a run, in marker order (`/hook/mark/<n>` is the n-th). A rule named twice keeps its strictest
    kind: never, then deny, then card, then allow. `read_only` (a side session) denies every tool that changes
    something; the CLI's deny beats any allow rule of the track."""
    marks = [Mark("never", rule, label) for rule, label in NEVER]
    marks += [Mark("deny", rule) for rule in [*BASE_DENY, *([] if lane.web else WEB),
                                              *(READ_ONLY_DENY if read_only else []), *lane.deny, *track.deny]]
    marks += [Mark("card", rule) for rule in [*BASE_CARD, *lane.card, *track.card]]
    marks += [Mark("allow", rule) for rule in [*BASE_ALLOW, *track.allow]]
    seen: set[str] = set()
    return [m for m in marks if not (m.rule in seen or seen.add(m.rule))]


class Unbindable(Exception):
    """The file a call would change is one the platform cannot read as the lane sees it, so no digest can hold it
    as it is and no grant may be given for the call."""


def _lane_relative(path: str) -> list[str] | None:
    """A lane path's components under /work, none for /work itself; None for a path that is not in /work."""
    relative = os.path.relpath(os.path.normpath(path), WORK)
    if relative == os.curdir:
        return []
    return None if relative.split(os.sep)[0] == os.pardir else relative.split(os.sep)


def _in_mount(parts: list[str], root: str) -> str:
    """The platform's path of a file in the lane's /work, its links followed as the lane follows them: an absolute
    target is a lane path, read back through the mount, and its `..` walks back over the links it has followed."""
    todo, done, links = list(reversed(parts)), [], 0
    while todo:
        part = todo.pop()
        if part in ("", os.curdir):
            continue
        if part == os.pardir:
            if not done:
                raise Unbindable("a link leads out of /work")
            done.pop()
            continue
        here = os.path.join(root, *done, part)
        try:  # not `islink`: it says False for a path it cannot stat, and a lane's 0700 folder would hide a link
            is_link = stat.S_ISLNK(os.lstat(here).st_mode)
        except FileNotFoundError:
            is_link = False  # not there yet (a Write creates it): nothing to follow
        except OSError:
            raise Unbindable("the platform cannot see this path") from None
        if not is_link:
            done.append(part)
            continue
        links += 1
        if links > 40:  # as the kernel's own limit: a loop of links
            raise Unbindable("too many links")
        try:
            link = os.readlink(here)
        except OSError:  # swapped between the two looks
            raise Unbindable("the link changed under the check") from None
        if os.path.isabs(link):
            if link != WORK and not link.startswith(WORK + "/"):
                raise Unbindable("a link leads out of /work")
            done, followed = [], link[len(WORK):].split("/")
        else:
            followed = link.split("/")
        todo += reversed(followed)
    return os.path.join(root, *done)


def file_hash(tool: str, tool_input: dict, work_mount: str) -> str:
    """SHA-256 of the file the call would change, read through the platform's mount of the lane's /work: `absent`
    when there is none yet, `outside` for a path not in /work (the CLI asks for such a call, and an ask is denied);
    empty for a tool that changes no file. Raises `Unbindable` for a file the platform cannot read: a link out of
    /work, or one it may not open."""
    key = transcript.EDITS.get(tool)
    if key is None:
        return ""
    path = tool_input.get(key)
    parts = _lane_relative(path) if isinstance(path, str) and os.path.isabs(path) else None
    if not parts:
        return "outside"
    try:
        with open(_in_mount(parts, os.path.realpath(work_mount)), "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        raise Unbindable(str(e)) from e


def digest(tool: str, tool_input: dict, cwd: str, file_hash: str) -> str:
    """What the owner's buttons are bound to: the call, where it runs and the file as it is now."""
    return courier.sha256_of({"tool": tool, "input": tool_input, "cwd": cwd, "file_hash": file_hash})


# --- what no call may carry or touch, on any lane ------------------------------------------------------------------

# The gate's own refusals, before any rule or card, on every lane. A call whose input has the shape of an Anthropic
# token — an OAuth token (sk-ant-oat01-…), its refresh token (sk-ant-ort01-…), an API key (sk-ant-api03-…), an admin
# key — goes nowhere: not in an address, a search, a command or a file. And a file tool works inside /work only, as
# the lane sees it, links followed through the platform's own mount: a path that leads out — by itself, by `..`, by
# `~`, or by a link a repository brought along (`README -> /proc/1/environ`) — is refused. The one exception is the
# CLI's own spill of a tool result too large for the conversation, which it tells the model to Read (probed with
# 2.1.304: <config>/projects/<project>/<session>/tool-results/<id>.txt): Read and Grep may go there, nothing writes
# there. A shape check is a speed bump against a token written out whole, not against one the model encodes; the
# lock is that no lane holds the token but in a pipe to its CLI (`lane_host.token_pipe`; the platform hands it over).
TOKEN_SHAPE = re.compile(r"sk-ant-[a-z]{2,8}[0-9]{0,4}-[A-Za-z0-9_-]{16,}", re.IGNORECASE)
SECRET = ("Denied: the call's input holds what looks like an Anthropic token (sk-ant-…). That goes nowhere — not into "
          "an address, a search, a command or a file.")
OUTSIDE = ("Denied: file tools work only inside /work, and this path leads outside — by itself or through a link. "
           "Outside are the CLI's environment, its settings and the lane's records; keep your files in the work folder.")
HOME = "/lane/config"  # the CLI's HOME in every lane: `~` in a path is here
SPILL = re.compile(r"/lane/config/claude/projects/[^/]+/[^/]+/tool-results/[^/]+")
SPILL_TOOLS = {"Read", "Grep"}
# The owner's skills, read-only in the lanes that have them: a skill tells the model to read its references and
# scripts there. Reading only, and only while the folder passes the lane host's own check — the platform reads it
# through its own read-only mount (`PlatformConfig.skills_mount`) — and the path, its links followed there, stays
# inside it.
SKILLS = cliengine.SKILLS_DIR
SKILLS_TOOLS = {"Read", "Grep", "Glob"}
# The owner's memory snapshot, read-only in lane-private, the same way: read only while it passes the lane host's own
# check (`instructions.usable_memory`), through the platform's mount (`PlatformConfig.owner_memory_mount`).
OWNER_MEMORY = cliengine.OWNER_MEMORY_DIR
PATHS = {"Read": ("file_path",), "Edit": ("file_path",), "MultiEdit": ("file_path",), "Write": ("file_path",),
         "NotebookEdit": ("notebook_path",), "Grep": ("path",), "Glob": ("path",), "LS": ("path",)}
GLOB_CHARS = re.compile(r"[*?\[{]")
BRACES = re.compile(r"\{([^{}]*)\}")  # an innermost `{a,b}`
BRACE_LIMIT = 64


def _absolute(path: str, cwd: str) -> str:
    """A tool's path as the CLI resolves it, by text: `~` is the lane's HOME, a relative path is in the call's
    directory, `.`, `..` and repeated slashes are folded (`//proc` is `/proc`)."""
    if path == "~" or path.startswith("~/"):
        path = HOME + path[1:]
    elif not path.startswith("/"):
        path = posixpath.join(cwd or WORK, path)
    return "/" + posixpath.normpath(path).lstrip("/")


def _in_owners(path: str, lane_root: str, mount: str, usable) -> bool:
    """A path under one of the owner's read-only folders, its links followed through the platform's mount of it, still
    inside it — and the folder passing the lane host's own check."""
    if not mount or usable(mount) is None:
        return False
    root = os.path.realpath(mount)
    target = os.path.realpath(os.path.join(root, os.path.relpath(path, lane_root)))
    return target == root or target.startswith(root + os.sep)


def _confined(path: str, tool: str, work_mount: str, skills_mount: str = "", memory_mount: str = "") -> bool:
    """Inside /work as the lane sees it, every link on the way followed (`_in_mount`), or the CLI's spill, the
    owner's skills or his memory for a tool that only reads."""
    if tool in SPILL_TOOLS and SPILL.fullmatch(path):
        return True
    if path == SKILLS or path.startswith(SKILLS + "/"):
        return tool in SKILLS_TOOLS and _in_owners(path, SKILLS, skills_mount, instructions.usable_skills)
    if path == OWNER_MEMORY or path.startswith(OWNER_MEMORY + "/"):
        return tool in SKILLS_TOOLS and _in_owners(path, OWNER_MEMORY, memory_mount, instructions.usable_memory)
    parts = _lane_relative(path)
    if parts is None:
        return False
    try:
        _in_mount(parts, os.path.realpath(work_mount))
    except Unbindable:
        return False
    return True


def _braces(pattern: str) -> list[str]:
    """Every alternative a brace pattern names (`{/proc/1/environ,x}` is two), innermost braces first."""
    found = BRACES.search(pattern)
    if not found:
        return [pattern]
    out: list[str] = []
    for alternative in found.group(1).split(","):
        out += _braces(pattern[:found.start()] + alternative + pattern[found.end():])
        if len(out) > BRACE_LIMIT:
            raise ValueError("too many alternatives")
    return out


def _glob_confined(pattern: str, base: str, work_mount: str, skills_mount: str = "", memory_mount: str = "") -> bool:
    """Each alternative's fixed part — up to its first wildcard — stays in /work, and no `..` follows a wildcard."""
    try:
        alternatives = _braces(pattern)
    except ValueError:
        return False
    for alternative in alternatives:
        head = GLOB_CHARS.split(alternative, 1)[0]
        fixed, rest = (head.rpartition("/")[0], alternative[len(head.rpartition("/")[0]):]) \
            if GLOB_CHARS.search(alternative) else (alternative, "")
        if ".." in rest.split("/"):
            return False
        if alternative.startswith("/") and not fixed:
            fixed = "/"
        if not _confined(_absolute(fixed, base) if fixed else base, "Glob", work_mount, skills_mount, memory_mount):
            return False
    return True


def _token_in(tool_input: dict) -> bool:
    text = json.dumps(tool_input, ensure_ascii=False)
    return any(TOKEN_SHAPE.search(form) for form in (text, urllib.parse.unquote_plus(text)))


def refusal(tool: str, tool_input: dict, cwd: str, work_mount: str, skills_mount: str = "",
            memory_mount: str = "") -> str:
    """Why the gate refuses this call whatever the rules and the owner say; empty when it does not."""
    if _token_in(tool_input):
        return SECRET
    for key in PATHS.get(tool, ()):
        if isinstance(path := tool_input.get(key), str) and path and \
                not _confined(_absolute(path, cwd), tool, work_mount, skills_mount, memory_mount):
            return OUTSIDE
    if tool == "Glob" and isinstance(pattern := tool_input.get("pattern"), str):
        given = tool_input.get("path")
        base = _absolute(given if isinstance(given, str) and given else cwd or WORK, cwd)
        if not _glob_confined(pattern, base, work_mount, skills_mount, memory_mount):
            return OUTSIDE
    return ""


def _shown(text: str) -> tuple[str, bool]:
    """The text with every invisible character written as its code, so what the owner reads is what runs."""
    shown = "".join(f"⟦U+{ord(c):04X}⟧" if c not in "\n\t" and unicodedata.category(c) in courier.INVISIBLE else c
                    for c in text)
    return shown, shown != text


def input_text(approval: dict) -> str:
    """The call's input as the owner reads it: a Bash command as it is, anything else as indented JSON. The other
    fields of a Bash call follow the command, one per line: what is approved is the whole input, and a flag such as
    `run_in_background` changes what the command does. Its `description` is left out: the model's own words about
    the command run nothing, and on a card they would only vouch for it."""
    tool_input = approval["input"]
    if approval["tool"] == "Bash" and isinstance(tool_input.get("command"), str):
        rest = {k: v for k, v in tool_input.items() if k not in ("command", "description")}
        return "\n".join([tool_input["command"], *(f"— {k}: {json.dumps(v, ensure_ascii=False)}"
                                                   for k, v in rest.items())]) if rest else tool_input["command"]
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


def card(approval: dict, session: str, track: str) -> tuple[courier.CardView, tuple[str, ...]]:
    """The approval card and the buttons it offers: approve and reject, or — for an input too long for one
    message — only «Open», which sends it whole."""
    text = input_text(approval)
    body, hidden = _shown(text)
    head = [f"Permission · {courier.line(track)} · session {session}", f"Tool: {courier.line(approval['tool'])}",
            f"Folder: {courier.line(approval['cwd'], 200)}"]
    if why := approval.get("why"):
        head.append(courier.line(why, 300))
    if hidden:
        head.append("Warning: the input has invisible characters — they are shown as codes ⟦U+…⟧")
    if found := courier.findings(text):
        head.append("Found in the text: " + "; ".join(found))
    head.append(f"Input, {len(text)} characters:")
    view = courier.CardView(head=head, body=body)
    if len(view.plain()) <= OPEN_LIMIT:
        return view, (APPROVE, REJECT)
    view.body = "The input is long — «Open» sends it whole as a file."
    return view, (OPEN,)


def classifier_why(reason: str) -> str:
    """The card's line for a call auto mode's classifier stopped: its reason as the CLI gave it (`[Remote Repoint]`)."""
    return f"Stopped by the classifier: {reason or 'no reason given'}"


def rule_why(rule: str) -> str:
    """The card's line for a call under a card rule: it comes to the owner whatever the classifier would say."""
    return f"Always ask: rule {rule}"


def blocked_card(approval: dict, session: str, track: str, why: str) -> courier.CardView:
    """A call auto's classifier blocked and that did not come back as a card (no retry, or the model went on): the
    owner sees it anyway, with no button — the session has moved past it."""
    body, hidden = _shown(input_text(approval))
    head = [f"Blocked · {courier.line(track)} · session {session}", f"Tool: {courier.line(approval['tool'])}",
            f"Folder: {courier.line(approval['cwd'], 200)}", courier.line(why, 300)]
    if hidden:
        head.append("Warning: the input has invisible characters — they are shown as codes ⟦U+…⟧")
    return courier.CardView(head=head, body=body, status="Not run: the session went on without this call. If you need "
                                                         "it, write to the session.")


# A push or a pull request in the room is the owner's button «Push» (topush): a branch and a PR, by the platform.
BY_BUTTON = {label for rule, label in NEVER if rule in ("Bash(git push:*)", "Bash(gh pr create:*)")}


def pushed_by_button(label: str, button: bool) -> bool:
    return button and label in BY_BUTTON


def courier_card(approval: dict, session: str, track: str, label: str, button: bool = False) -> courier.CardView:
    """A never-list call: not run, handed to the owner whole — the command, the session, the directory. `button`: the
    session's lane has «Push», which a push or a PR goes by instead."""
    body, hidden = _shown(input_text(approval))
    head = [f"Courier · {courier.line(track)} · session {session}", f"Wants: {label}",
            f"Folder: {courier.line(approval['cwd'], 200)}"]
    if hidden:
        head.append("Warning: the command has invisible characters — they are shown as codes ⟦U+…⟧")
    status = ("Not running it: press «Push» under the session's result — the platform pushes a branch and opens a PR, "
              "not into main." if pushed_by_button(label, button)
              else "Not running it: you do this yourself — the whole command is above.")
    return courier.CardView(head=head, body=body, status=status)


# --- what a resume will cost --------------------------------------------------------------------------------------

# Since CLI 2.1.304 a resume of a conversation idle past the one-hour prompt cache re-reads its whole context uncached,
# and on a subscription that is a noticeable share of the 5-hour window. A card left this long says so: pressing it is
# such a resume.
IDLE_WARN_S = 55 * 60
IDLE_PREFIX = "The session is idle"
# Cards persisted before the switch to English carry the Russian line; it is taken out the same way.
IDLE_PREFIXES = (IDLE_PREFIX, "Сессия простаивает")


def idle_words(idle_s: float) -> str:
    """«57 min» under two hours, «3 h» under a day, then «over a day»."""
    minutes = int(idle_s // 60)
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60} h" if minutes < 24 * 60 else "over a day"


def idle_line(idle_s: float, tokens: int | None) -> str | None:
    """The card's line about a resume after `idle_s` of silence, with the context it re-reads when known; None while
    the cache still holds."""
    if idle_s < IDLE_WARN_S:
        return None
    thousands = round(tokens / 1000) if tokens else 0
    size = f" (~{thousands}k tokens)" if thousands else ""
    return f"{IDLE_PREFIX} {idle_words(idle_s)} — resuming re-reads the context{size} uncached"


def idle_stamp(idle_s: float) -> str:
    """When the Telegram card is rewritten: past the threshold, at an hour and a half, then as the hours change — not
    every minute, and never saying fewer minutes than have passed by much."""
    if idle_s < 90 * 60:
        return "55 min"
    return "90 min" if idle_s < 7200 else idle_words(idle_s)


def without_idle(view: dict) -> dict:
    """A card no longer asking (decided, stale, expired): its idle line and stamp go — a resume's cost is moot."""
    out = {**view, "head": [h for h in view.get("head", []) if not h.startswith(IDLE_PREFIXES)]}
    out.pop("idle", None)
    return out


def with_idle(view: dict, line: str, stamp: str) -> dict:
    """The card's view with `line` among its header lines — in place of an older one, before «Input, N characters:» that
    names the body — and `stamp`, which says when it was last written."""
    head = [h for h in view.get("head", []) if not h.startswith(IDLE_PREFIXES)]
    at = len(head) - 1 if head and head[-1].startswith(("Input", "Ввод")) else len(head)
    return {**view, "head": [*head[:at], line, *head[at:]], "idle": stamp}
