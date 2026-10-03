"""Core with fake channels and a fake agent: one conversation per agent across channels, mirroring, commands."""

import asyncio

from retinue.config import RouterAgent
from retinue.core import AgentFile, Core
from retinue.protocol import Store


class FakeChannel:
    def __init__(self, name, is_record):
        self.name, self.is_record, self.typing_refresh_s = name, is_record, 0.01
        self.events = []

    async def start(self, core):
        self.core = core

    async def typing(self, agent_id, active):
        pass

    async def send(self, agent_id, text, files):
        self.events.append(("send", agent_id, text, [f.name for f in files]))

    async def mirror(self, agent_id, origin, text):
        self.events.append(("mirror", agent_id, origin, text))

    async def notice(self, agent_id, text):
        self.events.append(("notice", agent_id, text))

    async def protocol(self, line):
        self.events.append(("protocol", line))


async def _run(tmp_path):
    contexts = []

    async def fake_ask(url, text, context_id):
        contexts.append(context_id)
        return "done", f"echo: {text}", [AgentFile("a.txt", "text/plain", b"x")] if text == "file" else []

    store = Store(str(tmp_path / "r.sqlite"))
    core = Core([RouterAgent(id="travel", name="Путешествия", url="http://x")], store, "@owner:x", ask=fake_ask)
    matrix, telegram = FakeChannel("matrix", True), FakeChannel("telegram", False)
    await core.start([matrix, telegram])

    async def say(channel, text):
        await core.handle(channel, "travel", text)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                return

    await say(matrix, "из матрикса")
    await say(telegram, "file")
    assert contexts[0] == contexts[1], "both channels talk in the same conversation"
    assert ("send", "travel", "echo: из матрикса", []) in matrix.events
    assert not any(e[0] == "send" for e in telegram.events[:1]), "a Matrix turn is not copied to Telegram"
    assert ("mirror", "travel", "telegram", "file") in matrix.events, "Matrix records what was said in Telegram"
    assert ("send", "travel", "echo: file", ["a.txt"]) in telegram.events
    assert ("send", "travel", "echo: file", ["a.txt"]) in matrix.events

    await say(telegram, "!new")
    assert any(e[0] == "notice" and "Новый разговор" in e[2] for e in matrix.events)
    await say(matrix, "после сброса")
    assert contexts[2] != contexts[0], "!new starts a new conversation for every channel"

    await say(matrix, "!help")
    assert matrix.events[-1][0] == "notice" and "!new" in matrix.events[-1][2]
    rows = store.db.execute("SELECT channel, status FROM protocol ORDER BY id").fetchall()
    assert rows == [("matrix", "done"), ("telegram", "done"), ("telegram", "new"), ("matrix", "done")]


def test_core(tmp_path):
    asyncio.run(_run(tmp_path))


def test_upgrade_keeps_room_conversations(tmp_path):
    path = str(tmp_path / "old.sqlite")
    store = Store(path)
    store.db.execute("INSERT INTO rooms VALUES ('travel', '!r:x', 'room-old')")
    store.db.commit()
    assert Store(path).conversation("travel") == "room-old"
