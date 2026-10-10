"""The Engine seam: the only place that knows how an agent's model is run.

Today it is the Claude Agent SDK (unmodified `claude` underneath, your own login).
Another engine (Codex, an open-weight model) plugs in by implementing `Engine`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from claude_agent_sdk import (ClaudeAgentOptions, HookMatcher, RateLimitEvent, RateLimitInfo, ResultError,
                              ResultMessage, StreamEvent, SystemMessage, create_sdk_mcp_server, query, tool)

from . import courier, kbcheck, lifehub, travel
from .config import EngineConfig

log = logging.getLogger("retinue.engine")

SESSION_LOST = "_Прошлый разговор не сохранился, начинаю заново._\n\n"
UNREADABLE = "_Файл не прочитался, поэтому разговор начат заново._\n\n"
TOO_LARGE = "_Разговор с файлами стал больше, чем принимает модель, поэтому начат заново._\n\n"
UNREADABLE_NOTE = ("[Справка от Роутера: файлы из этой реплики модель прочитать не смогла — API их отклонил, и "
                   "разговор начат заново. Скажи Владельцу об этом одной фразой и ответь на то, на что можно "
                   "ответить без файлов. Понадобится вложение — достань его инструментом get_attachment по номеру "
                   "из метки.]")
TOO_LARGE_NOTE = ("[Справка от Роутера: с файлами разговор стал больше, чем принимает API, и начат заново. Скажи "
                  "Владельцу об этом одной фразой и ответь. Понадобится вложение — достань его инструментом "
                  "get_attachment по номеру из метки.]")
# A 400 is about the files only when it names them; any other 400 is an ordinary failure.
ABOUT_FILES = re.compile(r"image|document|pdf|media", re.IGNORECASE)
FILE_SESSIONS = "retinue-file-sessions.json"  # in the config dir: sessions that hold files, across restarts
COMPACT = "/compact"  # Claude Code's own command; the only prompt that is not delivered verbatim
STRUCTURED = "StructuredOutput"  # the CLI's own tool that carries an answer by a JSON schema (`--json-schema`)
BARE_TURNS = 4  # a bare run answers by the schema at once; a few steps leave room for one retry on a mismatch
LIMIT = "Лимит подписки исчерпан."  # the router words it for the owner; this text is for logs
# Tokens of a run as the API reports them -> the names the router keeps.
USAGE = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
         "cache_read_input_tokens": "cache_read_tokens", "cache_creation_input_tokens": "cache_write_tokens"}


@dataclass
class EngineResult:
    text: str
    is_error: bool
    session_id: str | None = None  # the session to resume next time
    num_turns: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0
    compacted: bool = False          # the CLI compacted the session during this run
    new_session: bool = False        # the session to resume was gone: this run started a new one
    limit: bool = False              # the subscription limit refused the run
    limit_until: int | None = None   # when the limit window resets, unix seconds, if the CLI said so
    usage: dict = field(default_factory=dict)       # tokens of this run: input, output, cache_read, cache_write
    rate_limit: dict = field(default_factory=dict)  # the limit state the CLI last reported in this run


# Called with the text of the reply being written so far (the current assistant message, not a delta).
OnText = Callable[[str], Awaitable[None]]
# What one turn says: text, or content blocks (text, image, document) when the owner sent files.
Prompt = str | list[dict]


class FilesRefused(Exception):
    """The API refused a run because of files: a file it cannot read (400 naming it) or a request swollen by them
    (413). The session holds them and would fail on every resume. `args[0]` is the HTTP status."""


def has_files(prompt: Prompt) -> bool:
    return isinstance(prompt, list) and any(block.get("type") in ("image", "document") for block in prompt)


def as_prompt(prompt: Prompt):
    """What `query()` takes. Text goes as it is. Content blocks go as streaming input: one user message, built
    before the stream, so that nothing can fail inside it. A new stream for every call: a retry needs its own."""
    if isinstance(prompt, str):
        return prompt
    message = {"type": "user", "session_id": "", "parent_tool_use_id": None,
               "message": {"role": "user", "content": prompt}}

    async def once():
        yield message

    return once()


class Engine(Protocol):
    """One conversation is one session: a call continues `session_id` (None starts a new one) and returns the
    session to continue next time. The host keeps the mapping; the engine keeps the transcript. With `schema` the
    answer is JSON by that schema; `bare` runs it with no tool at all — the mail's triage, one letter per run."""

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None, schema: dict | None = None, bare: bool = False) -> EngineResult: ...

    async def compact(self, session_id: str | None) -> EngineResult: ...


BUS_PREFIX = "mcp__retinue__"  # how the model sees the tools below


def bus_tools(bus_url: str, bus_token: str, turn_id: str) -> dict:
    """Everything an agent can do outside its container goes through the router's bus, bound to the current
    turn and signed with the agent's own token. Which of these an agent gets is `engine.bus_tools`."""
    headers = {"Authorization": f"Bearer {bus_token}"}

    def client(timeout: float) -> httpx.AsyncClient:
        # trust_env=False: the router is a neighbour on the internal network, never reached through the proxy.
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    @tool("ask_agent",
          "Спросить другого агента Retinue через Роутер и дождаться ответа. Роутер решает, разрешено ли; "
          "отказ приходит с причиной. agent — id агента, text — самодостаточный вопрос: агент не видит этот "
          "разговор. Список агентов — инструмент list_agents.",
          {"agent": str, "text": str})
    async def ask_agent(args):
        async with client(900) as http:
            response = await http.post(f"{bus_url}/call", headers=headers,
                                       json={"turn": turn_id, "agent": args["agent"], "text": args["text"]})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    @tool("list_agents", "Агенты Retinue, к которым тебе можно обращаться: id, имя, описание.", {})
    async def list_agents(args):
        async with client(30) as http:
            agents = (await http.get(f"{bus_url}/agents", headers=headers)).json()
        return {"content": [{"type": "text", "text": json.dumps(agents, ensure_ascii=False)}]}

    @tool("search_archive",
          "Найти в архиве то, что говорилось раньше: реплики Владельца, твои прошлые ответы, сообщения системы. "
          "query — одно-три ключевых слова, лучше существительные: окончания подбираются сами. Возвращает "
          "найденные реплики с датой, автором и пометкой «этот разговор» или «прошлый разговор». Если пусто — назовёт, что архив покрывает; попробуй другие слова.",
          {"query": str})
    async def search_archive(args):
        async with client(30) as http:
            response = await http.post(f"{bus_url}/archive/search", headers=headers,
                                       json={"turn": turn_id, "query": args["query"]})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    async def reminders(action: str, body: dict) -> dict:
        async with client(30) as http:
            response = await http.post(f"{bus_url}/reminders/{action}", headers=headers, json={"turn": turn_id, **body})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    @tool("set_reminder",
          "Поставить Владельцу напоминание. В срок Роутер разбудит тебя, и ты напишешь его своими словами; если не "
          "сможешь — пришлёт text дословно. Поэтому пиши text так, чтобы Владелец понял через несколько дней — что "
          "сделать, кому, зачем. when — местное время Владельца, в его "
          "поясе из справки «Сейчас», вида 2026-10-09T18:00. weekday — день недели, который ты имеешь в виду "
          "(«пятница»): Роутер сверит его с датой и откажет, если не совпало или время уже прошло. Тот же текст на "
          "то же время вернёт «Уже стоит». Владельцу назови день и время из ответа Роутера, id — нет.",
          {"text": str, "when": str, "weekday": str})
    async def set_reminder(args):
        return await reminders("add", {"text": args["text"], "when": args["when"], "weekday": args["weekday"]})

    @tool("list_reminders", "Активные напоминания Владельца, ближайшие первыми: когда, текст, [id].", {})
    async def list_reminders(args):
        return await reminders("list", {})

    @tool("cancel_reminder", "Отменить напоминание по id из list_reminders.", {"id": int})
    async def cancel_reminder(args):
        return await reminders("cancel", {"id": args["id"]})

    @tool("move_reminder",
          "Перенести напоминание по id из list_reminders на другое время. when и weekday — как в set_reminder.",
          {"id": int, "when": str, "weekday": str})
    async def move_reminder(args):
        return await reminders("move", {"id": args["id"], "when": args["when"], "weekday": args["weekday"]})

    @tool("get_attachment",
          "Достать снова то, что прислал Владелец, по номеру из метки [вложение #N: …]. После сжатия картинок и "
          "PDF в разговоре нет — этот инструмент вернёт картинку и текст: расшифровку, текст "
          "документа; у PDF — только его текстовый слой.",
          {"id": int})
    async def get_attachment(args):
        async with client(60) as http:
            response = await http.post(f"{bus_url}/attachments/get", headers=headers,
                                       json={"turn": turn_id, "id": args["id"]})
        data = response.json()
        images = [{"type": "image", "data": image["data"], "mimeType": image["mimeType"]}
                  for image in data.get("images", [])]
        return {"content": [*images, {"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    @tool("publish_trip",
          "Опубликовать страницу поездки в хабе Владельца: все варианты, которые ты нашла, с ценами, временем, "
          "отзывами и фото. Варианты называй ссылкой (link.url) и search_id из ответов travel-ops: цены, продавца и "
          "время Роутер возьмёт из travel-ops сам. Ответ — адрес страницы; дай его Владельцу в конце ответа ссылкой "
          "«подробнее». Отказ называет поле — исправь и повтори.",
          lifehub.TRIP_SCHEMA)
    async def publish_trip(args):
        async with client(120) as http:
            response = await http.post(f"{bus_url}/trips/publish", headers=headers,
                                       json={"turn": turn_id, "trip": args})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    @tool("draft_reply",
          "Предложить Владельцу ответ человеку — на письмо или сообщение в Telegram, — когда ответа правда ждут. "
          "reply_to — id входящего из архива (mail:… или tgb:…): адрес, тему и тред Роутер возьмёт оттуда. Новое "
          "письмо — to и subject, только на адрес, с которым Владелец уже переписывался. text — весь текст, как он "
          "уйдёт. Роутер покажет Владельцу карточку; уйдёт, только если он нажмёт «Отправить». Отказ называет, что "
          "исправить; «поправь» от Владельца — новый вызов с replaces.",
          courier.DRAFT_SCHEMA)
    async def draft_reply(args):
        async with client(120) as http:
            response = await http.post(f"{bus_url}/courier/draft", headers=headers,
                                       json={"turn": turn_id, "draft": args})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    return {t.name: t for t in (ask_agent, list_agents, search_archive, set_reminder, list_reminders,
                                cancel_reminder, move_reminder, get_attachment, publish_trip, draft_reply)}


TRAVEL = "travel"  # the MCP server's name: its tools are mcp__travel__<tool>
# One call to travel-ops: a search asks the sites for minutes, and travel-ops answers JSON only at the end, so the
# server's own limit (CLI 2.1.286: wall clock and idle) is raised past the five-minute idle default, below the
# router's 900 s for the whole turn.
TRAVEL_TIMEOUT_MS = 840_000


TRAVEL_CALLS = 12  # calls to travel-ops one run may make: each can carry a few checked words to the sites


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


async def _record(bus_url: str, bus_token: str, turn_id: str, tool: str, decision: str, reason: str, args,
                  where: str = "travel", path: str = "") -> None:
    """Tell the router's protocol about a decision (`/travel/log`, `/kb/log`). Never raises: the decision is made
    before, and stands."""
    try:
        try:
            chars = len(json.dumps(args, ensure_ascii=True, default=str))
        except Exception:
            chars = 0
        body = json.dumps({"turn": turn_id, "tool": tool, "decision": decision, "reason": reason, "chars": chars,
                           "path": path},
                          ensure_ascii=True)  # a lone surrogate in a key is \\ud800 here, not an encoder error
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            await http.post(f"{bus_url}/{where}/log", content=body.encode("ascii"),
                            headers={"Authorization": f"Bearer {bus_token}", "Content-Type": "application/json"})
    except Exception as exc:
        log.warning("%s call %s (%s) not recorded at the router: %s", where, tool, decision, type(exc).__name__)


def travel_guard(bus_url: str, bus_token: str, turn_id: str):
    """PreToolUse for every call to travel-ops. Its arguments are what the travel sites receive, and the agent
    holds the owner's data: each field is checked against `travel.FORMS` in code and must be sent exactly as the
    check would write it (`travel.exact`); a wrong one is refused with the reason, which the model reads as the
    tool's answer. At most TRAVEL_CALLS calls go out in one run.

    It fails closed: CLI 2.1.286 takes a hook that raised for «no decision», and the call would go out. So every
    error inside the check is a refusal, the decision is made before the router is told, and telling the router
    cannot raise. Other tools are not this hook's."""
    allowed = 0

    async def guard(hook_input, tool_use_id, context) -> dict:
        nonlocal allowed
        try:
            name = str(hook_input.get("tool_name", ""))
        except Exception:
            return _deny("Не отправлено: вызов не прочитан.")
        if not name.startswith(travel.PREFIX):
            return {}
        tool, args = name[len(travel.PREFIX):], None
        try:
            args = hook_input.get("tool_input")
            if allowed >= TRAVEL_CALLS:
                raise travel.Refused(f"не больше {TRAVEL_CALLS} вызовов travel-ops за один ход: ответь тем, что "
                                     "уже нашлось, и предложи продолжить следующей репликой")
            travel.exact(tool, args)
            decision, reason = "allow", ""
        except travel.Refused as exc:
            decision, reason = "deny", f"Не отправлено на сайты: {exc}"
        except Exception as exc:
            decision, reason = "deny", f"Не отправлено на сайты: проверка аргументов не удалась ({type(exc).__name__})."
        if decision == "allow":
            allowed += 1
        out = {} if decision == "allow" else _deny(reason)
        await _record(bus_url, bus_token, turn_id, tool, decision, reason, args)
        return out
    return guard


# The knowledge base: Claude Code's own file tools, each call checked by `memory_guard` before it runs.
MEMORY_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write"]
PATH_FIELD = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "Grep": "path", "Glob": "path"}
WRITES = {"Edit", "Write"}
MEMORY_HINT = ("Записи — только *.md в <Пространство>/{knowledge,profile,journal,artifacts}/; строки Витрины — в "
               "<Пространство>/AGENTS.md ниже маркера.")


NO_WORD = "Не записано: Роутер не подтвердил, что этот ход ещё идёт и пишет он один — правка не сохранилась бы."


async def _ack(bus_url: str, bus_token: str, turn_id: str | None, tool: str, path: str) -> bool:
    """Ask the router, synchronously, whether this run may write now: its turn is open and holds the queue. Any
    failure — no bus, no answer, a refusal — is a no: a run the router gave up on must not keep writing."""
    if not (bus_url and turn_id):
        return False
    try:
        body = json.dumps({"turn": turn_id, "tool": tool, "decision": "allow", "reason": "", "path": path},
                          ensure_ascii=True)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            response = await http.post(f"{bus_url}/kb/log", content=body.encode("ascii"),
                                       headers={"Authorization": f"Bearer {bus_token}",
                                                "Content-Type": "application/json"})
        return response.status_code == 200 and response.json().get("ok") is True
    except Exception:
        return False


def memory_guard(root: str, policy: kbcheck.Policy, writable: bool, bus_url: str = "", bus_token: str = "",
                 turn_id: str | None = None):
    """PreToolUse for Claude Code's file tools. Reading stays inside the base (`root`), path by path after symlinks
    are resolved: the container holds the subscription token in /proc and the session's transcripts. Writing goes
    only where the policy lets her (`kbcheck`, the same rules the router and the hub check after the turn): records
    and showcase lines below the marker; nothing code would run. A run outside a turn (the morning summary) only
    reads. Fails closed, like the travel guard: an error inside the check is a refusal. Every decision on a file
    tool is a protocol line `kb/<tool>` at the router, as for travel-ops — when the run has a turn to file it under."""
    base = os.path.realpath(root)

    async def guard(hook_input, tool_use_id, context) -> dict:
        out = await decide(hook_input)
        try:
            name = str(hook_input.get("tool_name", ""))
            args = hook_input.get("tool_input")
            path = str(args.get(PATH_FIELD[name]) or "")[:300] if name in PATH_FIELD and isinstance(args, dict) else ""
            if name in WRITES and not out:
                # The router's word, every time: this turn is open and the one holding the queue. Its answer is the
                # protocol line too.
                if not await _ack(bus_url, bus_token, turn_id, name, path):
                    out = _deny(NO_WORD)
                    await _record(bus_url, bus_token, turn_id, name, "deny", NO_WORD, args, "kb", path)
            elif name in PATH_FIELD and bus_url and turn_id:
                reason = out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "") if out else ""
                await _record(bus_url, bus_token, turn_id, name, "deny" if out else "allow", reason, args, "kb", path)
        except Exception as exc:
            log.warning("a decision on the base was not recorded: %s", type(exc).__name__)
            if str(hook_input.get("tool_name", "")) in WRITES:
                out = _deny(NO_WORD)
        return out

    async def decide(hook_input) -> dict:
        try:
            name = str(hook_input.get("tool_name", ""))
            if name not in PATH_FIELD:
                return {}
            args = hook_input.get("tool_input")
            if not isinstance(args, dict):
                return _deny("Не выполнено: вызов не прочитан.")
            raw = args.get(PATH_FIELD[name])
            if not raw:
                return _deny(f"Не выполнено: укажи {PATH_FIELD[name]} — абсолютный путь в базе, от {root}/.")
            raw = str(raw)
            if not raw.startswith("/"):
                return _deny(f"Не выполнено: путь — абсолютный, от {root}/.")
            path = os.path.realpath(raw)
            if path != base and not path.startswith(base + "/"):
                return _deny(f"Не выполнено: вне базы. Читать и писать можно только в {root}/.")
            rel = os.path.relpath(path, base)
            if kbcheck.hidden(rel) or kbcheck.hidden(os.path.relpath(os.path.normpath(raw), root)):
                # .git/ inside the base is storage git never sees or cleans; .claude/ is settings: not notes.
                return _deny(f"Не выполнено: {rel} — имя с точки, скрытое; в базе таких нет.")
            for field_name in ("pattern", "glob") if name in ("Glob", "Grep") else ():
                pattern = str(args.get(field_name) or "")
                if name == "Glob" or field_name == "glob":
                    if pattern.startswith("/") or ".." in pattern.split("/") or kbcheck.hidden(pattern):
                        return _deny(f"Не выполнено: {field_name} — относительный, без «..» и скрытых имён; папку "
                                     "задаёт path.")
            if name not in WRITES:
                return {}
            if not writable:
                return _deny("Не записано: вне хода разговора база только для чтения.")
            if os.path.islink(raw) or os.path.isdir(path):
                return _deny(f"Не записано: {rel} — не файл.")
            if kbcheck.any_match(rel, policy.lines_below):
                return _showcase_edit(name, args, path, rel, policy.marker)
            if kbcheck.any_match(rel, policy.never) or not kbcheck.any_match(rel, policy.writable):
                return _deny(f"Не записано: {rel} — сюда писать нельзя. {MEMORY_HINT}")
            return {}
        except Exception as exc:
            return _deny(f"Не выполнено: проверка пути не удалась ({type(exc).__name__}).")
    return guard


def _showcase_edit(name: str, args: dict, path: str, rel: str, marker: str) -> dict:
    """A showcase changes only below its marker: above it are the Space's rules."""
    text = Path(path).read_text() if os.path.isfile(path) else ""
    if not marker or text.count(marker) != 1:
        return _deny(f"Не записано: в {rel} нет маркера Витрины — такую Витрину правит только Владелец.")
    lines_start = text.index(marker) + len(marker)
    if name == "Write":
        content = str(args.get("content", ""))
        if not content.startswith(text[:lines_start]) or content.count(marker) != 1:
            return _deny(f"Не записано: {rel} — выше маркера Витрины правила; меняй только строки ниже него.")
        return {}
    old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
    first = text.find(old) if old else -1
    if first < lines_start or marker in new:
        return _deny(f"Не записано: {rel} — выше маркера Витрины правила; меняй только строки ниже него.")
    return {}


# Set in code for every run, so that no compose file can forget them.
RUN_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no auto-update, telemetry, error reports, feature flags
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",           # no claude.ai connectors: a subscription login brings them
    # Connecting and listing an MCP server (CLI 2.1.286 defaults: 5 s and 30 s): travel-ops down or hung must not
    # hold the owner's turn. A tool call has its own limit (the server's `timeout`).
    "MCP_CONNECT_TIMEOUT_MS": "5000",
    "MCP_TIMEOUT": "5000",
}


class ClaudeEngine:
    """Runs one turn of a Claude Code agent in its conversation's session.

    What a run may do is decided here and nowhere else:
    - the instructions are one file read at start (`engine.instructions`, mounted read-only) and passed as the
      system prompt. No settings, CLAUDE.md, hooks or skills are loaded from any folder (`setting_sources=[]`),
      so nothing the agent could write is ever read back as configuration;
    - only the MCP servers passed here exist (`strict_mcp_config`), only the built-in tools in `engine.tools`,
      and `dontAsk` denies every tool that is not in `allowed_tools`;
    - the prompt is delivered as written (`verbatim_prompts`): it carries prior turns and other people's text,
      and an `@/path` or a `/command` inside it must not make Claude Code read a file or run a command;
    - CLAUDE_CONFIG_DIR, where Claude Code keeps the session transcript (everything the model read), is
      `engine.config_dir`: one directory for the agent, on its own volume, readable by nobody else.
    - Claude Code compacts a long session by itself; `compact()` does it on request.
    """

    def __init__(self, cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "",
                 travel_url: str = "", memory: str = "", memory_policy: str = "") -> None:
        self.cfg = cfg
        self.workspace = workspace
        self.bus_url = bus_url
        self.bus_token = bus_token
        self.travel_url = travel_url  # travel-ops' MCP over HTTP, on the internal network `travel`
        self.memory = memory  # the knowledge base's folder: her file tools work there and nowhere else
        self.memory_policy = kbcheck.Policy.load(memory_policy) if memory else None
        instructions = Path(cfg.instructions)
        if not instructions.is_file():
            raise SystemExit(f"engine: instructions file {instructions} not found")
        self.instructions = instructions.read_text()
        self.env = dict(RUN_ENV)
        if cfg.config_dir:
            os.makedirs(cfg.config_dir, mode=0o700, exist_ok=True)
            os.chmod(cfg.config_dir, 0o700)
            self.env["CLAUDE_CONFIG_DIR"] = cfg.config_dir
        self.sessions_file = Path(cfg.config_dir) / FILE_SESSIONS if cfg.config_dir else None
        self.with_files: set[str] = set()  # sessions that have been sent files
        if self.sessions_file and self.sessions_file.is_file():
            self.with_files = set(json.loads(self.sessions_file.read_text()))

    def _took_files(self, session_id: str | None) -> None:
        if session_id and session_id not in self.with_files:
            self.with_files.add(session_id)
            if self.sessions_file:
                self.sessions_file.write_text(json.dumps(sorted(self.with_files)))

    def options(self, turn_id: str | None, streaming: bool, session_id: str | None = None,
                schema: dict | None = None, bare: bool = False) -> ClaudeAgentOptions:
        """Everything one run is allowed, in one place. A `bare` run has no tool, no server, no hook and no folder:
        it reads somebody else's letter and can only answer by the schema."""
        if bare:
            return ClaudeAgentOptions(
                cwd=self.workspace, system_prompt=self.instructions, setting_sources=[], tools=[],
                allowed_tools=[STRUCTURED] if schema else [], disallowed_tools=list(self.cfg.disallowed_tools),
                mcp_servers={}, strict_mcp_config=True, permission_mode="dontAsk", verbatim_prompts=True,
                env=dict(self.env), max_turns=BARE_TURNS, max_budget_usd=self.cfg.max_budget_usd, model=self.cfg.model,
                output_format={"type": "json_schema", "schema": schema} if schema else None)
        allowed, servers = list(self.cfg.allowed_tools), {}
        denied, hooks = list(self.cfg.disallowed_tools), {}
        builtin, dirs = self.cfg.tools, []
        if schema:
            allowed.append(STRUCTURED)
        if self.bus_url and self.bus_token and turn_id and self.cfg.bus_tools:
            tools = bus_tools(self.bus_url, self.bus_token, turn_id)
            servers["retinue"] = create_sdk_mcp_server("retinue", tools=[tools[name] for name in self.cfg.bus_tools])
            allowed += [BUS_PREFIX + name for name in self.cfg.bus_tools]
        if self.travel_url and self.bus_url and turn_id:
            # Every tool of travel-ops and its whole answer; the arguments of each call pass the guard first.
            servers[TRAVEL] = {"type": "http", "url": self.travel_url, "timeout": TRAVEL_TIMEOUT_MS, "alwaysLoad": True}
            allowed.append(f"mcp__{TRAVEL}")
            denied += [travel.PREFIX + tool for tool in travel.NOT_HERS]
            hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[travel_guard(self.bus_url, self.bus_token,
                                                                                  turn_id)])]
        if self.memory:
            # The base is a working directory of the run (so the CLI lets the tools reach it), never a source of
            # settings: `setting_sources=[]` stands. Writing only in a turn the router runs under its queue.
            builtin = [*(builtin or []), *[t for t in MEMORY_TOOLS if t not in (builtin or [])]]
            allowed += [t for t in MEMORY_TOOLS if t not in allowed]
            dirs = [self.memory]
            hooks.setdefault("PreToolUse", []).append(
                HookMatcher(matcher=None, hooks=[memory_guard(self.memory, self.memory_policy, turn_id is not None,
                                                              self.bus_url, self.bus_token, turn_id)]))
        return ClaudeAgentOptions(
            cwd=self.workspace,
            system_prompt=self.instructions,
            setting_sources=[],
            tools=builtin,
            add_dirs=dirs,
            allowed_tools=allowed,
            disallowed_tools=denied,
            mcp_servers=servers,
            hooks=hooks or None,
            strict_mcp_config=True,
            permission_mode="dontAsk",
            verbatim_prompts=True,
            env=dict(self.env),
            # session_id comes only from the host's own table, never from a message.
            resume=session_id,
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
            model=self.cfg.model,
            include_partial_messages=streaming,
            output_format={"type": "json_schema", "schema": schema} if schema else None,
        )

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None, schema: dict | None = None, bare: bool = False) -> EngineResult:
        def options(session: str | None = None) -> ClaudeAgentOptions:
            return self.options(turn_id, on_text is not None, session, schema, bare)

        try:
            try:
                return await self._run(prompt, options(session_id), on_text)
            except ResultError as exc:
                # The transcript is gone (e.g. the volume was recreated): start over instead of failing every turn.
                if not session_id or "No conversation found" not in str(exc):
                    raise
                log.warning("session %s not found, starting a new one", session_id)
                result = await self._run(prompt, options(), on_text)
                result.text = SESSION_LOST + result.text
                result.new_session = True
                return result
        except FilesRefused as refused:
            # The files are in the transcript now: every resume would send them again and fail. The same turn runs in
            # a new session without its files, the owner is told, and the model learns how to get a file back.
            status = refused.args[0]
            log.warning("the API refused the run because of files (%s); starting a new session without them", status)
            note = UNREADABLE_NOTE if status == 400 else TOO_LARGE_NOTE
            text = f"{note}\n\n{prompt}" if isinstance(prompt, str) else \
                [{"type": "text", "text": note}] + [b for b in prompt if b.get("type") == "text"]
            result = await self._run(text, options(), on_text)
            result.text = (UNREADABLE if status == 400 else TOO_LARGE) + result.text
            result.new_session = True
            return result

    def _about_files(self, status: int | None, message: str, prompt: Prompt, session_id: str | None) -> bool:
        """Did the API refuse the run because of files: 413, or a 400 that names them — in a turn with files or in a
        session that has been sent some."""
        files_here = has_files(prompt) or (session_id is not None and session_id in self.with_files)
        return files_here and (status == 413 or (status == 400 and bool(ABOUT_FILES.search(message))))

    async def compact(self, session_id: str | None) -> EngineResult:
        """Squeeze the session on the owner's request. The prompt is our constant, so the slash command may run."""
        if not session_id:
            return EngineResult(text="Сжимать нечего: разговор ещё не начат.", is_error=False)
        options = dataclasses.replace(self.options(None, False, session_id), verbatim_prompts=False)
        result = await self._run(COMPACT, options, None)
        result.session_id = result.session_id or session_id
        if not result.is_error:
            result.text = "Контекст сжат."
        return result

    async def _run(self, prompt: Prompt, options: ClaudeAgentOptions, on_text: OnText | None) -> EngineResult:
        result: ResultMessage | None = None
        rate: RateLimitInfo | None = None
        draft = ""
        compacted = False
        try:
            async for message in query(prompt=as_prompt(prompt), options=options):
                if isinstance(message, ResultMessage):
                    result = message
                elif isinstance(message, RateLimitEvent):
                    # Sent when the limit state changes: allowed, allowed_warning or rejected.
                    rate = message.rate_limit_info
                elif isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                    # Claude Code squeezed the session, by itself or on /compact: the router sends «now» again.
                    compacted = True
                    log.info("session compacted: %s", message.data.get("compact_metadata"))
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    # What the model really got. On a subscription apiKeySource is not an API key.
                    log.info("run init: tools=%s mcp=%s apiKeySource=%s model=%s", message.data.get("tools"),
                             [server.get("name") for server in message.data.get("mcp_servers") or []],
                             message.data.get("apiKeySource"), message.data.get("model"))
                elif isinstance(message, StreamEvent) and on_text and message.parent_tool_use_id is None:
                    event = message.event
                    if event.get("type") == "message_start":
                        draft = ""  # a new assistant message (e.g. after a tool call) starts a new draft
                    elif event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                        draft += event["delta"]["text"]
                        await on_text(draft)
        except ResultError as exc:
            said = f"{exc} {exc.result or ''} {exc.errors or ''} {result.result if result else ''}"
            if self._about_files(exc.api_error_status, said, prompt, options.resume):
                raise FilesRefused(exc.api_error_status) from exc
            # The CLI reports a failed run, then exits non-zero, and the SDK raises. A limit is an answer.
            if exc.api_error_status != 429 and not (rate and rate.status == "rejected"):
                raise
            return limited(rate, options.resume, compacted)
        if result is not None and result.is_error and (result.api_error_status == 429
                                                       or (rate and rate.status == "rejected")):
            return limited(rate, options.resume, compacted)
        if result is not None and result.is_error and self._about_files(
                result.api_error_status, f"{result.result or ''} {result.errors or ''}", prompt, options.resume):
            raise FilesRefused(result.api_error_status)
        if result is not None and not result.is_error and has_files(prompt):
            self._took_files(result.session_id)
        if result is None:
            return EngineResult(text="Агент не вернул результат.", is_error=True, session_id=options.resume,
                                compacted=compacted)
        text = result.result or ("; ".join(result.errors or []) or "Пустой ответ.")
        if isinstance(result.structured_output, dict) and not result.is_error:
            # The answer by the schema, as the text the router reads: one JSON object.
            text = json.dumps(result.structured_output, ensure_ascii=False)
        return EngineResult(
            text=text,
            is_error=result.is_error,
            session_id=result.session_id,
            num_turns=result.num_turns,
            cost_usd=result.total_cost_usd,
            duration_ms=result.duration_ms,
            compacted=compacted,
            usage={name: int(value) for key, name in USAGE.items()
                   if isinstance(value := (result.usage or {}).get(key), (int, float))},
            rate_limit=rate_of(rate),
        )


LIMIT_WINDOWS = ("five_hour", "seven_day")  # the subscription's two windows, as the CLI names them


def rate_of(info: RateLimitInfo | None) -> dict:
    """The subscription limit state as the CLI last reported it: status, window, share used, when it resets — and
    both windows, which CLI 2.1.286 sends with every event as `unifiedWindows` (in `raw`: the SDK does not model
    them). The top-level share is only the limiting window's, and only near the limit."""
    if info is None:
        return {}
    fields = {"status": info.status, "rate_limit_type": info.rate_limit_type, "utilization": info.utilization,
              "resets_at": info.resets_at}
    windows = {}
    raw = info.raw.get("unifiedWindows") if isinstance(info.raw, dict) else None
    for name in LIMIT_WINDOWS:
        window = raw.get(name) if isinstance(raw, dict) else None
        share = window.get("utilization") if isinstance(window, dict) else None
        if isinstance(share, (int, float)) and not isinstance(share, bool):
            resets = window.get("resetsAt")
            windows[name] = {"utilization": float(share),
                             "resets_at": int(resets) if isinstance(resets, (int, float)) else None}
    fields["windows"] = windows or None
    return {key: value for key, value in fields.items() if value is not None}


def limited(rate: RateLimitInfo | None, session_id: str | None, compacted: bool) -> EngineResult:
    """The run was refused by the subscription limit: an answer for the router, not a crash."""
    log.warning("subscription limit: %s", rate_of(rate) or "HTTP 429, no rate limit event")
    return EngineResult(text=LIMIT, is_error=True, session_id=session_id, compacted=compacted, limit=True,
                        limit_until=rate.resets_at if rate else None, rate_limit=rate_of(rate))


class EchoEngine:
    """No-model engine for smoke tests: answers with the prompt."""

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None, schema: dict | None = None, bare: bool = False) -> EngineResult:
        if on_text:
            await on_text("echo: ")
        if isinstance(prompt, list):  # the text, and how many files came with it
            files = sum(block.get("type") in ("image", "document") for block in prompt)
            prompt = "\n".join(b["text"] for b in prompt if b.get("type") == "text") + f"\n(файлов: {files})"
        return EngineResult(text=f"echo: {prompt}", is_error=False, session_id=session_id or "echo-session")

    async def compact(self, session_id: str | None) -> EngineResult:
        return EngineResult(text="Контекст сжат.", is_error=False, session_id=session_id)


def make_engine(cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "",
                travel_url: str = "", memory: str = "", memory_policy: str = "") -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace, bus_url, bus_token, travel_url, memory, memory_policy)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
