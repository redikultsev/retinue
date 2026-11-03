"""The Router's code on the owner's collections: each code rule reads and writes through the platform's
`/owner/collections/*` (docs/collections.md#from-the-assistant) by a handle bound to the rule and to one collection
(`Records`). The rights are the owner's to give (`collections rights …`): without them nothing here writes.

- **A read is every page**: up to PAGES pages of PAGE rows, and whether that was all of it (`Rows.complete`) — a read
  cut short takes nothing for deleted. None when it could not be read (no collection yet, no rights for the rule): said
  once in the log, then quiet until a read goes through again.
- **A write is all or none**: the ops in one apply, under the caller's idem, which is the call's own id too — so a
  retry of the same write is the same call, and two different writes never share one (`mark`). Its answer is the
  records at their new versions and the changes' ids; None when refused, logged, its words kept (`refused`).
- **Its changes**: what this writer did to this collection since a moment, and the revert of one.

The platform away is not a refusal: `Unavailable` reaches the caller, whose next look tries again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable

from .courier import line

log = logging.getLogger(__name__)

PLAN = "code:plan"          # the day plan's blocks (`planner`)
CALSYNC = "code:calsync"    # his moves in Google onto the blocks (`gcal`)
WAITING = "code:waiting"    # the tasks of the rows of «Waiting for you» (`tasks`)
QUIET = (PLAN, CALSYNC)     # the owner's own moves carried out: never in the evening list (`core.QUIET_RULES`)
PAGE = 200                  # rows a page, as the platform gives them
PAGES = 20                  # pages a read at most: 4000 rows — a month of blocks and more
CALL_S = 15                 # a call waits no longer: the looks run beside the digest's push
CHANGES = 200               # changes read back at most

Platform = Callable[[str, dict, float], Awaitable[dict]]  # the platform's `/owner/<call>` (`PlatformClient.owner`)


class Rows(list):
    """The rows a read gave, and whether that was all of them."""
    complete = True


class Records:
    """A writer's hand on one collection: the Router's code rule (`code:…`, via `code`), or the assistant herself
    when code writes on her word (`via` her own)."""

    def __init__(self, platform: Platform, actor: str, collection: str, via: str = "code") -> None:
        self.platform, self.actor, self.collection, self.via = platform, actor, collection, via
        self.refused = ""      # the words of the last refusal of a write
        self._unread = False   # the last read failed: said once, then quiet

    async def _call(self, name: str, arguments: dict, call: str) -> dict:
        body = {"actor": self.actor, "via": self.via, "foreign": [], "call": call, "arguments": arguments}
        return await self.platform(f"collections/{name}", body, CALL_S)

    async def query(self, filter: list[dict], fields: list[str] | None = None) -> Rows | None:
        rows, after = Rows(), None
        for page in range(PAGES):
            arguments = {"collection": self.collection, "filter": filter, "limit": PAGE,
                         **({"fields": fields} if fields else {}), **({"after": after} if after else {})}
            data = await self._call("query", arguments, f"q/{self.collection}/{page}/{secrets.token_hex(4)}")
            answer = data.get("answer") if data.get("ok") else None
            if not isinstance(answer, dict):
                if not self._unread:
                    log.warning("%s not read as %s: %s", self.collection, self.actor,
                                line(str(data.get("text") or "no answer"), 300))
                    self._unread = True
                return None
            rows += [r for r in answer.get("rows") or [] if isinstance(r, dict)]
            if not (after := answer.get("next")):
                break
        else:
            rows.complete = False
        self._unread = False
        return rows

    async def apply(self, ops: list[dict], idem: str) -> dict | None:
        """The ops in one apply, all or none: `{records, change_ids}`; nothing to write is written at once."""
        if not ops:
            return {"records": [], "change_ids": []}
        data = await self._call("apply", {"collection": self.collection, "ops": ops, "idem": idem}, idem)
        if not data.get("ok"):
            self.refused = str(data.get("text") or "no answer")
            log.warning("%s not written as %s: %s", self.collection, self.actor, line(self.refused, 300))
            return None
        return data.get("answer") or {"records": [], "change_ids": []}

    async def changes(self, since: float) -> list[dict]:
        """This writer's changes to this collection since `since`, oldest first, of the last CHANGES of everyone's."""
        data = await self.platform("collections/changes", {"since": since, "limit": CHANGES}, CALL_S)
        return [c for c in data.get("changes") or [] if isinstance(c, dict)
                and c.get("collection") == self.collection and c.get("actor") == self.actor]

    async def revert(self, change_id: str) -> bool:
        """One change taken back, as the owner's press under it in the evening list would."""
        return bool((await self.platform("collections/revert", {"change_id": change_id}, CALL_S)).get("ok"))


def mark(value: object, chars: int = 16) -> str:
    """An idem's part made of what it writes: two different writes never share one."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:chars]
