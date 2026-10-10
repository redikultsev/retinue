"""The mail's work in the router with a fake collector and a fake assistant: what is kept, what is dropped and by
whom, in what order and how fast — no model, no Google."""

import asyncio
import json
import sqlite3

from retinue.archive import Archive
from retinue.core import Core, Reply
from retinue.mail import MailStore, Unavailable
from retinue.mailroom import Mailroom
from retinue.protocol import Store

from test_core import AGENT, FakeChannel, drain

NOW = 1_760_000_000.0
ACCOUNT = "owner@example.org"


class FakeCollector:
    """The collector's answers from memory: items to look at, letters by id, attachments by number, status rows."""

    def __init__(self):
        self.found, self.letters, self.files, self.sources, self.confirmed, self.fetched = [], {}, {}, [], [], []
        self.fail_confirm = False

    def letter_in(self, seq, ref, sender, subject, text, labels=("INBOX",), backfill=False, attachments=(), box="INBOX"):
        self.found.append({"seq": seq, "account": ACCOUNT, "source": "mail", "ref": ref,
                           "data": {"box": box, "labels": list(labels), **({"backfill": True} if backfill else {})}})
        self.letters[ref] = {"sender": sender, "name": sender.split("@")[0], "to": [ACCOUNT], "cc": [],
                             "subject": subject, "date": "Thu, 9 Oct 2025 10:00:00 +0000", "text": text,
                             "hidden": 0, "unsubscribe": False, "auto": "", "labels": list(labels),
                             "attachments": [{"part": n, "name": name, "type": t, "size": len(d)}
                                             for n, (name, t, d) in enumerate(attachments, 2)],
                             "id": ref, "thread": "t", "ts": NOW - 60, "message_id": f"<{ref}@x>", "in_reply_to": ""}
        for n, file in enumerate(attachments, 2):
            self.files[(ref, n)] = file

    async def items(self):
        return [i for i in self.found if i["seq"] > (max(self.confirmed) if self.confirmed else 0)]

    async def confirm(self, upto):
        if self.fail_confirm:
            raise Unavailable("сборщик недоступен (ConnectError)")
        self.confirmed.append(upto)

    async def letter(self, account, message_id):
        return self.letters.get(message_id)

    async def attachment(self, account, message_id, part):
        self.fetched.append((message_id, part))
        return self.files[(message_id, part)]

    async def status(self):
        return {"sources": self.sources, "keys": []}

    async def agenda(self, start, end):
        return [], []



def triage_ask(asked, verdicts):
    """The assistant's triage: by the sender in the prompt, a verdict from `verdicts` (JSON text or an exception)."""
    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, attachments=None,
                       schema=None):
        asked.append({"text": text, "context": context_id, "turn": turn_id, "control": control, "schema": schema})
        for sender, verdict in verdicts.items():
            if f"<{sender}>" in text:
                if isinstance(verdict, Exception):
                    raise verdict
                if isinstance(verdict, Reply):
                    return verdict
                return Reply("done", json.dumps(verdict, ensure_ascii=False), [], {})
        return Reply("done", "{}", [], {})
    return fake_ask


def verdict(kind, keep, quote="", **extra):
    return {"kind": kind, "keep": keep, "who": "x", "summary": "сводка", "needs": "", "deadline": "", "quote": quote,
            **extra}


def mailroom_with(tmp_path, fake, asked, verdicts):
    store = Store(str(tmp_path / "r.sqlite"))
    room = Mailroom(fake, MailStore(store.db))
    core = Core([AGENT], store, "owner", ask=triage_ask(asked, verdicts), archive=Archive(str(tmp_path / "a.sqlite")),
                mail=room)
    return core, room


def test_a_letter_is_kept_or_dropped_by_her_one_per_run_and_his_block_needs_no_model(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "hr@acme.example", "Интервью", "Приглашаем на интервью в четверг.")
    fake.letter_in(2, "m2", "sale@shop.example", "Скидки", "Всё по 1 евро", ["INBOX", "CATEGORY_PROMOTIONS"])
    fake.letter_in(3, "m3", "noreply@ads.example", "Ещё скидки", "x")
    fake.letter_in(4, "m4", ACCOUNT, "Re: Интервью", "Да, удобно.", ["SENT"], box="SENT",
                   attachments=[("cv.pdf", "application/pdf", b"%PDF")])
    fake.found.append({"seq": 5, "account": ACCOUNT, "source": "mail", "ref": "m5", "data": {"box": "INBOX"}})
    verdicts = {"hr@acme.example": verdict("job", True, "интервью в четверг"),
                "sale@shop.example": verdict("promo", False)}

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, verdicts)
        room.store.rule("block", "noreply@ads.example")
        await core.start([FakeChannel("telegram", False)])
        for n in range(6):
            await room.work(NOW + n)
        return core, room

    core, room = asyncio.run(run())
    assert fake.confirmed == [5], "looked, kept, then confirmed up to the last one kept"
    assert [a["control"] for a in asked] == ["bare", "bare"], "only the two letters she must judge: one run each"
    assert asked[0]["context"] != asked[1]["context"] and asked[0]["turn"] is None, "no turn: no tool to bind"
    assert asked[0]["schema"]["required"][:2] == ["kind", "keep"] and "Скидки" not in asked[0]["text"]
    states = {i["seq"]: i["state"] for i in [room.store.get(n) for n in range(1, 6)]}
    assert states == {1: "kept", 2: "dropped", 3: "blocked", 4: "own", 5: "gone"}
    mails = core.archive.db.execute("SELECT id, kind, text, meta FROM events WHERE kind = 'mail' ORDER BY seq").fetchall()
    assert [m[0] for m in mails] == [f"mail:{ACCOUNT}/m4", f"mail:{ACCOUNT}/m1"], \
        "his own letter first, it needs no model; dropped and blocked: not stored"
    assert "От: hr <hr@acme.example>" in mails[1][2] and "Приглашаем на интервью" in mails[1][2]
    meta = json.loads(mails[1][3])
    assert meta["direction"] == "in" and meta["card"]["verified"] and meta["card"]["kind"] == "job"
    assert json.loads(mails[0][3])["direction"] == "out" and "отправлено Владельцем" in mails[0][2]
    assert "[не прочитано: «cv.pdf»: вложение его письма, не загружалось]" in mails[0][2] and fake.fetched == []
    log = room.store.db.execute("SELECT sender, subject, kind, why FROM mail_dropped ORDER BY ts").fetchall()
    assert log == [("sale@shop.example", "Скидки", "promo", "triage"), ("noreply@ads.example", "Ещё скидки", "", "blocked")]
    runs = core.store.db.execute("SELECT kind, status FROM runs").fetchall()
    assert runs == [("triage", "done"), ("triage", "done")]


def test_a_kept_letters_files_are_read_and_a_dropped_ones_never_downloaded(tmp_path):
    from test_attachments import make_pdf

    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "bank@bank.example", "Выписка", "Во вложении выписка.",
                   attachments=[("выписка.pdf", "application/pdf", make_pdf(["Balance 1200 EUR"])),
                                ("notes.txt", "text/plain", "итог: 1200".encode()),
                                ("run.exe", "application/x-msdownload", b"MZ")])
    fake.letter_in(2, "m2", "sale@shop.example", "Каталог", "x", attachments=[("cat.pdf", "application/pdf", b"%PDF")])
    verdicts = {"bank@bank.example": verdict("money", True), "sale@shop.example": verdict("promo", False)}

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, verdicts)
        await core.start([FakeChannel("telegram", False)])
        for n in range(3):
            await room.work(NOW + n)
        return core

    core = asyncio.run(run())
    assert fake.fetched == [("m1", 2), ("m1", 3), ("m1", 4)], "junk attachments are never downloaded"
    event = core.archive.get(f"mail:{ACCOUNT}/m1")
    files = core.archive.attachments_of(event.id)
    assert [(a.what, a.origin) for a in files] == [("PDF «выписка.pdf», 1 стр.", "из письма bank@bank.example"),
                                                   ("файл «notes.txt»", "из письма bank@bank.example")]
    assert "Balance 1200 EUR" in event.text and "итог: 1200" in event.text and "[не прочитано: «run.exe»" in event.text
    assert core.archive.read(files[0])[0][0] == "application/pdf", "the PDF itself is kept for her"


def test_live_mail_first_the_backfill_paced_and_nothing_while_the_window_is_spent(tmp_path):
    fake, asked = FakeCollector(), []
    for n in range(1, 4):
        fake.letter_in(n, f"b{n}", f"old{n}@x.example", "Старое", "x", backfill=True)
    fake.letter_in(4, "l4", "new@x.example", "Новое", "x")
    verdicts = {f"old{n}@x.example": verdict("person", True) for n in range(1, 4)}
    verdicts["new@x.example"] = verdict("person", True)

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, verdicts)
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)          # the live one first
        await room.work(NOW + 1)      # a backfill letter
        await room.work(NOW + 2)      # too soon for the next backfill letter
        core.store.run(agent_id="assistant", conversation_id="c", kind="conversation", status="done",
                       meta={"rate_limit": {"status": "allowed_warning", "rate_limit_type": "five_hour",
                                            "utilization": 0.85, "resets_at": NOW + 3600}})
        await room.work(NOW + 100)    # the window is nearly spent: the rest is the conversation's
        await room.work(NOW + 3700)   # it reset: her judgement of the two kept ones comes first
        await room.work(NOW + 3701)
        core.limit_until = NOW + 9000
        await room.work(NOW + 3800)   # the limit is known to refuse: nothing starts
        return room

    room = asyncio.run(run())
    senders = [a["text"].split("От: ")[1].split("\n")[0] for a in asked if a["control"] == "bare"]
    assert senders == ["new <new@x.example>", "old1 <old1@x.example>", "old2 <old2@x.example>"]
    assert room.store.get(3)["state"] == "new"


def test_a_run_that_fails_tries_again_and_a_letter_she_never_read_is_kept(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "a@x.example", "Тема", "текст")
    fake.letter_in(2, "m2", "b@x.example", "Тема", "текст")
    verdicts = {"a@x.example": RuntimeError("model down"),
                "b@x.example": Reply("failed", "Лимит подписки исчерпан.", [], {"limit": True, "limit_until": NOW + 50})}

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, verdicts)
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)                       # a: fails, waits RETRY_S
        await room.work(NOW + 1)                   # b: the limit
        limited = core.limit_until
        await room.work(NOW + 30)                  # nothing while the limit is known
        await room.work(NOW + 700)                 # a again, b again
        await room.work(NOW + 701)
        await room.work(NOW + 1400)                # a: third failure — kept unread
        return core, room, limited

    core, room, limited = asyncio.run(run())
    assert limited == NOW + 50
    a = room.store.get(1)
    assert a["state"] == "kept" and a["card"]["read"] is False and a["card"]["keep"] is True
    assert room.store.get(2)["state"] == "new", "the limit keeps it waiting, it is not spent"
    assert core.archive.get(f"mail:{ACCOUNT}/m1") is not None


def test_a_calendar_change_is_on_record_and_an_invitation_goes_on(tmp_path):
    fake, asked = FakeCollector(), []
    invite = {"id": "e1", "status": "confirmed", "summary": "Интервью", "updated": "u1", "organizer": "hr@acme.example",
              "mine": False, "answer": "needsAction", "attendees": 2, "location": "", "description": "",
              "start": {"dateTime": "2026-10-10T12:00:00Z"}, "end": {"dateTime": "2026-10-10T13:00:00Z"}}
    fake.found += [{"seq": 1, "account": ACCOUNT, "source": "calendar", "ref": "c/e1/u1",
                    "data": {"calendar_name": "Мой", "change": "new", "event": invite, "first": False}},
                   {"seq": 2, "account": ACCOUNT, "source": "calendar", "ref": "c/e2/u2",
                    "data": {"calendar_name": "Мой", "change": "new", "first": False,
                             "event": {**invite, "id": "e2", "mine": True, "summary": "Своё"}}}]

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, {})
        await core.start([FakeChannel("telegram", False)])
        core.limit_until = NOW + 600  # the limit holds no item that needs no model
        await room.work(NOW)
        return core, room

    core, room = asyncio.run(run())
    assert asked == [], "a calendar change needs no triage"
    assert [room.store.get(n)["state"] for n in (1, 2)] == ["kept", "done"]
    event = core.archive.get(f"calendar:{ACCOUNT}/c/e1/u1")
    assert event.kind == "calendar" and event.conversation_id == f"calendar:{ACCOUNT}" and "Интервью" in event.text


def test_a_login_google_refused_is_told_at_once_and_once(tmp_path):
    fake, asked = FakeCollector(), []
    fake.sources = [{"account": ACCOUNT, "source": "mail", "since": NOW - 86400, "last_ok": NOW - 900,
                     "failing_since": NOW - 300, "error": "invalid_grant"}]

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, {})
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await room.work(NOW)
        await room.work(NOW + 120)
        return telegram

    telegram = asyncio.run(run())
    told = [c[0] for c in telegram.cards]
    assert len(told) == 1 and told[0].startswith(f"Почта {ACCOUNT}: Google больше не пускает сборщик (invalid_grant)")
    assert f"`uv run deploy/mail/login.py --account {ACCOUNT} --kind gmail`" in told[0]


def test_a_confirmation_that_fails_loses_nothing_and_keeps_nothing_twice(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "a@x.example", "Тема", "текст")
    fake.fail_confirm = True

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, {"a@x.example": verdict("person", True)})
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)                     # kept, the confirmation failed: the item stays at the collector
        fake.fail_confirm = False
        room.looked = 0
        await room.work(NOW + 120)
        return room

    room = asyncio.run(run())
    assert fake.confirmed == [1] and room.store.db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 1
    assert len(asked) == 1, "read once"


def test_the_router_steps_the_mail_one_piece_at_a_time(tmp_path):
    fake, asked = FakeCollector(), []

    class Slow(Mailroom):
        runs = 0

        async def work(self, now):
            Slow.runs += 1
            await asyncio.sleep(0.2)

    async def run():
        store = Store(str(tmp_path / "r.sqlite"))
        room = Slow(fake, MailStore(store.db))
        core = Core([AGENT], store, "owner", ask=triage_ask(asked, {}), mail=room)
        await core.start([FakeChannel("telegram", False)])
        await core.step(NOW)
        await core.step(NOW + 30)
        await drain()
        await core.step(NOW + 60)
        await drain()

    asyncio.run(run())
    assert Slow.runs == 2, "a second piece does not start while the first runs"


def test_found_mail_says_where_it_is_from(tmp_path):
    archive, store = Archive(":memory:"), Store(str(tmp_path / "r.sqlite"))
    archive.append("mail", "Письмо · a · входящее\nТема: билеты в Лиссабон", conversation_id="mail:a", channel="mail",
                   native_id="a/m1")

    async def run():
        core = Core([AGENT.__class__(**{**AGENT.__dict__, "archive": True})], store, "owner", archive=archive)
        await core.start([FakeChannel("telegram", False)])
        turn = core.turns.open_root("assistant")
        return await core.archive_search(core.agents["assistant"], turn.id, "Лиссабон")

    ok, text = asyncio.run(run())
    assert ok and "· Письмо · почта]" in text


class FakeMemory:
    """The base as the router's commit sees it: which run would commit, and with what mark of someone else's text."""

    def __init__(self):
        from types import SimpleNamespace

        self.settled, self.cfg = [], SimpleNamespace(digest_at="21:00")

    def sync(self):
        pass

    def settle(self, turn, kind, foreign):
        from retinue.memory import Settled

        self.settled.append((kind, foreign))
        return Settled(turn, kind, foreign)

    def record(self, settled):
        pass


def judging_ask(asked, verdicts, judged, core_of):
    """Triage by sender, as above; her judgement (`control="oneshot"`) answers `judged` — a dict or an exception."""
    triage = triage_ask(asked, verdicts)

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, attachments=None,
                       schema=None):
        if control != "oneshot":
            return await triage(url, text, context_id, on_progress, turn_id, control, attachments, schema)
        asked.append({"text": text, "context": context_id, "turn": turn_id, "control": control, "schema": schema,
                      "writer": core_of().writer, "files": [f.name for f in attachments or []]})
        answer = judged.pop(0) if isinstance(judged, list) else judged
        if isinstance(answer, Exception):
            raise answer
        return Reply("done", json.dumps(answer, ensure_ascii=False), [], {})
    return fake_ask


def decided(*items):
    return {"items": [{"n": n, "needs": item[0], "deadline": "", "cost": item[1], "new": True, "text": item[2],
                       "job": item[3] if len(item) > 3 else False} for n, item in enumerate(items, 1)]}


def judged_room(tmp_path, fake, asked, verdicts, judged):
    store = Store(str(tmp_path / "r.sqlite"))
    room, memory = Mailroom(fake, MailStore(store.db)), FakeMemory()
    holder = {}
    core = Core([AGENT], store, "owner", ask=judging_ask(asked, verdicts, judged, lambda: holder["core"]),
                archive=Archive(str(tmp_path / "a.sqlite")), mail=room, memory=memory)
    holder["core"] = core
    return core, room, memory


INVITE = {"id": "e1", "status": "confirmed", "summary": "Созвон", "updated": "u1", "organizer": "pm@acme.example",
          "mine": False, "answer": "needsAction", "attendees": 3, "location": "", "description": "",
          "start": {"dateTime": "2026-10-10T12:00:00Z"}, "end": {"dateTime": "2026-10-10T13:00:00Z"}}


def test_her_judgement_is_one_run_with_her_tools_and_code_decides_what_to_say(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "hr@acme.example", "Интервью", "Приглашаем на интервью в четверг. Подтвердите до среды.")
    fake.letter_in(2, "m2", "bank@bank.example", "Выписка", "Выписка за сентябрь.")
    fake.found.append({"seq": 3, "account": ACCOUNT, "source": "calendar", "ref": "c/e1/u1",
                       "data": {"calendar_name": "Мой", "change": "new", "event": INVITE, "first": False}})
    verdicts = {"hr@acme.example": verdict("job", True, "Подтвердите до среды."),
                "bank@bank.example": verdict("money", True)}
    said = "Анна из Acme зовёт на интервью в чт — подтверди до среды."
    judged = decided(("reply", "high", said), ("pay", "high", "В выписке долг."), ("nothing", "none", ""))

    async def run():
        core, room, memory = judged_room(tmp_path, fake, asked, verdicts, judged)
        room.store.rule("mute", "bank@bank.example", "money")  # his «не уведомлять о таком» beats her «high»
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        for at in (NOW, NOW + 1, NOW + 2, NOW + 100):
            await room.work(at)
        assert [a["control"] for a in asked] == ["bare", "bare"], "not before the glue: others may come with it"
        await room.work(NOW + 200)
        card = telegram.cards[-1]
        shown = await core.press(telegram, card[1][0][1])
        await drain()
        return core, room, memory, telegram, card, shown

    core, room, memory, telegram, card, shown = asyncio.run(run())
    judge = asked[-1]
    assert judge["control"] == "oneshot" and judge["context"] == "mail-1" and judge["turn"], "her tools, a fresh session"
    assert judge["writer"] == judge["turn"], "she may write to the base: the run holds the writer's queue"
    assert judge["schema"]["required"] == ["items"] and "--- 3." in judge["text"] and "--- 4." not in judge["text"]
    assert "Разбор: поиск работы; сводка; цитата: «Подтвердите до среды.»" in judge["text"]
    assert "Пригласил: pm@acme.example" in judge["text"] and "Приглашаем на интервью" in judge["text"]
    assert memory.settled == [("mail", ["calendar", "mail"])], "her commit carries the mark of someone else's text"
    assert card[0] == "Почта: 1 важное.", "the job search is secret: the preview says no more"
    assert [label for label, _ in card[1]] == ["Показать", "Не уведомлять о таком",
                                              "Всегда отсеивать hr@acme.example (1 за 30 дн)"]
    assert shown.toast == "Показываю" and telegram.cards[-1][0] == f"{said}\n_(письмо hr@acme.example, «Интервью»)_"
    assert [room.store.get(n)["state"] for n in (1, 2, 3)] == ["told", "later", "silent"]
    assert core.store.db.execute("SELECT kind, status FROM runs WHERE kind = 'mail'").fetchall() == [("mail", "done")]


def test_an_invitation_about_the_job_search_is_as_secret_as_a_letter(tmp_path):
    fake, asked = FakeCollector(), []
    fake.found.append({"seq": 1, "account": ACCOUNT, "source": "calendar", "ref": "c/e1/u1",
                       "data": {"calendar_name": "Мой", "change": "new", "event": INVITE, "first": False}})

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, {},
                                    decided(("attend", "high", "Acme зовёт на созвон в сб, 15:00.", True)))
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await room.work(NOW)
        await room.work(NOW + 200)
        return telegram

    telegram = asyncio.run(run())
    (card,) = telegram.cards
    assert card[0] == "Почта: 1 важное." and [label for label, _ in card[1]] == ["Показать"], \
        "no rule buttons under a calendar change: the rules are for letters"


def test_one_sender_in_an_hour_is_one_message_and_his_buttons_are_rules(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "anna@x.example", "Срочно", "Нужен ответ сегодня.")
    verdicts = {"anna@x.example": verdict("person", True), "ads@x.example": verdict("promo", False)}
    judged = [decided(("reply", "high", "Анна ждёт ответа сегодня.")), decided(("reply", "high", "Анна напоминает."))]

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, verdicts, judged)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await room.work(NOW)
        await room.work(NOW + 200)                       # told at once
        fake.letter_in(2, "m2", "anna@x.example", "Срочно 2", "Напоминаю.")
        await room.work(NOW + 300)
        await room.work(NOW + 500)                       # within the hour: held
        await room.work(NOW + 3000)
        cards = len(telegram.cards)
        await room.work(NOW + 3900)                      # the hour is over: one message for what was held
        first = telegram.cards[0]
        mute = await core.press(telegram, first[1][0][1])
        block = await core.press(telegram, first[1][1][1])
        fake.letter_in(3, "m3", "anna@x.example", "Срочно 3", "Ещё раз.")
        fake.letter_in(4, "m4", "ads@x.example", "Реклама", "x")
        for at in (NOW + 4000, NOW + 4001, NOW + 4300):
            await room.work(at)
        return room, telegram, cards, mute, block

    room, telegram, cards, mute, block = asyncio.run(run())
    assert cards == 1 and telegram.cards[1][0] == ("Ещё 1 от anna@x.example за час:\n- Анна напоминает.\n"
                                                    "_(письмо anna@x.example, «Срочно 2»)_")
    assert mute.toast.startswith("Сразу о таком от anna@x.example больше не пишу")
    assert block.toast == "Письма anna@x.example отсеиваются: ни в архив, ни ассистентке."
    assert room.store.get(3)["state"] == "blocked", "his block: the next letter is dropped by code, unread"
    assert len(telegram.cards) == 2 and room.store.rules() == [("mute", "anna@x.example", "person"),
                                                               ("block", "anna@x.example", "")]


def test_a_judgement_that_fails_is_tried_again_then_left_for_the_summary(tmp_path):
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "a@x.example", "Тема", "текст")

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, {"a@x.example": verdict("person", True)},
                                    [RuntimeError("down"), RuntimeError("down"), RuntimeError("down")])
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)
        for at in (NOW + 200, NOW + 600, NOW + 1000):
            await room.work(at)
        return room

    room = asyncio.run(run())
    assert room.store.get(1)["state"] == "later" and len([a for a in asked if a["control"] == "oneshot"]) == 3


def test_the_owner_sees_his_mailboxes_and_rules_and_takes_a_rule_back(tmp_path):
    fake, asked = FakeCollector(), []
    fake.sources = [{"account": ACCOUNT, "source": "mail", "since": NOW - 86400, "last_ok": NOW - 240,
                     "failing_since": None, "error": None},
                    {"account": "b@example.org", "source": "calendar", "since": None, "last_ok": None,
                     "failing_since": NOW - 60, "error": "invalid_grant"}]

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, {}, [])
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await room.collect(NOW)
        room.store.rule("block", "ads@x.example")
        room.store.rule("mute", "hr@acme.example", "job")
        await core.handle(telegram, None, "!mail")
        text, buttons = telegram.cards[-1][0], telegram.cards[-1][1]
        back = await core.press(telegram, buttons[0][1])
        return text, buttons, back, room.store.rules()

    text, buttons, back, rules = asyncio.run(run())
    assert "- почта owner@example.org: синхронизирована" in text
    assert "- календарь b@example.org: нужен вход заново: `uv run deploy/mail/login.py --account b@example.org " \
           "--kind calendar`" in text
    assert "- отсеивать ads@x.example" in text and "- не уведомлять сразу: hr@acme.example, поиск работы" in text
    assert [label for label, _ in buttons] == ["Вернуть ads@x.example", "Уведомлять: hr@acme.example"]
    assert back.toast == "Правило снято." and rules == [("mute", "hr@acme.example", "job")]

    async def plain():
        core = Core([AGENT], Store(str(tmp_path / "p.sqlite")), "owner")
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await core.handle(telegram, None, "!mail")
        return telegram.cards[-1][0]

    assert asyncio.run(plain()).startswith("Почта не подключена"), "without the collector: said so, not the help"


def test_the_morning_summary_has_the_day_and_the_mail_that_waited_and_the_health_line_counts_it(tmp_path):
    """09:00: the day's calendar with the preparation she makes from the base, the letters she judged could wait;
    the health line — «почта N из N» with each mailbox's age, what was kept and dropped. Her run fails — code's
    summary still names the day and the mail."""
    from retinue.scheduler import SUMMARY, Job

    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "bank@bank.example", "Выписка", "Выписка за сентябрь.")
    fake.letter_in(2, "m2", "sale@shop.example", "Скидки", "x")
    fake.sources = [{"account": ACCOUNT, "source": "mail", "since": NOW - 30 * 86400, "last_ok": NOW - 240,
                     "failing_since": None, "error": None},
                    {"account": "b@example.org", "source": "mail", "since": NOW - 30 * 86400, "last_ok": NOW - 7200,
                     "failing_since": NOW - 3000, "error": "invalid_grant"},
                    {"account": ACCOUNT, "source": "calendar", "since": NOW, "last_ok": NOW - 300,
                     "failing_since": None, "error": None}]
    meeting = {**INVITE, "summary": "Собеседование в Acme", "location": "Zoom",
               "start": {"dateTime": "2025-10-09T12:00:00Z"}, "end": {"dateTime": "2025-10-09T13:00:00Z"}}

    async def agenda(start, end):
        return [meeting], ["c@example.org: нужен вход заново"]

    fake.agenda = agenda
    verdicts = {"bank@bank.example": verdict("money", True), "sale@shop.example": verdict("promo", False)}
    judged = [decided(("read", "low", "Пришла выписка за сентябрь.")), RuntimeError("down")]
    summaries = []

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, verdicts, judged)
        inner = core.ask

        async def ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
            if text.startswith("[Утренняя сводка"):
                summaries.append(text)
                if len(summaries) == 2:
                    raise RuntimeError("down")
                return Reply("done", "Доброе утро.", [], {})
            return await inner(url, text, context_id, on_progress, turn_id, control, **extra)

        core.ask = ask
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        for at in (NOW, NOW + 1, NOW + 200):
            await room.work(at)
        await core.summary(Job(1, SUMMARY, "", "2025-10-09T09:00", "Europe/Moscow", NOW + 300, "running", {}), NOW + 300)
        fake.letter_in(3, "m3", "bank@bank.example", "Ещё выписка", "x")
        room.looked = 0
        for at in (NOW + 400, NOW + 700):
            await room.work(at)
        room.store.set(3, NOW + 700, state="later", card={"text": "Вторая выписка."})
        await core.summary(Job(2, SUMMARY, "", "2025-10-10T09:00", "Europe/Moscow", NOW + 900, "running", {}), NOW + 900)
        return room, telegram

    room, telegram = asyncio.run(run())
    first = summaries[0]
    assert "Календарь на сегодня:\n- чт 9 октября, 15:00–16:00 МСК — Собеседование в Acme, Zoom (пригласил " \
           "pm@acme.example; Владелец: не ответил)\n- не прочитан: c@example.org: нужен вход заново" in first
    assert "подготовь из базы" in first and "Почта, которая ждала утра:\n- Пришла выписка за сентябрь. " \
           "_(письмо bank@bank.example, «Выписка»)_" in first
    assert room.store.get(1)["state"] == "done", "named in a summary once"
    assert "Пришла выписка" not in summaries[1] and "Вторая выписка" in summaries[1]
    cards = [c[0] for c in telegram.cards if "Здоровье за сутки" in c[0]]
    health = cards[0].split("Здоровье за сутки: ")[1]
    assert "почта 1 из 2 (b@example.org — нужен вход заново; owner@example.org — 9 мин назад)" in health
    assert "календарь 1 из 1 (owner@example.org — 10 мин назад)" in health
    assert "писем: оставлено 1, отсеяно 1 (реклама 1)" in health
    bare = cards[1]
    assert bare.startswith("Утренняя сводка — без ассистентки") and "Собеседование в Acme" in bare
    assert "Вторая выписка." in bare, "code names the waiting mail when she cannot"


def test_an_empty_search_names_every_source_and_its_gaps(tmp_path):
    fake, asked = FakeCollector(), []
    fake.sources = [{"account": ACCOUNT, "source": "mail", "since": NOW - 30 * 86400, "last_ok": NOW - 240,
                     "failing_since": NOW - 100, "error": "invalid_grant"}]

    async def run():
        core, room, _ = judged_room(tmp_path, fake, asked, {}, [])
        core.agents["assistant"].archive = True
        await core.start([FakeChannel("telegram", False)])
        await room.collect(NOW)
        turn = core.turns.open_root("assistant")
        return await core.archive_search(core.agents["assistant"], turn.id, "Лиссабон")

    try:
        ok, text = asyncio.run(run())
    finally:
        AGENT.archive = False
    assert ok and text.startswith("По запросу «Лиссабон» ничего не найдено. Что архив покрывает:")
    assert "- почта owner@example.org: с 2025-09-09 11:53 МСК, вторник, последняя синхронизация " in text
    assert "пробел: не собирается с 2025-10-09 11:51 МСК, четверг (нужен вход заново)" in text
    assert "отсеянные письма в архив не попадают" in text and "файлы на Mac" in text


def test_a_kept_letter_keeps_what_a_reply_needs(tmp_path):
    """From now on the archive keeps the recipients, Reply-To and References of every letter: the courier takes the
    address and the thread from there, never from the model."""
    fake, asked = FakeCollector(), []
    fake.letter_in(1, "m1", "hr@acme.example", "Интервью", "Удобно в четверг?")
    fake.letters["m1"].update({"cc": ["boss@acme.example"], "reply_to": "Jobs@Acme.example",
                               "references": ["<m0@x>"], "in_reply_to": "<m0@x>"})

    async def run():
        core, room = mailroom_with(tmp_path, fake, asked, {"hr@acme.example": verdict("job", True)})
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)
        return core

    core = asyncio.run(run())
    meta = core.archive.get(f"mail:{ACCOUNT}/m1").meta
    assert (meta["to"], meta["cc"], meta["reply_to"]) == ([ACCOUNT], ["boss@acme.example"], "jobs@acme.example")
    assert meta["references"] == ["<m0@x>"] and meta["message_id"] == "<m1@x>" and meta["thread"] == "t"


class FakeGateway:
    """The Telegram Business gateway's answers from memory: items of the owner's chosen chats, its status."""

    def __init__(self):
        self.found, self.confirmed, self.rights = [], [], ["can_reply"]
        self.files, self.fetched = {}, []

    async def file(self, file_id):
        self.fetched.append(file_id)
        return self.files[file_id]

    def message(self, seq, mid, text, direction="in", chat="777", edit=0, **extra):
        self.found.append({"seq": seq, "account": "telegram", "source": "telegram",
                           "ref": f"{chat}/{mid}" + (f"/e{edit}" if edit else ""),
                           "data": {"chat": chat, "message_id": mid, "date": int(NOW) - 60, "edit": edit,
                                    "direction": direction, "username": "anna_x", "name": "Анна", "text": text,
                                    "media": "", "reply_to": 0, "offline": False, **extra}})

    async def items(self):
        return [i for i in self.found if i["seq"] > (max(self.confirmed) if self.confirmed else 0)]

    async def confirm(self, upto):
        self.confirmed.append(upto)

    async def status(self):
        return {"connected": True, "rights": self.rights, "seen": NOW, "day": 0}


def chat_room(tmp_path, gateway, asked, judged):
    store = Store(str(tmp_path / "r.sqlite"))
    room, holder = Mailroom(None, MailStore(store.db), gateway=gateway), {}
    core = Core([AGENT], store, "owner", ask=judging_ask(asked, {}, judged, lambda: holder["core"]),
                archive=Archive(str(tmp_path / "a.sqlite")), mail=room, memory=FakeMemory())
    holder["core"] = core
    return core, room


def test_the_owners_chosen_chats_are_kept_like_mail_and_the_other_persons_words_are_judged(tmp_path):
    """Every message of a chosen chat is on record verbatim; his own and a reply the courier sent are his; the other
    person's is judged like a kept letter, with no triage, and code decides when to tell — about the job search, no
    more than «Telegram: 1 важное». Extra rights of the bot are told once."""
    gateway, asked = FakeGateway(), []
    gateway.rights = ["can_delete_all_messages", "can_reply"]
    gateway.message(1, 10, "Добрый день! Готовы обсудить оффер завтра?")
    gateway.message(2, 11, "Да, давайте в 11.", direction="own")
    gateway.message(3, 12, "[текст ответа]", direction="bot")
    gateway.message(4, 10, "Добрый день! Готовы обсудить оффер завтра в 11?", edit=int(NOW))
    gateway.message(5, 13, "", media="voice")

    async def run():
        core, room = chat_room(tmp_path, gateway, asked, decided(("reply", "high", "Рекрутёр ждёт ответа.", True),
                                                                 ("read", "low", "Голосовое.")))
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await room.work(NOW)
        await room.work(NOW + 200)
        gateway.found.clear()
        await room.work(NOW + 300)
        return core, room, telegram

    core, room, telegram = asyncio.run(run())
    assert gateway.confirmed == [5]
    events = core.archive.db.execute("SELECT id, kind, conversation_id, text FROM events WHERE kind = 'chat' "
                                     "ORDER BY seq").fetchall()
    assert [e[0] for e in events] == ["tgb:777/10", "tgb:777/11", "tgb:777/12", f"tgb:777/10/e{int(NOW)}",
                                      "tgb:777/13"] and {e[2] for e in events} == {"chat:777"}
    assert events[0][3] == "Telegram · Анна (@anna_x) · входящее\nДобрый день! Готовы обсудить оффер завтра?"
    assert events[2][3].startswith("Telegram · Анна (@anna_x) · ответ Владельца, отправленный по его карточке")
    assert events[3][3].startswith("Telegram · Анна (@anna_x) · входящее · изменено")
    assert events[4][3] == "Telegram · Анна (@anna_x) · входящее\n[голосовое — не загружалось]"
    assert [room.store.get(n)["state"] for n in range(1, 6)] == ["told", "own", "own", "done", "later"]
    judge = asked[-1]
    assert judge["control"] == "oneshot" and "Добрый день! Готовы обсудить оффер завтра?" in judge["text"]
    assert judge["text"].startswith("[Почта, календарь и Telegram.") and "--- 2." in judge["text"]
    rights, told = telegram.cards[0][0], telegram.cards[1][0]
    assert rights.startswith("У Business-бота лишние права: can_delete_all_messages.") and len(telegram.cards) == 2
    assert told == "Telegram: 1 важное.", "the job search is secret in Telegram too"
    meta = core.archive.get("tgb:777/10").meta
    assert (meta["chat"], meta["message_id"], meta["direction"], meta["username"]) == ("777", 10, "in", "anna_x")
    assert any("Telegram: только чаты" in line for line in room.coverage(NOW))
    assert "telegram: бот подключён" in core.health(NOW)


def test_the_other_persons_files_are_read_like_attachments_and_her_judgement_sees_the_exchange(tmp_path):
    """A photo, a voice note, a document from the other person: downloaded through the gateway, read by the same
    worker and speech to text as the owner's own files, kept with the message as someone else's — her judgement gets
    them as it gets a letter's. Over 20 MB: said, not downloaded. His own files: named only. Each new message comes
    with the earlier ones of the same chat."""
    from test_attachments import FakeScribe, picture

    gateway, asked = FakeGateway(), []
    gateway.files = {"p1": picture(64, 48), "v1": b"OggS-voice"}
    gateway.message(1, 10, "Вот фото квартиры.", file={"kind": "photo", "id": "p1", "name": "", "type": "",
                                                        "size": 900, "duration": 0})
    gateway.message(2, 11, "", file={"kind": "voice", "id": "v1", "name": "", "type": "audio/ogg", "size": 10,
                                     "duration": 7})
    gateway.message(3, 12, "И договор.", file={"kind": "document", "id": "d1", "name": "dogovor.pdf",
                                               "type": "application/pdf", "size": 25 * 2**20, "duration": 0})
    gateway.message(4, 13, "Моё фото", direction="own", media="photo")

    async def run():
        core, room = chat_room(tmp_path, gateway, asked, decided(*[("read", "low", "Анна прислала квартиру.")] * 3))
        core.scribe = FakeScribe("Перезвоню вечером, обсудим цену.")
        await core.start([FakeChannel("telegram", False)])
        await room.work(NOW)
        await room.work(NOW + 200)
        return core

    core = asyncio.run(run())
    photo, voice, contract, mine = (core.archive.get(f"tgb:777/{m}") for m in (10, 11, 12, 13))
    marks = [a.mark() for e in (photo, voice) for a in core.archive.attachments_of(e.id)]
    assert marks == ["[вложение #1: фото 64×48 · чужое: Telegram, Анна (@anna_x)]",
                     "[вложение #2: голосовое 0:07 · чужое: Telegram, Анна (@anna_x)]"]
    assert photo.text.endswith("Вот фото квартиры.") and photo.meta["attachments"] == [1]
    assert "Перезвоню вечером, обсудим цену." in voice.text, "voice: the transcript, as the owner's own"
    assert "Telegram отдаёт ботам файлы до 20 МБ" in contract.text and "[не прочитано:" in contract.text
    assert "[фото — не загружалось]" in mine.text and gateway.fetched == ["p1", "v1"], "too big and his own: no"
    judge = asked[-1]
    assert judge["files"] == ["вложение #1: фото 64×48 · чужое: Telegram, Анна (@anna_x)"], "she sees the photo"
    assert "Раньше в этой переписке — 1 последних, старые сверху:" in judge["text"] and "Новое:" in judge["text"]
