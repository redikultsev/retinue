"""The Router's mirror on the platform (docs/platform.md#the-routers-mirror): what the app shows of the Router's world
goes up as named snapshots, the owner's presses on it come back by kind, and the look keeps both in step. The platform
never calls the Router.

- **A snapshot** is one call of the platform's (`POST /owner/<call>`) and how its body is built (`Mirror.snapshot`):
  published whole, replacing the one there — when it changed since it last went, and at least every `every_s` anyway
  (0: every time it is asked). One at a time, each built when its turn comes, so the newest is the one that stays. A
  snapshot the platform refuses (400, or 413 for one too big) is logged and not pushed again until it changes or its
  time is up; what the platform kept but dropped part of (`dropped: [{id, why}]`) is logged item by item. The platform
  away is logged too, and the next publish tries again. A publish raises nothing: whoever asked for it goes on.
- **A press** in the app comes back as a note of its kind (`waiting-press`, `plan-press`), routed to the handler named
  for that kind (`Mirror.on`): in the notes' order, or beside them when it reads and writes the collections, so the
  cards behind it never wait. A press that fails is logged and the owner is told it did not apply; its note is done
  with either way.
- **The look** (`Look`): what the Router does every LOOK_S, step after step in the order it names them, each on its
  own — one that fails is logged and the next goes on. A poke (a mark by the owner's own hand) brings the next look
  forward, so the app shows it now and not in half a minute.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable

from .mail import Unavailable

log = logging.getLogger("retinue.mirror")

LOOK_S = 30.0          # the look comes this often…
TICK_S = 2.0           # …or, poked, within this
PUSH_S = 3600.0        # a snapshot that did not change goes again this often anyway
FAILED_PRESS = "The press did not apply: a server error. Try again."

Post = Callable[[str, dict], Awaitable[dict]]  # the platform's `/owner/<call>` with a short wait (`PlatformClient.publish`)
Press = Callable[[dict, float], Awaitable[str | None]]  # a press's data, now → what the owner is told, or None


class Snapshot:
    def __init__(self, post: Post, call: str, build: Callable[[float], dict | None], what: str, every_s: float) -> None:
        self.post, self.call, self.build, self.what, self.every_s = post, call, build, what, every_s
        self.mark, self.at = "", 0.0
        self.lock = asyncio.Lock()

    async def __call__(self, now: float) -> bool:
        """Published if it changed or its time is up: True when the platform answered, refusing or not."""
        async with self.lock:
            try:
                body = self.build(now)
                if body is None:  # nothing to show yet: not an empty one
                    return False
                mark = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if mark == self.mark and now - self.at < self.every_s:
                    return False
                answer = await self.post(self.call, body)
            except Unavailable as exc:
                log.warning("%s not pushed: %s", self.what, exc)
                return False
            except Exception:
                log.exception("%s not pushed", self.what)
                return False
            if answer.get("error"):
                log.warning("the platform refused %s: %s", self.what, answer["error"])
            for item in answer.get("dropped") or []:  # the rest went up: these did not, and the log says which and why
                if isinstance(item, dict):
                    log.warning("the platform dropped %s from %s: %s", item.get("id"), self.what, item.get("why"))
            self.mark, self.at = mark, now
            return True


class Mirror:
    def __init__(self, post: Post) -> None:
        self.post = post
        self._presses: dict[str, tuple[Press, bool]] = {}
        self._beside: set[asyncio.Task] = set()

    def snapshot(self, call: str, build: Callable[[float], dict | None], what: str,
                 every_s: float = PUSH_S) -> Snapshot:
        """A snapshot of `POST /owner/<call>`, built by `build(now)` (None: nothing to show yet); `what` names it in
        the log. Call it with the moment to publish."""
        return Snapshot(self.post, call, build, what, every_s)

    def on(self, kind: str, handler: Press, beside: bool = False) -> None:
        """The app's presses of this kind go to `handler`; `beside` the notes, when it may take a while."""
        self._presses[kind] = (handler, beside)

    def takes(self, kind: str) -> bool:
        return kind in self._presses

    async def press(self, kind: str, data: dict, now: float | None = None) -> str | None:
        """One press: what the owner is to be told, or None. One beside the notes is started and answers None."""
        handler, beside = self._presses[kind]
        now = time.time() if now is None else now
        if not beside:
            return await self._run(kind, handler, data, now)
        task = asyncio.create_task(self._run(kind, handler, data, now))
        self._beside.add(task)
        task.add_done_callback(self._beside.discard)
        return None

    @staticmethod
    async def _run(kind: str, handler: Press, data: dict, now: float) -> str | None:
        try:
            return await handler(data, now)
        except Exception:
            log.exception("a press %s was not applied", kind)
            return FAILED_PRESS


class Look:
    def __init__(self, steps: list[tuple[str, Callable[[float], object]]], every_s: float = LOOK_S) -> None:
        """`steps`: each its name for the log and what it does with the moment, in the order they run; one that
        starts work of its own returns it unawaited (a task), and the look goes on."""
        self.steps, self.every_s = steps, every_s
        self._at = 0.0

    def poke(self) -> None:
        """The next look at the next tick, not in LOOK_S."""
        self._at = 0.0

    async def once(self, now: float) -> bool:
        """The look, when it is due: True when it ran."""
        if now - self._at < self.every_s:
            return False
        self._at = now  # a poke while it runs is a look again after it
        for name, step in self.steps:
            try:
                done = step(now)
                if inspect.iscoroutine(done):
                    await done
            except Unavailable as exc:  # the platform restarting: the next look tries again
                log.warning("%s not done: %s", name, exc)
            except Exception:
                log.exception("%s failed", name)
        return True

    async def run(self) -> None:
        while True:
            await self.once(time.time())
            await asyncio.sleep(TICK_S)
