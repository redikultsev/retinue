"""Replies to people on the router's side, with a fake mail sender and a fake Telegram gateway: the envelope comes
from the archive, the card says what code found, and only the owner's «Отправить» under that card sends — once."""

import asyncio

from retinue import courier
from retinue.archive import Archive
from retinue.config import RouterAgent
from retinue.core import Core
from retinue.mail import MailStore, Unavailable
from retinue.mailroom import Mailroom
from retinue.outbox import Outbox, OutboxStore
from retinue.protocol import Store

from test_core import FakeChannel, drain

NOW = 1_760_000_000.0
ME = "owner@example.org"
ASSISTANT = RouterAgent(id="assistant", name="Ассистентка", url="http://a", trust_class="private", archive=True,
                        drafts=True)


class CardChannel(FakeChannel):
    """A channel that shows cards: every card and every rewrite of one, as code built it."""

    def __init__(self):
        super().__init__("telegram", False)
        self.shown = []  # (view, buttons, ref, edit)

    async def card(self, agent_id, view, buttons, ref, edit=False):
        self.shown.append((view, list(buttons), ref, edit))


class FakeNeighbour:
    """The mail sender or the gateway from memory: its status and what it was asked to send."""

    def __init__(self, status=None, answer=None):
        self.status_ = status if status is not None else {"accounts": [ME], "dead": {}}
        self.answer, self.asked = answer or {"status": "sent", "id": "g1"}, []

    async def status(self):
        if isinstance(self.status_, Exception):
            raise self.status_
        return self.status_

    async def send(self, key, envelope, digest):
        self.asked.append((key, envelope, digest))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def letter(archive, ref="m1", sender="anna@acme.example", ts=NOW - 600, **meta):
    event, _ = archive.append("mail", f"Письмо · {ME} · входящее\nОт: Анна <{sender}>\nТема: Интервью\n\nУдобно в чт?",
                              conversation_id=f"mail:{ME}", channel="mail", native_id=f"{ME}/{ref}", ts=ts,
                              meta={"account": ME, "direction": "in", "gmail": ref, "thread": "18c2f0a1b2c3d4e5",
                                    "sender": sender, "subject": "Интервью", "message_id": f"<{ref}@acme.example>",
                                    "in_reply_to": "", "to": [ME], "cc": [], "reply_to": "",
                                    "references": [], **meta})
    return event


def chat(archive, mid=10, ts=NOW - 600, username="anna_x", direction="in", chat_id="777"):
    event, _ = archive.append("chat", "Telegram · Анна · входящее\nСозвон в 7?", conversation_id=f"chat:{chat_id}",
                              channel="tgb", native_id=f"{chat_id}/{mid}", ts=ts,
                              meta={"chat": chat_id, "message_id": mid, "direction": direction, "by": direction,
                                    "name": "Анна", "username": username, "edit": False, "reply_to": 0})
    return event


def courier_core(tmp_path, sender=None, gateway=None, mail=False):
    store = Store(str(tmp_path / "r.sqlite"))
    outbox = Outbox(OutboxStore(store.db), sender=sender if sender is not None else FakeNeighbour(),
                    gateway=gateway if gateway is not None else FakeNeighbour({"connected": True, "rights": ["can_reply"]}),
                    owner_tg=42)
    room = Mailroom(None, MailStore(store.db)) if mail else None
    core = Core([ASSISTANT], store, "owner", archive=Archive(str(tmp_path / "a.sqlite")), outbox=outbox, mail=room)
    outbox.clock = lambda: NOW
    return core, outbox


async def propose(core, args, foreign=()):
    """Through the bus's door: her own turn, the grant, the mark of whose words started the run."""
    turn = core.turns.open_root("assistant")
    turn.tree.foreign.update(foreign)
    try:
        return await core.draft_reply(core.agents["assistant"], turn.id, args)
    finally:
        core.turns.close(turn)


def test_a_reply_takes_its_address_subject_and_thread_from_the_archive_never_from_her(tmp_path):
    async def run():
        core, outbox = courier_core(tmp_path)
        channel = CardChannel()
        await core.start([channel])
        event = letter(core.archive, references=["<m0@acme.example>"])
        done = await propose(core, {"reply_to": event.id, "text": "Здравствуйте! Да, четверг в 15:00 подходит.\n"
                                                                   "Резюме: https://cv.example/me?ref=acme"}, {"mail"})
        return core, outbox, channel, done

    core, outbox, channel, (ok, said) = asyncio.run(run())
    (draft,) = outbox.store.in_state("shown")
    assert ok and said.startswith(f"Черновик {draft['id']} показан Владельцу карточкой с текстом целиком. Уйдёт, только "
                                  "если он нажмёт «Отправить»") and "Не пиши, что отправлено." in said
    envelope = courier.Envelope.of(draft["envelope"])
    assert (envelope.account, envelope.to, envelope.subject, envelope.thread) == (
        ME, "anna@acme.example", "Re: Интервью", "18c2f0a1b2c3d4e5")
    assert envelope.in_reply_to == "<m1@acme.example>" and envelope.references == ("<m0@acme.example>", "<m1@acme.example>")
    assert draft["digest"] == courier.digest(envelope) and draft["thread"] == "mail:18c2f0a1b2c3d4e5"
    (view, buttons, ref, edit), = channel.shown
    assert view.head == [f"Черновик {draft['id']} · Gmail · {ME}", "Кому: <anna@acme.example>", "Тема: Re: Интервью",
                         "Ответ на письмо от чт 9 октября, 11:43 МСК",
                         "Внимание: новый получатель; по просьбе собеседника",
                         "В тексте найдено: ссылка https://cv.example/me?ref=acme — с параметрами", "Текст, 82 знаков:"]
    assert view.body == envelope.text and not edit and ref == draft["card"]
    assert [label for label, _ in buttons] == ["Отправить: anna@acme.example", "Поправить", "Не отвечать"], \
        "a flag fired: the button names whom it goes to"
    value = core.store.db.execute("SELECT value FROM buttons WHERE id = ?", (buttons[0][1],)).fetchone()[0]
    assert value == f"{draft['id']}:{draft['digest']}", "the button is bound to this envelope"
    record = core.archive.get(draft["card"])
    assert record.kind == "system" and envelope.text in record.text and record.meta == {"draft": draft["id"]}
    assert core.store.db.execute("SELECT target, status FROM protocol").fetchall() == [("courier/draft", "done")]


def test_a_draft_is_refused_with_what_to_fix(tmp_path):
    async def run():
        core, outbox = courier_core(tmp_path)
        await core.start([CardChannel()])
        event = letter(core.archive)
        mine = letter(core.archive, ref="m2", direction="out")
        plain = RouterAgent(id="assistant", name="Ассистентка", url="http://a", trust_class="private")
        turn = core.turns.open_root("assistant")
        said = [await core.draft_reply(plain, turn.id, {"reply_to": event.id, "text": "Да."})]
        for args in ({"reply_to": event.id, "to": "anna@acme.example", "text": "Да."}, {"text": "Да."},
                     {"reply_to": "mail:nothing", "text": "Да."}, {"reply_to": mine.id, "text": "Да."},
                     {"reply_to": event.id, "text": "Оплатите ‮счёт"}, {"reply_to": event.id, "text": "Да.",
                                                                             "html": "<b>"},
                     {"to": "stranger@other.example", "subject": "Привет", "text": "Да."},
                     {"to": "anna@acme.example", "subject": "Два\nстроки", "text": "Да."},
                     {"reply_to": event.id, "text": "Да.", "replaces": "0badc0de"}):
            said.append(await propose(core, args))
        new = await propose(core, {"to": "Anna@Acme.example", "subject": "Документы", "text": "Высылаю."})
        return said, new, outbox

    said, new, outbox = asyncio.run(run())
    assert said[0] == (False, "Отказано: этому агенту черновики не выданы.")
    texts = [text for ok, text in said[1:]]
    assert not any(ok for ok, _ in said)
    assert texts[0] == texts[1] == "Не принято: нужен ровно один из reply_to (ответ) и to (новое письмо)"
    assert texts[2].startswith("Не принято: reply_to: такого входящего") and texts[3] == texts[2], "not his own letter"
    assert texts[4] == "Не принято: текст: невидимый или управляющий символ U+202E на 10-м месте — убери его"
    assert texts[5].startswith("Не принято: аргументы: неизвестное поле html")
    assert texts[6] == ("Не принято: to: с stranger@other.example Владелец не переписывался — писать на новые адреса "
                        "нельзя")
    assert texts[7] == "Не принято: тема: одна строка" and texts[8] == "Не принято: черновика 0badc0de нет или он уже не действует"
    assert new[0], new
    (draft,) = outbox.store.in_state("shown")
    assert draft["envelope"]["to"] == "anna@acme.example" and draft["envelope"]["subject"] == "Документы"
    assert draft["envelope"]["thread"] == "" and draft["envelope"]["in_reply_to"] == ""


def test_a_telegram_reply_inside_the_window_and_a_deep_link_outside_it(tmp_path):
    """Inside the 24-hour window the card can send, and lives no longer than the window. Outside it — or with no
    gateway, or no working mail sender — the card says why, and the owner sends himself: a t.me link with the text,
    or the text to copy."""
    async def run():
        core, outbox = courier_core(tmp_path, sender=FakeNeighbour({"accounts": [ME], "dead": {ME: "invalid_grant"}}))
        channel = CardChannel()
        await core.start([channel])
        fresh = chat(core.archive, 10, NOW - 600)
        await propose(core, {"reply_to": fresh.id, "text": "Да, в 7."}, {"telegram"})
        old = chat(core.archive, 20, NOW - 25 * 3600, chat_id="778")
        await propose(core, {"reply_to": old.id, "text": "Да, в 7."})
        nameless = chat(core.archive, 21, NOW - 25 * 3600, username="", chat_id="779")
        await propose(core, {"reply_to": nameless.id, "text": "Да, в 7."})
        mail = letter(core.archive)
        await propose(core, {"reply_to": mail.id, "text": "Да."})
        return channel, outbox

    channel, outbox = asyncio.run(run())
    first, link, copy, mailto = (view for view, *_ in channel.shown)
    assert first.head[1:5] == ["Кому: Анна", "Ответ на сообщение от чт 9 октября, 11:43 МСК",
                               "Внимание: новый получатель; окно Telegram до пт 10 октября, 11:43 МСК; "
                               "по просьбе собеседника", "Текст, 8 знаков:"]
    assert [label for label, _ in channel.shown[0][1]] == ["Отправить: Анна", "Поправить", "Не отвечать"]
    (sent,) = outbox.store.in_state("shown")
    assert sent["envelope"] == {"channel": "telegram", "account": "42", "to": "777", "text": "Да, в 7.", "name": "Анна",
                                "subject": "", "thread": "", "in_reply_to": "", "references": [], "reply_to_message": 10}
    assert sent["expires"] == NOW + 3 * 3600, "three hours: the window has longer"
    assert link.head[1].startswith("окно Telegram закрыто: прошло больше суток") and \
        link.link == ("Открыть чат с этим текстом", "https://t.me/anna_x?text=%D0%94%D0%B0%2C%20%D0%B2%207.")
    assert [label for label, _ in channel.shown[1][1]] == ["Поправить", "Не отвечать"], "nothing to send: no button"
    assert copy.link == () and "нет username" in copy.head[1]
    assert mailto.head[1].startswith(f"Google не пускает отправку из {ME}: нужен вход заново") and \
        "Отправь сам из Gmail" in mailto.head[1]
    assert [d["state"] for d in outbox.store.in_state("link")] == ["link", "link", "link"]


def test_a_job_draft_waits_behind_show_and_a_corrected_draft_kills_the_old_card(tmp_path):
    """About the job search the card hides behind «Показать», like the letter it answers. «Поправь» in words makes a
    new card at once; the old one's buttons die and its last line says which card replaced it."""
    async def run():
        core, outbox = courier_core(tmp_path, mail=True)
        channel = CardChannel()
        await core.start([channel])
        event = letter(core.archive)
        core.mail.store.keep([{"seq": 1, "account": ME, "source": "mail", "ref": "m1", "data": {}}], NOW)
        core.mail.store.set(1, NOW, state="told", event_id=event.id, kind="job", sender="anna@acme.example")
        first = await propose(core, {"reply_to": event.id, "text": "Да, четверг подходит."}, {"mail"})
        (hidden,) = outbox.store.in_state("hidden")
        show = channel.cards[-1]
        shown = await core.press(channel, show[1][0][1])
        await drain()
        again = await core.press(channel, show[1][0][1])
        second = await propose(core, {"reply_to": event.id, "text": "Да, четверг в 15:00 подходит.",
                                      "replaces": hidden["id"]})
        return core, outbox, channel, first, hidden, show, shown, again, second

    core, outbox, channel, first, hidden, show, shown, again, second = asyncio.run(run())
    assert first[0] and show[0] == "Черновик на подтверждение: 1." and [b[0] for b in show[1]] == ["Показать"]
    assert "Да, четверг" not in show[0], "nothing of it on the screen before the press"
    assert shown.toast == "Показываю" and again.toast == "Кнопка уже нажата или устарела."
    card, retired = channel.shown
    old, (new,) = outbox.store.get(hidden["id"]), outbox.store.in_state("hidden")
    assert card[0].body == "Да, четверг подходит." and card[2] == old["card"] and not card[3]
    assert old["state"] == "replaced" and retired[3] is True and retired[2] == old["card"] and retired[1] == []
    assert retired[0].status == f"Заменён черновиком {new['id']}."
    alive = core.store.db.execute("SELECT COUNT(*) FROM buttons WHERE event_id = ? AND used IS NULL",
                                  (old["card"],)).fetchone()[0]
    assert alive == 0, "the old card's buttons are dead"
    assert second[0] and f"вместо черновика {old['id']}" in new["head"][4]
    assert channel.cards[-1][0] == "Черновик на подтверждение: 1.", "the new one hides behind «Показать» too"


async def card_of(core, channel, outbox, text="Да, четверг подходит.", foreign=("mail",)):
    event = letter(core.archive)
    await propose(core, {"reply_to": event.id, "text": text}, set(foreign))
    draft = outbox.store.in_state("shown")[-1]
    view, buttons, ref, _ = channel.shown[-1]
    return draft, dict(buttons)


def test_only_the_send_button_under_the_card_sends_after_the_hold_and_only_once(tmp_path, monkeypatch):
    """The owner's «да» is the button: the draft is checked again against the digest the button was bound to, held
    HOLD_S for «Отменить», then sent once with its id as the key. The word «да» in the chat sends nothing; a second
    press, a stale button, a changed envelope — nothing."""
    import retinue.outbox as outbox_module

    monkeypatch.setattr(outbox_module, "HOLD_S", 0)
    sender = FakeNeighbour()

    async def run():
        asked = []

        async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **kwargs):
            asked.append(text)
            return "done", "Хорошо.", []

        core, outbox = courier_core(tmp_path, sender=sender)
        core.ask = fake_ask
        channel = CardChannel()
        await core.start([channel])
        draft, buttons = await card_of(core, channel, outbox)
        await core.handle(channel, None, "да")
        await drain()
        said_yes = len(sender.asked)
        pressed = await core.press(channel, buttons["Отправить: anna@acme.example"])
        await drain()
        again = await core.press(channel, buttons["Отправить: anna@acme.example"])
        changed, changed_buttons = await card_of(core, channel, outbox, text="Другой текст.")
        outbox.store.db.execute("UPDATE drafts SET envelope = json_set(envelope, '$.to', 'evil@other.example') "
                                "WHERE id = ?", (changed["id"],))
        refused = await core.press(channel, changed_buttons["Отправить: anna@acme.example"])
        await drain()
        return core, outbox, channel, draft, said_yes, pressed, again, refused, changed

    core, outbox, channel, draft, said_yes, pressed, again, refused, changed = asyncio.run(run())
    assert said_yes == 0, "«да» in the chat is a message to her, never a confirmation"
    assert pressed.ok and pressed.toast == "Отправлю через 0 с" and [label for label, _ in pressed.keep] == ["Отменить"]
    assert pressed.view.status == "Отправлю через 0 с — можно отменить." and pressed.view.body == draft["envelope"]["text"]
    (key, envelope, digest), = sender.asked
    assert key == draft["id"] and envelope == draft["envelope"] and digest == draft["digest"]
    assert outbox.store.get(draft["id"])["state"] == "sent" and outbox.store.get(draft["id"])["result"] == {"id": "g1"}
    final = [s for s in channel.shown if s[2] == draft["card"] and s[3]]
    assert final[-1][0].status.startswith("Отправлено ") and final[-1][1] == []
    note = core.archive.db.execute("SELECT text FROM events WHERE json_extract(meta, '$.sent') = 1").fetchall()
    assert note == [(f"Отправлено по карточке {draft['id']}: anna@acme.example.",)], "she sees it in her next turn"
    assert again.toast == "Кнопка уже нажата или устарела."
    assert not refused.ok and refused.view.status == "Не отправлено: черновик изменился после показа."
    assert outbox.store.get(changed["id"])["state"] == "failed" and len(sender.asked) == 1
    assert outbox.store.sent(NOW - 86400, "mail") == 1 and outbox.store.sent(NOW - 86400, "telegram") == 0


def test_cancel_in_the_hold_a_newer_message_and_the_clock_kill_a_card(tmp_path, monkeypatch):
    import retinue.outbox as outbox_module

    monkeypatch.setattr(outbox_module, "HOLD_S", 0.3)
    sender = FakeNeighbour()

    async def run():
        core, outbox = courier_core(tmp_path, sender=sender)
        channel = CardChannel()
        await core.start([channel])
        held, buttons = await card_of(core, channel, outbox)
        pressed = await core.press(channel, buttons["Отправить: anna@acme.example"])
        cancelled = await core.press(channel, dict(pressed.keep)["Отменить"])
        await asyncio.sleep(0.5)
        stale, stale_buttons = await card_of(core, channel, outbox, text="Ещё раз да.")
        letter(core.archive, ref="m9", ts=NOW + 60)                      # she wrote again in the same thread
        newer = await core.press(channel, stale_buttons["Отправить: anna@acme.example"])
        fixed, fix_buttons = await card_of(core, channel, outbox, text="Поправлю.")
        fix = await core.press(channel, fix_buttons["Поправить"])
        skipped, skip_buttons = await card_of(core, channel, outbox, text="Не буду.")
        skip = await core.press(channel, skip_buttons["Не отвечать"])
        old, _ = await card_of(core, channel, outbox, text="Забуду.")
        await core.step(NOW + 3 * 3600)
        return outbox, channel, held, cancelled, stale, newer, fixed, fix, skipped, skip, old

    outbox, channel, held, cancelled, stale, newer, fixed, fix, skipped, skip, old = asyncio.run(run())
    assert cancelled.ok and cancelled.view.status == "Отменено — не отправлено." and sender.asked == []
    assert outbox.store.get(held["id"])["state"] == "cancelled"
    assert not newer.ok and newer.view.status == "Устарела: пришло новое сообщение.", "the second line of defence"
    assert outbox.store.get(stale["id"])["state"] == "stale"
    assert fix.view.status.startswith("Ждёт правки") and outbox.store.get(fixed["id"])["state"] == "fixing"
    assert skip.view.status == "Не отвечаю." and outbox.store.get(skipped["id"])["state"] == "dropped"
    assert outbox.store.get(old["id"])["state"] == "expired"
    assert channel.shown[-1][0].status == "Устарел: не отправлено." and channel.shown[-1][2] == old["card"]


def test_a_failure_and_a_doubt_are_told_and_a_doubt_is_settled_by_what_came_back(tmp_path, monkeypatch):
    """A refusal is the card's last line and a message; a doubt (no answer, a timeout) is never sent again and is
    settled when the letter comes back from Sent with our Message-ID, or the reply comes back from Telegram. A
    restart in the hold sends nothing and shows the card again; one during the send is a doubt."""
    import retinue.outbox as outbox_module
    from test_mailroom import FakeCollector, FakeGateway

    monkeypatch.setattr(outbox_module, "HOLD_S", 0)
    sender = FakeNeighbour(answer={"status": "failed", "error": "invalid_grant"})
    gateway = FakeNeighbour({"connected": True, "rights": ["can_reply"]}, answer=Unavailable("шлюз Telegram недоступен"))

    async def run():
        core, outbox = courier_core(tmp_path, sender=sender, gateway=gateway)
        collector, chats = FakeCollector(), FakeGateway()
        core.mail = Mailroom(collector, MailStore(core.store.db), gateway=chats)
        core.mail.attach(core)
        channel = CardChannel()
        await core.start([channel])
        failed, buttons = await card_of(core, channel, outbox)
        await core.press(channel, buttons["Отправить: anna@acme.example"])
        await drain()
        sender.answer = Unavailable("отправка почты недоступна")
        doubt, buttons = await card_of(core, channel, outbox, text="Второй.")
        await core.press(channel, buttons["Отправить: anna@acme.example"])
        await drain()
        message = chat(core.archive, 10, NOW - 600)
        await propose(core, {"reply_to": message.id, "text": "Да, в 7."})
        (in_chat,) = [d for d in outbox.store.in_state("shown") if d["channel"] == "telegram"]
        await core.press(channel, dict(channel.shown[-1][1])["Отправить: Анна"])
        await drain()
        collector.letter_in(1, "s1", ME, "Re: Интервью", "Второй.", ["SENT"], box="SENT")
        collector.letters["s1"]["message_id"] = f"<retinue.{doubt['id']}@example.org>"
        chats.message(1, 50, "Да, в 7.", direction="bot")
        await core.mail.work(NOW)
        return core, outbox, channel, failed, doubt, in_chat

    core, outbox, channel, failed, doubt, in_chat = asyncio.run(run())
    told = [text for text, *_ in channel.cards]
    assert outbox.store.get(failed["id"])["state"] == "failed"
    assert told[0].startswith(f"Не отправлено: черновик {failed['id']} (anna@acme.example) — Google не пускает отправку")
    assert f"`uv run deploy/mail/login.py --account {ME} --kind send`" in told[0]
    assert told[1].startswith(f"Не знаю, ушло ли: черновик {doubt['id']}") and "Повторно не отправляю" in told[1]
    assert outbox.store.get(doubt["id"])["state"] == "sent" and outbox.store.get(doubt["id"])["result"]["settled"]
    assert outbox.store.get(in_chat["id"])["state"] == "sent", "the reply came back from Telegram: it went"
    assert [s[0].status for s in channel.shown if s[2] == doubt["card"]][-1] == "Нашлось в отправленных: ушло."
    assert len(sender.asked) == 2 and len(gateway.asked) == 1, "a doubt is never asked again"

    async def restart():
        store = Store(str(tmp_path / "r.sqlite"))
        outbox = Outbox(OutboxStore(store.db), sender=FakeNeighbour(), owner_tg=42)
        outbox.clock = lambda: NOW
        core = Core([ASSISTANT], store, "owner", archive=Archive(str(tmp_path / "a.sqlite")), outbox=outbox)
        draft = outbox.store.get(failed["id"])
        outbox.store.db.execute("UPDATE drafts SET state = 'queued' WHERE id = ?", (failed["id"],))
        outbox.store.db.execute("UPDATE drafts SET state = 'sending' WHERE id = ?", (in_chat["id"],))
        outbox.store.db.commit()
        channel = CardChannel()
        await core.start([channel])
        return outbox, channel, draft

    outbox, channel, draft = asyncio.run(restart())
    assert outbox.store.get(failed["id"])["state"] == "shown" and outbox.store.get(in_chat["id"])["state"] == "unknown"
    again = [s for s in channel.shown if not s[3]]
    assert again[0][0].status.startswith("Роутер перезапускался во время отсрочки: не отправлено. Нажми ещё раз")
    assert outbox.store.get(failed["id"])["card"] != draft["card"], "a new card with live buttons"
    assert any(text.startswith(f"Не знаю, ушло ли: черновик {in_chat['id']}") for text, *_ in channel.cards)


def test_a_new_message_in_the_exchange_kills_the_open_card_at_once(tmp_path, monkeypatch):
    """The other person writes again before the owner pressed: the card dies at once — rewritten in place, buttons
    gone — and the new message goes to her judgement with the exchange above it, to draft afresh. A card in the hold
    dies too, and nothing leaves."""
    import retinue.outbox as outbox_module
    from test_mailroom import FakeGateway, decided, judging_ask

    monkeypatch.setattr(outbox_module, "HOLD_S", 0.3)
    chats, asked = FakeGateway(), []
    gateway = FakeNeighbour({"connected": True, "rights": ["can_reply"]})

    async def run():
        core, outbox = courier_core(tmp_path, gateway=gateway)
        core.ask = judging_ask(asked, {}, decided(("reply", "low", "Анна спрашивает про время.")), lambda: core)
        core.mail = Mailroom(None, MailStore(core.store.db), gateway=chats)
        core.mail.attach(core)
        channel = CardChannel()
        await core.start([channel])
        chats.message(1, 10, "Созвон в 7?")
        await core.mail.work(NOW)
        await propose(core, {"reply_to": "tgb:777/10", "text": "Да, в 7."}, {"telegram"})
        (first,) = outbox.store.in_state("shown")
        chats.message(2, 11, "Или лучше в 8?")
        await core.mail.work(NOW + 100)
        await core.mail.work(NOW + 300)
        await propose(core, {"reply_to": "tgb:777/11", "text": "В 8 тоже могу."}, {"telegram"})
        (second,) = outbox.store.in_state("shown")
        held = await core.press(channel, dict(channel.shown[-1][1])["Отправить: Анна"])
        chats.message(3, 12, "Алло?")
        await core.mail.work(NOW + 400)
        await asyncio.sleep(0.5)
        return core, outbox, channel, first, second, held

    core, outbox, channel, first, second, held = asyncio.run(run())
    assert outbox.store.get(first["id"])["state"] == "stale"
    killed = [s for s in channel.shown if s[2] == first["card"] and s[3]]
    assert killed[-1][0].status == "Устарела: пришло новое сообщение." and killed[-1][1] == [], "in place, no buttons"
    alive = core.store.db.execute("SELECT COUNT(*) FROM buttons WHERE event_id = ? AND used IS NULL",
                                  (first["card"],)).fetchone()[0]
    assert alive == 0
    judged = asked[-1]["text"]
    assert "Раньше в этой переписке" in judged and judged.index("Созвон в 7?") < judged.index("Или лучше в 8?"), \
        "her judgement of the new message sees the exchange above it"
    assert held.ok and outbox.store.get(second["id"])["state"] == "stale" and gateway.asked == [], \
        "a card in the hold dies too: nothing leaves"
