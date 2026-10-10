"""The Telegram Business gateway: code, no model, the only holder of the Business bot's token. That token both reads
and replies (research 49), so the gateway is one process: it takes the messages of the chats the owner chose in
Telegram's settings and hands them to the router by number, like the mail collector; it sends one reply when the
router asks with a key, the envelope and its digest — the envelope the owner pressed «Отправить» under — and never
the same key twice (`courier.send_once`).

Whose messages: only the owner's connection counts (`user.id` — the connection's id changes when he edits the bot's
settings). In those chats a message from him is his own, one with `sender_business_bot` is a reply this bot sent,
anything else is the other person's: data for the router, never a command. Group, secret and other chats never
reach a business bot; history before the connection never does either.

A file the other person sent — a photo, a document, a voice note, audio, a video — is not read here: the router asks
for it by its id (`/file`), only for an id that came in an item, and reads it with its own worker, like the owner's
files. The gateway only downloads it from Telegram, up to the Bot API's 20 MB.

A reply goes only where Telegram allows it: the owner's connection enabled with the right to reply, and an incoming
message in that chat within 24 hours. Outside the window the router offers the owner a deep link instead.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import sqlite3
import time
from pathlib import Path

import httpx
from aiohttp import web

from . import courier
from .config import GatewayConfig

log = logging.getLogger("retinue.tgbusiness")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
UPDATES = ["business_connection", "business_message", "edited_business_message"]
WINDOW_S = 24 * 3600      # can_reply: «private chats that had incoming messages in the last 24 hours»
WINDOW_MARGIN_S = 120     # a reply this close to the end of the window is not tried
TEXT_CHARS = 4096
MEDIA = ("photo", "video", "voice", "audio", "document", "video_note", "sticker", "animation", "contact", "location")
FILES = ("photo", "document", "voice", "audio", "video", "video_note")  # what the router reads, as the owner's own (8б)
MAX_FILE = 20 * 1024 * 1024  # getFile serves bots files up to 20 MB


class TelegramError(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(f"{code} {text}")
        self.code = code


class State:
    """The gateway's own SQLite: the update offset, the owner's connection, the items the router has not confirmed,
    and when each chat last had an incoming message (the 24-hour window)."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ref TEXT NOT NULL UNIQUE,          -- <chat>/<message>, an edit: <chat>/<message>/e<edit_date>
                data TEXT NOT NULL,
                given INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chats (chat TEXT PRIMARY KEY, last_in REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY);  -- the file ids items carried: the router's to ask
            """)
        self.db.commit()

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.db.commit()

    def connection(self) -> dict | None:
        value = self.get("connection")
        return json.loads(value) if value else None

    def add(self, ref: str, data: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO items (ref, data) VALUES (?, ?)", (ref, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def look(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT seq, ref, data FROM items WHERE given = 0 ORDER BY seq LIMIT ?", (limit,))
        return [{"seq": seq, "account": "telegram", "source": "telegram", "ref": ref, "data": json.loads(data)}
                for seq, ref, data in rows]

    def confirm(self, upto: int) -> int:
        cursor = self.db.execute("UPDATE items SET given = 1 WHERE seq <= ? AND given = 0", (upto,))
        self.db.commit()
        return cursor.rowcount

    def allow(self, file_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO files VALUES (?)", (file_id,))
        self.db.commit()

    def known(self, file_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is not None

    def saw_in(self, chat: str, ts: float) -> None:
        self.db.execute("INSERT INTO chats VALUES (?1, ?2) ON CONFLICT (chat) DO UPDATE SET last_in = MAX(last_in, ?2)",
                        (chat, ts))
        self.db.commit()

    def last_in(self, chat: str) -> float | None:
        row = self.db.execute("SELECT last_in FROM chats WHERE chat = ?", (chat,)).fetchone()
        return row[0] if row else None


def item_of(message: dict, owner: int) -> tuple[str, dict] | None:
    """A business message as the router gets it: whose it is, the chat, the text or the caption, what else it carried.
    None for anything but a private chat."""
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int) or "message_id" not in message:
        return None
    if message.get("sender_business_bot"):
        direction = "bot"       # a reply this bot sent: the courier's, confirmed by the owner
    elif (message.get("from") or {}).get("id") == owner:
        direction = "own"       # the owner wrote it himself
    else:
        direction = "in"
    edit = int(message.get("edit_date") or 0)
    ref = f"{chat['id']}/{message['message_id']}" + (f"/e{edit}" if edit else "")
    kind = next((key for key in FILES if key in message), "") if "animation" not in message else ""
    found = {}
    if kind and direction == "in" and not edit:  # the other person's file: the router reads it; his own he has
        item = (message[kind] or [{}])[-1] if kind == "photo" else message[kind]  # a photo: sizes, the largest last
        if isinstance(item, dict) and item.get("file_id"):
            found = {"file": {"kind": kind, "id": str(item["file_id"]), "name": str(item.get("file_name") or "")[:200],
                              "type": str(item.get("mime_type") or ""), "size": int(item.get("file_size") or 0),
                              "duration": int(item.get("duration") or 0)}}
    return ref, {**found, "chat": str(chat["id"]), "message_id": int(message["message_id"]), "date": int(message.get("date") or 0),
                 "edit": edit, "direction": direction, "username": str(chat.get("username") or ""),
                 "name": " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))[:120],
                 "text": str(message.get("text") or message.get("caption") or "")[:TEXT_CHARS],
                 "media": next((key for key in MEDIA if key in message), ""),
                 "reply_to": int((message.get("reply_to_message") or {}).get("message_id") or 0),
                 "offline": bool(message.get("is_from_offline"))}


class Gateway:
    def __init__(self, state: State, token: str, owner: int, http: httpx.AsyncClient | None = None) -> None:
        self.state, self.token, self.owner = state, token, owner
        self.http = http or httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)
        self.seen = 0.0  # the last update taken

    async def call(self, method: str, **params) -> dict:
        response = await self.http.post(f"{API}/bot{self.token}/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(int(data.get("error_code") or response.status_code), str(data.get("description")))
        return data["result"]

    def _keep(self, connection: dict) -> bool:
        """The owner's connection, kept with its rights; anybody else's is ignored."""
        if (connection.get("user") or {}).get("id") != self.owner:
            log.warning("a business connection of another user: ignored")
            return False
        rights = sorted(name for name, on in (connection.get("rights") or {}).items() if on is True)
        self.state.set("connection", json.dumps({"id": str(connection.get("id") or ""), "rights": rights,
                                                 "enabled": bool(connection.get("is_enabled"))}))
        return True

    async def ours(self, connection_id: str) -> bool:
        current = self.state.connection()
        if current and current["id"] == connection_id:
            return current["enabled"]
        try:  # a new id after the owner edited the settings: Telegram says whose it is
            connection = await self.call("getBusinessConnection", business_connection_id=connection_id)
        except TelegramError:
            return False
        return self._keep(connection) and bool(connection.get("is_enabled"))

    async def consume(self, update: dict) -> None:
        """One update, then the offset past it: a restart takes it again, and an item is kept once by its ref."""
        if connection := update.get("business_connection"):
            self._keep(connection)
        for key in ("business_message", "edited_business_message"):
            message = update.get(key)
            if not message or not await self.ours(str(message.get("business_connection_id") or "")):
                continue
            if found := item_of(message, self.owner):
                ref, data = found
                if data["direction"] == "in" and not data["edit"]:
                    self.state.saw_in(data["chat"], data["date"])
                if "file" in data:
                    self.state.allow(data["file"]["id"])
                self.state.add(ref, data)
        self.state.set("offset", str(int(update["update_id"]) + 1))
        self.seen = time.time()

    async def poll(self) -> None:
        while True:
            try:
                updates = await self.call("getUpdates", offset=int(self.state.get("offset") or 0),
                                          timeout=POLL_TIMEOUT_S, allowed_updates=UPDATES)
            except Exception as exc:  # worded by its type: a Bot API address carries the token
                log.warning("getUpdates failed: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            for update in updates:
                try:
                    await self.consume(update)
                except Exception:
                    log.exception("update %s failed", update.get("update_id"))
                    self.state.set("offset", str(int(update["update_id"]) + 1))

    async def deliver(self, envelope: courier.Envelope, key: str, now: float | None = None) -> tuple[str, dict]:
        """The transport of `send_once`: one sendMessage on the owner's behalf, as plain text with no preview, as a
        reply to the message it answers — and not at all if that message is gone."""
        now = time.time() if now is None else now
        connection = self.state.connection()
        if envelope.account != str(self.owner) or not connection or not connection["enabled"]:
            return "failed", {"error": "бот не подключён к аккаунту Владельца"}
        if "can_reply" not in connection["rights"]:
            return "failed", {"error": "у бота нет права отвечать"}
        last = self.state.last_in(envelope.to)
        if last is None or now - last > WINDOW_S - WINDOW_MARGIN_S:
            return "failed", {"error": "window"}
        params = {"business_connection_id": connection["id"], "chat_id": int(envelope.to), "text": envelope.text,
                  "link_preview_options": {"is_disabled": True}}
        if envelope.reply_to_message:
            params["reply_parameters"] = {"message_id": envelope.reply_to_message, "allow_sending_without_reply": False}
        try:
            sent = await self.call("sendMessage", **params)
        except TelegramError as exc:
            return ("unknown" if exc.code >= 500 else "failed"), {"error": str(exc)[:200]}
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return "failed", {"error": f"сеть: {type(exc).__name__}"}
        except httpx.HTTPError as exc:
            return "unknown", {"error": f"сеть после отправки запроса: {type(exc).__name__}"}
        return "sent", {"id": str(sent.get("message_id") or "")}


async def _fetch(gateway: Gateway, file_id: str) -> bytes:
    """getFile, then the bytes from Telegram's file host. Errors are worded without the address: it carries the
    token."""
    found = await gateway.call("getFile", file_id=file_id)
    if int(found.get("file_size") or 0) > MAX_FILE:
        raise TelegramError(413, "file is too big")
    try:
        response = await gateway.http.get(f"{API}/file/bot{gateway.token}/{found['file_path']}", timeout=120)
    except httpx.HTTPError as exc:
        raise TelegramError(502, f"file: {type(exc).__name__}") from None
    if response.status_code != 200 or len(response.content) > MAX_FILE:
        raise TelegramError(response.status_code, "file: not served")
    return response.content


class Service:
    """What the router may ask, on the network it shares with the gateway alone, with their token: look at the
    items, confirm them, send one reply, see the connection — its rights by name, never the token."""

    def __init__(self, gateway: Gateway, ledger: courier.Ledger, token: str) -> None:
        if not token:
            raise SystemExit("the gateway needs RETINUE_GATEWAY_TOKEN: without it anyone on its network replies")
        self.gateway, self.ledger, self.token = gateway, ledger, token

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=256 * 1024)  # requests; answers are larger
        app.add_routes([web.post("/items", self.items), web.post("/items/confirm", self.confirm),
                        web.post("/send", self.send), web.post("/file", self.file), web.get("/status", self.status)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, self.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def items(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"items": self.gateway.state.look(min(int(body.get("limit") or 50), 200))})

    async def confirm(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"confirmed": self.gateway.state.confirm(int(body["upto"]))})

    async def send(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        done = await courier.send_once(self.ledger, body, courier.TELEGRAM, self.gateway.deliver)
        return web.json_response(done, status=400 if done["status"] == "refused" else 200)

    async def file(self, request: web.Request) -> web.Response:
        """One file of the other person's, by the id an item carried — no other id is fetched."""
        file_id = str((await request.json()).get("id") or "")
        if not self.gateway.state.known(file_id):
            return web.json_response({"error": "такого файла не было в сообщениях"}, status=400)
        try:
            data = await _fetch(self.gateway, file_id)
        except (TelegramError, KeyError) as exc:
            return web.json_response({"error": f"Telegram не отдал файл ({exc})"}, status=400)
        return web.json_response({"data": base64.b64encode(data).decode()})

    async def status(self, request: web.Request) -> web.Response:
        connection = self.gateway.state.connection()
        return web.json_response({"connected": bool(connection and connection["enabled"]),
                                  "rights": connection["rights"] if connection else [], "seen": self.gateway.seen,
                                  "day": self.ledger.since(time.time() - courier.DAY)})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # every request URL carries the bot token
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = GatewayConfig.load()
    gateway = Gateway(State(cfg.state), cfg.bot_token, cfg.owner_id)
    service = Service(gateway, courier.Ledger(cfg.ledger), cfg.token)

    async def run() -> None:
        runner = web.AppRunner(service.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("telegram business gateway on :%d", cfg.listen_port)
        await gateway.poll()

    asyncio.run(run())


if __name__ == "__main__":
    main()
