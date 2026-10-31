"""Pings: the loudness rule and the header, a ping's life on the platform — its kind, how long its turn took, where
it is read — and in the bridge: its parts, its sound and its «To the Base»/«Push» buttons."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from retinue import pings
from retinue.bridge import PING, SESSION, ping_parts
from retinue.channels.telegram import TelegramError
from retinue.config import Sphere
from retinue.render import TELEGRAM_LIMIT
from test_bridge import make
from test_face import FaceWorld, faced
from test_face import input_id as face_input_id
from test_platformd import CLI_SESSION, short_wait  # noqa: F401 — the fixture is autouse

COMMON = Path(__file__).parent.parent / "deploy" / "lanes" / "instructions" / "common.md"

# --- the instructions and the header ------------------------------------------------------------------------------

def test_the_instructions_ask_with_a_question_card_and_never_teach_a_replies_line():
    common = " ".join(COMMON.read_text().split())
    assert "Replies:" not in common and "Ответы:" not in common, "a choice is a question card, never a line of text"
    for rule in ("ask with AskUserQuestion, never as a question in the text", "(Recommended)",
                 "Never ask to go on", "his answer changes what you do next"):
        assert rule in common, rule

def test_loudness_and_the_header():
    assert not pings.loud({"kind": "done", "took_s": 119, "attended": True})
    assert pings.loud({"kind": "done", "took_s": 120, "attended": True})
    assert pings.loud({"kind": "done", "took_s": 5, "attended": False}), "a run no owner's action started"
    assert pings.loud({"kind": "decision", "took_s": 5}) and pings.loud({"kind": "failed", "took_s": 5})
    assert pings.loud({"kind": "done"}), "an unknown length notifies"
    assert [pings.took_words(s) for s in (7, 125, 3600, 3725)] == ["7 s", "2 min", "1 h", "1 h 2 min"]
    assert pings.header({"kind": "decision", "place": "Путешествия", "title": "жильё", "took_s": 300}) == \
        "❓ Путешествия · жильё · 5 min"


# --- on the platform ----------------------------------------------------------------------------------------------

def result(text: str, error: bool = False) -> str:
    return json.dumps({"type": "result", "subtype": "error_during_execution" if error else "success",
                       "is_error": error, "result": text, "stop_reason": "end_turn", "session_id": CLI_SESSION})


async def turn(world: FaceWorld, text: str, *, input_id_: str = "tg:1", took: float = 0, error: bool = False) -> str:
    """A private session whose first turn ends with `text` after `took` seconds."""
    session = (await world.owner("new", {"input_id": input_id_, "track": "notes", "text": "найди жильё"}))["session"]
    run = world.lanes["private"].runs()[-1]
    world.clock.now += took
    world.lanes["private"].add(session, run["run"], result(text, error))
    world.lanes["private"].exited(session, run["run"])
    await world.poll()
    return session


def question_card(world: FaceWorld, session: str, approval: str = "q1", state: str = "shown") -> None:
    world.store.db.execute("INSERT INTO approvals (id, session, run, tool_use_id, kind, tool, input, cwd, "
                           "file_hash, digest, state, created, at) VALUES (?, ?, 'r', 't', 'question', "
                           "'AskUserQuestion', '{}', '', '', 'd', ?, 0, 0)", (approval, session, state))
    world.store.db.commit()

def test_a_turn_is_a_decision_only_while_a_question_card_of_its_session_is_shown_and_offers_no_replies(tmp_path):
    async def body(world: FaceWorld):
        session = await turn(world, 'Нашёл три варианта.\nReplies: "Бери первый" · "Покажи ещё"', took=600)
        [ping] = world.notes("summary")
        data = ping["data"]
        assert (data["text"], data["kind"], data["took_s"], data["line"]) == \
            ('Нашёл три варианта.\nReplies: "Бери первый" · "Покажи ещё"', "done", 600, None), \
            "a replies line is the model's text like any other"
        assert data["head"].startswith("Result · notes · session") and data["place"] == "notes"
        assert data["title"] == "найди жильё"
        assert not {"replies", "open", "answered", "buttons"} & data.keys()
        await world.login()
        [summary] = (await world.get(f"/api/sessions/{session}"))["summaries"]
        assert not {"replies", "answered"} & summary.keys() and summary["kind"] == "done"

        run = world.lanes["private"].runs()[-1]
        world.platform._summary(world.store.session(session), "Лимит подписки — жду сброса до 12:00", pings.FAILED,
                                run["run"], limit_until=1.0)
        assert world.notes("summary")[-1]["data"]["kind"] == "failed"
        assert world.store.last_summary(session)["limit_until"] == 1.0

        question_card(world, session, "q0", state="answered")
        world.platform._summary(world.store.session(session), "Готово.")
        assert world.notes("summary")[-1]["data"]["kind"] == "done", "a card already answered waits on nobody"
        question_card(world, session)
        world.platform._summary(world.store.session(session), "Выбери.")
        asked = world.notes("summary")[-1]["data"]
        assert (asked["kind"], asked["text"]) == ("decision", "Выбери."), "the question card asks, with its own buttons"
        world.platform._summary(world.store.session(session), "Сломалось.", pings.FAILED)
        assert world.notes("summary")[-1]["data"]["kind"] == "failed", "a failure stays a failure"
        assert world.notes("ping") == [], "nothing to close: a ping has no buttons of its own"

    faced(tmp_path, body)

def test_a_quick_done_turn_is_marked_quiet_and_a_long_one_loud(tmp_path):
    async def body(world: FaceWorld):
        await turn(world, "Готово.", took=30)
        await turn(world, "Готово.", input_id_="tg:2", took=900)
        quick, slow = (n["data"] for n in world.notes("summary"))
        assert (quick["kind"], quick["took_s"], quick["attended"], pings.loud(quick)) == ("done", 30, True, False)
        assert (slow["kind"], slow["took_s"], pings.loud(slow)) == ("done", 900, True)

    faced(tmp_path, body)


def test_a_ping_of_a_sphere_names_the_sphere_by_its_icon_and_title(tmp_path):
    async def body(world: FaceWorld):
        await world.down()
        world.cfg.spheres = [Sphere("travel", "Путешествия", "notes", "✈️"), Sphere("work", "Работа", "notes")]
        await world.up()
        said = await world.owner("new", {"input_id": "tg:1", "track": "travel", "text": "найди жильё"})
        run = world.lanes["private"].runs()[-1]
        world.lanes["private"].add(said["session"], run["run"], result("Готово."))
        await world.poll()
        [ping] = world.notes("summary")
        assert ping["data"]["place"] == "✈️ Путешествия"
        assert pings.header(ping["data"]).startswith("✅ ✈️ Путешествия · найди жильё")

    faced(tmp_path, body)


def test_a_run_no_owner_started_is_never_quiet(tmp_path):
    """A turn a restart cut goes on with CONTINUE; a resume the gate's shutdown left owed is queued by the platform:
    neither run was the owner's doing, and its ping notifies however short it is."""
    async def body(world: FaceWorld):
        session = await turn(world, "Готово.")
        store = world.store
        store.enqueue(session, None, world.clock.now)
        pings.unattended(store, session)
        [queued] = store.queued()
        pings.started(store, "gate-resume", queued["seq"], by_owner=True)
        pings.started(store, "continued", 12345, by_owner=False)
        pings.started(store, "owners", 12346, by_owner=True)
        assert [pings.attended(store, r) for r in ("gate-resume", "continued", "owners", None)] == \
            [False, False, True, False]
        assert store.db.execute("SELECT COUNT(*) FROM ping_unattended WHERE key LIKE 'q%'").fetchone()[0] == 0
        assert world.lanes["private"].runs()[0]["prompt"] == "найди жильё" and \
            world.notes("summary")[0]["data"]["attended"] is True

    faced(tmp_path, body)


async def ends(world: FaceWorld, session: str, text: str = "Сделал.") -> dict:
    """The session's running turn ends with `text`: its ping's data."""
    run = world.lanes["private"].runs()[-1]
    world.lanes["private"].add(session, run["run"], result(text))
    world.lanes["private"].exited(session, run["run"])
    await world.poll()
    return world.notes("summary")[-1]["data"]

def test_a_session_the_owner_drives_from_the_app_is_marked_read_there(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = await turn(world, "Готово.")
        assert "in_app" not in world.notes("summary")[0]["data"], "begun in Telegram: its turn goes there"

        status, _, _ = await world.call("POST", f"/api/sessions/{session}/input",
                                        {"input_id": face_input_id(), "text": "А подробнее?"})
        assert status == 200
        assert (await ends(world, session, "Вот."))["in_app"] is True, "his last message was written in the app"

        await world.owner("input", {"input_id": "tg:7", "session": session, "text": "Ещё раз"})
        assert "in_app" not in await ends(world, session), "back in Telegram: its turns go there again"

    faced(tmp_path, body)

def test_the_app_no_longer_takes_a_reply_as_input(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = await turn(world, "Готово.")
        [ping] = world.notes("summary")
        status, _, _ = await world.call("POST", f"/api/sessions/{session}/input", {"reply": {"seq": ping["seq"], "n": 0}})
        assert status == 400 and len(world.lanes["private"].runs()) == 1

    faced(tmp_path, body)

# --- in the bridge ------------------------------------------------------------------------------------------------

def ping_note(seq=5, session="ab12cd34", **data):
    base = {"head": "Result · notes · session ab12cd34", "text": "Нашёл три варианта.", "kind": "decision", "took_s": 600,
            "place": "Путешествия", "title": "жильё <в> Энске"}
    return {"seq": seq, "kind": "summary", "session": session, "approval": "", "data": {**base, **data}}

BASE = ["To the Base · 1", "p.base"]
PUSH = [["Push retinue · 2", "p.push1"], ["Push site · 1", "p.push2"]]

def test_a_ping_goes_out_with_its_header_its_buttons_on_the_last_part_and_its_sound(tmp_path):
    ch, bridge, _ = make(tmp_path, [ping_note(text="Сделал. " * 900, base=BASE, push=PUSH),
                                    ping_note(6, kind="done", took_s=30, attended=True)])
    asyncio.run(bridge.deliver())
    long_parts = [p for m, p, _ in ch.sent if m == "sendMessage"][:-1]
    quick = [p for m, p, _ in ch.sent if m == "sendMessage"][-1]
    assert len(long_parts) > 1
    assert long_parts[0]["text"].startswith("<b>❓ Путешествия · жильё &lt;в&gt; Энске · 10 min</b>\n\n")
    assert all("reply_markup" not in p for p in long_parts[:-1])
    assert long_parts[-1]["reply_markup"] == {"inline_keyboard": [
        [{"text": "To the Base · 1", "callback_data": "p.base"}],
        [{"text": "Push retinue · 2", "callback_data": "p.push1"}],
        [{"text": "Push site · 1", "callback_data": "p.push2"}]]}
    assert all("disable_notification" not in p for p in long_parts), "a decision notifies"
    assert quick["disable_notification"] is True and quick["text"].startswith("<b>✅ Путешествия")
    assert "reply_markup" not in quick, "nothing to carry: no buttons"
    sent = [900 + n for n in range(len(long_parts) + 1)]
    assert all(ch.store.sent_event(SESSION, str(n)) == "ab12cd34" for n in sent), "a reply to any part goes in"
    assert ch.store.sent_native(PING, "5") == [str(sent[len(long_parts) - 1])]

def test_buttons_telegram_refuses_cost_the_buttons_never_the_text(tmp_path):
    ch, bridge, platform = make(tmp_path, [ping_note(base=BASE)])
    real = ch.call

    async def call(method, files=None, **params):
        if params.get("reply_markup"):
            ch.sent.append((method, params, files))
            raise TelegramError(f"{method}: 400 Bad Request: BUTTON_DATA_INVALID", 400)
        return await real(method, files, **params)

    ch.call = call
    asyncio.run(bridge.deliver())
    sent = [p for m, p, _ in ch.sent if m == "sendMessage"]
    assert len(sent) == 2 and "reply_markup" in sent[0] and "reply_markup" not in sent[1]
    assert sent[1]["text"] == sent[0]["text"] and platform.confirmed == [5]
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34", "a reply to it still goes into the session"

def test_a_ping_note_rewrites_the_last_message_with_its_new_buttons_and_the_same_text(tmp_path):
    first = ping_note(base=BASE)
    relabelled = {"seq": 7, "kind": "ping", "session": "ab12cd34", "approval": "",
                  "data": {**first["data"], "base": ["To the Base · 2", "p.base"], "push": PUSH[:1], "seq": 5}}
    taken_off = {"seq": 8, "kind": "ping", "session": "ab12cd34", "approval": "",
                 "data": {**first["data"], "base": None, "push": [], "seq": 5}}
    ch, bridge, platform = make(tmp_path, [first, relabelled, taken_off])
    asyncio.run(bridge.deliver())
    [(_, sent, _)] = [s for s in ch.sent if s[0] == "sendMessage"]
    (_, put_on, _), (_, bare, _) = [s for s in ch.sent if s[0] == "editMessageText"]
    assert put_on["message_id"] == bare["message_id"] == 900
    assert put_on["text"] == bare["text"] == sent["text"] and "→" not in sent["text"]
    assert put_on["reply_markup"] == {"inline_keyboard": [[{"text": "To the Base · 2", "callback_data": "p.base"}],
                                                          [{"text": "Push retinue · 2", "callback_data": "p.push1"}]]}
    assert "reply_markup" not in bare
    assert platform.confirmed == [8]

def test_a_turn_read_in_the_app_reaches_telegram_only_when_long_and_only_as_its_header(tmp_path):
    ch, bridge, platform = make(tmp_path, [ping_note(in_app=True, took_s=40),
                                           ping_note(6, in_app=True, took_s=pings.AWAY_S, text="Сделал. " * 900)])
    bridge.face_url = "https://app.example.com"
    asyncio.run(bridge.deliver())
    [(_, params, _)] = [s for s in ch.sent if s[0] == "sendMessage"]
    assert params["text"] == "<b>❓ Путешествия · жильё &lt;в&gt; Энске · 5 min</b>\nAnswered in the app."
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "Open", "web_app": {
        "url": "https://app.example.com/s/ab12cd34"}}]]}
    assert platform.confirmed == [6], "both gone past: the quick one said nothing"
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_native(PING, "6") == []


def test_ping_parts_never_exceed_a_message_and_an_empty_text_is_the_header_alone():
    parts = ping_parts({**ping_note()["data"], "text": "x" * 5000})
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)
    assert ping_parts({**ping_note()["data"], "text": ""}) == ["<b>❓ Путешествия · жильё &lt;в&gt; Энске · 10 min</b>"]
