"""The calendar writer with a fake Google Calendar: it makes its one calendar once, writes a block's event from the
closed set of fields only, never writes over the owner's edit, says what he moved or deleted, and stops when he
deleted the calendar. No network, no Google."""

import asyncio
import json
import re

import httpx
from aiohttp.test_utils import TestClient, TestServer

from retinue import calwrite, google


class FakeCalendar:
    """Google Calendar as far as `calendar.app.created` reaches: the calendars it made and their events."""

    def __init__(self):
        self.calendars, self.events, self.tick, self.calls = {}, {}, 0, []

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def touch(self, event, **change):
        self.tick += 1
        event.update(change, etag=f'"e{self.tick}"', updated=str(self.tick))

    def handle(self, request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        path, method = request.url.path.removeprefix("/calendar/v3"), request.method
        self.calls.append((method, path, dict(request.url.params), request.headers.get("if-match")))
        body = json.loads(request.content) if request.content else {}
        if method == "POST" and path == "/calendars":
            calendar = f"c{len(self.calendars) + 1}@group.calendar.google.com"
            self.calendars[calendar] = body
            self.events[calendar] = {}
            return httpx.Response(200, json={"id": calendar, **body})
        calendar, events_part, event_id = re.fullmatch(r"/calendars/([^/]+)(?:/(events)(?:/([^/]+))?)?", path).groups()
        if calendar not in self.calendars:
            return httpx.Response(404)
        events = self.events[calendar]
        if not events_part:
            return httpx.Response(200, json={"id": calendar})
        if method == "POST":
            if body["id"] in events:
                return httpx.Response(409)
            events[body["id"]] = dict(body, status="confirmed")
            self.touch(events[body["id"]])
            return httpx.Response(200, json=events[body["id"]])
        if method == "GET" and event_id is None:
            token = request.url.params.get("syncToken")
            if token == "s-old":
                return httpx.Response(410)
            since = int(token[1:]) if token else 0
            listed = [e if e["status"] != "cancelled" else {"id": e["id"], "status": "cancelled"}  # its id alone
                      for e in events.values() if int(e["updated"]) > since and (token or e["status"] != "cancelled")]
            return httpx.Response(200, json={"items": listed, "nextSyncToken": f"s{self.tick}"})
        event = events.get(event_id)
        if event is None:
            return httpx.Response(404)
        if method == "GET":
            return httpx.Response(200, json=event)
        if method == "PUT":
            if request.headers.get("if-match") != event["etag"]:
                return httpx.Response(412)
            event.clear()
            event.update(body)
            self.touch(event)
            return httpx.Response(200, json=event)
        if method == "DELETE":
            if event["status"] == "cancelled":
                return httpx.Response(410)
            self.touch(event, status="cancelled")
            return httpx.Response(204)
        return httpx.Response(400)


def run(fake, tmp_path, script):
    account = google.Google(google.Key("owner@example.org", "calwrite", "rt", "cid", "cs"), fake.client())
    service = calwrite.Service(tmp_path / "calendar.json", lambda: [account], "tok")

    async def go():
        client = TestClient(TestServer(service.app()))
        await client.start_server()

        async def call(path, body=None, signed=True):
            headers = {"Authorization": "Bearer tok"} if signed else {}
            response = await (client.post(path, json=body or {}, headers=headers) if path != "/status"
                              else client.get(path, headers=headers))
            return response.status, await response.json()
        try:
            return await script(call)
        finally:
            await client.close()

    return asyncio.run(go())


ASK = {"block": "01M6D0SBP7J5NPTRJXTVBP3QDP", "title": "English homework", "start": "2026-11-04T14:30:00Z",
       "minutes": 90, "busy": False, "zone": "Europe/Berlin", "link": "https://app.example.com/today?tab=plan"}


def test_a_block_becomes_one_event_of_its_own_calendar_made_once(tmp_path):
    fake = FakeCalendar()

    async def script(call):
        assert await call("/put", ASK, signed=False) == (401, {"error": "unauthorized"})
        first = await call("/put", ASK)
        again = await call("/put", ASK)  # the router lost its answer: the same id, no second event
        wrong = await call("/put", {**ASK, "minutes": 0})
        hidden = await call("/put", {**ASK, "attendees": [{"email": "x@example.com"}], "link": "javascript:x"})
        return first, again, wrong, hidden, await call("/status")

    first, again, wrong, hidden, status = run(fake, tmp_path, script)
    assert first[0] == 200 and first[1]["event"]["start"] == "2026-11-04T14:30:00Z" and first[1]["event"]["minutes"] == 90
    calendar = next(iter(fake.calendars))
    assert again == (200, {"conflict": first[1]["event"], "calendar": calendar}), \
        "the event is there: the router takes it as it is"
    assert wrong == (400, {"error": "minutes: wrong"}) and hidden == (400, {"error": "link: wrong"})
    assert len(fake.calendars) == 1 and list(fake.calendars.values())[0]["summary"] == "Retinue"
    (event,) = fake.events[next(iter(fake.calendars))].values()
    assert event["id"] == "rt" + ASK["block"].encode().hex() and re.fullmatch(r"[0-9a-v]{5,1024}", event["id"])
    assert set(event) - {"status", "etag", "updated"} == {"id", "summary", "description", "start", "end",
                                                          "transparency", "extendedProperties", "reminders"}
    assert "attendees" not in event and event["transparency"] == "transparent"
    assert event["end"] == {"dateTime": "2026-11-04T16:00:00Z", "timeZone": "Europe/Berlin"}
    assert event["extendedProperties"] == {"private": {"retinue_block": ASK["block"]}}
    assert all(params.get("sendUpdates") == "none" for method, path, params, _ in fake.calls
               if method in ("POST", "PUT", "DELETE") and "/events" in path), "no one is ever notified"
    assert status == (200, {"accounts": ["owner@example.org"], "dead": "", "calendar": True, "gone": False})


def test_the_owners_edit_in_google_is_never_written_over(tmp_path):
    fake = FakeCalendar()

    async def script(call):
        _, made = await call("/put", ASK)
        etag = made["event"]["etag"]
        calendar = next(iter(fake.calendars))
        event = fake.events[calendar]["rt" + ASK["block"].encode().hex()]
        event["colorId"] = "5"
        fake.touch(event, start={"dateTime": "2026-11-04T18:00:00Z"}, end={"dateTime": "2026-11-04T19:00:00Z"})
        stale = await call("/put", {**ASK, "start": "2026-11-04T15:00:00Z", "etag": etag})
        _, changes = await call("/changes", {"token": f"s{fake.tick - 1}"})
        fresh = await call("/put", {**ASK, "start": "2026-11-04T18:00:00Z", "minutes": 60, "busy": True,
                                    "etag": changes["events"][0]["etag"]})
        return stale, changes, fresh, event

    stale, changes, fresh, event = run(fake, tmp_path, script)
    assert stale[1]["conflict"]["start"] == "2026-11-04T18:00:00Z" and stale[1]["conflict"]["minutes"] == 60
    assert [(e["block"], e["start"], e["minutes"]) for e in changes["events"]] == [(ASK["block"],
                                                                                   "2026-11-04T18:00:00Z", 60)]
    assert fresh[0] == 200 and event["transparency"] == "opaque", "a locked block keeps the time busy"
    assert event["colorId"] == "5", "his colour stays"


def test_his_deletions_come_back_and_a_deleted_calendar_stops_the_writes(tmp_path):
    fake = FakeCalendar()

    async def script(call):
        _, made = await call("/put", ASK)
        _, first = await call("/changes", {})
        calendar = next(iter(fake.calendars))
        fake.touch(fake.events[calendar]["rt" + ASK["block"].encode().hex()], status="cancelled")
        _, after = await call("/changes", {"token": first["token"]})
        gone = await call("/put", {**ASK, "etag": made["event"]["etag"]})
        expired = await call("/changes", {"token": "s-old"})
        dropped = await call("/drop", {"block": ASK["block"]})
        fake.calendars.clear()
        lost = await call("/put", {**ASK, "block": "OTHER1"})
        status = await call("/status")
        await call("/calendar")
        made_again = await call("/put", {**ASK, "block": "OTHER1"})
        return first, after, gone, expired, dropped, lost, status, made_again

    first, after, gone, expired, dropped, lost, status, made_again = run(fake, tmp_path, script)
    assert first["full"] is True and len(first["events"]) == 1
    assert after["full"] is False and after["events"][0]["status"] == "cancelled"
    assert gone[1]["gone"] is True and "event" not in gone[1], "deleted in Google: not made again"
    assert expired[1]["full"] is True and expired[1]["events"] == [], "a forgotten token: a full read, the deleted left out"
    assert dropped == (200, {"ok": True})
    assert lost == (400, {"error": "calendar deleted"}) and status[1]["gone"] is True
    assert made_again[0] == 200 and len(fake.calendars) == 1, "made again only when the router asks"


def test_an_event_he_made_himself_in_the_calendar_is_not_a_block():
    assert calwrite.brief({"id": "abc", "summary": "Lunch"}) is None
    assert calwrite.brief({"id": "rt00", "extendedProperties": {"private": {"retinue_block": "../x"}}}) is None
