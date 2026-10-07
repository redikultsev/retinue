"""Router -> A2A -> agent host roundtrip with a fake engine (no model, no Matrix)."""

import asyncio
import socket

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette

from retinue.agent_host import EngineExecutor, Outbox, SessionMap, build_card
from retinue.config import AgentConfig, EngineConfig, Skill
from retinue.engine import EngineResult
from retinue.core import ask_agent


class FakeEngine:
    def __init__(self, workspace):
        self.calls = []
        self.turns = []
        self.sessions = []   # the session each call resumed
        self.workspace = workspace

    async def compact(self, session_id):
        self.calls.append("/compact")
        self.sessions.append(session_id)
        return EngineResult(text="сжато", is_error=False, session_id=session_id)

    async def run(self, prompt, session_id, on_text=None, turn_id=None):
        self.calls.append(prompt)
        self.turns.append(turn_id)
        self.sessions.append(session_id)
        if on_text and prompt == "hello":
            await on_text("ec")
        if prompt == "file":
            (self.workspace / "out").mkdir(exist_ok=True)
            (self.workspace / "out" / "plan.html").write_text("<h1>plan</h1>")
            return EngineResult(text="готово", is_error=False)
        if prompt == "fail":
            return EngineResult(text="boom", is_error=True, limit=True, limit_until=1760000000)
        if prompt == "long":
            return EngineResult(text="ответ после сжатия", is_error=False, session_id=session_id, num_turns=2,
                                cost_usd=0.5, compacted=True, usage={"input_tokens": 1200, "output_tokens": 300},
                                rate_limit={"status": "allowed_warning", "rate_limit_type": "five_hour",
                                            "utilization": 0.8, "resets_at": 1760003600})
        return EngineResult(text=f"echo: {prompt}", is_error=False, num_turns=1, session_id=session_id or f"s{len(self.calls)}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run(tmp_path):
    port = free_port()
    cfg = AgentConfig(id="t", name="Test", description="d", trust_class="web",
                      skills=[Skill(id="chat", name="Chat", description="d")], engine=EngineConfig(),
                      public_url=f"http://127.0.0.1:{port}")
    engine = FakeEngine(tmp_path)
    card = build_card(cfg)
    sessions = SessionMap(str(tmp_path / "state" / "agent.sqlite"))
    handler = DefaultRequestHandler(agent_executor=EngineExecutor(engine, sessions, Outbox(str(tmp_path))),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        progress = []

        async def on_progress(text):
            progress.append(text)

        hello = await ask_agent(url, "hello", "ctx-1", on_progress)
        assert hello == ("done", "echo: hello", []) and hello.meta["new_session"] is True, "nothing to resume"
        assert progress == ["ec"], "the partial reply streams before the answer"
        again = await ask_agent(url, "again", "ctx-1", None, "turn-7")
        assert again == ("done", "echo: again", []) and again.meta["new_session"] is False
        assert engine.turns[-1] == "turn-7", "the router's turn id reaches the engine"
        assert engine.calls == ["hello", "again"] and engine.sessions == [None, "s1"], "one context, one session"
        assert SessionMap(str(tmp_path / "state" / "agent.sqlite")).get("ctx-1") == "s1", "kept across restarts"
        assert await ask_agent(url, "/compact", "ctx-1", control="compact") == ("done", "сжато", [])
        assert engine.calls[-1] == "/compact" and engine.sessions[-1] == "s1", "compact is a control, not a message"
        assert await ask_agent(url, "утренняя сводка", "ctx-1", control="oneshot") == ("done", "echo: утренняя сводка", [])
        assert engine.sessions[-1] is None, "a background run does not resume the conversation"
        assert SessionMap(str(tmp_path / "state" / "agent.sqlite")).get("ctx-1") == "s1", "and is not kept"
        failed = await ask_agent(url, "fail", "ctx-2")
        assert failed == ("failed", "boom", []) and failed.meta["limit"] is True and failed.meta["limit_until"] == 1760000000
        long = await ask_agent(url, "long", "ctx-1")
        assert long == ("done", "ответ после сжатия", []), "the reply still unpacks into three"
        assert long.meta["compacted"] is True and long.meta["new_session"] is False and long.meta["num_turns"] == 2
        assert long.meta["usage"] == {"input_tokens": 1200, "output_tokens": 300} and long.meta["cost_usd"] == 0.5
        assert long.meta["rate_limit"]["utilization"] == 0.8 and long.meta["rate_limit"]["resets_at"] == 1760003600
        status, answer, files = await ask_agent(url, "file", "ctx-3")
        assert (status, answer) == ("done", "готово")
        assert [(f.name, f.media_type, f.data) for f in files] == [("plan.html", "text/html", b"<h1>plan</h1>")]
        _, _, files = await ask_agent(url, "hello", "ctx-3")
        assert files == [], "an unchanged file is not sent again"
    finally:
        server.should_exit = True
        await serve


def test_roundtrip(tmp_path):
    asyncio.run(_run(tmp_path))
