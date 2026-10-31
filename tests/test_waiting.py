"""What waits for the owner in the assistant's world, as the router reads it from its own tables for the digest
(`waiting.digest`): people she judged to ask something of him, drafts waiting for «Send», the base's near
deadlines — with no address, no letter's text, and the owner's private matter named by a count only."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from types import SimpleNamespace

from retinue import feed, waiting
from retinue.mail import MailStore
from retinue.outbox import OutboxStore
from retinue.proactive import Deadline
from retinue.protocol import Store

NOW = 1_760_000_000.0  # 2025-10-09, a Thursday, 08:53 UTC
HOUR, DAY = 3600, 86400


def core(tmp_path, scanned=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(str(tmp_path / "r.sqlite"))
    return SimpleNamespace(mail=SimpleNamespace(store=MailStore(store.db)), outbox=SimpleNamespace(
        store=OutboxStore(store.db)), proactive=SimpleNamespace(scanned=scanned), tz="UTC", store=store,
        memory=SimpleNamespace(cfg=SimpleNamespace(private={"sphere": "home"})))


def letters(mail: MailStore, *rows):
    mail.keep([{"seq": n, "account": "me@example.com", "source": source, "ref": f"m{n}", "data": data}
               for n, (source, data, *_) in enumerate(rows, 1)], NOW - DAY)
    for n, (_, _, fields) in enumerate(rows, 1):
        mail.set(n, NOW - HOUR, **fields)


def draft(outbox: OutboxStore, draft_id: str, **over):
    outbox.add({"id": draft_id, "created": NOW - 600, "state": "shown", "channel": "mail",
                "envelope": {"name": "Анна Петрова", "to": "anna@acme.example"}, "digest": "d", "reply_to": "",
                "thread": "", "private": 0, "head": [], "card": "", "expires": NOW + HOUR, "send_after": None, "at": NOW,
                **over})


def test_people_who_ask_something_come_once_each_without_an_address_or_their_words(tmp_path):
    c = core(tmp_path)
    letters(c.mail.store,
            ("mail", {"box": "INBOX"}, {"state": "later", "kind": "money", "sender": "bank@bank.example",
                                        "ts": NOW - 2 * DAY, "event_id": "e1",
                                        "card": {"who": "Банк (no-reply@bank.example)", "text": "Подтвердить адрес.",
                                                 "needs_now": "act", "deadline_now": "пт"}}),
            ("mail", {"box": "INBOX"}, {"state": "told", "kind": "private", "sender": "desk@acme.example",
                                        "ts": NOW - DAY, "card": {"text": "Клиника Acme ждёт в среду",
                                                                  "needs_now": "reply"}}),
            ("mail", {"box": "INBOX"}, {"state": "later", "kind": "service", "sender": "shop@x.example",
                                        "card": {"text": "Заказ доставлен.", "needs_now": "read"}}),
            ("telegram", {"name": "Мария"}, {"state": "later", "kind": "chat", "sender": "tg:7", "ts": NOW - 3 * HOUR,
                                             "card": {"text": "Нужен ли трансфер?", "needs_now": "reply"}}),
            ("telegram", {"name": "Мария"}, {"state": "later", "kind": "chat", "sender": "tg:7", "ts": NOW - HOUR,
                                             "card": {"text": "И когда рейс?", "needs_now": "reply"}}),
            ("telegram", {"name": "Пётр"}, {"state": "later", "kind": "chat", "sender": "tg:8", "ts": NOW - 5 * HOUR,
                                            "card": {"text": "Скинешь фото?", "needs_now": "reply"}}),
            ("telegram", {}, {"state": "own", "kind": "chat", "sender": "tg:8", "ts": NOW - 4 * HOUR}))
    rows = waiting.digest(c, NOW)
    assert [(r["kind"], r["who"], r["title"]) for r in rows] == [
        ("letter", "Private", "1 private letter waits for a reply"),
        ("chat", "Мария", "reply: И когда рейс? (+1)"),
        ("letter", "Банк", "do: Подтвердить адрес. · deadline пт"),
    ], "reply before act; «read» only to know; Пётр was answered in his chat"
    assert rows[0]["sphere"] == "home" and rows[1]["since"] == NOW - 3 * HOUR
    said = json.dumps(rows, ensure_ascii=False)
    assert "@" not in said and "Acme" not in said and "tg:7" not in said, "no address, no private details, no handle"
    assert feed.clean({"items": rows}, NOW)[0], "the platform takes it as it is"
    assert waiting.digest(c, NOW + 60) == rows, "stable: the same digest is not pushed again"


def test_a_draft_waits_for_its_button_and_its_letter_is_not_counted_twice(tmp_path):
    c = core(tmp_path)
    letters(c.mail.store, ("mail", {"box": "INBOX"}, {"state": "later", "kind": "person", "sender": "anna@acme.example",
                                                      "event_id": "e7", "card": {"who": "Анна", "text": "Встретимся?",
                                                                                 "needs_now": "reply"}}))
    draft(c.outbox.store, "aaaa0001", reply_to="e7")
    draft(c.outbox.store, "aaaa0002", created=NOW - 500, state="link", channel="telegram",
          envelope={"name": "", "to": "777"})
    draft(c.outbox.store, "aaaa0003", created=NOW - 400, private=1)
    draft(c.outbox.store, "aaaa0004", expires=NOW - 1)
    draft(c.outbox.store, "aaaa0005", state="sent")
    rows = waiting.digest(c, NOW)
    assert [(r["id"], r["who"], r["title"], r["sphere"]) for r in rows] == [
        ("draft:aaaa0001", "Анна Петрова", waiting.DRAFT, None),
        ("draft:aaaa0002", "sender", waiting.LINK_DRAFT, None),
        ("draft:aaaa0003", "Private", waiting.DRAFT, "home"),
    ]


def test_the_bases_dates_are_those_the_heartbeat_pushes(tmp_path):
    today = date(2025, 10, 9)
    scanned = (NOW, today, [
        Deadline("Сдать отчёт", "work/journal/2025-10-01-report.md", "deadline", date(2025, 10, 11)),
        Deadline("Абонемент в спортзал", "personal/profile/gym.md", "until", date(2025, 10, 9)),
        Deadline("Правила спортзала", "personal/knowledge/gym.md", "stale_after", date(2025, 10, 10)),
        Deadline("Счёт", "personal/journal/2025-10-01-bill.md", "deadline", date(2025, 10, 8)),
        Deadline("Далеко", "work/journal/x.md", "deadline", date(2025, 10, 20)),
        Deadline("Абонемент в бассейн", "personal/knowledge/pool.md", "until", date(2025, 10, 10)),
    ], 0)
    rows = waiting.digest(core(tmp_path, scanned), NOW)
    assert [(r["title"], r["sphere"]) for r in rows] == [
        ("in 2 days: Сдать отчёт — deadline", "work"),
        ("today: Абонемент в спортзал — valid until", "personal"),
        ("overdue by 1 day: Счёт — deadline", "personal"),
    ]
    assert all(r["who"] == "The Base" and r["kind"] == "deadline" and r["since"] <= NOW for r in rows)
    assert waiting.digest(core(tmp_path / "none"), NOW) == [], "no reading of the base yet: nothing"


def test_the_week_ahead_goes_too_with_its_day_and_path_and_waits_only_from_the_push_rule(tmp_path):
    today = date(2025, 10, 9)
    scanned = (NOW, today, [
        Deadline("Сдать отчёт", "work/journal/2025-10-01-report.md", "deadline", date(2025, 10, 15)),
        Deadline("Паспорт", "personal/profile/passport.md", "until", date(2025, 10, 16)),
        Deadline("Восемь дней", "work/journal/x.md", "deadline", date(2025, 10, 17)),
        Deadline("Русское имя", "personal/journal/счёт.md", "deadline", date(2025, 10, 10)),
    ], 0)
    rows = waiting.digest(core(tmp_path, scanned), NOW)
    assert [(r["title"], r["when"], r.get("path")) for r in rows] == [
        ("in 6 days: Сдать отчёт — deadline", "2025-10-15", "work/journal/2025-10-01-report.md"),
        ("in 7 days: Паспорт — valid until", "2025-10-16", "personal/profile/passport.md"),
        ("tomorrow: Русское имя — deadline", "2025-10-10", None),
    ], "a path off the platform's rule is left out, the row is not"
    assert [r["since"] > NOW for r in rows] == [True, True, False], "waits from three days before"
    items, _ = feed.clean({"items": rows}, NOW)
    assert len(items) == 3, "the platform takes what the router sends"


def test_the_hubs_chips_cross_without_the_builds_age(tmp_path):
    c = core(tmp_path)
    assert waiting.chips(c, NOW) == [], "no hub here: no chips"
    c.lifehub = SimpleNamespace(chips=lambda now: [
        {"key": "health", "text": "all working", "level": "ok"},
        {"key": "backup", "text": "backup 3 h ago", "level": "ok"},
        {"key": "build", "text": "hub built 4 min ago", "level": "ok"}])
    chips = waiting.chips(c, NOW)
    assert [c["text"] for c in chips] == ["all working", "backup 3 h ago", "hub built"]
    assert feed.clean_chips({"chips": chips}) == chips


def test_a_letter_a_sent_draft_answered_waits_no_more(tmp_path):
    c = core(tmp_path)
    letters(c.mail.store, ("mail", {"box": "INBOX"}, {"state": "later", "kind": "person", "sender": "anna@acme.example",
                                                      "event_id": "e7", "card": {"who": "Анна", "text": "Встретимся?",
                                                                                 "needs_now": "reply"}}))
    draft(c.outbox.store, "aaaa0001", reply_to="e7")
    assert [r["kind"] for r in waiting.digest(c, NOW)] == ["draft"]
    for state in ("queued", "sending", "sent"):
        c.outbox.store.db.execute("UPDATE drafts SET state = ? WHERE id = 'aaaa0001'", (state,))
        assert waiting.digest(c, NOW) == [], state
    c.outbox.store.db.execute("UPDATE drafts SET state = 'failed' WHERE id = 'aaaa0001'")
    assert [r["title"] for r in waiting.digest(c, NOW)] == ["reply: Встретимся?"], "not sent: it waits again"


def test_her_words_cross_short_with_codes_numbers_and_addresses_masked():
    text = ("Код 483920 для входа, счёт 12 500 руб., звонить +7 (912) 345-67-89, ivan＠acme.com, "
            "ссылка https://evil.example/x?a=1 и pay.example.com/r — до 2025 года")
    out = waiting.said(text)
    assert len(out) <= waiting.SAID_CHARS
    for leak in ("483920", "345-67-89", "acme", "evil", "pay.example", "2025"):
        assert leak not in out, leak
    assert out.startswith("Код •••• для входа, счёт 12 500 руб., звонить ‹number›, ‹address›")


def test_ids_are_keyed_by_the_routers_own_salt_and_the_digest_fits_the_platform(tmp_path):
    rows = [("mail", {"box": "INBOX"}, {"state": "later", "kind": "person", "sender": f"p{n}@x.example", "ts": NOW - n,
                                        "card": {"who": f"Человек {n}", "text": "Ответь " * 40, "needs_now": "reply"}})
            for n in range(80)]
    first, second = core(tmp_path / "a"), core(tmp_path / "b")
    letters(first.mail.store, *rows)
    letters(second.mail.store, *rows)
    ids = [r["id"] for r in waiting.digest(first, NOW)]
    assert ids == [r["id"] for r in waiting.digest(first, NOW)], "stable with its salt"
    assert ids[0] != waiting.digest(second, NOW)[0]["id"], "another router's salt: another id"
    assert hashlib.sha256(b"mail:p0@x.example").hexdigest()[:12] not in ids[0] + ids[-1]
    assert len(ids) == feed.DIGEST_ITEMS, "the most pressing fifty"
    assert len(json.dumps({"items": waiting.digest(first, NOW)}, ensure_ascii=False).encode()) <= feed.DIGEST_BYTES
    assert feed.clean({"items": waiting.digest(first, NOW)}, NOW)
