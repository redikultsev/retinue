"""The voice: Retinue's one face to the owner as the main session of the root project — the sphere whose sessions
start in the root of the Base (`voice:` in platform.yaml) — when the router's `voice.engine` is `session`
(`router.SessionVoice`, docs/proactive.md «The voice»).

The router's core composes each turn as for the assistant — the owner's message, a reminder, the morning summary —
and `/owner/voice` writes it into the main session (a new one made main when there is none, a closed one resumed),
holds the call until the turn is over and answers with what the turn said. Those summaries leave the pump's journal,
so Telegram gets the answer once, from the router. Its character is a file at the root the root's `AGENTS.md`
includes; its rights are the install's: its track's rules, and `voice.tools`, the only tools its sessions may call.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gate import Run
    from .platformd import Platform

SCHEDULE = "schedule"  # the router's own write — a reminder, the morning summary: no one's word
HOLD_S = 600           # how long a router's call waits for the turn's end
STEP_S = 0.25
NO_VOICE = "The platform has no voice: add a `voice:` section to platform.yaml (docs/proactive.md)."
WORKING = "Still working on it: the answer comes as a message of the voice's session."
WAITING = "Waiting for your decision on a card."
NOT_GRANTED = "Not run: {tool} is not among the voice's tools (voice.tools in platform.yaml)."
NOT_HEARD = "The voice does not listen in {channel}: write to it in Telegram."
NO_COMPACT = "The voice's session compacts itself."
NOT_FOR_SESSION = "Not for the voice's session: someone else's text or a fixed form of answer — code takes it."


class Voice:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        self.cfg = platform.cfg.voice
        self.sphere = platform.spheres.by_name[self.cfg.sphere]
        self.holding: dict[str, list[int]] = {}  # session -> its summaries while a router's call waits on it
        self.one = asyncio.Lock()                 # one router's call at a time

    def refusal(self, event: str, run: Run | None, body: dict) -> dict | None:
        """A call of a session of the voice's sphere to a tool `voice.tools` does not name: denied at the hook.
        None: the gate decides, as for any session."""
        if self.cfg.tools is None or event != "PreToolUse" or run is None:
            return None
        session = self.store.session(run.session)
        tool = str(body.get("tool_name") or "")
        if session is None or session.get("sphere") != self.sphere.name or granted(tool, self.cfg.tools):
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": NOT_GRANTED.format(tool=tool)}}

    def summarized(self, session: str, seq: int) -> None:
        """A summary the router's call waits for: its answer, taken out of the pump's journal at once."""
        if session in self.holding:
            self.store.db.execute("UPDATE notes SET given = 1 WHERE seq = ?", (seq,))
            self.store.db.commit()
            self.holding[session].append(seq)

    async def write(self, input_id: str, text: str, attachments: object, by: str | None) -> dict:
        """Into the root project's main session, made when there is none: the owner's message (`by` None) or the
        schedule's."""
        platform, store = self.platform, self.store
        if (known := store.input_session(input_id)) is not None:  # the router sent it again
            return {"ok": True, "session": known, "created": False, "again": True}
        main, created = store.main_of(self.sphere.name), False
        if main is None:
            answer = await platform.new_session(f"{input_id}/open", self.sphere.track, "", sphere=self.sphere.name,
                                                main=True)
            if not answer["ok"]:
                return answer
            main, created = store.main_of(self.sphere.name) or answer["session"], True
        said = await (platform.write(input_id, main, text, attachments) if by is None else
                      platform.write_into(input_id, store.session(main), text, by))
        if store.input_session(input_id) != main:  # not taken: its lane retired, its files refused
            return {"ok": False, "say": said}
        return {"ok": True, "session": main, "created": created}

    async def answer(self, input_id: str, text: str, attachments: object = None, by: str | None = None) -> dict:
        """`/owner/voice`: written, then held until the turn is over. `status`: done | failed, with the turn's
        summaries; working | waiting, in the platform's words."""
        async with self.one:
            written = await self.write(input_id, text, attachments, by)
            if not written["ok"]:
                return {"ok": False, "say": written.get("say") or "Not written."}
            session = written["session"]
            self.holding[session] = []
            try:
                deadline = asyncio.get_running_loop().time() + HOLD_S
                while self._busy(session) and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(STEP_S)
            finally:
                notes = [self._note(seq) for seq in self.holding.pop(session)]  # later ones are pings again
            words = [n["text"] for n in notes]
            if self._busy(session):
                status, words = "working", [*words, WORKING]
            elif (self.store.session(session) or {}).get("state") == "waiting":
                status, words = "waiting", [*words, WAITING]
            elif notes:
                status = "failed" if notes[-1]["kind"] == "failed" else "done"
            elif written.get("again") and (last := self.store.last_summary(session)):
                status, words = "done", [str(last.get("text") or "")]
            else:
                status, words = "failed", ["The turn ended without a word."]
            return {"ok": True, "session": session, "status": status, "created": written["created"],
                    "text": "\n\n".join(w for w in words if w.strip())}

    def _busy(self, session: str) -> bool:
        return any(r["session"] == session for r in self.store.open_runs()) or \
            any(q["session"] == session for q in self.store.queued())

    def _note(self, seq: int) -> dict:
        row = self.store.db.execute("SELECT data FROM notes WHERE seq = ?", (seq,)).fetchone()
        data = json.loads(row[0]) if row else {}
        return {"text": str(data.get("text") or ""), "kind": str(data.get("kind") or "")}


def granted(tool: str, tools: list[str]) -> bool:
    """`Read` names Read; `mcp__retinue` every tool of that server."""
    parts = tool.split("__")
    return tool in tools or (len(parts) >= 3 and parts[0] == "mcp" and f"mcp__{parts[1]}" in tools)
