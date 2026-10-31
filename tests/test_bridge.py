"""The router's bridge to the platform without the network: its notes become Telegram messages mapped under the
platform's own channel names, a note that did not go out is not confirmed, and the owner's answers go back."""

import asyncio
import base64
import hashlib
import json
import re

import httpx
import pytest

from retinue import bridge as bridge_module
from retinue.bridge import CARD, NOT_SHOWN, SESSION, UNAVAILABLE, PlatformBridge
from retinue.channels.telegram import TelegramChannel, TelegramError
from retinue.config import TelegramConfig
from retinue.mail import Unavailable
from retinue.protocol import Store
from retinue.speech import SpeechError

OWNER = 42
VIEW = {"head": ["Permission · retinue · session ab12cd34", "Bash"], "body": "make test", "status": "", "link": []}
BUTTONS = [["Allow", "p.approve"], ["Deny", "p.reject"], ["Open", "p.open"]]


class FakePlatform:
    def __init__(self, notes=()):
        self.notes, self.confirmed, self.calls, self.down = list(notes), [], [], False
        self.answers = {}

    async def items(self, limit=50):
        if self.down:
            raise Unavailable("unavailable: the platform (ConnectError)")
        return [n for n in self.notes if n["seq"] > (self.confirmed[-1] if self.confirmed else 0)][:limit]

    async def confirm(self, upto):
        self.confirmed.append(upto)

    async def owner(self, call, body, timeout=60):
        self.calls.append((call, body))
        if self.down:
            raise Unavailable("unavailable: the platform (ConnectError)")
        return self.answers.get(call, {})

    async def waiting(self, body):
        return await self.owner("waiting", body)


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
    assert method == "sendMessage" and params["text"].startswith("Permission · retinue · session ab12cd34\nBash")
    assert "<pre>make test</pre>" in params["text"] and params["link_preview_options"] == {"is_disabled": True}
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": label, "callback_data": button}]
                                                          for label, button in BUTTONS]}
    assert platform.confirmed == [1]
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(CARD, "900") == "a1"
    assert ch.store.sent_event("telegram", "900") is None, "a platform message is never the assistant's"


def test_a_note_that_did_not_go_out_is_not_confirmed_and_comes_again(tmp_path):
    summary = {"seq": 2, "kind": "summary", "session": "ab12cd34",
               "data": {"head": "Result · retinue · session ab12cd34", "text": "Done."}}
    ch, bridge, platform = make(tmp_path, [card(1), summary])
    ch.fail = {"Result": "429 Too Many Requests: retry after 5"}

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
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"].count("Result · retinue · session ab12cd34\n\nDone.") == 2


def test_an_update_rewrites_every_message_of_its_approval_and_a_summary_maps_its_messages(tmp_path):
    stale = {**VIEW, "status": "Expired: you wrote a new message"}
    update = {"seq": 3, "kind": "update", "session": "ab12cd34", "approval": "a1",
              "data": {"view": stale, "buttons": []}}
    summary = {"seq": 4, "kind": "summary", "session": "ab12cd34",
               "data": {"head": "Result · retinue · session ab12cd34", "text": "Did it. " * 900}}
    ch, bridge, platform = make(tmp_path, [card(1), card(2), update, summary])
    asyncio.run(bridge.deliver())
    edits = [(p["message_id"], p) for m, p, _ in ch.sent if m == "editMessageText"]
    assert [i for i, _ in edits] == [900, 901], "both messages that showed the card"
    assert all(p["text"].endswith("<i>Expired: you wrote a new message</i>") and "reply_markup" not in p
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
               "data": {"head": "Result · retinue · session ab12cd34", "text": "Done."}}
    ch, bridge, platform = make(tmp_path, [card(1), malformed, summary])
    ch.fail = {"make test": "400 Bad Request: message is too long"}
    assert asyncio.run(bridge.deliver()) == 3
    assert platform.confirmed == [3]
    said = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert [t.startswith(NOT_SHOWN) for t in said] == [False, True, True, False], "the refused card, two notices"
    assert "message is too long" in said[1] and "KeyError" in said[2]
    assert said[3] == "Result · retinue · session ab12cd34\n\nDone."
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(SESSION, "901") == "ef56gh78", \
        "a reply to a notice goes to the session the note was for"
    assert ch.store.sent_event(CARD, "900") is None


def test_a_press_answers_rewrites_the_card_with_the_buttons_it_keeps_and_sends_the_input_as_a_file(tmp_path):
    ch, bridge, platform = make(tmp_path)
    decided = {**VIEW, "status": "Allowed"}
    platform.answers = {"press": {"toast": "Opening", "view": decided, "keep": [["Allow", "p.approve"]],
                                  "document": {"name": "input.txt", "text": "make test"}}}
    asyncio.run(bridge.press("q1", "p.open", 900))
    assert platform.calls == [("press", {"button": "p.open"})]
    (answer, _, _), (edit, params, _), (document, doc, files) = ch.sent
    assert (answer, edit, document) == ("answerCallbackQuery", "editMessageText", "sendDocument")
    assert ch.sent[0][1] == {"callback_query_id": "q1", "text": "Opening"}
    assert params["message_id"] == 900 and params["text"].endswith("<i>Allowed</i>")
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "Allow", "callback_data": "p.approve"}]]}
    assert doc == {"chat_id": str(OWNER)} and files == {"document": ("input.txt", b"make test", "text/plain")}


def test_a_press_that_keeps_no_button_leaves_the_card_bare_and_a_card_already_rewritten_is_no_failure(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"press": {"toast": "Allowed", "view": {**VIEW, "status": "Allowed"}, "keep": [],
                                  "document": {"name": "input.txt", "text": "make test"}}}
    ch.fail = {900: "400 Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}
    asyncio.run(bridge.press("q1", "p.approve", 900))
    (_, edit, _), (method, _, _) = ch.sent[1:]
    assert "reply_markup" not in edit, "no button kept, none shown"
    assert method == "sendDocument", "the platform's own update got there first; the press still goes through"


def test_the_owner_s_answers_go_to_the_platform_and_its_silence_is_said(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"new": {"ok": True, "session": "ab12cd34", "say": "Session ab12cd34 · retinue."},
                        "input": {"say": "Accepted."}}

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
    assert said == ["Session ab12cd34 · retinue.", "Accepted.", UNAVAILABLE]
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34" and ch.store.sent_event(SESSION, "901") == "ab12cd34"
    assert ch.store.sent_event(SESSION, "902") == "ab12cd34", "a reply to «unavailable» tries the session again"


def test_an_unknown_track_is_said_and_maps_to_nothing(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"new": {"ok": False, "say": "No such track: x. There are: notes, retinue"}}
    asyncio.run(bridge.new("x", "", "7"))
    assert [p["text"] for _, p, _ in ch.sent] == ["No such track: x. There are: notes, retinue"]
    assert ch.store.sent_event(SESSION, "900") is None


def real_call(tmp_path, notes, *answers):
    """A bridge whose channel talks to Telegram through its own `call`, Telegram answering `answers` in turn."""
    store = Store(str(tmp_path / "s.db"))
    bridge = PlatformBridge(FakePlatform(notes), store)
    ch = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="SECRET"), store, bridge=bridge)
    answers = iter(answers)
    ch.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(answers)))
    return bridge, store


SENT = httpx.Response(200, json={"ok": True, "result": {"message_id": 900}})


@pytest.mark.parametrize("passing", [
    httpx.Response(502, text="<html>502 Bad Gateway</html>"),
    httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 3"}),
], ids=["gateway-page", "too-many-requests"])
def test_a_passing_failure_from_telegram_leaves_the_note_unconfirmed(tmp_path, passing):
    bridge, store = real_call(tmp_path, [card(1)], passing, SENT)

    async def run():
        try:
            await bridge.deliver()
        except TelegramError as exc:
            assert exc.code == passing.status_code and "SECRET" not in str(exc), "the URL carries the token"
        assert bridge.neighbour.confirmed == [], "a passing failure is tried again, not said and confirmed past"
        assert await bridge.deliver() == 1

    asyncio.run(run())
    assert bridge.neighbour.confirmed == [1] and store.sent_event(CARD, "900") == "a1"


def test_a_card_telegram_refuses_is_said_and_confirmed_past(tmp_path):
    refused = httpx.Response(400, json={"ok": False, "error_code": 400,
                                        "description": "Bad Request: message is too long"})
    bridge, store = real_call(tmp_path, [card(1)], refused, SENT)
    assert asyncio.run(bridge.deliver()) == 1
    assert bridge.neighbour.confirmed == [1]
    assert store.sent_event(SESSION, "900") == "ab12cd34" and store.sent_event(CARD, "900") is None


FACE = "https://app.example.test"


def test_with_the_faces_address_an_approval_card_offers_it_as_a_mini_app_and_a_courier_card_does_not(tmp_path):
    courier = card(2, approval="c1", buttons=[])
    retired = {"seq": 3, "kind": "update", "session": "ab12cd34", "approval": "a1",
               "data": {"view": {**VIEW, "status": "Allowed — running."}, "buttons": []}}
    ch, bridge, platform = make(tmp_path, [card(1), courier, retired])
    bridge.face_url = FACE
    asyncio.run(bridge.deliver())
    (_, shown, _), (_, courier_sent, _), (edit, retired_sent, _) = ch.sent
    assert shown["reply_markup"]["inline_keyboard"] == [
        *([{"text": label, "callback_data": button}] for label, button in BUTTONS),
        [{"text": "Open in the app", "web_app": {"url": f"{FACE}/a/a1"}}]]
    assert "reply_markup" not in courier_sent, "a courier card has nothing to decide"
    assert edit == "editMessageText" and "reply_markup" not in retired_sent, "a decided card loses the face's button too"


def test_a_card_opened_whole_keeps_the_faces_button_with_its_own(tmp_path):
    ch, bridge, platform = make(tmp_path)
    bridge.face_url = FACE
    ch.store.save_sent(CARD, "900", "a1")
    platform.answers = {"press": {"toast": "The whole input — as a file.", "view": VIEW, "keep": [["Allow", "p.x"]],
                                  "document": {"name": "a1.txt", "text": "make test"}}}
    asyncio.run(bridge.press("q1", "p.open", 900))
    edit = ch.sent[1][1]
    assert edit["reply_markup"]["inline_keyboard"] == [[{"text": "Allow", "callback_data": "p.x"}],
                                                       [{"text": "Open in the app",
                                                         "web_app": {"url": f"{FACE}/a/a1"}}]]


def test_app_says_the_link_on_the_faces_own_host_and_the_code(tmp_path):
    ch, bridge, platform = make(tmp_path)
    bridge.face_url = FACE
    platform.answers = {"login": {"code": "ABCDE23456", "url": f"{FACE}/login#ABCDE23456"}}
    asyncio.run(bridge.login())
    assert platform.calls == [("login", {})]
    text = ch.sent[0][1]["text"]
    assert f'<a href="{FACE}/login#ABCDE23456">{FACE}/login#ABCDE23456</a>' in text
    assert "<code>ABCDE23456</code>" in text and ch.sent[0][1]["link_preview_options"] == {"is_disabled": True}
    assert ch.sent[0][1]["reply_markup"] == {"inline_keyboard": [[{"text": "Open", "web_app": {"url": f"{FACE}/"}}]]}, \
        "«Open»: the app as a Mini App, logged in by Telegram"
    assert ch.store.sent_event(SESSION, "900") is None, "a reply to it goes to no session"

    ch.sent.clear()
    platform.answers = {"login": {"code": "ABCDE23456", "url": "https://evil.example/login#ABCDE23456"}}
    asyncio.run(bridge.login())
    assert "<a " not in ch.sent[0][1]["text"], "a host not the router's own face is never a link"


def test_the_menu_button_opens_the_app_once_set_at_start_and_a_refusal_stops_nothing(tmp_path, caplog):
    ch, bridge, platform = make(tmp_path)
    bridge.face_url = FACE
    asyncio.run(ch.set_app_button())
    asyncio.run(ch.set_app_button())
    sent = [params for method, params, _ in ch.sent if method == "setChatMenuButton"]
    assert [p["menu_button"] for p in sent] == [{"type": "web_app", "text": "Open", "web_app": {"url": f"{FACE}/"}}] * 2, \
        "the same call"
    assert all(p["chat_id"] == ch.cfg.owner_id for p in sent), "the owner's chat, never every private chat's default"
    ch.sent.clear()
    bridge.face_url = ""
    asyncio.run(ch.set_app_button())
    assert ch.sent[0][:2] == ("setChatMenuButton", {"chat_id": ch.cfg.owner_id, "menu_button": {"type": "default"}}), \
        "no face: Telegram's own"

    async def refuse(method, files=None, **params):
        raise TelegramError(f"{method}: 400 Bad Request", 400)

    ch.call = refuse
    asyncio.run(ch.set_app_button())
    assert "menu button not set" in caplog.text


def test_app_without_a_face_or_a_platform_is_said(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"login": {"error": "The app is not set up: the platform has no RETINUE_FACE_ORIGIN."}}
    asyncio.run(bridge.login())
    platform.down = True
    asyncio.run(bridge.login())
    assert [p["text"] for _, p, _ in ch.sent] == ["The app is not set up: the platform has no RETINUE_FACE_ORIGIN.",
                                                  UNAVAILABLE]


def test_the_pump_backs_off_while_the_platform_is_away_and_comes_back_to_its_pace(tmp_path, monkeypatch):
    _, bridge, platform = make(tmp_path)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        platform.down = len(waits) < 6
        if len(waits) == 7:
            raise asyncio.CancelledError

    platform.down = True
    monkeypatch.setattr(bridge_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridge.pump())
    assert waits == [4.0, 8.0, 16.0, 30.0, 30.0, 30.0, bridge_module.PUMP_S]


def test_a_card_rewritten_with_its_buttons_keeps_the_faces_button(tmp_path):
    """The idle line rewrites a card that still asks: its buttons and «Open in the app» stay under it."""
    idle = {**VIEW, "head": [*VIEW["head"], "The session is idle 55 min — going on rereads the context without the cache"]}
    rewritten = {"seq": 2, "kind": "update", "session": "ab12cd34", "approval": "a1",
                 "data": {"view": idle, "buttons": BUTTONS}}
    ch, bridge, platform = make(tmp_path, [card(1), rewritten])
    bridge.face_url = FACE
    asyncio.run(bridge.deliver())
    (_, shown, _), (edit, params, _) = ch.sent
    assert edit == "editMessageText" and "The session is idle 55 min" in params["text"]
    assert params["reply_markup"] == shown["reply_markup"]


class FakeScribe:
    """Speech to text that remembers what it was given."""

    def __init__(self, text="", error=""):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, data, filename, media_type):
        self.calls.append((filename, media_type, data))
        await asyncio.sleep(0)  # ElevenLabs takes its time: the owner sees «typing…» meanwhile
        if self.error:
            raise SpeechError(self.error)
        return self.text


class VoiceCore:
    """What the channel needs of the core for a session's voice: its scribe, and a word to the owner."""

    def __init__(self, scribe):
        self.scribe, self.told = scribe, []

    async def tell_owner(self, text, **kwargs):
        self.told.append(text)


def voiced(tmp_path, scribe):
    """The real bridge behind the channel, a Bot API that serves files, a message 900 that is session ab12cd34's."""
    ch, bridge, platform = make(tmp_path)
    plain = ch.call

    async def call(method, files=None, **params):
        if method == "getFile":
            return {"file_path": f"voice/{params['file_id']}.oga"}
        return await plain(method, files, **params)

    ch.call, ch.core = call, VoiceCore(scribe)
    ch.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"OggS")))
    ch.store.save_sent(SESSION, "800", "ab12cd34")
    platform.answers = {"input": {"say": "Accepted."}}
    return ch, platform


def voice_update(update_id=1, message_id=10, **voice):
    return {"update_id": update_id, "message": {
        "message_id": message_id, "from": {"id": OWNER}, "chat": {"id": OWNER}, "reply_to_message": {"message_id": 800},
        "voice": {"file_id": "v1", "duration": 4, "mime_type": "audio/ogg", "file_size": 1000, **voice}}}


def test_a_voice_reply_goes_into_its_session_as_its_transcript_and_the_owner_sees_what_was_heard(tmp_path):
    scribe = FakeScribe("и прогони тесты")
    ch, platform = voiced(tmp_path, scribe)
    asyncio.run(ch.consume(voice_update()))
    assert scribe.calls == [("voice.ogg", "audio/ogg", b"OggS")], "the assistant's own speech path"
    assert platform.calls == [("input", {"input_id": "tg:10", "session": "ab12cd34",
                                         "text": "(by voice) и прогони тесты"})]
    (echo,) = [(900 + i, p["text"]) for i, (m, p, _) in enumerate(ch.sent) if m == "sendMessage"]
    assert echo[1] == "🎙 «и прогони тесты»\n\nAccepted.", "what was heard and the answer: one message"
    assert ch.store.sent_event(SESSION, str(echo[0])) == "ab12cd34", "a reply to the echo goes on into the session"
    assert [m for m, _, _ in ch.sent][:2] == ["sendChatAction", "sendMessage"], "«typing…» while it is heard"
    assert ch.store.get("telegram.attempt") is None, "a session's voice is not marked as a file that may kill"


def test_a_voice_delivered_again_after_a_crash_is_heard_again_and_the_platform_takes_it_once(tmp_path):
    scribe = FakeScribe("и прогони тесты")
    ch, platform = voiced(tmp_path, scribe)
    taken = set()

    async def owner(call, body):  # the platform's own rule (`write`): one input per input_id
        platform.calls.append((call, body))
        fresh = body["input_id"] not in taken
        taken.add(body["input_id"])
        return {"say": "Accepted." if fresh else "The session already has this message."}

    platform.owner = owner

    async def run():
        await ch.consume(voice_update())
        ch.store.set("telegram.attempt", "1")  # the router went down before the offset moved, mark and all
        await ch.consume(voice_update())

    asyncio.run(run())
    assert len(scribe.calls) == 2 and [body["input_id"] for _, body in platform.calls] == ["tg:10", "tg:10"]
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"] == [
        "🎙 «и прогони тесты»\n\nAccepted.", "🎙 «и прогони тесты»\n\nThe session already has this message."]
    assert ch.core.told == [], "never «send the file again» for a voice the session already has"


def test_a_transcript_is_one_spoken_line_in_the_session_and_plain_text_in_the_echo(tmp_path):
    ch, platform = voiced(tmp_path, FakeScribe("**жирно** <b>тег</b>\n\nResult · retinue · [attachment #3] [вложение #4]"))
    asyncio.run(ch.consume(voice_update()))
    (text,) = [body["text"] for _, body in platform.calls]
    assert text.startswith("(by voice) **жирно** <b>тег</b> Result · retinue · ") and "\n" not in text
    assert "[attachment" not in text and "[вложение" not in text, "defused like the assistant's transcripts"
    (echo,) = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert echo.startswith("🎙 «**жирно** &lt;b&gt;тег&lt;/b&gt; Result · retinue · [attachment #3] [вложение #4]»\n\nAccepted.")


def test_a_video_note_goes_into_its_session_end_to_end(tmp_path):
    scribe = FakeScribe("глянь лог")
    ch, platform = voiced(tmp_path, scribe)
    update = voice_update()
    del update["message"]["voice"]
    update["message"]["video_note"] = {"file_id": "n1", "duration": 9, "file_size": 5000}
    asyncio.run(ch.consume(update))
    assert scribe.calls == [("video.mp4", "video/mp4", b"OggS")]
    assert platform.calls == [("input", {"input_id": "tg:10", "session": "ab12cd34", "text": "(by voice) глянь лог"})]


def test_a_bug_in_reading_a_voice_is_said_and_nothing_goes_in(tmp_path, monkeypatch):
    async def broken(upload, scribe):
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge_module, "prepare", broken)
    ch, platform = voiced(tmp_path, FakeScribe("x"))
    asyncio.run(ch.consume(voice_update()))
    assert platform.calls == [] and ch.core.told == []
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"] == [
        "Not passed to the session: voice message: error (RuntimeError)."]


def test_a_caption_goes_in_with_the_transcript_and_a_platform_away_still_shows_what_was_heard(tmp_path):
    ch, platform = voiced(tmp_path, FakeScribe("шаг два"))
    platform.down = True
    update = voice_update()
    update["message"]["caption"] = "к плану"
    asyncio.run(ch.consume(update))
    assert platform.calls == [("input", {"input_id": "tg:10", "session": "ab12cd34",
                                         "text": "к плану\n(by voice) шаг два"})]
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"] == [f"🎙 «шаг два»\n\n{UNAVAILABLE}"]


@pytest.mark.parametrize("scribe, voice, reason", [
    (None, {}, "voice message 0:04: transcription is not set up"),
    (FakeScribe(error="ElevenLabs is unavailable (ConnectError)"), {},
     "voice message 0:04: ElevenLabs is unavailable (ConnectError)"),
    (FakeScribe("   "), {}, "voice message 0:04: no speech to make out"),
    (FakeScribe("не дойдёт"), {"file_size": 25 * 2**20}, "voice message, 25 MB: Telegram gives bots files up to 20 MB"),
], ids=["no-scribe", "provider-down", "empty", "too-big"])
def test_speech_that_cannot_be_read_is_said_and_nothing_goes_into_the_session(tmp_path, scribe, voice, reason):
    ch, platform = voiced(tmp_path, scribe)
    asyncio.run(ch.consume(voice_update(**voice)))
    assert platform.calls == []
    sent = [(900 + i, p["text"]) for i, (m, p, _) in enumerate(ch.sent) if m == "sendMessage"]
    assert [text for _, text in sent] == [f"Not passed to the session: {reason}."]
    assert ch.store.sent_event(SESSION, str(sent[0][0])) == "ab12cd34", "answering the refusal goes on in the session"
    assert ch.core.told == [], "said by the platform's side, in the session's thread"


# --- photos and files into a session ----------------------------------------------------------------------------

FILES = {"p1": b"\xff\xd8photo-one", "p2": b"\xff\xd8photo-two", "d1": b"PK\x03\x04zip"}


def filed(tmp_path):
    """The real bridge behind the channel, a Bot API that serves FILES by id, a message 800 that is ab12cd34's."""
    ch, bridge, platform = make(tmp_path)
    plain = ch.call

    async def call(method, files=None, **params):
        if method == "getFile":
            ch.fetched.append(params["file_id"])
            return {"file_path": f"f/{params['file_id']}"}
        return await plain(method, files, **params)

    ch.call, ch.core, ch.fetched = call, VoiceCore(None), []
    ch.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=FILES[request.url.path.rsplit("/", 1)[-1]])))
    ch.store.save_sent(SESSION, "800", "ab12cd34")
    platform.answers = {"input": {"say": "Accepted."}}
    return ch, bridge, platform


def file_update(update_id=1, message_id=10, **media):
    return {"update_id": update_id, "message": {
        "message_id": message_id, "from": {"id": OWNER}, "chat": {"id": OWNER}, "reply_to_message": {"message_id": 800},
        **media}}


def test_a_photo_and_a_file_in_reply_go_to_their_session_whole_and_unread(tmp_path):
    ch, _, platform = filed(tmp_path)

    async def run():
        await ch.consume(file_update(photo=[{"file_id": "small"}, {"file_id": "p1", "file_size": 11}], caption="что тут?"))
        await ch.consume(file_update(2, 11, document={"file_id": "d1", "file_name": "logs.zip",
                                                      "mime_type": "application/zip", "file_size": 7}))

    asyncio.run(run())
    assert platform.calls == [
        ("input", {"input_id": "tg:10", "session": "ab12cd34", "text": "что тут?", "attachments": [
            {"name": "", "media_type": "image/jpeg", "data": base64.b64encode(FILES["p1"]).decode(),
             "sha256": hashlib.sha256(FILES["p1"]).hexdigest()}]}),
        ("input", {"input_id": "tg:11", "session": "ab12cd34", "text": "", "attachments": [
            {"name": "logs.zip", "media_type": "application/zip", "data": base64.b64encode(FILES["d1"]).decode(),
             "sha256": hashlib.sha256(FILES["d1"]).hexdigest()}]})]
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"] == ["Accepted.", "Accepted."]
    assert ch.store.get("telegram.attempt") is None, "nothing is parsed in the router: no poison mark"
    assert ch.core.told == []


def test_an_album_is_one_input_under_its_first_part_with_the_captions_joined(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "ALBUM_S", 0.01)
    ch, _, platform = filed(tmp_path)

    async def run():
        await ch.consume(file_update(1, 10, photo=[{"file_id": "p1"}], media_group_id="g", caption="два фото"))
        await ch.consume(file_update(2, 11, photo=[{"file_id": "p2"}], media_group_id="g"))
        await asyncio.sleep(0.2)

    asyncio.run(run())
    [(call, body)] = platform.calls
    assert (call, body["input_id"], body["text"]) == ("input", "tg:10", "два фото")
    assert [base64.b64decode(a["data"]) for a in body["attachments"]] == [FILES["p1"], FILES["p2"]]


def test_a_file_over_the_limits_is_never_downloaded_and_is_said_with_the_rest_going_in(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "ALBUM_S", 0.01)
    ch, _, platform = filed(tmp_path)
    huge = 25 * 1024 * 1024

    async def run():
        await ch.consume(file_update(1, 10, document={"file_id": "big", "file_name": "big.iso", "file_size": huge}))
        await ch.consume(file_update(2, 11, photo=[{"file_id": "p1", "file_size": 11}], media_group_id="g"))
        await ch.consume(file_update(3, 12, document={"file_id": "d1", "file_name": "y.bin", "file_size": 16 * 2**20},
                                     media_group_id="g"))
        await ch.consume(file_update(4, 13, document={"file_id": "d1", "file_name": "x.bin", "file_size": 16 * 2**20},
                                     media_group_id="g"))
        await asyncio.sleep(0.2)

    asyncio.run(run())
    [(_, body)] = platform.calls
    assert [a["name"] for a in body["attachments"]] == ["", "y.bin"]
    said = [re.sub("</?code>", "", p["text"]) for m, p, _ in ch.sent if m == "sendMessage"]  # names look like hosts
    assert said[0].startswith('Not passed to the session: file "big.iso", 25 MB: Telegram gives bots files up to 20 MB')
    assert said[1] == 'Accepted.\nNot passed to the session: file "x.bin": over 30 MB together with the rest.'
    assert ch.fetched == ["p1", "d1"], "big.iso and x.bin never downloaded"


def test_files_for_a_platform_that_is_down_are_said_so_and_a_reply_tries_again(tmp_path):
    ch, _, platform = filed(tmp_path)
    platform.down = True
    asyncio.run(ch.consume(file_update(photo=[{"file_id": "p1"}])))
    assert [p["text"] for m, p, _ in ch.sent if m == "sendMessage"] == ["The platform is unavailable — try again later."]


def test_an_album_cut_by_a_restart_is_kept_and_completed_by_the_parts_telegram_gives_again(tmp_path, monkeypatch):
    """The router dies after the first part was kept: the next one gathers it again on start, the redelivered
    first part is not taken twice, the second joins by its group, and one input goes in."""
    monkeypatch.setattr(bridge_module, "ALBUM_S", 0.05)
    ch, bridge, platform = filed(tmp_path)

    async def before():
        await ch.consume(file_update(1, 10, photo=[{"file_id": "p1"}], media_group_id="g", caption="два фото"))
        for timer in bridge.timers.values():  # the process dies: its timer with it
            timer.cancel()

    asyncio.run(before())
    assert platform.calls == [] and json.loads(ch.store.get(bridge_module.ALBUMS))["g"]["parts"][0]["message_id"] == 10

    ch2, bridge2, platform2 = filed(tmp_path)  # a new router on the same database

    async def after():
        await bridge2.recover()
        await ch2.consume(file_update(1, 10, photo=[{"file_id": "p1"}], media_group_id="g", caption="два фото"))
        await ch2.consume(file_update(2, 11, photo=[{"file_id": "p2"}], media_group_id="g"))
        await asyncio.sleep(0.3)

    asyncio.run(after())
    [(_, body)] = platform2.calls
    assert (body["input_id"], body["text"]) == ("tg:10", "два фото")
    assert [base64.b64decode(a["data"]) for a in body["attachments"]] == [FILES["p1"], FILES["p2"]]
    assert json.loads(ch2.store.get(bridge_module.ALBUMS)) == {}


def test_an_album_caught_while_it_was_being_sent_goes_again_on_start(tmp_path, monkeypatch):
    ch, bridge, platform = filed(tmp_path)
    ch.store.set(bridge_module.ALBUMS, json.dumps({"g": {"group": "g", "session": "ab12cd34", "sending": True, "parts": [
        {"message_id": 10, "media_group_id": "g", "photo": [{"file_id": "p1"}]}]}}))

    async def run():
        await bridge.recover()
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert [body["input_id"] for _, body in platform.calls] == ["tg:10"], "the platform takes it once by its id"


def test_an_album_that_cannot_be_completed_is_said_aloud(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "ALBUM_S", 0.01)
    ch, bridge, platform = filed(tmp_path)

    async def broken(call, body, timeout=60):
        raise RuntimeError("bug")

    platform.owner = broken

    async def run():
        await ch.consume(file_update(1, 10, photo=[{"file_id": "p1"}], media_group_id="g"))
        await ch.consume(file_update(2, 11, photo=[{"file_id": "p2"}], media_group_id="g"))
        await asyncio.sleep(0.2)

    asyncio.run(run())
    said = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert said == ["Not passed to the session: an album of 2 files was not put together — an error on the router's side. "
                    "Send it again."]
    assert json.loads(ch.store.get(bridge_module.ALBUMS)) == {}


def test_a_question_s_message_offers_its_card_in_the_app_and_a_reply_to_it_names_the_card(tmp_path):
    """One message per question, keyed `<approval>/<n>`: the app opens the whole card, its own update edits only it,
    and the owner's reply to it carries the key — the platform takes it as his own answer while the question asks."""
    first = card(1, approval="q1/0", buttons=[["Центр", "p.a"], ["Окраина", "p.b"]])
    second = card(2, approval="q1/1", buttons=[["☐ Зонт", "p.c"], ["Done", "p.d"]])
    answered = {"seq": 3, "kind": "update", "session": "ab12cd34", "approval": "q1/0",
                "data": {"view": {**VIEW, "status": "Answer: Окраина"}, "buttons": []}}
    ch, bridge, platform = make(tmp_path, [first, second, answered])
    bridge.face_url = FACE
    platform.answers = {"input": {"say": "Answer passed to the session."}}

    async def run():
        await bridge.deliver()
        await bridge.input("ab12cd34", "У моря", "8", reply_to="901")
        await bridge.input("ab12cd34", "и ещё", "9", reply_to="700")

    asyncio.run(run())
    (_, shown, _), _, (edit, edited, _), _, _ = ch.sent
    assert shown["reply_markup"]["inline_keyboard"][-1] == [{"text": "Open in the app",
                                                             "web_app": {"url": f"{FACE}/a/q1"}}]
    assert edit == "editMessageText" and edited["message_id"] == 900, "only the first question's message"
    assert platform.calls == [("input", {"input_id": "tg:8", "session": "ab12cd34", "text": "У моря", "card": "q1/1"}),
                              ("input", {"input_id": "tg:9", "session": "ab12cd34", "text": "и ещё"})]


def test_a_photo_in_reply_to_a_question_card_carries_no_card(tmp_path):
    ch, bridge, platform = filed(tmp_path)
    ch.store.save_sent(CARD, "800", "q1/0")
    asyncio.run(ch.consume(file_update(photo=[{"file_id": "p1"}], caption="У моря")))
    [(call, body)] = platform.calls
    assert call == "input" and "card" not in body and body["attachments"]


# --- «Waiting for you»: the assistant's digest pushed, and `/wait` -------------------------------------------------------

def test_the_assistants_digest_is_pushed_when_it_changes_and_at_least_hourly(tmp_path, caplog):
    ch, bridge, platform = make(tmp_path)
    ch.username = "retinue_bot"
    digest = [{"id": "chat:1", "kind": "chat", "who": "Мария", "title": "ответить", "since": 1.0, "sphere": None}]
    bridge.waiting = lambda now: list(digest)
    platform.answers = {"waiting": {"ok": True}}

    async def run():
        assert await bridge.push_waiting(1000.0)
        assert not await bridge.push_waiting(1010.0), "looked at every 30 s, not every pump"
        assert not await bridge.push_waiting(1100.0), "the same digest is not pushed again"
        digest.append({**digest[0], "id": "chat:2"})
        assert await bridge.push_waiting(1200.0)
        assert await bridge.push_waiting(1200.0 + bridge_module.WAITING_PUSH_S), "an hour later, changed or not"
        platform.answers = {"waiting": {"error": "the digest is bigger than the platform takes (413)"}}
        digest.append({**digest[0], "id": "chat:3"})
        assert await bridge.push_waiting(9000.0)
        assert not await bridge.push_waiting(9100.0), "a refused digest is not hammered"
        platform.down = True
        digest.append({**digest[0], "id": "chat:4"})
        with pytest.raises(Unavailable):
            await bridge.push_waiting(9200.0)

    asyncio.run(run())
    pushed = [body for call, body in platform.calls if call == "waiting"]
    assert pushed[0] == {"items": digest[:1], "bot": "retinue_bot"} and len(pushed) == 5
    assert "refused the assistant's digest" in caplog.text
    assert not asyncio.run(PlatformBridge(FakePlatform(), ch.store).push_waiting(1.0)), "no digest, no push"


def test_the_hubs_chips_go_with_the_digest_and_a_change_of_them_is_a_change(tmp_path):
    ch, bridge, platform = make(tmp_path)
    chips = [{"key": "backup", "text": "бэкап 1 ч назад", "level": "ok"}]
    bridge.waiting = lambda now: []
    bridge.chips = lambda now: list(chips)
    platform.answers = {"waiting": {"ok": True}}

    async def run():
        assert await bridge.push_waiting(1000.0)
        assert not await bridge.push_waiting(1100.0)
        chips[0] = {**chips[0], "text": "бэкап 2 ч назад"}
        assert await bridge.push_waiting(1200.0), "the chips changed"

    asyncio.run(run())
    pushed = [body for call, body in platform.calls if call == "waiting"]
    assert pushed[0]["chips"] == [{"key": "backup", "text": "бэкап 1 ч назад", "level": "ok"}] and len(pushed) == 2


def test_a_hung_push_holds_back_no_card(tmp_path):
    ch, bridge, platform = make(tmp_path, [card(1)])
    stuck = asyncio.Event()

    async def hang(body):
        await stuck.wait()

    platform.waiting = hang
    bridge.waiting = lambda now: []

    async def run():
        pump = asyncio.create_task(bridge.pump())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if platform.confirmed:
                break
        pump.cancel()
        bridge._pushing.cancel()

    asyncio.run(run())
    assert platform.confirmed == [1], "the card went out while the push hung"


def test_a_digest_too_big_for_the_platform_is_its_refusal_and_a_push_waits_ten_seconds(tmp_path):
    seen = []

    def answer(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(413, text="<html>too big</html>")

    client = bridge_module.PlatformClient("http://platform:9500", "t", "the platform",
                                          httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    said = asyncio.run(client.waiting({"items": []}))
    assert "413" in said["error"] and seen[0]["read"] == bridge_module.WAITING_TIMEOUT_S


WAIT_ROWS = [
    {"action": "answer", "group": "Answer the question", "source": "question", "session": "ab12cd34",
     "approval": "q1", "card": "q1/0", "where": "Путешествия", "title": "куда поехать", "since": 0.0,
     "essence": "Где <жить>?"},
    {"action": "decide", "group": "Decide", "source": "approval", "session": "cd34ef56", "approval": "a1",
     "card": None, "where": "retinue", "title": "почини", "since": 0.0, "essence": "make test"},
    {"action": "assistant", "group": "From the assistant", "source": "chat", "session": None, "approval": None,
     "card": None, "where": "", "title": "Мария", "since": None, "essence": "ответить: трансфер"},
    {"action": "assistant", "group": "From the assistant", "source": "deadline", "session": None, "approval": None,
     "card": None, "where": "reading", "title": "Base", "since": None, "essence": "today: the report is due"},
]


class HerCore:
    def __init__(self):
        self.told = []

    async def tell_owner(self, text, **kwargs):
        self.told.append((text, kwargs))
        return "event-1"


def test_wait_says_the_queue_one_replyable_message_per_session_row(tmp_path):
    ch, bridge, platform = make(tmp_path)
    ch.core = HerCore()
    bridge.face_url = FACE
    platform.answers = {"feed": {"count": 14, "rows": WAIT_ROWS, "more": 10},
                        "input": {"say": "Answer passed to the session."}}

    async def run():
        assert await ch.on_platform_message({"text": "/wait"}, "/wait", "70", None)
        await bridge.input("ab12cd34", "У моря", "71", reply_to="901")

    asyncio.run(run())
    texts = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert texts[0] == "Waiting for you: 14. Reply to a line — it goes to its session.\n10 more — in the app."
    assert texts[1].startswith("<b>Answer the question</b>\n❓ Путешествия · куда поехать · ")
    assert "Где &lt;жить&gt;?" in texts[1] and texts[1].endswith("<code>session ab12cd34</code>")
    assert texts[2].startswith("<b>Decide</b>\n⚠️ retinue · почини")
    assert texts[2].endswith(f"<i>{bridge_module.DECIDE_HINT}</i>")
    assert texts[3:] == ["Answer passed to the session."], "hers go through her conversation, not here"
    [(said, how)] = ch.core.told
    assert said.splitlines() == ["Waiting for you from the assistant:", "- Мария: ответить: трансфер",
                                 "- Base (reading): today: the report is due",
                                 "Reply to this message — it goes to the assistant."]
    assert how == {"origin": ch}, "on record in her conversation: a reply to it reaches her with it quoted"
    buttons = [p.get("reply_markup") for m, p, _ in ch.sent if m == "sendMessage"]
    assert buttons[1] == {"inline_keyboard": [[{"text": "Open in the app", "web_app": {"url": f"{FACE}/a/q1"}}]]}
    assert buttons[2]["inline_keyboard"][0][0]["web_app"]["url"] == f"{FACE}/a/a1"
    assert [ch.store.sent_event(SESSION, str(n)) for n in range(900, 903)] == [None, "ab12cd34", None], \
        "a card to decide is not answered by a reply: that would only make it stale"
    assert ch.store.sent_event(CARD, "901") is None, "a card's update never rewrites the /wait line"
    assert ch.store.sent_event(bridge_module.QUESTION, "901") == "q1/0"
    assert platform.calls[0] == ("feed", {})
    assert platform.calls[1] == ("input", {"input_id": "tg:71", "session": "ab12cd34", "text": "У моря",
                                           "card": "q1/0"}), "a reply to a question's line is its answer"


def test_wait_with_nothing_waiting_or_no_platform_is_said(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers = {"feed": {"count": 0, "rows": [], "more": 0}}
    asyncio.run(bridge.wait())
    platform.down = True
    asyncio.run(bridge.wait())
    assert [p["text"] for _, p, _ in ch.sent] == ["Nothing is waiting for you.", UNAVAILABLE]


def test_with_a_platform_the_bot_menu_has_wait_and_the_bots_name_is_kept_for_the_face(tmp_path):
    ch, bridge, platform = make(tmp_path)
    plain = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="t"), ch.store)

    async def call(method, files=None, **params):
        ch.sent.append((method, params, files))
        return {"username": "retinue_bot"} if method == "getMe" else True

    async def idle():
        return None

    async def run():
        for channel in (ch, plain):
            channel.call, channel.poll = call, idle
            await channel.start(object())

    asyncio.run(run())
    menus = [[c["command"] for c in p["commands"]] for m, p, _ in ch.sent if m == "setMyCommands"]
    assert menus[0][-1] == "wait" and "wait" not in menus[1], "only with the platform"
    assert ch.username == "retinue_bot"


def test_the_spheres_are_asked_of_the_platform_kept_and_put_in_the_menu_when_they_change(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers["spheres"] = {"spheres": [
        {"name": "travel", "title": "Путешествия", "icon": "✈️", "track": "notes"},
        {"name": "new", "title": "чужая команда"}, {"name": "Bad name"}, "x"]}

    async def run():
        await bridge.refresh_spheres()
        await bridge.refresh_spheres()  # the same list: the menu is not set again

    asyncio.run(run())
    assert list(bridge.spheres()) == ["travel"], "a name of the bot's own commands or not a command's shape is not one"
    [(_, params, _)] = [c for c in ch.sent if c[0] == "setMyCommands"]
    assert params["commands"][-1] == {"command": "travel", "description": "✈️ Путешествия"}
    again = PlatformBridge(FakePlatform(), ch.store)
    assert list(again.spheres()) == ["travel"], "known across a restart of the router"


def test_a_sphere_command_is_answered_and_the_answer_maps_to_the_main_session(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers["sphere"] = {"ok": True, "session": "ab12cd34", "say": "✈️ Путешествия · сессия ab12cd34: Принято."}
    asyncio.run(bridge.sphere("travel", "найди жильё", "77"))
    assert platform.calls == [("sphere", {"input_id": "tg:77", "sphere": "travel", "text": "найди жильё"})]
    [(_, params, _)] = ch.sent
    assert params["text"] == "✈️ Путешествия · сессия ab12cd34: Принято."
    assert ch.store.sent_event(SESSION, "900") == "ab12cd34", "a reply to it goes into the main session"
    platform.down = True
    asyncio.run(bridge.sphere("travel", "", "78"))
    assert ch.sent[-1][1]["text"] == UNAVAILABLE


def test_a_press_in_the_app_is_marked_by_the_router_and_never_holds_back_the_cards_behind_it(tmp_path):
    press = lambda seq, t, action="done": {"seq": seq, "kind": "waiting-press", "session": "",  # noqa: E731
                                           "data": {"t": t, "action": action}}
    notes = [press(1, "a" * 32), press(2, "b" * 32), press(3, "c" * 32), {"seq": 4, "kind": "waiting-press",
                                                                         "session": "", "data": "битая"}, card(5)]
    ch, bridge, platform = make(tmp_path, notes)
    pressed = []

    def apply(t, action, now):
        pressed.append((t, action))
        if t == "c" * 32:
            raise RuntimeError("the router's tables are locked")
        return t == "a" * 32

    bridge.press_waiting, bridge._looked = apply, 1e12
    assert asyncio.run(bridge.deliver()) == 5
    assert pressed == [("a" * 32, "done"), ("b" * 32, "done"), ("c" * 32, "done")]
    assert bridge._looked == 0.0, "applied: the digest goes again at once"
    said = [p["text"] for m, p, _ in ch.sent if m == "sendMessage"]
    assert said.count(bridge_module.STALE_PRESS) == 1, "the token the router does not know: nothing applied"
    assert said.count(bridge_module.FAILED_PRESS) == 2, "the fault and the malformed note: told, no details"
    assert said[-1].startswith("Permission"), "the card behind a failed press went out"
    assert platform.confirmed == [5]
