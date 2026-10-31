"""Configuration for the router and agent hosts. Secrets come from the environment, never from YAML."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .cliengine import SETTINGS_DIR, rule_ok

log = logging.getLogger("retinue.config")

# Scribe's rules for `keyterms` (API reference, POST /v1/speech-to-text): at most 1000, each under 50 characters and
# at most 5 words, none of these characters. Each costs: +20% a request, and past 100 a request is billed 20 s at least.
KEYTERMS_MAX = 1000
KEYTERM_CHARS = 50
KEYTERM_WORDS = 5
KEYTERM_REFUSED = set("<>{}[]\\")
KEYTERMS_CHEAP = 100


def keyterm_problem(term: object) -> str | None:
    """Why Scribe would refuse this term, or None."""
    if not isinstance(term, str) or not term.strip():
        return "not a non-empty string"
    if len(term) >= KEYTERM_CHARS:
        return f"{KEYTERM_CHARS} characters or more"
    if len(term.split()) > KEYTERM_WORDS:
        return f"more than {KEYTERM_WORDS} words"
    if refused := sorted(set(term) & KEYTERM_REFUSED):
        return f"has {' '.join(refused)}"
    return None


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
    trips: bool = False                   # may publish a trip page among the owner's pages through the bus
    drafts: bool = False                  # may propose a reply to a person; the owner sends it with his button


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
    author: str = "Assistant <assistant@retinue>"
    checkout: list[str] = field(default_factory=lambda: ["/*"])  # sparse checkout: what of the base she sees
    digest_at: str = "21:00"            # the evening list of her commits, the owner's wall time
    # Spaces whose reading makes a draft's card louder: «*» — all of them, until the owner names the closed ones.
    # A file at the top (MAP.md) names every Space's records: always closed.
    sensitive: list[str] = field(default_factory=lambda: ["*"])
    # The owner's own words for what the code keeps generic; the server's own file fills them (docs/memory.md).
    location: str = ""                  # the Record whose header names his zone; '' — proactive.LOCATION
    tracker: dict = field(default_factory=dict)   # what his tracker tracks: path, title, states… (pages.Tracker)


@dataclass
class RouterConfig:
    agents: list[RouterAgent]
    owner: str = "owner"                # how the owner is named in the protocol log
    state_db: str = "/data/router.sqlite"
    archive_db: str = "/data/archive.sqlite"  # the raw archive: its own file, never mounted into an agent
    matrix: MatrixConfig | None = None      # each channel is optional; at least one is required
    telegram: TelegramConfig | None = None
    default_agent: str | None = None    # who gets messages without an address; the only agent, if there is one
    owner_tz: str = "UTC"               # IANA zone: every time the model and the owner see is local time here
    backup_status: str = "/status/backup.json"  # written by the host's backup; the morning summary reports it
    health_status: str = "/status/health.json"  # the containers, written by the host every 2 min (health.py)
    bus_listen_port: int = 9100         # agents reach the router here, on `agents`. The router listens on every
    # network it is on, so travel-ops (on `travel-router`) reaches the port too: every call needs an agent's token
    bus_secret: str = ""                # from RETINUE_BUS_SECRET; per-agent tokens are derived from it
    stt_key: str = ""                   # from ELEVENLABS_API_KEY: speech to text; without it voice is refused aloud
    travel_url: str = ""                # travel-ops' MCP over HTTP: the router collects price alerts there
    link_hosts: list[str] = field(default_factory=list)  # travel-ops' sites: a link there is clickable in Telegram
    memory: MemoryConfig | None = None  # the knowledge base she writes; without the section she has no base
    collector_url: str = ""             # the mail collector: the owner's mail and calendar; empty = no mail
    collector_token: str = ""           # from RETINUE_COLLECTOR_TOKEN: the router signs its calls to the collector
    pages_url: str = ""                 # the owner's pages' address, https://<host>; empty = no pages
    pages_data: str = "/pages"          # where the router writes the pages' data; the pages service reads it
    pages_build: str = "/pages-site/build.json"  # how the builder's last build went (read-only for the router)
    gateway_url: str = ""               # the Telegram Business gateway: the owner's chosen chats; empty = none
    gateway_token: str = ""             # from RETINUE_GATEWAY_TOKEN
    sender_url: str = ""                # the mail sender: replies and letters the owner confirmed; empty = none
    sender_token: str = ""              # from RETINUE_SENDER_TOKEN
    platform_url: str = ""              # the platform (`retinue-platform`): sessions in the lanes; empty = none
    platform_token: str = ""            # from RETINUE_PLATFORM_TOKEN
    face_url: str = ""                  # the platform's face, https://<host>: a card's «Open in the app», /app

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
        # The retired life hub's keys (docs/pages.md): a router.yaml setup.sh has not rewritten yet still starts —
        # with no pages until it does.
        if retired := [key for key in ("lifehub_url", "lifehub_data", "lifehub_build") if raw.pop(key, None)]:
            log.warning("router config: %s is the retired life hub's — run setup.sh PAGES=1 (docs/pages.md)",
                        ", ".join(retired))
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
        if cfg.collector_url:
            cfg.collector_token = _env("RETINUE_COLLECTOR_TOKEN")
        if cfg.gateway_url:
            cfg.gateway_token = _env("RETINUE_GATEWAY_TOKEN")
        if cfg.sender_url:
            cfg.sender_token = _env("RETINUE_SENDER_TOKEN")
        if cfg.platform_url:
            if not cfg.telegram:
                raise SystemExit("router config: the platform talks to the owner through Telegram — add `telegram:`")
            cfg.platform_token = _env("RETINUE_PLATFORM_TOKEN")
        if cfg.face_url and not (cfg.platform_url and FACE_ORIGIN.fullmatch(cfg.face_url)):
            raise SystemExit(f"router config: face_url {cfg.face_url!r} is not https://<host>, or no platform_url")
        if cfg.pages_url and not re.fullmatch(r"https://[a-z0-9.-]+", cfg.pages_url):
            raise SystemExit(f"router config: pages_url {cfg.pages_url!r} is not https://<host>")
        try:
            ZoneInfo(cfg.owner_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"router config: owner_tz {cfg.owner_tz!r} is not a time zone, e.g. Europe/Berlin")
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


@dataclass
class CollectorConfig:
    """The mail collector (`retinue-collector`): the owner's tokens, its own state, the token the router signs with.
    Everything from the environment: the container has no config file of its own."""
    keys: str = "/keys"                     # client.json and <account>.<gmail|calendar>.json, read-only
    state: str = "/state/collector.sqlite"  # cursors and items: its own folder, nobody else mounts it
    listen_port: int = 9200
    token: str = ""                         # from RETINUE_COLLECTOR_TOKEN

    @classmethod
    def load(cls) -> CollectorConfig:
        return cls(keys=os.environ.get("RETINUE_MAIL_KEYS", cls.keys),
                   state=os.environ.get("RETINUE_MAIL_STATE", cls.state), token=_env("RETINUE_COLLECTOR_TOKEN"))


@dataclass
class SenderConfig:
    """The mail sender (`retinue-mail-send`): the owner's `gmail.send` tokens, its own ledger, the router's token. From
    the environment, like the collector's."""
    keys: str = "/keys"                     # client.json and <account>.send.json, read-only
    state: str = "/state/sender.sqlite"     # every key it was asked to send; nobody else mounts it
    listen_port: int = 9400
    token: str = ""                         # from RETINUE_SENDER_TOKEN

    @classmethod
    def load(cls) -> SenderConfig:
        return cls(keys=os.environ.get("RETINUE_SEND_KEYS", cls.keys),
                   state=os.environ.get("RETINUE_SEND_STATE", cls.state), token=_env("RETINUE_SENDER_TOKEN"))


@dataclass
class GatewayConfig:
    """The Telegram Business gateway (`retinue-tg-business`): the Business bot's token, the owner's user id, its own
    state and ledger, the router's token. From the environment."""
    state: str = "/state/gateway.sqlite"    # the offset, the connection, the items, each chat's window
    ledger: str = "/state/sends.sqlite"     # every key it was asked to send
    listen_port: int = 9300
    token: str = ""                         # from RETINUE_GATEWAY_TOKEN
    bot_token: str = ""                     # from TELEGRAM_BUSINESS_TOKEN: a bot of its own, not the router's
    owner_id: int = 0                       # from TELEGRAM_OWNER_ID: the only account whose connection counts

    @classmethod
    def load(cls) -> GatewayConfig:
        owner = _env("TELEGRAM_OWNER_ID")
        if not owner.isdigit():
            raise SystemExit("TELEGRAM_OWNER_ID: the owner's numeric Telegram id")
        return cls(token=_env("RETINUE_GATEWAY_TOKEN"), bot_token=_env("TELEGRAM_BUSINESS_TOKEN"), owner_id=int(owner))


@dataclass
class LaneHostConfig:
    """A lane's host (`retinue-lane`): the platform's token, the one hook address a run may point at, the lane's
    egress proxy, how many CLI processes may run at once. From the environment. Not the login its CLI runs with:
    the platform hands that over with each run, and the lane host keeps it in memory only."""
    token: str = ""                     # from RETINUE_LANE_TOKEN: the platform signs every command with it
    hook_url: str = ""                  # from RETINUE_LANE_HOOK_URL: a settings file with any other address is refused
    proxy: str = ""                     # HTTPS_PROXY
    no_proxy: str = ""                  # NO_PROXY
    max_running: int = 2                # RETINUE_LANE_MAX
    work: str = "/work"                 # the sessions' working trees
    home: str = "/lane/config"          # the CLI's HOME
    config_dir: str = "/lane/config/claude"  # its CLAUDE_CONFIG_DIR: the CLI's transcripts
    state: str = "/lane/state"          # the journal
    run_dir: str = SETTINGS_DIR         # tmpfs: one settings file per run
    listen_port: int = 9600
    web: bool = False                   # RETINUE_LANE_WEB=1: the retired lane-web, which never reads a repository's
    # rules or skills
    mirrors: str = "/lane/mirrors"      # the platform's mirrors of the owner's repositories, read-only (docs/code.md)

    @classmethod
    def load(cls) -> LaneHostConfig:
        cap = os.environ.get("RETINUE_LANE_MAX", str(cls.max_running))
        if not cap.isdigit() or int(cap) < 1:
            raise SystemExit("RETINUE_LANE_MAX: a positive number of CLI processes")
        return cls(token=_env("RETINUE_LANE_TOKEN"), hook_url=_env("RETINUE_LANE_HOOK_URL").rstrip("/"), proxy=os.environ.get("HTTPS_PROXY", ""),
                   no_proxy=os.environ.get("NO_PROXY", ""), max_running=int(cap),
                   web=os.environ.get("RETINUE_LANE_WEB") == "1")



# On a lane with the web, an allow rule that takes every fetch or search past auto's classifier — one of the two things
# between a page the session read and a fetch carrying the owner's data out.
WEB_ALLOW_REFUSED = frozenset({"WebFetch", "WebSearch", "WebFetch(domain:*)", "WebFetch(*)", "WebSearch(*)"})
LANE_NAME = re.compile(r"[a-z][a-z0-9]{1,30}")  # its token is RETINUE_LANE_TOKEN_<NAME>, so no dash
TRACK_NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
TRACK_KINDS = ("code", "private")
# A sphere is also a bot command (`/travel …`): Telegram's own command alphabet, and never one the bot has already.
SPHERE_NAME = re.compile(r"[a-z][a-z0-9_]{1,30}")
BOT_COMMANDS = frozenset({"new", "compact", "check", "mail", "initiative", "help", "start", "app", "wait"})
FACE_ORIGIN = re.compile(r"https://[a-z0-9.-]+(:\d{1,5})?")


@dataclass
class LaneRef:
    """A lane as the platform reaches it: its lane host, the hook address its CLI is given, and where the platform
    reads the lane's /work (a read-only mount: the file hash an approval is bound to)."""
    name: str
    url: str
    hook_url: str
    work_mount: str
    max_sessions: int = 2
    deny: list[str] = field(default_factory=list)  # always-deny rules for every run on the lane
    card: list[str] = field(default_factory=list)  # always-ask rules: a call under one comes to the owner as a card
    web: bool = False                   # the web tools are not denied by code: a lane with an open proxy — the room
    # (docs/platform.md); every other lane has them denied whatever its `deny` says
    token: str = ""                     # from RETINUE_LANE_TOKEN_<NAME>
    retired: bool = False               # kept for its sessions, which stay readable; no run starts, nothing goes in,
    # its open runs hold no room, its journal is not pulled and it needs no token (lane-code, lane-web)


@dataclass
class Track:
    name: str
    lane: str
    kind: str                           # code: closes on its summary | private: goes idle
    cwd: str = ""                       # the session's directory, relative to the lane's /work
    allow: list[str] = field(default_factory=list)  # always-allow rules, in the CLI's syntax
    deny: list[str] = field(default_factory=list)
    card: list[str] = field(default_factory=list)   # always a card to the owner, whatever the classifier would say


@dataclass
class Sphere:
    """A sphere of the owner's life — a Space of his knowledge base (travel, work, home…): the face groups his
    sessions by it, `/<name> <text>` writes into its main session, and a new session of it runs on `track`."""
    name: str
    title: str
    track: str                          # the track a new session of this sphere runs on
    icon: str = ""                      # an emoji or a short mark before the title
    cwd: str = ""                       # its sessions' directory under the lane's /work, in the track's ('' : the
    # track's own) — e.g. `base/travel`: a session «in a Space» of the Base (docs/base.md)


@dataclass
class BaseConfig:
    """The owner's knowledge base cloned into a lane, kept fresh by the platform and committed by his button
    (`base.py`, docs/base.md). Absent from `platform.yaml`: off."""
    author: str                         # "Name <email>": the owner — his button makes his commit
    hub: str = "/hub"                   # the bare hub, as the platform mounts it
    tree: str = "base"                  # the clone's folder under the lane's /work
    lane: str = "private"               # the room: the Base beside the open web, by the owner's decision
    mount: str = ""                     # the same folder, read-write in the platform; '' : <the lane's work_mount>/<tree>,
    # where compose mounts it over the read-only lane view (docs/base.md)
    git_dir: str = "/platform/base.git"  # the clone's repository: the platform's volume, never the lane's
    policy: str = ""                    # the hub's rules (kbcheck.Policy); '' : <hub>/hooks/kb-policy.json
    # Never committed from the server: what the Base's own check runs (the hub runs it in the platform's container
    # when the platform pushes), and code in general. Globs as in the policy.
    protect: list[str] = field(default_factory=lambda: ["scripts/**", "justfile", "Justfile", "**/*.py", "**/*.sh"])
    max_file_mb: int = 10               # a bigger file is neither read nor committed
    every_s: int = 60                   # how often an idle clone looks at the hub


CODE_HOSTS = ("github.com", "gitlab.com")  # where a repository may live: the platform's proxy lets through these alone
REPO_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")   # its folder in the room and its mirror's name
TOKEN_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")    # RETINUE_GIT_TOKEN_<KEY>: one per resource owner on GitHub
REPO_URL = re.compile(r"https://(github\.com|gitlab\.com)/((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+?)(?:\.git)?/?")


@dataclass
class CodeRepo:
    """One of the owner's repositories in the room (docs/code.md): `/work/<root>/<name>` there, a clean mirror here,
    pushed as a branch and a pull request by his button with the token `RETINUE_GIT_TOKEN_<TOKEN>`."""
    name: str
    url: str                            # https only, github.com or gitlab.com: the push goes there, nowhere else
    token: str                          # the key of its token: RETINUE_GIT_TOKEN_<TOKEN> in the platform's env
    default_branch: str = ""            # '' : as the remote's HEAD says
    secret: str = ""                    # the token itself, from the environment — never from YAML, never logged
    off: str = ""                       # why the repository is off ('' : on), said where it is listed
    api: str = ""                       # GitHub's API root ('' : https://api.github.com); tests point it elsewhere

    @property
    def host(self) -> str:
        return self.url.split("/", 3)[2] if "://" in self.url else ""

    @property
    def path(self) -> str:
        """`owner/repo` on GitHub, `group/…/project` on GitLab: the URL's path without `.git`."""
        found = REPO_URL.fullmatch(self.url)
        return found.group(2) if found else self.url.rstrip("/").removesuffix(".git").split("/", 3)[-1]


@dataclass
class CodeConfig:
    """The owner's repositories in the room (`code.py`, docs/code.md). Absent from `platform.yaml`: off."""
    repos: list[CodeRepo] = field(default_factory=list)
    lane: str = "private"               # the room: the lane whose /work holds the clones
    root: str = "code"                  # the clones' folder under its /work
    track: str = "code"                 # `/new <track> <repo> [text]` starts in /work/<root>/<repo>; '' : none
    author: str = ""                    # «Name <email>», the clones' git identity; '' : base.author
    mirrors: str = "/code/mirrors"      # the mirrors, read-write here (the room mounts them read-only)
    lane_mirrors: str = "/lane/mirrors"  # the same folder as the room sees it: each clone's origin
    outbox: str = "/platform/code"      # the platform's own repositories, one per mirror: what a card fetched
    proxy: str = ""                     # the platform's way out (HTTPS_PROXY of git and of the API calls)
    every_s: int = 300                  # how often an idle mirror is fetched


@dataclass
class SpeechConfig:
    """The owner's voice in the app (`face.Face.speech`, docs/face.md «Voice»): ElevenLabs Scribe, called by the
    platform through its own proxy. Absent from `platform.yaml`: no terms, `code.proxy` as the way out."""
    keyterms: list[str] = field(default_factory=list)  # words Scribe should expect in dictation: «KV cache», «RAG»…
    proxy: str = ""                     # the platform's way out to api.elevenlabs.io; '' : code.proxy


@dataclass
class PlatformConfig:
    """The platform (`retinue-platform`): its lanes, the owner's tracks and their always rules, its own SQLite."""
    lanes: dict[str, LaneRef]
    tracks: list[Track]
    max_sessions: int = 5               # running sessions across every lane
    state_db: str = "/data/platform.sqlite"
    listen_port: int = 9500
    router_token: str = ""              # from RETINUE_PLATFORM_TOKEN
    hook_secret: str = ""               # from RETINUE_HOOK_SECRET
    oauth_token: str = ""               # from CLAUDE_CODE_OAUTH_TOKEN: the lanes' login, handed to a lane with each run
    # and held by no lane container (a lane's environment is readable to its own user at /proc/1/environ)
    # The face (`face.py`): the owner's app on his phone. Off unless its origin is set.
    face_port: int = 9600               # its own site, on its own network; the router and the hooks stay on 9500
    face_host: str = "127.0.0.1"        # from RETINUE_FACE_HOST: the address the face listens on — the platform's own
    # on the network `face-platform`, never 0.0.0.0, which the lanes' networks would reach too
    face_origin: str = ""               # from RETINUE_FACE_ORIGIN: https://<host>, what the owner's browser loads
    webapp_key: str = ""                # from RETINUE_WEBAPP_KEY: HMAC_SHA256("WebAppData", bot token), hex — the
    # Mini App's initData is checked with it; the platform never holds the bot token itself
    owner_tg: int = 0                   # from TELEGRAM_OWNER_ID: the only Telegram user a Mini App login is for
    owner_tz: str = "UTC"               # IANA zone: the times the platform says to the owner («… until 14:05»)
    skills_mount: str = "/skills"       # the owner's skills, read-only, as the lanes mount them (docs/instructions.md)
    owner_memory_mount: str = "/owner-memory"  # his memory snapshot, read-only, as lane-private mounts it (same doc)
    out_store_mb: int = 500             # the bytes of handed-over files the platform keeps, in all (outfiles)
    out_day_mb: int = 200               # and new in one day
    # The pages service's address, https://<host>: the one origin a page of «Pages» may open on (`library.py`); '' :
    # the router's list of pages is refused.
    pages_origin: str = ""
    spheres: list[Sphere] = field(default_factory=list)
    base: BaseConfig | None = None
    code: CodeConfig | None = None
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    stt_key: str = ""                   # from ELEVENLABS_API_KEY: the app's dictation; without it voice is refused aloud

    @classmethod
    def load(cls, path: str | Path = "/config/platform.yaml") -> PlatformConfig:
        raw = yaml.safe_load(Path(path).read_text())
        lanes = {name: LaneRef(name=name, **lane) for name, lane in (raw.pop("lanes") or {}).items()}
        tracks = [Track(**track) for track in raw.pop("tracks") or []]
        spheres = [Sphere(**sphere) for sphere in raw.pop("spheres", None) or []]
        given = raw.pop("base", None)
        if given is not None and not isinstance(given, dict):
            raise SystemExit("platform config: base is not a section (hub, tree, lane, author)")
        if unknown := sorted(set(given or {}) - set(BaseConfig.__dataclass_fields__)):
            raise SystemExit(f"platform config: base: unknown {', '.join(map(str, unknown))}; known: "
                             f"{', '.join(BaseConfig.__dataclass_fields__)}")
        if given is not None and "author" not in given:
            raise SystemExit("base: author is not «Name <email>» — the owner, whose commits the button makes")
        code = _code(raw.pop("code", None))
        voice = _speech(raw.pop("speech", None))
        if raw.pop("hub_origin", None):  # the retired life hub's: «Pages» stays empty until pages_origin is written
            log.warning("platform config: hub_origin is the retired life hub's — write pages_origin (docs/pages.md)")
        cfg = cls(lanes=lanes, tracks=tracks, spheres=spheres, base=BaseConfig(**given) if given else None, code=code,
                  speech=voice, **raw)
        for lane in cfg.lanes.values():
            if not LANE_NAME.fullmatch(lane.name):
                raise SystemExit(f"platform config: lane name {lane.name!r} is not {LANE_NAME.pattern}")
            if type(lane.max_sessions) is not int or lane.max_sessions < 1:
                raise SystemExit(f"platform config: lane {lane.name}: max_sessions is not a positive number")
            if type(lane.web) is not bool or type(lane.retired) is not bool:
                raise SystemExit(f"platform config: lane {lane.name}: web or retired is not true or false")
            if not lane.retired:
                lane.token = _env(f"RETINUE_LANE_TOKEN_{lane.name.upper()}")
        if type(cfg.max_sessions) is not int or cfg.max_sessions < 1:
            raise SystemExit("platform config: max_sessions is not a positive number")
        for name in ("out_store_mb", "out_day_mb"):
            if type(getattr(cfg, name)) is not int or getattr(cfg, name) < 1:
                raise SystemExit(f"platform config: {name} is not a positive number")
        names = [t.name for t in cfg.tracks]
        if len(set(names)) != len(names):
            raise SystemExit("platform config: two tracks with one name")
        for track in cfg.tracks:
            if not TRACK_NAME.fullmatch(track.name):
                raise SystemExit(f"platform config: track name {track.name!r} is not {TRACK_NAME.pattern}")
            if track.lane not in cfg.lanes:
                raise SystemExit(f"track {track.name}: unknown lane {track.lane!r}; known: {', '.join(cfg.lanes)}")
            if track.kind not in TRACK_KINDS:
                raise SystemExit(f"track {track.name}: kind {track.kind!r} is not one of {', '.join(TRACK_KINDS)}")
            if track.cwd.startswith("/") or ".." in Path(track.cwd).parts:
                raise SystemExit(f"track {track.name}: cwd {track.cwd!r} is not a path inside /work")
            if cfg.lanes[track.lane].web and (open_web := sorted(set(track.allow) & WEB_ALLOW_REFUSED)):
                raise SystemExit(f"track {track.name}: allow {open_web[0]} on a web lane would take every fetch past "
                                 "the classifier; leave the web tools to it (docs/platform.md, «The room»)")
        _check_spheres(cfg)
        _check_base(cfg)
        _check_code(cfg)
        _check_speech(cfg)
        for where, rules in [*((f"lane {lane.name}", lane.deny + lane.card) for lane in cfg.lanes.values()),
                             *((f"track {track.name}", track.allow + track.deny + track.card) for track in cfg.tracks)]:
            if bad := [rule for rule in rules if not rule_ok(rule)]:
                raise SystemExit(f"{where}: not a permission rule: {bad[0]!r}")
        try:
            ZoneInfo(cfg.owner_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"platform config: owner_tz {cfg.owner_tz!r} is not a time zone, e.g. Europe/Berlin") \
                from None
        cfg.router_token = _env("RETINUE_PLATFORM_TOKEN")
        cfg.hook_secret = _env("RETINUE_HOOK_SECRET")
        cfg.oauth_token = _env("CLAUDE_CODE_OAUTH_TOKEN")
        cfg.face_origin = os.environ.get("RETINUE_FACE_ORIGIN", "").rstrip("/")
        cfg.face_host = os.environ.get("RETINUE_FACE_HOST", "") or cfg.face_host
        try:
            ipaddress.ip_address(cfg.face_host)
        except ValueError:
            raise SystemExit(f"RETINUE_FACE_HOST {cfg.face_host!r} is not an IP address") from None
        cfg.webapp_key = os.environ.get("RETINUE_WEBAPP_KEY", "").lower()
        owner = os.environ.get("TELEGRAM_OWNER_ID", "")
        if cfg.face_origin and not FACE_ORIGIN.fullmatch(cfg.face_origin):
            raise SystemExit(f"RETINUE_FACE_ORIGIN {cfg.face_origin!r} is not https://<host>[:port]")
        cfg.pages_origin = str(cfg.pages_origin or "").rstrip("/").lower()
        if cfg.pages_origin and not re.fullmatch(r"https://[a-z0-9.-]+(:\d{1,5})?", cfg.pages_origin):
            raise SystemExit(f"platform config: pages_origin {cfg.pages_origin!r} is not https://<host>")
        if cfg.webapp_key and not re.fullmatch(r"[0-9a-f]{64}", cfg.webapp_key):
            raise SystemExit("RETINUE_WEBAPP_KEY: 64 hex digits, HMAC_SHA256(key=WebAppData, msg=<bot token>)")
        if owner and not owner.isdigit():
            raise SystemExit("TELEGRAM_OWNER_ID: the owner's numeric Telegram id")
        cfg.owner_tg = int(owner or 0)
        if type(cfg.face_port) is not int or not 0 < cfg.face_port < 65536 or cfg.face_port == cfg.listen_port:
            raise SystemExit("platform config: face_port is not a port of its own")
        return cfg

def _check_spheres(cfg: PlatformConfig) -> None:
    """A sphere is a bot command and a word of `/new <sphere|track>`: its name is unique among spheres and tracks and
    is no command the bot has; its track exists."""
    tracks = {t.name for t in cfg.tracks}
    seen: set[str] = set()
    for sphere in cfg.spheres:
        if not isinstance(sphere.name, str) or not SPHERE_NAME.fullmatch(sphere.name):
            raise SystemExit(f"platform config: sphere name {sphere.name!r} is not {SPHERE_NAME.pattern}")
        if sphere.name in seen or sphere.name in tracks or sphere.name in BOT_COMMANDS:
            raise SystemExit(f"platform config: sphere {sphere.name}: the name is taken by another sphere, a track or "
                             "a bot command")
        seen.add(sphere.name)
        if sphere.track not in tracks:
            raise SystemExit(f"sphere {sphere.name}: unknown track {sphere.track!r}; known: {', '.join(tracks)}")
        if not isinstance(sphere.title, str) or not 0 < len(sphere.title) <= 40:
            raise SystemExit(f"sphere {sphere.name}: title is not 1 to 40 characters")
        if not isinstance(sphere.icon, str) or len(sphere.icon) > 8:
            raise SystemExit(f"sphere {sphere.name}: icon is not a short mark (8 characters at most)")
        track = next(t for t in cfg.tracks if t.name == sphere.track)
        if not isinstance(sphere.cwd, str) or sphere.cwd.startswith("/") or ".." in Path(sphere.cwd).parts or (
                sphere.cwd and track.cwd and not _inside(sphere.cwd, track.cwd)):
            raise SystemExit(f"sphere {sphere.name}: cwd {sphere.cwd!r} is not a path inside its track's "
                             f"{track.cwd or '/work'}")


def _inside(path: str, folder: str) -> bool:
    path, folder = path.strip("/"), folder.strip("/")
    return not folder or path == folder or path.startswith(folder + "/")


def _check_base(cfg: PlatformConfig) -> None:
    """The Base in one folder of a configured lane's /work, committed under the owner's name. The lane may have the
    web — the room: a risk the owner takes knowingly (docs/platform.md, «The room»)."""
    base = cfg.base
    if base is None:
        return
    lane = cfg.lanes.get(base.lane)
    if lane is None:
        raise SystemExit(f"base: unknown lane {base.lane!r}; known: {', '.join(cfg.lanes)}")
    if lane.retired:
        raise SystemExit(f"base: lane {base.lane} is retired")
    parts = Path(base.tree).parts
    if not isinstance(base.tree, str) or not parts or base.tree.startswith("/") or any(
            p in (".", "..") or p.startswith(".") for p in parts):
        raise SystemExit(f"base: tree {base.tree!r} is not a folder inside the lane's /work")
    if not isinstance(base.author, str) or not re.fullmatch(r"[^<>\n]+ <[^<>@\s]+@[^<>\s]+>", base.author):
        raise SystemExit("base: author is not «Name <email>» — the owner, whose commits the button makes")
    base.mount = base.mount or f"{lane.work_mount.rstrip('/')}/{base.tree.strip('/')}"
    for name in ("hub", "mount", "git_dir"):
        if not isinstance(getattr(base, name), str) or not getattr(base, name).startswith("/"):
            raise SystemExit(f"base: {name} is not an absolute path")
    for name, least in (("max_file_mb", 1), ("every_s", 10)):
        if type(getattr(base, name)) is not int or getattr(base, name) < least:
            raise SystemExit(f"base: {name} is not a number of at least {least}")
    if not isinstance(base.protect, list) or not all(isinstance(p, str) for p in base.protect):
        raise SystemExit("base: protect is not a list of globs")


def _code(given: object) -> CodeConfig | None:
    """`code:` of platform.yaml, its repositories each with its token from the environment. A repository without one
    is listed as off with the reason, never a failed start: the room keeps working on what it has."""
    if given is None:
        return None
    if not isinstance(given, dict):
        raise SystemExit("platform config: code is not a section (repos, lane, root…)")
    if unknown := sorted(set(given) - set(CodeConfig.__dataclass_fields__)):
        raise SystemExit(f"platform config: code: unknown {', '.join(map(str, unknown))}; known: "
                         f"{', '.join(CodeConfig.__dataclass_fields__)}")
    repos = []
    for raw in given.get("repos") or []:
        if not isinstance(raw, dict) or (unknown := sorted(set(raw) - {"name", "url", "token", "default_branch"})):
            raise SystemExit(f"code: a repository is not {{name, url, token, default_branch?}}: {raw!r}")
        repos.append(CodeRepo(**{k: v for k, v in raw.items()}))
    return CodeConfig(**{**given, "repos": repos})


def _speech(given: object) -> SpeechConfig:
    """`speech:` of platform.yaml: the terms checked here by Scribe's own rules, so a bad one stops the start instead
    of failing every dictation."""
    if given is None:
        return SpeechConfig()
    if not isinstance(given, dict):
        raise SystemExit("platform config: speech is not a section (keyterms, proxy)")
    if unknown := sorted(set(given) - set(SpeechConfig.__dataclass_fields__)):
        raise SystemExit(f"platform config: speech: unknown {', '.join(map(str, unknown))}; known: "
                         f"{', '.join(SpeechConfig.__dataclass_fields__)}")
    terms = given.get("keyterms") or []
    if not isinstance(terms, list):
        raise SystemExit("speech: keyterms is not a list of words and phrases")
    if len(terms) > KEYTERMS_MAX:
        raise SystemExit(f"speech: keyterms: {len(terms)}, Scribe takes {KEYTERMS_MAX} at most")
    for term in terms:
        if why := keyterm_problem(term):
            raise SystemExit(f"speech: keyterm {term!r}: {why} (Scribe: under {KEYTERM_CHARS} characters, "
                             f"{KEYTERM_WORDS} words at most, none of < > {{ }} [ ] \\)")
    proxy = given.get("proxy") or ""
    if not isinstance(proxy, str):
        raise SystemExit("speech: proxy is not http://<host>:<port>")
    return SpeechConfig(keyterms=[t.strip() for t in terms], proxy=proxy)


def _check_speech(cfg: PlatformConfig) -> None:
    """The way out: speech.proxy, else code.proxy (the same `platform-egress`). The key from the environment, then out
    of it: no child of this process — git, a parse worker — inherits it."""
    sp = cfg.speech
    sp.proxy = sp.proxy or (cfg.code.proxy if cfg.code else "")
    if sp.proxy and not re.fullmatch(r"http://[a-z0-9.-]+:\d{1,5}", sp.proxy):
        raise SystemExit("speech: proxy is not http://<host>:<port>")
    if len(sp.keyterms) > KEYTERMS_CHEAP:
        log.warning("speech: %d keyterms — past %d each dictation is billed 20 s at least", len(sp.keyterms),
                    KEYTERMS_CHEAP)
    cfg.stt_key = os.environ.pop("ELEVENLABS_API_KEY", "").strip()


def git_token_env(key: str) -> str:
    return f"RETINUE_GIT_TOKEN_{key.upper()}"


def _check_code(cfg: PlatformConfig) -> None:
    """The repositories: unique plain names, https on GitHub or GitLab only — the push goes where platform.yaml says
    and the platform's proxy reaches nothing else — and each one's token from the environment."""
    code = cfg.code
    if code is None:
        return
    if code.lane not in cfg.lanes:
        raise SystemExit(f"code: unknown lane {code.lane!r}; known: {', '.join(cfg.lanes)}")
    if not isinstance(code.root, str) or not REPO_NAME.fullmatch(code.root):
        raise SystemExit(f"code: root {code.root!r} is not one plain folder name under the lane's /work")
    if code.track and code.track not in {t.name for t in cfg.tracks}:
        raise SystemExit(f"code: track {code.track!r} is not a track")
    author = code.author or (cfg.base.author if cfg.base else "")
    if not re.fullmatch(r"[^<>\n]+ <[^<>@\s]+@[^<>\s]+>", author or ""):
        raise SystemExit("code: author is not «Name <email>» (or base.author) — the clones' commits are the owner's")
    code.author = author
    for name in ("mirrors", "lane_mirrors", "outbox"):
        if not isinstance(getattr(code, name), str) or not getattr(code, name).startswith("/"):
            raise SystemExit(f"code: {name} is not an absolute path")
    if type(code.every_s) is not int or code.every_s < 30:
        raise SystemExit("code: every_s is not a number of at least 30")
    if code.proxy and not re.fullmatch(r"http://[a-z0-9.-]+:\d{1,5}", code.proxy):
        raise SystemExit("code: proxy is not http://<host>:<port>")
    if code.repos and not code.proxy:
        raise SystemExit("code: proxy is required with repositories (http://platform-egress:3128): the platform has no "
                         "other way out to GitHub or GitLab")
    seen: set[str] = set()
    for repo in code.repos:
        if not isinstance(repo.name, str) or not REPO_NAME.fullmatch(repo.name) or repo.name in seen:
            raise SystemExit(f"code: repository name {repo.name!r} is not {REPO_NAME.pattern} or is taken")
        seen.add(repo.name)
        if not isinstance(repo.url, str) or not REPO_URL.fullmatch(repo.url) or ".." in repo.url:
            raise SystemExit(f"code: {repo.name}: url is not https://github.com/<owner>/<repo> or "
                             "https://gitlab.com/<group>/<project>")
        if not isinstance(repo.token, str) or not TOKEN_KEY.fullmatch(repo.token):
            raise SystemExit(f"code: {repo.name}: token is not a key name ({TOKEN_KEY.pattern}), the value lives in "
                             f"{git_token_env('<key>')}")
        if not isinstance(repo.default_branch, str) or (repo.default_branch and not BRANCH.fullmatch(repo.default_branch)):
            raise SystemExit(f"code: {repo.name}: default_branch is not a branch name")
        repo.secret = os.environ.get(git_token_env(repo.token), "").strip()
        repo.off = "" if repo.secret else f"no token {git_token_env(repo.token)}"
    # Held in the repositories alone from here: out of this process's environment, so no child it starts inherits a
    # token (git gets its own environment anyway; the parse worker too). docs/code.md says what this does not cover.
    for name in [n for n in os.environ if n.startswith("RETINUE_GIT_TOKEN_")]:
        os.environ.pop(name, None)


# A branch name the platform pushes or reads: plain, no `..`, no `@{`, no leading `-` or `/`, no `.lock` end. The
# remote's own rules (`git check-ref-format`) are checked again before a push.
BRANCH = re.compile(r"(?!.*\.\.)(?!.*//)(?!.*/\.)(?!.*@\{)(?!.*\.lock(?:/|$))[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}(?<![./])")


# What an agent can be given through the router.
BUS_TOOLS = ("ask_agent", "list_agents", "search_archive", "set_reminder", "list_reminders", "cancel_reminder",
             "move_reminder", "set_timezone", "get_attachment", "publish_trip", "draft_reply")


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
    effort: str | None = None           # low | medium | high | xhigh | max; None = the CLI's default


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
