"""The mail collector against a fake Google: what it keeps as items, once; what it does when a cursor or a token dies."""

import asyncio
import json

from retinue import collector, google

from test_google import FakeGoogle

NOW = 1_760_000_000.0  # 2025-10-09
DAY = 86400


def account(fake, kind="gmail", token="rt1"):
    return google.Google(google.Key(fake.address, kind, token, "cid", "cs"), fake.client())


def refs(state):
    return [(item["source"], item["ref"]) for item in state.look(100)]


def test_the_first_sync_takes_thirty_days_in_and_out_then_history_brings_the_new():
    fake, state = FakeGoogle(), collector.State(":memory:")
    fake.add("m00001", b"x", ["INBOX"], ts=NOW - 40 * DAY)       # older than the backfill
    fake.add("m00002", b"x", ["INBOX", "CATEGORY_UPDATES"], ts=NOW - 3 * DAY)
    fake.add("m00003", b"x", ["SENT"], ts=NOW - 2 * DAY)
    gmail = account(fake)

    async def run():
        first = await collector.poll(state, [gmail], NOW)
        again = await collector.poll(state, [gmail], NOW + 600)
        fake.add("m00004", b"x", ["INBOX", "IMPORTANT"], ts=NOW + 700)
        fake.add("m00005", b"x", ["SENT"], ts=NOW + 800)
        later = await collector.poll(state, [gmail], NOW + 1200)
        return first, again, later

    first, again, later = asyncio.run(run())
    assert (first, again, later) == (2, 0, 2)
    items = state.look(100)
    assert [(i["ref"], i["data"]["box"], i["data"].get("backfill", False)) for i in items] == [
        ("m00002", "INBOX", True), ("m00003", "SENT", True), ("m00004", "INBOX", False), ("m00005", "SENT", False)]
    assert items[2]["data"]["labels"] == ["INBOX", "IMPORTANT"], "the labels come with a new letter"
    assert state.status() == [{"account": fake.address, "source": "mail", "since": NOW - 30 * DAY,
                               "last_ok": NOW + 1200, "failing_since": None, "error": None}]


def test_the_router_looks_then_confirms_and_a_full_sync_never_repeats_an_item():
    fake, state = FakeGoogle(), collector.State(":memory:")
    fake.add("m00001", b"x", ts=NOW - DAY)
    gmail = account(fake)

    async def run():
        await collector.poll(state, [gmail], NOW)
        looked = refs(state)
        assert refs(state) == looked, "looking takes nothing"
        assert state.confirm(state.look()[-1]["seq"]) == 1 and state.look() == []
        fake.add("m00002", b"x", ts=NOW + 100)
        fake.history_gone = True  # a week without a poll: Gmail forgot the cursor
        added = await collector.poll(state, [gmail], NOW + 9 * DAY)
        return looked, added

    looked, added = asyncio.run(run())
    assert looked == [("mail", "m00001")]
    assert added == 1 and refs(state) == [("mail", "m00002")], "the full sync finds the missed one and only it"
    assert state.look()[0]["data"]["backfill"] is True


def test_a_dead_token_is_that_sources_status_and_nothing_elses():
    dead, alive, state = FakeGoogle("a@example.org"), FakeGoogle("b@example.org"), collector.State(":memory:")
    alive.add("m00001", b"x", ts=NOW - DAY)
    dead.grant = "invalid_grant"

    async def run():
        await collector.poll(state, [account(dead), account(alive)], NOW)
        await collector.poll(state, [account(dead), account(alive)], NOW + 600)
        dead.grant = "ok"
        await collector.poll(state, [account(dead)], NOW + 1200)

    asyncio.run(run())
    status = {(s["account"], s["source"]): s for s in state.status()}
    assert status[("b@example.org", "mail")]["error"] is None and refs(state) == [("mail", "m00001")]
    dead_status = status[("a@example.org", "mail")]
    assert dead_status["error"] is None and dead_status["failing_since"] is None and dead_status["last_ok"] == NOW + 1200
    state2 = collector.State(":memory:")
    dead.grant = "invalid_grant"
    asyncio.run(collector.poll(state2, [account(dead)], NOW))
    asyncio.run(collector.poll(state2, [account(dead)], NOW + 600))
    assert state2.status() == [{"account": "a@example.org", "source": "mail", "since": None, "last_ok": None,
                                "failing_since": NOW, "error": "invalid_grant"}], "failing since the first failure"


EVENT = {"id": "e1", "status": "confirmed", "summary": "Интервью с Acme", "updated": "u1",
         "start": {"dateTime": "2025-10-10T12:00:00Z"}, "end": {"dateTime": "2025-10-10T13:00:00Z"},
         "organizer": {"email": "hr@acme.example"}, "description": "x" * 5000,
         "attendees": [{"email": "owner@example.org", "self": True, "responseStatus": "needsAction"},
                       {"email": "hr@acme.example"}]}


def test_calendar_changes_are_items_with_what_changed():
    fake, state = FakeGoogle(), collector.State(":memory:")
    fake.put(EVENT)
    fake.put({**EVENT, "id": "e2", "summary": "Своё", "organizer": {"email": fake.address, "self": True},
              "attendees": [], "start": {"dateTime": "2025-10-11T09:00:00Z"}})
    calendar = account(fake, "calendar", "rt2")

    async def run():
        await collector.poll(state, [calendar], NOW)
        first = state.look(100)
        state.confirm(first[-1]["seq"])
        fake.put({**EVENT, "updated": "u2", "start": {"dateTime": "2025-10-10T15:00:00Z"}})
        fake.put({**EVENT, "id": "e2", "updated": "u3", "status": "cancelled"})
        fake.put({**EVENT, "id": "e3", "updated": "u4", "status": "cancelled"})  # never seen: nothing to say
        await collector.poll(state, [calendar], NOW + 600)
        second = state.look(100)
        state.confirm(second[-1]["seq"])
        fake.sync_gone = True
        await collector.poll(state, [calendar], NOW + 1200)
        return first, second, state.look(100)

    first, second, third = asyncio.run(run())
    assert [(i["data"]["event"]["id"], i["data"]["change"], i["data"]["first"]) for i in first] == [
        ("e1", "new", True), ("e2", "new", True)]
    event = first[0]["data"]["event"]
    assert event["answer"] == "needsAction" and event["organizer"] == "hr@acme.example" and not event["mine"]
    assert len(event["description"]) == 4000 and event["attendees"] == 2, "an event's text is cut"
    assert [(i["data"]["event"]["id"], i["data"]["change"], i["data"]["first"]) for i in second] == [
        ("e1", "time", False), ("e2", "cancelled", False)]
    assert third == [], "a full sync after the token died finds nothing new"


def test_the_agenda_is_read_live_from_every_calendar():
    fake, broken = FakeGoogle(), FakeGoogle("x@example.org")
    fake.calendars.append({"id": "team", "summary": "Команда"})
    fake.put({**EVENT, "start": {"dateTime": "2025-10-10T15:00:00Z"}})
    fake.put({**EVENT, "id": "e9", "summary": "Стендап", "start": {"dateTime": "2025-10-10T08:00:00Z"}}, "team")
    fake.put({**EVENT, "id": "e8", "summary": "Завтра", "start": {"dateTime": "2025-10-12T08:00:00Z"}})
    broken.grant = "invalid_grant"
    events, failed = asyncio.run(collector.agenda([account(fake, "calendar"), account(fake), account(broken, "calendar")],
                                                  NOW, NOW + DAY + 30000))
    assert [(e["summary"], e["calendar_name"], e["account"]) for e in events] == [
        ("Стендап", "Команда", fake.address), ("Интервью с Acme", fake.address, fake.address)]
    assert failed == ["x@example.org: нужен вход заново"]


def test_the_router_reads_through_the_collector_with_its_token_and_nothing_more():
    """The service on the internal network: unsigned — nothing; signed — the items, a letter parsed here, an
    attachment by number, the agenda, the status with key names and no values."""
    import base64
    from email.message import EmailMessage

    from aiohttp.test_utils import TestClient, TestServer

    fake, state = FakeGoogle(), collector.State(":memory:")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "HR <hr@acme.example>", fake.address, "Оффер"
    message.set_content("Высылаем оффер.")
    message.add_attachment(b"%PDF-1.4 offer", maintype="application", subtype="pdf", filename="offer.pdf")
    fake.add("m00007", message.as_bytes(), ["INBOX", "IMPORTANT"], ts=NOW - 60)
    keys = [account(fake), account(fake, "calendar", "rt2")]

    async def run():
        await collector.poll(state, keys, NOW)
        client = TestClient(TestServer(collector.Service(state, lambda: keys, "tok").app()))
        await client.start_server()
        try:
            signed = {"Authorization": "Bearer tok"}
            unsigned = await client.post("/items", json={})
            items = await (await client.post("/items", json={}, headers=signed)).json()
            read = await (await client.post("/mail/letter", json={"account": fake.address.upper(), "id": "m00007"},
                                            headers=signed)).json()
            gone = await client.post("/mail/letter", json={"account": fake.address, "id": "m00008"}, headers=signed)
            part = read["attachments"][0]["part"]
            file = await (await client.post("/mail/attachment", json={"account": fake.address, "id": "m00007",
                                                                      "part": part}, headers=signed)).json()
            body = await client.post("/mail/attachment", json={"account": fake.address, "id": "m00007", "part": 0},
                                     headers=signed)
            confirmed = await (await client.post("/items/confirm", json={"upto": items["items"][-1]["seq"]},
                                                 headers=signed)).json()
            left = await (await client.post("/items", json={}, headers=signed)).json()
            status = await (await client.get("/status", headers=signed)).json()
            return unsigned.status, items, read, gone.status, file, body.status, confirmed, left, status
        finally:
            await client.close()

    unsigned, items, read, gone, file, body, confirmed, left, status = asyncio.run(run())
    assert unsigned == 401
    assert [i["ref"] for i in items["items"]] == ["m00007"]
    assert (read["sender"], read["subject"], read["text"], read["labels"], read["ts"]) == (
        "hr@acme.example", "Оффер", "Высылаем оффер.", ["INBOX", "IMPORTANT"], NOW - 60)
    assert [a["name"] for a in read["attachments"]] == ["offer.pdf"] and gone == 404
    assert (file["name"], file["type"], base64.b64decode(file["data"])) == ("offer.pdf", "application/pdf",
                                                                           b"%PDF-1.4 offer")
    assert body == 404, "the letter's own text is not an attachment"
    assert confirmed == {"confirmed": 1} and left == {"items": []}
    assert status["keys"] == [{"account": fake.address, "kind": "gmail"}, {"account": fake.address, "kind": "calendar"}]
    assert "rt1" not in json.dumps(status) and "cs" not in json.dumps(status["keys"]), "names, never values"


def test_the_collector_will_not_serve_without_a_token(monkeypatch):
    import pytest

    from retinue.config import CollectorConfig

    with pytest.raises(SystemExit):
        collector.Service(collector.State(":memory:"), list, "")
    monkeypatch.delenv("RETINUE_COLLECTOR_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        CollectorConfig.load()
    monkeypatch.setenv("RETINUE_COLLECTOR_TOKEN", "t")
    assert CollectorConfig.load() == CollectorConfig("/keys", "/state/collector.sqlite", 9200, "t")
