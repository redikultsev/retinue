"""Configuration for the router and agent hosts. Secrets come from the environment, never from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"environment variable {name} is required")
    return value


TRUST_CLASSES = ("private", "web", "none")  # base without web | web without base | neither (Concierge)


@dataclass
class RouterAgent:
    id: str
    name: str
    url: str
    topic: str = ""
    description: str = ""                 # shown to other agents in the bus tool
    trust_class: str = "web"
    can_call: list[str] = field(default_factory=list)  # agent ids this agent may ask through the bus; "*" = all
    archive: bool = False                 # may search the raw archive through the bus
    reminders: bool = False               # may set, list, move and cancel the owner's reminders through the bus


@dataclass
class TelegramConfig:
    owner_id: int                       # the only Telegram user the bot listens to
    bot_token: str = ""                 # from TELEGRAM_BOT_TOKEN


@dataclass
class MatrixConfig:
    homeserver: str
    server_name: str
    owner: str                          # the owner's Matrix id, e.g. "@alice:example.com"
    appservice_id: str = "retinue"
    bot_localpart: str = "retinue"
    agent_prefix: str = "agent_"
    listen_host: str = "0.0.0.0"
    listen_port: int = 29400
    as_token: str = ""                  # from RETINUE_AS_TOKEN
    hs_token: str = ""                  # from RETINUE_HS_TOKEN

    def agent_mxid(self, agent_id: str) -> str:
        return f"@{self.agent_prefix}{agent_id}:{self.server_name}"


@dataclass
class RouterConfig:
    agents: list[RouterAgent]
    owner: str = "owner"                # how the owner is named in the protocol log
    state_db: str = "/data/router.sqlite"
    archive_db: str = "/data/archive.sqlite"  # the raw archive: its own file, never mounted into an agent
    matrix: MatrixConfig | None = None      # each channel is optional; at least one is required
    telegram: TelegramConfig | None = None
    default_agent: str | None = None    # who gets messages without an address; the only agent, if there is one
    owner_tz: str = "Europe/Moscow"     # IANA zone: every time the model and the owner see is local time here
    bus_listen_port: int = 9100         # agents reach the router here (network `agents` only)
    bus_secret: str = ""                # from RETINUE_BUS_SECRET; per-agent tokens are derived from it

    @classmethod
    def load(cls, path: str | Path) -> RouterConfig:
        raw = yaml.safe_load(Path(path).read_text())
        agents = [RouterAgent(**a) for a in raw.pop("agents")]
        matrix = MatrixConfig(**raw.pop("matrix")) if raw.get("matrix") else None
        telegram = TelegramConfig(**raw.pop("telegram")) if raw.get("telegram") else None
        raw.pop("matrix", None)
        raw.pop("telegram", None)
        cfg = cls(agents=agents, matrix=matrix, telegram=telegram, **raw)
        if not (cfg.matrix or cfg.telegram):
            raise SystemExit("router config: no channel — add a `telegram:` or a `matrix:` section")
        if cfg.matrix:
            cfg.matrix.as_token = _env("RETINUE_AS_TOKEN")
            cfg.matrix.hs_token = _env("RETINUE_HS_TOKEN")
        if cfg.telegram:
            cfg.telegram.bot_token = _env("TELEGRAM_BOT_TOKEN")
        cfg.bus_secret = os.environ.get("RETINUE_BUS_SECRET", "")
        try:
            ZoneInfo(cfg.owner_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"router config: owner_tz {cfg.owner_tz!r} is not a time zone, e.g. Europe/Moscow")
        ids = [a.id for a in cfg.agents]
        for agent in cfg.agents:
            if agent.trust_class not in TRUST_CLASSES:
                raise SystemExit(f"agent {agent.id}: unknown trust_class {agent.trust_class!r}")
        if cfg.default_agent is None and len(ids) == 1:
            cfg.default_agent = ids[0]
        if cfg.default_agent is not None and cfg.default_agent not in ids:
            raise SystemExit(f"router config: default_agent {cfg.default_agent!r} is not in `agents`")
        if cfg.telegram and cfg.default_agent is None:
            raise SystemExit("router config: Telegram is one chat without addresses — set `default_agent`")
        return cfg


# What an agent can be given through the router.
BUS_TOOLS = ("ask_agent", "list_agents", "search_archive",
             "set_reminder", "list_reminders", "cancel_reminder", "move_reminder")


@dataclass
class EngineConfig:
    type: str = "claude"  # "claude" | "echo" (smoke tests, no model)
    instructions: str = "/agent/CLAUDE.md"  # the agent's instructions: a read-only file outside its working folder
    tools: list[str] | None = None      # built-in tools the model sees at all: [] = none, None = the CLI default
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    bus_tools: list[str] = field(default_factory=lambda: ["ask_agent", "list_agents"])  # subset of BUS_TOOLS
    config_dir: str | None = None       # CLAUDE_CONFIG_DIR: the session transcripts; the agent's own volume
    max_turns: int = 30
    max_budget_usd: float | None = None
    model: str | None = None


@dataclass
class Skill:
    id: str
    name: str
    description: str
    examples: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    id: str
    name: str
    description: str
    trust_class: str  # "private" (base, no web) | "web" (web, no base) | "none" (neither: the Concierge)
    skills: list[Skill]
    engine: EngineConfig
    workspace: str = "/workspace"
    state_db: str = "/data/agent.sqlite"  # conversation -> session id; on the agent's own volume
    public_url: str = "http://localhost:9000"
    listen_host: str = "0.0.0.0"
    listen_port: int = 9000
    bus_url: str = ""    # from RETINUE_BUS_URL: the router's bus; empty = this agent cannot ask others
    bus_token: str = ""  # from RETINUE_BUS_TOKEN

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        raw = yaml.safe_load(Path(path).read_text())
        skills = [Skill(**s) for s in raw.pop("skills")]
        engine = EngineConfig(**raw.pop("engine", {}))
        cfg = cls(skills=skills, engine=engine, **raw)
        cfg.bus_url = os.environ.get("RETINUE_BUS_URL", "")
        cfg.bus_token = os.environ.get("RETINUE_BUS_TOKEN", "")
        if cfg.trust_class not in TRUST_CLASSES:
            raise SystemExit(f"unknown trust_class {cfg.trust_class!r}")
        if unknown := [t for t in cfg.engine.bus_tools if t not in BUS_TOOLS]:
            raise SystemExit(f"unknown bus_tools {unknown}; known: {', '.join(BUS_TOOLS)}")
        return cfg
