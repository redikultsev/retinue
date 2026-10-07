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

from . import travel
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
    session to continue next time. The host keeps the mapping; the engine keeps the transcript."""

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult: ...

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

    return {t.name: t for t in (ask_agent, list_agents, search_archive, set_reminder, list_reminders,
                                cancel_reminder, move_reminder, get_attachment)}


TRAVEL = "travel"  # the MCP server's name: its tools are mcp__travel__<tool>
# One call to travel-ops: a search asks the sites for minutes, and travel-ops answers JSON only at the end, so the
# server's own limit (CLI 2.1.286: wall clock and idle) is raised past the five-minute idle default, below the
# router's 900 s for the whole turn.
TRAVEL_TIMEOUT_MS = 840_000


TRAVEL_CALLS = 12  # calls to travel-ops one run may make: each can carry a few checked words to the sites


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


async def _record(bus_url: str, bus_token: str, turn_id: str, tool: str, decision: str, reason: str, args) -> None:
    """Tell the router's protocol about a decision. Never raises: the decision is made before, and stands."""
    try:
        try:
            chars = len(json.dumps(args, ensure_ascii=True, default=str))
        except Exception:
            chars = 0
        body = json.dumps({"turn": turn_id, "tool": tool, "decision": decision, "reason": reason, "chars": chars},
                          ensure_ascii=True)  # a lone surrogate in a key is \\ud800 here, not an encoder error
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            await http.post(f"{bus_url}/travel/log", content=body.encode("ascii"),
                            headers={"Authorization": f"Bearer {bus_token}", "Content-Type": "application/json"})
    except Exception as exc:
        log.warning("travel-ops call %s (%s) not recorded at the router: %s", tool, decision, type(exc).__name__)


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
                 travel_url: str = "") -> None:
        self.cfg = cfg
        self.workspace = workspace
        self.bus_url = bus_url
        self.bus_token = bus_token
        self.travel_url = travel_url  # travel-ops' MCP over HTTP, on the internal network `travel`
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

    def options(self, turn_id: str | None, streaming: bool, session_id: str | None = None) -> ClaudeAgentOptions:
        """Everything one run is allowed, in one place."""
        allowed, servers = list(self.cfg.allowed_tools), {}
        denied, hooks = list(self.cfg.disallowed_tools), {}
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
        return ClaudeAgentOptions(
            cwd=self.workspace,
            system_prompt=self.instructions,
            setting_sources=[],
            tools=self.cfg.tools,
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
        )

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
        try:
            try:
                return await self._run(prompt, self.options(turn_id, on_text is not None, session_id), on_text)
            except ResultError as exc:
                # The transcript is gone (e.g. the volume was recreated): start over instead of failing every turn.
                if not session_id or "No conversation found" not in str(exc):
                    raise
                log.warning("session %s not found, starting a new one", session_id)
                result = await self._run(prompt, self.options(turn_id, on_text is not None), on_text)
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
            result = await self._run(text, self.options(turn_id, on_text is not None), on_text)
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


def rate_of(info: RateLimitInfo | None) -> dict:
    """The subscription limit state as the CLI last reported it: status, window, share used, when it resets."""
    if info is None:
        return {}
    fields = {"status": info.status, "rate_limit_type": info.rate_limit_type, "utilization": info.utilization,
              "resets_at": info.resets_at}
    return {key: value for key, value in fields.items() if value is not None}


def limited(rate: RateLimitInfo | None, session_id: str | None, compacted: bool) -> EngineResult:
    """The run was refused by the subscription limit: an answer for the router, not a crash."""
    log.warning("subscription limit: %s", rate_of(rate) or "HTTP 429, no rate limit event")
    return EngineResult(text=LIMIT, is_error=True, session_id=session_id, compacted=compacted, limit=True,
                        limit_until=rate.resets_at if rate else None, rate_limit=rate_of(rate))


class EchoEngine:
    """No-model engine for smoke tests: answers with the prompt."""

    async def run(self, prompt: Prompt, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
        if on_text:
            await on_text("echo: ")
        if isinstance(prompt, list):  # the text, and how many files came with it
            files = sum(block.get("type") in ("image", "document") for block in prompt)
            prompt = "\n".join(b["text"] for b in prompt if b.get("type") == "text") + f"\n(файлов: {files})"
        return EngineResult(text=f"echo: {prompt}", is_error=False, session_id=session_id or "echo-session")

    async def compact(self, session_id: str | None) -> EngineResult:
        return EngineResult(text="Контекст сжат.", is_error=False, session_id=session_id)


def make_engine(cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "",
                travel_url: str = "") -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace, bus_url, bus_token, travel_url)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
