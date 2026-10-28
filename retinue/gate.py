"""The gate: the platform's answers to a lane CLI's hooks. Every call a session makes passes PreToolUse here first,
and the gate decides whether the CLI decides it — its rules, then its auto classifier — the owner does, or nobody may.

A run's settings put a marker hook before the general one for every always rule, card rule and never-list entry
(`approvals.marks_for`); the CLI fires a marker only when its own matcher says the call falls under that rule. The
general PreToolUse waits a moment for the call's marks: a never mark is denied and handed to the owner as a courier
card, a card mark is deferred to the owner, an always rule's mark leaves the decision to the CLI's rule, and a call
nothing marked gets no decision — auto mode's classifier decides it, as on the owner's Mac. A call the classifier
blocks fires PermissionDenied: the gate tells the CLI the model may retry, and the same call made again is deferred
to the owner with the classifier's reason on the card.

A deferral ends the run; the owner gets a card, and his decision comes back as a bare `--resume` that fires
PreToolUse again for the same call. A grant is bound to the digest of the call as the owner saw it and is spent before
the gate answers `allow`, which bypasses the classifier; so it runs at most once: a crash after that asks the owner
again.

AskUserQuestion is always the owner's: deferred as a question card, and on the resume after his answer allowed with
the answers in its `updatedInput` (`questions`).

A run the CLI did not start in auto — the hook's `permission_mode`, or the run's init line, says another mode — falls
back to the gate before auto: a call nothing marked is deferred to the owner, and a call the CLI asks about although a
rule's marker fired (PermissionRequest) is denied and goes to the owner when it is made again.

A subagent's calls pass the same hooks, marked by its `agent_id`, and the same rules — but a subagent cannot wait: a
call that would be deferred to the owner is denied instead (`SUBAGENT`), and the main agent makes it itself.

The platform's own MCP tools (`subs`: sub-sessions) pass here like any call — the classifier judges them — and the
gate remembers each one it let through (`passed`): the platform's server runs a `tools/call` only for a call this gate
saw with the same arguments. A subagent, a side question and a sub-session never get them past the gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import approvals, exfil, questions, subs
from .approvals import Mark, Unbindable
from .config import LaneRef
from .sessions import PlatformStore

log = logging.getLogger("retinue.gate")

MARK_WAIT_S = 1.0     # how long the general PreToolUse waits for the call's marker hooks
# Retries the gate lets one run make after classifier blocks. The CLI aborts a headless run after 3 classifier denials
# in a row or 20 in all; a retry the gate allows is deferred, never denied, so it adds none — the cap keeps it so even
# for a model that tries one blocked call after another.
RETRIES_PER_RUN = 2
CALL_KEEP_S = 3600.0  # how long a call's marks are remembered: a late mark, the same call fired again on resume

NEVER = "Это делает только Владелец: команда ушла ему карточкой. Не повторяй; скажи в итоге, что шаг ждёт его."
NEVER_PUSH = ("Пушит платформа по кнопке Владельца «Запушить» под твоим итогом: ветка и PR. Не повторяй; закоммить "
              "всё нужное и скажи в итоге репозиторий, ветку и сколько коммитов.")
REJECTED = "Владелец отказал"
STALE = "Владелец написал новое сообщение — вызов не выполнен"
CHANGED = "Вызов или файл изменились после разрешения — вызов не выполнен"
UNBINDABLE = "Файл, который меняет вызов, платформе не прочитать — разрешить такой вызов нельзя"
ASKED = "Это не покрыто правилами сессии. Повтори тот же вызов — он уйдёт Владельцу на разрешение."
AUTO = "auto"
UNKNOWN = "Неизвестный запуск"
DEFERRED = "Ждёт решения Владельца"
CLOSING = "Платформа останавливается"
UNCHECKED = "Платформа не смогла проверить этот вызов — он не выполнен. Упрости его: другой путь или шаблон."
UNANSWERED = "Вопрос остался без ответа Владельца. Если ответ ещё нужен — спроси снова."
# A subagent's call never waits for the owner: a deferral inside a subagent ends the subagent without its report and
# is lost — the run goes on and no resume brings the call back (probed with 2.1.304). So what would be deferred to the
# owner is denied with this, no card made; the main agent, told so in the report, makes the call itself.
SUBAGENT = ("Субагент не может ждать решения Владельца — вызов не выполнен. Не повторяй; закончи отчёт и назови в нём "
            "этот шаг: главная сессия сделает его сама.")


@dataclass(frozen=True)
class Run:
    """A run as its hook token names it: the session it serves and the marks its settings carry, in marker order."""
    id: str
    session: str
    track: str
    lane: str
    marks: tuple[Mark, ...]
    mode: str | None = None  # the permission mode its init line named; None until the platform has read it
    read_only: bool = False  # a side question's run: it never leaves anything to the classifier
    subs: bool = False       # it may start sub-sessions (`subs.may_start`)
    turn_by: str = ""        # whose doing its session's turn is: owner | parent | report (`subs`)


@dataclass(frozen=True)
class _Reask:
    """A call to put to the owner when the session makes it again: why, and the call as it was asked or blocked."""
    why: str
    run: str
    call_id: str
    tool: str
    input: dict
    cwd: str
    blocked: bool  # the classifier blocked it (a notice card if it never comes again), not a fallback ask


@dataclass
class _Call:
    """One tool call as the gate has seen it so far, by its `tool_use_id`."""
    seen: float
    marks: set[Mark] = field(default_factory=set)
    marked: asyncio.Event = field(default_factory=asyncio.Event)
    decided: bool = False
    retried: bool = False  # the classifier blocked it and the model was told it may retry: once per call


def _decision(decision: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def _ask_denied(message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "deny", "message": message}}}


def retire(store: PlatformStore, approval: dict, status: str, now: float) -> None:
    """A shown card that no longer asks anything: rewritten with `status` and no buttons."""
    if approval["kind"] == "question":
        return questions.retire(store, approval, status, now)  # one Telegram message per question
    view = {**approvals.without_idle(approval["view"]), "status": status}
    store.move(approval["id"], (approval["state"],), approval["state"], now, view=view)
    store.add_note("update", approval["session"], {"view": view, "buttons": []}, approval=approval["id"])


class Gate:
    def __init__(self, store: PlatformStore, lanes: dict[str, LaneRef], secret: str,
                 clock: Callable[[], float] = time.time, skills_mount: str = "", memory_mount: str = "") -> None:
        if not secret:
            raise SystemExit("the platform needs RETINUE_HOOK_SECRET: hook tokens are kept only as its HMAC")
        self.store, self.lanes, self.secret, self.clock = store, lanes, secret.encode(), clock
        self.skills_mount = skills_mount  # the owner's skills as the platform reads them (approvals.SKILLS)
        self.memory_mount = memory_mount  # and his memory snapshot (approvals.OWNER_MEMORY)
        self.push_lanes: frozenset[str] = frozenset()  # lanes whose `git push` is the owner's «Запушить» (topush)
        self.closing = False
        self._calls: dict[str, _Call] = {}
        # (session, reask key): the CLI asked, or its classifier blocked the call; made again, it goes to the owner
        self._reasks: dict[tuple[str, str], _Reask] = {}
        self._retried: dict[str, set[str]] = {}  # run -> the reask keys it was let retry
        self.budget = exfil.Budget()  # per run: the address characters each site has taken (`exfil.SITE_BUDGET`)
        self._passed: dict[tuple[str, str], tuple[str, str, str]] = {}  # (run, call) -> (tool, digest, how)


    def token_hash(self, token: str) -> str:
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    def run_of(self, token: str) -> Run | None:
        """The run a token was given to, while it runs: a token of a run that ended opens nothing — no hook, no
        `tools/call` of the platform's server."""
        row = self.store.run_of(self.token_hash(token)) if token else None
        session = self.store.session(row["session"]) if row and row.get("ended") is None else None
        if session is None:
            return None
        return Run(row["id"], row["session"], session["track"], session["lane"],
                   tuple(Mark(**mark) for mark in row["marks"]), row.get("mode"), bool(session.get("parent")),
                   subs.may_start(session), session.get("turn_by") or "")

    def close(self) -> None:
        """Shutting down: every PreToolUse still waiting, and every one after, defers."""
        self.closing = True
        for call in self._calls.values():
            call.marked.set()

    def _call(self, call_id: str) -> _Call:
        now = time.monotonic()
        if len(self._calls) > 1000:
            self._calls = {k: c for k, c in self._calls.items() if now - c.seen < CALL_KEEP_S}
        call = self._calls.setdefault(call_id, _Call(now))
        call.seen = now
        return call

    async def hook(self, event: str, token: str, body: dict) -> dict:
        """`event` is the last part of the hook's address: a CLI event, or `mark/<n>`."""
        run = self.run_of(token)
        if run is None:
            if event == "PermissionDenied":
                return {}  # no retry: the classifier's deny stands
            return _ask_denied(UNKNOWN) if event == "PermissionRequest" else _decision("deny", UNKNOWN)
        if event.startswith("mark/"):
            return self.mark(run, int(event.removeprefix("mark/")), body)
        if event == "PreToolUse":
            answer = await self.pre_tool_use(run, body)
            self._remember(run, body, answer)
            return answer
        if event == "PermissionRequest":
            return self.permission_request(run, body)
        if event == "PermissionDenied":
            return self.permission_denied(run, body)
        if event == "PostToolUse":
            self.post_tool_use(run, body)
        return {}

    def _remember(self, run: Run, body: dict, answer: dict) -> None:
        """A call of the platform's own tools this gate let through — to the CLI's rules or classifier, or allowed —
        is one `tools/call` may serve (`passed`). Kept in memory: a restart in between refuses the call, and the model
        makes it again."""
        tool, tool_input, _ = _call_of(body)
        if tool.startswith(subs.PREFIX) and (decision := decision_of(answer)) not in ("deny", "defer"):
            if len(self._passed) > 1000:
                self._passed.clear()
            how = "granted" if decision == "allow" else "passed"  # an allow is only ever the owner's grant here
            self._passed[(run.id, str(body.get("tool_use_id") or ""))] = (tool, subs.args_digest(tool_input), how)

    def passed(self, run: str, call_id: str, tool: str, arguments: dict) -> str | None:
        """Whether the platform's server may run this call: None unless this gate saw it, in this run, with these
        arguments, and let it through; `granted` when the owner allowed it on a card, else `passed`. A second
        `tools/call` of the same id passes too: the server answers it from its own tables."""
        seen = self._passed.get((run, call_id))
        return seen[2] if seen is not None and seen[:2] == (tool, subs.args_digest(arguments)) else None

    def mark(self, run: Run, n: int, body: dict) -> dict:
        if not 0 <= n < len(run.marks):
            return {}
        mark, call_id = run.marks[n], str(body.get("tool_use_id") or "")
        call = self._call(call_id)
        call.marks.add(mark)
        call.marked.set()
        if mark.kind == "never" and call.decided:
            self._courier(run, call_id, mark, body)
        return {}

    async def pre_tool_use(self, run: Run, body: dict) -> dict:
        try:
            refused = approvals.refusal(*_call_of(body), self.lanes[run.lane].work_mount, self.skills_mount,
                                        self.memory_mount)
        except Exception as exc:  # noqa: BLE001 — a failed hook lets the CLI run the call: the check fails closed
            log.warning("refusal check failed: %s", type(exc).__name__)  # the type only: the input may hold a secret
            refused = UNCHECKED
        if refused:
            return _decision("deny", refused)  # first: no rule, card or courier ever holds such an input
        if _call_of(body)[0].startswith(subs.PREFIX) and (_subagent(body) or not run.subs):
            return _decision("deny", subs.NO_DEPTH)  # depth 1: not a subagent's, a side question's or a child's
        if self.closing:
            return _decision("deny" if _subagent(body) else "defer", CLOSING)
        call_id = str(body.get("tool_use_id") or "")
        call = self._call(call_id)
        await self._wait_for_marks(run, body, call)
        if self.closing:
            return _decision("deny" if _subagent(body) else "defer", CLOSING)
        call.decided = True
        if never := next((m for m in call.marks if m.kind == "never"), None):
            self._courier(run, call_id, never, body)
            return _decision("deny", NEVER_PUSH if approvals.pushed_by_button(never.label, run.lane in self.push_lanes)
                             else NEVER)
        if any(m.kind == "deny" for m in call.marks):
            return {}  # the CLI's own deny rule refuses it; no card may grant it past that
        if _call_of(body)[0] == questions.QUESTION:
            return _decision("deny", SUBAGENT) if _subagent(body) else self._question(run, call_id, body)

        tool, tool_input, cwd = _call_of(body)
        try:
            file_hash = approvals.file_hash(tool, tool_input, self.lanes[run.lane].work_mount)
            digest = approvals.digest(tool, tool_input, cwd, file_hash)
        except Unbindable as exc:
            log.warning("call %s: %s", call_id, exc)
            file_hash = digest = None
        key = _reask_key(tool, tool_input, cwd, file_hash) if digest is not None else None
        if found := self.store.approvals(session=run.session, tool_use_id=call_id, kind="approval"):
            approval, now = found[-1], self.clock()
            state = approval["state"]
            if state == "granted":
                if digest == approval["digest"] and self.store.move(approval["id"], ("granted",), "executing", now):
                    return _decision("allow", "Владелец разрешил")
                if self.store.move(approval["id"], ("granted",), "dropped", now):
                    retire(self.store, {**approval, "state": "dropped"}, "Не выполнено: " + CHANGED.lower(), now)
                return _decision("deny", CHANGED)
            if state == "rejected":
                return _decision("deny", REJECTED)
            if state == "stale":
                return _decision("deny", STALE)
            if state in ("pending", "shown"):
                return _decision("defer", DEFERRED)
        command = tool_input.get("command") if tool in approvals.COMMAND_TOOLS else None
        if isinstance(command, str) and (label := approvals.never_in(command)):
            # Its text names a never-list action the CLI's matcher did not catch (`git -C . push`): the owner reads it.
            if digest is None:
                return _decision("deny", UNBINDABLE)
            return self._to_owner(run, call_id, body, file_hash, digest, approvals.never_why(label))
        if leak := exfil.why(tool, tool_input) or self.budget.over(run.id, tool, tool_input):
            # A web call whose address or query looks like it carries data out, or one that takes its site past the
            # run's budget: the owner reads it whole first, whatever an allow rule or the classifier would say
            # (`exfil`). The owner's card path: a subagent's call is never deferred there.
            if digest is None:
                return _decision("deny", UNBINDABLE)
            return self._to_owner(run, call_id, body, file_hash, digest, leak)
        if key is not None and (run.session, key) in self._reasks:
            if _subagent(body):  # the main agent's re-ask stays its own, for when it makes the call
                return _decision("deny", SUBAGENT)
            # One card per ask or block: the same call made after the owner's answer is asked about anew.
            return self._to_owner(run, call_id, body, file_hash, digest, self._reasks.pop((run.session, key)).why)
        if tool == subs.PREFIX + subs.START and run.turn_by == "report":
            if digest is None:
                return _decision("deny", UNBINDABLE)
            return self._to_owner(run, call_id, body, file_hash, digest, subs.FROM_REPORT_CARD)
        if card := next((m for m in call.marks if m.kind == "card"), None):
            if digest is None:
                return _decision("deny", UNBINDABLE)
            return self._to_owner(run, call_id, body, file_hash, digest, approvals.rule_why(card.rule))
        if call.marks:
            return {}  # an always rule matched: the CLI's own rule decides
        if _auto(run, body):
            return {}  # nothing names it: auto mode's classifier decides, as on the owner's Mac
        if digest is None:
            return _decision("deny", UNBINDABLE)
        return self._to_owner(run, call_id, body, file_hash, digest)

    async def _wait_for_marks(self, run: Run, body: dict, call: _Call) -> None:
        """Wait, up to MARK_WAIT_S, for the marks that change the answer. A tool a card rule names waits until a card
        or never mark comes, or the whole time: an allow mark that comes first must not run it card-less, nor a slow
        card mark leave it to the classifier. Otherwise in auto no mark changes the answer — a never or deny rule is
        the CLI's own --disallowedTools too, and a never mark that comes late still makes its courier card — and in
        the fallback the first mark settles it. A command whose text names a never-list action waits the same way for
        its never mark: a push the CLI's matcher caught is a courier card and a deny, never a card that defers."""
        tool, tool_input, _ = _call_of(body)
        command = tool_input.get("command") if tool in approvals.COMMAND_TOOLS else None
        if any(m.kind == "card" and m.rule.partition("(")[0] == tool for m in run.marks) or \
                (isinstance(command, str) and approvals.never_in(command)):
            done = lambda: any(m.kind in ("card", "never") for m in call.marks)  # noqa: E731
        elif run.marks and not _auto(run, body):
            done = lambda: bool(call.marks)  # noqa: E731
        else:
            return
        deadline = time.monotonic() + MARK_WAIT_S
        while not done() and not self.closing and (left := deadline - time.monotonic()) > 0:
            call.marked.clear()
            if done():
                break
            try:
                await asyncio.wait_for(call.marked.wait(), left)
            except TimeoutError:
                break

    def _to_owner(self, run: Run, call_id: str, body: dict, file_hash: str, digest: str, why: str = "") -> dict:
        if _subagent(body):
            return _decision("deny", SUBAGENT)
        tool, tool_input, cwd = _call_of(body)
        self.store.add_approval(run.session, run.id, call_id, "approval", tool, tool_input, cwd, file_hash, digest,
                                self.clock(), why=why)
        return _decision("defer", DEFERRED)

    def _question(self, run: Run, call_id: str, body: dict) -> dict:
        """AskUserQuestion is always the owner's: deferred to him as a question card, never the classifier's, and on
        the resume after his answer allowed once with the answers in its input (`questions.updated_input`). A question
        gone stale (he wrote a new message) or left unanswered is denied, so the model goes on with what he did write."""
        tool, tool_input, cwd = _call_of(body)
        digest = approvals.digest(tool, tool_input, cwd, "")
        if found := self.store.approvals(session=run.session, tool_use_id=call_id, kind="question"):
            asked, now = found[-1], self.clock()
            state = asked["state"]
            if state == "answered":
                if digest == asked["digest"] and self.store.move(asked["id"], ("answered",), "executing", now):
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "allow",
                        "permissionDecisionReason": "Владелец ответил",
                        "updatedInput": questions.updated_input(tool_input, asked["view"].get("answers") or {})}}
                if self.store.move(asked["id"], ("answered",), "dropped", now):
                    retire(self.store, {**asked, "state": "dropped"}, "Не передано: " + CHANGED.lower(), now)
                return _decision("deny", CHANGED)
            if state == "stale":
                return _decision("deny", STALE)
            if state in ("expired", "dropped"):
                return _decision("deny", UNANSWERED)
            if state in ("pending", "shown"):
                return _decision("defer", DEFERRED)
            # executing: the answer was handed over and the run cut before the call finished — asked again
        self.store.add_approval(run.session, run.id, call_id, "question", tool, tool_input, cwd, "", digest,
                                self.clock())
        return _decision("defer", DEFERRED)

    def permission_request(self, run: Run, body: dict) -> dict:
        """A run that fell back from auto: the CLI asks although a marker said a rule decides (a compound command, a
        path outside the directory). Denied, and the same call made again goes to the owner. In auto the classifier
        decides ordinary calls without asking, but a tool that needs a person asks — AskUserQuestion when allowed
        without its answers (probed with 2.1.304) — and that ask is denied here too: nothing is granted by an ask."""
        tool, tool_input, cwd = _call_of(body)
        try:
            file_hash = approvals.file_hash(tool, tool_input, self.lanes[run.lane].work_mount)
        except Unbindable:
            return _ask_denied(UNBINDABLE)
        self._reasks[(run.session, _reask_key(tool, tool_input, cwd, file_hash))] = _Reask(
            "", run.id, str(body.get("tool_use_id") or ""), tool, tool_input, cwd, blocked=False)
        return _ask_denied(ASKED)

    def permission_denied(self, run: Run, body: dict) -> dict:
        """Auto mode's classifier blocked the call (the CLI has denied it already). The model is told it may retry —
        once per call, once per call key in a run, RETRIES_PER_RUN in all — and the same call made again is deferred
        to the owner with the classifier's reason (`pre_tool_use`). A block not retried is still the owner's to see:
        at once as a notice card when the gate gives no retry, at the run's end (`run_ended`) when the model went on."""
        call_id = str(body.get("tool_use_id") or "")
        if not call_id or (call := self._call(call_id)).retried:
            return {}
        call.retried = True
        tool, tool_input, cwd = _call_of(body)
        why = approvals.classifier_why(str(body.get("reason") or ""))
        try:
            key = _reask_key(tool, tool_input, cwd,
                             approvals.file_hash(tool, tool_input, self.lanes[run.lane].work_mount))
        except Unbindable:
            key = None  # no card could grant it
        retried = self._retried.setdefault(run.id, set())
        block = _Reask(why, run.id, call_id, tool, tool_input, cwd, blocked=True)
        if key is None or key in retried or len(retried) >= RETRIES_PER_RUN or _subagent(body):
            self._notice(run.session, block)  # a subagent's retry could only be denied (SUBAGENT): none
            return {}
        retried.add(key)
        self._reasks[(run.session, key)] = block
        return {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}}

    def run_ended(self, run: str) -> None:
        """The run is over (its result, or its process gone): a block it was let retry and never made again is the
        owner's as a notice card; its asks and retries are forgotten."""
        self._retried.pop(run, None)
        self.budget.forget(run)
        for key in [k for k in self._passed if k[0] == run]:  # its token opens nothing now: its calls go too
            del self._passed[key]
        for key, reask in [(k, r) for k, r in self._reasks.items() if r.run == run]:
            del self._reasks[key]
            if reask.blocked:
                self._notice(key[0], reask)

    def _notice(self, session: str, block: _Reask) -> None:
        """A notice-only card of a classifier block, once per call; courier-kind: no button, the command to copy."""
        if self.store.approvals(session=session, tool_use_id=block.call_id, kind="courier"):
            return
        row = self.store.session(session)
        call = {"tool": block.tool, "input": block.input, "cwd": block.cwd}
        view = dataclasses.asdict(approvals.blocked_card(call, session, row["track"] if row else "", block.why))
        notice = self.store.add_approval(session, block.run, block.call_id, "courier", block.tool, block.input,
                                         block.cwd, "", "", self.clock(), state="dropped", view=view, why=block.why)
        self.store.add_note("card", session, {"view": view, "buttons": []}, approval=notice)

    def post_tool_use(self, run: Run, body: dict) -> None:
        call_id = str(body.get("tool_use_id") or "")
        for approval in self.store.approvals(session=run.session, tool_use_id=call_id, kind=questions.KINDS,
                                             states=("executing",)):
            self.store.move(approval["id"], ("executing",), "executed", self.clock())

    def _courier(self, run: Run, call_id: str, mark: Mark, body: dict) -> None:
        """A never-list call: no approval of it may stay grantable, and the owner gets it once as a courier card."""
        if self.store.approvals(session=run.session, tool_use_id=call_id, kind="courier"):
            return
        now = self.clock()
        for approval in self.store.approvals(session=run.session, tool_use_id=call_id, kind="approval",
                                             states=("pending", "shown", "granted")):
            if self.store.move(approval["id"], (approval["state"],), "dropped", now) and approval["state"] == "shown":
                retire(self.store, {**approval, "state": "dropped"}, "Снято: это делает только Владелец — "
                                                                     "ушло карточкой курьера", now)
        tool, tool_input, cwd = _call_of(body)
        call = {"tool": tool, "input": tool_input, "cwd": cwd}
        view = dataclasses.asdict(approvals.courier_card(call, run.session, run.track, mark.label,
                                                         button=run.lane in self.push_lanes))
        courier = self.store.add_approval(run.session, run.id, call_id, "courier", tool, tool_input, cwd, "", "", now,
                                          state="dropped", view=view)
        self.store.add_note("card", run.session, {"view": view, "buttons": []}, approval=courier)


def decision_of(answer: dict) -> str | None:
    return (answer.get("hookSpecificOutput") or {}).get("permissionDecision")

def _auto(run: Run, body: dict) -> bool:
    """The CLI runs this call in auto: its hook says so, and the run's init line, once read, said so too. Anything
    else — manual, plan, a hook without the field — falls back to the owner's cards; and a side question's run always
    does: it changes nothing, so nothing of it is the classifier's to let through."""
    return not run.read_only and body.get("permission_mode") == AUTO and run.mode in (None, AUTO)


def _subagent(body: dict) -> bool:
    """A call a subagent makes: its hooks carry the subagent's `agent_id` (and `agent_type`), the main agent's none
    (probed with 2.1.304)."""
    return bool(body.get("agent_id"))


def _reask_key(tool: str, tool_input: dict, cwd: str, file_hash: str) -> str:
    """The same call made again, as the gate recognises it: the digest without a Bash call's `description`, the
    model's own words about the command, which it may well put differently the second time."""
    if tool in approvals.COMMAND_TOOLS:
        tool_input = {k: v for k, v in tool_input.items() if k != "description"}
    return approvals.digest(tool, tool_input, cwd, file_hash)


def _call_of(body: dict) -> tuple[str, dict, str]:
    tool_input = body.get("tool_input")
    return (str(body.get("tool_name") or ""), tool_input if isinstance(tool_input, dict) else {},
            str(body.get("cwd") or ""))
