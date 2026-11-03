"""The calendar writer: code, no model, the only holder of the owner's `calendar.app.created` token. It keeps one
calendar of his, «Retinue», which it made itself, and in it one event per block of his plan — when the router asks,
built from a closed set of fields: the task's title, the block's time, busy or free, a link to the app, the block's
id in the event's private properties. No guests, no conference, no notification to anyone (`sendUpdates=none`):
the token cannot invite people or touch his other calendars. And it tells the router what changed there — his own
moves and deletions in Google, which the router carries back to the blocks (docs/calendar.md).

Its own container, its own user, its own keys folder: the collector, which reads strangers' invitations, holds no key
that writes. Its way out is the mail proxy: Google's hosts only. What it keeps is the calendar's id; the router keeps
the rest.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
from aiohttp import web

from . import google
from .config import CalwriteConfig

log = logging.getLogger("retinue.calwrite")

SUMMARY = "Retinue"
BLOCK = re.compile(r"[0-9A-Za-z_-]{2,64}")
EVENT = re.compile(r"rt((?:[0-9a-f]{2}){2,64})")
ZONE = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}|UTC")
TITLE_CHARS = 200
LINK_CHARS = 300
MINUTES_MAX = 24 * 60
BACK_S = 2 * 86400   # a full read takes the events from this long ago on: the router keeps no older ones
PRIVATE = "retinue_block"  # the block on the event as well, for the owner's other apps and the collector


def event_id(block: str) -> str:
    """The block's event id in Google's alphabet (a–v, 0–9): `rt` and the id's bytes in hex — the same on every
    try, so an insert that went once is not a second event."""
    return "rt" + block.encode().hex()


def block_of(event: object) -> str | None:
    """The block an event id is, decoded back — the only way an event is known as a block's: Google may send a
    deleted event with its id alone, and a copy the owner made has another id."""
    found = EVENT.fullmatch(str(event or ""))
    if not found:
        return None
    try:
        block = bytes.fromhex(found.group(1)).decode()
    except UnicodeDecodeError:
        return None
    return block if BLOCK.fullmatch(block) else None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(round(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def moment(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def brief(event: dict) -> dict | None:
    """One of its events as the router compares it: the block it is, its time, whether it is still there, its etag.
    None: an event the owner made in this calendar himself — not a block's, not touched."""
    block = block_of(event.get("id"))
    if block is None:
        return None
    kept = {"block": block, "event": str(event.get("id") or ""), "etag": str(event.get("etag") or ""),
            "status": "cancelled" if event.get("status") == "cancelled" else "confirmed"}
    start = moment((event.get("start") or {}).get("dateTime"))
    end = moment((event.get("end") or {}).get("dateTime"))
    if start is not None and end is not None and end > start:
        kept.update(start=iso(start), minutes=max(1, round((end - start) / 60)))
    return kept


class Refused(Exception):
    """The router's request is not one it makes: nothing written."""


def ask_of(body: object) -> dict:
    """What a block's event is made of, checked: anything else in the request is not read."""
    if not isinstance(body, dict):
        raise Refused("not an object")
    block, title, start = body.get("block"), body.get("title"), moment(body.get("start"))
    minutes, zone, link = body.get("minutes"), body.get("zone"), body.get("link") or ""
    if not isinstance(block, str) or not BLOCK.fullmatch(block):
        raise Refused("block: wrong")
    if not isinstance(title, str) or not title.strip():
        raise Refused("title: wrong")
    if start is None:
        raise Refused("start: wrong")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MINUTES_MAX:
        raise Refused("minutes: wrong")
    if not isinstance(zone, str) or not ZONE.fullmatch(zone):
        raise Refused("zone: wrong")
    if not isinstance(link, str) or (link and not link.startswith("https://")):
        raise Refused("link: wrong")
    etag = body.get("etag") or ""
    if not isinstance(etag, str) or len(etag) > 100:
        raise Refused("etag: wrong")
    return {"block": block, "title": " ".join(title.split())[:TITLE_CHARS], "start": start, "minutes": minutes,
            "busy": body.get("busy") is True, "zone": zone, "link": link[:LINK_CHARS], "etag": etag,
            "revive": body.get("revive") is True, "calendar": str(body.get("calendar") or "")[:200]}


def fields(ask: dict) -> dict:
    """The event's own fields — the only ones it ever writes."""
    end = ask["start"] + ask["minutes"] * 60
    text = "Planned by Retinue. Move it or delete it here, and the plan follows."
    return {"summary": ask["title"], "description": f"{text}\n{ask['link']}" if ask["link"] else text,
            "start": {"dateTime": iso(ask["start"]), "timeZone": ask["zone"]},
            "end": {"dateTime": iso(end), "timeZone": ask["zone"]},
            "transparency": "opaque" if ask["busy"] else "transparent",
            "extendedProperties": {"private": {PRIVATE: ask["block"]}}}


class Service:
    """What the router may ask, on the internal network it shares with the writer alone, with their token: put a
    block's event, drop it, tell what changed in the calendar, say how the calendar and the key stand."""

    def __init__(self, state: str | Path, accounts: Callable[[], list[google.Google]], token: str) -> None:
        if not token:
            raise SystemExit("the calendar writer needs RETINUE_CALWRITE_TOKEN: without it anyone on its network writes")
        self.path, self.accounts, self.token = Path(state), accounts, token
        self.dead = ""         # invalid_grant: the owner must sign in again
        self.lock = asyncio.Lock()

    # --- its one calendar -----------------------------------------------------------------------------------------

    def _state(self) -> dict:
        """What it keeps; none yet is {} — a file it cannot read is a refusal, never a calendar to make anew."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise Refused("its state is unreadable") from None
        if not isinstance(data, dict):
            raise Refused("its state is unreadable")
        return data

    def _keep(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".new")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def _account(self) -> google.Google:
        found = self.accounts()
        if len(found) != 1:
            raise Refused("no calendar key" if not found else "more than one calendar key: one account writes")
        return found[0]

    async def _adopt(self, account: google.Google, hint: str) -> str:
        """Its state lost, the calendar the router names taken back when Google still has it: no second «Retinue»."""
        if not hint or len(hint) > 200:
            return ""
        try:
            await account.calendar(hint)
        except google.Gone:
            return ""
        self._keep({"calendar": hint, "account": account.key.account})
        log.info("took back the calendar %s", SUMMARY)
        return hint

    async def _calendar(self, account: google.Google, zone: str, hint: str = "") -> str:
        """Its calendar's id: made at the first write. One the owner deleted stays deleted — that was his say —
        until the router asks to make it again (`/calendar`)."""
        state = self._state()
        if state.get("gone"):
            raise Refused("calendar deleted")
        if state.get("calendar"):
            return str(state["calendar"])
        if adopted := await self._adopt(account, hint):
            return adopted
        made = await account.make_calendar(SUMMARY, zone)
        self._keep({"calendar": str(made["id"]), "account": account.key.account})
        log.info("made the calendar %s", SUMMARY)
        return str(made["id"])

    async def _gone(self, account: google.Google, calendar: str) -> bool:
        """Whether a 404 was the calendar's, not the event's: then it is marked deleted."""
        try:
            await account.calendar(calendar)
            return False
        except google.Gone:
            self._keep({**self._state(), "gone": True})
            log.warning("the calendar %s was deleted in Google", SUMMARY)
            return True

    # --- the routes -----------------------------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=64 * 1024)
        app.add_routes([web.post("/put", self.put), web.post("/drop", self.drop), web.post("/changes", self.changes),
                        web.post("/calendar", self.again), web.get("/status", self.status)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, self.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            async with self.lock:  # one write at a time: the calendar is made once
                return await handler(request)
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except google.InvalidGrant:
            self.dead = "invalid_grant"
            return web.json_response({"error": "invalid_grant"}, status=400)
        except google.GoogleError as exc:
            return web.json_response({"error": str(exc)[:200]}, status=400)

    @staticmethod
    async def _body(request: web.Request) -> object:
        try:
            return await request.json()
        except ValueError:
            return None

    async def put(self, request: web.Request) -> web.Response:
        """A block's event as asked: made when the router has no etag for it, else changed against that etag. Its
        answer — the event as Google keeps it now; `conflict` with the event when the owner changed it since (his
        edit stands, the router takes it); `gone` when he deleted it."""
        ask = ask_of(await self._body(request))
        account = self._account()
        calendar = await self._calendar(account, ask["zone"], ask["calendar"])
        if ask["calendar"] and ask["calendar"] != calendar:
            ask["etag"] = ""  # an etag of another calendar's event: here it is made, not changed
        eid, body = event_id(ask["block"]), fields(ask)
        try:
            if not ask["etag"]:
                try:
                    made = await account.insert(calendar, {"id": eid, "reminders": {"useDefault": True}, **body})
                except google.Exists:  # it went before, or it was deleted: what is there says
                    there = await account.event(calendar, eid)
                    if there.get("status") == "cancelled" and ask["revive"]:
                        # Its block came back to the plan after the router took the event off: the same event back.
                        done = await account.update(calendar, eid, {**there, **body, "status": "confirmed"},
                                                    str(there.get("etag") or ""))
                        self.dead = ""
                        return web.json_response({"event": brief(done), "calendar": calendar})
                    if there.get("status") == "cancelled":
                        return web.json_response({"gone": True, "calendar": calendar})
                    return web.json_response({"conflict": brief(there), "calendar": calendar})
                self.dead = ""
                return web.json_response({"event": brief(made), "calendar": calendar})
            there = await account.event(calendar, eid)
            if there.get("status") == "cancelled":
                return web.json_response({"gone": True, "calendar": calendar})
            if str(there.get("etag") or "") != ask["etag"]:
                return web.json_response({"conflict": brief(there), "calendar": calendar})
            # What the owner set in Google beyond these fields — his colour, his reminders — stays as it is.
            done = await account.update(calendar, eid, {**there, **body}, ask["etag"])
        except google.Conflict:
            return web.json_response({"conflict": brief(await account.event(calendar, eid)), "calendar": calendar})
        except google.Gone:
            if await self._gone(account, calendar):
                raise Refused("calendar deleted") from None
            return web.json_response({"gone": True, "calendar": calendar})
        self.dead = ""
        return web.json_response({"event": brief(done), "calendar": calendar})

    async def drop(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        block = body.get("block") if isinstance(body, dict) else None
        if not isinstance(block, str) or not BLOCK.fullmatch(block):
            raise Refused("block: wrong")
        account = self._account()
        state = self._state()
        if state.get("calendar") and not state.get("gone"):
            await account.delete(str(state["calendar"]), event_id(block))
        return web.json_response({"ok": True})

    async def changes(self, request: web.Request) -> web.Response:
        """The block events changed since the router's `token` — his moves and deletions, the writer's own echoes
        too — the next token, and the calendar's id (another id than the router knew: another calendar, nothing of
        the old one holds); without a token (or one Google forgot), every event from BACK_S ago on, `full` — a full
        read leaves the deleted out, so nothing missing from it is taken for deleted."""
        body = await self._body(request)
        token = body.get("token") if isinstance(body, dict) else None
        hint = str(body.get("calendar") or "") if isinstance(body, dict) else ""
        account = self._account()
        state = self._state()
        if not state and await self._adopt(account, hint):
            state = self._state()
        if state.get("gone"):
            return web.json_response({"events": [], "token": "", "full": False, "calendar": "", "gone": True})
        if not state.get("calendar"):
            return web.json_response({"events": [], "token": "", "full": False, "calendar": "", "gone": False})
        calendar, full = str(state["calendar"]), not token
        try:
            if token:
                events, next_token = await account.events(calendar, sync_token=str(token))
            else:
                events, next_token = await account.events(calendar, since=time.time() - BACK_S)
        except google.Expired:
            events, next_token, full = *await account.events(calendar, since=time.time() - BACK_S), True
        except google.Gone:
            if await self._gone(account, calendar):
                return web.json_response({"events": [], "token": "", "full": False, "calendar": "", "gone": True})
            raise
        self.dead = ""
        return web.json_response({"events": [b for b in (brief(e) for e in events) if b], "token": next_token,
                                  "full": full, "calendar": calendar, "gone": False})

    async def again(self, request: web.Request) -> web.Response:
        """The owner asked for the calendar back: the next write makes a new one. Only after it was deleted — an old
        button pressed later makes no second calendar."""
        if not self._state().get("gone"):
            raise Refused("the calendar is there")
        self._keep({})
        return web.json_response({"ok": True})

    async def status(self, request: web.Request) -> web.Response:
        state = self._state()
        found = self.accounts()
        return web.json_response({"accounts": sorted(a.key.account for a in found), "dead": self.dead,
                                  "calendar": bool(state.get("calendar")), "gone": bool(state.get("gone"))})


def main() -> None:
    from .collector import Accounts

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = CalwriteConfig.load()
    accounts = Accounts(cfg.keys, httpx.AsyncClient(trust_env=True), kinds=("calwrite",))
    service = Service(cfg.state, accounts, cfg.token)

    async def run() -> None:
        runner = web.AppRunner(service.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("calendar writer on :%d, %d keys", cfg.listen_port, len(accounts()))
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
