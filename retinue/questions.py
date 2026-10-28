"""AskUserQuestion: the model's question with options, put to the owner as a card and answered by a button, in
Telegram or in the face — or in his own words.

The CLI runs it the way it runs any call the owner decides (probed with 2.1.304): the gate defers it, the turn ends
`tool_deferred`, and the owner's answer comes back as a bare `--resume` whose PreToolUse the gate answers `allow` with
`updatedInput` = the call's input plus `answers`, keyed by each question's text — a multi-select's labels joined by
«, », a typed answer as it was written. The CLI then hands the model «Your questions have been answered: …». An
`allow` without the answers is not enough: the tool asks again, and that ask is denied.

One call may carry up to four questions. It is one approval row (kind `question`), bound to the call's digest like any
card, and one Telegram message per question, each keyed `<approval>/<n>` in the notes and the router's `sent` table.
The approval's `view` holds what was answered so far (`answers`) and a multi-select's ticks not yet sent (`picks`);
the call is answered — and the session resumed — once every question has its answer. Everything is in SQLite: a
press an hour later, after any restart, answers once.
"""

from __future__ import annotations

import dataclasses
import re

from . import courier, transcript
from .sessions import PlatformStore

QUESTION = transcript.QUESTION
KINDS = ("approval", "question")  # the cards a session waits on: a decision or an answer
OTHER_LIMIT = 4000                # a typed answer, as long as one Telegram message
LABEL_CHARS = 60                  # a button's words
BODY_CHARS = 3000                 # one question as its Telegram card shows it; the face shows it whole
DONE = "done"
TICK, UNTICK = "☑ ", "☐ "
OPEN_SINGLE = "Выбери вариант кнопкой — или ответь на это сообщение своим текстом."
OPEN_MULTI = "Отметь варианты и нажми «Готово» — или ответь на это сообщение своим текстом."


def of(tool_input: dict) -> list[dict]:
    """The call's questions as the owner is shown them: text, header, options (label, description), multi-select.
    Whatever is not shaped so is dropped — the CLI validated the call before it asked."""
    out = []
    for q in tool_input.get("questions") or []:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            continue
        options = [{"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                   for o in q.get("options") or [] if isinstance(o, dict) and o.get("label")]
        out.append({"question": q["question"], "header": str(q.get("header") or ""), "options": options,
                    "multiSelect": q.get("multiSelect") is True})
    return out


def key(approval: str, n: int) -> str:
    """The note and the Telegram message of question `n` of a call."""
    return f"{approval}/{n}"


def split_key(value: str) -> tuple[str, int] | None:
    approval, _, n = value.partition("/")
    return (approval, int(n)) if n.isdigit() and approval else None


def action(n: int, choice: int | str) -> str:
    return f"q{n}:{choice}"


def parse_action(value: str) -> tuple[int, int | str] | None:
    """(question, option index or `done`) of a question button's action; None for any other action."""
    found = re.fullmatch(r"q([0-9]+):([0-9]+|done)", value or "")
    if not found:
        return None
    choice = found.group(2)
    return int(found.group(1)), (choice if choice == DONE else int(choice))


def is_tick(approval: dict, value: str) -> bool:
    """The action ticks or unticks an option of a multi-select: a button that may be pressed again."""
    parsed = parse_action(value)
    asked = of(approval["input"])
    return parsed is not None and parsed[1] != DONE and parsed[0] < len(asked) and asked[parsed[0]]["multiSelect"]


def open_view() -> dict:
    return {"answers": {}, "picks": {}, "status": ""}


def joined(labels: list[str]) -> str:
    return ", ".join(labels)


def in_order(question: dict, labels: list[str]) -> list[str]:
    """The labels picked, once each, in the options' own order."""
    picked = set(labels)
    return [o["label"] for o in question["options"] if o["label"] in picked]


def left(approval: dict) -> int:
    """How many of the call's questions have no answer yet."""
    return len(of(approval["input"])) - len(approval["view"].get("answers") or {})


def updated_input(tool_input: dict, answers: dict) -> dict:
    """What the gate's `allow` hands the CLI: the call's input as it was, plus each answer under its question's text."""
    return {**tool_input, "answers": {q["question"]: answers[str(n)]["value"]
                                      for n, q in enumerate(of(tool_input)) if str(n) in answers}}


def card(approval: dict, n: int, session: str, track: str, status: str = "") -> courier.CardView:
    """Question `n` of the call as its Telegram message: who asks, the question and its options verbatim, and what
    to do — or its answer, or `status` once it no longer asks."""
    questions = of(approval["input"])
    q = questions[n]
    count = f" · {n + 1} из {len(questions)}" if len(questions) > 1 else ""
    head = [f"Вопрос · {courier.line(track)} · сессия {session}{count}"]
    if q["header"]:
        head.append(courier.line(q["header"], 120))
    lines = [q["question"], ""] if q["options"] else [q["question"]]
    for i, option in enumerate(q["options"], 1):
        lines.append(f"{i}. {option['label']}" + (f" — {option['description']}" if option["description"] else ""))
    body = "\n".join(lines)
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS - 1] + "…"
    view = approval.get("view") or {}
    answer = (view.get("answers") or {}).get(str(n))
    if answer:
        words = "свой ответ" if answer.get("other") else "ответ"
        status = f"{words[0].upper()}{words[1:]}: {courier.line(answer['value'], 1000)}"
    elif not status:
        status = OPEN_MULTI if q["multiSelect"] else OPEN_SINGLE
    return courier.CardView(head=head, body=body, status=status)


def buttons(approval: dict, n: int) -> list[tuple[str, str]]:
    """Question `n`'s buttons while it asks, (words, action): one per option; a multi-select's options as ticks, and
    «Готово» last. None once it has its answer."""
    view = approval.get("view") or {}
    if str(n) in (view.get("answers") or {}):
        return []
    q = of(approval["input"])[n]
    picks = set((view.get("picks") or {}).get(str(n)) or [])
    out = []
    for i, option in enumerate(q["options"]):
        label = courier.line(option["label"], LABEL_CHARS)
        if q["multiSelect"]:
            label = (TICK if option["label"] in picks else UNTICK) + label
        out.append((label, action(n, i)))
    if q["multiSelect"]:
        out.append(("Готово", action(n, DONE)))
    return out


def retire(store: PlatformStore, approval: dict, status: str, now: float) -> None:
    """A question card that no longer asks (stale, expired, dropped): the approval moved to its state with `status`,
    and every question still unanswered rewritten in Telegram with it and no buttons. The answered ones keep their
    answer."""
    view = {**approval["view"], "status": status}
    store.move(approval["id"], (approval["state"],), approval["state"], now, view=view)
    store.void_buttons(approval["id"], "q", now)
    row = store.session(approval["session"])
    track = row["track"] if row else ""
    for n in range(len(of(approval["input"]))):
        if str(n) in (view.get("answers") or {}):
            continue
        shown = dataclasses.asdict(card({**approval, "view": view}, n, approval["session"], track, status))
        store.add_note("update", approval["session"], {"view": shown, "buttons": []}, approval=key(approval["id"], n))
