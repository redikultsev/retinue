"""The mail collector: code, no model. It reads the owner's Gmail and Google Calendar with read-only tokens every ten
minutes and keeps what it found as numbered items for the router. The router — the archive's one owner — looks at
them, keeps them in its own table, then confirms up to the last one it kept (`upto`): the hand-off of travel-ops'
price alerts, so a restart on either side loses nothing and keeps nothing twice.

A letter waits here as an id: its body is read from the mailbox when the router asks for it, and an attachment only
for a letter the router kept. A calendar change comes whole — an event is small — with what changed in it.

Its own container, its own user, its own way out: a proxy that lets through Google's three hosts (`google.HOSTS`).
No model, no knowledge base, no archive file.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from aiohttp import web

from . import google, letter
from .config import CollectorConfig

log = logging.getLogger("retinue.collector")

POLL_S = 600             # how often each mailbox and calendar is read
BACKFILL_DAYS = 30       # the first sync of a mailbox takes the letters of this many days back
CALENDAR_BACK_DAYS = 30  # the first sync of a calendar takes events from this many days back on
BOXES = ("INBOX", "SENT")  # what is collected: letters to the owner and his own; Spam and drafts are not
TEXT = {"summary": 300, "location": 300, "description": 4000}  # an event's own text kept, at most


class State:
    """The collector's own SQLite: cursors, the items the router has not confirmed yet, the last seen state of every
    calendar event (to say what changed), and how each source is doing."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cursors (account TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY (account, name));
            CREATE TABLE IF NOT EXISTS items (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                source TEXT NOT NULL,           -- mail | calendar
                ref TEXT NOT NULL,              -- mail: the message id; calendar: <calendar>/<event>/<updated>
                data TEXT NOT NULL,
                given INTEGER NOT NULL DEFAULT 0,  -- 1: the router confirmed it; kept, so a full sync adds it never again
                created REAL NOT NULL,
                UNIQUE (account, source, ref)
            );
            CREATE TABLE IF NOT EXISTS events (account TEXT NOT NULL, calendar TEXT NOT NULL, id TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY (account, calendar, id));
            CREATE TABLE IF NOT EXISTS status (
                account TEXT NOT NULL,
                source TEXT NOT NULL,           -- mail | calendar
                since REAL,                     -- what it covers: from this moment on
                last_ok REAL,                   -- the last sync that went through
                failing_since REAL,             -- the first failure in a row; NULL while it works
                error TEXT,                     -- the last failure: invalid_grant, or what went wrong
                PRIMARY KEY (account, source)
            );
            """
        )
        self.db.commit()

    def cursor(self, account: str, name: str) -> str | None:
        row = self.db.execute("SELECT value FROM cursors WHERE account = ? AND name = ?", (account, name)).fetchone()
        return row[0] if row else None

    def set_cursor(self, account: str, name: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM cursors WHERE account = ? AND name = ?", (account, name))
        else:
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?, ?)", (account, name, value))
        self.db.commit()

    def add(self, account: str, source: str, ref: str, data: dict, now: float) -> bool:
        """Keep a found item once: the same letter or the same version of an event is never an item twice."""
        cursor = self.db.execute("INSERT OR IGNORE INTO items (account, source, ref, data, created) VALUES (?, ?, ?, ?, ?)",
                                 (account, source, ref, json.dumps(data, ensure_ascii=False), now))
        self.db.commit()
        return cursor.rowcount == 1

    def look(self, limit: int = 50) -> list[dict]:
        """Items the router has not confirmed, oldest first. Looking takes nothing."""
        rows = self.db.execute("SELECT seq, account, source, ref, data FROM items WHERE given = 0 ORDER BY seq LIMIT ?",
                               (limit,)).fetchall()
        return [{"seq": r[0], "account": r[1], "source": r[2], "ref": r[3], "data": json.loads(r[4])} for r in rows]

    def confirm(self, upto: int) -> int:
        """The router kept everything up to `upto`: those items are given out."""
        cursor = self.db.execute("UPDATE items SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return cursor.rowcount

    def event(self, account: str, calendar: str, event_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM events WHERE account = ? AND calendar = ? AND id = ?",
                              (account, calendar, event_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put_event(self, account: str, calendar: str, event: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?)",
                        (account, calendar, event["id"], json.dumps(event, ensure_ascii=False)))
        self.db.commit()

    def ok(self, account: str, source: str, now: float, since: float | None = None) -> None:
        self.db.execute("INSERT INTO status (account, source, since, last_ok) VALUES (?1, ?2, ?3, ?4) "
                        "ON CONFLICT (account, source) DO UPDATE SET last_ok = ?4, failing_since = NULL, error = NULL, "
                        "since = COALESCE(status.since, ?3)", (account, source, since or now, now))
        self.db.commit()

    def failed(self, account: str, source: str, error: str, now: float) -> None:
        self.db.execute("INSERT INTO status (account, source, failing_since, error) VALUES (?1, ?2, ?3, ?4) "
                        "ON CONFLICT (account, source) DO UPDATE SET error = ?4, "
                        "failing_since = COALESCE(status.failing_since, ?3)", (account, source, now, error[:200]))
        self.db.commit()

    def status(self) -> list[dict]:
        rows = self.db.execute("SELECT account, source, since, last_ok, failing_since, error FROM status "
                               "ORDER BY account, source").fetchall()
        return [dict(zip(("account", "source", "since", "last_ok", "failing_since", "error"), row)) for row in rows]


# --- Gmail ---------------------------------------------------------------------------------------------------------

async def sync_mail(state: State, account: google.Google, now: float) -> int:
    """New letters of one mailbox, in and out, as items. The first sync — and a full one after Gmail forgot the
    cursor — lists the letters since the mailbox's start (BACKFILL_DAYS before its first sync) and marks them
    `backfill`; a letter already an item is not added again. Returns how many were added."""
    name = account.key.account
    since = float(state.cursor(name, "mail.since") or now - BACKFILL_DAYS * 86400)
    state.set_cursor(name, "mail.since", repr(since))
    cursor, added = state.cursor(name, "mail.history"), 0
    if cursor is None:
        start = str((await account.profile())["historyId"])  # before the listing: what comes during it, history brings
        for box in BOXES:
            for message in await account.listed(box, since):
                added += state.add(name, "mail", message["id"], {"box": box, "thread": message.get("threadId", ""),
                                                                 "labels": [box], "backfill": True}, now)
        state.set_cursor(name, "mail.history", start)
        state.ok(name, "mail", now, since)
        return added
    reached = []
    for box in BOXES:
        try:
            found, at = await account.added(cursor, box)
        except google.Expired:
            log.warning("%s: Gmail no longer knows the history cursor; a full sync", name)
            state.set_cursor(name, "mail.history", None)
            return added + await sync_mail(state, account, now)
        reached.append(int(at))
        for message in found:
            added += state.add(name, "mail", message["id"], {"box": box, "thread": message.get("threadId", ""),
                                                             "labels": message.get("labelIds") or [box]}, now)
    # Each box answered with the mailbox's position at its own call: the earlier one is safe for both.
    state.set_cursor(name, "mail.history", str(min(reached)))
    state.ok(name, "mail", now, since)
    return added


# --- Calendar ------------------------------------------------------------------------------------------------------

def brief(event: dict) -> dict:
    """An event as it is kept and handed on: its times, its own text cut, who invited whom, and the owner's answer."""
    attendees = [a for a in event.get("attendees") or [] if isinstance(a, dict)]
    me = next((a for a in attendees if a.get("self")), {})
    organizer = event.get("organizer") or {}
    kept = {"id": str(event.get("id", "")), "status": str(event.get("status") or "confirmed"),
            "start": event.get("start") or {}, "end": event.get("end") or {}, "updated": str(event.get("updated", "")),
            "organizer": str(organizer.get("email") or ""), "mine": bool(organizer.get("self")),
            "answer": str(me.get("responseStatus") or ""), "attendees": len(attendees),
            "recurring": bool(event.get("recurrence") or event.get("recurringEventId"))}
    for field, cap in TEXT.items():
        kept[field] = str(event.get(field) or "")[:cap]
    return kept


def changed(before: dict | None, after: dict) -> str:
    """What a person would call the change: new, cancelled, time, place, title. Empty: nothing worth saying."""
    if after["status"] == "cancelled":
        return "cancelled" if before and before["status"] != "cancelled" else ""
    if before is None or before["status"] == "cancelled":
        return "new"
    what = [name for name, fields in (("time", ("start", "end")), ("place", ("location",)), ("title", ("summary",)))
            if any(before.get(f) != after.get(f) for f in fields)]
    return ",".join(what)


async def sync_calendar(state: State, account: google.Google, now: float) -> int:
    """Changed events of every calendar the account sees, as items with what changed. The first sync of a calendar
    takes its events from CALENDAR_BACK_DAYS ago on and marks them `first`; after Google forgot the token, a full
    sync is compared with what was seen, so an unchanged event is not an item again. Returns how many were added."""
    name, added = account.key.account, 0
    since = float(state.cursor(name, "calendar.since") or now)
    state.set_cursor(name, "calendar.since", repr(since))
    for calendar in await account.calendars():
        calendar_id = str(calendar.get("id", ""))
        key = f"calendar.{calendar_id}"
        token = state.cursor(name, key)
        try:
            events, next_token = await account.events(calendar_id, sync_token=token) if token else \
                await account.events(calendar_id, since=now - CALENDAR_BACK_DAYS * 86400)
        except google.Expired:
            log.warning("%s: Google no longer knows the sync token of a calendar; a full sync", name)
            events, next_token = await account.events(calendar_id, since=now - CALENDAR_BACK_DAYS * 86400)
        for event in events:
            if not event.get("id"):
                continue
            after = brief(event)
            change = changed(state.event(name, calendar_id, after["id"]), after)
            state.put_event(name, calendar_id, after)
            if change:
                added += state.add(name, "calendar", f"{calendar_id}/{after['id']}/{after['updated']}",
                                   {"calendar": calendar_id, "calendar_name": str(calendar.get("summary") or "")[:100],
                                    "change": change, "event": after, "first": token is None}, now)
        if next_token:
            state.set_cursor(name, key, next_token)
    state.ok(name, "calendar", now, since)
    return added


async def agenda(accounts: list[google.Google], start: float, end: float) -> tuple[list[dict], list[str]]:
    """The occurrences of every calendar between `start` and `end`, read live — the morning summary's day. The
    accounts that could not be read are named, not hidden."""
    found, failed = [], []
    for account in accounts:
        if account.key.kind != "calendar":
            continue
        try:
            for calendar in await account.calendars():
                for event in await account.agenda(str(calendar.get("id", "")), start, end):
                    found.append({**brief(event), "account": account.key.account,
                                  "calendar_name": str(calendar.get("summary") or "")[:100]})
        except google.GoogleError as exc:
            failed.append(f"{account.key.account}: {'нужен вход заново' if isinstance(exc, google.InvalidGrant) else exc}")
    return sorted(found, key=lambda e: str(e["start"].get("dateTime") or e["start"].get("date") or "")), failed


async def poll(state: State, accounts: list[google.Google], now: float | None = None) -> int:
    """One pass over every token: each mailbox and each account's calendars. A failure is that source's status and
    nothing else's. Returns how many items were added."""
    now = time.time() if now is None else now
    added = 0
    for account in accounts:
        source = "mail" if account.key.kind == "gmail" else "calendar"
        try:
            added += await (sync_mail if source == "mail" else sync_calendar)(state, account, now)
        except google.InvalidGrant:
            log.warning("%s %s: the token is dead (invalid_grant)", account.key.account, source)
            state.failed(account.key.account, source, "invalid_grant", now)
        except google.GoogleError as exc:
            log.warning("%s %s: %s", account.key.account, source, exc)
            state.failed(account.key.account, source, str(exc), now)
    return added


# --- the router's side of the hand-off ------------------------------------------------------------------------------

MAX_ATTACHMENT = 20 * 1024 * 1024  # what the router reads of a file at most (attachments.MAX_DOWNLOAD)


class Accounts:
    """The owner's tokens as Google clients, read from the keys folder on every call: a new login works without a
    restart, and an access token is reused while its refresh token stays the same."""

    def __init__(self, folder: str, http: httpx.AsyncClient, kinds: tuple[str, ...] = google.READ) -> None:
        self.folder, self.http, self.kinds = folder, http, kinds
        self.known: dict[tuple[str, str, str], google.Google] = {}

    def __call__(self) -> list[google.Google]:
        found = []
        for key in google.keys(self.folder, self.kinds):
            found.append(self.known.setdefault((key.account, key.kind, key.refresh_token), google.Google(key, self.http)))
        return found


class Service:
    """What the router may ask, on the internal network it shares with the collector alone, with their shared token:
    look at the items, confirm them up to a number, read one letter or one attachment of it, read the agenda, see how
    each source is doing. Nothing else: no listing of a mailbox, no search."""

    def __init__(self, state: State, accounts: Callable[[], list[google.Google]], token: str) -> None:
        if not token:
            raise SystemExit("the collector needs RETINUE_COLLECTOR_TOKEN: without it anyone on its network reads mail")
        self.state, self.accounts, self.token = state, accounts, token

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=1024 * 1024)
        app.add_routes([web.post("/items", self.items), web.post("/items/confirm", self.confirm),
                        web.post("/mail/letter", self.letter), web.post("/mail/attachment", self.attachment),
                        web.post("/agenda", self.agenda), web.get("/status", self.status)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, self.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _mailbox(self, name: str) -> google.Google | None:
        return next((a for a in self.accounts() if a.key.kind == "gmail" and a.key.account == name.lower()), None)

    async def items(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"items": self.state.look(min(int(body.get("limit") or 50), 200))})

    async def confirm(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"confirmed": self.state.confirm(int(body["upto"]))})

    async def letter(self, request: web.Request) -> web.Response:
        """One letter read from the mailbox now and parsed here: the router never parses MIME."""
        body = await request.json()
        mailbox = self._mailbox(str(body.get("account", "")))
        if mailbox is None:
            return web.json_response({"error": "нет ключа этого ящика"}, status=404)
        try:
            message = await mailbox.raw(str(body.get("id", "")))
        except google.Gone:
            return web.json_response({"gone": True}, status=404)
        except google.GoogleError as exc:
            return web.json_response({"error": "invalid_grant" if isinstance(exc, google.InvalidGrant) else str(exc)},
                                     status=503)
        read = letter.parse(message["raw"]).as_dict()
        read.update({"id": str(body["id"]), "labels": message["labels"], "thread": message["thread"],
                     "ts": message["ts"], "size": message["size"]})
        return web.json_response(read)

    async def attachment(self, request: web.Request) -> web.Response:
        """One attachment of a letter the router kept, by its number in `letter.parse`."""
        body = await request.json()
        mailbox = self._mailbox(str(body.get("account", "")))
        if mailbox is None:
            return web.json_response({"error": "нет ключа этого ящика"}, status=404)
        try:
            message = await mailbox.raw(str(body.get("id", "")))
            name, media_type, data = letter.attachment(message["raw"], int(body.get("part", -1)))
        except (google.Gone, KeyError, ValueError):
            return web.json_response({"gone": True}, status=404)
        except google.GoogleError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if len(data) > MAX_ATTACHMENT:
            return web.json_response({"error": f"больше {MAX_ATTACHMENT // 2**20} МБ", "size": len(data)}, status=413)
        return web.json_response({"name": name, "type": media_type, "data": base64.b64encode(data).decode()})

    async def agenda(self, request: web.Request) -> web.Response:
        body = await request.json()
        events, failed = await agenda(self.accounts(), float(body["start"]), float(body["end"]))
        return web.json_response({"events": events, "failed": failed})

    async def status(self, request: web.Request) -> web.Response:
        """How each source is doing, and which tokens the collector holds — names, never values."""
        keys = [{"account": a.key.account, "kind": a.key.kind} for a in self.accounts()]
        return web.json_response({"sources": self.state.status(), "keys": keys})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = CollectorConfig.load()
    # trust_env: HTTPS_PROXY is the collector's proxy, which lets through Google's three hosts and nothing else.
    accounts = Accounts(cfg.keys, httpx.AsyncClient(trust_env=True))
    state = State(cfg.state)

    async def run() -> None:
        runner = web.AppRunner(Service(state, accounts, cfg.token).app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("collector on :%d, %d keys", cfg.listen_port, len(accounts()))
        while True:
            try:
                added = await poll(state, accounts())
                if added:
                    log.info("%d new items", added)
            except Exception:  # one bad pass must not end the collector
                log.exception("poll failed")
            await asyncio.sleep(POLL_S * random.uniform(0.85, 1.15))  # Google asks clients not to poll in step

    asyncio.run(run())


if __name__ == "__main__":
    main()
