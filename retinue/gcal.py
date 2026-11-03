"""The plan's blocks in the owner's Google calendar «Retinue», both ways (docs/calendar.md). The calendar writer
(`calwrite`) holds the key and writes; the Router decides, keeping in its kv what it last wrote of each block (`KEY`).

Every SYNC_S, beside the digest's look, in a task of its own (a slow Google never holds the digest or a press):
1. **What changed in Google** — the writer's events since the Router's token, each known by its id (the block's own).
   One whose etag the Router wrote itself is its echo. One the owner moved or resized is his: the block takes its
   time and is locked, as a drag on the grid would be (`code:calsync`). One he deleted takes its block off the plan
   (`skipped`, «removed in Google»). Either is a sign that he was here (`planner.sign`).
2. **What changed in Retinue** — the planned and passed blocks against what was written: a block of the last day or
   the next SPAN_DAYS without an event gets one; a moved, resized, renamed or (un)locked one its event changed; a block
   skipped or gone its event taken off. A change refused because the owner changed the event since (`conflict`) is
   his edit and goes the way of step 1.
What it writes is kept at once, write by write: a look cut short never takes the Router's own write for his. A write
of the blocks refused (a block moved meanwhile) keeps nothing of the look: the next one reads the same changes again,
so his move in Google is never written over. Nothing missing from a read is taken for deleted: a full read leaves the
deleted out, so a block it lacks is asked about by its own write — the writer answers `gone` or `conflict`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from .core import Button
from .courier import line
from .mail import Unavailable
from .outbox import Neighbour

log = logging.getLogger(__name__)

KEY = "gcal.sync"       # the Router's kv: {calendar, token, events: {block: {etag, start, minutes, title, busy}},
                        # dropped: {block: start} — events taken off whose block may come back, gone: {block: start}
                        # — passed blocks he deleted in Google, told: {dead?, gone?}}
RULE = "code:calsync"   # the Router's writer of his moves in Google onto the blocks
BLOCKS, TASKS = "blocks", "tasks"
SYNC_S = 60
SPAN_DAYS = 30          # a block this far ahead gets its event
SEEN_DAYS = 400         # and one that has an event is followed this far — his move a month on is not a deletion
BACK_S = 86400          # blocks begun this long ago are followed; older ones are left in Google as they are
PUTS = 40               # events written in one look at most: a first sync of a full month goes over a few looks
BUDGET_S = 50           # and no longer than this: the next look goes on
PAGES = 20              # pages of 200 read at most: a read cut short takes nothing off Google
MINUTES_MAX = 24 * 60
CALL_S = 30
STOP = ("invalid_grant", "calendar deleted", "its state is unreadable", "no calendar key")


class Writer(Neighbour):
    async def put(self, ask: dict) -> dict:
        return await self._call("POST", "/put", ask, CALL_S)

    async def drop(self, block: str) -> dict:
        return await self._call("POST", "/drop", {"block": block}, CALL_S)

    async def changes(self, token: str, calendar: str = "") -> dict:
        return await self._call("POST", "/changes", {"token": token, "calendar": calendar}, CALL_S)

    async def again(self) -> dict:
        return await self._call("POST", "/calendar", {}, CALL_S)


class Gcal:
    def __init__(self, core, planner, writer: Writer, zone: str, link: str = "") -> None:
        self.core, self.planner, self.writer, self.zone, self.link = core, planner, writer, zone, link
        self._at = 0.0
        self._task: asyncio.Task | None = None
        self.lock = asyncio.Lock()  # a sync and «Make it again» never interleave

    # --- the Router's kv ------------------------------------------------------------------------------------------

    def _state(self) -> dict:
        try:
            data = json.loads(self.core.store.get(KEY) or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}

        def part(name):
            return data.get(name) if isinstance(data.get(name), dict) else {}
        return {"calendar": str(data.get("calendar") or ""), "token": str(data.get("token") or ""),
                "events": part("events"), "dropped": part("dropped"), "gone": part("gone"), "told": part("told")}

    def _keep(self, state: dict) -> None:
        self.core.store.set(KEY, json.dumps(state, sort_keys=True))

    def _calendar(self, state: dict, calendar: str) -> None:
        """Another calendar than the one known (the writer's state lost, the calendar made again): nothing known of
        the old one holds — every block is written into this one, and nothing is taken for deleted."""
        if not calendar or calendar == state["calendar"]:
            return
        if state["calendar"]:
            log.warning("the calendar «Retinue» is another one now: every block is written again")
            state.update(token="", events={}, dropped={}, gone={})
        state["calendar"] = calendar
        state["told"].pop("gone", None)

    async def _tell(self, state: dict, what: str, text: str, buttons=()) -> None:
        if state["told"].get(what):
            return
        state["told"][what] = True
        self._keep(state)
        await self.core.tell_owner(text, buttons=buttons)

    async def again_pressed(self, _value: str = "") -> str:
        """«Make it again» under the line that the calendar was deleted: a new one, every block written into it."""
        async with self.lock:
            answer = await self.writer.again()
            if answer.get("error"):
                return "The calendar «Retinue» is there: nothing to make."
            self._keep({"calendar": "", "token": "", "events": {}, "dropped": {}, "gone": {}, "told": {}})
        self._at = 0.0
        return "The calendar «Retinue» is made again: the plan goes there within a minute."

    # --- the look -------------------------------------------------------------------------------------------------

    def cycle(self, now: float) -> asyncio.Task | None:
        """Started from the digest's look, run apart from it: one sync at a time."""
        if now - self._at < SYNC_S or self.core.platform is None or (self._task and not self._task.done()):
            return None
        self._at = now
        self._task = asyncio.create_task(self.run(now))
        return self._task

    async def run(self, now: float) -> None:
        try:
            async with self.lock:
                await self._sync(now)
        except Unavailable as exc:
            log.warning("the calendar was not synced: %s", exc)
        except Exception:
            log.exception("the calendar was not synced")

    async def _stopped(self, state: dict, answer: dict) -> str:
        """The writer's refusal: '' none; 'stop' — nothing more this look; 'skip' — this block only."""
        error = str(answer.get("error") or "")
        if not error:
            return ""
        if error == "invalid_grant":
            await self._tell(state, "dead", "Google no longer takes the key of your calendar «Retinue»: the plan does "
                             "not reach Google. Sign in again on your Mac: deploy/mail/login.py --kind calwrite.")
        elif error == "calendar deleted":
            await self._gone(state)
        else:
            log.warning("the calendar writer refused: %s", line(error, 200))
        return "stop" if error in STOP else "skip"

    async def _gone(self, state: dict) -> None:
        await self._tell(state, "gone", "The calendar «Retinue» was deleted in Google: the plan no longer goes there.",
                         [Button("Make it again", "gcal-again")])

    async def _sync(self, now: float) -> None:
        state = self._state()
        answer = await self.writer.changes(state["token"], state["calendar"])
        if await self._stopped(state, answer):
            return
        if answer.get("gone"):
            await self._gone(state)
            return
        if not answer.get("calendar") and state["calendar"]:
            # The writer has no calendar while the Router knows one, and could not take it back: every block anew.
            log.warning("the calendar writer has no calendar: every block is written again")
            state.update(calendar="", token="", events={}, dropped={}, gone={})
        self._calendar(state, str(answer.get("calendar") or ""))
        state["told"].pop("dead", None)
        known: dict[str, dict] = state["events"]
        blocks = await self._blocks(now)
        if blocks is None:
            return
        rows, complete = blocks
        changed = [e for e in answer.get("events") or [] if isinstance(e, dict) and e.get("block")]
        moved: dict[str, tuple[str, int]] = {}
        removed: set[str] = set()
        for event in changed:
            block, was = event["block"], known.get(event["block"])
            if was is None or event.get("etag") == was.get("etag"):
                continue  # not one it wrote (its next write asks), or its own write coming back
            row = rows.get(block)
            if event.get("status") == "cancelled":
                if row is not None:
                    removed.add(block)
            elif row is not None and event.get("start") and (event["start"], event.get("minutes")) != (
                    row["fields"].get("start"), row["fields"].get("minutes")):
                # Its time is not the block's: his. One the same as the block's is the Router's own write whose
                # answer was lost — nothing to carry.
                moved[block] = (event["start"], min(int(event["minutes"]), MINUTES_MAX))
        ops = self._carry(rows, moved, removed)
        if ops:
            done = await self._apply(ops)
            if done is None:
                return  # nothing kept: the next look reads the same changes, and the owner's move is not undone
            self._versions(rows, done)
            self.planner.sign(now)
        for event in changed:
            block = event["block"]
            if event.get("status") == "cancelled":
                known.pop(block, None)
                if block in removed:
                    row = rows.pop(block)
                    # A planned block may come back (Undo): its event comes back with it. A passed one stays gone.
                    keep = state["gone"] if row["fields"].get("state") == "passed" else state["dropped"]
                    keep[block] = self._begins(row)
            elif block in known and event.get("start"):
                known[block].update(etag=event.get("etag"), start=event["start"], minutes=event.get("minutes"))
        if answer.get("full"):
            # A full read lists what is there and leaves the deleted out: what it lacks is asked by its next write.
            seen = {e["block"] for e in changed}
            for block in [b for b in known if b not in seen]:
                known[block]["etag"] = ""
        state["token"] = str(answer.get("token") or "")
        self._keep(state)
        later = await self._push(state, rows, complete, now)
        if later:
            if await self._apply(later) is not None:
                self.planner.sign(now)
            else:  # not carried: its etag forgotten, so the next write meets his edit again instead of writing over it
                for op in later:
                    if op["id"] in known:
                        known[op["id"]]["etag"] = ""
                self._keep(state)
        if ops or later:
            await self.planner.push(now)

    @staticmethod
    def _begins(row: dict) -> float:
        from .planner import moment
        return moment((row.get("fields") or {}).get("start")) or 0.0

    async def _read(self, collection: str, filter: list[dict], fields: list[str] | None = None
                    ) -> tuple[list[dict], bool] | None:
        """Up to PAGES pages, and whether that was all of it. None: unread."""
        rows, after = [], None
        for page in range(PAGES):
            arguments = {"collection": collection, "filter": filter, **({"fields": fields} if fields else {}),
                         **({"after": after} if after else {})}
            data = await self.planner._call("query", arguments, f"q/{collection}/gcal/{page}/{time.time_ns()}")
            answer = data.get("answer") if data.get("ok") else None
            if not isinstance(answer, dict):
                log.warning("%s not read: %s", collection, line(str(data.get("text") or "no answer"), 300))
                return None
            rows += [r for r in answer.get("rows") or [] if isinstance(r, dict)]
            if not (after := answer.get("next")):
                return rows, True
        return rows, False

    async def _blocks(self, now: float) -> tuple[dict[str, dict], bool] | None:
        from .planner import iso
        read = await self._read(BLOCKS, [{"field": "state", "op": "in", "value": ["planned", "passed"]},
                                         {"field": "start", "op": "between",
                                          "value": [iso(now - BACK_S), iso(now + SEEN_DAYS * 86400)]}])
        # The open tasks' titles: a closed task's block keeps the title it was written with (`_push`).
        tasks = await self._read(TASKS, [{"field": "state", "op": "eq", "value": "open"}], ["title"])
        if read is None or tasks is None:
            return None
        titles = {t["id"]: str(t["fields"].get("title") or "") for t in tasks[0]}
        return {r["id"]: {**r, "title": titles.get(r["fields"].get("task"), "")} for r in read[0]}, read[1]

    @staticmethod
    def _carry(rows: dict[str, dict], moved: dict[str, tuple[str, int]], removed: set[str]) -> list[dict]:
        """His moves and deletions in Google as writes of the blocks, each at the version just read; a block already
        as he left it needs none."""
        ops = []
        for block, (start, minutes) in moved.items():
            row = rows[block]
            f = row["fields"]
            if (f.get("start"), f.get("minutes"), f.get("locked")) != (start, minutes, True):
                ops.append({"op": "set", "id": block, "version": row["version"],
                            "fields": {"start": start, "minutes": minutes, "locked": True}})
                f.update(start=start, minutes=minutes, locked=True)
        for block in removed:
            row = rows[block]
            if row["fields"].get("state") == "planned":
                ops.append({"op": "set", "id": block, "version": row["version"],
                            "fields": {"state": "skipped", "why": "removed in Google"}})
        return ops

    @staticmethod
    def _versions(rows: dict[str, dict], done: dict) -> None:
        for record in done.get("records") or []:
            if isinstance(record, dict) and record.get("id") in rows and isinstance(record.get("version"), int):
                rows[record["id"]]["version"] = record["version"]

    async def _apply(self, ops: list[dict]) -> dict | None:
        """The writes as `code:calsync`, all or none; the idem is the writes' own, so a retry of the same is the
        same call and different writes are never refused as a repeat."""
        idem = "gcal:" + hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest()[:40]
        body = {"actor": RULE, "via": "code", "foreign": [], "call": idem,
                "arguments": {"collection": BLOCKS, "ops": ops, "idem": idem}}
        data = await self.core.platform("collections/apply", body, CALL_S)
        if not data.get("ok"):
            log.warning("his calendar's changes not written to the blocks: %s",
                        line(str(data.get("text") or "no answer"), 300))
            return None
        return data.get("answer") or {}

    async def _push(self, state: dict, rows: dict[str, dict], complete: bool, now: float) -> list[dict]:
        """Each block's event as the block is now, each write kept as it is made; returns the writes of the blocks
        his own edits in Google, met on the way, ask for."""
        from .planner import moment
        known: dict[str, dict] = state["events"]
        dropped, gone = state["dropped"], state["gone"]
        later: list[dict] = []
        puts, until = 0, time.monotonic() + BUDGET_S
        for block, row in sorted(rows.items(), key=lambda kv: str(kv[1]["fields"].get("start"))):
            f = row["fields"]
            was = known.get(block)
            minutes = f.get("minutes")
            if was is None and ((moment(f.get("start")) or 0.0) > now + SPAN_DAYS * 86400 or block in gone):
                continue
            if not isinstance(minutes, int) or not 0 < minutes <= MINUTES_MAX:
                continue  # Google takes no event a day long or longer
            want = {"start": f.get("start"), "minutes": minutes,
                    "title": row["title"] or (was or {}).get("title") or "Task", "busy": bool(f.get("locked"))}
            if was and was.get("etag") and all(was.get(k) == v for k, v in want.items()):
                continue
            if puts >= PUTS or time.monotonic() > until:
                break
            puts += 1
            answer = await self.writer.put({"block": block, **want, "zone": self.zone, "link": self.link,
                                            "etag": (was or {}).get("etag") or "", "revive": block in dropped,
                                            "calendar": state["calendar"]})
            stop = await self._stopped(state, answer)
            if stop == "stop":
                break
            if stop:
                continue
            self._calendar(state, str(answer.get("calendar") or ""))
            if answer.get("event"):
                event = answer["event"]
                known[block] = {**want, "etag": event.get("etag"), "start": event.get("start") or want["start"],
                                "minutes": event.get("minutes") or want["minutes"]}
                dropped.pop(block, None)
            elif answer.get("gone"):
                known.pop(block, None)
                if f.get("state") == "planned":
                    dropped[block] = moment(f.get("start")) or now
                    later.append({"op": "set", "id": block, "version": row["version"],
                                  "fields": {"state": "skipped", "why": "removed in Google"}})
                else:
                    gone[block] = moment(f.get("start")) or now
            elif theirs := answer.get("conflict"):
                # He changed it in Google since: his time stands. Known as his, with no etag until the block is his
                # time too — the next write asks again rather than write over him.
                his = theirs.get("start") and (theirs["start"], theirs.get("minutes")) != (want["start"], minutes)
                known[block] = {"etag": "" if his else theirs.get("etag"), "start": theirs.get("start"),
                                "minutes": theirs.get("minutes"), "title": want["title"], "busy": None}
                if his:
                    later.append({"op": "set", "id": block, "version": row["version"],
                                  "fields": {"start": theirs["start"],
                                             "minutes": min(int(theirs["minutes"]), MINUTES_MAX), "locked": True}})
            self._keep(state)
        if not complete:
            return later  # a read cut short: what it lacks may be there
        for block in [b for b in known if b not in rows]:
            begun = moment(known[block].get("start")) or 0.0
            if begun >= now - BACK_S:
                if time.monotonic() > until:
                    break
                stop = await self._stopped(state, await self.writer.drop(block))
                if stop == "stop":
                    break
                if stop:
                    continue
                dropped[block] = begun
            known.pop(block)  # an older one: out of the span, left in Google as it is
            self._keep(state)
        for kept in (dropped, gone):
            for block in [b for b, at in kept.items() if not isinstance(at, (int, float)) or at < now - BACK_S]:
                kept.pop(block)
        return later
