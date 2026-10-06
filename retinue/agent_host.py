"""Agent host: exposes one agent over A2A. One host = one agent = one container (its trust boundary)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
import time
from collections import defaultdict
from pathlib import Path

import uvicorn
from a2a.helpers import get_message_text, new_raw_part, new_task_from_user_message, new_text_message, new_text_part
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

TURN_KEY = "retinue/turn"  # set by the router; passed back when the agent uses the bus

OUTBOX = "out"  # files the agent writes here during a turn go to the owner as attachments
MAX_FILES = 10
MAX_FILE_BYTES = 15 * 1024 * 1024
PROGRESS_INTERVAL_S = 0.8  # how often a partial reply is pushed to the router


class Outbox:
    """Finds files created or changed in <workspace>/out during one engine turn."""

    def __init__(self, workspace: str) -> None:
        self.root = Path(workspace) / OUTBOX

    def snapshot(self) -> dict[Path, tuple[int, int]]:
        if not self.root.is_dir():
            return {}
        return {f: (f.stat().st_mtime_ns, f.stat().st_size) for f in self.root.rglob("*")
                if f.is_file() and not f.is_symlink()}

    def changed(self, before: dict[Path, tuple[int, int]]) -> tuple[list[Path], list[str]]:
        """Return (files to send, names skipped for size or count)."""
        after = self.snapshot()
        fresh = sorted(f for f, sig in after.items() if before.get(f) != sig)
        send, skipped = [], []
        for f in fresh:
            if len(send) < MAX_FILES and after[f][1] <= MAX_FILE_BYTES:
                send.append(f)
            else:
                skipped.append(f.name)
        return send, skipped


class EngineExecutor(AgentExecutor):
    def __init__(self, engine: Engine, outbox: Outbox | None = None) -> None:
        self.engine = engine
        self.outbox = outbox
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
        # One turn at a time per conversation; the order of the owner's messages is kept by the router.
        async with self.locks[task.context_id]:
            before = self.outbox.snapshot() if self.outbox else {}
            last = 0.0

            async def on_text(draft: str) -> None:
                # The partial reply travels as a WORKING status message; the final answer is the artifact.
                nonlocal last
                if time.monotonic() - last >= PROGRESS_INTERVAL_S:
                    last = time.monotonic()
                    await updater.update_status(TaskState.TASK_STATE_WORKING, message=new_text_message(draft))

            metadata = context.message.metadata if context.message else {}
            turn_id = str(metadata[TURN_KEY]) if TURN_KEY in metadata else None
            # A short run: no session is stored and none is resumed. The context id only groups A2A tasks.
            result = await self.engine.run(prompt, on_text, turn_id)
            files, skipped = self.outbox.changed(before) if self.outbox else ([], [])
        metadata = {"num_turns": result.num_turns, "cost_usd": result.cost_usd, "duration_ms": result.duration_ms}
        if result.is_error:
            await updater.update_status(TaskState.TASK_STATE_FAILED, message=new_text_message(result.text), metadata=metadata)
            return
        text = result.text
        if skipped:
            text += f"\n\nНе отправлены (больше {MAX_FILES} файлов или {MAX_FILE_BYTES // 2**20} МБ): {', '.join(skipped)}"
        await updater.add_artifact(parts=[new_text_part(text=text, media_type="text/markdown")], name="answer")
        for f in files:
            media_type = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            await updater.add_artifact(parts=[new_raw_part(f.read_bytes(), media_type=media_type, filename=f.name)],
                                       name=f.name)
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
        capabilities=AgentCapabilities(streaming=True),
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
        agent_executor=EngineExecutor(make_engine(cfg.engine, cfg.workspace, cfg.bus_url, cfg.bus_token),
                                      Outbox(cfg.workspace)),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    log.info("agent %s (%s) on %s:%s", cfg.id, cfg.trust_class, cfg.listen_host, cfg.listen_port)
    uvicorn.run(Starlette(routes=routes), host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
