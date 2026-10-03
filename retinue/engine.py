"""The Engine seam: the only place that knows how an agent's model is run.

Today it is the Claude Agent SDK (unmodified `claude` underneath, your own login).
Another engine (Codex, an open-weight model) plugs in by implementing `Engine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import EngineConfig


@dataclass
class EngineResult:
    text: str
    session_id: str | None
    is_error: bool
    num_turns: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0


class Engine(Protocol):
    async def run(self, prompt: str, session_id: str | None) -> EngineResult: ...


class ClaudeEngine:
    """Runs one turn of a Claude Code agent inside its workspace.

    Permissions are deny-by-default (`dontAsk`): only tools listed in `allowed_tools`
    run. The workspace's CLAUDE.md is the agent's instructions (setting source "project").
    """

    def __init__(self, cfg: EngineConfig, workspace: str) -> None:
        self.cfg = cfg
        self.workspace = workspace

    async def run(self, prompt: str, session_id: str | None) -> EngineResult:
        # session_id comes only from our own state DB, never from a message (CVE-2026-96620).
        options = ClaudeAgentOptions(
            cwd=self.workspace,
            allowed_tools=self.cfg.allowed_tools,
            disallowed_tools=self.cfg.disallowed_tools,
            permission_mode="dontAsk",
            setting_sources=["project"],
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
            model=self.cfg.model,
            resume=session_id,
        )
        result: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
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

    async def run(self, prompt: str, session_id: str | None) -> EngineResult:
        return EngineResult(text=f"echo: {prompt}", session_id=session_id or "echo-session", is_error=False)


def make_engine(cfg: EngineConfig, workspace: str) -> Engine:
    if cfg.type == "echo":
        return EchoEngine()
    if cfg.type == "claude":
        return ClaudeEngine(cfg, workspace)
    raise SystemExit(f"unknown engine type {cfg.type!r}")
