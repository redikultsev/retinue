"""The app's chat: «Talk to Retinue» in the face is one more channel of the assistant, beside Telegram — the same
conversation, the same memory, her answers where he wrote (docs/face.md#talk-to-retinue).

The router never serves the app; the platform does, and the platform never calls the router. What the owner writes in
the app the platform journals as a note (`chat`), a button he presses there as `chat-press`; the bridge pulls them with
its other notes and hands them here first (`PlatformBridge.app`), so a card Telegram is slow to take holds back nothing
of the app's. What she and the system say goes up as rows (`POST /owner/chat`), each named by its archive id, so a row
pushed twice is one row, and a card rewritten by a press — here or in Telegram — is the same row again. What she is
writing goes up as she writes it (`POST /owner/chat/draft`), replaced whole each time and never kept: the host's
drafts are the whole text so far, and start again at each message of hers.

The app is a record channel: it gets what the owner wrote through the others (`mirror`) and every answer, so it reads
as the whole conversation. A push the platform did not take (it restarts) makes the channel stale, and the
conversation's last BACKFILL rows go up again as soon as it answers — at the router's start too.

Her files go up beside their row (`POST /owner/chat/file`), up to FILE_MAX each, and the app offers them as downloads;
a bigger one is named, not kept. The app takes no files from the owner yet: the platform refuses them with a line that
points to Telegram.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import TYPE_CHECKING

from ..archive import ASSISTANT, OWNER, SYSTEM, Event
from ..core import AgentFile, Core, Pressed, card_text
from ..mail import Unavailable
from .telegram import SLASH_COMMANDS

if TYPE_CHECKING:
    from ..bridge import PlatformClient

log = logging.getLogger("retinue.app")

BACKFILL = 200        # the conversation's last rows, pushed again when the app may have missed some
CHUNK = 50            # rows in one push
RETRY_S = 30.0        # a backfill the platform did not take is tried again after this
FILE_MAX = 20 * 2**20  # a file of hers kept for the app: the same bound as a session's handed-over file
FILE_TIMEOUT_S = 120
COMMANDS = {**SLASH_COMMANDS, "/help": "!help"}  # «/new» in the app as in Telegram
BY = {OWNER: "owner", ASSISTANT: "her", SYSTEM: "notice"}


class AppChannel:
    name = "app"
    is_record = True
    typing_refresh_s = 4.0

    def __init__(self, platform: PlatformClient) -> None:
        self.platform = platform
        self.core: Core | None = None
        self.stale = asyncio.Event()  # set: the app may lack rows — the conversation goes up again
        self.keeper: asyncio.Task | None = None

    async def start(self, core: Core) -> None:
        self.core = core
        self.stale.set()
        self.keeper = asyncio.create_task(self.keep())

    async def keep(self) -> None:
        while True:
            await self.stale.wait()
            self.stale.clear()
            try:
                await self.backfill(time.time())
            except Exception as exc:
                self.stale.set()
                log.warning("the app's chat not brought up to date: %s (again in %.0f s)", exc, RETRY_S)
                await asyncio.sleep(RETRY_S)

    async def backfill(self, now: float) -> int:
        """The conversation's last rows as the archive has them, live buttons and all: what the app missed while the
        platform was away. Files are not on record: the rows keep the ones they had."""
        core = self.core
        events = core.archive.recent(core.store.conversation(core.default_agent), BACKFILL)
        rows = [row for event in events if (row := self.row_of(event, now)) is not None]
        for start in range(0, len(rows), CHUNK):
            await self._push(rows[start:start + CHUNK])
        return len(rows)

    def row_of(self, event: Event, now: float) -> dict | None:
        """An archive event as the app's row; None for what the chat does not show (a press, someone else's
        letter)."""
        by = BY.get(event.kind)
        if by is None or (by == "owner" and event.text.startswith("[button] ")):
            return None
        row = {"id": event.id, "at": event.ts, "by": by, "text": event.text}
        if by == "owner" and event.channel != self.name:
            row["via"] = event.channel
        if by == "notice":
            row["buttons"] = [list(b) for b in self.core.store.card_buttons(event.id, now)[0]]
        return row

    # --- what the app sends -------------------------------------------------------------------------------------

    async def take(self, kind: str, data: dict) -> None:
        """A note of the app's (`chat`, `chat-press`), from the bridge."""
        if kind == "chat":
            await self.said(str(data["text"]), str(data["input_id"]))
        elif kind == "chat-press":
            await self.pressed(str(data["id"]), str(data["row"]))

    async def said(self, text: str, input_id: str) -> None:
        """His message in the app: into the conversation, then his row confirmed as the archive has it — a second
        delivery of the same note is the same message (`Core.handle`, by `input_id`)."""
        head, _, rest = text.strip().partition(" ")
        if head.lower() in COMMANDS:
            text = f"{COMMANDS[head.lower()]} {rest}".strip()
        await self.core.handle(self, None, text, native_id=input_id)
        event = self.core.archive.get(f"{self.name}:{input_id}")
        await self._push([{"id": f"{self.name}:{input_id}", "at": event.ts if event else time.time(), "by": "owner",
                           "text": event.text if event else text}])

    async def pressed(self, button_id: str, row: str) -> None:
        """His press in the app. One that applied rewrites its card (`rewrite`, as every channel's press does); one
        that did not — pressed already, expired — leaves the card without buttons, and says why."""
        pressed = await self.core.press(self, button_id)
        if not pressed.ok and not pressed.card and pressed.view is None:
            await self._push([{"id": row, "buttons": [], "toast": pressed.toast}])

    # --- what the core shows ------------------------------------------------------------------------------------

    async def typing(self, agent_id: str, active: bool) -> None:
        if active:  # a pause in the drafts, kept alive; her row ends it
            await self._draft({})

    async def draft(self, agent_id: str, text: str) -> None:
        await self._draft({"text": text})

    async def send(self, agent_id: str, text: str, files: list[AgentFile], ref: str | None = None) -> None:
        row = self._new(ref, "her", text)
        row["files"] = [await self._file(row["id"], f) for f in files]
        await self._push([row])

    async def mirror(self, agent_id: str, origin: str, text: str, ref: str | None = None) -> None:
        await self._push([{**self._new(ref, "owner", text), "via": origin}])

    async def notice(self, agent_id: str, text: str, buttons: list[tuple[str, str]] | None = None,
                     ref: str | None = None) -> None:
        await self._push([{**self._new(ref, "notice", text), "buttons": [list(b) for b in buttons or []]}])

    async def rewrite(self, card_id: str, pressed: Pressed) -> None:
        """A card pressed anywhere: its text after the press, the buttons still alive, what the press said."""
        row: dict = {"id": card_id, "buttons": [list(b) for b in pressed.keep], "toast": pressed.toast}
        if pressed.view is not None:
            row["text"] = card_text(pressed.view)
        elif pressed.card:
            row["text"] = pressed.card
        await self._push([row])

    async def protocol(self, line: str) -> None:
        pass  # the protocol lives in the Store

    async def trace(self, agent_id: str, tree_id: str, text: str) -> None:
        pass  # agents' conversation is not shown here

    # --- the platform -------------------------------------------------------------------------------------------

    def _new(self, ref: str | None, by: str, text: str) -> dict:
        event = self.core.archive.get(ref) if ref else None
        return {"id": ref or f"{self.name}-row:{uuid.uuid4()}", "at": event.ts if event else time.time(), "by": by,
                "text": text}

    async def _push(self, rows: list[dict]) -> None:
        """Rows up, or the channel stale: the platform away raises, so whoever needed the row knows it did not go."""
        try:
            answer = await self.platform.publish("chat", {"rows": rows})
        except Unavailable:
            self.stale.set()
            raise
        if answer.get("error"):
            log.warning("the platform refused the app's chat rows: %s", answer["error"])

    async def _draft(self, body: dict) -> None:
        try:
            await self.platform.publish("chat/draft", body)
        except Unavailable:
            pass  # a draft is gone the moment the next one comes: nothing to tell or to try again

    async def _file(self, row: str, f: AgentFile) -> dict:
        """One of her files kept for the app: its row there, or its name with why it is not kept."""
        said = {"id": None, "name": f.name, "size": len(f.data), "mime": f.media_type}
        if len(f.data) > FILE_MAX:
            return {**said, "why": f"over {FILE_MAX // 2**20} MB: not kept in the app"}
        body = {"row": row, "name": f.name, "mime": f.media_type,
                "data": await asyncio.to_thread(lambda: base64.b64encode(f.data).decode())}
        try:
            answer = await self.platform.owner("chat/file", body, FILE_TIMEOUT_S)
        except Unavailable as exc:
            return {**said, "why": f"not kept in the app ({exc})"}
        if not isinstance(answer.get("file"), dict):
            return {**said, "why": str(answer.get("error") or "not kept in the app")}
        return answer["file"]
