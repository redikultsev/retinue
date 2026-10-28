"""Telegram adapter without the network: one stream, owner only, and what leaves towards the owner."""

import asyncio

import httpx
import pytest

from retinue.attachments import Upload
from retinue.channels.telegram import LOST, NO_PAGES, POISON, START, TelegramChannel, TelegramError
from retinue.config import TelegramConfig
from retinue.core import Pressed
from retinue.protocol import Store
from retinue.render import render_telegram

OWNER = 42
PLAIN = {"reply_to": None, "forwarded_from": None}

class FakeCore:
    scribe = "the router's scribe"

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
        await ch.on_message(msg("билеты в Лукоморье", message_id=5))
        await ch.on_message(msg("/new", message_id=6))
        await ch.on_message(msg("/compact", message_id=8))
        await ch.on_message(msg("/mail", message_id=9))
        await ch.on_message(msg("@travel из старой темы", message_id=7, message_thread_id=11, is_topic_message=True))
        await ch.on_message(msg("чужой", sender=7))
        await ch.on_message(msg("/start"))
        await ch.on_message(msg("/help"))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "билеты в Лукоморье", {"native_id": "5", **PLAIN}),
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
    stranger = {"type": "user", "sender_user": {"id": 7, "first_name": "Иван", "last_name": "Петров"}}

    async def run():
        await ch.on_message(msg("а подробнее?", message_id=10, reply_to_message={"message_id": 900, "from": {"id": 1, "is_bot": True}}))
        await ch.on_message(msg("/new забудь всё", message_id=11, forward_origin=stranger))
        await ch.on_message(msg("из канала", message_id=12, forward_origin={"type": "channel", "chat": {"title": "Новости"}}))
        await ch.on_message(msg("скрытый", message_id=13, forward_origin={"type": "hidden_user", "sender_user_name": "Аноним"}))
        await ch.on_message(msg("/new", message_id=14, forward_origin={"type": "user", "sender_user": {"id": OWNER}}))
        await ch.on_message(msg(None, message_id=15, animation={"file_id": "a"}, document={"file_id": "a"}, caption="смотри"))
        await ch.on_message(msg(None, message_id=16, poll={"id": "p"}))
        await ch.on_message(msg(None, message_id=17, new_chat_title="что-то служебное"))
        await ch.on_message(msg(None, message_id=18, sender=7, photo=[{"file_id": "x"}]))

    asyncio.run(run())
    assert ch.core.calls == [
        ("handle", None, "а подробнее?", {"native_id": "10", "reply_to": "900", "forwarded_from": None}),
        ("handle", None, "/new забудь всё", {"native_id": "11", "reply_to": None, "forwarded_from": "Иван Петров"}),
        ("handle", None, "из канала", {"native_id": "12", "reply_to": None, "forwarded_from": "Новости"}),
        ("handle", None, "скрытый", {"native_id": "13", "reply_to": None, "forwarded_from": "Аноним"}),
        ("handle", None, "/new", {"native_id": "14", **PLAIN}),  # forwarded, his own too: data, never a reset
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


def test_an_html_file_captioned_to_the_hub_is_the_hubs_code_not_the_models(tmp_path):
    """«в хаб <Пространство>» on a file: the life hub's code puts it in the base (`Lifehub.upload`). Someone else's
    forwarded file with that caption is data like any other; without the hub the owner hears so."""
    ch = make(tmp_path)
    uploads = []

    class Hub:
        async def upload(self, channel, space, upload, native_id):
            uploads.append((channel is ch, space, upload, native_id))

    ch.core.lifehub = Hub()
    page = {"file_id": "h", "file_name": "course.html", "mime_type": "text/html", "file_size": 4000}
    ivan = {"type": "user", "sender_user": {"id": 7, "first_name": "Иван"}}

    async def run():
        await ch.on_message(msg(None, message_id=40, document=page, caption=" В хаб Work "))
        await ch.on_message(msg(None, message_id=41, document=page, caption="в хаб work", forward_origin=ivan))
        await ch.on_message(msg(None, message_id=42, document=page, caption="в хаб"))
        await ch.on_message(msg(None, message_id=43, document=page, caption="положи в хаб work, пожалуйста"))
        await ch.on_message(msg(None, message_id=45, document=page, caption="в хаб work", forward_date=1,
                                forward_sender_name="Скрыт"))
        await ch.on_message(msg(None, message_id=46, document=page, caption="в хаб work",
                                forward_origin={"type": "user", "sender_user": {"id": OWNER}}))
        ch.core.lifehub = None
        await ch.on_message(msg(None, message_id=44, document=page, caption="в хаб work"))

    asyncio.run(run())
    assert uploads == [(True, "work", Upload("document", b"", "course.html", "text/html", 0, 4000), "40")]
    assert [call[0] for call in ch.core.calls] == ["receive"] * 5 + ["tell"], \
        "a forward (a hidden sender's, his own too), a bare «в хаб» and a sentence go to her as files"
    assert ch.core.calls[-1] == ("tell", NO_PAGES)


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
    place = {"latitude": 55.0001, "longitude": 37.0001}

    async def run():
        await ch.on_message(msg(None, message_id=40, sticker={"emoji": "👍", "file_id": "s"}))
        await ch.on_message(msg(None, message_id=41, location=place))
        await ch.on_message(msg(None, message_id=42, location=place,
                                venue={"location": place, "title": "Кафе", "address": "Примерная 1"}))
        await ch.on_message(msg(None, message_id=43, contact={"first_name": "Иван", "last_name": "Петров",
                                                              "phone_number": "+442079460000"}))

    asyncio.run(run())
    assert [(c[2], c[3]["native_id"]) for c in ch.core.calls] == [
        ("[стикер 👍]", "40"), ("[геопозиция: 55.0001, 37.0001]", "41"),
        ("[место: Кафе, Примерная 1 (55.0001, 37.0001)]", "42"), ("[контакт: Иван Петров, +442079460000]", "43")]
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


def test_a_courier_card_shows_the_text_verbatim_and_one_button_per_row(tmp_path):
    """The text that will be sent is one <pre>: no Markdown, no link, no split. Addresses in the header are <code>;
    the only link is a t.me one code made. Each button on its own row. A press rewrites it as a card."""
    from retinue.courier import CardView

    ch = make(tmp_path)
    text = "*не жирный* <b>x</b> [ссылка](https://evil.example) https://evil.example/?q=1"
    view = CardView(["Черновик ab12cd34 · Gmail · owner@example.org", "Кому: <hr@acme.example>"], text)
    later = CardView(view.head, text, "Отправлю через 10 с — можно отменить.")
    linked = CardView(view.head, text, "Окно закрыто.", ("Открыть чат", "https://t.me/anna_x?text=%D0%94"))
    evil = CardView(view.head, text, "", ("Открыть", "https://evil.example/x"))

    async def press(channel, button_id):
        return Pressed(True, "Отправлю через 10 с", view=later, keep=[("Отменить", "c1")])

    ch.core.press = press

    async def run():
        await ch.card("assistant", view, [("Отправить", "s1"), ("Поправить", "f1"), ("Не отвечать", "d1")], "ev-1")
        await ch.on_update({"callback_query": {"id": "q", "from": {"id": OWNER}, "data": "s1",
                                               "message": {"message_id": 900, "chat": {"id": OWNER}}}})
        await ch.card("assistant", linked, [], "ev-1", edit=True)
        await ch.card("assistant", evil, [], "ev-2")

    asyncio.run(run())
    shown, toast, edited, rewritten, other = ch.sent
    html = shown[1]["text"]
    assert html == ("Черновик ab12cd34 · Gmail · <code>owner@example.org</code>\nКому: &lt;<code>hr@acme.example</code>&gt;"
                    "\n\n<pre>*не жирный* &lt;b&gt;x&lt;/b&gt; [ссылка](https://evil.example) "
                    "https://evil.example/?q=1</pre>")
    assert shown[1]["link_preview_options"] == {"is_disabled": True} and shown[1]["parse_mode"] == "HTML"
    assert shown[1]["reply_markup"] == {"inline_keyboard": [[{"text": "Отправить", "callback_data": "s1"}],
                                                            [{"text": "Поправить", "callback_data": "f1"}],
                                                            [{"text": "Не отвечать", "callback_data": "d1"}]]}
    assert edited[0] == "editMessageText" and edited[1]["message_id"] == 900
    assert edited[1]["text"].endswith("</pre>\n\n<i>Отправлю через 10 с — можно отменить.</i>")
    assert edited[1]["reply_markup"] == {"inline_keyboard": [[{"text": "Отменить", "callback_data": "c1"}]]}
    assert rewritten[0] == "editMessageText" and rewritten[1]["message_id"] == 900, "found by the card's archive id"
    assert rewritten[1]["text"].endswith('<a href="https://t.me/anna_x?text=%D0%94">Открыть чат</a>')
    assert "reply_markup" not in rewritten[1], "a rewrite without buttons takes them away"
    assert "<a " not in other[1]["text"], "no link but t.me, whatever a view carries"


class FakeBridge:
    """The platform's side as the channel sees it: which messages it claims, and what reached it."""

    def __init__(self, store):
        self.store, self.calls = store, []
        self.replied: list[str | None] = []  # the message each input or spoken reply answered: a question's card
        self.face_url = "https://app.example.test"

    def attach(self, channel):
        self.channel = channel

    def session_of(self, reply_to):
        return self.store.sent_event("platform", reply_to) if reply_to else None

    async def new(self, track, text, native_id):
        self.calls.append(("new", track, text, native_id))

    async def input(self, session, text, native_id, reply_to=None):
        self.calls.append(("input", session, text, native_id))
        self.replied.append(reply_to)

    async def spoken(self, session, upload, native_id, scribe, caption="", reply_to=None):
        self.calls.append(("spoken", session, upload, native_id, scribe, caption))
        self.replied.append(reply_to)

    async def files(self, session, message):
        self.calls.append(("files", session, message["message_id"], message.get("caption", "")))

    async def say(self, text, session=""):
        self.calls.append(("say", text, session))

    async def press(self, query_id, button, native_id):
        self.calls.append(("press", button, native_id))

    async def login(self):
        self.calls.append(("login",))

    def spheres(self):
        return {"travel": {"name": "travel", "title": "Путешествия", "icon": "✈️"}}

    def menu(self):
        return [{"command": "travel", "description": "✈️ Путешествия"}]

    async def sphere(self, name, text, native_id):
        self.calls.append(("sphere", name, text, native_id))


def bridged(tmp_path):
    ch = make(tmp_path)
    ch.bridge = FakeBridge(ch.store)
    ch.store.save_sent("platform", "900", "ab12cd34")
    ch.store.save_sent("telegram", "901", "ev-1")
    return ch


def test_the_platform_gets_replies_to_its_sessions_and_new_with_a_track(tmp_path):
    ch = bridged(tmp_path)
    to_session, to_her = {"reply_to_message": {"message_id": 900}}, {"reply_to_message": {"message_id": 901}}
    me = {"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}}

    async def run():
        await ch.on_message(msg("и тесты тоже", message_id=10, **to_session))
        await ch.on_message(msg("/new retinue fix CI\nи прогони тесты", message_id=11))
        await ch.on_message(msg("/new@retinue_bot notes", message_id=12))
        await ch.on_message(msg("/new", message_id=13))
        await ch.on_message(msg("/new x", message_id=14, forward_origin={"type": "user", "sender_user": {"id": 7}}))
        await ch.on_message(msg("переслал сам", message_id=15, **to_session, **me))
        await ch.on_message(msg("а ей", message_id=16, **to_her))
        await ch.on_message(msg(None, message_id=17, photo=[{"file_id": "x"}], caption="вот", **to_session))
        await ch.on_message(msg("чужой", sender=7, message_id=18, **to_session))
        await ch.on_message(msg(None, message_id=19, video={"file_id": "v"}, **to_session))

    asyncio.run(run())
    assert ch.bridge.calls == [
        ("input", "ab12cd34", "и тесты тоже", "10"),
        ("new", "retinue", "fix CI\nи прогони тесты", "11"),
        ("new", "notes", "", "12"),
        ("files", "ab12cd34", 17, "вот"),
        ("say", "В сессии принимаю текст, голосовые, фото и файлы.", "ab12cd34"),  # a video
    ]
    assert ch.bridge.replied == ["900"], "the message replied to goes along: a question's card takes it as an answer"
    assert ch.core.calls == [
        ("handle", None, "!new", {"native_id": "13", **PLAIN}),  # bare /new still resets her
        ("handle", None, "/new x", {"native_id": "14", "reply_to": None, "forwarded_from": "неизвестный отправитель"}),
        # Any forward is data for her, his own one too: a session gets only what he typed or said there.
        ("handle", None, "переслал сам", {"native_id": "15", "reply_to": "900", "forwarded_from": None}),
        ("handle", None, "а ей", {"native_id": "16", "reply_to": "901", "forwarded_from": None}),
    ], "the platform's messages never reach the core, and hers never reach the platform"


def test_a_spoken_reply_to_a_session_is_the_platform_s_and_other_voice_stays_the_assistant_s(tmp_path):
    ch = bridged(tmp_path)
    to_session, to_her = {"reply_to_message": {"message_id": 900}}, {"reply_to_message": {"message_id": 901}}
    voice = {"file_id": "v1", "duration": 4, "mime_type": "audio/ogg", "file_size": 1000}
    ivan = {"forward_origin": {"type": "user", "sender_user": {"id": 7, "first_name": "Иван"}}}
    mine = {"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}}
    hidden = {"forward_origin": {"type": "hidden_user", "sender_user_name": "Аноним"}}

    async def run():
        await ch.on_message(msg(None, message_id=10, voice=voice, **to_session))
        await ch.on_message(msg(None, message_id=11, video_note={"file_id": "n1", "duration": 9}, **to_session))
        await ch.on_message(msg(None, message_id=12, caption="к шагу 2", audio={"file_id": "a1", "file_name": "x.mp3",
                                                                              "mime_type": "audio/mpeg"}, **to_session))
        await ch.on_message(msg(None, message_id=13, document={"file_id": "d1", "file_name": "a.pdf"}, **to_session))
        await ch.on_message(msg(None, message_id=14, voice=voice))
        await ch.on_message(msg(None, message_id=15, voice=voice, **to_her))
        await ch.on_message(msg(None, message_id=16, voice=voice, **to_session, **ivan))
        await ch.on_message(msg(None, message_id=17, sender=7, voice=voice, **to_session))
        await ch.on_message(msg(None, message_id=18, voice=voice, **to_session, **mine))
        await ch.on_message(msg(None, message_id=19, voice=voice, **to_session, **hidden))

    asyncio.run(run())
    scribe = FakeCore.scribe
    assert ch.bridge.calls == [
        ("spoken", "ab12cd34", Upload("voice", b"", "", "audio/ogg", 4, 1000), "10", scribe, ""),
        ("spoken", "ab12cd34", Upload("video_note", duration=9), "11", scribe, ""),
        ("spoken", "ab12cd34", Upload("audio", b"", "x.mp3", "audio/mpeg"), "12", scribe, "к шагу 2"),
        ("files", "ab12cd34", 13, ""),
    ]
    assert [(c[0], c[3]["native_id"], c[3]["reply_to"], c[3]["forwarded_from"]) for c in ch.core.calls] == [
        ("receive", "14", None, None),
        ("receive", "15", "901", None),
        ("receive", "16", "900", "Иван"),  # somebody else's voice is data for her, not the owner's words
        ("receive", "18", "900", None),  # his own forward too: not said to the session here
        ("receive", "19", "900", "Аноним"),
    ], "voice outside a session's thread reaches the assistant as before; a stranger's is ignored"


def test_an_own_forwarded_command_is_data_for_the_assistant_and_never_the_platform_s(tmp_path):
    ch = bridged(tmp_path)
    mine = {"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}}

    async def run():
        await ch.on_message(msg("/new code x", message_id=10, **mine))
        await ch.on_message(msg("/app", message_id=11, **mine))
        await ch.on_message(msg("/help", message_id=12, **mine))

    asyncio.run(run())
    assert ch.bridge.calls == [], "no session started, no login"
    assert ch.core.calls == [
        ("handle", None, "/new code x", {"native_id": "10", **PLAIN}),  # not «!new»: her conversation stays
        ("handle", None, "/app", {"native_id": "11", **PLAIN}),
        ("handle", None, "/help", {"native_id": "12", **PLAIN}),
    ]


def test_app_asks_the_platform_for_a_login_and_only_from_the_owner(tmp_path):
    ch = bridged(tmp_path)

    async def run():
        await ch.on_message(msg("/app", message_id=10))
        await ch.on_message(msg("/app@retinue_bot", message_id=11))
        await ch.on_message(msg("/app", message_id=12, sender=7))
        await ch.on_message(msg("/app", message_id=13, forward_origin={"type": "user", "sender_user": {"id": 7}}))

    asyncio.run(run())
    assert ch.bridge.calls == [("login",), ("login",)]
    assert [c[2] for c in ch.core.calls] == ["/app"], "somebody else's /app is data for her"


def test_without_the_faces_address_app_is_the_assistants_as_before(tmp_path):
    ch = bridged(tmp_path)
    ch.bridge.face_url = ""
    asyncio.run(ch.on_message(msg("/app", message_id=10)))
    assert ch.bridge.calls == [] and [c[2] for c in ch.core.calls] == ["/app"]


def test_platform_buttons_go_to_the_platform_and_the_others_to_the_core(tmp_path):
    from retinue.sessions import PlatformStore

    ch = bridged(tmp_path)
    minted = PlatformStore(str(tmp_path / "p.sqlite")).add_button("a1", "approve", "d", expires=10.0)

    async def run():
        await ch.on_update(press(minted))
        await ch.on_update(press("btn-live"))
        await ch.on_update(press(minted, sender=7))

    asyncio.run(run())
    assert ch.bridge.calls == [("press", minted, 900)], "a button the platform minted is the platform's"
    assert [c for c in ch.core.calls if c[0] == "press"] == [("press", "btn-live")]


def test_a_platform_that_does_not_answer_is_said_and_the_assistant_is_not_asked(tmp_path):
    from retinue.bridge import UNAVAILABLE, PlatformBridge, PlatformClient

    ch = make(tmp_path)
    ch.bridge = PlatformBridge(PlatformClient("http://127.0.0.1:9", "t", "платформа"), ch.store)
    ch.bridge.attach(ch)
    ch.store.save_sent("platform", "900", "ab12cd34")

    async def run():
        await ch.on_message(msg("и тесты", message_id=10, reply_to_message={"message_id": 900}))
        await ch.on_message(msg("/new retinue fix CI", message_id=11))
        await ch.on_update(press("p.AbCdEfGhIjKlMnOp"))

    asyncio.run(run())
    assert ch.core.calls == []
    assert [p["text"] for _, p in ch.sent] == [UNAVAILABLE] * 3
    assert ch.sent[-1][0] == "answerCallbackQuery"


def test_a_spheres_command_goes_to_its_main_session_and_the_menu_names_the_spheres(tmp_path):
    ch = bridged(tmp_path)

    async def run():
        await ch.on_message(msg("/travel найди жильё\nв Зурбагане", message_id=20))
        await ch.on_message(msg("/travel@retinue_bot", message_id=21))
        await ch.on_message(msg("/new travel план", message_id=22))
        await ch.on_message(msg("/work x", message_id=23))  # not a sphere: hers
        await ch.on_message(msg("/travel x", message_id=24, forward_origin={"type": "user", "sender_user": {"id": 7}}))
        await ch.set_menu()

    asyncio.run(run())
    assert ch.bridge.calls == [("sphere", "travel", "найди жильё\nв Зурбагане", "20"), ("sphere", "travel", "", "21"),
                               ("new", "travel", "план", "22")]
    assert [c[2] for c in ch.core.calls] == ["/work x", "/travel x"]
    [(method, params)] = [c for c in ch.sent if c[0] == "setMyCommands"]
    assert params["commands"][0]["command"] == "new" and params["commands"][-1] == {"command": "travel",
                                                                                    "description": "✈️ Путешествия"}
