"""The mail sender: code, no model, the only holder of the owner's `gmail.send` tokens. It sends one letter when the
router asks with a key, the envelope and its digest — the envelope the owner pressed «Отправить» under — and nothing
else: no listing, no reading (the scope reads nothing), no second send of a key (`courier.send_once`).

Its own container, its own user, its own keys folder: the collector, which parses strangers' letters, never holds a
key that sends (research 48, 60). Its way out is the mail proxy: Google's hosts only.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Callable

import httpx
from aiohttp import web

from . import courier, google
from .config import SenderConfig

log = logging.getLogger("retinue.sender")


class Service:
    """What the router may ask, on the internal network it shares with the sender alone, with their token: send one
    letter, and say which mailboxes it can send from and which of them Google refuses."""

    def __init__(self, ledger: courier.Ledger, accounts: Callable[[], list[google.Google]], token: str) -> None:
        if not token:
            raise SystemExit("the sender needs RETINUE_SENDER_TOKEN: without it anyone on its network sends mail")
        self.ledger, self.accounts, self.token = ledger, accounts, token
        self.dead: dict[str, str] = {}  # mailbox -> invalid_grant: the owner must log in again

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._signed], client_max_size=256 * 1024)
        app.add_routes([web.post("/send", self.send), web.get("/status", self.status)])
        return app

    @web.middleware
    async def _signed(self, request: web.Request, handler):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, self.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _gmail(self, envelope: courier.Envelope, key: str) -> tuple[str, dict]:
        mailbox = next((a for a in self.accounts() if a.key.account == envelope.account), None)
        if mailbox is None:
            return "failed", {"error": f"нет ключа отправки для {envelope.account}"}
        try:
            answer = await mailbox.send(courier.mail_bytes(envelope, key), envelope.thread)
        except google.InvalidGrant:
            self.dead[envelope.account] = "invalid_grant"
            return "failed", {"error": "invalid_grant"}
        except google.Ambiguous as exc:
            return "unknown", {"error": str(exc)}
        except (google.GoogleError, courier.Refused) as exc:
            return "failed", {"error": str(exc)}
        self.dead.pop(envelope.account, None)
        return "sent", {"id": str(answer.get("id") or ""), "thread": str(answer.get("threadId") or "")}

    async def send(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        done = await courier.send_once(self.ledger, body, courier.MAIL, self._gmail)
        log.info("send %s: %s", str(body.get("key", ""))[:8] if isinstance(body, dict) else "", done["status"])
        return web.json_response(done, status=400 if done["status"] == "refused" else 200)

    async def status(self, request: web.Request) -> web.Response:
        """The mailboxes it holds a key for — names, never values — and those Google refused."""
        return web.json_response({"accounts": sorted(a.key.account for a in self.accounts()), "dead": self.dead,
                                  "day": self.ledger.since(time.time() - courier.DAY)})


def main() -> None:
    from .collector import Accounts

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = SenderConfig.load()
    # trust_env: HTTPS_PROXY is the mail proxy, which lets through Google's hosts and nothing else.
    accounts = Accounts(cfg.keys, httpx.AsyncClient(trust_env=True), kinds=("send",))
    service = Service(courier.Ledger(cfg.state), accounts, cfg.token)

    async def run() -> None:
        runner = web.AppRunner(service.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("sender on :%d, %d keys", cfg.listen_port, len(accounts()))
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
