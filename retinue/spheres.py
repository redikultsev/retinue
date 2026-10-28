"""Spheres: the owner's sessions grouped by the sphere of his life they serve — a Space of his knowledge base (travel,
work, home…), configured in `platform.yaml` (`config.Sphere`) — with one **main session** per sphere that he
talks to by the sphere's name, and **forks**: a new session that starts from a copy of another's conversation.

The platform owns all of it; the router only maps `/<sphere> <text>` to `/owner/sphere` and puts the spheres in the
bot's menu (`bridge.PlatformBridge.spheres`). A main session is a mark on a session row, at most one per sphere, held
by SQLite (`sessions.PlatformStore.claim_main`, the `sessions_main` index). A fork is an ordinary session of the
parent's track, lane and sphere, with the track's full rights (unlike a side question, which only reads): its first
run resumes the parent's CLI session with `--fork-session` (`cliengine.FORK_SESSION`), which copies the conversation
and leaves the parent's own as it was (pinned by `tests/test_probes.py`).

A fork is refused while its parent works or waits on a card, at the owner's request and again when its run starts:
the parent's transcript then ends in the middle of a turn, and a fork's resume of a parent with a deferred call runs
that call itself — under the parent's tool_use id, in another session — and the parent's own resume runs it again
(probed with 2.1.304).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import transcript
from .config import Sphere

if TYPE_CHECKING:
    from .platformd import Platform

STATE_WORDS = {"active": "работает", "waiting": "ждёт тебя", "idle": "готова", "closed": "закрыта"}
LINE_CHARS = 300  # what `/<sphere>` alone shows of the main session's last summary
BUSY = {"active": "сессия сейчас работает — ответви, когда она закончит ход",
        "waiting": "сессия ждёт твоего решения по карточке — ответви после него"}


def label(sphere: Sphere) -> str:
    return f"{sphere.icon} {sphere.title}" if sphere.icon else sphere.title


def first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…"


DEFERRED = "разговор сессии кончается вызовом, который ещё не выполнен — ответвить можно после её следующего хода"
MOVED = "трек сессии теперь на другой линии или в другой папке — её разговора там нет"


class Spheres:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.by_name = {sphere.name: sphere for sphere in platform.cfg.spheres}

    def listed(self) -> list[dict]:
        """The spheres as the face and the router read them, in the config's order."""
        return [{"name": s.name, "title": s.title, "icon": s.icon, "track": s.track} for s in self.platform.cfg.spheres]

    async def command(self, input_id: str, name: str, text: str) -> dict:
        """`/<sphere> [text]`: with text, into the sphere's main session — a new one, made main, when the sphere has
        none; alone, the main session's state and the first line of its last summary. `session` names the session a
        reply to the answer goes to."""
        sphere = self.by_name.get(name)
        if sphere is None:
            return {"ok": False, "say": f"Нет такой сферы: {name}."}
        store = self.platform.store
        main = store.main_of(name)
        if not text.strip():
            return self.status(sphere, main)
        if (known := store.input_session(input_id)) is not None:  # the router sent it again
            return {"ok": True, "session": known, "say": "Это сообщение сессия уже получила."}
        if main is not None:
            said = await self.platform.write(input_id, main, text)
            return {"ok": True, "session": main, "say": f"{label(sphere)} · сессия {main}: {said}"}
        # Main from its INSERT, before its run is started: a message meanwhile already writes into it.
        answer = await self.platform.new_session(input_id, sphere.track, text, sphere=name, main=True)
        if not answer["ok"]:
            return answer
        session = answer["session"]
        made = store.session(session)["main"] == 1  # lost only to a main session made meanwhile: an ordinary one
        head = f"{label(sphere)} · новая {'главная ' if made else ''}сессия {session}"
        return {"ok": True, "session": session,
                "say": f"{head}. Чтобы писать в неё, отвечай на её сообщения или пиши /{name} …"}

    def status(self, sphere: Sphere, main: str | None) -> dict:
        if main is None:
            return {"ok": True, "say": f"{label(sphere)}: главной сессии нет. Напиши /{sphere.name} <задача> — "
                                       "она начнётся и станет главной."}
        store = self.platform.store
        row = store.face_session(main) or {}
        waits = f" · ждут карточки: {row['waiting']}" if row.get("waiting") else ""
        head = f"{label(sphere)} · сессия {main} · {STATE_WORDS.get(row.get('state'), row.get('state'))}{waits}"
        said = store.last_summary(main)
        line = first_line(str(said.get("text") or "")) if said else ""
        return {"ok": True, "session": main, "say": f"{head}\n{line}" if line else f"{head}\nИтогов пока нет."}

    async def fork(self, input_id: str, parent_id: str, text: str) -> dict:
        """«Ответвить»: a new session from a copy of `parent_id`'s conversation, started with `text`; the same
        `input_id` again names the same fork."""
        store = self.platform.store
        if (known := store.input_session(input_id)) is not None:
            return {"ok": True, "session": known, "say": "Это сообщение сессия уже получила."}
        parent = store.session(parent_id)
        if (why := self.fork_refusal(parent)) is not None:
            return {"ok": False, "say": f"Не ответвил: {why}."}
        return await self.platform.new_session(input_id, parent["track"], text, sphere=parent["sphere"],
                                               fork_of=parent["id"])

    def fork_start(self, session: dict) -> tuple[str | None, str | None]:
        """A fork's run before its first init line: the parent's CLI session to copy, or why it cannot start now.
        (None, None) for any other run."""
        if session["cli_session"] or not session.get("fork_of"):
            return None, None
        parent = self.platform.store.session(session["fork_of"])
        if (why := self.fork_refusal(parent)) is not None:
            return None, f"Ответвление не запущено: {why}. Напиши в эту сессию ещё раз — и оно запустится."
        return parent["cli_session"], None

    def fork_refusal(self, parent: dict | None) -> str | None:
        """Why a session cannot be forked now, in the owner's words; None when it can. `parent`: its store row.
        Whatever its state says, a parent whose conversation ends in a deferred call — a card expired or decided, a
        resume refused or lost — or that has a run queued is refused: the fork's resume would run that call itself,
        under the parent's tool_use id (probed, `tests/test_probes.py`)."""
        if parent is None:
            return "нет такой сессии"
        if parent.get("parent"):
            return "вопрос в сторону не ответвляется — ответви сессию, к которой он задан"
        if not parent["cli_session"]:
            return "сессия ещё не сделала ни одного хода — ответвляться не от чего"
        if why := BUSY.get(parent["state"]):
            return why
        store = self.platform.store
        if any(q["session"] == parent["id"] for q in store.queued()):
            return BUSY["active"]
        last = store.last_line(parent["id"])
        if last is not None and transcript.deferred_of(last[0]):
            return DEFERRED
        track = self.platform.tracks.get(parent["track"])
        if track is None or track.lane != parent["lane"] or (
                parent.get("cwd") is not None and self.platform.cwd_for(track, parent.get("sphere")) != parent["cwd"]):
            return MOVED
        return None
