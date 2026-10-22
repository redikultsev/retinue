"""The router's bridge to the platform without the network: its notes become Telegram messages mapped under the
platform's own channel names, a note that did not go out is not confirmed, and the owner's answers go back."""

import asyncio

import httpx

from retinue.bridge import CARD, NOT_SHOWN, SESSION, UNAVAILABLE, PlatformBridge
from retinue.channels.telegram import TelegramChannel, TelegramError
from retinue.config import TelegramConfig
from retinue.mail import Unavailable
from retinue.protocol import Store

OWNER = 42
VIEW = {"head": ["Разрешение · retinue · сессия ab12cd34", "Bash"], "body": "make test", "status": "", "link": []}
BUTTONS = [["Разрешить", "p.approve"], ["Отказать", "p.reject"], ["Открыть", "p.open"]]


class FakePlatform:
    def __init__(self, notes=()):
        self.notes, self.confirmed, self.calls, self.down = list(notes), [], [], False
        self.answers = {}

    async def items(self, limit=50):
        if self.down:
            raise Unavailable("платформа недоступна (ConnectError)")
        return [n for n in self.notes if n["seq"] > (self.confirmed[-1] if self.confirmed else 0)][:limit]

    async def confirm(self, upto):
        self.confirmed.append(upto)

    async def owner(self, call, body):
        self.calls.append((call, body))
        if self.down:
            raise Unavailable("платформа недоступна (ConnectError)")
        return self.answers.get(call, {})


def make(tmp_path, notes=()):
    store = Store(str(tmp_path / "s.db"))
    bridge = PlatformBridge(FakePlatform(notes), store)
    ch = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="t"), store, bridge=bridge)
    ch.sent, ch.fail = [], {}  # a text it holds, or a message id -> what Telegram says
    ids = iter(range(900, 10_000))

    async def call(method, files=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        ch.sent.append((method, params, files))
        for match, error in ch.fail.items():
            if match == params.get("message_id") or isinstance(match, str) and match in params.get("text", ""):
                raise TelegramError(f"{method}: {error}", int(error.split()[0]))
        return {"message_id": next(ids)}

    ch.call = call
    return ch, bridge, bridge.neighbour


def card(seq, approval="a1", session="ab12cd34", buttons=BUTTONS):
    return {"seq": seq, "kind": "card", "session": session, "approval": approval,
            "data": {"view": VIEW, "buttons": buttons}}


def test_a_card_goes_out_once_with_one_button_per_row_and_maps_to_its_session_and_approval(tmp_path):
    ch, bridge, platform = make(tmp_path, [card(1)])
    assert asyncio.run(bridge.deliver()) == 1
    assert asyncio.run(bridge.deliver()) == 0, "a confirmed note is not shown again"
    ((method, params, _),) = ch.sent
    assert method == "sendMessage" and params["text"].startswith("Разрешение · retinue · сессия ab12cd34\nBash")
    assert "<pre>make test</pre>" in params["text"] and params["link_preview_options"] == {"is_disabled": True}
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": label, "callback_data": button}]
                                                          for label, button in BUTTONS]}
    assert platform.confirmed == [1]
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(CARD, "900") == "a1"
    assert ch.store.sent_event("telegram", "900") is None, "a platform message is never the assistant's"


def test_a_note_that_did_not_go_out_is_not_confirmed_and_comes_again(tmp_path):
    summary = {"seq": 2, "kind": "summary", "session": "ab12cd34",
               "data": {"head": "Итог · retinue · сессия ab12cd34", "text": "Готово."}}
    ch, bridge, platform = make(tmp_path, [card(1), summary])
    ch.fail = {"Итог": "429 Too Many Requests: retry after 5"}

    async def run():
        try:
            await bridge.deliver()
        except TelegramError:
            pass
        assert platform.confirmed == [1], "the card went out; the summary did not"
        ch.fail = {}
        assert await bridge.deliver() == 1

    asyncio.run(run())
    assert platform.confirmed == [1, 2]
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"].count("Итог · retinue · сессия ab12cd34\n\nГотово.") == 2


def test_an_update_rewrites_every_message_of_its_approval_and_a_summary_maps_its_messages(tmp_path):
    stale = {**VIEW, "status": "Устарела: ты написал новое сообщение"}
    update = {"seq": 3, "kind": "update", "session": "ab12cd34", "approval": "a1",
              "data": {"view": stale, "buttons": []}}
    summary = {"seq": 4, "kind": "summary", "session": "ab12cd34",
               "data": {"head": "Итог · retinue · сессия ab12cd34", "text": "Сделал. " * 900}}
    ch, bridge, platform = make(tmp_path, [card(1), card(2), update, summary])
    asyncio.run(bridge.deliver())
    edits = [(p["message_id"], p) for m, p, _ in ch.sent if m == "editMessageText"]
    assert [i for i, _ in edits] == [900, 901], "both messages that showed the card"
    assert all(p["text"].endswith("<i>Устарела: ты написал новое сообщение</i>") and "reply_markup" not in p
               for _, p in edits)
    parts = [n for n in range(902, 902 + 10) if ch.store.sent_event(SESSION, str(n))]
    assert len(parts) > 1 and all(ch.store.sent_event(SESSION, str(n)) == "ab12cd34" for n in parts)
    assert platform.confirmed == [4]


def test_an_update_telegram_has_nothing_left_to_do_for_is_settled(tmp_path):
    update = {"seq": 2, "kind": "update", "session": "s", "approval": "a1", "data": {"view": VIEW, "buttons": []}}
    ch, bridge, platform = make(tmp_path, [card(1), update])
    asyncio.run(bridge.deliver())
    platform.confirmed.clear()
    ch.fail = {900: "400 Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}
    platform.notes = [update]
    asyncio.run(bridge.deliver())
    assert platform.confirmed == [2], "a resent update the card already shows does not block the notes after it"


def test_a_note_that_can_never_be_shown_is_said_and_does_not_hold_back_the_notes_after_it(tmp_path):
    malformed = {"seq": 2, "kind": "card", "session": "ef56gh78", "approval": "a2", "data": {"buttons": []}}
    summary = {"seq": 3, "kind": "summary", "session": "ab12cd34",
               "data": {"head": "Итог · retinue · сессия ab12cd34", "text": "Готово."}}
    ch, bridge, platform = make(tmp_path, [card(1), malformed, summary])
    ch.fail = {"make test": "400 Bad Request: message is too long"}
    assert asyncio.run(bridge.deliver()) == 3
    assert platform.confirmed == [3]
    said = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert [t.startswith(NOT_SHOWN) for t in said] == [False, True, True, False], "the refused card, two notices"
    assert "message is too long" in said[1] and "KeyError" in said[2]
    assert said[3] == "Итог · retinue · сессия ab12cd34\n\nГотово."
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(SESSION, "901") == "ef56gh78", \
        "a reply to a notice goes to the session the note was for"
    assert ch.store.sent_event(CARD, "900") is None


def test_a_press_answers_rewrites_the_card_with_the_buttons_it_keeps_and_sends_the_input_as_a_file(tmp_path):
    ch, bridge, platform = make(tmp_path)
    decided = {**VIEW, "status": "Разрешено"}
    platform.answers = {"press": {"toast": "Открываю", "view": decided, "keep": [["Разрешить", "p.approve"]],
                                  "document": {"name": "input.txt", "text": "make test"}}}
    asyncio.run(bridge.press("q1", "p.open", 900))
    assert platform.calls == [("press", {"button": "p.open"})]
    (answer, _, _), (edit, params, _), (document, doc, files) = ch.sent
    assert (answer, edit, document) == ("answerCallbackQuery", "editMessageText", "sendDocument")
    assert ch.sent[0][1] == {"callback_query_id": "q1", "text": "Открываю"}
    assert params["message_id"] == 900 and params["text"].endswith("<i>Разрешено</i>")
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "Разрешить", "callback_data": "p.approve"}]]}
    assert doc == {"chat_id": str(OWNER)} and files == {"document": ("input.txt", b"make test", "text/plain")}


def test_a_press_that_keeps_no_button_leaves_the_card_bare_and_a_card_already_rewritten_is_no_failure(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"press": {"toast": "Разрешено", "view": {**VIEW, "status": "Разрешено"}, "keep": [],
                                  "document": {"name": "input.txt", "text": "make test"}}}
    ch.fail = {900: "400 Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}
    asyncio.run(bridge.press("q1", "p.approve", 900))
    (_, edit, _), (method, _, _) = ch.sent[1:]
    assert "reply_markup" not in edit, "no button kept, none shown"
    assert method == "sendDocument", "the platform's own update got there first; the press still goes through"


def test_the_owner_s_answers_go_to_the_platform_and_its_silence_is_said(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"new": {"ok": True, "session": "ab12cd34", "say": "Сессия ab12cd34 · retinue."},
                        "input": {"say": "Принято."}}

    async def run():
        await bridge.new("retinue", "fix CI", "7")
        await bridge.input("ab12cd34", "и тесты", "8")
        platform.down = True
        await bridge.input("ab12cd34", "ещё", "9")

    asyncio.run(run())
    assert platform.calls == [("new", {"input_id": "tg:7", "track": "retinue", "text": "fix CI"}),
                              ("input", {"input_id": "tg:8", "session": "ab12cd34", "text": "и тесты"}),
                              ("input", {"input_id": "tg:9", "session": "ab12cd34", "text": "ещё"})]
    said = [p["text"] for _, p, _ in ch.sent]
    assert said == ["Сессия ab12cd34 · retinue.", "Принято.", UNAVAILABLE]
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(SESSION, "901") == "ab12cd34"
    assert ch.store.sent_event(SESSION, "902") is None


def test_an_unknown_track_is_said_and_maps_to_nothing(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"new": {"ok": False, "say": "Нет такого трека: x. Есть: notes, retinue"}}
    asyncio.run(bridge.new("x", "", "7"))
    assert [p["text"] for _, p, _ in ch.sent] == ["Нет такого трека: x. Есть: notes, retinue"]
    assert ch.store.sent_event(SESSION, "900") is None


def test_a_gateway_error_page_from_telegram_leaves_the_note_unconfirmed(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    bridge = PlatformBridge(FakePlatform([card(1)]), store)
    ch = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="SECRET"), store, bridge=bridge)
    answers = iter([httpx.Response(502, text="<html>502 Bad Gateway</html>"),
                    httpx.Response(200, json={"ok": True, "result": {"message_id": 900}})])
    ch.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(answers)))

    async def run():
        try:
            await bridge.deliver()
        except TelegramError as exc:
            assert exc.code == 502 and "SECRET" not in str(exc), "the URL carries the token"
        assert bridge.neighbour.confirmed == [], "a passing failure is tried again, not said and confirmed past"
        assert await bridge.deliver() == 1

    asyncio.run(run())
    assert bridge.neighbour.confirmed == [1] and store.sent_event(CARD, "900") == "a1"
