"""The life hub's data: what the router writes for the pages — checked by a schema, written whole, named by nothing."""

import asyncio
import json
import os
import stat

import pytest

from retinue import lifehub


def trip(**fields):
    base = {"title": "Вена — Котор", "summary": "Летим в пятницу, жильё у моря.", "start": "2026-10-22",
            "end": "2026-10-24", "options": [{"kind": "flight", "search_id": "f4k2m9qa", "note": "прямой",
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
    assert refusal([]) == "аргументы: нужен объект"
    assert refusal(trip(title="")) == "title: пустая строка"
    assert refusal(trip(title="x" * 81)) == "title: не длиннее 80 знаков"
    assert refusal(trip(start="22.10.2026")).startswith("start: дата вида 2026-11-14")
    assert refusal(trip(end="2026-02-30")).startswith("end: дата вида 2026-11-14"), "a date that is not one"
    assert refusal(trip(options=[])) == "options: от 1 до 12 элементов"
    assert refusal(trip(html="<b>x</b>")).startswith("аргументы: неизвестное поле html. Поля: trip_id, title")
    option = trip()["options"][0]
    assert refusal(trip(options=[{**option, "kind": "boat"}])) == "options[0].kind: одно из: flight, stay, ground"
    assert refusal(trip(options=[option, {**option, "search_id": "../../x"}])).startswith(
        "options[1].search_id: search_id из ответа travel-ops")
    assert refusal(trip(options=[{k: v for k, v in option.items() if k != "link"}])) == "options[0]: нет поля link"
    assert refusal(trip(options=[{**option, "pick": "yes"}])) == "options[0].pick: true или false"
    assert refusal(trip(trip_id="Mon-Kotor")).startswith("trip_id: id страницы из ответа publish_trip")
    assert refusal(trip(options=[{**option, "note": 5}])) == "options[0].note: нужна строка"
    numbers = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 3}}}
    assert refusal({"n": 4}, numbers) == "n: от 1 до 3" and refusal({"n": True}, numbers) == "n: нужно целое число"
    assert lifehub.check(numbers, {"n": 2}) == {"n": 2}


def test_data_is_written_whole_or_not_at_all(tmp_path):
    """The build container reads the folder at any moment: a file is either the old one or the new one, never half.
    Files are readable by the build (0644); a name cannot climb out of the folder."""
    data = lifehub.Data(str(tmp_path / "data"))
    data.write("trips/0123456789abcdef.json", {"title": "Котор", "html": "<script>"})
    data.write_bytes("charts/tokens.svg", b"<svg/>")
    path = tmp_path / "data" / "trips" / "0123456789abcdef.json"
    assert json.loads(path.read_text()) == {"title": "Котор", "html": "<script>"}, "data, not markup: Hugo escapes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert data.read("trips/0123456789abcdef.json")["title"] == "Котор" and data.read("nothing.json") is None
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
    """Decision 5: processed, connectors, messages sent, both limit windows, tokens by run kind, the mail kept and
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
        "owner": 1, "answers": 1, "notices": 1, "runs": [["удачно", 2], ["сбой", 1]], "reminders": 0,
        "letters": {"kept": 1, "dropped": 2}}
    assert status["limit"] == [{"window": "5 часов", "share": 23, "resets": "2025-10-09 12:53 МСК, четверг",
                                "seen": "2025-10-09 11:46 МСК, четверг"},
                               {"window": "неделя", "share": 63, "resets": "2025-10-10 11:53 МСК, пятница",
                                "seen": "2025-10-09 11:46 МСК, четверг"}], "the last run that said, not the last row"
    tokens = {row["kind"]: row for row in status["tokens"]}
    assert list(tokens) == ["conversation", "triage", "summary"], "seven days, the largest first; older runs left out"
    assert tokens["triage"] == {"kind": "triage", "name": "разбор письма", "runs": 2, "input": 422, "output": 86278,
                                "cache_read": 1795872, "cache_write": 304355, "cost_usd": 1.0, "cells": [
                                    "разбор письма", "2", "422", "86 278", "1 795 872", "304 355", "1.00"]}
    assert status["mail"]["day"] == {"kept": [["деньги", 1]], "dropped": [["реклама", 1]],
                                     "blocked": [["spam@x.example", 1]]}
    assert status["mail"]["rules"] == ["отсеивать spam@x.example", "не уведомлять сразу: news@site.example, рассылки"]
    connectors = {c["name"]: c for c in status["connectors"]}
    assert connectors[f"почта {ACCOUNT}"] == {"name": f"почта {ACCOUNT}", "ok": True,
                                              "state": "синхронизирована 4 мин назад"}
    assert connectors["travel-ops"]["ok"] is False and connectors["travel-ops"]["state"] == "не подключён"
    assert "бэкап" not in connectors, "a core with no backup status says nothing of backups"
    assert status["charts"] == ["charts/tokens.svg", "charts/runs.svg"]

    hub.write_status(NOW)
    assert hub.data.read("status.json") == status
    assert (tmp_path / "data" / "charts" / "tokens.svg").read_bytes().startswith(b"<svg ")
    runs = (tmp_path / "data" / "charts" / "runs.svg").read_text()
    assert ">чт 9.10<" in runs and ">пт 3.10<" in runs, "seven days back, each by its local date"


def record(kb, path, head, body="Текст.\n"):
    file = kb / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("---\n" + head + "\n---\n" + body)


def test_the_now_page_has_trips_the_day_reminders_mail_deadlines_and_health(tmp_path):
    """Decision 4: upcoming trips, today's and tomorrow's meetings, his reminders, mail that waits for him, document
    deadlines from the base (`until`, `stale_after`), the health line. A letter about the job search says only that
    it is one: the page is open on a phone too."""
    from test_mailroom import ACCOUNT, NOW

    kb = tmp_path / "kb"
    record(kb, "relocation/profile/residence.md", "id: residence\ntitle: Вид на жительство\ntype: profile\n"
           "from: 2025-03-01\nuntil: 2025-12-14")
    record(kb, "relocation/knowledge/d-visa.md", "id: d-visa\ntitle: Условия D-визы\ntype: knowledge\n"
           "verified: 2025-09-01\nstale_after: 2025-11-01\nstatus: current")
    record(kb, "relocation/knowledge/old.md", "id: old\ntitle: Старое правило\ntype: knowledge\n"
           "stale_after: 2025-10-20\nstatus: superseded")
    record(kb, "career/profile/long-ago.md", "id: long-ago\ntitle: Давний контракт\ntype: profile\nuntil: 2024-01-01")
    record(kb, "career/journal/2025-10-01-x.md", "id: x\ntitle: Событие\ntype: event\nuntil: 2025-10-20")
    record(kb, "broken/profile/broken.md", "id: [unclosed\nuntil: 2025-11-01")
    core, hub, fake = hub_core(tmp_path, kb=str(kb))
    hub.data.write("trips/aaaaaaaaaaaaaaaa.json", {"id": "aaaaaaaaaaaaaaaa", "title": "Вена — Котор",
                                                   "start": "2025-10-22", "end": "2025-10-24"})
    hub.data.write("trips/bbbbbbbbbbbbbbbb.json", {"id": "bbbbbbbbbbbbbbbb", "title": "Прошлая", "start": "2025-09-01",
                                                   "end": "2025-09-03"})
    hub.data.write("trips/cccccccccccccccc.json", {"id": "cccccccccccccccc", "title": "Лиссабон", "start": "2025-10-12",
                                                   "end": "2025-10-15"})

    async def agenda(start, end):
        day = "2025-10-09" if start < NOW else "2025-10-10"
        return [{"summary": f"Встреча {day[-2:]}", "start": {"dateTime": f"{day}T12:00:00Z"},
                 "end": {"dateTime": f"{day}T13:00:00Z"}, "mine": True}], []

    fake.agenda = agenda
    core.jobs.add("позвонить в банк", "2025-10-10T18:00", "пятница", NOW)
    mail = core.mail.store
    mail.keep([{"seq": n, "account": ACCOUNT, "source": "mail", "ref": f"m{n}", "data": {"box": "INBOX"}}
               for n in (1, 2, 3)], NOW - 900)
    mail.set(1, NOW - 600, state="later", kind="money", sender="bank@bank.example",
             card={"text": "Банк просит подтвердить адрес до пятницы.", "needs_now": "act", "deadline_now": "пт"})
    mail.set(2, NOW - 500, state="told", kind="job", sender="hr@acme.example",
             card={"text": "Acme зовёт на интервью", "needs_now": "reply", "job": True})
    mail.set(3, NOW - 400, state="silent", kind="service", sender="shop@x.example",
             card={"text": "Заказ доставлен.", "needs_now": "nothing"})

    page = asyncio.run(hub.now(NOW))
    assert [t["title"] for t in page["trips"]] == ["Лиссабон", "Вена — Котор"], "upcoming only, the nearest first"
    assert page["trips"][0]["url"] == "/trips/cccccccccccccccc/"
    assert page["today"] == ["чт 9 октября, 15:00–16:00 МСК — Встреча 09"]
    assert page["tomorrow"] == ["пт 10 октября, 15:00–16:00 МСК — Встреча 10"]
    assert page["reminders"] == ["пт 10 октября, 18:00 МСК — позвонить в банк"]
    assert page["mail"] == [{"text": "Банк просит подтвердить адрес до пятницы.", "from": "bank@bank.example",
                             "needs": "сделать", "deadline": "пт"},
                            {"text": "Поиск работы — подробности в Telegram.", "from": "", "needs": "ответить",
                             "deadline": ""}]
    assert page["deadlines"] == [
        {"title": "Условия D-визы", "date": "2025-11-01", "what": "перепроверить", "path": "relocation/knowledge/d-visa.md"},
        {"title": "Вид на жительство", "date": "2025-12-14", "what": "действует до",
         "path": "relocation/profile/residence.md"}], "superseded, journal, broken and long past ones left out"
    assert page["health"].startswith("ответов — 0, напоминаний — 0, сбоев — 0") and "почта" not in page["health"][:5]
    empty, _, _ = hub_core(tmp_path / "other", kb=str(tmp_path / "nothing"))
    assert asyncio.run(empty.lifehub.now(NOW))["deadlines"] == [], "the template says «без срока»"


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
    assert page["today"] == ["календарь не прочитан: сборщик недоступен"], "a part that fails is named, the rest stays"
    assert hub.data.read("status.json")["updated"] and hub.data.read("site.json") == {
        "url": "https://hub.in.example.com", "link_hosts": ["www.kupibilet.ru", "www.booking.com", "www.google.com"]}


SEGMENT = {"carrier": "OS", "flight": "OS 731", "origin": "VIE", "destination": "TIV", "operating": None,
           "departs": "2025-10-22T12:50:00+02:00", "arrives": "2025-10-22T13:45:00+02:00"}
FARE = {"price": {"amount": "497.00", "currency": "EUR"}, "converted": {"amount": "497.00", "currency": "EUR"},
        "seller": "kupibilet", "source": "kupibilet", "cabin": "economy", "refundable": None, "fare_name": None,
        "baggage": {"checked": False, "checked_kg": None, "carry_on": True},
        "link": {"url": "https://www.kupibilet.ru/x", "kind": "search"}, "seen_at": "2025-10-09T08:00:00+00:00"}
FLIGHTS = {"search_id": "f4k2m9qa", "searched_at": "2025-10-09T07:58:00+00:00", "currency": "EUR", "cards": [
    {"outbound": [SEGMENT], "inbound": [], "route": ["VIE", "TIV"], "stops": 0, "duration_min": 55,
     "return_duration_min": None, "groups": [{"cabin": "economy", "checked_bag": False, "fares": [
         {**FARE, "link": {"url": "https://www.kupibilet.ru/other", "kind": "search"}, "seller": "other"}, FARE]}]}]}
RATE = {"total": {"amount": "36.00", "currency": "EUR"}, "converted": {"amount": "36.00", "currency": "EUR"},
        "per_night": {"amount": "18.00", "currency": "EUR"}, "seller": "Booking.com", "source": "booking",
        "link": {"url": "https://www.booking.com/hotel/me/golden-bay.html", "kind": "offer"},
        "seen_at": "2025-10-09T08:01:00+00:00", "free_cancellation": True, "free_cancel_until": "2025-10-20",
        "pay_at_property": None, "meals": None, "room": "Апартаменты с видом на море"}
STAYS = {"search_id": "s7n3p2xk", "searched_at": "2025-10-09T07:59:00+00:00", "currency": "EUR", "cards": [
    {"stay": {"source": "booking", "source_id": "golden-bay", "name": "Golden Bay <b>Apartment</b>",
              "kind": "apartment", "rating": 9.2, "reviews": 243, "district": None, "center_km": 1.5,
              "photos": ["https://cf.bstatic.com/1.jpg"], "amenities": []}, "nights": 2, "rates": [RATE]}]}


def trip_with(*options, **fields):
    return {"title": "Вена — Тиват, 22–24 октября", "summary": "Прямой рейс и жильё у моря.",
            "start": "2025-10-22", "end": "2025-10-24", "options": list(options), **fields}


FLIGHT = {"kind": "flight", "search_id": "f4k2m9qa", "link": "https://www.kupibilet.ru/x", "note": "прямой",
          "pick": True}
STAY = {"kind": "stay", "search_id": "s7n3p2xk", "link": "https://www.booking.com/hotel/me/golden-bay.html",
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
        {"name": "refine_flights", "arguments": {"search_id": "f4k2m9qa", "limit": 100, "max_stops": 3,
                                                 "max_leg_hours": 72, "sort": "departure"}},
        {"name": "refine_stays", "arguments": {"search_id": "s7n3p2xk", "limit": 100, "min_rating": 0,
                                               "max_center_km": 100, "sort": "rating"}}], "a stored search, not a site"
    (name,) = hub.data.names("trips")
    page = hub.data.read(name)
    assert lifehub.ID.fullmatch(page["id"]) and name == f"trips/{page['id']}.json"
    assert text == (f"Опубликовано: https://hub.in.example.com/trips/{page['id']}/ (trip_id {page['id']} — чтобы "
                    f"обновить эту же страницу). В конце ответа Владельцу: [подробнее](https://hub.in.example.com/"
                    f"trips/{page['id']}/)")
    assert (page["title"], page["summary"], page["start"], page["end"]) == (
        "Вена — Тиват, 22–24 октября", "Прямой рейс и жильё у моря.", "2025-10-22", "2025-10-24")
    flight, stay = page["options"]
    assert flight == {"kind": "flight", "what": "Перелёт VIE → TIV, без пересадок", "pick": True, "note": "прямой",
                      "price": "497 EUR", "converted": "", "seller": "kupibilet",
                      "lines": ["туда 22.10 12:50 → 22.10 13:45 · OS 731", "в пути 55 мин",
                                "багаж: только ручная кладь"],
                      "seen": "цена на 2025-10-09 11:00 МСК, четверг (поиск 2025-10-09 10:58 МСК, четверг)",
                      "link": "https://www.kupibilet.ru/x", "linkable": True, "photos": []}, \
        "the fare her link names, not the cheaper one beside it"
    assert stay["what"] == "Жильё: Golden Bay <b>Apartment</b>" and stay["price"] == "36 EUR"
    assert stay["lines"] == ["апартаменты · рейтинг 9,2 (243 отзыва) · 1,5 км от центра",
                             "2 ночи · 18 EUR за ночь · Апартаменты с видом на море",
                             "бесплатная отмена до 2025-10-20"]
    assert stay["photo_from"] == {"search_id": "s7n3p2xk", "stay": "golden-bay"} and stay["pick"] is False
    assert hub.poked == NOW, "the pages are built again within a minute"


def test_a_trip_is_refused_with_the_field_to_fix(tmp_path):
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)

    def publish(trip, *answers):
        return asyncio.run(hub.publish(trip, travel_ops(list(answers), []), NOW))

    assert publish({**trip_with(FLIGHT), "price": 1}) == (False, "Не опубликовано: аргументы: неизвестное поле price. "
                                                          "Поля: trip_id, title, summary, start, end, options.")
    assert publish(trip_with({**FLIGHT, "link": "https://evil.example/x"})) == (
        False, "Не опубликовано: options[0].link — не ссылка на площадку travel-ops: возьми link.url из ответа "
               "инструмента целиком.")
    assert publish(trip_with({**FLIGHT, "link": "https://www.kupibilet.ru/gone"}), FLIGHTS) == (
        False, "Не опубликовано: options[0] — этой ссылки нет в поиске f4k2m9qa. Возьми link.url варианта из ответа "
               "travel-ops по этому search_id.")
    assert publish(trip_with(FLIGHT), {"error": "no search 'f4k2m9qa' in memory (results are kept for 7 days)"}) == (
        False, "Не опубликовано: travel-ops отказал: no search 'f4k2m9qa' in memory (results are kept for 7 days).")
    assert publish(trip_with(FLIGHT), 503) == (False, "Не опубликовано: travel-ops ответил HTTP 503.")
    assert publish(trip_with(FLIGHT, trip_id="0123456789abcdef")) == (
        False, "Не опубликовано: страницы 0123456789abcdef нет — опубликуй без trip_id.")
    assert asyncio.run(hub.publish(trip_with(FLIGHT), None, NOW)) == (
        False, "Не опубликовано: travel-ops не подключён — цены для страницы брать неоткуда.")
    assert hub.data.names("trips") == [], "nothing half-published"


def test_the_same_trip_is_published_again_at_its_own_address(tmp_path):
    from test_mailroom import NOW
    from test_travel import travel_ops

    core, hub, _ = hub_core(tmp_path)
    ok, _ = asyncio.run(hub.publish(trip_with(FLIGHT), travel_ops([FLIGHTS], []), NOW))
    (name,) = hub.data.names("trips")
    page_id = hub.data.read(name)["id"]
    ok, text = asyncio.run(hub.publish(trip_with(FLIGHT, STAY, trip_id=page_id, title="Тиват"),
                                       travel_ops([FLIGHTS, STAYS], []), NOW + 60))
    assert ok and hub.data.names("trips") == [name] and len(hub.data.read(name)["options"]) == 2
    assert hub.data.read(name)["title"] == "Тиват" and f"/trips/{page_id}/" in text


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
    content = [{"type": "text", "text": "Golden Bay (booking): 7 photos known"},
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
                                                                          "stays": ["golden-bay"], "per_stay": 2}}
    flight, stay = hub.data.read(name)["options"]
    assert flight["photos"] == [] and stay["photos"] == [f"photos/{page_id}/2-1.jpg"], "the broken one is dropped"
    assert (tmp_path / "data" / "photos" / page_id / "2-1.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    assert hub.poked == NOW + 5, "a page with new photos is built again"
    down = travel_ops([503], [])
    asyncio.run(hub.photos(page_id, down, NOW + 10))
    assert hub.data.read(name)["options"][1]["photos"] == [f"photos/{page_id}/2-1.jpg"], "a failed look keeps what was"


def test_the_status_page_counts_what_was_sent_to_people_by_channel(tmp_path):
    """Decision 9 of stage 13: sent by the owner's «Отправить», by channel, for a day and a week — counted from the
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
    assert status["sent"] == [["почта", 1, 2], ["Telegram", 1, 1]]
    connectors = {c["name"]: c for c in status["connectors"]}
    assert connectors["Telegram Business"] == {"name": "Telegram Business", "ok": True,
                                               "state": "бот подключён, права: can_reply"}
    assert connectors["отправка почты"] == {"name": "отправка почты", "ok": False, "state": "не подключена"}
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "deploy" / "lifehub" / "site" / "layouts" / "status.html").read_text()
    assert "{{ with $s.sent }}" in page and "Отправлено людям" in page
