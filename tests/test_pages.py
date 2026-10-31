"""The owner's pages, the router's side: what it writes for the pages service — checked by a schema, written whole,
named by nothing, and a trip page rendered with every value escaped."""

import asyncio
import json
import os
import stat

import pytest

from retinue import pages

from test_kbcheck import base  # noqa: F401  (a fixture: the knowledge base the pages come from)


def trip(**fields):
    base = {"title": "Энск — Лукоморье", "summary": "Летим в пятницу, жильё у моря.", "start": "2026-05-22",
            "end": "2026-05-24", "options": [{"kind": "flight", "search_id": "f4k2m9qa", "note": "прямой",
                                              "link": "https://www.kupibilet.ru/x"}]}
    return {**base, **fields}


def refusal(value, schema=pages.TRIP_SCHEMA):
    with pytest.raises(pages.Refused) as caught:
        pages.check(schema, value)
    return str(caught.value)


def test_her_trip_is_checked_by_the_schema_and_every_refusal_names_the_field():
    """The model sends JSON; code checks it by the same schema the tool shows her, and a refusal says which field
    and what is wrong — she fixes it and calls again."""
    assert pages.check(pages.TRIP_SCHEMA, trip()) == trip()
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
    assert pages.check(numbers, {"n": 2}) == {"n": 2}


def test_data_is_written_whole_or_not_at_all(tmp_path):
    """The pages service reads the folder at any moment: a file is either the old one or the new one, never half.
    Files are readable by the build (0644); a name cannot climb out of the folder."""
    data = pages.Data(str(tmp_path / "data"))
    data.write("trips/0123456789abcdef.json", {"title": "Лукоморье", "html": "<script>"})
    data.write_bytes("pages/x.html", b"<p>x</p>")
    path = tmp_path / "data" / "trips" / "0123456789abcdef.json"
    assert json.loads(path.read_text()) == {"title": "Лукоморье", "html": "<script>"}, "data, as it was sent"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert data.read("trips/0123456789abcdef.json")["title"] == "Лукоморье" and data.read("nothing.json") is None
    assert sorted(os.listdir(path.parent)) == ["0123456789abcdef.json"], "no temporary file left behind"
    for name in ("../router.yaml", "/etc/passwd", "trips/../../x.json", "trips/.hidden.json", ""):
        with pytest.raises(ValueError):
            data.write(name, {})
    assert data.names("trips") == ["trips/0123456789abcdef.json"] and data.names("photos") == []


def test_a_trip_id_says_nothing_and_is_never_reused(tmp_path):
    """The address of a page goes through Telegram, whose chats are not end-to-end: no date, city or name in it."""
    data = pages.Data(str(tmp_path / "data"))
    ids = {data.new_id("trips") for _ in range(200)}
    assert len(ids) == 200 and all(pages.ID.fullmatch(i) for i in ids)
    data.write("trips/0000000000000000.json", {})
    assert data.has("trips", "0000000000000000") and not data.has("trips", "../../etc")


def hub_core(tmp_path, **extra):
    """A router core with the pages and a mailroom, no channel, no model."""
    from retinue.archive import Archive
    from retinue.core import Core
    from retinue.mail import MailStore
    from retinue.mailroom import Mailroom
    from retinue.protocol import Store

    from test_core import AGENT
    from test_mailroom import FakeCollector

    store = Store(str(tmp_path / "r.sqlite"))
    hub = pages.Pages(pages.Data(str(tmp_path / "data")), "https://pages.in.example.com",
                          ["www.kupibilet.ru", "www.booking.com", "www.google.com/travel/"], **extra)
    fake = FakeCollector()
    core = Core([AGENT], store, "owner", archive=Archive(":memory:"), pages=hub,
                mail=Mailroom(fake, MailStore(store.db)))
    return core, hub, fake


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
    assert pages.ID.fullmatch(page["id"]) and name == f"trips/{page['id']}.json"
    assert text == (f"Published: https://pages.in.example.com/trip-{page['id']} (trip_id {page['id']} — to update "
                    f"this same page). At the end of your reply to the owner: [details](https://pages.in.example.com/"
                    f"trip-{page['id']})")
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
    listed = {entry["id"]: entry for entry in hub.data.read("pages.json")}
    body = (tmp_path / "data" / "pages" / f"trip-{page['id']}.html").read_bytes()
    assert listed[f"trip-{page['id']}"] == {**page["page"], "words": listed[f"trip-{page['id']}"]["words"]} and \
        page["page"]["url"] == f"/trip-{page['id']}" and page["page"]["path"] == f"trips/{page['id']}"
    assert {k: page["page"][k] for k in ("topic", "author", "scripts", "updated")} == {
        "topic": "trips", "author": "assistant", "scripts": "off", "updated": "2025-10-09"}
    import hashlib

    assert page["page"]["sha256"] == hashlib.sha256(body).hexdigest(), "the service takes the page by these bytes"
    assert page["page"]["words"] > 20 and page["page"]["reading_min"] == 1


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
    assert hub.data.read(name)["title"] == "Лукоморье" and f"pages.in.example.com/trip-{page_id} " in text
    assert [entry["id"] for entry in hub.data.read("pages.json")] == [f"trip-{page_id}"], "one page, one entry"
    assert "<title>Лукоморье</title>" in (tmp_path / "data" / "pages" / f"trip-{page_id}.html").read_text()


def test_stay_photos_come_from_travel_ops_and_are_re_encoded_by_code(tmp_path):
    """The page cannot load a photo from a site (the policy is `default-src 'self'`), and the router has no way to
    the sites: travel-ops fetches them (`stay_photos`), the router's file worker re-encodes each as a JPEG — what
    does not decode is dropped — and each goes into the page itself as a data: address: the page loads nothing. A
    photo that would take the page past PAGE_MAX is left out."""
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
    (photo,) = stay["photos"]
    assert flight["photos"] == [] and photo.startswith(pages.JPEG), "the broken one is dropped"
    assert base64.b64decode(photo.removeprefix(pages.JPEG))[:3] == b"\xff\xd8\xff", "re-encoded: a JPEG"
    html = (tmp_path / "data" / "pages" / f"trip-{page_id}.html").read_text()
    assert f'<img src="{photo}"' in html and not (tmp_path / "data" / "photos").exists(), "in the page, not beside it"
    listed = {entry["id"]: entry for entry in hub.data.read("pages.json")}
    import hashlib

    assert listed[f"trip-{page_id}"]["sha256"] == hashlib.sha256(html.encode()).hexdigest(), "the list names the new bytes"
    down = travel_ops([503], [])
    asyncio.run(hub.photos(page_id, down, NOW + 10))
    assert hub.data.read(name)["options"][1]["photos"] == [photo], "a failed look keeps what was"
    tight = travel_ops([{"content": content}], [])
    size = len(html.encode()) - len(photo) - pages.PHOTO_TAG
    original = pages.PAGE_MAX
    try:
        pages.PAGE_MAX = size + len(photo) // 2  # room for the page, not for its photo
        asyncio.run(hub.photos(page_id, tight, NOW + 20))
    finally:
        pages.PAGE_MAX = original
    assert hub.data.read(name)["options"][1]["photos"] == [] and "<img" not in \
        (tmp_path / "data" / "pages" / f"trip-{page_id}.html").read_text(), "a photo past the page's limit is left out"


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


def test_a_sender_chip_is_short():
    """Her «who» is a description — «Google (no-reply@…)», «CarHire Example (…), Maria Example»: the chip takes it
    up to its first aside."""
    assert pages.chip("Google (no-reply@accounts.google.com)") == "Google"
    assert pages.chip("CarHire Example (Прокат на побережье), Maria Example") == "CarHire Example"
    assert pages.chip("Пётр Сидоров (через Google Таблицы)") == "Пётр Сидоров"
    assert pages.chip("Владелец (owner) → прокат") == "Владелец"
    assert pages.chip("Анна Петрова, поддержка") == "Анна Петрова"
    assert pages.chip("Jane Doe (via Google Sheets)") == "Jane Doe" and pages.chip("Jane Doe via Acme") == "Jane Doe"
    assert pages.chip("A" * 40) == "A" * 27 + "…" and pages.chip("") == "no name"


def test_health_lines_are_read_in_either_language():
    """The backup's, the containers' and the health line's words come from other modules (core, health.py), and an
    older router or a persisted line may still say them in Russian: both read the same."""
    for said in ("backup — 5 h ago", "backup — under an hour ago", "бэкап — 5 ч назад"):
        assert pages.backed_up(said), said
    for said in ("backup — 7 h ago, the last run failed: x", "бэкап — 7 ч назад, последний запуск не удался: x",
                 "no backup for 2 d: x", "бэкапа нет 2 сут: x", "backup status unreadable", "backup not set up"):
        assert not pages.backed_up(said), said
    assert pages.BACKUP.sub(r"\1 ", "backup — 5 h ago", count=1) == "backup 5 h ago"
    assert pages.BACKUP.sub("", "бэкап — 5 ч назад", count=1) == "5 ч назад"
    assert pages.CONTAINERS_OK.match("containers: all 3 OK") and pages.CONTAINERS_OK.match(
        "контейнеры: все 3 в порядке")
    assert not pages.CONTAINERS_OK.match("containers: 2 of 3 OK, not OK — assistant (stopped, code 1)")
    assert pages.CONTAINERS.sub("", "containers: none visible", count=1) == "none visible"
    assert (pages.plural(1, "stop", "stops"), pages.plural(21, "stop", "stops")) == ("stop", "stops")
    assert pages.plural(21, "день", "дня", "дней") == "день", "three forms: the Russian rule, for Russian words"


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


def test_the_tracker_is_read_by_its_header():
    """The tracker in the base is read by its header — the Record's own header and markdown in cells do not get in
    the way; a note's address is taken out of its words."""
    rows = pages.Tracker().rows(TRACKER_TEXT)
    assert [(r["n"], r["item"], r["status"], r["report"], r["tone"]) for r in rows] == [
        ("1", "Acme", "meeting", "001", "live"), ("2", "?", "new", "002", "next"),
        ("3", "Globex", "sent", "003", "waiting"), ("4", "Initech", "Maybe later", "", "other")]
    assert (rows[2]["notes"], rows[2]["links"]) == ("form", [
        {"url": "https://forms.globex.example/42", "host": "forms.globex.example"}]), "an address is a button, not text"


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
    tracker = pages.Tracker.of(memory)
    assert (tracker.path, tracker.title, tracker.counted, tracker.meetings) == (
        "plans/profile/bids.md", "Заявки", ("filed", "won"), ("защита",))
    rows = tracker.rows("| # | Date | Fund | Topic | Status |\n|---|---|---|---|---|\n| 1 | 2030-01-02 | Acme | Код | Filed |\n")
    assert [(r["item"], r["detail"], r["key"], r["status"], r["tone"]) for r in rows] == [
        ("Acme", "Код", "filed", "подана", "live")]
    assert pages.Tracker.of(SimpleNamespace(cfg=SimpleNamespace(tracker={"title": "Т"}))).states == pages.STATES
    assert tracker.line(str(tmp_path)) == "", "no Record in the base: nothing told to her"
    (tmp_path / "plans/profile").mkdir(parents=True)
    (tmp_path / "plans/profile/bids.md").write_text("x", encoding="utf-8")
    assert tracker.line(str(tmp_path)) == (f"The owner's tracker \"Заявки\": `{tmp_path}/plans/profile/bids.md`. "
                                           "Status after sending — exactly one of `Filed`, `Won`; a new row — `Filed`.")
    with pytest.raises(SystemExit):
        pages.Tracker.of(SimpleNamespace(cfg=SimpleNamespace(tracker={"rows": 3})))


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


def test_only_a_sound_address_becomes_a_link():
    """Review of the redesign: Go's net/url (Hugo's urls.Parse) refuses `/%zz`, and one such invitation would stop
    every build; Python reads `https://evil.com\\@zoom.us/j/1` as zoom.us while a browser goes to evil.com. An
    address is kept only as both read it alike; an upper-case host or an explicit port is normalised."""
    for bad in ("https://x.zoom.us/%zz", "https://x.com/a%2", "https://evil.com\\@zoom.us/j/1",
                "https://user:pw@zoom.us/j/1", "https://x.com/a b", "http://x.com/", "https://[::1]/x",
                "https://zoom.us@evil.com/", "javascript:alert(1)", ""):
        assert pages.sound(bad) == "", bad
        assert " " in bad or pages.unlinked(f"see {bad} now")[1] == [], bad  # in text a space ends an address
    assert pages.sound("https://Example.ZOOM.us:443/j/1?pwd=A%20b") == "https://example.zoom.us/j/1?pwd=A%20b"
    assert pages.sound("https://x.example:8443/a%2Fb") == "https://x.example:8443/a%2Fb"


def test_a_person_is_named_never_by_an_address():
    assert pages.named("Банк (no-reply@bank.example)", "x@bank.example") == "Банк"
    assert pages.named("", "billing@mail.bank.example") == "mail.bank.example"
    assert pages.named("hr@acme.example", "hr@acme.example") == "acme.example"
    assert pages.named("", "tg:777") == "sender" and pages.domain("tg:777") == ""
    assert pages.named("без имени", "") == pages.named("no name", "") == "sender", "older data: the Russian word too"


# --- the owner's pages: HTML files of the base ---------------------------------------------------------------------

PAGE = ('<!doctype html><html><head><meta charset="utf-8">{mark}<title>{title}</title></head><body>'
        "<h1>{title}</h1><p>Первый абзац о деле.</p><script>var a = 1;</script><p>Ещё слова тут.</p></body></html>")
MARK = '<meta name="lifehub" content="publish">'


@pytest.fixture
def based(tmp_path, base):
    """A core with the base (its hub checks pushes; html may go into `artifacts/` and `journal/`), the pages with
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
    hub = pages.Pages(pages.Data(str(tmp_path / "data")), "https://pages.in.example.com", [])
    core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", archive=Archive(":memory:"), memory=memory,
                pages=hub)
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
        "url": "/regex-course", "path": "notes/artifacts/regex-course.html",
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
    assert os.listdir(tmp_path / "data" / "pages") == ["her-page.html"], "a page gone from the base goes from the data"


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
    """«to pages notes» with an HTML file: code puts it in notes/artifacts/ with the mark, commits it with the trailer
    that says it is his — the router's identity, his page: scripts run — and answers with the address."""
    core, hub, push, subprocess = based
    sent = PAGE.format(mark="", title="Шпаргалка").encode()
    told = asyncio.run(hub.put_page("notes", "Шпаргалка Regex.html", Sent("Шпаргалка Regex.html", sent), 1_760_000_000))
    assert told == ("Page in the Base: notes/artifacts/shpargalka-regex.html. In a couple of minutes it opens at "
                    "https://pages.in.example.com/shpargalka-regex (with the VPN), with its scripts — it is yours.")
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
    assert [r[0] for r in rows] == ["owner", "system"] and rows[0][1] == "[page to pages notes: x.html]"
    assert rows[1][1].startswith("Page in the Base: notes/artifacts/x.html.")


def test_a_page_says_what_the_library_shows():
    meta = pages.page_meta('<html><head><meta name="lifehub" content="Publish"><title> Курс\n по Regex </title>'
                             '<meta name="lifehub:id" content="Regex"></head><body><svg><title>Схема</title><text>не '
                             'слово</text></svg><p><span>Глава</span><span>№1</span></p><p>Раз два <b>три</b>, '
                             "и ещё немного слов о самом главном деле этой страницы.</p><pre class=mermaid>graph</pre></body></html>")
    assert meta == {"id": "regex", "title": "Курс по Regex", "description": "Раз два три, и ещё немного слов о самом главном деле этой страницы.",
                    "words": 16, "reading_min": 1}, "not an SVG's title, not an eyebrow line"
    assert pages.page_meta("<title>x</title><p>нет метки</p>") is None
    assert pages.opted_in("<!DOCTYPE html><p>x</p>") == "<!DOCTYPE html>\n" + MARK + "\n<p>x</p>"
    assert pages.opted_in("<p>x</p>") == "\n" + MARK + "\n<p>x</p>"
    assert pages.opted_in("<html><HEAD lang=ru>" + MARK) == "<html><HEAD lang=ru>" + MARK, "once"
    assert pages.slug("Шпаргалка: Regex & Co") == "shpargalka-regex-co" and pages.slug("…") == ""


def test_a_trip_page_is_text_with_no_script_and_loads_nothing(tmp_path):
    """Her words and a site's are values, never markup: every one through `html.escape`, no script, no event
    handler, nothing from elsewhere; one stylesheet in the page; a link is a button with its host only to a
    travel-ops site, and an address that is not even sound is not named."""
    import re
    from html.parser import HTMLParser

    hosts = ["www.kupibilet.ru", "www.booking.com"]
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>"'
    option = {"kind": "flight", "what": f"Flight {evil}", "pick": True, "note": f"note {evil}", "price": "412 EUR",
              "converted": "", "seller": f"seller {evil}", "lines": [f"line {evil}", ""], "seen": f"seen {evil}",
              "link": 'https://www.kupibilet.ru/x?a="><script>', "photos": ["javascript:alert(3)",
                                                                          "data:image/svg+xml;base64,PHN2Zz4="]}
    trip = {"id": "0123456789abcdef", "title": f"Title {evil}", "summary": f"Summary {evil}",
            "paragraphs": [f"Paragraph {evil}"], "start": "2026-05-22", "end": "2026-05-24",
            "published": "2025-10-09 08:00 UTC", "timeline": [{"day": "2026-05-22", "label": f"Day {evil}", "legs": [
                {"leg": f"AMS → LIS {evil}", "kind": "flight", "name": "Flight", "options": [0, 1, 7, "0"]}]}],
            "options": [option, {**option, "pick": False, "link": "https://evil.example/x", "photos": []}]}
    html = pages.trip_page(trip, hosts)
    tags = []

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))

    Tags().feed(html)
    assert "script" not in {tag for tag, _ in tags} and "<script" not in html.lower(), "her script is text"
    assert not [name for _, attrs in tags for name in attrs if name.startswith("on")], "no event handler on any tag"
    assert [attrs["src"][:23] for tag, attrs in tags if "src" in attrs] == [], "her photos are not data: JPEGs"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=alert(2)&gt;&quot;" in html
    for said in ("Title", "Paragraph", "Day", "AMS → LIS", "Flight", "note", "seller", "line", "seen"):
        assert f"{said} &lt;script&gt;" in html, said
    assert not re.search(r"(src|href)=\"(?!data:image/jpeg;base64,|https://www\.kupibilet\.ru/)", html), \
        "nothing loaded from anywhere, no address but a travel-ops site's"
    assert "javascript:" not in html and "svg+xml" not in html, "only a JPEG's data: address is an image"
    assert html.count("<style>") == 1 and "prefers-color-scheme: dark" in html and '<meta charset="utf-8">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "<title>Title &lt;script&gt;" in html and html.count('class="option') == 2, "two options; 7 and \"0\" are not"
    assert html.count('class="btn"') == 0 and html.count('<span class="chip">kupibilet.ru</span>') == 1
    assert [attrs["href"] for tag, attrs in tags if tag == "a"] == [], "a link with quotes and brackets is no link"
    good = pages.trip_page({**trip, "options": [{**option, "link": "https://www.kupibilet.ru/x?a=1&b=2"}]}, hosts)
    assert '<a class="btn" href="https://www.kupibilet.ru/x?a=1&amp;b=2" rel="noopener noreferrer">kupibilet.ru</a>' \
        in good, "the travel-ops site: a button showing its host"
    assert '<span class="chip">evil.example</span>' in html, "another site: its host, not a link"
    assert html.count("recommended") == 1
    plain = pages.trip_page({**trip, "timeline": [], "options": [{**option, "kind": "stay"}]}, hosts)
    assert "<h2>Stays</h2>" in plain, "no timeline: the options by kind"


def test_the_pages_keep_her_trip_pages_and_a_base_file_cannot_take_their_address(based, tmp_path):
    """The router's trip pages live beside the base's in the same folder and list: reading the base again keeps
    them, and a base file that claims a trip- id is refused — the address is the router's."""
    core, hub, push, _ = based
    hub.data.write("trips/0123456789abcdef.json", {
        "id": "0123456789abcdef", "title": "AMS — LIS", "summary": "Прямой рейс.", "paragraphs": ["Прямой рейс."],
        "start": "2026-05-22", "end": "2026-05-24", "published": "x", "published_ts": 1_760_000_000,
        "options": [], "timeline": []})
    push({"notes/artifacts/course.html": PAGE.format(mark=MARK, title="Курс"),
          "notes/artifacts/fake.html": PAGE.format(mark=MARK + '<meta name="lifehub:id" content="trip-0123456789abcdef">',
                                                   title="Чужой адрес")})
    assert hub.pages(1_760_000_000) is True
    listed = {page["id"]: page for page in json.loads((tmp_path / "data" / "pages.json").read_text())}
    assert sorted(listed) == ["course", "trip-0123456789abcdef"], "a trip written before trip pages: rendered now"
    assert listed["trip-0123456789abcdef"]["path"] == "trips/0123456789abcdef"
    assert {"id": "trip-0123456789abcdef", "why": "trip- ids are the router's trip pages"} in \
        json.loads((tmp_path / "data" / "pages-refused.json").read_text())
    trip = (tmp_path / "data" / "pages" / "trip-0123456789abcdef.html").read_text()
    assert "<title>AMS — LIS</title>" in trip, "the router's page, not the file that claimed its id"
    push({"notes/artifacts/course.html": None})
    assert hub.pages(1_760_000_300) is True
    assert sorted(os.listdir(tmp_path / "data" / "pages")) == ["trip-0123456789abcdef.html"], \
        "a page gone from the base goes; the router's trip page stays"
    assert [p["id"] for p in json.loads((tmp_path / "data" / "pages.json").read_text())] == ["trip-0123456789abcdef"]
    assert hub.library() == [{"id": "trip-0123456789abcdef", "title": "AMS — LIS",
                              "url": "https://pages.in.example.com/trip-0123456789abcdef",
                              "updated": listed["trip-0123456789abcdef"]["updated"], "section": "trips",
                              "minutes": 1}], "a page is at <pages_url>/<id>"


def test_the_library_gives_each_page_its_full_address(tmp_path):
    data = pages.Data(str(tmp_path / "data"))
    hub = pages.Pages(data, "https://pages.in.example.com/", [], build_status=str(tmp_path / "build.json"))
    assert hub.library() is None, "not read yet: not an empty list"
    data.write("pages.json", [{"id": "regex", "title": "", "url": "/regex", "updated": "2026-05-01", "topic": "notes",
                               "reading_min": 3},
                              {"id": "gone", "title": "Gone", "url": "/gone", "updated": "2026-05-01", "topic": "notes"}])
    (tmp_path / "build.json").write_text(json.dumps({"ok": True, "at": 0, "refused": [{"id": "gone", "why": "x"}]}))
    assert hub.library() == [{"id": "regex", "title": "regex", "url": "https://pages.in.example.com/regex",
                              "updated": "2026-05-01", "section": "notes", "minutes": 3}]


def test_the_build_chip_and_the_health_line_speak_of_pages(tmp_path):
    from types import SimpleNamespace

    build = tmp_path / "build.json"
    hub = pages.Pages(pages.Data(str(tmp_path / "data")), "https://pages.in.example.com", [], build_status=str(build))
    hub.core = SimpleNamespace(tz="UTC")
    assert hub.health(1000.0) == "pages not built yet"
    build.write_text(json.dumps({"ok": False, "at": 900, "error": "disk full."}))
    assert hub.health(1000.0) == "pages not updated: disk full"
    build.write_text(json.dumps({"ok": True, "at": 700, "refused": [{"id": "x", "why": "over 3 MB"}]}))
    assert hub.health(1000.0) == "pages built 5 min ago; not published: x (over 3 MB)"
    build.write_text("[")
    assert hub.health(1000.0) == "pages status unreadable"


def test_a_refresh_that_fails_says_so_in_the_log_and_the_next_one_comes(tmp_path, caplog):
    from test_mailroom import NOW

    core, hub, _ = hub_core(tmp_path)

    def broken():
        raise RuntimeError("disk gone")

    hub.write_index = broken
    asyncio.run(hub.refresh(NOW))
    assert "pages: not written" in caplog.text
    hub.write_index = lambda: None
    asyncio.run(hub.refresh(NOW + 300))
