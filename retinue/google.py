"""Google's APIs as the mail collector reaches them: plain httpx and our own token refresh, no Google client library.
Read-only scopes only: `gmail.readonly` for a mailbox, `calendar.events.readonly` with `calendar.calendarlist.readonly`
for its calendars — two refresh tokens per account from the same Desktop client, because a password change kills
every token that holds a Gmail scope (research 44, 57).

Three hosts and nothing else; the collector's proxy lets through exactly these (`HOSTS`).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("retinue.google")

TOKEN = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
HOSTS = ("oauth2.googleapis.com", "gmail.googleapis.com", "www.googleapis.com")
SCOPES = {"gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
          "calendar": ["https://www.googleapis.com/auth/calendar.events.readonly",
                       "https://www.googleapis.com/auth/calendar.calendarlist.readonly"]}
PAGE = 500          # history.list and messages.list: the most Gmail gives in one page
EVENTS_PAGE = 250   # events.list: the default; the same in the first request and every one after (syncToken)
MAX_PAGES = 200     # a listing longer than this is cut: the next poll continues from the cursor
MESSAGE_ID = re.compile(r"[0-9A-Za-z_-]{6,64}")
TIMEOUT = 60


class GoogleError(Exception):
    """A call that failed this time — the network, a 5xx, a quota. The next poll tries again."""


class InvalidGrant(GoogleError):
    """The refresh token no longer works: access revoked, the password changed (Gmail scopes), six months unused.
    Only the owner can fix it, with a new consent on his Mac."""


class Expired(GoogleError):
    """A cursor Google no longer knows: Gmail's historyId (404), Calendar's syncToken (410). A full sync follows."""


class Gone(GoogleError):
    """The message is no longer in the mailbox (404)."""


@dataclass
class Key:
    account: str        # the address Google confirmed at consent
    kind: str           # gmail | calendar
    refresh_token: str
    client_id: str
    client_secret: str


def keys(folder: str | Path) -> list[Key]:
    """The tokens the owner put on the server (deploy/mail/login.py): `client.json` and one `<account>.<kind>.json`
    per consent. Read on every poll, so a new login works without a restart. A broken file is skipped by name."""
    folder = Path(folder)
    try:
        client = json.loads((folder / "client.json").read_text())
    except (OSError, ValueError):
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        if path.name == "client.json":
            continue
        try:
            data = json.loads(path.read_text())
            found.append(Key(str(data["account"]).lower(), str(data["kind"]), str(data["refresh_token"]),
                             str(client["client_id"]), str(client["client_secret"])))
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("key file %s is not readable; skipped", path.name)
    return [key for key in found if key.kind in SCOPES]


def rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Google:
    """One account's one token: Gmail or Calendar."""

    def __init__(self, key: Key, http: httpx.AsyncClient) -> None:
        self.key, self.http = key, http
        self.access, self.until = "", 0.0

    async def _token(self) -> str:
        if self.access and time.time() < self.until - 60:
            return self.access
        try:
            response = await self.http.post(TOKEN, data={"client_id": self.key.client_id,
                                                         "client_secret": self.key.client_secret,
                                                         "refresh_token": self.key.refresh_token,
                                                         "grant_type": "refresh_token"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise GoogleError(f"сеть: {type(exc).__name__}") from None
        try:
            said = response.json()
        except ValueError:
            said = {}
        if response.status_code in (400, 401) and said.get("error") in ("invalid_grant", "invalid_client",
                                                                          "unauthorized_client"):
            raise InvalidGrant(str(said.get("error")))
        if response.status_code != 200 or not said.get("access_token"):
            raise GoogleError(f"токен: HTTP {response.status_code}")
        self.access, self.until = str(said["access_token"]), time.time() + float(said.get("expires_in") or 3600)
        return self.access

    async def get(self, url: str, params: dict | None = None) -> dict:
        for attempt in (1, 2):
            token = await self._token()
            try:
                response = await self.http.get(url, params=params, timeout=TIMEOUT,
                                               headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise GoogleError(f"сеть: {type(exc).__name__}") from None
            if response.status_code == 401 and attempt == 1:  # the access token died early: a fresh one, once
                self.access = ""
                continue
            break
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            raise Gone(url.rsplit("/", 1)[-1])
        if response.status_code == 410:
            raise Expired("syncToken")
        raise GoogleError(f"{httpx.URL(url).host}: HTTP {response.status_code}")

    async def _pages(self, url: str, params: dict, key: str) -> tuple[list[dict], dict]:
        """Every page of a listing: the items under `key`, and the last page (its historyId, nextSyncToken)."""
        found, page, token = [], {}, None
        for _ in range(MAX_PAGES):
            page = await self.get(url, {**params, **({"pageToken": token} if token else {})})
            found += [item for item in page.get(key) or [] if isinstance(item, dict)]
            token = page.get("nextPageToken")
            if not token:
                break
        return found, page

    # --- Gmail ------------------------------------------------------------------------------------------------

    async def profile(self) -> dict:
        """{emailAddress, historyId, …}: who this token reads, and where the mailbox's history is now."""
        return await self.get(f"{GMAIL}/profile")

    async def added(self, start: str, label: str) -> tuple[list[dict], str]:
        """Messages added under `label` (INBOX, SENT) since the cursor `start`: [{id, threadId, labelIds}], and the
        mailbox's historyId at this call. A cursor too old for Gmail — Expired."""
        try:
            records, last = await self._pages(f"{GMAIL}/history", {"startHistoryId": start, "labelId": label,
                                                                  "historyTypes": "messageAdded",
                                                                  "maxResults": PAGE}, "history")
        except Gone:
            raise Expired("historyId") from None
        found = [added["message"] for record in records for added in record.get("messagesAdded") or []
                 if isinstance(added.get("message"), dict) and added["message"].get("id")]
        return found, str(last.get("historyId") or start)

    async def listed(self, label: str, after: float) -> list[dict]:
        """Messages under `label` that arrived after `after`: [{id, threadId}] — the first sync and a full one."""
        found, _ = await self._pages(f"{GMAIL}/messages", {"labelIds": label, "q": f"after:{int(after)}",
                                                           "maxResults": PAGE}, "messages")
        return found

    async def raw(self, message_id: str) -> dict:
        """One message as it is: {raw: bytes, labels, thread, ts (the mailbox's own time), size}."""
        if not MESSAGE_ID.fullmatch(message_id):
            raise Gone(message_id)
        data = await self.get(f"{GMAIL}/messages/{message_id}", {"format": "raw"})
        raw = str(data.get("raw") or "")
        return {"raw": base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), "labels": data.get("labelIds") or [],
                "thread": str(data.get("threadId") or ""), "ts": int(data.get("internalDate") or 0) / 1000,
                "size": int(data.get("sizeEstimate") or 0)}

    # --- Calendar ---------------------------------------------------------------------------------------------

    async def calendars(self) -> list[dict]:
        """Every calendar the account sees, hidden ones too: [{id, summary, primary, accessRole}]."""
        found, _ = await self._pages(f"{CALENDAR}/users/me/calendarList", {"showHidden": "true"}, "items")
        return found

    async def events(self, calendar_id: str, sync_token: str | None = None,
                     since: float | None = None) -> tuple[list[dict], str]:
        """Changed events since `sync_token`, or every event from `since` on (a full sync), and the next
        syncToken. Recurring events stay as their series (`singleEvents=false`); cancelled ones come with
        `status: cancelled`. A token too old for Google — Expired."""
        params = {"maxResults": EVENTS_PAGE, "singleEvents": "false"}
        params.update({"syncToken": sync_token} if sync_token else {"timeMin": rfc3339(since or time.time())})
        found, last = await self._pages(f"{CALENDAR}/calendars/{quote(calendar_id, safe='')}/events", params,
                                        "items")
        return found, str(last.get("nextSyncToken") or "")

    async def agenda(self, calendar_id: str, start: float, end: float) -> list[dict]:
        """The single occurrences between `start` and `end`, in order: what the owner's day holds."""
        found, _ = await self._pages(f"{CALENDAR}/calendars/{quote(calendar_id, safe='')}/events",
                                     {"singleEvents": "true", "orderBy": "startTime", "timeMin": rfc3339(start),
                                      "timeMax": rfc3339(end), "maxResults": EVENTS_PAGE}, "items")
        return found
