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

from . import attachments
from .archive import Archive
from .bridge import PlatformBridge, PlatformClient
from .bus import BusServer, bus_token
from .channels.telegram import TelegramChannel
from .config import RouterConfig
from .core import Core, ask_agent
from .health import Watch
from .pages import Data, Pages
from .mail import Collector, MailStore
from .mailroom import Mailroom
from .outbox import Neighbour, Outbox, OutboxStore
from .memory import Memory
from .proactive import Proactive
from .protocol import Store
from .speech import Scribe
from .travel import TravelOps
from .waiting import chips, counts, detail, digest, press


def build_channels(cfg: RouterConfig, store: Store, loop: asyncio.AbstractEventLoop,
                   bridge: PlatformBridge | None = None) -> list:
    channels = []
    if cfg.matrix:
        # Imported only here: without a `matrix:` section the router never loads the Matrix library.
        from .channels.matrix import MatrixChannel
        channels.append(MatrixChannel(cfg.matrix, cfg.agents, store, loop, f"{cfg.state_db}.mx-state.json"))
    if cfg.telegram:
        # An address of the owner's pages is clickable too: a trip page she published, a page he sent. The host serves
        # nothing else.
        pages = [f"{urlsplit(cfg.pages_url).hostname}/"] if cfg.pages_url else []
        channels.append(TelegramChannel(cfg.telegram, store, [*cfg.link_hosts, *pages], bridge))
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
        return None, (f"The Base is not connected: the hub {cfg.memory.hub} did not answer ({why}). "
                      "Its edits are not saved until the router is restarted with a working hub.")


def signed(tokens: dict[str, str]):
    """`ask_agent` with each agent's own bus token: its host answers nothing else."""
    async def ask(url, *args, **kwargs):
        return await ask_agent(url, *args, token=tokens.get(url, ""), **kwargs)
    return ask


def main() -> None:
    parser = argparse.ArgumentParser(description="Retinue router")
    parser.add_argument("--config", default="/config/router.yaml")
    args = parser.parse_args()
    # Its environment holds the bot token and the keys: a parse worker for the owner's files must not read them.
    attachments.undumpable()
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
    gateway = Neighbour(cfg.gateway_url, cfg.gateway_token, "the Telegram gateway") if cfg.gateway_url else None
    mail = Mailroom(Collector(cfg.collector_url, cfg.collector_token) if cfg.collector_url else None, MailStore(store.db),
                    gateway=gateway) if cfg.collector_url or gateway else None
    sender = Neighbour(cfg.sender_url, cfg.sender_token, "the mail sender") if cfg.sender_url else None
    # Replies to people: the mail sender and the gateway hold the keys; the router holds the drafts and the «yes».
    outbox = Outbox(OutboxStore(store.db), sender=sender, gateway=gateway,
                    owner_tg=cfg.telegram.owner_id if cfg.telegram else None) if sender or gateway else None
    pages = Pages(Data(cfg.pages_data), cfg.pages_url, cfg.link_hosts, build_status=cfg.pages_build) \
        if cfg.pages_url else None
    core = Core(cfg.agents, store, cfg.owner, ask=signed(tokens), archive=Archive(cfg.archive_db),
                default_agent=cfg.default_agent,
                tz=cfg.owner_tz, backup_status=cfg.backup_status, scribe=Scribe(cfg.stt_key, store=store),
                travel=TravelOps(cfg.travel_url) if cfg.travel_url else None, memory=memory, mail=mail,
                pages=pages, outbox=outbox, proactive=Proactive(),
                watch=Watch(cfg.health_status, store.db))
    # The platform's sessions: its cards and summaries reach the owner through Telegram, never through the core.
    bridge = PlatformBridge(PlatformClient(cfg.platform_url, cfg.platform_token, "the platform"), store, cfg.face_url) \
        if cfg.platform_url else None
    if bridge:  # the week's tally of the platform's sessions, for the weekly review
        core.platform = bridge.neighbour.owner
    if bridge:  # what waits in the assistant's world, pushed for the face's «Waiting for you»
        bridge.waiting = lambda now: digest(core, now)
        bridge.chips = lambda now: chips(core, now)
        bridge.counts = lambda now: counts(core, now)
        bridge.press_waiting = lambda token, action, now: press(core, token, action, now)
        bridge.detail_waiting = lambda ref, token, now: detail(core, ref, token, now)
        if mail:  # a mark by his Telegram button shows in the app at once too
            mail.changed = bridge.poke
    if bridge and pages:  # the owner's pages, for the face's «Read»
        bridge.library = pages.library
    loop.run_until_complete(core.start(build_channels(cfg, store, loop, bridge)))
    # The tasks are held here for as long as the loop runs: the loop itself keeps only weak references to them.
    notes = loop.create_task(bridge.pump()) if bridge else None  # noqa: F841  the platform's cards and summaries
    if memory_error:
        loop.run_until_complete(core.tell_owner(memory_error))
    ticking = loop.create_task(core.clock())  # noqa: F841  reminders, the morning summary, retries after the limit
    if cfg.bus_secret:
        loop.run_until_complete(BusServer(core, cfg.bus_secret, cfg.bus_listen_port).start())
    loop.run_forever()


if __name__ == "__main__":
    main()
