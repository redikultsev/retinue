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

from . import courier
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

FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
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


def file_hash(tool: str, tool_input: dict, work_mount: str) -> str:
    """SHA-256 of the file the call would change, read through the platform's mount of the lane's /work: `absent`
    when there is none yet, `outside` for a path that is not in /work, `unreadable` for one the platform may not
    read; empty for a tool that changes no file."""
    key = FILE_TOOLS.get(tool)
    if key is None:
        return ""
    path = tool_input.get(key)
    if not isinstance(path, str) or not os.path.isabs(path):
        return "outside"
    relative = os.path.relpath(os.path.normpath(path), WORK)
    if relative == os.curdir or relative.split(os.sep)[0] == os.pardir:
        return "outside"
    root = os.path.realpath(work_mount)
    target = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath([root, target]) != root:
        return "outside"
    try:
        with open(target, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"


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
