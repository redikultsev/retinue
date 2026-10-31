"""Replies to people on the router's side, with a fake mail sender and a fake Telegram gateway: the envelope comes
from the archive, the card says what code found, and only the owner's «Send» under that card sends — once."""

import asyncio
import sqlite3

import httpx
import pytest

from retinue import clock, courier
from retinue.archive import Archive
from retinue.config import RouterAgent
from retinue.core import Core
from retinue.mail import MailStore, Unavailable
from retinue.mailroom import DIRECTIONS, Mailroom
from retinue.outbox import Neighbour, Outbox, OutboxStore
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
                                                                   "Ссылка: https://cv.example/me?ref=acme"}, {"mail"})
        return core, outbox, channel, done

    core, outbox, channel, (ok, said) = asyncio.run(run())
    (draft,) = outbox.store.in_state("shown")
    assert ok and said.startswith(f"Draft {draft['id']} is shown to the owner as a card with the whole text. It leaves "
                                  "only if he presses «Send»") and "Do not say it was sent." in said
    envelope = courier.Envelope.of(draft["envelope"])
    assert (envelope.account, envelope.to, envelope.subject, envelope.thread) == (
        ME, "anna@acme.example", "Re: Интервью", "18c2f0a1b2c3d4e5")
    assert envelope.in_reply_to == "<m1@acme.example>" and envelope.references == ("<m0@acme.example>", "<m1@acme.example>")
    assert draft["digest"] == courier.digest(envelope) and draft["thread"] == "mail:18c2f0a1b2c3d4e5"
    (view, buttons, ref, edit), = channel.shown
    assert view.head == [f"Draft {draft['id']} · Gmail · {ME}", "To: <anna@acme.example>", "Subject: Re: Интервью",
                         f"Reply to the letter of {clock.day(NOW - 600, core.tz)}",
                         "Warning: new recipient; at the other person's request",
                         "Found in the text: link https://cv.example/me?ref=acme — with parameters", "Text, 82 characters:"]
    assert view.body == envelope.text and not edit and ref == draft["card"]
    assert [label for label, _ in buttons] == ["Send: anna@acme.example", "Edit", "Don't reply"], \
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
    assert said[0][0] is False and not said[0][1].startswith("Not accepted"), "no grant: refused by the core"
    texts = [text for ok, text in said[1:]]
    assert not any(ok for ok, _ in said)
    assert texts[0] == texts[1] == "Not accepted: exactly one of reply_to (a reply) and to (a new letter) is needed"
    assert texts[2].startswith("Not accepted: reply_to: no such incoming") and texts[3] == texts[2], "not his own letter"
    assert texts[4] == "Not accepted: text: an invisible or control character U+202E at position 10 — remove it"
    assert texts[5].startswith("Not accepted: ") and "html" in texts[5], "the schema's own refusal"
    assert texts[6] == ("Not accepted: to: the owner has never corresponded with stranger@other.example — writing to new "
                        "addresses is not allowed")
    assert texts[7] == "Not accepted: subject: one line only"
    assert texts[8] == "Not accepted: draft 0badc0de does not exist or is no longer valid"
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
    tz = outbox.core.tz
    first, link, copy, mailto = (view for view, *_ in channel.shown)
    assert first.head[1:5] == ["To: Анна", f"Reply to the message of {clock.day(NOW - 600, tz)}",
                               f"Warning: new recipient; Telegram window until {clock.day(NOW - 600 + 86400, tz)}; "
                               "at the other person's request", "Text, 8 characters:"]
    assert [label for label, _ in channel.shown[0][1]] == ["Send: Анна", "Edit", "Don't reply"]
    (sent,) = outbox.store.in_state("shown")
    assert sent["envelope"] == {"channel": "telegram", "account": "42", "to": "777", "text": "Да, в 7.", "name": "Анна",
                                "subject": "", "thread": "", "in_reply_to": "", "references": [], "reply_to_message": 10}
    assert sent["expires"] == NOW + 3 * 3600, "three hours: the window has longer"
    assert link.head[1].startswith("the Telegram window is closed: more than a day") and \
        link.link == ("Open the chat with this text", "https://t.me/anna_x?text=%D0%94%D0%B0%2C%20%D0%B2%207.")
    assert [label for label, _ in channel.shown[1][1]] == ["Edit", "Don't reply"], "nothing to send: no button"
    assert copy.link == () and "has no username" in copy.head[1]
    assert mailto.head[1].startswith(f"Google does not let {ME} send: log in again") and \
        "Send it yourself from Gmail" in mailto.head[1]
    assert [d["state"] for d in outbox.store.in_state("link")] == ["link", "link", "link"]


def test_a_private_draft_waits_behind_show_and_a_corrected_draft_kills_the_old_card(tmp_path):
    """About the private matter the card hides behind «Show», like the letter it answers. «Change it» in words makes a
    new card at once; the old one's buttons die and its last line says which card replaced it."""
    async def run():
        core, outbox = courier_core(tmp_path, mail=True)
        channel = CardChannel()
        await core.start([channel])
        event = letter(core.archive)
        core.mail.store.keep([{"seq": 1, "account": ME, "source": "mail", "ref": "m1", "data": {}}], NOW)
        core.mail.store.set(1, NOW, state="told", event_id=event.id, kind="private", sender="anna@acme.example")
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
    assert first[0] and show[0] == "Draft to confirm: 1." and [b[0] for b in show[1]] == ["Show"]
    assert "Да, четверг" not in show[0], "nothing of it on the screen before the press"
    assert shown.toast == "Showing" and not again.ok, "a pressed button is spent"
    card, retired = channel.shown
    old, (new,) = outbox.store.get(hidden["id"]), outbox.store.in_state("hidden")
    assert card[0].body == "Да, четверг подходит." and card[2] == old["card"] and not card[3]
    assert old["state"] == "replaced" and retired[3] is True and retired[2] == old["card"] and retired[1] == []
    assert retired[0].status == f"Replaced by draft {new['id']}."
    alive = core.store.db.execute("SELECT COUNT(*) FROM buttons WHERE event_id = ? AND used IS NULL",
                                  (old["card"],)).fetchone()[0]
    assert alive == 0, "the old card's buttons are dead"
    assert second[0] and f"replaces draft {old['id']}" in new["head"][4]
    assert channel.cards[-1][0] == "Draft to confirm: 1.", "the new one hides behind «Show» too"


async def card_of(core, channel, outbox, text="Да, четверг подходит.", foreign=("mail",)):
    event = letter(core.archive)
    await propose(core, {"reply_to": event.id, "text": text}, set(foreign))
    draft = outbox.store.in_state("shown")[-1]
    view, buttons, ref, _ = channel.shown[-1]
    return draft, dict(buttons)


def test_only_the_send_button_under_the_card_sends_after_the_hold_and_only_once(tmp_path, monkeypatch):
    """The owner's «yes» is the button: the draft is checked again against the digest the button was bound to, held
    HOLD_S for «Cancel», then sent once with its id as the key. The word «yes» in the chat sends nothing; a second
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
        await core.handle(channel, None, "yes")
        await drain()
        said_yes = len(sender.asked)
        pressed = await core.press(channel, buttons["Send: anna@acme.example"])
        await drain()
        again = await core.press(channel, buttons["Send: anna@acme.example"])
        changed, changed_buttons = await card_of(core, channel, outbox, text="Другой текст.")
        outbox.store.db.execute("UPDATE drafts SET envelope = json_set(envelope, '$.to', 'evil@other.example') "
                                "WHERE id = ?", (changed["id"],))
        refused = await core.press(channel, changed_buttons["Send: anna@acme.example"])
        await drain()
        return core, outbox, channel, draft, said_yes, pressed, again, refused, changed

    core, outbox, channel, draft, said_yes, pressed, again, refused, changed = asyncio.run(run())
    assert said_yes == 0, "«yes» in the chat is a message to her, never a confirmation"
    assert pressed.ok and pressed.toast == "Sending in 0 s" and [label for label, _ in pressed.keep] == ["Cancel"]
    assert pressed.view.status == "Sending in 0 s — you can cancel." and pressed.view.body == draft["envelope"]["text"]
    (key, envelope, digest), = sender.asked
    assert key == draft["id"] and envelope == draft["envelope"] and digest == draft["digest"]
    assert outbox.store.get(draft["id"])["state"] == "sent" and outbox.store.get(draft["id"])["result"] == {"id": "g1"}
    final = [s for s in channel.shown if s[2] == draft["card"] and s[3]]
    assert final[-1][0].status.startswith("Sent ") and final[-1][1] == []
    note = core.archive.db.execute("SELECT text FROM events WHERE json_extract(meta, '$.sent') = 1").fetchall()
    assert note == [(f"Sent by card {draft['id']}: anna@acme.example.",)], "she sees it in her next turn"
    assert not again.ok, "a pressed button is spent"
    assert not refused.ok and refused.view.status == "Not sent: the draft changed after it was shown."
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
        pressed = await core.press(channel, buttons["Send: anna@acme.example"])
        cancelled = await core.press(channel, dict(pressed.keep)["Cancel"])
        await asyncio.sleep(0.5)
        stale, stale_buttons = await card_of(core, channel, outbox, text="Ещё раз да.")
        letter(core.archive, ref="m9", ts=NOW + 60)                      # she wrote again in the same thread
        newer = await core.press(channel, stale_buttons["Send: anna@acme.example"])
        fixed, fix_buttons = await card_of(core, channel, outbox, text="Поправлю.")
        fix = await core.press(channel, fix_buttons["Edit"])
        skipped, skip_buttons = await card_of(core, channel, outbox, text="Не буду.")
        skip = await core.press(channel, skip_buttons["Don't reply"])
        old, _ = await card_of(core, channel, outbox, text="Забуду.")
        await core.step(NOW + 3 * 3600)
        return outbox, channel, held, cancelled, stale, newer, fixed, fix, skipped, skip, old

    outbox, channel, held, cancelled, stale, newer, fixed, fix, skipped, skip, old = asyncio.run(run())
    assert cancelled.ok and cancelled.view.status == "Cancelled — not sent." and sender.asked == []
    assert outbox.store.get(held["id"])["state"] == "cancelled"
    assert not newer.ok and newer.view.status == "Expired: a new message came in.", "the second line of defence"
    assert outbox.store.get(stale["id"])["state"] == "stale"
    assert fix.view.status.startswith("Waiting for an edit") and outbox.store.get(fixed["id"])["state"] == "fixing"
    assert skip.view.status == "Not replying." and outbox.store.get(skipped["id"])["state"] == "dropped"
    assert outbox.store.get(old["id"])["state"] == "expired"
    assert channel.shown[-1][0].status == "Expired: not sent." and channel.shown[-1][2] == old["card"]


def test_a_failure_and_a_doubt_are_told_and_a_doubt_is_settled_by_what_came_back(tmp_path, monkeypatch):
    """A refusal is the card's last line and a message; a doubt (no answer, a timeout) is never sent again and is
    settled when the letter comes back from Sent with our Message-ID, or the reply comes back from Telegram. A
    restart in the hold sends nothing and shows the card again; one during the send is a doubt."""
    import retinue.outbox as outbox_module
    from test_mailroom import FakeCollector, FakeGateway

    monkeypatch.setattr(outbox_module, "HOLD_S", 0)
    sender = FakeNeighbour(answer={"status": "failed", "error": "invalid_grant"})
    gateway = FakeNeighbour({"connected": True, "rights": ["can_reply"]}, answer=Unavailable("unavailable: Telegram gateway"))

    async def run():
        core, outbox = courier_core(tmp_path, sender=sender, gateway=gateway)
        collector, chats = FakeCollector(), FakeGateway()
        core.mail = Mailroom(collector, MailStore(core.store.db), gateway=chats)
        core.mail.attach(core)
        channel = CardChannel()
        await core.start([channel])
        failed, buttons = await card_of(core, channel, outbox)
        await core.press(channel, buttons["Send: anna@acme.example"])
        await drain()
        sender.answer = Unavailable("unavailable: mail sending")
        doubt, buttons = await card_of(core, channel, outbox, text="Второй.")
        await core.press(channel, buttons["Send: anna@acme.example"])
        await drain()
        message = chat(core.archive, 10, NOW - 600)
        await propose(core, {"reply_to": message.id, "text": "Да, в 7."})
        (in_chat,) = [d for d in outbox.store.in_state("shown") if d["channel"] == "telegram"]
        await core.press(channel, dict(channel.shown[-1][1])["Send: Анна"])
        await drain()
        collector.letter_in(1, "s1", ME, "Re: Интервью", "Второй.", ["SENT"], box="SENT")
        collector.letters["s1"]["message_id"] = f"<retinue.{doubt['id']}@example.org>"
        chats.message(1, 50, "Да, в 7.", direction="bot")
        await core.mail.work(NOW)
        return core, outbox, channel, failed, doubt, in_chat

    core, outbox, channel, failed, doubt, in_chat = asyncio.run(run())
    told = [text for text, *_ in channel.cards]
    assert outbox.store.get(failed["id"])["state"] == "failed"
    assert told[0].startswith(f"Not sent: draft {failed['id']} (anna@acme.example) — Google does not let it send")
    assert f"`uv run deploy/mail/login.py --account {ME} --kind send`" in told[0]
    assert told[1].startswith(f"Not sure it went: draft {doubt['id']}") and "Not sending it again" in told[1]
    assert outbox.store.get(doubt["id"])["state"] == "sent" and outbox.store.get(doubt["id"])["result"]["settled"]
    assert outbox.store.get(in_chat["id"])["state"] == "sent", "the reply came back from Telegram: it went"
    assert [s[0].status for s in channel.shown if s[2] == doubt["card"]][-1] == "Found in Sent: it went."
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
    assert again[0][0].status.startswith("The router restarted during the hold: not sent. Press again")
    assert outbox.store.get(failed["id"])["card"] != draft["card"], "a new card with live buttons"
    assert any(text.startswith(f"Not sure it went: draft {in_chat['id']}") for text, *_ in channel.cards)


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
        held = await core.press(channel, dict(channel.shown[-1][1])["Send: Анна"])
        chats.message(3, 12, "Алло?")
        await core.mail.work(NOW + 400)   # the chat's last message has been quiet for long: judged first
        await core.mail.work(NOW + 410)   # the next step reads the new one
        await asyncio.sleep(0.5)
        return core, outbox, channel, first, second, held

    core, outbox, channel, first, second, held = asyncio.run(run())
    assert outbox.store.get(first["id"])["state"] == "stale"
    killed = [s for s in channel.shown if s[2] == first["card"] and s[3]]
    assert killed[-1][0].status == "Expired: a new message came in." and killed[-1][1] == [], "in place, no buttons"
    alive = core.store.db.execute("SELECT COUNT(*) FROM buttons WHERE event_id = ? AND used IS NULL",
                                  (first["card"],)).fetchone()[0]
    assert alive == 0
    judged = asked[-1]["text"]
    assert judged.index("Созвон в 7?") < judged.index("Или лучше в 8?"), \
        "her judgement of the new message sees the exchange above it"
    assert held.ok and outbox.store.get(second["id"])["state"] == "stale" and gateway.asked == [], \
        "a card in the hold dies too: nothing leaves"


def test_a_telegram_reply_that_left_is_in_the_exchange_at_once(tmp_path, monkeypatch):
    """Seen on the server: Telegram does not give a bot back the messages it sent, so the exchange her next
    judgement reads lacked his reply, and she drafted the same answer again. A reply that left is put in the
    exchange by the router at once — as his reply sent by his card, under the message id Telegram gave it."""
    import retinue.outbox as outbox_module

    monkeypatch.setattr(outbox_module, "HOLD_S", 0)
    gateway = FakeNeighbour({"connected": True, "rights": ["can_reply"]}, {"status": "sent", "id": "55"})

    async def run():
        core, outbox = courier_core(tmp_path, gateway=gateway)
        channel = CardChannel()
        await core.start([channel])
        asked = chat(core.archive, 10, NOW - 600)
        await propose(core, {"reply_to": asked.id, "text": "Да, в 7."}, {"telegram"})
        await core.press(channel, dict(channel.shown[-1][1])["Send: Анна"])
        await drain()
        later = chat(core.archive, 56, NOW + 60)
        return core, core.archive.earlier(later, 10)

    core, earlier = asyncio.run(run())
    assert [e.text for e in earlier] == ["Telegram · Анна · входящее\nСозвон в 7?",
                                         f"Telegram · Анна · {DIRECTIONS['bot']}\nДа, в 7."]
    reply = earlier[-1]
    assert (reply.kind, reply.conversation_id, reply.channel) == ("chat", "chat:777", "tgb")
    assert reply.meta["by"] == "bot" and reply.meta["direction"] == "out" and reply.meta["message_id"] == 55


def test_a_card_names_what_she_read_outside_this_conversation_and_warns_louder_about_the_private(tmp_path):
    """A draft may use what she found elsewhere — a bus booking in the mail for a friend's «when do you arrive?» — but
    the card says so: «Read while drafting: …» with counts by source, never their words; code tracked every read of the
    run (search finds, files, the base). The private matter, closed Spaces of the base and, in a run somebody else
    started, the owner's own conversation make it louder; a run that read the private matter hides the card behind
    «Show», like a card about it."""
    from types import SimpleNamespace

    async def run():
        core, outbox = courier_core(tmp_path, mail=True)
        core.memory = SimpleNamespace(cfg=SimpleNamespace(tree="/kb", sensitive=["work", "legal"]))
        channel = CardChannel()
        await core.start([channel])
        target = chat(core.archive)
        for ref, text, kind in (("b1", "Бронь автобуса Энск — Зурбаган, 14:20", "travel"),
                                ("j1", "Юрист ждёт вас в четверг", "private")):
            event, _ = core.archive.append("mail", text, conversation_id=f"mail:{ME}", channel="mail",
                                           native_id=f"{ME}/{ref}", ts=NOW - 3600, meta={"thread": ref})
            core.mail.store.keep([{"seq": len(ref), "account": ME, "source": "mail", "ref": ref, "data": {}}], NOW)
            core.mail.store.db.execute("UPDATE mail_items SET kind = ?, event_id = ? WHERE ref = ?", (kind, event.id, ref))
        core.archive.append("owner", "Я в Зурбагане до пятницы", conversation_id=core.store.conversation("assistant"),
                            channel="telegram", ts=NOW - 7200)
        me, cards = core.agents["assistant"], []

        async def draft(started_by_her_chat, reads, text):
            turn = core.turns.open_root("assistant")
            if started_by_her_chat:
                turn.tree.foreign.add("telegram")
                turn.tree.exchanges.add("chat:777")
            for kind, value in reads:
                if kind == "kb":
                    await core.kb_log(me, turn.id, "Read", "allow", value)
                else:
                    await core.archive_search(me, turn.id, value, scope=kind)
            ok, said = await core.draft_reply(me, turn.id, {"reply_to": target.id, "text": text})
            core.turns.close(turn)
            draft_ = outbox.store.get(said.split()[1])
            cards.append(draft_)
            return draft_

        await draft(True, [("chat", "Созвон")], "В 7 подходит.")
        await draft(True, [("mail", "автобус"), ("kb", "/kb/reading/knowledge/books.md")], "Приеду в 14:20.")
        await draft(True, [("kb", "/kb/work/profile/contract.md"), ("owner", "Зурбаган")], "Я в Зурбагане до пятницы.")
        await draft(False, [("all", "юрист")], "Занят в четверг.")
        return outbox, channel, cards

    outbox, channel, (only_here, booking, closed, private) = asyncio.run(run())
    assert not any(line.startswith("Read while drafting") for line in only_here["head"]), "this chat only: nothing to say"
    assert "Read while drafting: mail 1, Base (reading) 1" in booking["head"]
    assert not any("closed matter" in line for line in booking["head"])
    assert "Read while drafting: your conversation 1, Base (work) 1" in closed["head"]
    warning = next(line for line in closed["head"] if line.startswith("Warning:"))
    assert "read closed matter: your conversation 1, Base (work) 1 — check that none of it went into the text" in warning
    assert "read closed matter: private 1" in next(line for line in private["head"] if line.startswith("Warning:"))
    assert private["private"] == 1 and outbox.store.get(private["id"])["state"] == "hidden", \
        "behind «Show», as the private matter"
    assert channel.cards[-1][0] == "Draft to confirm: 1."
    assert all("Бронь" not in line and "Юрист" not in line for c in (booking, private) for line in c["head"]), \
        "counts, never words"


def test_a_draft_flagged_before_the_switch_to_english_still_names_its_recipient(tmp_path):
    """A live draft keeps its head as it was made: one flagged in Russian still gets the button that names whom."""
    _, outbox = courier_core(tmp_path)
    draft = {"id": "d1", "state": "shown", "digest": "x", "envelope": {"to": "anna@acme.example", "name": ""},
             "head": ["Черновик d1 · Gmail · owner@example.org", "Внимание: новый получатель"]}
    assert [b.label for b in outbox.buttons(draft)] == ["Send: anna@acme.example", "Edit", "Don't reply"]
    draft["head"] = ["Draft d1 · Gmail · owner@example.org", "Warning: new recipient"]
    assert outbox.buttons(draft)[0].label == "Send: anna@acme.example"
    draft["head"] = ["Draft d1 · Gmail · owner@example.org"]
    assert outbox.buttons(draft)[0].label == "Send"


def test_a_drafts_table_of_the_former_column_name_is_renamed_on_open():
    """A router database written before the column was renamed: the drafts keep their flag."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE drafts (id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL, channel TEXT "
               "NOT NULL, envelope TEXT NOT NULL, digest TEXT NOT NULL, reply_to TEXT NOT NULL DEFAULT '', thread TEXT "
               "NOT NULL DEFAULT '', head TEXT NOT NULL, job INTEGER NOT NULL DEFAULT 0, card TEXT NOT NULL DEFAULT "
               "'', expires REAL NOT NULL, send_after REAL, result TEXT NOT NULL DEFAULT '{}', at REAL NOT NULL)")
    db.execute("INSERT INTO drafts (id, created, state, channel, envelope, digest, head, job, expires, at) VALUES "
               "('d1', 1, 'hidden', 'mail', '{}', 'x', '[]', 1, 9, 1)")
    store = OutboxStore(db)
    assert store.get("d1")["private"] == 1 and OutboxStore(db).get("d1")["private"] == 1, "once, then as it is"


@pytest.mark.parametrize("respond, said", [
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")), "unavailable: the platform (ConnectError)"),
    (lambda request: httpx.Response(503, json={}), "the platform: HTTP 503")])
def test_a_neighbours_silence_names_it(respond, said):
    neighbour = Neighbour("http://platform:9500", "t", "the platform",
                          httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    with pytest.raises(Unavailable) as caught:
        asyncio.run(neighbour.items())
    assert str(caught.value) == said
