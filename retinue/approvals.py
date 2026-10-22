"""What a session may do without asking, what it may never do, and the cards that put the rest to the owner.

The CLI matches the rules itself: every always rule and every never-list entry becomes a marker hook of the run's
settings (`cliengine.settings`), and its mark tells the platform's gate whether the CLI's own rule decides the call
or the owner does. An approval is bound to a digest of the call — the tool, its input, the directory and the hash
of the file it would change — so a grant runs only the call the owner saw, on the file as it was.

The never list is what leaves the system: a push, a pull request, a release, a publish, a deploy, a connection to
another machine, a letter. Such a call is never granted; the owner gets it as a courier card and runs it himself.
Each entry is also in `--disallowedTools`, so even a mistaken grant cannot run it.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass

from . import courier, transcript
from .config import LaneRef, Track

NEVER: list[tuple[str, str]] = [
    ("Bash(git push:*)", "отправить коммиты (git push)"),
    ("Bash(gh pr create:*)", "открыть пулл-реквест (gh pr create)"),
    ("Bash(gh pr merge:*)", "влить пулл-реквест (gh pr merge)"),
    ("Bash(gh release create:*)", "выпустить релиз (gh release create)"),
    ("Bash(npm publish:*)", "опубликовать пакет npm (npm publish)"),
    ("Bash(uv publish:*)", "опубликовать пакет PyPI (uv publish)"),
    ("Bash(twine upload:*)", "опубликовать пакет PyPI (twine upload)"),
    ("Bash(cargo publish:*)", "опубликовать крейт (cargo publish)"),
    ("Bash(docker push:*)", "отправить образ (docker push)"),
    ("Bash(kubectl apply:*)", "выкатить в кластер (kubectl apply)"),
    ("Bash(ssh:*)", "зайти на другую машину (ssh)"),
    ("Bash(scp:*)", "скопировать на другую машину (scp)"),
    ("Bash(rsync:*)", "синхронизировать с другой машиной (rsync)"),
    ("Bash(sendmail:*)", "отправить письмо (sendmail)"),
    ("Bash(mail:*)", "отправить письмо (mail)"),
]
BASE_ALLOW = ["Read", "Grep", "Glob", "TodoWrite"]
BASE_DENY = ["WebSearch", "WebFetch", "CronCreate", "CronDelete", "ScheduleWakeup", "RemoteTrigger"]

WORK = "/work"  # the lane's own view of its work volume; the platform reads it at `LaneRef.work_mount`
OPEN_LIMIT = 4096  # one Telegram message: a longer card offers only «Открыть»
APPROVE, REJECT, OPEN = "approve", "reject", "open"


@dataclass(frozen=True)
class Mark:
    kind: str        # allow | deny | never
    rule: str        # in the CLI's syntax: what its marker hook's `if` matches
    label: str = ""  # never: what the courier card says the session wants


def marks_for(track: Track, lane: LaneRef) -> list[Mark]:
    """Every rule of a run, in marker order (`/hook/mark/<n>` is the n-th). A rule named twice keeps its strictest
    kind: never, then deny, then allow."""
    marks = [Mark("never", rule, label) for rule, label in NEVER]
    marks += [Mark("deny", rule) for rule in [*BASE_DENY, *lane.deny, *track.deny]]
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
    target is a lane path, read back through the mount."""
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
        if not os.path.islink(here):
            done.append(part)
            continue
        links += 1
        if links > 40:  # as the kernel's own limit: a loop of links
            raise Unbindable("too many links")
        link = os.readlink(here)
        if os.path.isabs(link):
            done, followed = [], _lane_relative(link)
            if followed is None:
                raise Unbindable("a link leads out of /work")
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


def _shown(text: str) -> tuple[str, bool]:
    """The text with every invisible character written as its code, so what the owner reads is what runs."""
    shown = "".join(f"⟦U+{ord(c):04X}⟧" if c not in "\n\t" and unicodedata.category(c) in courier.INVISIBLE else c
                    for c in text)
    return shown, shown != text


def _body(approval: dict) -> str:
    tool_input = approval["input"]
    if approval["tool"] == "Bash" and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


def card(approval: dict, session: str, track: str) -> tuple[courier.CardView, tuple[str, ...]]:
    """The approval card and the buttons it offers: approve and reject, or — for an input too long for one
    message — only «Открыть», which sends it whole."""
    text = _body(approval)
    body, hidden = _shown(text)
    head = [f"Разрешение · {courier.line(track)} · сессия {session}", f"Инструмент: {courier.line(approval['tool'])}",
            f"Папка: {courier.line(approval['cwd'], 200)}"]
    if hidden:
        head.append("Внимание: во вводе невидимые символы — они показаны кодами ⟦U+…⟧")
    if found := courier.findings(text):
        head.append("В тексте найдено: " + "; ".join(found))
    head.append(f"Ввод, {len(text)} знаков:")
    view = courier.CardView(head=head, body=body)
    if len(view.plain()) <= OPEN_LIMIT:
        return view, (APPROVE, REJECT)
    view.body = "Ввод длинный — целиком он придёт файлом по кнопке «Открыть»."
    return view, (OPEN,)


def courier_card(approval: dict, session: str, track: str, label: str) -> courier.CardView:
    """A never-list call: not run, handed to the owner whole — the command, the session, the directory."""
    body, hidden = _shown(_body(approval))
    head = [f"Курьер · {courier.line(track)} · сессия {session}", f"Хочет: {label}",
            f"Папка: {courier.line(approval['cwd'], 200)}"]
    if hidden:
        head.append("Внимание: в команде невидимые символы — они показаны кодами ⟦U+…⟧")
    return courier.CardView(head=head, body=body, status="Не выполняю: это делаешь ты сам — команда выше целиком.")
