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
    attachments: bool = False             # may fetch what the owner sent (#N) again through the bus


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
class MemoryConfig:
    """The knowledge base the assistant writes: a git repository whose hub is outside the stack. The router holds the
    working copy's repository and commits; the assistant's container sees only the files (`tree`)."""
    tree: str = "/kb"                   # the working copy's files; the assistant mounts the same folder
    git_dir: str = "/kb-git"            # its repository: in the router's container only, so she cannot write hooks
    hub: str = "/hub"                   # the bare repository every copy pushes to; its pre-receive checks each push
    policy: str = "/config/memory-policy.json"  # what she may write (kbcheck.Policy); the hub has the same file
    author: str = "Ассистентка <assistant@retinue>"
    checkout: list[str] = field(default_factory=lambda: ["/*"])  # sparse checkout: what of the base she sees
    digest_at: str = "21:00"            # the evening list of her commits, the owner's wall time


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
    backup_status: str = "/status/backup.json"  # written by the host's backup; the morning summary reports it
    bus_listen_port: int = 9100         # agents reach the router here, on `agents`. The router listens on every
    # network it is on, so travel-ops (on `travel-router`) reaches the port too: every call needs an agent's token
    bus_secret: str = ""                # from RETINUE_BUS_SECRET; per-agent tokens are derived from it
    stt_key: str = ""                   # from ELEVENLABS_API_KEY: speech to text; without it voice is refused aloud
    travel_url: str = ""                # travel-ops' MCP over HTTP: the router collects price alerts there
    link_hosts: list[str] = field(default_factory=list)  # travel-ops' sites: a link there is clickable in Telegram
    memory: MemoryConfig | None = None  # the knowledge base she writes; without the section she has no base

    @classmethod
    def load(cls, path: str | Path) -> RouterConfig:
        raw = yaml.safe_load(Path(path).read_text())
        agents = [RouterAgent(**a) for a in raw.pop("agents")]
        matrix = MatrixConfig(**raw.pop("matrix")) if raw.get("matrix") else None
        telegram = TelegramConfig(**raw.pop("telegram")) if raw.get("telegram") else None
        memory = MemoryConfig(**raw.pop("memory")) if raw.get("memory") else None
        raw.pop("matrix", None)
        raw.pop("telegram", None)
        raw.pop("memory", None)
        cfg = cls(agents=agents, matrix=matrix, telegram=telegram, memory=memory, **raw)
        if not (cfg.matrix or cfg.telegram):
            raise SystemExit("router config: no channel — add a `telegram:` or a `matrix:` section")
        if cfg.matrix:
            cfg.matrix.as_token = _env("RETINUE_AS_TOKEN")
            cfg.matrix.hs_token = _env("RETINUE_HS_TOKEN")
        if cfg.telegram:
            cfg.telegram.bot_token = _env("TELEGRAM_BOT_TOKEN")
        cfg.bus_secret = os.environ.get("RETINUE_BUS_SECRET", "")
        cfg.stt_key = os.environ.get("ELEVENLABS_API_KEY", "")
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
             "set_reminder", "list_reminders", "cancel_reminder", "move_reminder", "get_attachment")


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
    travel_url: str = ""  # from RETINUE_TRAVEL_URL: travel-ops' MCP over HTTP; empty = no travel tools
    memory: str = ""      # from RETINUE_MEMORY: the knowledge base's folder; empty = no file tools at all
    memory_policy: str = ""  # from RETINUE_MEMORY_POLICY: what she may write there (kbcheck.Policy), read-only

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        raw = yaml.safe_load(Path(path).read_text())
        skills = [Skill(**s) for s in raw.pop("skills")]
        engine = EngineConfig(**raw.pop("engine", {}))
        cfg = cls(skills=skills, engine=engine, **raw)
        cfg.bus_url = os.environ.get("RETINUE_BUS_URL", "")
        cfg.bus_token = os.environ.get("RETINUE_BUS_TOKEN", "")
        cfg.travel_url = os.environ.get("RETINUE_TRAVEL_URL", "")
        cfg.memory = os.environ.get("RETINUE_MEMORY", "")
        cfg.memory_policy = os.environ.get("RETINUE_MEMORY_POLICY", "")
        if cfg.memory and not cfg.memory_policy:
            raise SystemExit("RETINUE_MEMORY needs RETINUE_MEMORY_POLICY: without the policy nothing may be written")
        if cfg.trust_class not in TRUST_CLASSES:
            raise SystemExit(f"unknown trust_class {cfg.trust_class!r}")
        if unknown := [t for t in cfg.engine.bus_tools if t not in BUS_TOOLS]:
            raise SystemExit(f"unknown bus_tools {unknown}; known: {', '.join(BUS_TOOLS)}")
        return cfg
