"""Pings: the replies line parsed strictly, the loudness rule, and a ping's life on the platform — its reply pressed in
Telegram or in the face goes in once, closes everywhere, and survives a restart — and in the bridge."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from retinue import pings
from retinue.bridge import PING, SESSION, ping_parts
from retinue.channels.telegram import TelegramError
from retinue.config import Sphere
from retinue.render import TELEGRAM_LIMIT
from test_bridge import make
from test_face import FaceWorld, faced
from test_platformd import CLI_SESSION, HOUR, short_wait  # noqa: F401 — the fixture is autouse

COMMON = Path(__file__).parent.parent / "deploy" / "lanes" / "instructions" / "common.md"


# --- the replies line ---------------------------------------------------------------------------------------------

def test_the_instructions_own_example_parses():
    example = re.search(r"`(Ответы: [^`]+)`", COMMON.read_text()).group(1)
    assert pings.split(f"Сделал.\n\n{example}") == ("Сделал.", ["Да, делай", "Сначала покажи план"])


@pytest.mark.parametrize("text, expected", [
    ("Готово.\nОтветы: «Да» · «Нет»", ("Готово.", ["Да", "Нет"])),
    ("Готово.\n\n**Ответы:** «Да»  ·  «Нет»  \n\n", ("Готово.", ["Да", "Нет"])),
    ("Готово.\nОтветы: «a» · «b» · «c» · «d»", ("Готово.", ["a", "b", "c"])),  # three at most, in its order
    ("Готово.\nОтветы: «Да» · «Да» · «  »", ("Готово.", ["Да"])),  # duplicates and empty ones dropped
    ("Готово.\nОтветы: «" + "x" * 61 + "» · «Коротко»", ("Готово.", ["Коротко"])),  # too long for a button: out
    ("Готово.\nОтветы: «на‮зад» · «a​b»", ("Готово.", ["назад", "ab"])),  # bidi and invisible chars out
    ("Ответы: «Да»", ("", ["Да"])),  # the line alone: an empty text, the header carries the message
])
def test_a_well_formed_last_line_becomes_replies(text, expected):
    assert pings.split(text) == expected


@pytest.mark.parametrize("text", [
    "Готово.",
    "Ответы: «Да» · «Нет»\nА вот ещё текст.",  # not the last line
    "Готово.\nОтветы: Да · Нет",  # no quotes
    "Готово.\nОтветы: «Да» «Нет»",  # no separator
    "Готово.\nОтветы: «Да» · «Нет» и ещё",  # trailing words
    "Готово.\nОтветы: «Да «вложенный» ответ»",  # nested quotes
    "Готово.\nответы: «Да»",  # another label
    "Готово.\n> Ответы: «Да»",  # quoted
    "```\nОтветы: «Да»",  # inside an open code fence
    "Готово.\nОтветы: «" + "x" * 61 + "»",  # nothing left after the caps: the line stays as it was
    "Готово.\nОтветы: «<b>жирно</b>" + "x" * 60 + "»",
    "Готово.\nОтветы: «/deploy» · «!rm -rf .» · «#remember»",  # a command in the owner's mouth: never a button
    "Готово.\nОтветы: «\u3164» · «\u2800» · «\u0301» · «…»",  # blank letters, a braille blank, a lone mark, no word
    "```\n~~~\nОтветы: «Да»",  # a ~~~ never closes a ``` fence
    "````\n```\nОтветы: «Да»",  # nor does a shorter one
    "```\n``` not a close\nОтветы: «Да»",  # nor one with words after it
])
def test_anything_else_stays_text_and_gives_no_replies(text):
    assert pings.split(text) == (text, [])


@pytest.mark.parametrize("text", ["~~~\ncode\n~~~\nОтветы: «Да»", "```py\nx = 1\n```\nОтветы: «Да»",
                                  "````\n```\n````\nОтветы: «Да»"])
def test_a_closed_fence_of_its_own_kind_lets_the_line_count(text):
    assert pings.split(text)[1] == ["Да"]


def test_parse_gives_the_line_as_written_and_drops_only_the_bad_replies():
    assert pings.parse("Ок.\n  Ответы: «/run» · «Да, делай» · «ㅤ»  ") == ("Ок.", ["Да, делай"], "Ответы: «/run» · «Да, делай» · «ㅤ»")
    assert pings.parse("Ок.") == ("Ок.", [], "")


def test_a_reply_keeps_markup_as_plain_text():
    assert pings.split("Ок.\nОтветы: «<b>да</b>» · «*нет*»")[1] == ["<b>да</b>", "*нет*"]


def test_loudness_and_the_header():
    assert not pings.loud({"kind": "done", "took_s": 119, "attended": True})
    assert pings.loud({"kind": "done", "took_s": 120, "attended": True})
    assert pings.loud({"kind": "done", "took_s": 5, "attended": False}), "a run no owner's action started"
    assert pings.loud({"kind": "decision", "took_s": 5}) and pings.loud({"kind": "failed", "took_s": 5})
    assert pings.loud({"kind": "done"}), "an unknown length notifies"
    assert [pings.took_words(s) for s in (7, 125, 3600, 3725)] == ["7 с", "2 мин", "1 ч", "1 ч 2 мин"]
    assert pings.header({"kind": "decision", "place": "Путешествия", "title": "жильё", "took_s": 300}) == \
        "❓ Путешествия · жильё · 5 мин"


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


def test_a_turn_with_replies_is_a_decision_and_its_reply_goes_in_once_from_either_place(tmp_path):
    async def body(world: FaceWorld):
        session = await turn(world, "Нашёл три варианта.\nОтветы: «Бери первый» · «Покажи ещё»", took=600)
        [ping] = world.notes("summary")
        data = ping["data"]
        assert (data["text"], data["replies"], data["kind"], data["took_s"]) == \
            ("Нашёл три варианта.", ["Бери первый", "Покажи ещё"], "decision", 600)
        assert data["head"].startswith("Итог · notes · сессия") and data["place"] == "notes"
        assert data["title"] == "найди жильё"
        assert [label for label, _ in data["buttons"]] == ["Бери первый", "Покажи ещё"]

        await world.login()
        snapshot = await world.get(f"/api/sessions/{session}")
        assert snapshot["summaries"][0]["replies"] == ["Бери первый", "Покажи ещё"]

        # A restart, an hour: the Telegram press still sends once.
        await world.down()
        world.clock.now += HOUR
        await world.up()
        await world.login()
        pressed = await world.owner("press", {"button": data["buttons"][1][1]})
        assert pressed["toast"] == "Принято."
        assert world.lanes["private"].runs()[-1]["prompt"] == "Покажи ещё"
        assert world.store.input_session(pings.input_of(ping["seq"])) == session

        again = await world.owner("press", {"button": data["buttons"][0][1]})
        assert again["toast"] == pings.GONE, "the ping's other buttons are spent with it"
        status, said, _ = await world.call("POST", f"/api/sessions/{session}/input",
                                           {"reply": {"seq": ping["seq"], "n": 0}})
        assert status == 200 and said["say"] == "Уже отправлено: «Покажи ещё»"
        assert len(world.lanes["private"].runs()) == 2, "one run for the reply, whatever was pressed after"

        snapshot = await world.get(f"/api/sessions/{session}")
        assert snapshot["summaries"][0]["replies"] == [] and snapshot["summaries"][0]["answered"] == "Покажи ещё"
        [closed] = world.notes("ping")
        assert closed["data"]["seq"] == ping["seq"] and closed["data"]["answered"] == "Покажи ещё"
        assert "buttons" not in closed["data"]

    faced(tmp_path, body)


def test_a_face_press_is_the_same_press_and_the_owner_typing_instead_makes_the_replies_stale(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        first = await turn(world, "Готово.\nОтветы: «Да» · «Нет»")
        [ping] = world.notes("summary")
        status, said, _ = await world.call("POST", f"/api/sessions/{first}/input",
                                           {"reply": {"seq": ping["seq"], "n": 0}})
        assert status == 200 and said["say"] == "Принято."
        assert (said["summary"]["replies"], said["summary"]["answered"]) == ([], "Да")
        assert world.lanes["private"].runs()[-1]["prompt"] == "Да"
        assert (await world.owner("press", {"button": ping["data"]["buttons"][1][1]}))["toast"] == pings.GONE

        second = await turn(world, "Готово.\nОтветы: «Да» · «Нет»", input_id_="tg:2")
        [_, ping2] = world.notes("summary")
        await world.owner("input", {"input_id": "tg:3", "session": second, "text": "Нет, по-другому"})
        status, said, _ = await world.call("POST", f"/api/sessions/{second}/input",
                                           {"reply": {"seq": ping2["seq"], "n": 0}})
        assert said["say"] == pings.STALE
        assert world.lanes["private"].runs()[-1]["prompt"] == "Нет, по-другому"
        assert [n["data"]["answered"] for n in world.notes("ping")] == ["Да", None]

        for bad in ({"seq": ping2["seq"], "n": 5}, {"seq": 999, "n": 0}, {"seq": ping["seq"], "n": 0}):
            status, said, _ = await world.call("POST", f"/api/sessions/{second}/input", {"reply": bad})
            assert status == 200 and said["say"] in (pings.GONE, pings.STALE)
        status, _, _ = await world.call("POST", f"/api/sessions/{second}/input", {"reply": {"seq": "1", "n": 0}})
        assert status == 400

    faced(tmp_path, body)


def test_a_newer_ping_closes_the_older_one_and_a_waiting_card_or_a_failure_gets_no_replies(tmp_path):
    async def body(world: FaceWorld):
        session = await turn(world, "Готово.\nОтветы: «Да»")
        run = world.lanes["private"].runs()[-1]
        world.platform._summary(world.store.session(session), "Лимит подписки — жду сброса до 12:00", pings.FAILED,
                                run["run"], limit_until=1.0)
        first, limit = world.notes("summary")
        assert world.notes("ping")[0]["data"]["seq"] == first["seq"]
        assert (limit["data"]["kind"], limit["data"]["replies"], limit["data"]["buttons"]) == ("failed", [], [])
        assert world.store.last_summary(session)["limit_until"] == 1.0

        broken = await turn(world, "Ответы: «Да»", input_id_="tg:9", error=True)
        failed = world.notes("summary")[-1]["data"]
        assert failed["kind"] == "failed" and failed["replies"] == [] and failed["text"].endswith("Ответы: «Да»")
        assert world.store.session(broken)["state"] == "idle"

        world.store.db.execute("INSERT INTO approvals (id, session, run, tool_use_id, kind, tool, input, cwd, "
                               "file_hash, digest, state, created, at) VALUES ('q1', ?, 'r', 't', 'question', "
                               "'AskUserQuestion', '{}', '', '', 'd', 'shown', 0, 0)", (broken,))
        world.platform._summary(world.store.session(broken), "Выбери.\nОтветы: «Да»")
        asked = world.notes("summary")[-1]["data"]
        assert (asked["kind"], asked["replies"], asked["text"]) == ("decision", [], "Выбери."), \
            "the question card has its own buttons"

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


def test_a_face_press_lives_as_long_as_the_telegram_button(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = await turn(world, "Готово.\nОтветы: «Да»")
        [ping] = world.notes("summary")
        assert (await world.get(f"/api/sessions/{session}"))["summaries"][0]["line"] == "Ответы: «Да»"
        world.clock.now += pings.BUTTON_TTL_S
        status, said, _ = await world.call("POST", f"/api/sessions/{session}/input",
                                           {"reply": {"seq": ping["seq"], "n": 0}})
        assert said["say"] == pings.GONE and len(world.lanes["private"].runs()) == 1

    faced(tmp_path, body)


# --- in the bridge ------------------------------------------------------------------------------------------------

def ping_note(seq=5, session="ab12cd34", **data):
    base = {"head": "Итог · notes · сессия ab12cd34", "text": "Нашёл три варианта.", "kind": "decision", "took_s": 600,
            "place": "Путешествия", "title": "жильё <в> Энске", "replies": ["Бери первый", "Покажи ещё"],
            "open": True, "answered": None, "buttons": [["Бери первый", "p.r0"], ["Покажи ещё", "p.r1"]]}
    return {"seq": seq, "kind": "summary", "session": session, "approval": "", "data": {**base, **data}}


def test_a_ping_goes_out_with_its_header_its_buttons_on_the_last_part_and_its_sound(tmp_path):
    ch, bridge, _ = make(tmp_path, [ping_note(text="Сделал. " * 900),
                                    ping_note(6, kind="done", took_s=30, attended=True, replies=[], open=False,
                                              buttons=[])])
    asyncio.run(bridge.deliver())
    long_parts = [p for m, p, _ in ch.sent if m == "sendMessage"][:-1]
    quick = [p for m, p, _ in ch.sent if m == "sendMessage"][-1]
    assert len(long_parts) > 1
    assert long_parts[0]["text"].startswith("<b>❓ Путешествия · жильё &lt;в&gt; Энске · 10 мин</b>\n\n")
    assert all("reply_markup" not in p for p in long_parts[:-1])
    assert long_parts[-1]["reply_markup"] == {"inline_keyboard": [[{"text": "Бери первый", "callback_data": "p.r0"}],
                                                                  [{"text": "Покажи ещё", "callback_data": "p.r1"}]]}
    assert all("disable_notification" not in p for p in long_parts), "a decision notifies"
    assert quick["disable_notification"] is True and quick["text"].startswith("<b>✅ Путешествия")
    sent = [900 + n for n in range(len(long_parts) + 1)]
    assert all(ch.store.sent_event(SESSION, str(n)) == "ab12cd34" for n in sent), "a reply to any part goes in"
    assert ch.store.sent_native(PING, "5") == [str(sent[len(long_parts) - 1])]


def test_buttons_telegram_refuses_cost_the_buttons_never_the_text(tmp_path):
    ch, bridge, platform = make(tmp_path, [ping_note()])
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


def test_a_closed_ping_rewrites_its_last_message_without_buttons(tmp_path):
    answered = {"seq": 7, "kind": "ping", "session": "ab12cd34", "approval": "",
                "data": {**ping_note()["data"], "open": False, "answered": "Покажи ещё", "seq": 5}}
    ch, bridge, platform = make(tmp_path, [ping_note(), answered])
    asyncio.run(bridge.deliver())
    [(method, params, _)] = [s for s in ch.sent if s[0] == "editMessageText"]
    assert params["message_id"] == 900 and "reply_markup" not in params
    assert params["text"].endswith("\n\n→ Покажи ещё") and "Нашёл три варианта." in params["text"]
    assert platform.confirmed == [7]


def test_ping_parts_never_exceed_a_message_and_an_empty_text_is_the_header_alone():
    parts = ping_parts({**ping_note()["data"], "text": "x" * 5000, "answered": "Да"})
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)
    assert ping_parts({**ping_note()["data"], "text": ""}) == ["<b>❓ Путешествия · жильё &lt;в&gt; Энске · 10 мин</b>"]
