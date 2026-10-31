"""The life hub's data: what the router writes for the pages — checked by a schema, written whole, named by nothing."""

import asyncio
import json
import os
import stat

import pytest

from retinue import lifehub

from test_kbcheck import base  # noqa: F401  (a fixture: the knowledge base the pages come from)


def trip(**fields):
    base = {"title": "Энск — Лукоморье", "summary": "Летим в пятницу, жильё у моря.", "start": "2026-05-22",
            "end": "2026-05-24", "options": [{"kind": "flight", "search_id": "f4k2m9qa", "note": "прямой",
                                              "link": "https://www.kupibilet.ru/x"}]}
    return {**base, **fields}


def refusal(value, schema=lifehub.TRIP_SCHEMA):
    with pytest.raises(lifehub.Refused) as caught:
        lifehub.check(schema, value)
    return str(caught.value)


def test_her_trip_is_checked_by_the_schema_and_every_refusal_names_the_field():
    """The model sends JSON; code checks it by the same schema the tool shows her, and a refusal says which field
    and what is wrong — she fixes it and calls again."""
    assert lifehub.check(lifehub.TRIP_SCHEMA, trip()) == trip()
    assert refusal([]) == "arguments: must be an object"
    assert refusal(trip(title="")) == "title: empty string"
    assert refusal(trip(title="x" * 81)) == "title: at most 80 characters"
    assert refusal(trip(start="22.05.2026")).startswith("start: a date like 2026-05-14")
    assert refusal(trip(end="2026-02-30")).startswith("end: a date like 2026-05-14"), "a date that is not one"
    assert refusal(trip(options=[])) == "options: from 1 to 12 items"
    assert refusal(trip(html="<b>x</b>")).startswith("arguments: unknown field html. Fields: trip_id, title")
    option = trip()["options"][0]
    assert refusal(trip(options=[{**option, "kind": "boat"}])) == "options[0].kind: one of: flight, stay, ground"
    assert refusal(trip(options=[option, {**option, "search_id": "../../x"}])).startswith(
        "options[1].search_id: the search_id of travel-ops' answer")
    assert refusal(trip(options=[{k: v for k, v in option.items() if k != "link"}])) == "options[0]: missing field link"
    assert refusal(trip(options=[{**option, "pick": "yes"}])) == "options[0].pick: true or false"
    assert refusal(trip(trip_id="Mon-Springfield")).startswith("trip_id: the page's id from publish_trip's answer")
    assert refusal(trip(options=[{**option, "note": 5}])) == "options[0].note: must be a string"
    numbers = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 3}}}
    assert refusal({"n": 4}, numbers) == "n: from 1 to 3" and refusal({"n": True}, numbers) == "n: must be an integer"
    assert lifehub.check(numbers, {"n": 2}) == {"n": 2}


def test_data_is_written_whole_or_not_at_all(tmp_path):
    """The build container reads the folder at any moment: a file is either the old one or the new one, never half.
    Files are readable by the build (0644); a name cannot climb out of the folder."""
    data = lifehub.Data(str(tmp_path / "data"))
    data.write("trips/0123456789abcdef.json", {"title": "Лукоморье", "html": "<script>"})
    data.write_bytes("charts/tokens.svg", b"<svg/>")
    path = tmp_path / "data" / "trips" / "0123456789abcdef.json"
    assert json.loads(path.read_text()) == {"title": "Лукоморье", "html": "<script>"}, "data, not markup: Hugo escapes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert data.read("trips/0123456789abcdef.json")["title"] == "Лукоморье" and data.read("nothing.json") is None
    assert sorted(os.listdir(path.parent)) == ["0123456789abcdef.json"], "no temporary file left behind"
    for name in ("../router.yaml", "/etc/passwd", "trips/../../x.json", "trips/.hidden.json", ""):
        with pytest.raises(ValueError):
            data.write(name, {})
    assert data.names("trips") == ["trips/0123456789abcdef.json"] and data.names("photos") == []


def test_a_trip_id_says_nothing_and_is_never_reused(tmp_path):
    """The address of a page goes through Telegram, whose chats are not end-to-end: no date, city or name in it."""
    data = lifehub.Data(str(tmp_path / "data"))
    ids = {data.new_id("trips") for _ in range(200)}
    assert len(ids) == 200 and all(lifehub.ID.fullmatch(i) for i in ids)
    data.write("trips/0000000000000000.json", {})
    assert data.has("trips", "0000000000000000") and not data.has("trips", "../../etc")


def hub_core(tmp_path, **extra):
    """A router core with a lifehub and a mailroom, no channel, no model."""
    from retinue.archive import Archive
    from retinue.core import Core
    from retinue.mail import MailStore
    from retinue.mailroom import Mailroom
    from retinue.protocol import Store

    from test_core import AGENT
    from test_mailroom import FakeCollector

    store = Store(str(tmp_path / "r.sqlite"))
    hub = lifehub.Lifehub(lifehub.Data(str(tmp_path / "data")), "https://hub.in.example.com",
                          ["www.kupibilet.ru", "www.booking.com", "www.google.com/travel/"], **extra)
    fake = FakeCollector()
    core = Core([AGENT], store, "owner", archive=Archive(":memory:"), lifehub=hub,
                mail=Mailroom(fake, MailStore(store.db)))
    return core, hub, fake


def test_the_status_page_counts_what_the_router_saw(tmp_path):
    """The status page: processed, connectors, messages sent, both limit windows, tokens by run kind, the mail kept and
    dropped by kind and the owner's rules. Counted by code from the router's tables; the model writes none of it."""
    from retinue.archive import ASSISTANT, OWNER, SYSTEM

    from test_mailroom import ACCOUNT, NOW

    core, hub, fake = hub_core(tmp_path)
    day = 86400
    for ts, kind, status, usage in ((NOW - 600, "conversation", "done", [272, 51121, 7785378, 1058198]),
                                    (NOW - 500, "triage", "done", [422, 86278, 1795872, 304355]),
                                    (NOW - 400, "triage", "error", [0, 0, 0, 0]),
                                    (NOW - 3 * day, "summary", "done", [6, 1592, 16440, 31123]),
                                    (NOW - 9 * day, "conversation", "done", [1, 1, 1, 1])):
        core.store.run(agent_id="assistant", conversation_id="c", kind=kind, status=status, ts=ts, meta={
            "cost_usd": 0.5, "usage": dict(zip(("input_tokens", "output_tokens", "cache_read_tokens",
                                                 "cache_write_tokens"), usage)),
            "rate_limit": {"status": "allowed", "windows": {"five_hour": {"utilization": 0.23, "resets_at": NOW + 3600},
                                                            "seven_day": {"utilization": 0.63, "resets_at": NOW + day}}}})
    for kind, text in ((OWNER, "привет"), (ASSISTANT, "привет!"), (SYSTEM, "Почта: 1 важное.")):
        core.archive.append(kind, text, conversation_id="c", channel="telegram")
    mail = core.mail.store
    mail.keep([{"seq": 1, "account": ACCOUNT, "source": "mail", "ref": "m1", "data": {"box": "INBOX"}}], NOW - 300)
    mail.set(1, NOW - 300, state="later", kind="money", sender="bank@bank.example")
    mail.dropped(NOW - 200, ACCOUNT, "sale@shop.example", "Скидки", "promo", "triage")
    mail.dropped(NOW - 100, ACCOUNT, "spam@x.example", "Выиграл", "spam", "blocked")
    mail.rule("block", "spam@x.example", now=NOW - 1000)
    mail.rule("mute", "news@site.example", "newsletter", now=NOW - 900)
    mail.sources([{"account": ACCOUNT, "source": "mail", "since": NOW - 30 * day, "last_ok": NOW - 240,
                   "failing_since": None, "error": None}])

    status = hub.status(NOW)
    assert status["updated"].startswith("2025-10-09 ") and status["day"] == {
        "owner": 1, "answers": 1, "notices": 1, "runs": [["ok", 2], ["failure", 1]], "reminders": 0,
        "letters": {"kept": 1, "dropped": 2}}
    assert status["limit"] == [{"window": "5 hours", "share": 23, "level": "ok", "resets": "2025-10-09 09:53 UTC, Thursday",
                                "resets_ts": round(NOW + 3600), "resets_in": "1 h",
                                "seen": "2025-10-09 08:46 UTC, Thursday"},
                               {"window": "week", "share": 63, "level": "ok", "resets": "2025-10-10 08:53 UTC, Friday",
                                "resets_ts": round(NOW + day), "resets_in": "1 d",
                                "seen": "2025-10-09 08:46 UTC, Thursday"}], "the last run that said, not the last row"
    tokens = {row["kind"]: row for row in status["tokens"]}
    assert list(tokens) == ["conversation", "triage", "summary"], "seven days, the largest first; older runs left out"
    assert tokens["triage"] == {"kind": "triage", "name": "letter triage", "runs": 2, "input": 422, "output": 86278,
                                "cache_read": 1795872, "cache_write": 304355, "cost_usd": 1.0, "cells": [
                                    "letter triage", "2", "422", "86 278", "1 795 872", "304 355", "1.00"]}
    assert status["mail"]["day"] == {"kept": [["money", 1]], "dropped": [["promo", 1]],
                                     "blocked": [["spam@x.example", 1]]}
    assert status["mail"]["rules"] == ["drop spam@x.example", "do not notify at once: news@site.example, newsletters"]
    connectors = {c["name"]: c for c in status["connectors"]}
    assert connectors["mail"] == {"name": "mail", "detail": ACCOUNT, "ok": True,
                                  "state": "synced 4 min ago"}, "the account under the name, not in it"
    assert connectors["travel-ops"]["ok"] is False and connectors["travel-ops"]["state"] == "not connected"
    assert "backup" not in connectors, "a core with no backup status says nothing of backups"
    assert [c["name"] for c in status["problems"]] == [c["name"] for c in status["connectors"] if not c["ok"]], \
        "what is broken comes first on the page"
    assert status["build"] is None, "a router with no hub builder has no build to show"
    tokens_chart, runs_chart = status["charts"]
    assert tokens_chart["title"] == "Tokens over 7 days by kind of run" and [r["label"] for r in tokens_chart[
        "rows"]] == ["conversation", "letter triage", "morning brief"]
    days = [r["label"] for r in runs_chart["rows"]]
    assert days[0] == "Fri 3.10" and days[-1] == "Thu 9.10", "seven days back, each by its local date"
    assert [sum(s["w"] for s in r["segments"]) > 0 for r in runs_chart["rows"]] == [
        False, False, False, True, False, False, True], "the days a model ran: three days ago and today"

    hub.write_status(NOW)
    assert hub.data.read("status.json") == status


def record(kb, path, head, body="Текст.\n"):
    file = kb / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("---\n" + head + "\n---\n" + body)


def test_the_now_page_has_trips_the_day_reminders_mail_deadlines_and_health(tmp_path):
    """What is next, who waits for an answer (by person), today, the evening and tomorrow in the owner's local
    time, deadlines overdue and coming, upcoming trips, the status chips. A meeting is a host on a «join» button,
    never its address in text; nobody's e-mail; a letter of the private matter says only that it is one."""
    from test_mailroom import ACCOUNT, NOW

    kb = tmp_path / "kb"
    record(kb, "personal/profile/gym.md", "id: gym\ntitle: Абонемент в спортзал\ntype: profile\n"
           "from: 2025-03-01\nuntil: 2025-12-14")
    record(kb, "personal/knowledge/gym-rules.md", "id: gym-rules\ntitle: Правила спортзала\ntype: knowledge\n"
           "verified: 2025-09-01\nstale_after: 2025-11-01\nstatus: current")
    record(kb, "personal/knowledge/fee.md", "id: fee\ntitle: Тариф\ntype: knowledge\nstale_after: 2025-10-05")
    record(kb, "personal/knowledge/old.md", "id: old\ntitle: Старое правило\ntype: knowledge\n"
           "stale_after: 2025-10-20\nstatus: superseded")
    record(kb, "work/profile/long-ago.md", "id: long-ago\ntitle: Давний контракт\ntype: profile\nuntil: 2024-01-01")
    record(kb, "work/journal/2025-10-01-x.md", "id: x\ntitle: Событие\ntype: event\nuntil: 2025-10-20")
    record(kb, "broken/profile/broken.md", "id: [unclosed\nuntil: 2025-11-01")
    core, hub, fake = hub_core(tmp_path, kb=str(kb))
    hub.data.write("trips/aaaaaaaaaaaaaaaa.json", {"id": "aaaaaaaaaaaaaaaa", "title": "Энск — Лукоморье",
                                                   "start": "2026-05-22", "end": "2026-05-24"})
    hub.data.write("trips/bbbbbbbbbbbbbbbb.json", {"id": "bbbbbbbbbbbbbbbb", "title": "Прошлая", "start": "2025-09-01",
                                                   "end": "2025-09-03"})
    hub.data.write("trips/cccccccccccccccc.json", {"id": "cccccccccccccccc", "title": "Лиссабон", "start": "2025-10-12",
                                                   "end": "2025-10-15"})
    asked = []

    def at(moment, end, summary, **fields):
        return {"summary": summary, "start": {"dateTime": moment}, "end": {"dateTime": end}, **fields}

    async def agenda(start, end):  # NOW is 2025-10-09 08:53 UTC; calendars give their own offsets
        asked.append((start, end))
        return [at("2025-10-09T05:00:00Z", "2025-10-09T06:00:00Z", "Утро"),  # 05:00–06:00 UTC: over
                at("2025-10-09T09:30:00Z", "2025-10-09T10:00:00Z", "Созвон", location="Офис, Примерная 1"),
                at("2025-10-09T09:00:00Z", "2025-10-09T09:30:00Z", "Стендап", recurring=True,
                   organizer="lead@acme.example", answer="needsAction", attendees=9,
                   location="https://example.zoom.us/j/123?pwd=SECRET"),
                at("2025-10-09T07:00:00Z", "2025-10-09T15:00:00Z", "Блок", recurring=True, mine=True),
                at("2025-10-09T18:30:00Z", "2025-10-09T20:00:00Z", "Ужин", mine=True),
                at("2025-10-10T01:00:00Z", "2025-10-10T09:00:00Z", "Фокус", recurring=True, mine=True),
                at("2025-10-10T07:00:00Z", "2025-10-10T08:00:00Z", "Стоматолог",
                   organizer="clinic@x.example", answer="needsAction", attendees=2,
                   description="Ссылка: https://meet.google.com/abc-defg-hij"),
                {"summary": "Праздник", "start": {"date": "2025-10-10"}, "end": {"date": "2025-10-11"}}], []

    fake.agenda = agenda
    core.jobs.add("позвонить в банк", "2025-10-10T18:00", "пятница", NOW)
    mail = core.mail.store
    mail.keep([{"seq": n, "account": ACCOUNT, "source": "telegram" if n > 3 else "mail", "ref": f"m{n}",
                "data": {"name": "Александр"} if n > 3 else {"box": "INBOX"}} for n in range(1, 7)], NOW - 900)
    mail.set(1, NOW - 600, state="later", kind="money", sender="bank@bank.example", ts=NOW - 2 * 86400,
             card={"who": "Банк (no-reply@bank.example)", "text": "Банк просит подтвердить адрес до пятницы.",
                   "needs_now": "act", "deadline_now": "пт"})
    mail.set(2, NOW - 500, state="told", kind="private", sender="desk@acme.example",
             card={"text": "Клиника Acme ждёт ответа", "needs_now": "reply", "private": True})
    mail.set(3, NOW - 400, state="silent", kind="service", sender="shop@x.example",
             card={"text": "Заказ доставлен.", "needs_now": "nothing"})
    for seq, ago, text, need in ((4, 7200, "Александр шлёт статью.", "read"), (5, 3600, "Спрашивает про автобус.",
                                                                              "reply"), (6, 60, "И про билеты.", "read")):
        mail.set(seq, NOW - 300, state="later", kind="chat", sender="tg:777", ts=NOW - ago,
                 card={"text": text, "needs_now": need})

    page = asyncio.run(hub.now(NOW))
    assert asked == [(1759968000.0, 1760140800.0)], "one read: from today's local midnight to the day after tomorrow"
    assert (page["ts"], page["at"], page["date"], page["zone"]) == (round(NOW), "08:53", "2025-10-09", "UTC")
    assert [t["title"] for t in page["trips"]] == ["Лиссабон", "Энск — Лукоморье"], "upcoming only, the nearest first"
    assert page["trips"][0]["url"] == "/trips/cccccccccccccccc/" and page["trips"][0]["days"] == 3
    assert [(e["time"], e["title"]) for e in page["today"]] == [("09:00", "Стендап"), ("09:30", "Созвон")], \
        "by the moment, whatever the offset; what is over is gone"
    daily, call = page["today"]
    assert (daily["place"], daily["join_host"], daily["until"], daily["unanswered"]) == (
        "zoom", "example.zoom.us", "09:30", False)
    assert daily["join"] == "https://example.zoom.us/j/123?pwd=SECRET", "the button's address; the page shows the host"
    assert "lead@acme.example" not in json.dumps(page), "nobody's e-mail on the page"
    assert (call["place"], call["where"], call["join"]) == ("address", "Офис, Примерная 1", "")
    assert [e["title"] for e in page["evening"]] == ["Ужин"]
    assert page["blocks"] == [{"title": "Блок", "time": "07:00", "until": "15:00"}], "a busy block is named once"
    assert [(e["title"], e["all_day"]) for e in page["tomorrow"]] == [("Праздник", True), ("Стоматолог", False)]
    dentist = page["tomorrow"][1]
    assert (dentist["place"], dentist["join_host"], dentist["unanswered"]) == ("meet", "meet.google.com", True)
    assert page["next"]["title"] == "Стендап" and page["next"]["going"] is False
    assert page["reminders"] == [{"ts": page["reminders"][0]["ts"], "time": "18:00", "date": "2025-10-10",
                                  "day": "Fri 10 October", "text": "позвонить в банк"}]
    waiting = page["mail"]
    assert (waiting["count"], waiting["act"]) == (5, 3)
    assert [(g["who"], g["needs"], g["count"]) for g in waiting["groups"]] == [
        ("Александр", "reply", 3), ("Private", "reply", 1), ("Банк", "do", 1)], \
        "by person, the most pressing ask first"
    alex, private, bank = waiting["groups"]
    assert [i["text"] for i in alex["items"]] == ["И про билеты.", "Спрашивает про автобус.", "Александр шлёт статью."]
    assert alex["text"] == "И про билеты." and alex["age"] == "2 h" and alex["where"] == "Telegram"
    assert private["text"] == "1 letter about the private matter — on the tracker page" and private["items"] == []
    assert bank["deadline"] == "пт" and bank["age"] == "2 d" and "bank@" not in json.dumps(bank)
    assert [(d["title"], d["kind"], d["overdue"], d["days"]) for d in page["deadlines"]] == [
        ("Тариф", "recheck", True, -4), ("Правила спортзала", "recheck", False, 23),
        ("Абонемент в спортзал", "document", False, 66)], "superseded, journal, broken and long past ones left out"
    assert page["chips"][0] == {"key": "health", "text": "2 failures", "level": "bad"}, "no travel-ops, no base here"
    assert page["health"].startswith("answers — 0, reminders — 0, failures — 0") and "mail" not in page["health"][:5]
    empty, _, _ = hub_core(tmp_path / "other", kb=str(tmp_path / "nothing"))
    assert asyncio.run(empty.lifehub.now(NOW))["deadlines"] == [], "the template says «no deadlines»"


def test_the_agenda_is_sorted_by_the_moment_not_by_the_string():
    """Seen on the hub: 12:00 stood before 10:30 — two calendars, two offsets, sorted as strings."""
    from retinue.collector import begins

    events = [{"start": {"dateTime": "2025-10-09T12:00:00+03:00"}}, {"start": {"dateTime": "2025-10-09T07:30:00Z"}},
              {"start": {"date": "2025-10-09"}}, {"start": {}}]
    assert [e["start"] for e in sorted(events, key=begins)] == [
        {"date": "2025-10-09"}, {"dateTime": "2025-10-09T07:30:00Z"}, {"dateTime": "2025-10-09T12:00:00+03:00"}, {}]


def test_pages_refresh_every_five_minutes_and_a_minute_after_a_run(tmp_path):
    from test_mailroom import NOW

    core, hub, _ = hub_core(tmp_path)
    written = []

    async def refresh(now):
        written.append(now)

    hub.refresh = refresh

    async def run():
        for at in (NOW, NOW + 30, NOW + 299, NOW + 300):
            hub.step(at)
            await asyncio.sleep(0)
        core.store.run(agent_id="assistant", conversation_id="c", kind="conversation", status="done", meta={})
        core._account(core.agents["assistant"], "c", "conversation", "done", {})
        hub.poked = NOW + 310
        for at in (NOW + 330, NOW + 370, NOW + 400):
            hub.step(at)
            await asyncio.sleep(0)

    asyncio.run(run())
    assert written == [NOW, NOW + 300, NOW + 370], "every five minutes, and a minute after a run ended"


def test_a_refresh_that_fails_says_so_in_the_log_and_the_next_one_comes(tmp_path, caplog):
    from test_mailroom import NOW

    core, hub, fake = hub_core(tmp_path)

    async def broken(start, end):
        raise RuntimeError("collector down")

    fake.agenda = broken
    asyncio.run(hub.refresh(NOW))
    page = hub.data.read("now.json")
    assert page["calendar"] == "calendar not read: the collector is unavailable" and page["today"] == [], \
        "a part that fails is named, the rest stays"
    assert hub.data.read("status.json")["updated"] and hub.data.read("site.json") == {
        "url": "https://hub.in.example.com", "link_hosts": ["www.kupibilet.ru", "www.booking.com", "www.google.com"]}


SEGMENT = {"carrier": "XX", "flight": "XX 123", "origin": "AMS", "destination": "LIS", "operating": None,
           "departs": "2026-05-22T10:15:00+02:00", "arrives": "2026-05-22T11:10:00+02:00"}
FARE = {"price": {"amount": "412.00", "currency": "EUR"}, "converted": {"amount": "412.00", "currency": "EUR"},
        "seller": "kupibilet", "source": "kupibilet", "cabin": "economy", "refundable": None, "fare_name": None,
        "baggage": {"checked": False, "checked_kg": None, "carry_on": True},
        "link": {"url": "https://www.kupibilet.ru/x", "kind": "search"}, "seen_at": "2025-10-09T08:00:00+00:00"}
FLIGHTS = {"search_id": "f4k2m9qa", "searched_at": "2025-10-09T07:58:00+00:00", "currency": "EUR", "cards": [
    {"outbound": [SEGMENT], "inbound": [], "route": ["AMS", "LIS"], "stops": 0, "duration_min": 55,
     "return_duration_min": None, "groups": [{"cabin": "economy", "checked_bag": False, "fares": [
         {**FARE, "link": {"url": "https://www.kupibilet.ru/other", "kind": "search"}, "seller": "other"}, FARE]}]}]}
RATE = {"total": {"amount": "40.00", "currency": "EUR"}, "converted": {"amount": "40.00", "currency": "EUR"},
        "per_night": {"amount": "20.00", "currency": "EUR"}, "seller": "Booking.com", "source": "booking",
        "link": {"url": "https://www.booking.com/hotel/pt/harbor-view.html", "kind": "offer"},
        "seen_at": "2025-10-09T08:01:00+00:00", "free_cancellation": True, "free_cancel_until": "2026-05-20",
        "pay_at_property": None, "meals": None, "room": "Апартаменты с видом на море"}
STAYS = {"search_id": "s7n3p2xk", "searched_at": "2025-10-09T07:59:00+00:00", "currency": "EUR", "cards": [
    {"stay": {"source": "booking", "source_id": "harbor-view", "name": "Harbor View <b>Apartment</b>",
              "kind": "apartment", "rating": 8.7, "reviews": 154, "district": None, "center_km": 1.5,
              "photos": ["https://cf.bstatic.com/1.jpg"], "amenities": []}, "nights": 2, "rates": [RATE]}]}


def trip_with(*options, **fields):
    return {"title": "Энск — Лукоморье, 22–24 мая", "summary": "Прямой рейс и жильё у моря.",
            "start": "2026-05-22", "end": "2026-05-24", "options": list(options), **fields}


FLIGHT = {"kind": "flight", "search_id": "f4k2m9qa", "link": "https://www.kupibilet.ru/x", "note": "прямой",
          "pick": True}
STAY = {"kind": "stay", "search_id": "s7n3p2xk", "link": "https://www.booking.com/hotel/pt/harbor-view.html",
        "note": "в отзывах хвалят вид, жалуются на шум"}


def test_a_trip_page_takes_its_prices_from_travel_ops_not_from_her(tmp_path):
    """She names the options by `search_id` and `link`; code looks each up in travel-ops' stored search — no site is
    asked — and the price, the seller, the times, the rating and when the price was seen come from there. The page
    gets an address that says nothing; the answer tells her how to link it."""
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)
    seen = []
    core.travel = travel_ops([FLIGHTS, STAYS], seen)
    ok, text = asyncio.run(hub.publish(trip_with(FLIGHT, STAY), core.travel, NOW))
    assert ok, text
    assert [call[2]["params"] for call in seen] == [
        {"name": "refine_flights", "arguments": {"search_id": "f4k2m9qa", "limit": 100}},
        {"name": "refine_stays", "arguments": {"search_id": "s7n3p2xk", "limit": 100}}], \
        "a stored search, not a site; first the view she was shown"
    (name,) = hub.data.names("trips")
    page = hub.data.read(name)
    assert lifehub.ID.fullmatch(page["id"]) and name == f"trips/{page['id']}.json"
    assert text == (f"Published: https://hub.in.example.com/trips/{page['id']}/ (trip_id {page['id']} — to update "
                    f"this same page). At the end of your reply to the owner: [details](https://hub.in.example.com/"
                    f"trips/{page['id']}/)")
    assert (page["title"], page["summary"], page["start"], page["end"]) == (
        "Энск — Лукоморье, 22–24 мая", "Прямой рейс и жильё у моря.", "2026-05-22", "2026-05-24")
    flight, stay = page["options"]
    assert flight == {"kind": "flight", "what": "Flight AMS → LIS, nonstop", "pick": True, "note": "прямой",
                      "price": "412 EUR", "converted": "", "seller": "kupibilet",
                      "lines": ["out 22.05 10:15 → 22.05 11:10 · XX 123", "travel time 55 min",
                                "baggage: hand luggage only"],
                      "seen": "price as of 2025-10-09 08:00 UTC, Thursday (search 2025-10-09 07:58 UTC, Thursday)",
                      "link": "https://www.kupibilet.ru/x", "linkable": True, "photos": [], "seen_ts": 1759996800,
                      "leg": "AMS → LIS", "day": "2026-05-22", "host": "kupibilet.ru"}, \
        "the fare her link names, not the cheaper one beside it"
    assert page["paragraphs"] == ["Прямой рейс и жильё у моря."]
    assert page["timeline"] == [
        {"day": "2026-05-22", "label": "Friday, 22 May",
         "legs": [{"leg": "AMS → LIS", "kind": "flight", "name": "Flight", "options": [0]}]},
        {"day": "", "label": "Stays", "legs": [{"leg": "Harbor View <b>Apartment</b>", "kind": "stay", "name": "Stay",
                                               "options": [1]}]}], "by day, the stays without one last"
    assert stay["what"] == "Stay: Harbor View <b>Apartment</b>" and stay["price"] == "40 EUR"
    assert stay["lines"] == ["apartment · rating 8.7 (154 reviews) · 1.5 km from the centre",
                             "2 nights · 20 EUR per night · Апартаменты с видом на море",
                             "free cancellation until 2026-05-20"]
    assert stay["photo_from"] == {"search_id": "s7n3p2xk", "stay": "harbor-view"} and stay["pick"] is False
    assert hub.poked == NOW, "the pages are built again within a minute"


def test_a_trip_is_refused_with_the_field_to_fix(tmp_path):
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)

    def publish(trip, *answers):
        return asyncio.run(hub.publish(trip, travel_ops(list(answers), []), NOW))

    assert publish({**trip_with(FLIGHT), "price": 1}) == (False, "Not published: arguments: unknown field price. "
                                                          "Fields: trip_id, title, summary, start, end, options.")
    assert publish(trip_with({**FLIGHT, "link": "https://evil.example/x"})) == (
        False, "Not published: options[0].link is not a link to a travel-ops site: take link.url from the tool's "
               "answer, whole.")
    assert publish(trip_with({**FLIGHT, "link": "https://www.kupibilet.ru/gone"}), FLIGHTS, FLIGHTS, FLIGHTS) == (
        False, "Not published: options[0] — this link is not in search f4k2m9qa. Take the option's link.url from "
               "travel-ops' answer for this search_id.")
    assert publish(trip_with(FLIGHT), {"error": "no search 'f4k2m9qa' in memory (results are kept for 7 days)"}) == (
        False, "Not published: travel-ops refused: no search 'f4k2m9qa' in memory (results are kept for 7 days).")
    assert publish(trip_with(FLIGHT), 503) == (False, "Not published: travel-ops answered HTTP 503.")
    assert publish(trip_with(FLIGHT, trip_id="0123456789abcdef")) == (
        False, "Not published: there is no page 0123456789abcdef — publish without trip_id.")
    assert asyncio.run(hub.publish(trip_with(FLIGHT), None, NOW)) == (
        False, "Not published: travel-ops is not connected — there is nowhere to take the page's prices from.")
    assert hub.data.names("trips") == [], "nothing half-published"


def test_the_same_trip_is_published_again_at_its_own_address(tmp_path):
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)
    ok, _ = asyncio.run(hub.publish(trip_with(FLIGHT), travel_ops([FLIGHTS], []), NOW))
    (name,) = hub.data.names("trips")
    page_id = hub.data.read(name)["id"]
    ok, text = asyncio.run(hub.publish(trip_with(FLIGHT, STAY, trip_id=page_id, title="Лукоморье"),
                                       travel_ops([FLIGHTS, STAYS], []), NOW + 60))
    assert ok and hub.data.names("trips") == [name] and len(hub.data.read(name)["options"]) == 2
    assert hub.data.read(name)["title"] == "Лукоморье" and f"/trips/{page_id}/" in text


def test_stay_photos_come_from_travel_ops_and_are_re_encoded_by_code(tmp_path):
    """The page cannot load a photo from a site (the policy is `default-src 'self'`), and the router has no way to
    the sites: travel-ops fetches them (`stay_photos`), the router's file worker re-encodes each as a JPEG — what
    does not decode is dropped — and the page shows them from the hub itself."""
    import base64
    import io

    from PIL import Image

    from test_mailroom import NOW
    from test_travel import travel_ops

    picture = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 120, 40)).save(picture, "PNG")
    content = [{"type": "text", "text": "Harbor View (booking): 7 photos known"},
               {"type": "text", "text": "photo 1: https://cf.bstatic.com/1.jpg"},
               {"type": "image", "data": base64.b64encode(picture.getvalue()).decode(), "mimeType": "image/png"},
               {"type": "image", "data": base64.b64encode(b"<svg onload=alert(1)>").decode(), "mimeType": "image/png"}]
    core, hub, _ = hub_core(tmp_path)
    seen = []
    travel = travel_ops([FLIGHTS, STAYS, {"content": content}], seen)
    ok, _ = asyncio.run(hub.publish(trip_with(FLIGHT, STAY), travel, NOW))
    (name,) = hub.data.names("trips")
    page_id = hub.data.read(name)["id"]
    assert hub.pending == [page_id], "the page goes up at once; its photos come with the next step of the clock"
    core.travel = travel

    async def step():
        hub.step(NOW + 5)
        await hub.fetching

    asyncio.run(step())
    assert hub.pending == []
    assert seen[-1][2]["params"] == {"name": "stay_photos", "arguments": {"search_id": "s7n3p2xk",
                                                                          "stays": ["harbor-view"], "per_stay": 2}}
    flight, stay = hub.data.read(name)["options"]
    assert flight["photos"] == [] and stay["photos"] == [f"photos/{page_id}/2-1.jpg"], "the broken one is dropped"
    assert (tmp_path / "data" / "photos" / page_id / "2-1.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    assert hub.poked == NOW + 5, "a page with new photos is built again"
    down = travel_ops([503], [])
    asyncio.run(hub.photos(page_id, down, NOW + 10))
    assert hub.data.read(name)["options"][1]["photos"] == [f"photos/{page_id}/2-1.jpg"], "a failed look keeps what was"


def test_the_status_page_counts_what_was_sent_to_people_by_channel(tmp_path):
    """Sent by the owner's «Send», by channel, for a day and a week — counted from the
    drafts' table; a doubt is not counted as sent. The gateway and the sender among the connectors."""
    from retinue.outbox import Outbox, OutboxStore

    from test_mailroom import NOW, FakeGateway

    core, hub, _ = hub_core(tmp_path)
    core.outbox = Outbox(OutboxStore(core.store.db))
    core.mail.gateway, core.mail.chats = FakeGateway(), (NOW, {"connected": True, "rights": ["can_reply"]})
    for n, (channel, state, at) in enumerate((("mail", "sent", NOW - 60), ("mail", "sent", NOW - 3 * 86400),
                                             ("telegram", "sent", NOW - 120), ("telegram", "unknown", NOW - 60),
                                             ("mail", "dropped", NOW - 60), ("mail", "sent", NOW - 9 * 86400))):
        core.outbox.store.db.execute("INSERT INTO drafts (id, created, state, channel, envelope, digest, head, expires,"
                                     " at) VALUES (?, ?, ?, ?, '{}', '', '[]', 0, ?)", (f"{n:08x}", at, state, channel, at))
    status = hub.status(NOW)
    assert status["sent"] == [["mail", 1, 2], ["Telegram", 1, 1]]
    connectors = {c["name"]: c for c in status["connectors"]}
    assert connectors["Telegram Business"] == {"name": "Telegram Business", "ok": True,
                                               "state": "bot connected, rights: can_reply"}
    assert connectors["mail sending"] == {"name": "mail sending", "ok": False, "state": "not connected"}
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "deploy" / "lifehub" / "site" / "layouts" / "status.html").read_text()
    assert "{{ with $s.sent }}" in page and "Sent to people" in page


def test_an_option_beyond_the_first_hundred_of_a_view_is_still_found(tmp_path):
    """Seen on the server: a search of ~800 flights, her direct flight on the later day. The loose view sorted by
    departure held only the first hundred — the earlier day's flights with stops — so her link was «not in the
    search». An option is looked for in the view she was shown first, then in the loose views by price and by
    departure; each view is asked once for all the options of its search."""
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)
    empty = {**FLIGHTS, "cards": []}
    seen = []
    ok, text = asyncio.run(hub.publish(trip_with(FLIGHT, {**FLIGHT, "pick": False}),
                                       travel_ops([empty, empty, FLIGHTS], seen), NOW))
    assert ok, text
    assert [call[2]["params"]["arguments"] for call in seen] == [
        {"search_id": "f4k2m9qa", "limit": 100},
        {"search_id": "f4k2m9qa", "limit": 100, "max_stops": 3, "max_leg_hours": 72, "sort": "price"},
        {"search_id": "f4k2m9qa", "limit": 100, "max_stops": 3, "max_leg_hours": 72, "sort": "departure"}]
    (name,) = hub.data.names("trips")
    assert [option["price"] for option in hub.data.read(name)["options"]] == ["412 EUR", "412 EUR"]


def test_the_review_page_shows_what_she_decided_on_each_letter_and_message(tmp_path):
    """A week of her judgements, newest first: where from, from whom, her words, where code sent it, and what became of a draft.
    His own messages are not hers to judge; the dropped are a line each from the technical log."""
    import re

    from retinue.outbox import Outbox, OutboxStore

    from test_mailroom import ACCOUNT, NOW

    core, hub, _ = hub_core(tmp_path)
    core.outbox = Outbox(OutboxStore(core.store.db))
    mail, day = core.mail.store, 86400

    def item(ref, source, state, ts, sender, kind, card=None, data=None, event_id=""):
        mail.keep([{"seq": 1, "account": ACCOUNT, "source": source, "ref": ref, "data": data or {}}], ts)
        seq = mail.db.execute("SELECT MAX(seq) FROM mail_items").fetchone()[0]
        mail.set(seq, ts, state=state, sender=sender, kind=kind, ts=ts, event_id=event_id,
                 **({"card": card} if card else {}))

    item("m1", "mail", "later", NOW - 3000, "anna@acme.example", "person",
         {"who": "Анна, Acme", "summary": "Зовёт на созвон", "text": "Анна зовёт на созвон в пятницу.",
          "needs_now": "reply", "deadline_now": "пт 17.10", "cost": "low"})
    item("c1", "telegram", "silent", NOW - 2000, "tg:777", "chat",
         {"text": "Александр шлёт мемы.", "needs_now": "nothing", "cost": "none"}, {"name": "Александр"})
    item("c2", "telegram", "told", NOW - 1000, "tg:777", "chat",
         {"text": "Александр спрашивает, когда автобус.", "needs_now": "reply", "cost": "high"},
         {"name": "Александр"}, event_id="tgb:777/12")
    item("c3", "telegram", "own", NOW - 900, "tg:777", "chat", data={"name": "Александр"})
    item("c4", "telegram", "kept", NOW - 30, "tg:777", "chat", data={"name": "Александр"})
    item("m2", "mail", "silent", NOW - 8 * day, "old@x.example", "service", {"text": "Старое."})
    mail.dropped(NOW - 500, ACCOUNT, "sale@shop.example", "Скидки до 70%", "promo", "triage")
    mail.dropped(NOW - 9 * day, ACCOUNT, "old@shop.example", "Давно", "promo", "triage")
    core.outbox.store.db.execute(
        "INSERT INTO drafts (id, created, state, channel, envelope, digest, reply_to, head, expires, at) "
        "VALUES ('0000000a', ?, 'sent', 'telegram', '{}', '', 'tgb:777/12', '[]', 0, ?)", (NOW - 900, NOW - 800))

    page = hub.review(NOW)
    assert page["updated"].startswith("2025-10-09 ") and page["days"] == 7
    views = {view["path"]: view for view in page["views"]}
    everything = views["all"]
    assert [(i["where"], i["who"], i["decision"]) for i in everything["items"]] == [
        ("Telegram", "Александр", "awaiting judgement"),
        ("Telegram", "Александр", "told you at once"),
        ("Telegram", "Александр", "silently"),
        ("mail", "Анна, Acme", "in the morning brief")], "a week, newest first; his own and older ones left out"
    told, silent, letter = everything["items"][1:]
    assert told["text"] == "Александр спрашивает, когда автобус." and told["needs"] == "reply" and \
        told["draft"] == "draft sent" and told["mark"] == "told"
    assert letter["text"] == "Анна зовёт на созвон в пятницу." and letter["deadline"] == "пт 17.10" and \
        letter["from"] == "anna@acme.example" and letter["draft"] == "" and letter["mark"] == "later"
    assert silent["needs"] == "" and told["time"] == "08:36" and told["day"] == "Thursday, 9 October"
    assert (everything["page"], everything["pages"], everything["prev"], everything["next"]) == (1, 1, "", "")
    facets = page["facets"]
    assert [(f["name"], f["count"], f["path"]) for f in facets["sources"]] == [("Telegram", 3, "src-telegram"),
                                                                             ("mail", 1, "src-mail")]
    assert [(f["name"], f["count"]) for f in facets["decisions"]] == [
        ("told you at once", 1), ("in the morning brief", 1), ("silently", 1), ("awaiting judgement", 1)]
    (alex, anna) = facets["senders"]
    assert (alex["name"], alex["count"], anna["name"], anna["count"]) == ("Александр", 3, "Анна", 1), "a short chip"
    assert re.fullmatch(r"who-[0-9a-f]{10}", alex["path"]) and "@" not in anna["path"], "no address in a path"
    assert [i["who"] for i in views[anna["path"]]["items"]] == ["Анна, Acme"] and views[anna["path"]]["title"] == \
        "Анна, Acme"
    assert [i["mark"] for i in views["dec-told"]["items"]] == ["told"] and views["src-mail"]["filter"] == \
        {"source": "mail", "decision": "", "who": ""}
    assert views["dropped"]["rows"] == [{"when": views["dropped"]["rows"][0]["when"], "from": "sale@shop.example",
                                        "subject": "Скидки до 70%", "kind": "promo"}] and page["dropped"] == 1
    assert all(re.fullmatch(r"[a-z0-9-]{1,40}", path) for path in views), "the adapter checks every path again"
    hub.write_review(NOW)
    assert hub.data.read("review.json") == page


def test_the_review_pages_are_cut_into_pages_of_twenty_five(tmp_path):
    from test_mailroom import ACCOUNT, NOW

    core, hub, _ = hub_core(tmp_path)
    mail = core.mail.store
    for n in range(60):
        mail.keep([{"seq": 1, "account": ACCOUNT, "source": "mail", "ref": f"m{n}", "data": {}}], NOW - n * 60)
        seq = mail.db.execute("SELECT MAX(seq) FROM mail_items").fetchone()[0]
        mail.set(seq, NOW - n * 60, state="silent", sender=f"s{n % 2}@x.example", kind="service", ts=NOW - n * 60,
                 card={"who": f"S{n % 2}", "text": f"Письмо {n}."})
    views = {view["path"]: view for view in hub.review(NOW)["views"]}
    assert [(p, views[p]["page"], views[p]["pages"], len(views[p]["items"])) for p in ("all", "all-p2", "all-p3")] == [
        ("all", 1, 3, 25), ("all-p2", 2, 3, 25), ("all-p3", 3, 3, 10)]
    assert (views["all"]["prev"], views["all"]["next"], views["all-p2"]["prev"], views["all-p3"]["next"]) == (
        "", "all-p2", "all", "")
    assert views["all"]["items"][0]["text"] == "Письмо 0." and views["all-p3"]["items"][-1]["text"] == "Письмо 59."
    assert views["all-p2"]["numbers"] == [{"page": 1, "path": "all"}, {"page": 2, "path": "all-p2"},
                                          {"page": 3, "path": "all-p3"}]


def test_a_sender_chip_is_short_and_the_dropped_count_is_the_whole_week(tmp_path):
    """Her «who» is a description — «Google (no-reply@…)», «CarHire Example (…), Maria Example»: the chip takes it
    up to its first aside. The dropped log shows the newest REVIEW_DROPPED lines, and says how many there were in
    all."""
    from test_mailroom import NOW

    from retinue import lifehub

    assert lifehub.chip("Google (no-reply@accounts.google.com)") == "Google"
    assert lifehub.chip("CarHire Example (Прокат на побережье), Maria Example") == "CarHire Example"
    assert lifehub.chip("Пётр Сидоров (через Google Таблицы)") == "Пётр Сидоров"
    assert lifehub.chip("Владелец (owner) → прокат") == "Владелец"
    assert lifehub.chip("Анна Петрова, поддержка") == "Анна Петрова"
    assert lifehub.chip("Jane Doe (via Google Sheets)") == "Jane Doe" and lifehub.chip("Jane Doe via Acme") == "Jane Doe"
    assert lifehub.chip("A" * 40) == "A" * 27 + "…" and lifehub.chip("") == "no name"
    core, hub, _ = hub_core(tmp_path)
    mail = core.mail.store
    for n in range(lifehub.REVIEW_DROPPED + 5):
        mail.db.execute("INSERT INTO mail_dropped (ts, account, sender, subject, kind, why) VALUES (?, ?, ?, ?, ?, ?)",
                        (NOW - n, "a@example.org", "promo@shop.example", f"Скидки {n}", "promo", "triage"))
    mail.db.commit()
    page = hub.review(NOW)
    assert (page["dropped"], page["dropped_shown"]) == (lifehub.REVIEW_DROPPED + 5, lifehub.REVIEW_DROPPED)


def test_health_lines_are_read_in_either_language():
    """The backup's, the containers' and the health line's words come from other modules (core, health.py), and an
    older router or a persisted line may still say them in Russian: both read the same."""
    for said in ("backup — 5 h ago", "backup — under an hour ago", "бэкап — 5 ч назад"):
        assert lifehub.backed_up(said), said
    for said in ("backup — 7 h ago, the last run failed: x", "бэкап — 7 ч назад, последний запуск не удался: x",
                 "no backup for 2 d: x", "бэкапа нет 2 сут: x", "backup status unreadable", "backup not set up"):
        assert not lifehub.backed_up(said), said
    assert lifehub.BACKUP.sub(r"\1 ", "backup — 5 h ago", count=1) == "backup 5 h ago"
    assert lifehub.BACKUP.sub("", "бэкап — 5 ч назад", count=1) == "5 ч назад"
    assert lifehub.health_words("Health for the day: answers — 1, failures — 0.") == "answers — 1, failures — 0"
    assert lifehub.health_words("Здоровье за сутки: ответов — 1.") == "ответов — 1"
    assert lifehub.CONTAINERS_OK.match("containers: all 3 OK") and lifehub.CONTAINERS_OK.match(
        "контейнеры: все 3 в порядке")
    assert not lifehub.CONTAINERS_OK.match("containers: 2 of 3 OK, not OK — assistant (stopped, code 1)")
    assert lifehub.CONTAINERS.sub("", "containers: none visible", count=1) == "none visible"
    assert (lifehub.plural(1, "stop", "stops"), lifehub.plural(21, "stop", "stops")) == ("stop", "stops")
    assert lifehub.plural(21, "день", "дня", "дней") == "день", "three forms: the Russian rule, for Russian words"


TRACKER_TEXT = """---
id: tracker
type: profile
---

# Трекер

| # | Date | Item | Detail | Score | Status | PDF | Report | Notes |
|---|------|------|--------|-------|--------|-----|--------|-------|
| 1 | 2026-09-23 | Acme | Грант на исследование | 4.1/5 | **Meeting** | ✅ | [001](../reports/001-acme.md) | remote |
| 2 | 2026-09-24 | ? | Конкурс | 3.5/5 | New | ❌ | 002 | |
| 3 | 2026-09-25 | Globex | Партнёрство | 3.0/5 | Sent | ❌ | 003 | form: https://forms.globex.example/42. |
| 4 | 2026-09-26 | Initech | Заявка | 2.0/5 | Maybe later | ❌ | | |
"""


def test_the_tracker_page_reads_the_tracker_the_calendar_and_unanswered_private_letters(tmp_path):
    """The tracker page: the tracker in the base is read by its header — the Record's own header and markdown in
    cells do not get in the way; the funnel counts the tracker's states; a calendar event is one of its meetings
    when it names an item of the tracker or says a meeting word; a letter of the private matter waits until he
    writes in its thread."""
    from retinue.archive import MAIL

    from test_mailroom import ACCOUNT, NOW

    rows = lifehub.Tracker().rows(TRACKER_TEXT)
    assert [(r["n"], r["item"], r["status"], r["report"], r["tone"]) for r in rows] == [
        ("1", "Acme", "meeting", "001", "live"), ("2", "?", "new", "002", "next"),
        ("3", "Globex", "sent", "003", "waiting"), ("4", "Initech", "Maybe later", "", "other")]
    assert (rows[2]["notes"], rows[2]["links"]) == ("form", [
        {"url": "https://forms.globex.example/42", "host": "forms.globex.example"}]), "an address is a button, not text"
    kb = tmp_path / "kb"
    (kb / "profile").mkdir(parents=True)
    (kb / lifehub.Tracker().path).write_text(TRACKER_TEXT, encoding="utf-8")
    core, hub, fake = hub_core(tmp_path, kb=str(kb))

    async def agenda(start, end):
        return [{"summary": "Acme — обсуждение", "start": {"dateTime": "2025-10-10T12:00:00Z"},
                 "end": {"dateTime": "2025-10-10T13:00:00Z"}, "location": "Zoom"},
                {"summary": "Встреча с Hooli", "start": {"dateTime": "2025-10-11T12:00:00Z"},
                 "end": {"dateTime": "2025-10-11T13:00:00Z"}},
                {"summary": "Стоматолог", "start": {"dateTime": "2025-10-12T12:00:00Z"},
                 "end": {"dateTime": "2025-10-12T13:00:00Z"}}], []

    fake.agenda = agenda
    store = core.mail.store
    for seq, thread in ((1, "t-acme"), (2, "t-globex")):
        event, _ = core.archive.append(MAIL, "Письмо", conversation_id=f"mail:{ACCOUNT}", channel="mail",
                                       native_id=f"{ACCOUNT}/j{seq}", ts=NOW - 3600,
                                       meta={"direction": "in", "thread": thread})
        store.keep([{"seq": seq, "account": ACCOUNT, "source": "mail", "ref": f"j{seq}", "data": {}}], NOW - 3600)
        n = store.db.execute("SELECT MAX(seq) FROM mail_items").fetchone()[0]
        store.set(n, NOW - 3600, state="later", kind="private", sender=f"desk@{thread}.example", ts=NOW - 3600,
                  event_id=event.id, card={"text": f"Письмо {thread}", "needs_now": "reply"})
    core.archive.append(MAIL, "Ответ", conversation_id=f"mail:{ACCOUNT}", channel="mail", native_id=f"{ACCOUNT}/o1",
                        ts=NOW - 60, meta={"direction": "out", "thread": "t-globex"})
    store.keep([{"seq": 3, "account": ACCOUNT, "source": "mail", "ref": "j3", "data": {}}], NOW - 600)
    n = store.db.execute("SELECT MAX(seq) FROM mail_items").fetchone()[0]
    store.set(n, NOW - 600, state="later", kind="private", sender="notifications@crm.example", ts=NOW - 600,
              card={"text": "В CRM новая заявка: https://crm.example/leads/1", "needs_now": "read"})
    page = asyncio.run(hub.tracker(NOW))
    assert (page["title"], page["path"]) == ("Tracker", "profile/tracker.md")
    assert page["funnel"] == [{"key": "new", "state": "new", "count": 1, "tone": "next"},
                              {"key": "sent", "state": "sent", "count": 1, "tone": "waiting"},
                              {"key": "meeting", "state": "meeting", "count": 1, "tone": "live"}]
    assert [(g["key"], [r["n"] for r in g["rows"]]) for g in page["groups"]] == [
        ("live", ["1"]), ("waiting", ["3"]), ("next", ["2"]), ("other", ["4"])], "what goes on, what waits, what next"
    assert [i["title"] for i in page["meetings"]] == ["Acme — обсуждение", "Встреча с Hooli"]
    assert page["meetings"][0]["time"] == "12:00" and page["meetings"][0]["place"] == "address"
    assert [letter["text"] for letter in page["letters"]] == ["Письмо t-acme"], "he answered Globex: not waiting"
    assert [n["from"] for n in page["notices"]] == ["notifications@crm.example"], \
        "his own CRM's robot does not wait for his answer"
    asyncio.run(hub.write_tracker(NOW))
    assert hub.data.read("tracker.json")["title"] == "Tracker"


def test_what_the_tracker_tracks_is_the_owners_config(tmp_path):
    """The Record's path, the title, the states and their names, the groups, the counted states, the meeting words
    and the columns come from `memory.tracker`; anything it does not name keeps its neutral default, and a key it
    does not know stops the router at start."""
    from types import SimpleNamespace

    config = {"path": "plans/profile/bids.md", "title": "Заявки", "item": "Fund", "detail": "Topic",
              "states": {"Draft": "черновик", "Filed": "подана", "Won": "выиграна"},
              "groups": [{"key": "live", "title": "Поданы", "states": ["filed"]},
                         {"key": "closed", "title": "Итог", "states": ["won"]}],
              "counted": ["Filed", "Won"], "meetings": ["Защита"]}
    memory = SimpleNamespace(cfg=SimpleNamespace(tracker=config), tree=str(tmp_path))
    tracker = lifehub.Tracker.of(memory)
    assert (tracker.path, tracker.title, tracker.counted, tracker.meetings) == (
        "plans/profile/bids.md", "Заявки", ("filed", "won"), ("защита",))
    rows = tracker.rows("| # | Date | Fund | Topic | Status |\n|---|---|---|---|---|\n| 1 | 2030-01-02 | Acme | Код | Filed |\n")
    assert [(r["item"], r["detail"], r["key"], r["status"], r["tone"]) for r in rows] == [
        ("Acme", "Код", "filed", "подана", "live")]
    assert lifehub.Tracker.of(SimpleNamespace(cfg=SimpleNamespace(tracker={"title": "Т"}))).states == lifehub.STATES
    assert tracker.line(str(tmp_path)) == "", "no Record in the base: nothing told to her"
    (tmp_path / "plans/profile").mkdir(parents=True)
    (tmp_path / "plans/profile/bids.md").write_text("x", encoding="utf-8")
    assert tracker.line(str(tmp_path)) == (f"The owner's tracker \"Заявки\": `{tmp_path}/plans/profile/bids.md`. "
                                           "Status after sending — exactly one of `Filed`, `Won`; a new row — `Filed`.")
    with pytest.raises(SystemExit):
        lifehub.Tracker.of(SimpleNamespace(cfg=SimpleNamespace(tracker={"rows": 3})))


def test_the_pages_bring_the_base_to_the_hub_first_when_no_run_is_going(tmp_path):
    """What the Mac pushed — the tracker, the goals — shows within a refresh, not after her next run; never in the
    middle of a run, which owns the tree."""
    from types import SimpleNamespace

    from test_mailroom import NOW

    core, hub, _ = hub_core(tmp_path)
    synced = []
    core.memory = SimpleNamespace(sync=lambda: synced.append(1), cfg=SimpleNamespace(digest_at="21:00"))

    async def run():
        await hub.refresh(NOW)
        async with core.queue:  # a run is going
            await hub.refresh(NOW + 300)

    asyncio.run(run())
    assert synced == [1]


def test_only_a_sound_address_becomes_a_link_or_a_meeting():
    """Review of the redesign: Go's net/url (Hugo's urls.Parse) refuses `/%zz`, and one such invitation would stop
    every build; Python reads `https://evil.com\\@zoom.us/j/1` as zoom.us while a browser goes to evil.com. An
    address is kept only as both read it alike; an upper-case host or an explicit port is normalised."""
    for bad in ("https://x.zoom.us/%zz", "https://x.com/a%2", "https://evil.com\\@zoom.us/j/1",
                "https://user:pw@zoom.us/j/1", "https://x.com/a b", "http://x.com/", "https://[::1]/x",
                "https://zoom.us@evil.com/", "javascript:alert(1)", ""):
        assert lifehub.sound(bad) == "", bad
        assert lifehub.places(bad, bad)["join"] == "" and lifehub.places("", bad)["place"] == "", bad
        assert " " in bad or lifehub.unlinked(f"see {bad} now")[1] == [], bad  # in text a space ends an address
    assert lifehub.sound("https://Example.ZOOM.us:443/j/1?pwd=A%20b") == "https://example.zoom.us/j/1?pwd=A%20b"
    assert lifehub.sound("https://x.example:8443/a%2Fb") == "https://x.example:8443/a%2Fb"
    assert lifehub.places("", "Join: https://Example.Zoom.US/j/9?pwd=x.") == {
        "place": "zoom", "where": "", "join": "https://example.zoom.us/j/9?pwd=x", "join_host": "example.zoom.us"}
    assert lifehub.places("https://evil.com\\@zoom.us/j/1", "") == {"place": "", "where": "", "join": "",
                                                                   "join_host": ""}, "not named, not a link"


def test_a_person_is_named_never_by_an_address_and_what_runs_on_from_yesterday_is_today(tmp_path):
    from test_mailroom import NOW

    assert lifehub.named("Банк (no-reply@bank.example)", "x@bank.example") == "Банк"
    assert lifehub.named("", "billing@mail.bank.example") == "mail.bank.example"
    assert lifehub.named("hr@acme.example", "hr@acme.example") == "acme.example"
    assert lifehub.named("", "tg:777") == "sender" and lifehub.domain("tg:777") == ""
    assert lifehub.named("без имени", "") == lifehub.named("no name", "") == "sender", "older data: the Russian word too"
    core, hub, _ = hub_core(tmp_path)

    def at(start, end, summary, **fields):
        return {"summary": summary, "start": start, "end": end, **fields}

    shown = hub.timeline([  # NOW is 2025-10-09 08:53 UTC
        at({"dateTime": "2025-10-08T20:00:00Z"}, {"dateTime": "2025-10-09T10:00:00Z"}, "Ночной релиз"),
        at({"date": "2025-10-07"}, {"date": "2025-10-11"}, "Конференция"),
        at({"date": "2025-10-08"}, {"date": "2025-10-09"}, "Вчерашнее"),
        at({"dateTime": "2025-10-08T19:00:00Z"}, {"dateTime": "2025-10-09T09:30:00Z"}, "Блок",
           recurring=True, mine=True)], NOW)
    assert [e["title"] for e in shown["today"]] == ["Конференция", "Ночной релиз"], "begun before today, still on"
    assert shown["blocks"] == [{"title": "Блок", "time": "19:00", "until": "09:30"}]
    assert shown["next"]["title"] == "Ночной релиз" and shown["next"]["going"] is True


# --- the owner's pages: HTML files of the base ---------------------------------------------------------------------

PAGE = ('<!doctype html><html><head><meta charset="utf-8">{mark}<title>{title}</title></head><body>'
        "<h1>{title}</h1><p>Первый абзац о деле.</p><script>var a = 1;</script><p>Ещё слова тут.</p></body></html>")
MARK = '<meta name="lifehub" content="publish">'


@pytest.fixture
def based(tmp_path, base):
    """A core with the base (its hub checks pushes; html may go into `artifacts/` and `journal/`), a lifehub with
    the pages' address, and a Mac clone that pushes as the owner."""
    import sqlite3
    import subprocess

    from retinue.archive import Archive
    from retinue.config import MemoryConfig
    from retinue.core import Core
    from retinue.memory import Memory
    from retinue.protocol import Store

    from test_core import AGENT
    from test_kbcheck import git, install_hub, policy

    writable = ["notes/knowledge/**/*.md", "notes/journal/**/*.md", "notes/**/*.html"]
    hub_git = install_hub(tmp_path, base, writer_uid=-1, writable=writable)
    rules = tmp_path / "policy.json"
    rules.write_text(json.dumps(policy(writable=writable).__dict__, ensure_ascii=False))
    memory = Memory(MemoryConfig(tree=str(tmp_path / "tree"), git_dir=str(tmp_path / "git"), hub=str(hub_git),
                                 policy=str(rules)), sqlite3.connect(":memory:"))
    memory.prepare()
    hub = lifehub.Lifehub(lifehub.Data(str(tmp_path / "data")), "https://hub.in.example.com", [])
    core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", archive=Archive(":memory:"), memory=memory,
                lifehub=hub)
    mac = tmp_path / "mac"
    git(tmp_path, "clone", "-q", str(hub_git), str(mac))

    def push(files: dict, email="owner@mac.example"):
        for name, text in files.items():
            path = mac / name
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        identity = {"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
        git(mac, "add", "-A")
        git(mac, "commit", "-q", "-m", "pages", env=identity)
        git(mac, "push", "-q", "origin", "main")
        memory.sync()

    return core, hub, push, subprocess


def test_the_base_pages_that_opted_in_go_to_the_data_with_who_ever_wrote_them(based, tmp_path):
    """Every *.html in a nature folder with the owner's mark: its bytes to data/pages/<id>.html, the library's line to
    pages.json with the bytes' sha256. The owner's scripts run; a page any commit of hers (the router's identity)
    ever touched runs none — an edit of his on top does not change that."""
    import hashlib

    core, hub, push, _ = based
    push({"notes/artifacts/regex-course.html": PAGE.format(mark=MARK, title="Курс: Regex"),
          "notes/knowledge/draft.html": PAGE.format(mark="", title="Черновик"),          # no mark: not a page
          "notes/top.html": PAGE.format(mark=MARK, title="Не в папке природы"),
          "notes/journal/x.html": PAGE.format(mark=MARK + '<meta name="lifehub:id" content="her-page">'
                                              '<meta name="description" content="Её конспект.">', title="Её")})
    push({"notes/journal/x.html": PAGE.format(mark=MARK + '<meta name="lifehub:id" content="her-page">'
                                              '<meta name="description" content="Её конспект.">', title="Её!")},
         email="assistant@retinue")
    assert hub.pages(1_760_000_000) is True
    listed = {page["id"]: page for page in json.loads((tmp_path / "data" / "pages.json").read_text())}
    assert sorted(listed) == ["her-page", "regex-course"]
    today = listed["regex-course"]["updated"]
    assert listed["regex-course"] == {
        "id": "regex-course", "title": "Курс: Regex", "description": "", "words": 9,
        "reading_min": 1, "topic": "notes", "updated": today, "author": "owner", "scripts": "on",
        "url": "/pages/regex-course.html", "path": "notes/artifacts/regex-course.html",
        "sha256": hashlib.sha256(PAGE.format(mark=MARK, title="Курс: Regex").encode()).hexdigest()}
    assert (listed["her-page"]["author"], listed["her-page"]["scripts"], listed["her-page"]["description"]) == (
        "assistant", "off", "Её конспект."), "her last commit: no scripts, whatever the page says"
    assert sorted(os.listdir(tmp_path / "data" / "pages")) == ["her-page.html", "regex-course.html"]
    assert (tmp_path / "data" / "pages" / "regex-course.html").read_text() == PAGE.format(mark=MARK, title="Курс: Regex")
    assert hub.pages(1_760_000_300) is False, "the hub has not moved: nothing read again"

    push({"notes/journal/x.html": PAGE.format(mark=MARK + '<meta name="lifehub:id" content="her-page">',
                                              title="Её, проверено"), "notes/artifacts/regex-course.html": None})
    assert hub.pages(1_760_000_600) is True
    listed = json.loads((tmp_path / "data" / "pages.json").read_text())
    assert [(p["id"], p["author"], p["scripts"]) for p in listed] == [("her-page", "assistant", "off")], \
        "his commit on top of hers: still hers — he publishes it under a new name to run its scripts"
    assert os.listdir(tmp_path / "data" / "pages") == ["her-page.html"], "a page gone from the base goes from the hub"


def test_a_name_with_a_glob_is_literal_and_never_borrows_the_owners_history(based, tmp_path):
    """Her file named `*.html` must not be judged by the history of every file `*` would match: git reads the path
    literally here, and a name that is not plain a-z0-9 is not published at all."""
    core, hub, push, _ = based
    push({"notes/artifacts/course.html": PAGE.format(mark=MARK, title="Его")})
    page = PAGE.format(mark=MARK + '<meta name="lifehub:id" content="glob">', title="Её")
    push({"notes/artifacts/[c]ourse.html": page}, email="assistant@retinue")
    assert hub.author("notes/artifacts/[c]ourse.html", "refs/remotes/origin/main")[0] == "assistant"
    assert hub.author("notes/artifacts/*.html", "refs/remotes/origin/main")[0] == "assistant", "no such file: hers"
    hub.pages(1_760_000_000)
    assert [p["id"] for p in json.loads((tmp_path / "data" / "pages.json").read_text())] == ["course"]
    assert json.loads((tmp_path / "data" / "pages-refused.json").read_text()) == [
        {"id": "?", "why": "file name malformed: a-z, 0-9, / _ . -"}], "nothing of the name itself"


def test_an_id_two_files_claim_goes_to_the_owners_or_to_nobody(based, tmp_path):
    core, hub, push, _ = based
    claim = '<meta name="lifehub:id" content="{}">'
    push({"notes/artifacts/a.html": PAGE.format(mark=MARK + claim.format("cv"), title="Его"),
          "notes/artifacts/c.html": PAGE.format(mark=MARK + claim.format("twin"), title="Его"),
          "notes/artifacts/d.html": PAGE.format(mark=MARK + claim.format("twin"), title="И это его")})
    push({"notes/artifacts/b.html": PAGE.format(mark=MARK + claim.format("cv"), title="Её"),
          "notes/journal/e.html": PAGE.format(mark=MARK + claim.format("hers"), title="Её"),
          "notes/knowledge/f.html": PAGE.format(mark=MARK + claim.format("hers"), title="И это её")},
         email="assistant@retinue")
    hub.pages(1_760_000_000)
    listed = json.loads((tmp_path / "data" / "pages.json").read_text())
    assert [(p["id"], p["path"], p["scripts"]) for p in listed] == [("cv", "notes/artifacts/a.html", "on")]
    assert sorted(json.loads((tmp_path / "data" / "pages-refused.json").read_text()), key=lambda r: r["id"]) == [
        {"id": "hers", "why": "one id on two files, neither of them yours"},
        {"id": "twin", "why": "one id on two files of yours"}]


class Sent:
    """A file the owner sent, as the Telegram channel hands it over."""

    def __init__(self, name, data, size=None, refused=""):
        self.name, self.data, self.size, self.refused = name, data, len(data) if size is None else size, refused

    async def fetch(self):
        return self.data


def test_a_page_sent_in_telegram_goes_to_the_base_as_the_owners(based, tmp_path):
    """«to hub notes» with an HTML file: code puts it in notes/artifacts/ with the mark, commits it with the trailer
    that says it is his — the router's identity, his page: scripts run — and answers with the address."""
    core, hub, push, subprocess = based
    sent = PAGE.format(mark="", title="Шпаргалка").encode()
    told = asyncio.run(hub.put_page("notes", "Шпаргалка Regex.html", Sent("Шпаргалка Regex.html", sent), 1_760_000_000))
    assert told == ("Page in the Base: notes/artifacts/shpargalka-regex.html. In a couple of minutes it opens at "
                    "https://hub.in.example.com/pages/shpargalka-regex.html (with the VPN), with its scripts — it is yours.")
    log = subprocess.run([*core.memory.repo, "log", "-1", "--format=%ae|%s|%b", "origin/main"], capture_output=True,
                         text=True, env=core.memory.env).stdout
    assert log.strip() == ("assistant@retinue|The owner via Telegram: page notes/artifacts/shpargalka-regex.html|"
                           "Retinue-Upload: telegram")
    stored = (core.memory.tree / "notes/artifacts/shpargalka-regex.html").read_text()
    assert stored.startswith('<!doctype html><html><head>\n<meta name="lifehub" content="publish"><meta charset')
    assert core.memory.since(0) == [], "not hers: no line in the evening list, no «Revert»"
    hub.pages(1_760_000_100)
    listed = json.loads((tmp_path / "data" / "pages.json").read_text())
    assert [(p["id"], p["author"], p["scripts"]) for p in listed] == [("shpargalka-regex", "owner", "on")]
    again = asyncio.run(hub.put_page("notes", "Шпаргалка Regex.html", Sent("x.html", sent + b" "), 1_760_000_150))
    assert again.startswith("Page in the Base: notes/artifacts/shpargalka-regex-2.html.") and \
        "was already there — this one went beside it under a new name, the old one is untouched" in again, "never replaced: it may be hers"

    for space, name, upload, said in (
            ("travel", "a.html", Sent("a.html", sent), "there is no Space \"travel\" in the Base. There are: notes."),
            ("notes", "a.pdf", Sent("a.pdf", sent), "this is not an HTML file (.html needed)."),
            ("notes", "a.html", Sent("a.html", b"x", size=4 * 2**20), "the file is over 3 MB."),
            ("notes", "a.html", Sent("a.html", "<title>ё</title>".encode("cp1251")), "the file is not UTF-8."),
            ("notes", "a.html", Sent("a.html", PAGE.format(mark='<meta name="lifehub:id" content="A B">',
                                                           title="x").encode()),
             "lifehub:id in the file — only a-z, 0-9 and hyphens.")):
        assert asyncio.run(hub.put_page(space, name, upload, 1_760_000_200)) == f"Page not published: {said}"
    core.memory = None
    assert asyncio.run(hub.put_page("notes", "a.html", Sent("a.html", sent), 0)) == (
        "Page not published: the Base is not connected — there is nowhere to put it.")


def test_the_owners_upload_is_on_record_and_answered_once(based):
    core, hub, _, _ = based
    from types import SimpleNamespace

    channel = SimpleNamespace(name="telegram")
    sent = Sent("x.html", PAGE.format(mark=MARK, title="x").encode())
    asyncio.run(hub.upload(channel, "notes", sent, "77"))
    asyncio.run(hub.upload(channel, "notes", sent, "77"))  # delivered again after a restart
    rows = core.archive.db.execute("SELECT kind, text FROM events ORDER BY ts").fetchall()
    assert [r[0] for r in rows] == ["owner", "system"] and rows[0][1] == "[page to hub notes: x.html]"
    assert rows[1][1].startswith("Page in the Base: notes/artifacts/x.html.")


def test_a_page_says_what_the_library_shows():
    meta = lifehub.page_meta('<html><head><meta name="lifehub" content="Publish"><title> Курс\n по Regex </title>'
                             '<meta name="lifehub:id" content="Regex"></head><body><svg><title>Схема</title><text>не '
                             'слово</text></svg><p><span>Глава</span><span>№1</span></p><p>Раз два <b>три</b>, '
                             "и ещё немного слов о самом главном деле этой страницы.</p><pre class=mermaid>graph</pre></body></html>")
    assert meta == {"id": "regex", "title": "Курс по Regex", "description": "Раз два три, и ещё немного слов о самом главном деле этой страницы.",
                    "words": 16, "reading_min": 1}, "not an SVG's title, not an eyebrow line"
    assert lifehub.page_meta("<title>x</title><p>нет метки</p>") is None
    assert lifehub.opted_in("<!DOCTYPE html><p>x</p>") == "<!DOCTYPE html>\n" + MARK + "\n<p>x</p>"
    assert lifehub.opted_in("<p>x</p>") == "\n" + MARK + "\n<p>x</p>"
    assert lifehub.opted_in("<html><HEAD lang=ru>" + MARK) == "<html><HEAD lang=ru>" + MARK, "once"
    assert lifehub.slug("Шпаргалка: Regex & Co") == "shpargalka-regex-co" and lifehub.slug("…") == ""
