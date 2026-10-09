"""Telegram adapter without the network: one stream, owner only, and what leaves towards the owner."""

import asyncio

import httpx
import pytest

from retinue.attachments import Upload
from retinue.channels.telegram import LOST, POISON, START, TelegramChannel, TelegramError
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

    async def receive(self, channel, text, uploads, **kwargs):
        self.calls.append(("receive", text, uploads, kwargs))

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
        await ch.on_message(msg("/compact", message_id=8))
        await ch.on_message(msg("/mail", message_id=9))
        await ch.on_message(msg("@travel из старой темы", message_id=7, message_thread_id=11, is_topic_message=True))
        await ch.on_message(msg("чужой", sender=7))
        await ch.on_message(msg("/start"))
        await ch.on_message(msg("/help"))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "билеты в Черногорию", {"native_id": "5", **PLAIN}),
        ("handle", None, "!new", {"native_id": "6", **PLAIN}),
        ("handle", None, "!compact", {"native_id": "8", **PLAIN}),
        ("handle", None, "!mail", {"native_id": "9", **PLAIN}),
        ("handle", None, "@travel из старой темы", {"native_id": "7", **PLAIN}),  # no topics, no addresses
        ("tell", START),
        ("tell", START),  # Telegram's help names Telegram's commands, «/new», not the core's «!new»
    ]
    assert ch.sent == [], "the adapter itself says nothing: the core does"
    assert "/compact" in START and "/mail" in START

def test_reply_forward_and_unsupported_types(tmp_path):
    ch = make(tmp_path)
    recruiter = {"type": "user", "sender_user": {"id": 7, "first_name": "Иван", "last_name": "Рекрутёр"}}

    async def run():
        await ch.on_message(msg("а подробнее?", message_id=10, reply_to_message={"message_id": 900, "from": {"id": 1, "is_bot": True}}))
        await ch.on_message(msg("/new забудь всё", message_id=11, forward_origin=recruiter))
        await ch.on_message(msg("из канала", message_id=12, forward_origin={"type": "channel", "chat": {"title": "Вакансии"}}))
        await ch.on_message(msg("скрытый", message_id=13, forward_origin={"type": "hidden_user", "sender_user_name": "Аноним"}))
        await ch.on_message(msg("/new", message_id=14, forward_origin={"type": "user", "sender_user": {"id": OWNER}}))
        await ch.on_message(msg(None, message_id=15, animation={"file_id": "a"}, document={"file_id": "a"}, caption="смотри"))
        await ch.on_message(msg(None, message_id=16, poll={"id": "p"}))
        await ch.on_message(msg(None, message_id=17, new_chat_title="что-то служебное"))
        await ch.on_message(msg(None, message_id=18, sender=7, photo=[{"file_id": "x"}]))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "а подробнее?", {"native_id": "10", "reply_to": "900", "forwarded_from": None}),
        ("handle", None, "/new забудь всё", {"native_id": "11", "reply_to": None, "forwarded_from": "Иван Рекрутёр"}),
        ("handle", None, "из канала", {"native_id": "12", "reply_to": None, "forwarded_from": "Вакансии"}),
        ("handle", None, "скрытый", {"native_id": "13", "reply_to": None, "forwarded_from": "Аноним"}),
        ("handle", None, "!new", {"native_id": "14", **PLAIN}),  # the owner's own words, forwarded by himself
        ("unsupported", "гифки", {"native_id": "15", "caption": "смотри"}),  # a GIF is a document too: not read
        ("unsupported", "опросы", {"native_id": "16", "caption": ""}),
        ("unsupported", "такие сообщения", {"native_id": "17", "caption": ""}),
    ], "a stranger's photo is ignored; the owner's is refused aloud, never dropped in silence"

def fetching(tmp_path, missing=()):
    """The adapter with a Bot API that serves files: getFile names a path, the file URL returns bytes."""
    ch = make(tmp_path)
    plain, ch.fetched = ch.call, []

    async def call(method, files=None, **params):
        if method == "getFile":
            if params["file_id"] in missing:
                raise TelegramError("getFile: 400 Bad Request: wrong file_id")
            return {"file_path": f"files/{params['file_id']}"}
        return await plain(method, files, **params)

    def serve(request):
        ch.fetched.append(request.url.path)
        return httpx.Response(200, content=f"bytes of {request.url.path.rsplit('/', 1)[1]}".encode())

    ch.call, ch.http = call, httpx.AsyncClient(transport=httpx.MockTransport(serve))
    return ch


def test_files_are_fetched_and_handed_to_the_core(tmp_path):
    ch = fetching(tmp_path, missing={"lost"})
    ivan = {"type": "user", "sender_user": {"id": 7, "first_name": "Иван"}}

    async def run():
        await ch.on_message(msg(None, message_id=30, caption="чек", media_group_id="g1",
                                photo=[{"file_id": "small", "file_size": 900}, {"file_id": "big", "file_size": 90000}]))
        await ch.on_message(msg(None, message_id=31, forward_origin=ivan,
                                voice={"file_id": "v1", "duration": 42, "mime_type": "audio/ogg", "file_size": 1000}))
        await ch.on_message(msg(None, message_id=32, document={"file_id": "d1", "file_name": "film.mp4",
                                                               "mime_type": "video/mp4", "file_size": 35 * 2**20}))
        await ch.on_message(msg(None, message_id=33, document={"file_id": "lost", "file_name": "x.pdf"}))

    asyncio.run(run())
    (photo, voice, film, lost) = ch.core.calls
    assert ch.fetched == [], "nothing is downloaded before the core has the message: an album part is not held up"
    assert photo == ("receive", "чек", [Upload("photo", size=90000)],
                     {"native_id": "30", "reply_to": None, "forwarded_from": None, "group": "g1"}), "the largest size"
    assert voice == ("receive", "", [Upload("voice", b"", "", "audio/ogg", 42, 1000)],
                     {"native_id": "31", "reply_to": None, "forwarded_from": "Иван", "group": None})
    assert film[2] == [Upload("document", b"", "film.mp4", "video/mp4", 0, 35 * 2**20,
                              "файл «film.mp4», 35 МБ: Telegram отдаёт ботам файлы до 20 МБ")]
    assert film[2][0].fetch is None, "a file over 20 MB is not even asked for"

    async def fetch_all():
        got = [await photo[2][0].fetch(), await voice[2][0].fetch()]
        with pytest.raises(TelegramError, match="getFile: 400 Bad Request: wrong file_id"):
            await lost[2][0].fetch()
        return got

    assert asyncio.run(fetch_all()) == [b"bytes of big", b"bytes of v1"], "the core downloads when it reads"
    assert ch.fetched == ["/file/bott/files/big", "/file/bott/files/v1"]


def test_a_file_that_took_the_router_down_is_not_tried_a_third_time(tmp_path):
    ch = make(tmp_path)
    attempts = []

    async def receive(channel, text, uploads, **kwargs):
        attempts.append(ch.store.get("telegram.attempt"))

    ch.core.receive = receive
    photo = {"photo": [{"file_id": "p", "file_size": 10}]}

    async def run():
        await ch.consume({"update_id": 200, "message": msg(None, message_id=1, **photo)})
        ch.store.set("telegram.offset", "201")
        ch.store.set("telegram.attempt", "201")  # the router died while handling update 201; Telegram sends it again
        await ch.consume({"update_id": 201, "message": msg(None, message_id=2, **photo)})

    asyncio.run(run())
    assert attempts == ["200"], "marked before the file is touched; the replay of 201 is not handled again"
    assert [c for c in ch.core.calls if c[0] == "tell"] == [("tell", POISON)]
    assert ch.store.get("telegram.offset") == "202"


def test_stickers_places_and_contacts_are_text(tmp_path):
    ch = make(tmp_path)
    place = {"latitude": 44.8125, "longitude": 20.4612}

    async def run():
        await ch.on_message(msg(None, message_id=40, sticker={"emoji": "👍", "file_id": "s"}))
        await ch.on_message(msg(None, message_id=41, location=place))
        await ch.on_message(msg(None, message_id=42, location=place,
                                venue={"location": place, "title": "Кафе", "address": "Кнеза Михаила 1"}))
        await ch.on_message(msg(None, message_id=43, contact={"first_name": "Иван", "last_name": "Петров",
                                                              "phone_number": "+381601234567"}))

    asyncio.run(run())
    assert [(c[2], c[3]["native_id"]) for c in ch.core.calls] == [
        ("[стикер 👍]", "40"), ("[геопозиция: 44.8125, 20.4612]", "41"),
        ("[место: Кафе, Кнеза Михаила 1 (44.8125, 20.4612)]", "42"), ("[контакт: Иван Петров, +381601234567]", "43")]
    assert all(c[0] == "handle" for c in ch.core.calls)


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


def test_a_link_to_a_travel_site_is_clickable_and_still_has_no_preview(tmp_path):
    ch = make(tmp_path)
    ch.link_hosts = ("kiwi.com",)
    asyncio.run(ch.send("assistant", "[kiwi, 99 €](https://kiwi.com/u/abc) или https://evil.example/u", [], "ev-1"))
    (out,) = texts(ch)
    assert out["text"] == '<a href="https://kiwi.com/u/abc">kiwi, 99 €</a> или <code>https://evil.example/u</code>'
    assert out["link_preview_options"] == {"is_disabled": True}, "a link, and still no preview"


def test_many_buttons_go_in_rows_and_a_press_can_leave_the_others(tmp_path):
    """Telegram takes at most 8 buttons in a row; ten «Откатить» go in rows of four. A press that spends only its
    own button (an evening list) rewrites the card with the buttons still alive."""
    ch = make(tmp_path)
    buttons = [(f"Откатить {i}", f"b{i}") for i in range(1, 11)]

    async def press(channel, button_id):
        return Pressed(True, "Откатываю…", "Список\n\n_Нажато: Откатить 2_", keep=[b for b in buttons if b[1] != "b2"])

    ch.core.press = press

    async def run():
        await ch.notice("assistant", "Список", buttons, "ev-1")
        await ch.on_update(press_of("b2"))

    def press_of(data):
        return {"callback_query": {"id": "q2", "from": {"id": OWNER}, "data": data,
                                   "message": {"message_id": 900, "chat": {"id": OWNER}}}}

    asyncio.run(run())
    rows = ch.sent[0][1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [4, 4, 2]
    assert [(b["text"], b["callback_data"]) for row in rows for b in row] == buttons
    edit = ch.sent[2][1]
    kept = [(b["text"], b["callback_data"]) for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert kept == [b for b in buttons if b[1] != "b2"] and edit["text"].endswith("<i>Нажато: Откатить 2</i>")
