"""A session's lifecycle (`retinue/lifecycle.py`) as a transition table: what happened, the state the session was in,
the state it leaves — and what the move does to its cards, its turn's start, the face's list and its parent. On the
store alone: the platform's wiring of each event is `test_platformd`'s, the reports' own rules `test_subs`'."""

from __future__ import annotations

import asyncio
import json

import pytest

from retinue import lifecycle
from retinue.lifecycle import Lifecycle
from retinue.sessions import PlatformStore

T0 = 1_000_000.0  # when each session was made, and its last move before the one under test
NOW = T0 + 3600
DAY = 86400


class Subs:
    """What of the sub-sessions a move asks for, recorded."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, str, bool, str | None]] = []
        self.stops: list[str] = []

    def turn_ended(self, session: dict, text: str, failed: bool = False, run: str | None = None) -> None:
        self.reports.append((session["id"], text, failed, run))

    def stopped(self, session: dict) -> None:
        self.stops.append(session["id"])


class World:
    def __init__(self, tmp_path) -> None:
        self.store = PlatformStore(str(tmp_path / "platform.sqlite"), clock=lambda: NOW)
        self.subs = Subs()
        self.interrupted: list[str] = []

        async def interrupt(session: dict) -> None:
            self.interrupted.append(session["id"])

        self.life = Lifecycle(self.store, self.subs, interrupt)

    def session(self, state: str, kind: str = "private", lane: str = "room", archived: bool = False) -> str:
        """A session in `state`, made and last moved at T0; kind `sub` is a private sub-session."""
        parent = self.store.new_session("notes", lane, "private", T0) if kind == "sub" else None
        session = self.store.new_session("notes", lane, "private" if kind == "sub" else kind, T0, sub_of=parent)
        self.store.update_session(session, T0, state=state, turn_started=T0, idle_since=T0 if state == "idle" else None,
                                  archived=T0 if archived else None)
        return session

    def card(self, session: str, state: str = "shown", at: float = T0) -> str:
        return self.store.add_approval(session, "r1", f"toolu_{state}", "approval", "Bash", {"command": "ls"}, "/work",
                                       "", "d", at, state=state, view={"head": ["Allow?"], "body": "ls"})

    def row(self, session: str) -> dict:
        turn_started = self.store.db.execute("SELECT turn_started FROM sessions WHERE id = ?", (session,)).fetchone()[0]
        return {**self.store.session(session), "turn_started": turn_started}

    def updates(self) -> list[tuple[str, str]]:
        rows = self.store.db.execute("SELECT approval, data FROM notes WHERE kind = 'update' ORDER BY seq").fetchall()
        return [(approval, json.loads(data)["view"]["status"]) for approval, data in rows]


BEGINS = {"queued", "decided", "started"}
EVENTS = {
    "queued": lambda w, row: w.life.queued(row["id"], NOW),
    "decided": lambda w, row: w.life.decided(row["id"], NOW),
    "started": lambda w, row: w.life.started(row["id"], NOW),
    "deferred, its card shown": lambda w, row: w.life.deferred(row["id"], NOW, shown=True),
    "deferred, no card": lambda w, row: w.life.deferred(row["id"], NOW, shown=False),
    "turn ended": lambda w, row: w.life.turn_ended(row, "done", NOW, run="r1"),
    "turn failed": lambda w, row: w.life.turn_ended(row, "broke", NOW, failed=True, run="r1"),
    "run ended": lambda w, row: w.life.run_ended(row["id"], NOW),
    "owner stopped": lambda w, row: w.life.owner_stopped(row, NOW),
    "made empty": lambda w, row: w.life.empty(row["id"], NOW),
    "made twice": lambda w, row: w.life.duplicate(row["id"], NOW),
}
TABLE = [
    # what happened, the session's kind, the state it was in, the state it leaves
    *[(event, "private", before, "active") for event in sorted(BEGINS)
      for before in ("idle", "closed", "waiting", "active")],
    ("deferred, its card shown", "private", "active", "waiting"),
    ("deferred, no card", "private", "active", "idle"),
    ("turn ended", "private", "active", "idle"),
    ("turn ended", "code", "active", "closed"),
    ("turn ended", "sub", "active", "closed"),
    ("turn failed", "private", "active", "idle"),
    ("turn failed", "code", "active", "idle"),
    ("turn failed", "sub", "active", "closed"),
    ("turn failed", "private", "waiting", "idle"),  # a fork refused, a card expired
    ("run ended", "private", "active", "idle"),
    ("run ended", "private", "waiting", "waiting"),  # its card still asks
    ("run ended", "private", "idle", "idle"),
    ("run ended", "sub", "closed", "closed"),  # stopped by the owner: closed already
    ("owner stopped", "private", "active", "active"),  # the lane's `stopped` line moves it (`run ended`)
    ("owner stopped", "sub", "active", "closed"),
    ("owner stopped", "sub", "waiting", "closed"),
    ("made empty", "private", "active", "idle"),
    ("made twice", "private", "active", "closed"),
]


@pytest.mark.parametrize("event, kind, before, after", TABLE)
def test_a_move_leaves_the_state_its_event_names(tmp_path, event, kind, before, after):
    world = World(tmp_path)
    session = world.session(before, kind, archived=True)
    EVENTS[event](world, world.row(session))
    row = world.row(session)
    assert row["state"] == after
    if before != after:
        assert row["idle_since"] == (NOW if after == "idle" else None), "idle counts from the move to idle"
    began = event in BEGINS
    assert row["turn_started"] == (NOW if began and before in ("idle", "closed") else T0), \
        "a new turn from idle or closed; a resume after a card, or a run of a turn already on, is the same turn"
    assert row["archived"] == (None if began else T0), "a turn beginning brings an archived session back"
    reported = [(session, text, failed) for session, text, failed, _ in world.subs.reports]
    if kind == "sub" and event.startswith("turn"):
        assert reported == [(session, "done" if event == "turn ended" else "broke", event == "turn failed")]
    else:
        assert reported == [], "only a sub-session's turn is reported, and only when it is over"


def slot(world: World, session: str) -> int | None:
    return world.store.db.execute("SELECT slot FROM sessions WHERE id = ?", (session,)).fetchone()[0]


@pytest.mark.parametrize("event", sorted(BEGINS))
def test_a_turn_beginning_in_an_archived_session_takes_its_place_on_the_map_again(tmp_path, event):
    world = World(tmp_path)
    first, second = world.session("idle"), world.session("idle")
    assert (slot(world, first), slot(world, second)) == (0, 1)
    world.store.archive(first, T0)
    assert slot(world, first) is None, "freed when archived"
    EVENTS[event](world, world.row(first))
    assert world.row(first)["archived"] is None and slot(world, first) == 0, "the lowest place free now"
    EVENTS[event](world, world.row(second))
    assert slot(world, second) == 1, "a session not archived keeps its own place"


def test_a_new_session_made_twice_is_no_spheres_main_session(tmp_path):
    world = World(tmp_path)
    session = world.store.new_session("notes", "room", "private", T0, sphere="travel", main=True)
    assert world.row(session)["main"] == 1
    world.life.duplicate(session, NOW)
    assert world.row(session)["main"] == 0


def test_the_owners_word_stales_every_card_still_asking_and_rewrites_the_shown_ones(tmp_path):
    world = World(tmp_path)
    session = world.session("waiting")
    shown, pending, granted = world.card(session), world.card(session, "pending"), world.card(session, "granted")
    world.life.owner_wrote(session, NOW)
    states = {a["id"]: a["state"] for a in world.store.approvals(session=session)}
    assert states == {shown: "stale", pending: "stale", granted: "granted"}
    assert world.updates() == [(shown, "Expired: you wrote a new message")], "a pending card was never shown"
    assert world.row(session)["state"] == "waiting", "his message is queued as a turn of its own (`queued`)"


def test_a_card_withdrawn_twice_moves_once(tmp_path):
    world = World(tmp_path)
    card = world.store.approval(world.card(world.session("waiting")))
    assert lifecycle.withdraw(world.store, card, "stale", "first", NOW)
    assert not lifecycle.withdraw(world.store, card, "expired", "second", NOW)
    assert world.store.approval(card["id"])["state"] == "stale" and world.updates() == [(card["id"], "first")]


@pytest.mark.parametrize("running", [True, False])
def test_a_halt_drops_the_queue_stales_the_cards_interrupts_the_run_and_closes(tmp_path, running):
    world = World(tmp_path)
    session = world.session("active")
    card = world.card(session)
    world.store.enqueue(session, "next", T0)
    if running:
        world.store.add_run(session, "token", [], T0)
    asyncio.run(world.life.halt(world.row(session), NOW))
    assert world.subs.stops == [session], "what its family owes goes"
    assert world.store.queued() == []
    assert world.updates() == [(card, lifecycle.STOPPED)]
    assert world.interrupted == ([session] if running else [])
    assert world.row(session)["state"] == "closed"


def test_archive_halts_the_kin_still_open_and_leaves_a_closed_one_be(tmp_path):
    world = World(tmp_path)
    parent = world.session("waiting")
    card = world.card(parent)
    open_child = world.store.new_session("notes", "room", "private", T0, sub_of=parent)
    done_child = world.store.new_session("notes", "room", "private", T0, sub_of=parent)
    world.store.update_session(done_child, T0, state="closed")
    assert asyncio.run(world.life.archive(parent, NOW))
    assert sorted(world.subs.stops) == sorted([parent, open_child])
    assert world.subs.stops[-1] == parent, "its kin first: nothing they say after it starts a turn of it"
    assert world.updates() == [(card, lifecycle.ARCHIVED)]
    assert {s: world.row(s)["state"] for s in (parent, open_child, done_child)} == dict.fromkeys(
        (parent, open_child, done_child), "closed")
    assert all(world.row(s)["archived"] == NOW for s in (parent, open_child, done_child))
    assert not asyncio.run(world.life.archive("nosuch", NOW))


def test_a_week_idle_closes_a_session(tmp_path):
    world = World(tmp_path)
    session = world.session("idle")
    world.life.tick(T0 + lifecycle.IDLE_CLOSE_S - 1)
    assert world.row(session)["state"] == "idle"
    world.life.tick(T0 + lifecycle.IDLE_CLOSE_S + 1)
    assert world.row(session)["state"] == "closed"


@pytest.mark.parametrize("kind, before, after", [("private", "waiting", "idle"), ("code", "waiting", "idle"),
                                                 ("sub", "waiting", "closed"), ("private", "active", "active")])
def test_a_card_unpressed_for_a_week_expires_and_ends_the_turn_it_held(tmp_path, kind, before, after):
    world = World(tmp_path)
    session = world.session(before, kind)
    card = world.card(session)
    world.life.tick(T0 + lifecycle.CARD_TTL_S - 1)
    assert world.store.approval(card)["state"] == "shown"
    world.life.tick(T0 + lifecycle.CARD_TTL_S)
    assert world.store.approval(card)["state"] == "expired"
    assert world.updates() == [(card, "Expired: no decision for seven days")]
    assert world.row(session)["state"] == after
    if kind == "sub":
        assert world.subs.reports == [(session, "The card expired: no decision for seven days, the turn is not "
                                                "finished.", True, f"expired:{card}")]


def test_a_lane_gone_from_the_settings_stales_its_cards_and_its_sessions_stop_waiting(tmp_path):
    world = World(tmp_path)
    gone, live = world.session("waiting", lane="lane-web"), world.session("waiting", lane="room")
    shown, pending, kept = world.card(gone), world.card(gone, "pending"), world.card(live)
    world.life.lanes_gone({"room"}, NOW)
    assert [world.store.approval(a)["state"] for a in (shown, pending, kept)] == ["stale", "stale", "shown"]
    assert world.updates() == [(shown, "Expired: the lane is retired")]
    assert (world.row(gone)["state"], world.row(live)["state"]) == ("idle", "waiting")
    assert world.subs.reports == [], "not a turn's end: nothing could take its answer"
