"""What waits for the owner in the assistant's world, as the router reads it from its own tables for the digest
(`waiting.digest`): people she judged to ask something of him, drafts waiting for «Send», the base's near
deadlines — with no address, no letter's text, and the owner's private matter named by a count only."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from types import SimpleNamespace

import pytest

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
        ("letter", "acme.example", "reply: Клиника Acme ждёт в среду"),
        ("chat", "Мария", "reply: И когда рейс? (+1)"),
        ("letter", "Банк", "do: Подтвердить адрес. · deadline пт"),
    ], "reply before act; «read» only to know; Пётр was answered in his chat"
    assert rows[0]["sphere"] == "home" and rows[0]["private"] is True and rows[1]["since"] == NOW - 3 * HOUR
    assert "private" not in rows[1] and "private" not in rows[2]
    said = json.dumps(rows, ensure_ascii=False)
    assert "@" not in said and "tg:7" not in said, "no address, no handle"
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


def test_the_chips_cross_without_the_builds_age(tmp_path):
    c = core(tmp_path)
    assert waiting.chips(c, NOW) == [], "no pages here: no chips"
    c.pages = SimpleNamespace(chips=lambda now: [
        {"key": "health", "text": "all working", "level": "ok"},
        {"key": "backup", "text": "backup 3 h ago", "level": "ok"},
        {"key": "build", "text": "pages built 4 min ago", "level": "ok"}])
    chips = waiting.chips(c, NOW)
    assert [c["text"] for c in chips] == ["all working", "backup 3 h ago", "pages built"]
    assert feed.clean_chips({"chips": chips}) == chips
    c.pages = SimpleNamespace(chips=lambda now: [{"key": "build", "text": "pages not updated: disk full", "level": "bad"}])
    assert [c["text"] for c in waiting.chips(c, NOW)] == ["pages not built"]


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


# --- what closes an ask: his marks, his answers, its time ----------------------------------------------------------

def put(mail: MailStore, *rows: dict) -> list[int]:
    """Items as the mailroom leaves them: kept by the collector, then read (`fields`). Returns their numbers."""
    seqs = []
    for row in rows:
        ref = f"r{len(mail.db.execute('SELECT seq FROM mail_items').fetchall()) + 1}"
        mail.keep([{"seq": 1, "account": row.get("account", "me@example.com"), "source": row.get("source", "mail"),
                    "ref": ref, "data": row.get("data", {})}], NOW - DAY)
        seq = mail.db.execute("SELECT MAX(seq) FROM mail_items").fetchone()[0]
        mail.set(seq, row.get("at", NOW - HOUR), **row["fields"])
        seqs.append(seq)
    return seqs


def asking(need="reply", sender="anna@example.org", ts=NOW - 2 * HOUR, message_id="<m1@example.org>", **over) -> dict:
    """A letter of Анна Петрова that asks him something."""
    return {"data": {"box": "INBOX", "thread": "t1", "head": {"message_id": message_id, "reply_to": ""}}, **over,
            "fields": {"state": "later", "kind": "person", "sender": sender, "ts": ts, "event_id": f"e{message_id}",
                       "card": {"who": "Анна Петрова", "text": "Оплатить хостинг до пятницы.", "needs_now": need}}}


def his(to, ts, thread="t1", account="me@example.com", in_reply_to="", references=(), cc=()) -> dict:
    """His own letter, from Sent."""
    return {"account": account, "data": {"box": "SENT", "thread": thread, "head": {
        "message_id": f"<own{ts}@example.com>", "in_reply_to": in_reply_to, "references": list(references),
        "to": list(to), "cc": list(cc), "reply_to": ""}},
            "fields": {"state": "own", "kind": "own", "sender": account, "ts": ts}}


def opened(mail: MailStore, now: float = NOW) -> list[int]:
    return [item["seq"] for item in mail.asks(now)]


def test_his_letter_to_its_sender_answers_a_forward_to_somebody_else_does_not(tmp_path):
    mail = core(tmp_path).mail.store
    [ask] = put(mail, asking())
    put(mail, his(["olga@example.net"], NOW - 90 * 60), his(["olga@example.net"], NOW - 80 * 60, cc=["x@example.net"]))
    assert opened(mail) == [ask], "forwarded to a third in the same thread: not an answer"
    put(mail, his(["anna@example.org"], NOW - 3 * HOUR))
    assert opened(mail) == [ask], "his letter before hers answers nothing"
    put(mail, his(["anna@example.org"], NOW - HOUR, thread="t9", account="me2@example.net"))
    assert opened(mail) == [ask], "another mailbox, another thread, no reference: not this one"
    put(mail, his(["Anna@Example.org"], NOW - 30 * 60, thread="t9", account="me2@example.net",
                  in_reply_to="<m1@example.org>"))
    assert opened(mail) == [], "from his second mailbox, by In-Reply-To: the thread id is that mailbox's own"
    assert mail.ask_counts(NOW, NOW - DAY, NOW - 7 * DAY) == (1, 0)


def test_an_answer_to_its_reply_to_in_the_same_thread_closes_and_only_reply(tmp_path):
    mail = core(tmp_path).mail.store
    [ask] = put(mail, asking())
    [pay] = put(mail, asking("pay", message_id="<m2@example.org>"))
    mail.db.execute("UPDATE mail_items SET data = json_set(data, '$.head.reply_to', 'billing@example.org')")
    put(mail, his(["billing@example.org"], NOW - 30 * 60, references=["<m0@example.org>"]))
    assert opened(mail) == [pay] and ask != pay, "to its Reply-To in its thread; paying is not answered by a letter"


def test_a_draft_that_went_closes_reply_decide_act_but_not_pay_or_attend(tmp_path):
    mail = core(tmp_path).mail.store
    seqs = put(mail, *(asking(need, message_id=f"<{need}@example.org>") for need in ("reply", "decide", "act", "pay",
                                                                                       "attend")))
    sent = {f"e<{need}@example.org>": NOW - 10 * 60 for need in ("reply", "decide", "act", "pay", "attend")}
    assert [i["seq"] for i in mail.asks(NOW, sent)] == seqs[3:]


def test_vernut_takes_back_his_last_press_only_and_an_older_answer_stays_an_answer(tmp_path):
    mail = core(tmp_path).mail.store
    [ask, other] = put(mail, asking(), asking("decide", message_id="<m2@example.org>"))
    put(mail, his(["anna@example.org"], NOW - 30 * 60))
    assert opened(mail) == [other], "he answered the one"
    mail.mark([ask, other], "done", NOW)
    assert opened(mail) == []
    assert mail.mark([ask, other], "open", NOW + 60) == 2
    assert opened(mail, NOW + 120) == [other], "«Undo» after «Done»: what his letter answered stays answered"
    assert mail.ask_counts(NOW + 120, NOW - DAY, NOW - 7 * DAY) == (1, 0)
    mail.mark([other], "later", NOW + 180, until=NOW + 12 * HOUR)
    assert opened(mail, NOW + HOUR) == [], "put off till the morning"
    mail.mark([other], "open", NOW + 2 * HOUR)
    assert opened(mail, NOW + 3 * HOUR) == [other], "«Undo» after «Tomorrow»: back now"
    assert mail.mark([other], "open", NOW + 4 * HOUR) == 0, "nothing left to take back"
    mail.mark([other], "done", NOW + 16 * HOUR)
    assert mail.mark([other], "done", NOW + 17 * HOUR) == 0, "the same press twice is one"


def test_an_answer_older_than_zavtra_does_not_close_it_in_the_morning(tmp_path):
    mail = core(tmp_path).mail.store
    [ask] = put(mail, asking())
    mail.mark([ask], "later", NOW - 50 * 60, until=NOW + 12 * HOUR)  # pressed on a list shown before his answer
    put(mail, his(["anna@example.org"], NOW - 55 * 60))
    assert opened(mail, NOW + 13 * HOUR) == [ask], "his press is newer than that answer"
    put(mail, his(["anna@example.org"], NOW + 14 * HOUR))
    assert opened(mail, NOW + 15 * HOUR) == [], "a new answer closes it"


def test_a_chat_message_after_it_answers_a_reply_and_vernut_does_not_undo_it(tmp_path):
    mail = core(tmp_path).mail.store
    [ask] = put(mail, {"source": "telegram", "data": {"name": "Анна Петрова"},
                       "fields": {"state": "later", "kind": "chat", "sender": "tg:7", "ts": NOW - 2 * HOUR,
                                  "card": {"text": "Скинешь фото?", "needs_now": "reply"}}})
    put(mail, {"source": "telegram", "fields": {"state": "own", "kind": "chat", "sender": "tg:7", "ts": NOW - HOUR}})
    assert opened(mail) == []
    mail.mark([ask], "dismissed", NOW)
    mail.mark([ask], "open", NOW + 60)
    assert opened(mail, NOW + 120) == [], "«Not a task» taken back: his message still answered it"


def test_an_ask_runs_out_in_7_or_14_days_from_her_judgement_and_a_press_gives_it_7_more(tmp_path):
    mail = core(tmp_path).mail.store
    reply, pay, attend, kept = put(mail, asking(), asking("pay", message_id="<m2@example.org>"),
                                   asking("attend", message_id="<m3@example.org>"),
                                   asking(message_id="<m4@example.org>"))
    judged = NOW - HOUR
    assert opened(mail, judged + 7 * DAY - 1) == [reply, pay, attend, kept]
    assert opened(mail, judged + 7 * DAY) == [pay, attend], "«reply»: 7 days"
    assert opened(mail, judged + 14 * DAY) == [], "«pay», «attend»: 14"
    assert mail.ask_counts(judged + 14 * DAY, judged, judged + 7 * DAY + 1) == (0, 2), "two in the last week"

    mail.mark([kept], "later", judged + 5 * DAY, until=judged + 6 * DAY)
    assert opened(mail, judged + 13 * DAY - 1) == [pay, attend, kept], "7 days from the end of his «Tomorrow»"
    assert opened(mail, judged + 13 * DAY) == [pay, attend]
    mail.mark([reply], "later", NOW, until=NOW + DAY)
    assert reply not in opened(mail, NOW + DAY - 1)
    assert reply in opened(mail, NOW + 8 * DAY - 1) and reply not in opened(mail, NOW + 8 * DAY), \
        "«Tomorrow»: 7 days from its morning"


def test_its_time_runs_from_her_judgement_not_from_a_later_state_change(tmp_path):
    mail = core(tmp_path).mail.store
    judged = asking()
    judged["fields"]["card"]["judged"] = NOW - 6 * DAY  # «at» moved since: the morning summary, the hour's glue
    [ask] = put(mail, judged)
    assert opened(mail, NOW + DAY - 1) == [ask] and opened(mail, NOW + DAY) == []


def test_zavtra_is_tomorrow_at_nine_in_his_zone():
    from retinue.mail import later_until

    assert later_until(NOW, "UTC") == 1_760_086_800.0, "2025-10-10 09:00 UTC"
    assert later_until(NOW, "Asia/Tokyo") == 1_760_054_400.0, "17:53 there already: 09:00 of its next day"


def test_a_row_is_a_person_and_an_ask_and_a_press_closes_exactly_its_items(tmp_path):
    c = core(tmp_path)
    pay, decide, decide2 = put(c.mail.store, asking("pay"), asking("decide", message_id="<m2@example.org>"),
                               asking("decide", message_id="<m3@example.org>", ts=NOW - 3 * HOUR))
    rows = waiting.digest(c, NOW)
    assert [(r["who"], r["title"].split(":")[0]) for r in rows] == [("Анна Петрова", "decide"),
                                                                     ("Анна Петрова", "pay")]
    assert len({r["id"] for r in rows}) == 2 and all(len(r["t"]) == 32 for r in rows)
    assert set(rows[0]) == {"id", "kind", "who", "title", "since", "sphere", "t"}, "no item's number leaves"
    assert feed.clean({"items": rows}, NOW)[0]
    assert waiting.press(c, rows[0]["t"], "done", NOW)
    assert opened(c.mail.store) == [pay], "«decide» closed, both its letters; «pay» stays"
    assert waiting.counts(c, NOW + 60) == {"closed_today": 1, "expired_week": 0}, "one row closed, of two letters"
    assert [r["t"] for r in waiting.digest(c, NOW + 60)] == [rows[1]["t"]]
    assert waiting.press(c, rows[0]["t"], "open", NOW + 120), "«Undo» by the same token, its row gone"
    assert sorted(opened(c.mail.store, NOW + 180)) == sorted([pay, decide, decide2])


def test_a_token_names_the_items_it_was_made_of_across_a_restart_and_a_refused_digest(tmp_path):
    c = core(tmp_path)
    [first] = put(c.mail.store, asking())
    [old] = waiting.digest(c, NOW)  # the platform took this one; the next one it refused
    [second] = put(c.mail.store, asking(message_id="<m2@example.org>", ts=NOW - 30 * 60))
    [new] = waiting.digest(c, NOW + 30)
    assert new["t"] != old["t"] and new["id"] == old["id"], "the same row, other items: another token"

    restarted = core(tmp_path)  # the same database, a new process: nothing kept in memory
    assert waiting.press(restarted, old["t"], "done", NOW + 60)
    assert opened(restarted.mail.store, NOW + 60) == [second], "the press names what he saw, not what came after"
    other = core(tmp_path / "other")
    put(other.mail.store, asking())
    assert not waiting.press(other, old["t"], "done", NOW + 60), "another router's token names nothing"
    assert len(opened(other.mail.store, NOW + 60)) == 1 and first not in opened(restarted.mail.store, NOW + 60)


def test_an_unknown_or_outdated_token_applies_nothing(tmp_path):
    c = core(tmp_path)
    [ask] = put(c.mail.store, asking())
    [row] = waiting.digest(c, NOW)
    assert not waiting.press(c, "0" * 32, "done", NOW)
    with pytest.raises(ValueError):
        waiting.press(c, row["t"], "delete", NOW)  # not one of his buttons: a fault, not an old list
    assert not waiting.press(c, row["t"], "done", NOW + waiting.ROW_S + 1), "a list older than a day"
    assert opened(c.mail.store) == [ask]


def test_a_private_letter_is_a_row_of_its_own_named_in_the_app_and_closed_on_its_own(tmp_path):
    c = core(tmp_path)
    attend, pay = put(c.mail.store, *({**asking(need, message_id=f"<{need}@example.org>", ts=NOW - n * HOUR),
                                       "fields": {**asking(need, ts=NOW - n * HOUR)["fields"], "kind": "private",
                                                  "event_id": need}}
                                      for n, need in ((2, "attend"), (1, "pay"))))
    rows = waiting.digest(c, NOW)
    assert [(r["who"], r["title"].split(":")[0], r["sphere"], r["private"]) for r in rows] == \
        [("Анна Петрова", "pay", "home", True), ("Анна Петрова", "attend", "home", True)], "one row per ask, as any"
    assert all(r["private"] is True for r in feed.clean({"items": rows}, NOW)[0])
    sheet = waiting.detail(c, rows[0]["id"], rows[0]["t"], NOW)
    assert sheet["who"] == "Анна Петрова" and [a["need"] for a in sheet["asks"]] == ["pay"]
    assert feed.clean_detail(sheet) == sheet, "the platform takes it as it is"
    assert waiting.press(c, rows[0]["t"], "done", NOW), "the row's token closes its letter"
    assert opened(c.mail.store, NOW + 60) == [attend], "one letter closed, the other still asks"


def test_the_morning_says_what_closed_and_what_ran_out_only_when_something_did(tmp_path):
    from retinue.mailroom import Mailroom

    c = core(tmp_path)
    room = Mailroom(None, c.mail.store)
    room.core = c
    assert room.asked(NOW) == ""
    done, _, _ = put(c.mail.store, asking(), asking(message_id="<m2@example.org>", at=NOW - 8 * DAY),
                     asking(message_id="<m3@example.org>", at=NOW - 30 * DAY))
    c.mail.store.mark([done], "done", NOW - HOUR)
    assert room.asked(NOW) == "Ran out unmarked this week: 1; closed in the last day: 1.", "a month old: not counted"


def test_in_reply_to_and_references_match_by_id_whatever_is_around_it(tmp_path):
    mail = core(tmp_path).mail.store
    [ask, other] = put(mail, asking(message_id="<M1@Example.org>"), asking(message_id="<m2@example.org>"))
    put(mail, his(["anna@example.org"], NOW - 30 * 60, thread="t9", account="me2@example.net",
                  in_reply_to="<m1@example.org> (сообщение Анны Петровой)"))
    put(mail, his(["anna@example.org"], NOW - 20 * 60, thread="t9", account="me2@example.net",
                  references=["<m0@example.org> <m2@example.org>"]))
    assert opened(mail) == [] and ask != other


def test_a_draft_answers_from_when_it_was_sent_not_from_its_last_state_change(tmp_path):
    c = core(tmp_path)
    draft(c.outbox.store, "aaaa0001", reply_to="e7", state="sent", send_after=NOW - 300, at=NOW + HOUR)
    draft(c.outbox.store, "aaaa0002", reply_to="e8", state="queued", at=NOW)
    assert c.outbox.store.answered() == {"e7": NOW - 300, "e8": NOW - 600}, "the hold's end; without one, its making"


def test_letters_read_before_their_head_was_kept_get_it_from_the_archive(tmp_path):
    from retinue.archive import MAIL, Archive
    from retinue.mailroom import Mailroom

    c = core(tmp_path)
    c.archive = Archive(":memory:")
    ids = []
    for meta in ({"direction": "in", "message_id": "<m1@example.org>", "to": ["me@example.com"]},
                 {"direction": "out", "message_id": "<r1@example.com>", "in_reply_to": "<m1@example.org>",
                  "to": ["Anna@example.org"]}):
        event, _ = c.archive.append(MAIL, "письмо", conversation_id="mail:me@example.com", channel="mail",
                                    native_id=meta["message_id"], meta=meta)
        ids.append(event.id)
    [ask] = put(c.mail.store, {**asking(), "data": {"box": "INBOX", "thread": "t1"},
                               "fields": {**asking()["fields"], "event_id": ids[0]}})
    put(c.mail.store, {"account": "me2@example.net", "data": {"box": "SENT", "thread": "t9"},
                       "fields": {"state": "own", "kind": "own", "sender": "me2@example.net", "ts": NOW - 30 * 60,
                                  "event_id": ids[1]}})
    assert opened(c.mail.store) == [ask], "no head: his answer from the other mailbox is not seen"
    room = Mailroom(None, c.mail.store)
    room.core = c
    assert room.heads(NOW) == 2 and room.heads(NOW) == 0
    assert opened(c.mail.store) == [], "now it is"


# --- a row's sheet: asked by the app for one row, answered to that request only ------------------------------------

THREAD = "18c2f0a1b2c3d4e5"


def test_a_persons_sheet_says_her_words_masked_and_where_the_thread_is_never_the_letter(tmp_path):
    c = core(tmp_path)
    data = {"box": "INBOX", "thread": THREAD, "head": {"message_id": "<m1@example.org>", "reply_to": ""}}
    put(c.mail.store, asking("pay", data=data), asking("pay", message_id="<m2@example.org>", ts=NOW - HOUR, data={
        **data, "head": {"message_id": "<m2@example.org>", "reply_to": ""}}))
    c.mail.store.db.execute("UPDATE mail_items SET card = json_set(card, '$.text', ?, '$.quote', ?, "
                            "'$.deadline_now', ?) WHERE seq = 2",
                            ("Pay the invoice 12345678 at https://pay.example/x?t=1 or call +1 555 010 0199.\nBy Friday.",
                             "VERBATIM QUOTE", "Fri"))
    [row] = waiting.digest(c, NOW)
    sheet = waiting.detail(c, row["id"], row["t"], NOW)
    gmail = {"url": f"https://mail.google.com/mail/?authuser=me@example.com#all/{THREAD}", "label": "Open in Gmail"}
    assert sheet["kind"] == "letter" and sheet["who"] == "Анна Петрова" and sheet["open"] == gmail
    assert [a["at"] for a in sheet["asks"]] == [NOW - HOUR, NOW - 2 * HOUR], "newest first"
    assert sheet["asks"][0]["text"] == "Pay the invoice ‹number› at ‹link› or call ‹number›.\nBy Friday."
    assert sheet["asks"][0]["deadline"] == "Fri" and sheet["asks"][0]["open"] == gmail
    assert waiting.masked("1. 123\n2. 456", 100) == "1. 123\n2. 456", "two lines, two numbers"
    said = json.dumps(sheet, ensure_ascii=False)
    assert "VERBATIM" not in said and "anna@example.org" not in said, "no quote, no sender's address"
    assert feed.clean_detail(sheet) == sheet, "the platform takes it as it is"
    assert waiting.detail(c, row["id"], "0" * 32, NOW) is None, "a token it never showed"
    assert waiting.detail(c, row["id"], row["t"], NOW + waiting.ROW_S + 1) is None, "a list older than a day"
    waiting.press(c, row["t"], "done", NOW)
    assert waiting.detail(c, row["id"], row["t"], NOW + 60) is None, "nothing under it asks any more"


def test_a_chats_sheet_opens_the_chat_by_its_username_or_has_no_link(tmp_path):
    c = core(tmp_path)
    chat = {"source": "telegram", "account": "telegram", "data": {"chat": "777", "username": "anna_example",
                                                                   "name": "Анна"}}
    put(c.mail.store, {**chat, "fields": {"state": "later", "kind": "chat", "sender": "tg:777", "ts": NOW - HOUR,
                                          "card": {"text": "Is a transfer needed?", "needs_now": "reply"}}},
        {**chat, "data": {"chat": "888", "username": "", "name": "Пётр"},
         "fields": {"state": "later", "kind": "chat", "sender": "tg:888", "ts": NOW - HOUR,
                    "card": {"text": "Photos?", "needs_now": "reply"}}})
    anna, petr = sorted(waiting.digest(c, NOW), key=lambda r: r["who"] != "Анна")
    sheet = waiting.detail(c, anna["id"], anna["t"], NOW)
    assert sheet["kind"] == "chat" and sheet["account"] == "" and \
        sheet["open"] == {"url": "https://t.me/anna_example", "label": "Open the chat"}
    bare = waiting.detail(c, petr["id"], petr["t"], NOW)
    assert "open" not in bare and "open" not in bare["asks"][0], "no @username: Telegram's id links open nothing"
    assert feed.clean_detail(sheet) and feed.clean_detail(bare)


def test_a_drafts_sheet_has_its_text_and_its_letter_and_a_private_one_none(tmp_path):
    c = core(tmp_path)
    put(c.mail.store, asking(data={"box": "INBOX", "thread": THREAD, "head": {"message_id": "<m1@example.org>",
                                                                               "reply_to": ""}}))
    draft(c.outbox.store, "aaaa0001", reply_to="e<m1@example.org>", envelope={
        "name": "Анна Петрова", "to": "anna@example.org", "account": "me@example.com", "subject": "Re: hosting",
        "text": "Hello Anna,\n\nPaid, ref 99887766, code 4321.\n\nThanks", "thread": THREAD})
    draft(c.outbox.store, "aaaa0002", private=1)
    draft(c.outbox.store, "aaaa0003", expires=NOW - 1)
    sheet = waiting.detail(c, "draft:aaaa0001", "", NOW)
    assert sheet["kind"] == "draft" and sheet["who"] == "Анна Петрова" and sheet["account"] == "me@example.com"
    assert sheet["draft"] == {"text": "Hello Anna,\n\nPaid, ref ‹number›, code ••••.\n\nThanks", "channel": "mail",
                              "subject": "Re: hosting", "expires": NOW + HOUR, "state": "shown"}
    assert sheet["open"]["url"].endswith(f"#all/{THREAD}") and sheet["asks"][0]["need"] == "reply"
    assert "anna@example.org" not in json.dumps(sheet, ensure_ascii=False)
    assert feed.clean_detail(sheet) == sheet
    assert waiting.detail(c, "draft:aaaa0002", "", NOW) is None, "the private matter's draft waits behind «Show»"
    assert waiting.detail(c, "draft:aaaa0003", "", NOW) is None and waiting.detail(c, "draft:ffff0000", "", NOW) is None
