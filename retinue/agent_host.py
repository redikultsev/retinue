"""Agent host: exposes one agent over A2A. One host = one agent = one container (its trust boundary)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

import uvicorn
from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette

from .config import AgentConfig
from .engine import Engine, make_engine

log = logging.getLogger("retinue.agent")


class SessionMap:
    """A2A context id -> engine session id. The only source of `resume` values."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (context_id TEXT PRIMARY KEY, session_id TEXT NOT NULL)")
        self.db.commit()

    def get(self, context_id: str) -> str | None:
        row = self.db.execute("SELECT session_id FROM sessions WHERE context_id = ?", (context_id,)).fetchone()
        return row[0] if row else None

    def set(self, context_id: str, session_id: str) -> None:
        self.db.execute(
            "INSERT INTO sessions VALUES (?, ?) ON CONFLICT(context_id) DO UPDATE SET session_id = excluded.session_id",
            (context_id, session_id),
        )
        self.db.commit()


class EngineExecutor(AgentExecutor):
    def __init__(self, engine: Engine, sessions: SessionMap) -> None:
        self.engine = engine
        self.sessions = sessions
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        prompt = get_message_text(context.message) if context.message else ""
        if not prompt.strip():
            await updater.update_status(TaskState.TASK_STATE_REJECTED, message=new_text_message("Пустое сообщение."))
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING)
        # One turn at a time per conversation: the engine session is not concurrent-safe.
        async with self.locks[task.context_id]:
            result = await self.engine.run(prompt, self.sessions.get(task.context_id))
            if result.session_id:
                self.sessions.set(task.context_id, result.session_id)
        metadata = {"num_turns": result.num_turns, "cost_usd": result.cost_usd, "duration_ms": result.duration_ms}
        if result.is_error:
            await updater.update_status(TaskState.TASK_STATE_FAILED, message=new_text_message(result.text), metadata=metadata)
            return
        await updater.add_artifact(parts=[new_text_part(text=result.text, media_type="text/markdown")], name="answer")
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=metadata)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported yet")


def build_card(cfg: AgentConfig) -> AgentCard:
    return AgentCard(
        name=cfg.name,
        description=cfg.description,
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=cfg.public_url, protocol_version="1.0")],
        skills=[
            AgentSkill(id=s.id, name=s.name, description=s.description, tags=[cfg.trust_class], examples=s.examples)
            for s in cfg.skills
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Retinue agent host")
    parser.add_argument("--config", default="/agent/agent.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = AgentConfig.load(args.config)
    card = build_card(cfg)
    handler = DefaultRequestHandler(
        agent_executor=EngineExecutor(make_engine(cfg.engine, cfg.workspace), SessionMap(cfg.state_db)),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    log.info("agent %s (%s) on %s:%s", cfg.id, cfg.trust_class, cfg.listen_host, cfg.listen_port)
    uvicorn.run(Starlette(routes=routes), host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
