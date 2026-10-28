"""The platform's SQLite: inputs once, a lane's replay stored once, one of two racing moves, the notes journal, and a
projection that rebuilds the same as it grew and outlives the raw lines it came from."""

from __future__ import annotations

from pathlib import Path

from retinue import sessions
from retinue.sessions import PlatformStore

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
DAY = 86400


def raw(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text().splitlines()


def feed(store: PlatformStore, session: str, runs: list[tuple[str, list[str]]], now: float = 0.0,
         seq: int = 0) -> list[int | None]:
    """Lines as a lane journals them, one run after another, each re-projected as it is stored."""
    turns = []
    for run, lines in runs:
        for line in lines:
            seq += 1
            turn = store.add_raw("code", seq, session, run, line, now)
            if turn is not None:
                store.reproject(session, turn)
            turns.append(turn)
    return turns


def test_an_input_is_taken_once(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    assert store.add_input("tg:7", session, "почини CI", 1.0) is True
    assert store.add_input("tg:7", session, "почини CI", 2.0) is False


def test_a_lane_replay_stores_nothing_twice_and_turns_follow_results_and_runs(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    deferred, resumed = raw("deferred"), raw("resumed")
    turns = feed(store, session, [("r1", deferred), ("r2", resumed)])
    assert turns == [0] * len(deferred) + [1] * (len(resumed) - 1) + [2], "a result closes a turn"
    assert feed(store, session, [("r1", deferred), ("r2", resumed)]) == [None] * (len(deferred) + len(resumed))
    assert store.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == len(deferred) + len(resumed)
    assert feed(store, session, [("r3", raw("reads"))], seq=100)[0] == 3, \
        "a new run closes the turn the last one left open"


def test_the_projection_rebuilt_from_raw_equals_the_one_that_grew(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    feed(store, session, [("r1", raw("deferred")), ("r2", raw("resumed")), ("r3", raw("edit")),
                          ("r4", raw("subagent"))])
    grown = store.items(session)
    store.db.execute("DELETE FROM items")
    store.rebuild(session)
    assert store.items(session) == grown
    resumed_call = next(i for i in grown if i["turn"] == 1)
    assert resumed_call["kind"] == "tool" and resumed_call["data"]["tool"] == "Bash", \
        "the resumed call is named from the deferred result before it"


def test_of_two_racing_moves_exactly_one_wins(tmp_path):
    path = str(tmp_path / "p.sqlite")
    press, owner_wrote = PlatformStore(path), PlatformStore(path)
    approval = press.add_approval("s1", "r1", "toolu_1", "approval", "Bash", {"command": "make"}, "/work", "", "d",
                                  0.0, state="shown")
    assert press.move(approval, ("shown",), "granted", 1.0) is True
    assert owner_wrote.move(approval, ("shown",), "stale", 1.0) is False
    assert owner_wrote.approval(approval)["state"] == "granted"
    assert press.move(approval, ("granted",), "executing", 2.0) and not press.move(approval, ("granted",), "executing", 3.0)


def test_a_button_is_spent_once_and_not_after_it_expired(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    button = store.add_button("a1", "approve", "d", expires=10.0)
    late = store.add_button("a1", "reject", "d", expires=10.0)
    assert button.startswith("p.") and len(button) == 18
    assert store.spend_button(button, 5.0) == {"approval": "a1", "action": "approve", "digest": "d"}
    assert store.spend_button(button, 6.0) is None
    assert store.spend_button(late, 10.0) is None


def test_notes_are_looked_at_and_confirmed_by_number(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    first = store.add_note("card", "s1", {"view": {"head": ["x"]}}, approval="a1")
    second = store.add_note("summary", "s1", {"text": "готово"})
    assert [n["seq"] for n in store.look()] == [first, second]
    assert store.look(1)[0] == {"seq": first, "kind": "card", "session": "s1", "approval": "a1",
                                "data": {"view": {"head": ["x"]}}}
    assert store.confirm(first) == 1
    assert [n["seq"] for n in PlatformStore(str(tmp_path / "p.sqlite")).look()] == [second], "survives a restart"


def test_prune_drops_old_raw_turns_and_keeps_their_projection(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    feed(store, session, [("r1", raw("edit"))], now=0.0)
    feed(store, session, [("r2", raw("reads")[:3])], now=0.0, seq=100)
    projected = store.items(session)
    assert store.prune(sessions.RAW_KEEP_S + DAY) == len(raw("edit")), "the open turn keeps its lines, however old"
    store.rebuild(session)
    assert store.items(session) == projected
    assert feed(store, session, [("r2", raw("reads")[3:])], now=sessions.RAW_KEEP_S, seq=103)[-1] == 1, \
        "the turns are numbered on, not again from what is left"


def test_a_resume_rebuilt_after_its_deferred_turn_was_pruned_keeps_its_call(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    feed(store, session, [("r1", raw("deferred"))], now=0.0)
    feed(store, session, [("r2", raw("resumed"))], now=sessions.RAW_KEEP_S, seq=100)
    projected = store.items(session)
    assert store.prune(sessions.RAW_KEEP_S + DAY) == len(raw("deferred"))
    store.rebuild(session)
    assert store.items(session) == projected
    resumed_call = next(i for i in projected if i["turn"] == 1)
    assert resumed_call["data"]["tool"] == "Bash" and resumed_call["data"]["input"]


def test_a_run_is_found_by_its_hook_token_and_a_session_updated(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    run = store.add_run(session, "h1", [{"kind": "allow", "rule": "Read", "label": ""}], 1.0)
    assert store.run_of("h1") == {"id": run, "session": session,
                                  "marks": [{"kind": "allow", "rule": "Read", "label": ""}], "ended": None,
                                  "mode": None}
    assert store.run_of("h2") is None
    store.update_session(session, 2.0, state="waiting", cli_session="c1")
    assert {k: store.session(session)[k] for k in ("state", "cli_session", "at")} == \
        {"state": "waiting", "cli_session": "c1", "at": 2.0}


def test_a_database_from_before_pins_and_side_sessions_gets_their_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, track TEXT NOT NULL, lane TEXT NOT NULL, "
                "kind TEXT NOT NULL, state TEXT NOT NULL, cli_session TEXT NOT NULL DEFAULT '', "
                "cli_version TEXT NOT NULL DEFAULT '', created REAL NOT NULL, at REAL NOT NULL, idle_since REAL, "
                "turn INTEGER NOT NULL DEFAULT 0, turn_run TEXT)")
    old.execute("INSERT INTO sessions (id, track, lane, kind, state, created, at) VALUES "
                "('ab12cd34', 'retinue', 'code', 'code', 'idle', 1, 1)")
    old.commit()
    old.close()
    store = PlatformStore(path)
    assert store.face_session("ab12cd34")["pinned"] is False and store.face_session("ab12cd34")["parent"] is None
    assert store.session("ab12cd34")["context_tokens"] is None
    assert store.set_pinned("ab12cd34", True) and store.face_session("ab12cd34")["pinned"] is True
    PlatformStore(path)  # opened again: nothing to add


def test_a_database_from_before_auto_gets_the_runs_mode_and_the_cards_reason(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, session TEXT NOT NULL, hook_token_hash TEXT NOT NULL UNIQUE, "
                "marks TEXT NOT NULL, started REAL NOT NULL, ended REAL)")
    old.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, session TEXT NOT NULL, run TEXT NOT NULL, "
                "tool_use_id TEXT NOT NULL, kind TEXT NOT NULL, tool TEXT NOT NULL, input TEXT NOT NULL, "
                "cwd TEXT NOT NULL, file_hash TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, "
                "view TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, at REAL NOT NULL)")
    old.execute("INSERT INTO runs VALUES ('r1', 's1', 'h1', '[]', 1, NULL)")
    old.execute("INSERT INTO approvals VALUES ('a1', 's1', 'r1', 'toolu_1', 'approval', 'Bash', '{}', '/work', '', "
                "'d', 'shown', '{}', 1, 1)")
    old.commit()
    old.close()
    store = PlatformStore(path)
    assert store.run_of("h1")["mode"] is None and store.approval("a1")["why"] == ""
    PlatformStore(path)


def test_a_session_is_told_once_that_its_cli_did_not_start_in_auto(tmp_path):
    """`set_run_mode` says when to tell: the first run of a session whose init named another mode — not a later run,
    not the same line ingested again."""
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    session, other = store.new_session("notes", "private", "private", 0.0), store.new_session("notes", "private",
                                                                                             "private", 0.0)
    auto, manual, again = (store.add_run(session, f"h{n}", [], 1.0) for n in range(3))
    assert store.set_run_mode(auto, "auto") is False
    assert store.set_run_mode(manual, "default") is True
    assert store.set_run_mode(manual, "default") is False, "the line again: the mode is set once"
    assert store.set_run_mode(again, "default") is False, "the session was told"
    assert store.set_run_mode(store.add_run(other, "h9", [], 1.0), "default") is True, "another session is told"
    assert store.run_of("h0")["mode"] == "auto" and store.run_of("h1")["mode"] == "default"
    assert store.set_run_mode("nope", "default") is False
