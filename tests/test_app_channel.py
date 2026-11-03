"""The app's chat as a channel of the assistant (`channels/app.py`) against a fake platform: what the owner writes in the
app is the same conversation as Telegram's, her answer and her drafts go up as rows by their archive ids, a press there
or in Telegram rewrites the card's row, and a platform that was away gets the conversation again."""

import asyncio
import time

import pytest

from retinue.archive import Archive
from retinue.channels import app as app_module
from retinue.channels.app import AppChannel
from retinue.config import RouterAgent
from retinue.core import AgentFile, Button, Core
from retinue.mail import Unavailable
from retinue.protocol import Store
from test_core import FakeChannel

AGENT = RouterAgent(id="assistant", name="Assistant", url="http://assistant")


class FakePlatform:
    def __init__(self):
        self.pushed, self.drafts, self.files, self.down = [], [], [], False

    async def publish(self, call, body):
        if self.down:
            raise Unavailable("unavailable: the platform (ConnectError)")
        (self.drafts if call == "chat/draft" else self.pushed).append(body if call == "chat/draft" else body["rows"])
        return {"ok": True}

    async def owner(self, call, body, timeout=60):
        assert call == "chat/file"
        self.files.append(body)
        return {"file": {"id": "f" * 32, "name": body["name"], "size": 3, "mime": body["mime"]}}

    def rows(self) -> dict[str, dict]:
        """Every row as the platform would have it: merged by id."""
        out: dict[str, dict] = {}
        for push in self.pushed:
            for row in push:
                out[row["id"]] = {**out.get(row["id"], {}), **row}
        return out


def world(tmp_path, answer="Booked for 19:00.", files=()):
    async def ask(url, text, context_id, on_progress=None, turn_id=None, **kwargs):
        world.asked.append(text)
        await on_progress("Booking")
        await on_progress("Booking the table")
        return "done", answer, list(files)

    world.asked = []
    platform = FakePlatform()
    core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", ask=ask,
                archive=Archive(str(tmp_path / "archive.sqlite")))
    return core, AppChannel(platform), FakeChannel("telegram", False), platform


def test_his_message_in_the_app_is_the_conversation_and_her_answer_and_drafts_go_up_by_archive_id(tmp_path):
    async def run():
        core, app, telegram, platform = world(tmp_path)
        await core.start([telegram, app])
        await settle(app)
        await app.take("chat", {"text": "Book the usual table", "input_id": "face:1"})
        await settle(app)
        await app.take("chat", {"text": "Book the usual table", "input_id": "face:1"})  # the note pulled again
        await settle(app)
        return core, platform, telegram

    core, platform, telegram = asyncio.run(run())
    assert len(world.asked) == 1 and ", in the app]\nBook the usual table" in world.asked[0], "once, named as the app's"
    rows = platform.rows()
    assert rows["app:face:1"]["by"] == "owner" and rows["app:face:1"]["text"] == "Book the usual table"
    her = [r for r in rows.values() if r["by"] == "her"]
    assert [r["text"] for r in her] == ["Booked for 19:00."]
    reply = core.archive.recent(core.store.conversation("assistant"), 5)[-1]
    assert her[0]["id"] == reply.id and her[0]["at"] == reply.ts and her[0]["files"] == []
    assert {"text": "Booking the table"} in platform.drafts, "the whole draft, each time"
    assert not [e for e in telegram.events if e[0] == "send"], "Telegram did not write and is no record"


def test_telegram_s_words_and_her_cards_reach_the_app_and_a_press_rewrites_the_card_wherever_it_came(tmp_path):
    async def run():
        core, app, telegram, platform = world(tmp_path)
        await core.start([telegram, app])
        await settle(app)
        await core.handle(telegram, None, "Hello from the phone", native_id="7")
        await settle(app)
        card = await core.tell_owner("Pick one", buttons=[Button("Yes", "check"), Button("No", "check")])
        (_, yes), (_, no) = platform.rows()[card]["buttons"]
        await app.take("chat-press", {"id": yes, "row": card})
        pressed = dict(platform.rows()[card])
        await app.take("chat-press", {"id": no, "row": card})  # spent with the card
        spent = dict(platform.rows()[card])
        second = await core.tell_owner("Again?", buttons=[Button("Yes", "check")])
        await core.press(telegram, platform.rows()[second]["buttons"][0][1])  # pressed in Telegram
        return platform, card, pressed, spent, second

    platform, card, pressed, spent, second = asyncio.run(run())
    rows = platform.rows()
    assert rows["telegram:7"]["via"] == "telegram" and rows["telegram:7"]["by"] == "owner"
    assert pressed["text"] == "Pick one\n\n_Chosen: Yes_" and pressed["buttons"] == []
    assert pressed["toast"] == "The button reached the router."
    assert spent["toast"] == "The button was already pressed or has expired." and spent["text"] == pressed["text"]
    assert rows[second]["text"] == "Again?\n\n_Chosen: Yes_", "a press in Telegram rewrites the app's row too"


def test_a_platform_that_was_away_gets_the_conversation_again_without_presses_and_with_live_buttons(tmp_path):
    async def run():
        core, app, telegram, platform = world(tmp_path)
        platform.down = True
        await core.start([telegram, app])
        await core.handle(telegram, None, "Remind me at 9", native_id="1")
        await settle(app)
        card = await core.tell_owner("Still on?", buttons=[Button("Yes", "check"), Button("No", "check")])
        assert app.stale.is_set(), "a push that did not go: stale"
        await core.press(telegram, core.store.card_buttons(card, time.time())[0][1][1])
        platform.down = False
        await app.backfill(time.time())
        return platform, card

    platform, card = asyncio.run(run())
    rows = platform.rows()
    assert [r["by"] for r in rows.values()] == ["owner", "her", "notice"], "no «[button] …» row"
    assert rows["telegram:1"]["via"] == "telegram" and rows[card]["buttons"] == [], "the card's buttons as they are"


async def settle(app: AppChannel) -> None:
    """Until every task the core started is done — the app's keeper waits for the next stale, and is no such task."""
    for _ in range(500):
        await asyncio.sleep(0.01)
        if not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t is not app.keeper]:
            return


def test_her_files_go_up_beside_their_row_and_one_too_big_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FILE_MAX", 4)
    files = [AgentFile("plan.pdf", "application/pdf", b"%PD"), AgentFile("big.zip", "application/zip", b"12345")]

    async def run():
        core, app, telegram, platform = world(tmp_path, files=files)
        await core.start([telegram, app])
        await app.take("chat", {"text": "Send me the plan", "input_id": "face:9"})
        await settle(app)
        return platform

    platform = asyncio.run(run())
    [her] = [r for r in platform.rows().values() if r["by"] == "her"]
    assert [f["row"] for f in platform.files] == [her["id"]] and platform.files[0]["data"] == "JVBE"
    kept, named = her["files"]
    assert kept == {"id": "f" * 32, "name": "plan.pdf", "size": 3, "mime": "application/pdf"}
    assert named["id"] is None and named["name"] == "big.zip" and "not kept in the app" in named["why"]


@pytest.mark.parametrize("typed, command", [("/new", "New conversation"), ("/help", "Commands:")])
def test_a_slash_command_in_the_app_is_the_bot_s(tmp_path, typed, command):
    async def run():
        core, app, telegram, platform = world(tmp_path)
        await core.start([telegram, app])
        await app.take("chat", {"text": typed, "input_id": "face:3"})
        await settle(app)
        return platform

    rows = asyncio.run(run()).rows()
    assert world.asked == [] and any(r["by"] == "notice" and command in r["text"] for r in rows.values())
    assert rows["app:face:3"]["text"] == typed.replace("/", "!"), "his row, as the router took it"
