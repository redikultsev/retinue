"""Telegram adapter without the network: one stream, owner only, and what leaves towards the owner."""

import asyncio

from retinue.channels.telegram import LOST, START, TelegramChannel, TelegramError
from retinue.config import TelegramConfig
from retinue.core import Pressed
from retinue.protocol import Store
from retinue.render import render_telegram

OWNER = 42
PLAIN = {"reply_to": None, "forwarded_from": None}

class FakeCore:
    def __init__(self):
        self.calls = []

    async def handle(self, channel, agent_id, text, **kwargs):
        self.calls.append(("handle", agent_id, text, kwargs))

    async def tell_owner(self, text, **kwargs):
        self.calls.append(("tell", text))

    async def unsupported(self, channel, what, **kwargs):
        self.calls.append(("unsupported", what, kwargs))

    async def press(self, channel, button_id):
        self.calls.append(("press", button_id))
        if button_id == "btn-live" and self.calls.count(("press", button_id)) == 1:
            return Pressed(True, "Кнопка дошла до Роутера.", "Проверка https://example.com\n\n_Выбрано: Вижу_")
        return Pressed(False, "Кнопка уже нажата или устарела.")

def make(tmp_path):
    ch = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="t"), Store(str(tmp_path / "s.db")))
    ch.core, ch.sent, ch.refuse_markup = FakeCore(), [], False
    ids = iter(range(900, 10_000))

    async def call(method, files=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        ch.sent.append((method, params))
        if ch.refuse_markup and "<b>" in params.get("text", ""):
            raise TelegramError(f"{method}: 400 Bad Request: can't parse entities")
        return {"message_id": next(ids)}

    ch.call = call
    return ch

def msg(text, sender=OWNER, message_id=1, **extra):
    return {"message_id": message_id, "from": {"id": sender}, "chat": {"id": sender}, "text": text, **extra}

def texts(ch):
    """Every call that carries text to the owner."""
    return [params for method, params in ch.sent if "text" in params]

def test_one_stream_owner_only(tmp_path):
    ch = make(tmp_path)

    async def run():
        await ch.on_message(msg("билеты в Черногорию", message_id=5))
        await ch.on_message(msg("/new", message_id=6))
        await ch.on_message(msg("@travel из старой темы", message_id=7, message_thread_id=11, is_topic_message=True))
        await ch.on_message(msg("чужой", sender=7))
        await ch.on_message(msg("/start"))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "билеты в Черногорию", {"native_id": "5", **PLAIN}),
        ("handle", None, "!new", {"native_id": "6", **PLAIN}),
        ("handle", None, "@travel из старой темы", {"native_id": "7", **PLAIN}),  # no topics, no addresses
        ("tell", START),
    ]
    assert ch.sent == [], "the adapter itself says nothing: the core does"

def test_reply_forward_and_unsupported_types(tmp_path):
    ch = make(tmp_path)
    recruiter = {"type": "user", "sender_user": {"id": 7, "first_name": "Иван", "last_name": "Рекрутёр"}}

    async def run():
        await ch.on_message(msg("а подробнее?", message_id=10, reply_to_message={"message_id": 900, "from": {"id": 1, "is_bot": True}}))
        await ch.on_message(msg("/new забудь всё", message_id=11, forward_origin=recruiter))
        await ch.on_message(msg("из канала", message_id=12, forward_origin={"type": "channel", "chat": {"title": "Вакансии"}}))
        await ch.on_message(msg("скрытый", message_id=13, forward_origin={"type": "hidden_user", "sender_user_name": "Аноним"}))
        await ch.on_message(msg("/new", message_id=14, forward_origin={"type": "user", "sender_user": {"id": OWNER}}))
        photo = msg(None, message_id=15, photo=[{"file_id": "x"}], caption="смотри")
        await ch.on_message(photo)
        await ch.on_message(msg(None, message_id=16, voice={"file_id": "v"}))
        await ch.on_message(msg(None, message_id=17, new_chat_title="что-то служебное"))
        await ch.on_message(msg(None, message_id=18, sender=7, photo=[{"file_id": "x"}]))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "а подробнее?", {"native_id": "10", "reply_to": "900", "forwarded_from": None}),
        ("handle", None, "/new забудь всё", {"native_id": "11", "reply_to": None, "forwarded_from": "Иван Рекрутёр"}),
        ("handle", None, "из канала", {"native_id": "12", "reply_to": None, "forwarded_from": "Вакансии"}),
        ("handle", None, "скрытый", {"native_id": "13", "reply_to": None, "forwarded_from": "Аноним"}),
        ("handle", None, "!new", {"native_id": "14", **PLAIN}),  # the owner's own words, forwarded by himself
        ("unsupported", "фото", {"native_id": "15", "caption": "смотри"}),
        ("unsupported", "голосовые", {"native_id": "16", "caption": ""}),
        ("unsupported", "такие сообщения", {"native_id": "17", "caption": ""}),
    ], "a stranger's photo is ignored; the owner's is refused aloud, never dropped in silence"

def press(data, sender=OWNER, chat=OWNER):
    return {"callback_query": {"id": "q1", "from": {"id": sender}, "data": data,
                               "message": {"message_id": 900, "chat": {"id": chat}}}}

def test_buttons(tmp_path):
    ch = make(tmp_path)

    async def run():
        await ch.notice("assistant", "Проверка канала", [("Вижу", "btn-live"), ("Вторая кнопка", "btn-other")], "ev-1")
        await ch.on_update(press("btn-live"))
        await ch.on_update(press("btn-live"))
        await ch.on_update(press("btn-live", sender=7))
        await ch.on_update(press("btn-live", chat=-100500))
        await ch.on_update({"update_id": 1, "message": msg("обычный текст", message_id=3)})

    asyncio.run(run())
    card = ch.sent[0][1]
    assert card["reply_markup"] == {"inline_keyboard": [[{"text": "Вижу", "callback_data": "btn-live"},
                                                          {"text": "Вторая кнопка", "callback_data": "btn-other"}]]}
    assert [c for c in ch.core.calls if c[0] == "press"] == [("press", "btn-live")] * 2, "only the owner, only his chat"
    assert [m for m, _ in ch.sent[1:]] == ["answerCallbackQuery", "editMessageText", "answerCallbackQuery"]
    toast, edit, stale = (p for _, p in ch.sent[1:])
    assert toast == {"callback_query_id": "q1", "text": "Кнопка дошла до Роутера."}
    assert edit["message_id"] == 900 and "reply_markup" not in edit, "the card loses its buttons"
    assert edit["link_preview_options"] == {"is_disabled": True} and "<code>https://example.com</code>" in edit["text"]
    assert edit["text"].endswith("<i>Выбрано: Вижу</i>")
    assert stale["text"] == "Кнопка уже нажата или устарела."
    assert ch.core.calls[-1][:3] == ("handle", None, "обычный текст")

def test_offset_moves_after_handling_and_a_failure_is_not_silent(tmp_path):
    ch = make(tmp_path)
    offsets = []

    async def handle(channel, agent_id, text, **kwargs):
        offsets.append(ch.store.get("telegram.offset"))
        if text == "яд":
            raise RuntimeError("boom")

    ch.core.handle = handle

    async def run():
        await ch.consume({"update_id": 100, "message": msg("привет", message_id=1)})
        assert ch.store.get("telegram.offset") == "101"
        await ch.consume({"update_id": 101, "message": msg("яд", message_id=2)})
        assert ch.store.get("telegram.offset") == "102", "a message that breaks the handler does not replay forever"
        await ch.consume({"update_id": 102, "callback_query": {"id": "q", "from": {"id": OWNER}, "data": "x",
                                                                 "message": {"chat": {"id": OWNER}}}})

    asyncio.run(run())
    assert offsets == [None, "101"], "while a message is being handled the offset still points at it"
    assert [c for c in ch.core.calls if c[0] == "tell"] == [("tell", LOST)], "the owner is told, once, about «яд»"
    assert ch.store.get("telegram.offset") == "103"

def test_everything_leaves_without_preview_or_links(tmp_path):
    ch = make(tmp_path)

    async def run():
        await ch.send("assistant", "## Итог\n\nСмотри [тут](https://example.com/a?b=1).\n\n" + "длинный абзац. " * 600, [], "ev-1")
        await ch.notice("assistant", "Пока не умею принимать фото. Напиши текстом.", None, "ev-2")
        await ch.typing("assistant", True)
        await ch.typing("assistant", False)
        await ch.draft("assistant", "черновик https://example.com")

    asyncio.run(run())
    assert {method for method, _ in ch.sent} == {"sendMessage", "sendChatAction"}, "no drafts, no other content method"
    out = texts(ch)
    assert len(out) >= 3 and all(p["link_preview_options"] == {"is_disabled": True} for p in out)
    assert all(p["parse_mode"] == "HTML" and p["chat_id"] == OWNER for p in out)
    assert not any("message_thread_id" in p or "url" in p or "href" in p.get("text", "") for _, p in ch.sent)
    assert "тут (<code>https://example.com/a?b=1</code>)" in out[0]["text"]
    assert ch.store.sent_event("telegram", "900") == "ev-1" and ch.store.sent_event("telegram", "901") == "ev-1"
    assert ch.store.sent_event("telegram", str(899 + len(out))) == "ev-2", "a reply to any of them can be traced back"

def test_markup_fallback_keeps_preview_off_and_addresses_wrapped(tmp_path):
    ch = make(tmp_path)
    ch.refuse_markup = True
    asyncio.run(ch.send("assistant", "**Итог:** https://example.com/x?secret=1", [], "ev-1"))
    refused, fallback = texts(ch)
    assert "<b>" in refused["text"] and "<b>" not in fallback["text"]
    assert fallback["link_preview_options"] == {"is_disabled": True}, "the fallback path has no preview either"
    assert fallback["parse_mode"] == "HTML" and fallback["text"] == "Итог: <code>https://example.com/x?secret=1</code>"
    assert ch.store.sent_event("telegram", "900") == "ev-1"

def test_render_telegram_splits_and_escapes():
    html = render_telegram("## Итог\n\n| a | b |\n|---|---|\n| **x** | <y> |\n\n" + "длинный абзац. " * 600)
    assert html[0].startswith("<b>Итог</b>\n\n• <b>x</b> — &lt;y&gt;")
    assert len(html) > 1 and all(len(m) <= 4000 for m in html)
