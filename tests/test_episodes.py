"""Turns that went badly (`retinue/episodes.py`), marked by the platform's own events on the turn of the run they
happened in: a stop, an error, a denied card, a loop, a request asked again — with the turn's summary and the owner's
correction, kept past the raw lines' 90 days while marked."""

from __future__ import annotations

import json

from retinue import episodes, signals
from retinue.sessions import PlatformStore
from test_platformd import lane_event, short_wait  # noqa: F401 — the fixture is autouse
from test_subs import CLI, SubsWorld, subs_scenario

INIT = json.dumps({"type": "system", "subtype": "init", "session_id": CLI, "cwd": "/work", "permissionMode": "auto"})
RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
                     "result": "ok", "session_id": CLI})


def rows(store: PlatformStore) -> list[tuple]:
    return list(store.db.execute("SELECT session, turn, why, correction FROM episodes ORDER BY at"))


def summary(store: PlatformStore, session: str, turn: int) -> str | None:
    row = store.db.execute("SELECT text FROM episode_summaries WHERE session = ? AND turn = ?", (session, turn)).fetchone()
    return row[0] if row else None


def test_same_request_is_most_words_shared_not_the_same_text_or_a_short_one():
    assert episodes.same_request("найди недорогие кофемолки с доставкой до пятницы",
                                 "найди кофемолки подешевле с доставкой до пятницы, без б/у")
    assert not episodes.same_request("найди недорогие кофемолки", "найди недорогие кофемолки")
    assert not episodes.same_request("да", "да, давай")
    assert not episodes.same_request("найди недорогие кофемолки с доставкой", "напиши письмо про ремонт крыши")


def test_a_mark_lands_on_the_turn_of_its_run_not_on_the_newest_one():
    store = PlatformStore(":memory:")
    episodes.prepare(store, 0.0)
    session = store.new_session("notes", "private", "private", 0.0)
    first = store.add_run(session, "h1", [], 0.0)
    store.add_raw("private", 1, session, first, INIT, 0.0)
    store.add_raw("private", 2, session, first, RESULT, 0.0)
    store.end_run(first, 1.0)
    second = store.add_run(session, "h2", [], 2.0)
    assert episodes.turn_of(store, session, first) == 0
    assert episodes.turn_of(store, session) == 1, "the open run, before its first line"
    episodes.mark(store, session, episodes.CARD_REJECTED, 3.0, run=first)
    episodes.mark(store, session, episodes.LOOP, 3.0, run=second)
    store.end_run(second, 4.0)
    episodes.mark(store, session, episodes.OWNER_STOP, 5.0)  # no run open: nothing to mark
    assert [(t, w) for _, t, w, _ in rows(store)] == [(0, episodes.CARD_REJECTED), (1, episodes.LOOP)]


def test_the_owners_stop_marks_the_running_turn_its_error_is_not_another_mark_and_his_next_word_corrects_it(tmp_path):
    async def body(world: SubsWorld):
        session = await world.parent("сравни тарифы хостинга для сайта")
        run = world.run_of(session)
        await world.platform.stop(session)
        world.lanes["private"].add(session, run["run"], INIT, json.dumps(
            {"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": CLI}),
                                   lane_event("stopped"))
        world.lanes["private"].exited(session, run["run"])
        await world.poll()
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "нужны только тарифы с IPv6"})
        assert rows(world.store) == [(session, 0, episodes.OWNER_STOP, "нужны только тарифы с IPv6")]
        await world.turn(session, "Сравнил только с IPv6.")
        await world.platform.stop(session)  # nothing running now
        assert len(rows(world.store)) == 1
    subs_scenario(tmp_path, body)


def test_an_error_and_its_request_asked_again_soon_mark_the_turn_and_its_summary_is_kept(tmp_path):
    async def body(world: SubsWorld):
        session = await world.parent("сравни тарифы хостинга для сайта на год")
        await world.turn(session, "boom", ok=False)
        world.clock.now += 60
        await world.owner("input", {"input_id": "tg:2", "session": session,
                                    "text": "сравни тарифы хостинга для сайта на год, ещё раз"})
        [(_, turn, why, correction)] = rows(world.store)
        assert turn == 0 and why == f"{episodes.RUN_ERROR},{episodes.ASKED_AGAIN}"
        assert correction.endswith("ещё раз")
    subs_scenario(tmp_path, body)


def test_an_answer_to_the_sessions_question_and_a_late_rewording_are_not_asked_again(tmp_path):
    async def body(world: SubsWorld):
        session = await world.parent("сравни тарифы хостинга для сайта на год")
        await world.turn(session, "Какой бюджет и нужен ли IPv6?")
        world.clock.now += 60
        await world.owner("input", {"input_id": "tg:2", "session": session,
                                    "text": "тарифы хостинга для сайта на год, бюджет до 50 евро"})
        await world.turn(session, "Сравнил.")
        world.clock.now += episodes.AGAIN_S + 1
        await world.owner("input", {"input_id": "tg:3", "session": session,
                                    "text": "тарифы хостинга для сайта на год, бюджет до 50 евро, подробнее"})
        assert rows(world.store) == []
    subs_scenario(tmp_path, body)


def test_marked_turns_keep_their_raw_lines_and_summary_past_ninety_days_and_count_in_the_week(tmp_path):
    async def body(world: SubsWorld):
        a = await world.parent("первая задача про кофемолки")
        await world.turn(a, "Готово.")
        b = await world.parent("вторая задача про чайники")
        await world.turn(b, "Готово.", ok=False)
        world.clock.now += 91 * 86400
        world.store.prune(world.clock.now)
        episodes.trim(world.store, world.clock.now)
        kept = {s for (s,) in world.store.db.execute("SELECT DISTINCT session FROM raw WHERE turn = 0")}
        assert b in kept and a not in kept
        assert summary(world.store, b, 0) and summary(world.store, a, 0) is None
        week = signals.tally(world.store, 0, world.clock.now)["episodes"]
        assert week == {"turns": 1, "by_mark": {episodes.RUN_ERROR: 1}}
    subs_scenario(tmp_path, body)


def test_rows_past_keep_or_keep_days_go_on_a_trim(tmp_path):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    episodes.prepare(store, 0.0)
    for n in range(episodes.KEEP + 5):
        session = store.new_session("notes", "private", "private", float(n))
        episodes.mark(store, session, episodes.LOOP, float(n), turn=0)
    assert store.db.execute("SELECT COUNT(*), MIN(at) FROM episodes").fetchone() == (episodes.KEEP, 5.0)
    episodes.trim(store, episodes.KEEP_DAYS * 86400 + 100.0)
    assert store.db.execute("SELECT COUNT(*), MIN(at) FROM episodes").fetchone() == (episodes.KEEP + 5 - 100, 100.0)
