"""Configuration for the router and agent hosts. Secrets come from the environment, never from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class TelegramConfig:
    owner_id: int                       # the only Telegram user the bot listens to
    bot_token: str = ""                 # from TELEGRAM_BOT_TOKEN


@dataclass
class RouterConfig:
    homeserver: str
    server_name: str
    owner: str
    agents: list[RouterAgent]
    appservice_id: str = "retinue"
    bot_localpart: str = "retinue"
    agent_prefix: str = "agent_"
    listen_host: str = "0.0.0.0"
    listen_port: int = 29400
    state_db: str = "/data/router.sqlite"
    as_token: str = ""
    hs_token: str = ""
    telegram: TelegramConfig | None = None
    default_agent: str | None = None    # who gets unaddressed messages (Telegram general chat): the Concierge
    bus_listen_port: int = 9100         # agents ask each other here (network `agents` only)
    bus_secret: str = ""                # from RETINUE_BUS_SECRET; per-agent tokens are derived from it

    @classmethod
    def load(cls, path: str | Path) -> RouterConfig:
        raw = yaml.safe_load(Path(path).read_text())
        agents = [RouterAgent(**a) for a in raw.pop("agents")]
        telegram = TelegramConfig(**raw.pop("telegram")) if raw.get("telegram") else None
        raw.pop("telegram", None)
        cfg = cls(agents=agents, telegram=telegram, **raw)
        if cfg.telegram:
            cfg.telegram.bot_token = _env("TELEGRAM_BOT_TOKEN")
        cfg.bus_secret = os.environ.get("RETINUE_BUS_SECRET", "")
        for agent in cfg.agents:
            if agent.trust_class not in TRUST_CLASSES:
                raise SystemExit(f"agent {agent.id}: unknown trust_class {agent.trust_class!r}")
        cfg.as_token = _env("RETINUE_AS_TOKEN")
        cfg.hs_token = _env("RETINUE_HS_TOKEN")
        return cfg

    def agent_mxid(self, agent_id: str) -> str:
        return f"@{self.agent_prefix}{agent_id}:{self.server_name}"


@dataclass
class EngineConfig:
    type: str = "claude"  # "claude" | "echo" (smoke tests, no model)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
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
    state_db: str = "/data/agent.sqlite"
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
        return cfg
