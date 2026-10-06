"""Router entry point: the core plus the channels the owner talks through.

The core (`core.py`) keeps one conversation per agent and talks to agents over A2A. Each channel adapter
(`channels/`) turns a messenger into core calls. Every channel is optional: it runs when its section is in
the config.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .archive import Archive
from .bus import BusServer
from .channels.telegram import TelegramChannel
from .config import RouterConfig
from .core import Core
from .protocol import Store


def build_channels(cfg: RouterConfig, store: Store, loop: asyncio.AbstractEventLoop) -> list:
    channels = []
    if cfg.matrix:
        # Imported only here: without a `matrix:` section the router never loads the Matrix library.
        from .channels.matrix import MatrixChannel
        channels.append(MatrixChannel(cfg.matrix, cfg.agents, store, loop, f"{cfg.state_db}.mx-state.json"))
    if cfg.telegram:
        channels.append(TelegramChannel(cfg.telegram, store))
    return channels


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
    core = Core(cfg.agents, store, cfg.owner, archive=Archive(cfg.archive_db), default_agent=cfg.default_agent)
    loop.run_until_complete(core.start(build_channels(cfg, store, loop)))
    if cfg.bus_secret:
        loop.run_until_complete(BusServer(core, cfg.bus_secret, cfg.bus_listen_port).start())
    loop.run_forever()


if __name__ == "__main__":
    main()
