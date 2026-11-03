"""A session's lifecycle (`Platform.life`): the state the face, the router and the queue read, and what each move
takes with it. Callers say what happened; this module knows which state that leaves, which of the session's cards stop
asking, when a sub-session's turn is over for its parent, and that a turn beginning brings an archived session back.

- **active** — a turn is queued or running: the owner's message, a parent's, a report, a follow-up, a source check
  (`queued`), a card decided (`decided`), a run the lane took (`started`). From idle, closed or new it is a new turn
  (`turn_started`); from waiting it is the same turn resumed, which moves only `at`.
- **waiting** — the run ended on a deferred call whose card the owner was shown (`deferred`).
- **idle** — the turn is over (`turn_ended`), or its run is gone without an end (`run_ended`): it waits for him.
- **closed** — a code session's summary, any turn of a sub-session (a message opens it again), the owner's stop of a
  sub-session (`owner_stopped`), a halt (`halt`, `archive`), a week idle (`tick`).

A card that stops asking is withdrawn (`withdraw`): moved to its new state, and rewritten in Telegram with the reason
when the owner was shown it. His word stales them all (`owner_wrote`); a week unpressed expires one and ends the turn
it held (`tick`); a lane gone from the settings stales its sessions' at start (`lanes_gone`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from . import questions
from .gate import retire
from .sessions import PlatformStore

if TYPE_CHECKING:
    from .subs import Subs

OPEN = ("pending", "shown")  # a card still asking: shown to the owner, or about to be
CARD_TTL_S = 7 * 86400       # a card nobody pressed expires
IDLE_CLOSE_S = 7 * 86400     # an idle session closes
STOPPED = "Expired: you stopped the session"
ARCHIVED = "Expired: you archived the session"


def withdraw(store: PlatformStore, approval: dict, to: str, why: str, now: float) -> bool:
    """A card that no longer asks: moved from the state it was read in to `to`, and — when the owner was shown it —
    rewritten with `why` and no buttons. True when this call moved it."""
    if not store.move(approval["id"], (approval["state"],), to, now):
        return False
    if approval["state"] == "shown":
        retire(store, {**approval, "state": to}, why, now)
    return True


class Lifecycle:
    def __init__(self, store: PlatformStore, subs: Subs, interrupt: Callable[[dict], Awaitable[None]]) -> None:
        """`subs`: a sub-session's report and the owner's stop of a family (`Subs.turn_ended`, `Subs.stopped`).
        `interrupt`: the running turn of a session stopped by its lane."""
        self.store, self.subs, self.interrupt = store, subs, interrupt

    # --- a turn begins or goes on -------------------------------------------------------------------------------

    def queued(self, session: str, now: float) -> None:
        """A turn waits for its run: a message into the session — the owner's, a parent's, a report, a follow-up's,
        a source check's."""
        self._active(session, now)

    def decided(self, session: str, now: float) -> None:
        """The owner decided its card or answered its question: the turn goes on with a bare resume."""
        self._active(session, now)

    def started(self, session: str, now: float) -> None:
        """The lane took the session's run."""
        self._active(session, now)

    def working(self, session: str) -> bool:
        """A turn is queued or running: neither waiting on a card nor over."""
        return (self.store.session(session) or {}).get("state") == "active"

    # --- a new session ------------------------------------------------------------------------------------------

    def empty(self, session: str, now: float) -> None:
        """A session made with nothing to run waits for the owner's first message."""
        self._move(session, "idle", now)

    def duplicate(self, session: str, now: float) -> None:
        """A session made for a message another copy of it already started one for: closed, and no sphere's main
        session."""
        self._move(session, "closed", now)
        self.store.set_main(session, False)

    # --- the run's end ------------------------------------------------------------------------------------------

    def deferred(self, session: str, now: float, shown: bool) -> None:
        """The run ended on a deferred call: waiting while its card is shown to the owner, idle when none is — the
        call is asked again by a resume, or went to him as a courier card."""
        self._move(session, "waiting" if shown else "idle", now)

    def turn_ended(self, session: dict, text: str, now: float, failed: bool = False, run: str | None = None) -> None:
        """The turn is over, however it ended — its summary, an error, a refusal, a card expired, a tool gone: idle,
        and a code session closed on its summary. A sub-session closes on any end (a message opens it again), and its
        parent is owed `text` as a report when the turn was the parent's doing (`Subs.turn_ended`)."""
        if session.get("sub_of"):
            self.subs.turn_ended(session, text, failed, run)
            self._move(session["id"], "closed", now)
        else:
            self._move(session["id"], "closed" if session["kind"] == "code" and not failed else "idle", now)

    def run_ended(self, session: str, now: float) -> None:
        """The run's process is gone with the turn not over — a crash, a kill, the owner's stop, a restart before the
        CLI named the session: a working session goes idle and waits for him. A waiting one keeps its card; a
        finished one has said so."""
        if self.working(session):
            self._move(session, "idle", now)

    # --- the owner ----------------------------------------------------------------------------------------------

    def owner_wrote(self, session: str, now: float) -> None:
        """His new message: every card of the session still asking goes stale, so the call it asked about is not
        run."""
        self._withdraw_open(session, "Expired: you wrote a new message", now)

    def owner_stopped(self, session: dict, now: float) -> bool:
        """His «Stop» on a session whose lane is live: what its family owes dropped (`Subs.stopped`), and a
        sub-session closed at once, whether its run's end comes later or came already. True when it was."""
        self.subs.stopped(session)
        if not session.get("sub_of"):
            return False
        self._move(session["id"], "closed", now)
        return True

    async def halt(self, session: dict, now: float, why: str = STOPPED) -> None:
        """A session ended where it stands: what its family owes dropped (`Subs.stopped`), its queued runs dropped,
        its cards stale with `why`, its running turn interrupted, and it closed."""
        self.subs.stopped(session)
        self.store.db.execute("DELETE FROM queue WHERE session = ? AND attempt IS NULL", (session["id"],))
        self.store.db.commit()
        self._withdraw_open(session["id"], why, now)
        if any(r["session"] == session["id"] for r in self.store.open_runs()):
            await self.interrupt(session)
        self._move(session["id"], "closed", now)

    async def archive(self, session: str, now: float) -> bool:
        """«Archive»: the session and its sub-sessions and side questions out of the face's list, each one still
        open halted. A turn beginning in one brings it back (`queued`). False when there is no such session."""
        ids = self.store.archive(session, now)
        for id_ in reversed(ids):  # its kin first: nothing they say after it starts a turn of it
            row = self.store.session(id_)
            if row is not None and row["state"] != "closed":
                await self.halt(row, now, ARCHIVED)
        return bool(ids)

    # --- the clock and the start --------------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        """A session idle for a week closes; a card unpressed for a week expires, and the turn it held is over."""
        for session in self.store.sessions_in("idle", idle_before=now - IDLE_CLOSE_S):
            self._move(session, "closed", now)
        for approval in self.store.approvals(kind=questions.KINDS, states=("shown",)):
            if approval["at"] > now - CARD_TTL_S or \
                    not withdraw(self.store, approval, "expired", "Expired: no decision for seven days", now):
                continue
            row = self.store.session(approval["session"])
            if row["state"] == "waiting":
                self.turn_ended(row, "The card expired: no decision for seven days, the turn is not finished.", now,
                                failed=True, run=f"expired:{approval['id']}")

    def lanes_gone(self, live: set[str], now: float) -> None:
        """At start: every card still asking in a session of a lane not in `live` goes stale, and its session stops
        waiting — nothing could take its answer."""
        for approval in self.store.approvals(kind=questions.KINDS, states=OPEN):
            row = self.store.session(approval["session"])
            if row is None or row["lane"] in live:
                continue
            withdraw(self.store, approval, "stale", "Expired: the lane is retired", now)
            if row["state"] == "waiting":
                self._move(row["id"], "idle", now)

    # --- the moves ----------------------------------------------------------------------------------------------

    def _withdraw_open(self, session: str, why: str, now: float) -> None:
        for approval in self.store.approvals(session=session, kind=questions.KINDS, states=OPEN):
            withdraw(self.store, approval, "stale", why, now)

    def _active(self, session: str, now: float) -> None:
        """A turn begins — from idle, closed or new it is a new one, and `turn_started` moves (the face's «Working»
        counts from it) — or goes on; either way the session is back in the face's list."""
        fields: dict = {"archived": None}
        if (self.store.session(session) or {}).get("state") not in ("active", "waiting"):
            fields["turn_started"] = now
        self._move(session, "active", now, **fields)

    def _move(self, session: str, state: str, now: float, **fields) -> None:
        self.store.update_session(session, now, state=state, idle_since=now if state == "idle" else None, **fields)
