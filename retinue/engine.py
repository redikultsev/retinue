"""The Engine seam: the only place that knows how an agent's model is run.

Today it is the Claude Agent SDK (unmodified `claude` underneath, your own login).
Another engine (Codex, an open-weight model) plugs in by implementing `Engine`.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from claude_agent_sdk import (ClaudeAgentOptions, ResultMessage, StreamEvent, SystemMessage, create_sdk_mcp_server,
                              query, tool)

from .config import EngineConfig

log = logging.getLogger("retinue.engine")


@dataclass
class EngineResult:
    text: str
    is_error: bool
    num_turns: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0


# Called with the text of the reply being written so far (the current assistant message, not a delta).
OnText = Callable[[str], Awaitable[None]]


class Engine(Protocol):
    """One call is one short run: it starts with nothing but the prompt and leaves nothing behind. What the
    agent should remember of the conversation, the router puts into the prompt."""

    async def run(self, prompt: str, on_text: OnText | None = None, turn_id: str | None = None) -> EngineResult: ...


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
          "найденные реплики с датой и автором. Если пусто — назовёт, что архив покрывает; попробуй другие слова.",
          {"query": str})
    async def search_archive(args):
        async with client(30) as http:
            response = await http.post(f"{bus_url}/archive/search", headers=headers,
                                       json={"turn": turn_id, "query": args["query"]})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    return {t.name: t for t in (ask_agent, list_agents, search_archive)}


# Set in code for every run, so that no compose file can forget them.
RUN_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no auto-update, telemetry, error reports, feature flags
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",           # no claude.ai connectors: a subscription login brings them
}


class ClaudeEngine:
    """Runs one short turn of a Claude Code agent: a new session every time, nothing kept afterwards.

    What a run may do is decided here and nowhere else:
    - the instructions are one file read at start (`engine.instructions`, mounted read-only) and passed as the
      system prompt. No settings, CLAUDE.md, hooks or skills are loaded from any folder (`setting_sources=[]`),
      so nothing the agent could write is ever read back as configuration;
    - only the MCP servers passed here exist (`strict_mcp_config`), only the built-in tools in `engine.tools`,
      and `dontAsk` denies every tool that is not in `allowed_tools`;
    - the prompt is delivered as written (`verbatim_prompts`): it carries prior turns and other people's text,
      and an `@/path` or a `/command` inside it must not make Claude Code read a file or run a command;
    - CLAUDE_CONFIG_DIR, where Claude Code writes the transcript of everything it read, is a fresh directory
      under `engine.run_root` (a tmpfs in production) and is removed when the run ends.
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

    def options(self, turn_id: str | None, streaming: bool, config_dir: str) -> ClaudeAgentOptions:
        """Everything one run is allowed, in one place. There is no `resume`: every run is a new session."""
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
            env={**RUN_ENV, "CLAUDE_CONFIG_DIR": config_dir},
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
            model=self.cfg.model,
            include_partial_messages=streaming,
        )

    async def run(self, prompt: str, on_text: OnText | None = None, turn_id: str | None = None) -> EngineResult:
        config_dir = tempfile.mkdtemp(prefix="claude-", dir=self.cfg.run_root)
        try:
            return await self._run(prompt, on_text, turn_id, config_dir)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    async def _run(self, prompt: str, on_text: OnText | None, turn_id: str | None, config_dir: str) -> EngineResult:
        options = self.options(turn_id, on_text is not None, config_dir)
        result: ResultMessage | None = None
        draft = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
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
        if result is None:
            return EngineResult(text="Агент не вернул результат.", is_error=True)
        text = result.result or ("; ".join(result.errors or []) or "Пустой ответ.")
        return EngineResult(
            text=text,
            is_error=result.is_error,
            num_turns=result.num_turns,
            cost_usd=result.total_cost_usd,
            duration_ms=result.duration_ms,
        )


class EchoEngine:
    """No-model engine for smoke tests: answers with the prompt."""

    async def run(self, prompt: str, on_text: OnText | None = None, turn_id: str | None = None) -> EngineResult:
        if on_text:
            await on_text("echo: ")
        return EngineResult(text=f"echo: {prompt}", is_error=False)


def make_engine(cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "") -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace, bus_url, bus_token)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
