"""Router entry point: the core plus the channels the owner talks through.

The core (`core.py`) keeps one conversation per agent and talks to agents over A2A. Each channel adapter
(`channels/`) turns a messenger into core calls. Every channel is optional: it runs when its section is in
the config.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import subprocess
from urllib.parse import urlsplit

from .archive import Archive
from .bus import BusServer, bus_token
from .channels.telegram import TelegramChannel
from .config import RouterConfig
from .core import Core, ask_agent
from .lifehub import Data, Lifehub
from .mail import Collector, MailStore
from .mailroom import Mailroom
from .outbox import Neighbour, Outbox, OutboxStore
from .memory import Memory
from .protocol import Store
from .speech import Scribe
from .travel import TravelOps


def build_channels(cfg: RouterConfig, store: Store, loop: asyncio.AbstractEventLoop) -> list:
    channels = []
    if cfg.matrix:
        # Imported only here: without a `matrix:` section the router never loads the Matrix library.
        from .channels.matrix import MatrixChannel
        channels.append(MatrixChannel(cfg.matrix, cfg.agents, store, loop, f"{cfg.state_db}.mx-state.json"))
    if cfg.telegram:
        # A trip page of the life hub is clickable too, and nothing else of it: the path is under /trips/.
        hub = [f"{urlsplit(cfg.lifehub_url).hostname}/trips/"] if cfg.lifehub_url else []
        channels.append(TelegramChannel(cfg.telegram, store, [*cfg.link_hosts, *hub]))
    return channels


def build_memory(cfg: RouterConfig, db: sqlite3.Connection) -> tuple[Memory | None, str]:
    """The knowledge base's working copy, made ready from the hub. A hub that does not answer does not stop the
    router: the owner is told, and her turns are not committed until he fixes it and restarts the router."""
    if not cfg.memory:
        return None, ""
    try:
        memory = Memory(cfg.memory, db)
        memory.prepare()
        return memory, ""
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        logging.getLogger("retinue.router").exception("the knowledge base is not ready")
        said = (getattr(exc, "stderr", "") or str(exc)).strip().splitlines()
        why = said[-1][:200] if said else type(exc).__name__
        return None, (f"База знаний не подключена: хаб {cfg.memory.hub} не ответил ({why}). "
                      "Её правки не сохраняются, пока Роутер не перезапущен с рабочим хабом.")


def signed(tokens: dict[str, str]):
    """`ask_agent` with each agent's own bus token: its host answers nothing else."""
    async def ask(url, *args, **kwargs):
        return await ask_agent(url, *args, token=tokens.get(url, ""), **kwargs)
    return ask


def main() -> None:
    parser = argparse.ArgumentParser(description="Retinue router")
    parser.add_argument("--config", default="/config/router.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The homeserver sends hs_token in the query string; the access log would write it to disk.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # httpx logs every request URL, and Bot API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = RouterConfig.load(args.config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store = Store(cfg.state_db)
    tokens = {a.url: bus_token(cfg.bus_secret, a.id) for a in cfg.agents} if cfg.bus_secret else {}
    memory, memory_error = build_memory(cfg, store.db)
    gateway = Neighbour(cfg.gateway_url, cfg.gateway_token, "шлюз Telegram") if cfg.gateway_url else None
    mail = Mailroom(Collector(cfg.collector_url, cfg.collector_token) if cfg.collector_url else None, MailStore(store.db),
                    gateway=gateway) if cfg.collector_url or gateway else None
    sender = Neighbour(cfg.sender_url, cfg.sender_token, "отправка почты") if cfg.sender_url else None
    # Replies to people: the mail sender and the gateway hold the keys; the router holds the drafts and the «да».
    outbox = Outbox(OutboxStore(store.db), sender=sender, gateway=gateway,
                    owner_tg=cfg.telegram.owner_id if cfg.telegram else None) if sender or gateway else None
    lifehub = Lifehub(Data(cfg.lifehub_data), cfg.lifehub_url, cfg.link_hosts, kb=cfg.memory.tree if memory else "",
                      build_status=cfg.lifehub_build) if cfg.lifehub_url else None
    core = Core(cfg.agents, store, cfg.owner, ask=signed(tokens), archive=Archive(cfg.archive_db),
                default_agent=cfg.default_agent,
                tz=cfg.owner_tz, backup_status=cfg.backup_status, scribe=Scribe(cfg.stt_key, store=store),
                travel=TravelOps(cfg.travel_url) if cfg.travel_url else None, memory=memory, mail=mail,
                lifehub=lifehub, outbox=outbox)
    loop.run_until_complete(core.start(build_channels(cfg, store, loop)))
    if memory_error:
        loop.run_until_complete(core.tell_owner(memory_error))
    ticking = loop.create_task(core.clock())  # reminders, the morning summary, retries after the limit
    if cfg.bus_secret:
        loop.run_until_complete(BusServer(core, cfg.bus_secret, cfg.bus_listen_port).start())
    loop.run_forever()


if __name__ == "__main__":
    main()
