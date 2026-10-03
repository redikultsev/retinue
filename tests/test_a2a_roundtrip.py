"""Router -> A2A -> agent host roundtrip with a fake engine (no model, no Matrix)."""

import asyncio
import socket

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette

from retinue.agent_host import EngineExecutor, SessionMap, build_card
from retinue.config import AgentConfig, EngineConfig, Skill
from retinue.engine import EngineResult
from retinue.router import ask_agent


class FakeEngine:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, session_id):
        self.calls.append((prompt, session_id))
        if prompt == "fail":
            return EngineResult(text="boom", session_id=None, is_error=True)
        return EngineResult(text=f"echo: {prompt}", session_id="sess-1", is_error=False, num_turns=1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run(tmp_path):
    port = free_port()
    cfg = AgentConfig(id="t", name="Test", description="d", trust_class="web",
                      skills=[Skill(id="chat", name="Chat", description="d")], engine=EngineConfig(),
                      public_url=f"http://127.0.0.1:{port}")
    engine = FakeEngine()
    card = build_card(cfg)
    handler = DefaultRequestHandler(agent_executor=EngineExecutor(engine, SessionMap(str(tmp_path / "s.db"))),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        assert await ask_agent(url, "hello", "ctx-1") == ("done", "echo: hello")
        assert await ask_agent(url, "again", "ctx-1") == ("done", "echo: again")
        assert engine.calls[1] == ("again", "sess-1"), "second turn must resume the stored session"
        status, answer = await ask_agent(url, "fail", "ctx-2")
        assert status == "failed" and answer == "boom"
    finally:
        server.should_exit = True
        await serve


def test_roundtrip(tmp_path):
    asyncio.run(_run(tmp_path))
