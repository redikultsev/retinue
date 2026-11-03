"""Router entry point: the core plus the channels the owner talks through.

The core (`core.py`) keeps one conversation per agent and talks to agents over A2A. Each channel adapter
(`channels/`) turns a messenger into core calls. Every channel is optional: it runs when its section is in
the config.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import logging
import sqlite3
import subprocess
from urllib.parse import urlsplit

from . import attachments, voice
from .archive import Archive
from .bridge import PlatformBridge, PlatformClient
from .bus import BusServer, bus_token
from .channels.telegram import TelegramChannel
from .config import RouterConfig
from .core import AgentFile, Core, Reply, ask_agent
from .gcal import Gcal, Writer
from .health import Watch
from .pages import Data, Pages
from .mail import Collector, MailStore
from .mailroom import Mailroom
from .outbox import Neighbour, Outbox, OutboxStore
from .memory import Memory
from .proactive import Proactive
from .protocol import Store
from .speech import Scribe
from .plan_layout import Hours
from .planner import Planner
from .tasks import Tasks
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


class SessionVoice:
    """The voice's engine `session` (`voice:` in router.yaml): the core's runs of its default agent — the owner's
    turns, reminders, the morning summary — go to the root project's main session (`/owner/voice`, `voice.py`)
    instead of the assistant's host; every other agent is asked as before (`other`). A bare run of someone else's
    letter and an answer bound to a schema are refused (code takes them, as when the assistant fails), and so is an
    owner's turn from a channel `voice.channels` does not name."""

    def __init__(self, platform: PlatformClient, url: str, channels: list[str], other) -> None:
        self.platform, self.url, self.channels, self.other = platform, url, list(channels), other
        self.core: Core | None = None

    def attach(self, core: Core) -> None:
        self.core = core

    async def ask(self, url: str, text: str, context_id: str, on_progress=None, turn_id: str | None = None,
                  control: str | None = None, attachments: list[AgentFile] | None = None, token: str = "",
                  schema: dict | None = None) -> Reply:
        if url != self.url:
            given = {k: v for k, v in (("control", control), ("attachments", attachments), ("schema", schema)) if v}
            return await self.other(url, text, context_id, on_progress, turn_id, **given)
        if control == "compact":
            return Reply("error", voice.NO_COMPACT, [])
        if control == "bare" or schema is not None:
            return Reply("error", voice.NOT_FOR_SESSION, [])
        owner = self._owner_event(turn_id)
        if owner is not None and owner.channel not in self.channels:
            return Reply("error", voice.NOT_HEARD.format(channel=owner.channel), [])
        # The owner's message by its archive id, so a router that restarts in the turn asks for the same one; the
        # schedule's by the run it is (a summary's context names its job).
        body: dict = {"input_id": f"voice:{owner.id}" if owner else f"voice:{context_id}" + (f":{turn_id}" if turn_id
                                                                                          else ""), "text": text}
        if owner is None:
            body["by"] = voice.SCHEDULE
        if attachments:
            body["attachments"] = [{"name": f.name, "media_type": f.media_type, "data": base64.b64encode(f.data).decode(),
                                    "sha256": hashlib.sha256(f.data).hexdigest()} for f in attachments]
        answer = await self.platform.owner("voice", body, voice.HOLD_S + 60)
        if not answer.get("ok"):
            return Reply("error", str(answer.get("say") or "The voice's session did not take the message."), [])
        # Only a turn that ended with its answer is hers; working, waiting or failed is the platform's word.
        return Reply("done" if answer.get("status") == "done" else "error", str(answer.get("text") or ""), [],
                     {"new_session": answer.get("created") is True})

    def _owner_event(self, turn_id: str | None):
        """The owner's message this run answers, from the core's own record of its turn; None for the schedule's."""
        turn = self.core.turns.turns.get(turn_id) if self.core is not None and turn_id else None
        event_id = self.core.inbound.get(turn.tree.id) if turn is not None else None
        return self.core.archive.get(event_id) if event_id else None


def build_voice(cfg: RouterConfig, platform: PlatformClient | None, ask) -> SessionVoice | None:
    """The voice's engine: None — today's assistant, asked by `ask` over A2A, unchanged; a `SessionVoice` — the root
    project's main session, `ask` kept for every other agent."""
    if cfg.voice.engine != "session" or platform is None or cfg.default_agent is None:
        return None
    url = next(a.url for a in cfg.agents if a.id == cfg.default_agent)
    return SessionVoice(platform, url, cfg.voice.channels, ask)


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
    platform = PlatformClient(cfg.platform_url, cfg.platform_token, "the platform") if cfg.platform_url else None
    session_voice = build_voice(cfg, platform, signed(tokens))  # None: the assistant, as ever
    core = Core(cfg.agents, store, cfg.owner, ask=session_voice.ask if session_voice else signed(tokens),
                archive=Archive(cfg.archive_db),
                default_agent=cfg.default_agent,
                tz=cfg.owner_tz, backup_status=cfg.backup_status, scribe=Scribe(cfg.stt_key, store=store),
                travel=TravelOps(cfg.travel_url) if cfg.travel_url else None, memory=memory, mail=mail,
                pages=pages, outbox=outbox, proactive=Proactive(week=cfg.proactive.week),
                watch=Watch(cfg.health_status, store.db))
    # The platform's sessions: its cards and summaries reach the owner through Telegram, never through the core.
    if session_voice:
        session_voice.attach(core)
    bridge = PlatformBridge(platform, store, cfg.face_url) if platform else None
    if bridge:  # the week's tally of the platform's sessions, for the weekly review
        core.platform = bridge.neighbour.owner
    if bridge:  # what waits in the assistant's world, pushed for the face's «Waiting for you»
        bridge.waiting = lambda now: digest(core, now)
        bridge.chips = lambda now: chips(core, now)
        bridge.counts = lambda now: counts(core, now)
        bridge.press_waiting = lambda token, action, now: press(core, token, action, now)
        bridge.detail_waiting = lambda ref, token, now: detail(core, ref, token, now)
        tasks = Tasks(core)  # the tasks against their rows, a row's task for «Plan», the busy window
        bridge.cycle = tasks.cycle
        planner = Planner(core, tasks, Hours.parse(cfg.plan_hours, cfg.plan_zone or cfg.owner_tz))  # the day plan
        tasks.planner = core.planner = bridge.planner = planner
        core.actions.update({"plan-undo": planner.undo_pressed, "plan-busy": planner.busy_pressed})
        if cfg.calwrite_url:  # the plan's blocks in his Google calendar «Retinue», both ways
            tasks.gcal = Gcal(core, planner, Writer(cfg.calwrite_url, cfg.calwrite_token, "the calendar writer"),
                              planner.hours.zone, f"{cfg.face_url.rstrip('/')}/today?tab=schedule" if cfg.face_url else "")
            core.actions["gcal-again"] = tasks.gcal.again_pressed
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
