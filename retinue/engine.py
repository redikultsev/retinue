"""The Engine seam: the only place that knows how an agent's model is run.

Today it is the Claude Agent SDK (unmodified `claude` underneath, your own login).
Another engine (Codex, an open-weight model) plugs in by implementing `Engine`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, StreamEvent, create_sdk_mcp_server, query, tool
from claude_agent_sdk._errors import ResultError

from .config import EngineConfig

log = logging.getLogger("retinue.engine")
SESSION_LOST = "_Прошлый разговор не сохранился, начинаю заново._\n\n"


@dataclass
class EngineResult:
    text: str
    session_id: str | None
    is_error: bool
    num_turns: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0


# Called with the text of the reply being written so far (the current assistant message, not a delta).
OnText = Callable[[str], Awaitable[None]]


class Engine(Protocol):
    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult: ...


BUS_TOOL = "mcp__retinue__ask_agent"


def bus_server(bus_url: str, bus_token: str, turn_id: str):
    """The only way an agent reaches another agent: the router's bus, bound to the current turn."""
    headers = {"Authorization": f"Bearer {bus_token}"}

    @tool("ask_agent",
          "Спросить другого агента Retinue через Роутер и дождаться ответа. Роутер решает, разрешено ли; "
          "отказ приходит с причиной. agent — id агента, text — самодостаточный вопрос: агент не видит этот "
          "разговор. Список агентов — инструмент list_agents.",
          {"agent": str, "text": str})
    async def ask_agent(args):
        async with httpx.AsyncClient(timeout=900) as http:
            response = await http.post(f"{bus_url}/call", headers=headers,
                                       json={"turn": turn_id, "agent": args["agent"], "text": args["text"]})
        data = response.json()
        return {"content": [{"type": "text", "text": data["text"]}], "is_error": not data["ok"]}

    @tool("list_agents", "Агенты Retinue, к которым тебе можно обращаться: id, имя, описание.", {})
    async def list_agents(args):
        async with httpx.AsyncClient(timeout=30) as http:
            agents = (await http.get(f"{bus_url}/agents", headers=headers)).json()
        return {"content": [{"type": "text", "text": json.dumps(agents, ensure_ascii=False)}]}

    return create_sdk_mcp_server("retinue", tools=[ask_agent, list_agents])


class ClaudeEngine:
    """Runs one turn of a Claude Code agent inside its workspace.

    Permissions are deny-by-default (`dontAsk`): only tools listed in `allowed_tools`
    run. The workspace's CLAUDE.md is the agent's instructions (setting source "project").
    """

    def __init__(self, cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "") -> None:
        self.cfg = cfg
        self.workspace = workspace
        self.bus_url = bus_url
        self.bus_token = bus_token

    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
        try:
            return await self._run(prompt, session_id, on_text, turn_id)
        except ResultError as exc:
            # The session transcript is gone (e.g. lost config dir): start over instead of failing every turn.
            if not session_id or "No conversation found" not in str(exc):
                raise
            log.warning("session %s not found, starting a new one", session_id)
            result = await self._run(prompt, None, on_text, turn_id)
            result.text = SESSION_LOST + result.text
            return result

    async def _run(self, prompt: str, session_id: str | None, on_text: OnText | None,
                   turn_id: str | None) -> EngineResult:
        # session_id comes only from our own state DB, never from a message (CVE-2026-96620).
        allowed, servers = list(self.cfg.allowed_tools), {}
        if self.bus_url and self.bus_token and turn_id:
            servers["retinue"] = bus_server(self.bus_url, self.bus_token, turn_id)
            allowed += [BUS_TOOL, "mcp__retinue__list_agents"]
        options = ClaudeAgentOptions(
            cwd=self.workspace,
            allowed_tools=allowed,
            mcp_servers=servers,
            disallowed_tools=self.cfg.disallowed_tools,
            permission_mode="dontAsk",
            setting_sources=["project"],
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
            model=self.cfg.model,
            resume=session_id,
            include_partial_messages=on_text is not None,
        )
        result: ResultMessage | None = None
        draft = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
            elif isinstance(message, StreamEvent) and on_text and message.parent_tool_use_id is None:
                event = message.event
                if event.get("type") == "message_start":
                    draft = ""  # a new assistant message (e.g. after a tool call) starts a new draft
                elif event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                    draft += event["delta"]["text"]
                    await on_text(draft)
        if result is None:
            return EngineResult(text="Агент не вернул результат.", session_id=session_id, is_error=True)
        text = result.result or ("; ".join(result.errors or []) or "Пустой ответ.")
        return EngineResult(
            text=text,
            session_id=result.session_id,
            is_error=result.is_error,
            num_turns=result.num_turns,
            cost_usd=result.total_cost_usd,
            duration_ms=result.duration_ms,
        )


class EchoEngine:
    """No-model engine for smoke tests: answers with the prompt."""

    async def run(self, prompt: str, session_id: str | None, on_text: OnText | None = None,
                  turn_id: str | None = None) -> EngineResult:
        if on_text:
            await on_text("echo: ")
        return EngineResult(text=f"echo: {prompt}", session_id=session_id or "echo-session", is_error=False)


def make_engine(cfg: EngineConfig, workspace: str, bus_url: str = "", bus_token: str = "") -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace, bus_url, bus_token)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
