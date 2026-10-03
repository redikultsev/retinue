"""Telegram adapter without the network: routing by topic, @agent, owner-only, rendering."""

import asyncio

from retinue.channels.telegram import TelegramChannel
from retinue.config import RouterAgent, TelegramConfig
from retinue.protocol import Store
from retinue.render import render_telegram

OWNER = 42


class FakeCore:
    def __init__(self):
        self.calls = []

    async def handle(self, channel, agent_id, text):
        self.calls.append((agent_id, text))


def make(tmp_path, topics):
    agents = [RouterAgent(id="travel", name="Путешествия", url="x"), RouterAgent(id="study", name="Учёба", url="y")]
    ch = TelegramChannel(TelegramConfig(owner_id=OWNER, bot_token="t"), agents, Store(str(tmp_path / "s.db")))
    ch.topics, ch.core, ch.sent = topics, FakeCore(), []

    async def call(method, files=None, **params):
        ch.sent.append((method, {k: v for k, v in params.items() if v is not None}))
        return {}

    ch.call = call
    ch.store.save_place("telegram", "travel", "11")
    return ch


def msg(text, sender=OWNER, thread=None):
    m = {"from": {"id": sender}, "chat": {"id": sender}, "text": text}
    if thread:
        m.update(message_thread_id=thread, is_topic_message=True)
    return m


def test_routing_with_topics(tmp_path):
    ch = make(tmp_path, topics=True)

    async def run():
        await ch.on_message(msg("билеты", thread=11))
        await ch.on_message(msg("/new", thread=11))
        await ch.on_message(msg("@учёба когда сессия?"))
        await ch.on_message(msg("чужой", sender=7, thread=11))
        await ch.on_message(msg("без адреса"))

    asyncio.run(run())
    assert ch.core.calls == [("travel", "билеты"), ("travel", "!new"), ("study", "когда сессия?")]
    assert ch.sent[-1][0] == "sendMessage" and "Кому?" in ch.sent[-1][1]["text"]


def test_one_stream_without_topics(tmp_path):
    ch = make(tmp_path, topics=False)

    async def run():
        await ch.on_message(msg("@travel Белград → Ереван"))
        await ch.on_message(msg("а на поезде?"))
        await ch.send("travel", "ok", [])

    asyncio.run(run())
    assert ch.core.calls == [("travel", "Белград → Ереван"), ("travel", "а на поезде?")]
    assert ch.sent[-1][1]["text"].startswith("<b>Путешествия</b>")
    assert "message_thread_id" not in ch.sent[-1][1]


def test_render_telegram_splits_and_escapes():
    html = render_telegram("## Итог\n\n| a | b |\n|---|---|\n| **x** | <y> |\n\n" + "длинный абзац. " * 600)
    assert html[0].startswith("<b>Итог</b>\n\n• <b>x</b> — &lt;y&gt;")
    assert len(html) > 1 and all(len(m) <= 4000 for m in html)
