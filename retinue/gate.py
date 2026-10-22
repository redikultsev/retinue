"""The gate: the platform's answers to a lane CLI's hooks. Every call a session makes passes PreToolUse here, and the
gate decides whether the CLI's own rules decide it, the owner does, or nobody may.

A run's settings put a marker hook before the general one for every always rule and never-list entry
(`approvals.marks_for`); the CLI fires a marker only when its own matcher says the call falls under that rule. The
general PreToolUse waits a moment for the call's marks: a never mark is denied and handed to the owner as a courier
card, an always mark leaves the decision to the CLI's rule, and a call nothing marked is deferred — the run ends, the
owner gets a card, and his decision comes back as a bare `--resume` that fires PreToolUse again for the same call.
A grant is bound to the digest of the call as the owner saw it and is spent before the gate answers `allow`, so it
runs at most once: a crash after that asks the owner again.
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

from . import approvals
from .approvals import Mark, Unbindable
from .config import LaneRef
from .sessions import PlatformStore

log = logging.getLogger("retinue.gate")

MARK_WAIT_S = 1.0     # how long the general PreToolUse waits for the call's marker hooks
CALL_KEEP_S = 3600.0  # how long a call's marks are remembered: a late mark, the same call fired again on resume

NEVER = "Это делает только Владелец: команда ушла ему карточкой. Не повторяй; скажи в итоге, что шаг ждёт его."
REJECTED = "Владелец отказал"
STALE = "Владелец написал новое сообщение — вызов не выполнен"
CHANGED = "Вызов или файл изменились после разрешения — вызов не выполнен"
UNBINDABLE = "Файл, который меняет вызов, платформе не прочитать — разрешить такой вызов нельзя"
ASKED = "Это не покрыто правилами сессии. Повтори тот же вызов — он уйдёт Владельцу на разрешение."
UNKNOWN = "Неизвестный запуск"
DEFERRED = "Ждёт решения Владельца"
CLOSING = "Платформа останавливается"


@dataclass(frozen=True)
class Run:
    """A run as its hook token names it: the session it serves and the marks its settings carry, in marker order."""
    id: str
    session: str
    track: str
    lane: str
    marks: tuple[Mark, ...]


@dataclass
class _Call:
    """One tool call as the gate has seen it so far, by its `tool_use_id`."""
    seen: float
    marks: set[Mark] = field(default_factory=set)
    marked: asyncio.Event = field(default_factory=asyncio.Event)
    decided: bool = False


def _decision(decision: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def _ask_denied(message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "deny", "message": message}}}


def retire(store: PlatformStore, approval: dict, status: str, now: float) -> None:
    """A shown card that no longer asks anything: rewritten with `status` and no buttons."""
    view = {**approval["view"], "status": status}
    store.move(approval["id"], (approval["state"],), approval["state"], now, view=view)
    store.add_note("update", approval["session"], {"view": view, "buttons": []}, approval=approval["id"])


class Gate:
    def __init__(self, store: PlatformStore, lanes: dict[str, LaneRef], secret: str,
                 clock: Callable[[], float] = time.time) -> None:
        if not secret:
            raise SystemExit("the platform needs RETINUE_HOOK_SECRET: hook tokens are kept only as its HMAC")
        self.store, self.lanes, self.secret, self.clock = store, lanes, secret.encode(), clock
        self.closing = False
        self._calls: dict[str, _Call] = {}
        self._reasks: set[tuple[str, str]] = set()  # (session, digest): the CLI asked, the call goes to the owner

    def token_hash(self, token: str) -> str:
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    def run_of(self, token: str) -> Run | None:
        row = self.store.run_of(self.token_hash(token)) if token else None
        session = self.store.session(row["session"]) if row else None
        if session is None:
            return None
        return Run(row["id"], row["session"], session["track"], session["lane"],
                   tuple(Mark(**mark) for mark in row["marks"]))

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
            return _ask_denied(UNKNOWN) if event == "PermissionRequest" else _decision("deny", UNKNOWN)
        if event.startswith("mark/"):
            return self.mark(run, int(event.removeprefix("mark/")), body)
        if event == "PreToolUse":
            return await self.pre_tool_use(run, body)
        if event == "PermissionRequest":
            return self.permission_request(run, body)
        if event == "PostToolUse":
            self.post_tool_use(body)
        return {}

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
        if self.closing:
            return _decision("defer", CLOSING)
        call_id = str(body.get("tool_use_id") or "")
        call = self._call(call_id)
        if run.marks and not call.marks:
            try:
                await asyncio.wait_for(call.marked.wait(), MARK_WAIT_S)
            except TimeoutError:
                pass
        if self.closing:
            return _decision("defer", CLOSING)
        call.decided = True
        if never := next((m for m in call.marks if m.kind == "never"), None):
            self._courier(run, call_id, never, body)
            return _decision("deny", NEVER)

        tool, tool_input, cwd = _call_of(body)
        try:
            file_hash = approvals.file_hash(tool, tool_input, self.lanes[run.lane].work_mount)
            digest = approvals.digest(tool, tool_input, cwd, file_hash)
        except Unbindable as exc:
            log.warning("call %s: %s", call_id, exc)
            file_hash = digest = None
        if found := self.store.approvals(tool_use_id=call_id, kind="approval"):
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
        if digest is not None and (run.session, digest) in self._reasks:
            return self._to_owner(run, call_id, body, file_hash, digest)
        if call.marks:
            return {}  # an always rule matched: the CLI's own rule decides
        if digest is None:
            return _decision("deny", UNBINDABLE)
        return self._to_owner(run, call_id, body, file_hash, digest)

    def _to_owner(self, run: Run, call_id: str, body: dict, file_hash: str, digest: str) -> dict:
        tool, tool_input, cwd = _call_of(body)
        self.store.add_approval(run.session, run.id, call_id, "approval", tool, tool_input, cwd, file_hash, digest,
                                self.clock())
        return _decision("defer", DEFERRED)

    def permission_request(self, run: Run, body: dict) -> dict:
        """The CLI asks although a marker said a rule decides (a compound command, a path outside the directory):
        denied, and the same call made again goes to the owner."""
        tool, tool_input, cwd = _call_of(body)
        try:
            file_hash = approvals.file_hash(tool, tool_input, self.lanes[run.lane].work_mount)
        except Unbindable:
            return _ask_denied(UNBINDABLE)
        self._reasks.add((run.session, approvals.digest(tool, tool_input, cwd, file_hash)))
        return _ask_denied(ASKED)

    def post_tool_use(self, body: dict) -> None:
        call_id = str(body.get("tool_use_id") or "")
        for approval in self.store.approvals(tool_use_id=call_id, kind="approval", states=("executing",)):
            self.store.move(approval["id"], ("executing",), "executed", self.clock())

    def _courier(self, run: Run, call_id: str, mark: Mark, body: dict) -> None:
        """A never-list call: no approval of it may stay grantable, and the owner gets it once as a courier card."""
        if self.store.approvals(tool_use_id=call_id, kind="courier"):
            return
        now = self.clock()
        for approval in self.store.approvals(tool_use_id=call_id, kind="approval",
                                             states=("pending", "shown", "granted")):
            if self.store.move(approval["id"], (approval["state"],), "dropped", now) and approval["state"] == "shown":
                retire(self.store, {**approval, "state": "dropped"}, "Снято: это делает только Владелец — "
                                                                     "ушло карточкой курьера", now)
        tool, tool_input, cwd = _call_of(body)
        call = {"tool": tool, "input": tool_input, "cwd": cwd}
        view = dataclasses.asdict(approvals.courier_card(call, run.session, run.track, mark.label))
        courier = self.store.add_approval(run.session, run.id, call_id, "courier", tool, tool_input, cwd, "", "", now,
                                          state="dropped", view=view)
        self.store.add_note("card", run.session, {"view": view, "buttons": []}, approval=courier)


def _call_of(body: dict) -> tuple[str, dict, str]:
    tool_input = body.get("tool_input")
    return (str(body.get("tool_name") or ""), tool_input if isinstance(tool_input, dict) else {},
            str(body.get("cwd") or ""))
