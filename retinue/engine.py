"""The Engine seam: the only place that knows how an agent's model is run.

Today it is the Claude Agent SDK (unmodified `claude` underneath, your own login).
Another engine (Codex, an open-weight model) plugs in by implementing `Engine`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from claude_agent_sdk import (ClaudeAgentOptions, RateLimitEvent, RateLimitInfo, ResultError, ResultMessage,
                              StreamEvent, SystemMessage, create_sdk_mcp_server, query, tool)

from .config import EngineConfig

log = logging.getLogger("retinue.engine")

SESSION_LOST = "_Прошлый разговор не сохранился, начинаю заново._\n\n"
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


class Engine(Protocol):
    """One conversation is one session: a call continues `session_id` (None starts a new one) and returns the
    session to continue next time. The host keeps the mapping; the engine keeps the transcript."""

    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
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
          "Поставить Владельцу напоминание. В срок Роутер сам пришлёт text дословно, без тебя: пиши так, чтобы "
          "Владелец понял через несколько дней — что сделать, кому, зачем. when — местное время Владельца, в его "
          "поясе из справки «Сейчас», вида 2026-10-09T18:00. weekday — день недели, который ты имеешь в виду "
          "(«пятница»): Роутер сверит его с датой и откажет, если не совпало или время уже прошло. Тот же текст на "
          "то же время вернёт «Уже стоит». Ответ Роутера (номер, день, время) назови Владельцу.",
          {"text": str, "when": str, "weekday": str})
    async def set_reminder(args):
        return await reminders("add", {"text": args["text"], "when": args["when"], "weekday": args["weekday"]})

    @tool("list_reminders", "Активные напоминания Владельца, ближайшие первыми: номер, когда, текст.", {})
    async def list_reminders(args):
        return await reminders("list", {})

    @tool("cancel_reminder", "Отменить напоминание по номеру из list_reminders.", {"id": int})
    async def cancel_reminder(args):
        return await reminders("cancel", {"id": args["id"]})

    @tool("move_reminder",
          "Перенести напоминание по номеру из list_reminders на другое время. when и weekday — как в set_reminder.",
          {"id": int, "when": str, "weekday": str})
    async def move_reminder(args):
        return await reminders("move", {"id": args["id"], "when": args["when"], "weekday": args["weekday"]})

    return {t.name: t for t in (ask_agent, list_agents, search_archive, set_reminder, list_reminders,
                                cancel_reminder, move_reminder)}


# Set in code for every run, so that no compose file can forget them.
RUN_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no auto-update, telemetry, error reports, feature flags
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",           # no claude.ai connectors: a subscription login brings them
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

    def __init__(self, cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "") -> None:
        self.cfg = cfg
        self.workspace = workspace
        self.bus_url = bus_url
        self.bus_token = bus_token
        instructions = Path(cfg.instructions)
        if not instructions.is_file():
            raise SystemExit(f"engine: instructions file {instructions} not found")
        self.instructions = instructions.read_text()
        self.env = dict(RUN_ENV)
        if cfg.config_dir:
            os.makedirs(cfg.config_dir, mode=0o700, exist_ok=True)
            os.chmod(cfg.config_dir, 0o700)
            self.env["CLAUDE_CONFIG_DIR"] = cfg.config_dir

    def options(self, turn_id: str | None, streaming: bool, session_id: str | None = None) -> ClaudeAgentOptions:
        """Everything one run is allowed, in one place."""
        allowed, servers = list(self.cfg.allowed_tools), {}
        if self.bus_url and self.bus_token and turn_id and self.cfg.bus_tools:
            tools = bus_tools(self.bus_url, self.bus_token, turn_id)
            servers["retinue"] = create_sdk_mcp_server("retinue", tools=[tools[name] for name in self.cfg.bus_tools])
            allowed += [BUS_PREFIX + name for name in self.cfg.bus_tools]
        return ClaudeAgentOptions(
            cwd=self.workspace,
            system_prompt=self.instructions,
            setting_sources=[],
            tools=self.cfg.tools,
            allowed_tools=allowed,
            disallowed_tools=self.cfg.disallowed_tools,
            mcp_servers=servers,
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

    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
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

    async def _run(self, prompt: str, options: ClaudeAgentOptions, on_text: OnText | None) -> EngineResult:
        result: ResultMessage | None = None
        rate: RateLimitInfo | None = None
        draft = ""
        compacted = False
        try:
            async for message in query(prompt=prompt, options=options):
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
            # The CLI reports a failed run, then exits non-zero, and the SDK raises. A limit is an answer.
            if exc.api_error_status != 429 and not (rate and rate.status == "rejected"):
                raise
            return limited(rate, options.resume, compacted)
        if result is not None and result.is_error and (result.api_error_status == 429
                                                       or (rate and rate.status == "rejected")):
            return limited(rate, options.resume, compacted)
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

    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
        if on_text:
            await on_text("echo: ")
        return EngineResult(text=f"echo: {prompt}", is_error=False, session_id=session_id or "echo-session")

    async def compact(self, session_id: str | None) -> EngineResult:
        return EngineResult(text="Контекст сжат.", is_error=False, session_id=session_id)


def make_engine(cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "") -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace, bus_url, bus_token)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
