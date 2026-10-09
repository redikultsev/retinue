"""Google's APIs for the collector, against a fake Google behind httpx: tokens, cursors, pages, what expires."""

import asyncio
import base64
import json
from urllib.parse import unquote

import httpx
import pytest

from retinue import google


class FakeGoogle:
    """One account's Gmail and Calendar, as much of them as the collector uses. Pages hold `page` items."""

    def __init__(self, address="owner@example.org"):
        self.address, self.page = address, 2
        self.messages = {}     # id -> {raw, labels, ts, thread}
        self.history = []      # (historyId, id, labels)
        self.history_id = 100
        self.grant = "ok"      # "invalid_grant": the refresh token is dead
        self.history_gone = False
        self.calendars = [{"id": address, "summary": address, "primary": True}]
        self.events = {}       # calendar id -> {event id: event}
        self.tick = 1          # calendar changes are numbered; a syncToken is "s<tick>"
        self.sync_gone = False
        self.calls = []

    def add(self, message_id, raw: bytes, labels=("INBOX",), ts=1_760_000_000.0, thread="t1"):
        self.history_id += 1
        self.messages[message_id] = {"raw": raw, "labels": list(labels), "ts": ts, "thread": thread}
        self.history.append((self.history_id, message_id, list(labels)))

    def put(self, event, calendar=None):
        self.tick += 1
        self.events.setdefault(calendar or self.address, {})[event["id"]] = {**event, "_tick": self.tick}

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def _paged(self, request, items, key, last):
        start = int(request.url.params.get("pageToken") or 0)
        page = {key: items[start:start + self.page]}
        if start + self.page < len(items):
            page["nextPageToken"] = str(start + self.page)
        else:
            page.update(last)
        return httpx.Response(200, json=page)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, params = request.url, request.url.params
        self.calls.append((request.method, url.host, url.path, dict(params)))
        if url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if self.grant != "ok" or form.get("grant_type") != "refresh_token":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired"})
            return httpx.Response(200, json={"access_token": "at-" + form["refresh_token"], "expires_in": 3599})
        assert request.headers["authorization"].startswith("Bearer at-")
        path = url.path
        if path == "/gmail/v1/users/me/profile":
            return httpx.Response(200, json={"emailAddress": self.address, "historyId": str(self.history_id)})
        if path == "/gmail/v1/users/me/history":
            if self.history_gone:
                return httpx.Response(404, json={"error": {"code": 404}})
            start, label = int(params["startHistoryId"]), params["labelId"]
            records = [{"id": str(h), "messagesAdded": [{"message": {"id": m, "threadId": "t", "labelIds": labels}}]}
                       for h, m, labels in self.history if h > start and label in labels]
            return self._paged(request, records, "history", {"historyId": str(self.history_id)})
        if path == "/gmail/v1/users/me/messages":
            after = int(params["q"].removeprefix("after:"))
            found = [{"id": m, "threadId": d["thread"]} for m, d in self.messages.items()
                     if params["labelIds"] in d["labels"] and d["ts"] > after]
            return self._paged(request, found, "messages", {})
        if path.startswith("/gmail/v1/users/me/messages/"):
            message_id = path.rsplit("/", 1)[-1]
            if message_id not in self.messages:
                return httpx.Response(404, json={"error": {"code": 404}})
            d = self.messages[message_id]
            return httpx.Response(200, json={"id": message_id, "threadId": d["thread"], "labelIds": d["labels"],
                                             "internalDate": str(int(d["ts"] * 1000)), "sizeEstimate": len(d["raw"]),
                                             "raw": base64.urlsafe_b64encode(d["raw"]).decode().rstrip("=")})
        if path == "/calendar/v3/users/me/calendarList":
            return self._paged(request, self.calendars, "items", {})
        if path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            calendar = unquote(path.split("/")[4])
            events = sorted(self.events.get(calendar, {}).values(), key=lambda e: e["_tick"])
            if params.get("singleEvents") == "true":
                found = [e for e in events if e.get("status") != "cancelled"
                         and params["timeMin"] <= e["start"]["dateTime"] < params["timeMax"]]
                return self._paged(request, sorted(found, key=lambda e: e["start"]["dateTime"]), "items", {})
            if "syncToken" in params:
                if self.sync_gone:
                    return httpx.Response(410, json={"error": {"code": 410}})
                since = int(params["syncToken"][1:])
                found = [e for e in events if e["_tick"] > since]
            else:
                found = [e for e in events if e.get("status") != "cancelled"]
            return self._paged(request, [{k: v for k, v in e.items() if k != "_tick"} for e in found], "items",
                               {"nextSyncToken": f"s{self.tick}"})
        return httpx.Response(500)


KEY = google.Key("owner@example.org", "gmail", "rt1", "cid", "csecret")


def test_the_token_is_refreshed_once_and_a_dead_one_says_so():
    fake = FakeGoogle()

    async def run():
        account = google.Google(KEY, fake.client())
        await account.profile()
        await account.profile()
        fake.grant = "invalid_grant"
        account.access = ""  # as after an hour
        with pytest.raises(google.InvalidGrant):
            await account.profile()

    asyncio.run(run())
    tokens = [c for c in fake.calls if c[1] == "oauth2.googleapis.com"]
    assert len(tokens) == 2, "one refresh for two calls, then the dead one"
    assert all(c[1] in google.HOSTS for c in fake.calls), "nothing but the three hosts"


def test_history_brings_what_was_added_under_a_label_and_an_old_cursor_expires():
    fake = FakeGoogle()
    for n, labels in enumerate([["INBOX"], ["SENT"], ["INBOX", "CATEGORY_PROMOTIONS"], ["INBOX"]]):
        fake.add(f"m{n:05d}", b"x", labels)

    async def run():
        account = google.Google(KEY, fake.client())
        inbox, now = await account.added("100", "INBOX")
        sent, _ = await account.added("100", "SENT")
        later, _ = await account.added("103", "INBOX")
        fake.history_gone = True
        with pytest.raises(google.Expired):
            await account.added("100", "INBOX")
        return inbox, now, sent, later

    inbox, now, sent, later = asyncio.run(run())
    assert [m["id"] for m in inbox] == ["m00000", "m00002", "m00003"] and now == "104", "pages followed to the end"
    assert [m["id"] for m in sent] == ["m00001"] and [m["id"] for m in later] == ["m00003"]
    assert inbox[1]["labelIds"] == ["INBOX", "CATEGORY_PROMOTIONS"], "the labels come with the id"


def test_a_message_is_listed_by_date_and_read_raw():
    fake = FakeGoogle()
    fake.add("m00001", b"From: a@b.c\r\n\r\nold", ts=1_000.0)
    fake.add("m00002", b"From: a@b.c\r\n\r\nnew\xff", ["INBOX", "IMPORTANT"], ts=2_000.5)

    async def run():
        account = google.Google(KEY, fake.client())
        listed = await account.listed("INBOX", 1_500)
        message = await account.raw("m00002")
        with pytest.raises(google.Gone):
            await account.raw("m99999")
        calls = len(fake.calls)
        with pytest.raises(google.Gone):
            await account.raw("../../profile")
        return listed, message, len(fake.calls) - calls

    listed, message, asked = asyncio.run(run())
    assert listed == [{"id": "m00002", "threadId": "t1"}]
    assert message == {"raw": b"From: a@b.c\r\n\r\nnew\xff", "labels": ["INBOX", "IMPORTANT"], "thread": "t1",
                       "ts": 2_000.5, "size": 19}
    assert asked == 0, "an id that is not an id never reaches Google"


def test_calendar_full_then_incremental_and_an_old_token_expires():
    fake = FakeGoogle()
    meeting = {"id": "e1", "status": "confirmed", "summary": "Интервью",
               "start": {"dateTime": "2026-10-10T12:00:00Z"}, "end": {"dateTime": "2026-10-10T13:00:00Z"}}
    fake.put(meeting)
    fake.put({**meeting, "id": "e2", "summary": "Стоматолог", "start": {"dateTime": "2026-10-11T09:00:00Z"}})
    calendar_key = google.Key("owner@example.org", "calendar", "rt2", "cid", "csecret")

    async def run():
        account = google.Google(calendar_key, fake.client())
        calendars = await account.calendars()
        full, token = await account.events(calendars[0]["id"], since=1_760_000_000)
        fake.put({**meeting, "start": {"dateTime": "2026-10-10T15:00:00Z"}})
        fake.put({"id": "e2", "status": "cancelled"})
        changed, token2 = await account.events(calendars[0]["id"], sync_token=token)
        day = await account.agenda(calendars[0]["id"], 1_760_000_000, 1_900_000_000)
        fake.sync_gone = True
        with pytest.raises(google.Expired):
            await account.events(calendars[0]["id"], sync_token=token2)
        return full, token, changed, token2, day

    full, token, changed, token2, day = asyncio.run(run())
    assert [e["id"] for e in full] == ["e1", "e2"] and token == "s3"
    assert [(e["id"], e.get("status")) for e in changed] == [("e1", "confirmed"), ("e2", "cancelled")] and token2 == "s5"
    assert [e["summary"] for e in day] == ["Интервью"], "single occurrences, cancelled ones left out"
    events = [c for c in fake.calls if c[2].endswith("/events")]
    assert events[0][3]["timeMin"] == "2025-10-09T08:53:20Z" and "syncToken" not in events[0][3]
    assert events[1][3]["syncToken"] == "s3" and "timeMin" not in events[1][3], "a token and a time never together"
    assert events[0][3]["singleEvents"] == events[1][3]["singleEvents"] == "false", "the same as the first request"


def test_keys_are_read_from_the_owners_folder(tmp_path):
    (tmp_path / "client.json").write_text(json.dumps({"client_id": "cid", "client_secret": "cs"}))
    (tmp_path / "a@x.org.gmail.json").write_text(json.dumps({"account": "A@x.org", "kind": "gmail",
                                                             "refresh_token": "r1"}))
    (tmp_path / "a@x.org.calendar.json").write_text(json.dumps({"account": "a@x.org", "kind": "calendar",
                                                                "refresh_token": "r2"}))
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "other.json").write_text(json.dumps({"account": "b@x.org", "kind": "drive", "refresh_token": "r"}))
    found = google.keys(tmp_path)
    assert [(k.account, k.kind, k.refresh_token, k.client_id) for k in found] == [
        ("a@x.org", "calendar", "r2", "cid"), ("a@x.org", "gmail", "r1", "cid")]
    assert google.keys(tmp_path / "nothing") == [], "no client, no keys"
