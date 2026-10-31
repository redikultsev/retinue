"""The owner's pages (`pages`): HTML pages on his own server, behind his VPN, for his devices only, each at
`<pages_url>/<id>` — served by the pages service (`retinue/pagesd.py`, deploy/pages), not by the router.

The router's side is a data folder the service reads: the base's HTML files that opted in, with the list that names
them (pages.json, each with its sha256: the service takes a page only with the bytes the list names), and the trip
pages it renders itself from her references and travel-ops' numbers (`publish`). The model never writes the HTML of
a trip page: every value she or a site sent goes through `html.escape`, and the page has no script and loads nothing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from .render import linkable
from .travel import Refused as TravelRefused, Unavailable as TravelUnavailable

log = logging.getLogger("retinue.pages")

ID = re.compile(r"[0-9a-f]{16}")  # a page's address: random, so that a link in a chat says nothing


class Refused(ValueError):
    """What the assistant sent does not fit; the text names the field, in words she acts on."""


# --- the schema: the same dict is the tool's input schema for her and the check here --------------------------------

TYPES = {"object": (dict, "must be an object"), "array": (list, "must be a list"),
         "string": (str, "must be a string"), "integer": (int, "must be an integer"),
         "number": ((int, float), "must be a number"), "boolean": (bool, "true or false")}


def check(schema: dict, value, where: str = ""):
    """`value` if it fits `schema` — a subset of JSON Schema: type, properties, required, additionalProperties,
    items, minItems, maxItems, enum, minLength, maxLength, pattern, format date, minimum, maximum. Raises Refused
    naming the first field that does not fit."""
    name = where or "arguments"
    kind = schema.get("type")
    if kind:
        types, words = TYPES[kind]
        if not isinstance(value, types) or (kind != "boolean" and isinstance(value, bool)):
            raise Refused(f"{name}: {words}")
    if "enum" in schema and value not in schema["enum"]:
        raise Refused(f"{name}: one of: {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise Refused(f"{name}: empty string" if schema["minLength"] == 1 else
                          f"{name}: at least {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", len(value)):
            raise Refused(f"{name}: at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise Refused(f"{name}: {schema.get('description') or 'malformed'}")
        if schema.get("format") == "date":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError(value)
                date.fromisoformat(value)
            except ValueError:
                raise Refused(f"{name}: a date like 2026-05-14") from None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if (low is not None and value < low) or (high is not None and value > high):
            raise Refused(f"{name}: from {low} to {high}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise Refused(f"{name}: from {schema.get('minItems', 0)} to {schema.get('maxItems')} items")
        for i, item in enumerate(value):
            check(schema.get("items", {}), item, f"{where}[{i}]")
    if isinstance(value, dict):
        fields = schema.get("properties", {})
        if schema.get("additionalProperties") is False and (unknown := [k for k in value if k not in fields]):
            raise Refused(f"{name}: unknown field {str(unknown[0])[:40]}. Fields: {', '.join(fields)}.")
        for field in schema.get("required", []):
            if field not in value:
                raise Refused(f"{name}: missing field {field}")
        for field, inner in fields.items():
            if field in value:
                check(inner, value[field], f"{where}.{field}" if where else field)
    return value


def text(cap: int, about: str, least: int = 0) -> dict:
    return {"type": "string", "maxLength": cap, "description": about, **({"minLength": least} if least else {})}


OPTION_KINDS = ("flight", "stay", "ground")
# What she sends: her words and references to travel-ops' answers. Prices, sellers, times, ratings and photos the
# router takes from travel-ops itself by `search_id` and `link` — what is on the page was seen by code, not retold.
TRIP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "summary", "start", "end", "options"],
    "properties": {
        "trip_id": {"type": "string", "pattern": "^[0-9a-f]{16}$",
                    "description": "the page's id from publish_trip's answer — only to update the same trip"},
        "title": text(80, "the page's title: from, to, dates — \"Springfield — Shelbyville, 22–24 May\"", 1),
        "summary": text(1500, "your conclusion for the owner: what you recommend and why, what the price leaves out, "
                              "what is off with the dates — plain text, no markup and no links"),
        "start": {"type": "string", "format": "date", "description": "the trip's first day, 2026-05-22"},
        "end": {"type": "string", "format": "date", "description": "the trip's last day, 2026-05-24"},
        "options": {"type": "array", "minItems": 1, "maxItems": 12, "description": "the options you found",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["kind", "search_id", "link", "note"],
                        "properties": {
                            "kind": {"type": "string", "enum": list(OPTION_KINDS),
                                     "description": "flight — a flight, stay — a place to stay, ground — a train or a bus"},
                            "search_id": {"type": "string", "pattern": "^[a-z0-9]{6,16}$",
                                          "description": "the search_id of travel-ops' answer that has this option"},
                            "link": text(1000, "the option's link from travel-ops' answer, whole (link.url)"),
                            "note": text(600, "your words about the option: why this one, what the reviews say (as someone else's opinion)"),
                            "pick": {"type": "boolean", "description": "true — you recommend this option"},
                        }}},
    },
}


# --- the folder the pages are built from --------------------------------------------------------------------------

class Data:
    """The data folder: the router writes it, the build container reads it at any moment. A file is replaced
    whole (a temporary file and `rename`), so a build sees the old file or the new one, never half of one."""

    def __init__(self, folder: str) -> None:
        self.root = Path(folder)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o755)

    def path(self, name: str) -> Path:
        parts = PurePosixPath(name).parts
        if not name or name.startswith("/") or any(p in ("..", ".") or p.startswith(".") for p in parts):
            raise ValueError(f"not a data file name: {name!r}")
        return self.root.joinpath(*parts)

    def write_bytes(self, name: str, data: bytes) -> None:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        for folder in (path.parent, *path.parent.parents):
            if folder == self.root or self.root not in folder.parents:
                break
            os.chmod(folder, 0o755)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        except BaseException:
            os.unlink(temporary)
            raise

    def write(self, name: str, value) -> None:
        self.write_bytes(name, json.dumps(value, ensure_ascii=False, indent=1).encode())

    def read(self, name: str):
        try:
            return json.loads(self.path(name).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def names(self, folder: str) -> list[str]:
        where = self.path(folder)
        return sorted(f"{folder}/{p.name}" for p in where.iterdir() if not p.name.startswith(".")) \
            if where.is_dir() else []

    def remove(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def has(self, folder: str, page_id: str) -> bool:
        return bool(ID.fullmatch(page_id)) and self.path(f"{folder}/{page_id}.json").is_file()

    def new_id(self, folder: str) -> str:
        while self.has(folder, page_id := secrets.token_hex(8)):
            pass
        return page_id


# --- what the router keeps here ----------------------------------------------------------------------------------

REFRESH_S = 300           # the base is brought to the hub's head and the pages written this often
AFTER_RUN_S = 60          # and this long after a model run ended: a page she just committed shows soon
URL = re.compile(r"https?://[^\s<>\"'`]+")
LINKS = 4                 # addresses taken out of one note at most
URL_CHARS = 2000          # an address longer than this is not made a link
SUMMARY_CHARS = 280       # a trip summary's paragraph is cut after a sentence past this
KIND_NAMES = {"flight": "Flight", "stay": "Stay", "ground": "Overland"}
NEEDS = {"read": "read", "reply": "reply", "decide": "decide", "attend": "attend", "pay": "pay", "act": "do"}
CHIP_CHARS = 28           # a sender's name on a chip: her «who» up to its first aside, then cut
TRACKER_NOTES = 300       # a tracker row's notes, cut to this
# The tracker's defaults; the owner's own are his config (`Tracker`).
STATES = {"new": "new", "sent": "sent", "answered": "answered", "meeting": "meeting", "won": "accepted",
          "lost": "rejected", "dropped": "dropped"}
# The page's groups of rows: what goes on, where an answer is due, what to do next, the closed. A group keyed «next»
# is sorted by score; «closed» is folded. Rows of no group's state are «Other».
GROUPS = (("live", "Ongoing", ("meeting", "answered")), ("waiting", "Awaiting reply", ("sent",)),
          ("next", "Next", ("new",)), ("closed", "Closed", ("won", "lost", "dropped")))
COUNTED = ("sent", "answered", "meeting", "won", "lost")  # a row counts for the week when it reached one of these
MEETING_WORDS = ("meeting", "встреча")  # a calendar event with one of these, or a row's item, in its title (the
# calendar is the owner's data: Russian titles too)


@dataclass(frozen=True)
class Tracker:
    """The owner's tracker: a table of items with statuses, kept as a Record of his base, and a weekly goal — the
    Record's header field `weekly_goal`. What it tracks is his config — `memory.tracker` in router.yaml — not code:
    the Record's `path`, the page's `title`, the `states` and their names, the page's `groups` (key, title, states),
    the states a row `counted` for the week once it reached (in the order a row moves; a new row starts at the first),
    the words of a calendar event that is one of its `meetings`, and the table's columns of the `item` and its
    `detail`. Each has a neutral default."""
    path: str = "profile/tracker.md"
    title: str = "Tracker"
    states: dict = dataclass_field(default_factory=lambda: dict(STATES))
    groups: tuple = GROUPS
    counted: tuple = COUNTED
    meetings: tuple = MEETING_WORDS
    item: str = "item"
    detail: str = "detail"

    @classmethod
    def of(cls, memory) -> Tracker:
        raw = dict(getattr(getattr(memory, "cfg", None), "tracker", None) or {})
        try:
            if "states" in raw:
                raw["states"] = {str(k).lower(): str(v) for k, v in dict(raw["states"]).items()}
            if "groups" in raw:
                raw["groups"] = tuple((str(g["key"]), str(g["title"]), tuple(str(s).lower() for s in g["states"]))
                                      for g in raw["groups"])
            for name in ("counted", "meetings"):
                if name in raw:
                    raw[name] = tuple(str(word).lower() for word in raw[name])
            for name in ("path", "title", "item", "detail"):
                if name in raw:
                    raw[name] = str(raw[name]).strip()
            return cls(**raw)
        except (TypeError, ValueError, KeyError):
            raise SystemExit("router config: memory.tracker takes path, title, states, groups (key, title, states), "
                             f"counted, meetings, item, detail — not {sorted(raw)}")

    def rows(self, text: str) -> list[dict]:
        """The tracker's table, read by its header — the columns vary between versions: the number, the date, the
        item, its detail, the score, the status, the report's number, the notes. Markdown is stripped; a status not
        in `states` is shown as it is. Anything else in the file — the Record's header — is skipped."""
        header, rows = [], []
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not header:
                if any(cell.lower() == "status" for cell in cells):
                    header = [cell.lower() for cell in cells]
                continue
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                continue
            row = dict(zip(header, (plain(cell) for cell in cells)))
            status = row.get("status", "")
            key = status.lower() if status.lower() in self.states else ""
            notes, links = unlinked(row.get("notes", ""))
            rows.append({"n": row.get("#", ""), "date": row.get("date", ""),
                         "item": row.get(self.item.lower(), "") or "?", "detail": row.get(self.detail.lower(), ""),
                         "score": row.get("score", ""), "key": key, "status": self.states.get(key, status),
                         "tone": next((g for g, _, states in self.groups if key in states), "other"),
                         "report": "".join(re.findall(r"\d+", row.get("report", "")))[:6],
                         "notes": notes[:TRACKER_NOTES], "links": links})
        return rows

    def line(self, tree: str) -> str:
        """The judgement run's line about the tracker, when its Record is in the base: where it is and the states a
        letter may move a row to."""
        if not tree or not (Path(tree) / self.path).is_file():
            return ""
        moves = ", ".join(f"`{state.capitalize()}`" for state in self.counted)
        return (f"The owner's tracker \"{self.title}\": `{tree.rstrip('/')}/{self.path}`. Status after sending — "
                f"exactly one of {moves}; a new row — `{self.counted[0].capitalize()}`." if self.counted else
                f"The owner's tracker \"{self.title}\": `{tree.rstrip('/')}/{self.path}`.")


def unlinked(text: str) -> tuple[str, list[dict]]:
    """A note's addresses taken out of its words: the text without them, and each https address with its host — a
    page shows the host on a button, never the address as text that runs off a phone's screen."""
    links = []
    for found in URL.findall(text):
        url = sound(found.rstrip(".,;:!?)»"))
        if url and all(link["url"] != url for link in links):
            links.append({"url": url, "host": host_of(url)})
    words = re.sub(r"\s{2,}", " ", URL.sub("", text)).strip(" ;,:—-")
    return re.sub(r"\s+([.,;:])", r"\1", words), links[:LINKS]


def plain(cell: str) -> str:
    """A table cell as text: a link is its text, emphasis marks go."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell).replace("**", "").replace("`", "").strip()


class Pages:
    """The router's side of the owner's pages: what it writes to the data folder and when. Attached to the core like
    the mailroom; reads the base and travel-ops' stored searches, and never calls a model."""

    def __init__(self, data: Data, url: str, link_hosts: list[str], build_status: str = "") -> None:
        self.data = data
        self.pages_head = ""                  # the hub's commit the pages were last read from
        self.base_pages: list[dict] | None = None  # the base's entries of pages.json as last read; None — not yet
        self.build_status = build_status      # build.json the builder writes after every build; "" — not known
        self.url = url.rstrip("/")            # the pages' address as the owner's devices open it
        self.link_hosts = list(link_hosts)    # travel-ops' sites: a link there is a button on a trip page
        self.core = None
        self.written = 0.0                    # when the data was last written
        self.poked = 0.0                      # when a model run last ended, until the data is written
        self.task: asyncio.Task | None = None
        self.pending: list[str] = []          # trip pages whose photos are still to fetch
        self.fetching: asyncio.Task | None = None

    def attach(self, core) -> None:
        self.core = core

    # --- when ------------------------------------------------------------------------------------------------------

    def poke(self, now: float) -> None:
        """A model run ended: the pages are written again a minute later (runs end in bursts)."""
        self.poked = self.poked or now

    def step(self, now: float) -> None:
        """Called by the router's clock: one refresh at a time, every REFRESH_S and AFTER_RUN_S after a run; the
        photos of a page just published, one page at a time."""
        due = now - self.written >= REFRESH_S or (self.poked and now - self.poked >= AFTER_RUN_S)
        if due and (self.task is None or self.task.done()):
            self.written, self.poked = now, 0.0
            self.task = asyncio.create_task(self.refresh(now))
        if self.pending and (self.fetching is None or self.fetching.done()):  # photos: one page at a time
            self.fetching = asyncio.create_task(self.photos(self.pending.pop(0), self.core.travel, now))

    async def refresh(self, now: float) -> None:
        """The base brought to the hub's head when no run is going — what the Mac pushed shows without waiting for
        her next run (and the heartbeat skips its own fetch: `written`) — then the pages written. A failure is logged,
        and the next refresh comes."""
        core = self.core
        try:
            if core.memory and not core.queue.locked():
                async with core.queue:
                    await core._fresh_base()
            await self.write_pages(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pages: not written")

    @property
    def tz(self) -> str:
        """The owner's effective time zone: every time on a trip page is local there. The one place the pages read
        it. `core.tz` is the zone in force — router.yaml's `owner_tz` at first, then whatever the owner set in words
        (`set_timezone`, kept in the router's state and put back into `core.tz` on start: retinue/proactive.py)."""
        return self.core.tz

    def chips(self, now: float) -> list[dict]:
        """The row of status chips «Waiting for you» shows (`waiting.chips`): whether anything is broken, the backup,
        the limit, the pages' build — each `ok`, `warn` or `bad`."""
        broken = [c for c in self.connectors(now) if not c["ok"]]
        out = [{"key": "health", "text": "all working" if not broken else
                f"{len(broken)} {plural(len(broken), 'failure', 'failures')}", "level": "bad" if broken else "ok"}]
        if self.core.backup_status:
            said = self.core.backup(now)
            good = backed_up(said)
            out.append({"key": "backup", "text": BACKUP.sub(r"\1 ", said, count=1) if good else said,
                        "level": "ok" if good else "bad"})
        windows = self.core.store.windows()
        if windows:
            share = max(round(w["utilization"] * 100) for w in windows.values())
            out.append({"key": "limit", "text": f"limit {share} %", "level": level(share)})
        if (built := self.built(now)) is not None:
            out.append({"key": "build", "text": built["said"],
                        "level": "ok" if built["ok"] else "bad"})
        return out

    def stamp(self, ts: float) -> str:
        from . import clock

        return clock.stamp(ts, self.tz)

    def connectors(self, now: float) -> list[dict]:
        """Each way in and out the router has, and how it is doing."""
        from . import clock

        core, out = self.core, []
        for channel in core.channels:
            out.append({"name": channel.name.capitalize() if channel.name == "telegram" else channel.name,
                        "ok": True, "state": "working"})
        if core.travel is None:
            out.append({"name": "travel-ops", "ok": False, "state": "not connected"})
        elif core.travel_state is None:
            out.append({"name": "travel-ops", "ok": True, "state": "connected, not polled yet"})
        else:
            seen, error = core.travel_state
            out.append({"name": "travel-ops", "ok": not error,
                        "state": f"{'did not answer' if error else 'answered'} {clock.ago(now - seen)} ago"
                                 + (f": {error}" if error else "")})
        if core.mail:
            for row in core.mail.store.health():
                what = "mail" if row["source"] == "mail" else "calendar"
                if row["error"] == "invalid_grant":
                    state, ok = "needs a new sign-in", False
                elif row["error"]:
                    state, ok = f"failure: {row['error']}", False
                elif row["last_ok"]:
                    state, ok = f"synced {clock.ago(now - row['last_ok'])} ago", True
                else:
                    state, ok = "not synced yet", False
                out.append({"name": what, "detail": row["account"], "ok": ok, "state": state})
        else:
            out.append({"name": "mail and calendar", "ok": False, "state": "not connected"})
        if core.mail and core.mail.gateway:
            seen, said = core.mail.chats or (0.0, "not polled yet")
            ok = isinstance(said, dict) and bool(said.get("connected"))
            out.append({"name": "Telegram Business", "ok": ok, "state": said if isinstance(said, str) else
                        ("bot connected" if ok else "bot not connected") + (
                            f", rights: {', '.join(said.get('rights') or []) or 'none'}" if isinstance(said, dict) else "")})
        if core.outbox:
            out.append({"name": "mail sending", "ok": bool(core.outbox.sender),
                        "state": "connected" if core.outbox.sender else "not connected"})
        out.append({"name": "the Base", "ok": bool(core.memory),
                    "state": "connected" if core.memory else "not connected"})
        if core.backup_status:
            said = core.backup(now)
            out.append({"name": "backup", "ok": backed_up(said), "state": BACKUP.sub("", said, count=1)})
        if (built := self.built(now)) is not None:
            out.append({"name": "pages", "ok": built["ok"], "state": built["said"].removeprefix("pages ")})
        if getattr(core, "watch", None):
            said = core.watch.health(now)
            out.append({"name": "containers", "ok": bool(CONTAINERS_OK.match(said)),
                        "state": CONTAINERS.sub("", said, count=1)})
        return out

    def health(self, now: float) -> str:
        """The pages' part of the health line, from the builder's build.json: built when, or why not."""
        built = self.built(now)
        return built["said"] if built else ""

    def built(self, now: float) -> dict | None:
        """How the last build went, from build.json the builder writes: ok, when, how long ago, the error, the
        release, and the words for the health line. None when the builder is not set up here."""
        from . import clock

        if not self.build_status:
            return None
        out = {"ok": False, "at": None, "ago": "", "error": "", "release": ""}
        try:
            with open(self.build_status) as file:
                built = json.load(file)
            if not isinstance(built, dict):
                raise ValueError(built)
        except FileNotFoundError:
            return {**out, "said": "pages not built yet"}
        except (OSError, ValueError):
            return {**out, "said": "pages status unreadable"}
        at = float(built.get("at") or 0)
        out.update(at=round(at), ago=clock.ago(now - at), release=str(built.get("release") or ""))
        if not built.get("ok"):
            out["error"] = str(built.get("error") or "no reason recorded").rstrip(". ")
            return {**out, "said": f"pages not updated: {out['error']}"}
        # Pages the build left out: their ids and the check, in the builder's fixed words — never a page's text.
        out["refused"] = [f"{page.get('id')} ({page.get('why')})" for page in built.get("refused") or []
                          if isinstance(page, dict)]
        return {**out, "ok": True, "said": f"pages built {out['ago']} ago" + (
            f"; not published: {', '.join(out['refused'][:3])}" if out["refused"] else "")}

    # --- a trip page: her references, travel-ops' numbers ------------------------------------------------------------

    async def publish(self, trip, travel, now: float) -> tuple[bool, str]:
        """A trip page from what she sends (`TRIP_SCHEMA`): every option is looked up in travel-ops' stored search by
        its `search_id` and `link` — no site is asked — and the page takes the price, the seller, the times, the
        rating and when the price was seen from there. Refused whole, naming what to fix; nothing half-written."""
        try:
            check(TRIP_SCHEMA, trip)
        except Refused as exc:
            return False, f"Not published: {str(exc).rstrip('.')}."
        if travel is None:
            return False, "Not published: travel-ops is not connected — there is nowhere to take the page's prices from."
        page_id = trip.get("trip_id")
        if page_id and not self.data.has("trips", page_id):
            return False, f"Not published: there is no page {page_id} — publish without trip_id."
        for number, option in enumerate(trip["options"]):
            if not linkable(option["link"], self.link_hosts):
                return False, (f"Not published: options[{number}].link is not a link to a travel-ops site: take "
                               "link.url from the tool's answer, whole.")
        views: dict[tuple, dict] = {}
        options = []
        try:
            for number, option in enumerate(trip["options"]):
                found = None
                for bars in LOOKS[option["kind"]]:  # each view is asked once for all the options of its search
                    key = (option["kind"], option["search_id"], tuple(bars.items()))
                    if key not in views:
                        views[key] = await travel.call(VIEWS[option["kind"]], {
                            "search_id": option["search_id"], "limit": VIEW_LIMIT, **bars})
                    found = next(((card, offer, views[key]) for card, offer in offers(option["kind"], views[key])
                                  if (offer.get("link") or {}).get("url") == option["link"]), None)
                    if found:
                        break
                if found is None:
                    return False, (f"Not published: options[{number}] — this link is not in search "
                                   f"{option['search_id']}. Take the option's link.url from travel-ops' answer for "
                                   "this search_id.")
                options.append(self.describe(option, *found))
        except (TravelRefused, TravelUnavailable) as exc:
            return False, f"Not published: {str(exc).rstrip('.')}."
        page_id = page_id or self.data.new_id("trips")
        self.render({
            "id": page_id, "title": trip["title"], "summary": trip["summary"], "paragraphs": paragraphs(trip["summary"]),
            "start": trip["start"], "end": trip["end"], "published": self.stamp(now), "published_ts": round(now),
            "options": options, "timeline": timeline(options)}, now)
        self.write_index()
        if any(option.get("photo_from") for option in options):  # the page goes up now, its photos a little later
            self.pending.append(page_id)
        url = f"{self.url}/{TRIP}{page_id}"
        return True, (f"Published: {url} (trip_id {page_id} — to update this same page). At the end of your reply to "
                      f"the owner: [details]({url})")

    def render(self, trip: dict, now: float) -> None:
        """A trip's data to trips/<id>.json and its page to pages/trip-<id>.html, with the page's entry of pages.json
        kept in the data (`page`): the list is put together from the base's pages and these (`write_index`)."""
        body = trip_page(trip, self.link_hosts).encode("utf-8")
        meta = page_meta(body.decode("utf-8")) or {}
        trip["page"] = {
            "id": f"{TRIP}{trip['id']}", "title": str(trip.get("title") or ""),
            "description": meta.get("description", ""), "words": meta.get("words", 0),
            "reading_min": meta.get("reading_min", 1), "topic": "trips",
            "updated": _local_day(trip.get("published_ts") or now, self.tz), "author": "assistant", "scripts": "off",
            "url": f"/{TRIP}{trip['id']}", "path": f"trips/{trip['id']}",
            "sha256": hashlib.sha256(body).hexdigest()}
        self.data.write_bytes(f"pages/{TRIP}{trip['id']}.html", body)
        self.data.write(f"trips/{trip['id']}.json", trip)

    def write_index(self) -> None:
        """pages.json: the base's pages as last read (`pages`) and the router's trip pages, the newest first. A trip
        whose page is not in the data — written by a router before trip pages were its own — is rendered here."""
        base = self.base_pages
        if base is None:  # not read since the router started: the base's entries as the list has them
            listed = self.data.read("pages.json")
            base = [page for page in listed if isinstance(page, dict) and not str(page.get("id", "")).startswith(TRIP)] \
                if isinstance(listed, list) else []
        trips = []
        for name in self.data.names("trips"):
            trip = self.data.read(name)
            if not isinstance(trip, dict) or not ID.fullmatch(str(trip.get("id", ""))):
                continue
            if not isinstance(trip.get("page"), dict) or not self.data.path(f"pages/{TRIP}{trip['id']}.html").is_file():
                self.render(trip, float(trip.get("published_ts") or time.time()))
            trips.append(trip["page"])
        pages = sorted(base + trips, key=lambda page: (str(page.get("updated", "")), str(page.get("id", ""))),
                       reverse=True)
        self.data.write("pages.json", pages)

    async def photos(self, page_id: str, travel, now: float) -> None:
        """Photos of the page's stays: travel-ops fetches them from the sites (`stay_photos`; the router has no way
        there, and a page may not load them from there), the router's file worker re-encodes each as a JPEG, and what
        does not decode is dropped. Each goes into the page itself as a data: address — the page loads nothing — at
        most PHOTOS a stay, and a photo that would take the page past PAGE_MAX is left out. A look that fails leaves
        the page as it was."""
        from .attachments import Unreadable, Upload, isolated

        name = f"trips/{page_id}.json"
        page = self.data.read(name) or {}
        found: dict[str, list[str]] = {}  # an option's link: its photos as data: addresses
        for option in page.get("options") or []:
            source = option.get("photo_from") or {}
            if not source.get("stay"):
                continue
            try:
                result = await travel.result("stay_photos", {"search_id": source["search_id"],
                                                             "stays": [source["stay"]], "per_stay": PHOTOS})
            except (TravelRefused, TravelUnavailable) as exc:
                log.warning("pages: photos of %s not fetched: %s", page_id, exc)
                continue
            names = []
            for block in result.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "image" or len(names) >= PHOTOS:
                    continue
                try:
                    done = await isolated(Upload("photo", base64.b64decode(str(block.get("data", "")))))
                except (Unreadable, ValueError):
                    continue
                names.append(JPEG + base64.b64encode(done.files[0][1]).decode("ascii"))
            if names:
                found[option["link"]] = names
        page = self.data.read(name)  # she may have published it again meanwhile: her new page, these photos
        if found and page:
            options = page.get("options") or []
            wanted = [found.get(option.get("link"), option.get("photos") or []) for option in options]
            for option in options:
                option["photos"] = []
            size = len(trip_page(page, self.link_hosts).encode("utf-8"))
            for option, photos in zip(options, wanted):
                for photo in photos[:PHOTOS]:
                    if size + len(photo) + PHOTO_TAG <= PAGE_MAX:
                        option["photos"].append(photo)
                        size += len(photo) + PHOTO_TAG
            self.render(page, now)
            self.write_index()

    def describe(self, option: dict, card: dict, offer: dict, view: dict) -> dict:
        """One option as the page shows it, in code's words from travel-ops' card: what, the price, the lines."""
        kind = option["kind"]
        price = money(offer.get("price") or offer.get("total"))
        converted = money(offer.get("converted"))
        seen = f"price as of {self.moment(offer.get('seen_at'))}"
        if view.get("searched_at"):
            seen += f" (search {self.moment(view['searched_at'])})"
        out = {"kind": kind, "what": "", "pick": option.get("pick") is True, "note": option["note"], "price": price,
               "converted": "" if converted == price else converted, "seller": str(offer.get("seller") or ""),
               "lines": [], "seen": seen, "link": option["link"], "linkable": True, "photos": [],
               "seen_ts": _epoch(offer.get("seen_at")), "leg": "", "day": "", "host": host_of(option["link"])}
        if kind == "flight":
            route, legs = card.get("route") or [], card.get("outbound") or []
            out["leg"] = f"{route[0]} → {route[-1]}" if len(route) >= 2 else ""
            out["day"] = str((legs[0] if legs else {}).get("departs") or "")[:10]
            stops = card.get("stops") or 0
            changes = f"{stops} {plural(stops, 'stop', 'stops')}" if stops else "nonstop"
            out["what"] = f"Flight {' → '.join(card.get('route') or [])}, {changes}"
            for word, legs in (("out", card.get("outbound") or []), ("back", card.get("inbound") or [])):
                if legs:
                    flights = dict.fromkeys(str(leg.get("flight") or leg.get("carrier")) for leg in legs)
                    out["lines"].append(f"{word} {clock_of(legs[0]['departs'])} → {clock_of(legs[-1]['arrives'])} · "
                                        + ", ".join(flights))
            out["lines"].append("travel time " + duration(card.get("duration_min"))
                                + (f", back {duration(card['return_duration_min'])}"
                                   if card.get("return_duration_min") else ""))
            baggage = offer.get("baggage") or {}
            out["lines"].append("baggage: " + ("included" + (f", {baggage['checked_kg']} kg" if baggage.get("checked_kg")
                                                             else "") if baggage.get("checked") else
                                               "hand luggage only" if baggage.get("checked") is False else
                                               "the source did not say"))
        elif kind == "stay":
            stay = card.get("stay") or {}
            out["what"] = f"Stay: {stay.get('name', '')}"
            out["leg"] = str(stay.get("name") or "")
            about = [STAY_KINDS.get(stay.get("kind"), "stay")]
            if stay.get("rating") is not None:
                reviews = stay.get("reviews")
                about.append(f"rating {decimal(stay['rating'])}" + (
                    f" ({reviews} {plural(reviews, 'review', 'reviews')})" if reviews else ""))
            if stay.get("center_km") is not None:
                about.append(f"{decimal(stay['center_km'])} km from the centre")
            out["lines"].append(" · ".join(about))
            nights = card.get("nights") or 0
            night = [f"{nights} {plural(nights, 'night', 'nights')}"] if nights else []
            night += [f"{money(offer['per_night'])} per night"] if offer.get("per_night") else []
            night += [str(offer["room"])] if offer.get("room") else []
            out["lines"].append(" · ".join(night))
            if offer.get("free_cancellation"):
                out["lines"].append("free cancellation" + (f" until {offer['free_cancel_until']}"
                                                           if offer.get("free_cancel_until") else ""))
            elif offer.get("free_cancellation") is False:
                out["lines"].append("no free cancellation")
            out["photo_from"] = {"search_id": option["search_id"], "stay": str(stay.get("source_id") or "")}
        else:
            rides = card.get("rides") or []
            out["what"] = "Overland " + " → ".join([str(rides[0].get("from"))] + [str(r.get("to")) for r in rides]) \
                if rides else "Overland"
            if rides:
                out["leg"] = f"{rides[0].get('from')} → {rides[-1].get('to')}"
                out["day"] = str(rides[0].get("departs") or "")[:10]
            for ride in rides:
                out["lines"].append(f"{RIDES.get(ride.get('mode'), ride.get('mode'))} {clock_of(ride.get('departs'))}"
                                    f" → {clock_of(ride.get('arrives'))} · {ride.get('carrier') or ''} "
                                    f"{ride.get('number') or ''}".rstrip(" ·"))
            out["lines"].append("travel time " + duration(card.get("duration_min")))
        return out

    def moment(self, iso) -> str:
        try:
            return self.stamp(datetime.fromisoformat(str(iso)).timestamp())
        except ValueError:
            return str(iso)

    # --- the owner's own pages: HTML files of the base, served sandboxed at <pages_url>/<id> --------------------------

    async def write_pages(self, now: float) -> None:
        if self.core.memory:
            await asyncio.to_thread(self.pages, now)
        self.write_index()  # again on the loop: a trip she published while the thread read the base is in the list

    def pages(self, now: float) -> bool:
        """Every HTML file in a nature folder of the base that says `<meta name="lifehub" content="publish">`, as the
        hub's `main` has it: its bytes to data/pages/<id>.html, what the «Library» lists to pages.json (with each
        file's sha256: the builder takes a page only with the bytes this list names; the router's trip pages are
        listed beside them, `write_index`), what was left out and why to pages-refused.json. Scripts run only when no
        commit of hers ever touched the file (`author`). An id that begins with `trip-` is the router's, never a
        file's. Read again only when `main` moved. Returns whether it read the hub."""
        import hashlib

        from . import clock

        memory = self.core.memory
        ref = f"refs/remotes/origin/{memory.policy.branch}"
        head = git_out(memory, "rev-parse", "--verify", "-q", ref).strip()
        if not head or head == self.pages_head:
            return False
        listing = git_out(memory, "ls-tree", "-r", "-z", "--long", ref)
        found: dict[str, list[dict]] = {}
        refused: list[dict] = []
        for line in filter(None, listing.split("\0")):
            info, _, path = line.partition("\t")
            mode, kind, blob, size = info.split()
            parts = path.split("/")
            if (kind != "blob" or not path.endswith(".html") or len(parts) < 3 or parts[1] not in NATURES
                    or any(part.startswith(".") for part in parts)):
                continue
            if mode != "100644" or not PAGE_PATH.fullmatch(path):  # a name git or a glob would read another way
                log.warning("pages: %r is not a plain page name: not published", path)
                refused.append({"id": "?", "why": "file name malformed: a-z, 0-9, / _ . -"})
                continue
            if int(size) > PAGE_MAX:
                refused.append({"id": slug(PurePosixPath(path).stem) or "?", "why": "over 3 MB"})
                continue
            body = git_bytes(memory, "cat-file", "blob", blob)
            meta = page_meta(body.decode("utf-8", "replace"))
            if meta is None:
                continue
            page_id = meta.pop("id") or slug(PurePosixPath(path).stem)
            if not PAGE_ID.fullmatch(page_id):
                refused.append({"id": "?", "why": "lifehub:id malformed: a-z, 0-9, -"})
                continue
            if page_id.startswith(TRIP):  # the address of a trip page the router renders: not a file's to take
                refused.append({"id": page_id, "why": f"{TRIP} ids are the router's trip pages"})
                continue
            author, seconds = self.author(path, ref)
            found.setdefault(page_id, []).append({
                "id": page_id, **meta, "topic": parts[0],
                "updated": clock.local(seconds or now, self.core.tz).date().isoformat(),
                "author": author, "scripts": "on" if author == "owner" else "off",
                "url": f"/{page_id}", "path": path,
                "sha256": hashlib.sha256(body).hexdigest(), "body": body})
        pages = []
        for page_id, claims in found.items():
            owners = [page for page in claims if page["author"] == "owner"]
            if len(claims) > 1 and len(owners) != 1:  # one address, two files: nobody's guess which one is meant
                refused.append({"id": page_id, "why": "one id on two files, neither of them yours" if not owners
                                else "one id on two files of yours"})
                continue
            pages.append(owners[0] if len(claims) > 1 else claims[0])
        pages.sort(key=lambda page: (page["updated"], page["id"]), reverse=True)
        if len(pages) > PAGES_MAX:
            refused += [{"id": page["id"], "why": f"more than {PAGES_MAX} pages"} for page in pages[PAGES_MAX:]]
            pages = pages[:PAGES_MAX]
        for page in pages:
            body = page.pop("body")
            name = f"pages/{page['id']}.html"
            if (self.data.path(name).read_bytes() if self.data.path(name).is_file() else None) != body:
                self.data.write_bytes(name, body)
        ids = {page["id"] for page in pages}
        for name in self.data.names("pages"):  # a page no longer published goes from the data, and so from the site
            stem = PurePosixPath(name).stem
            if stem not in ids and not stem.startswith(TRIP):  # a trip page is the router's, not the base's
                self.data.remove(name)
        self.data.write("pages-refused.json", refused)
        self.base_pages = pages
        self.write_index()
        self.pages_head = head
        return True

    def library(self) -> list[dict] | None:
        """The list «Read» shows in the face, pushed to the platform (`bridge.push_library`): each page's id, title,
        full address, Space, day and reading time — no description, no body, no path in the base; a page with no
        <title> goes by its id, a page the last build refused is left out (its address is a 404). None until the
        pages were first read: an empty list would say every page is gone."""
        pages = self.data.read("pages.json")
        if not isinstance(pages, list):
            return None
        refused = self.refused_pages()
        return [{"id": page["id"], "title": page["title"] or page["id"], "url": f"{self.url}{page['url']}",
                 "updated": page["updated"], "section": page.get("topic"),
                 **({"minutes": page["reading_min"]} if page.get("reading_min") else {})}
                for page in pages if isinstance(page, dict) and page.get("id") not in refused]

    def refused_pages(self) -> set[str]:
        """The ids of the pages the builder left out of its last release (build.json), or none when it says nothing."""
        try:
            with open(self.build_status) as file:
                built = json.load(file)
        except (OSError, ValueError):
            return set()
        listed = built.get("refused") if isinstance(built, dict) else None
        return {str(page.get("id")) for page in listed or [] if isinstance(page, dict)} if isinstance(listed, list) \
            else set()

    def author(self, path: str, ref: str) -> tuple[str, float]:
        """("assistant" | "owner", when the file last changed). Hers when any commit in the file's whole history is
        the router's identity without the owner's upload trailer: she reads mail and the web, and a page of hers is
        where an injected script would run — an edit of his on top does not make the rest his. The path is literal
        (`*` is a character, not a glob) and every side of a merge counts. To run a page's scripts after she touched
        it, the owner publishes it under a new name."""
        memory = self.core.memory
        router = memory.env.get("GIT_AUTHOR_EMAIL", "")
        out = git_out(memory, "--literal-pathspecs", "log", "--full-history", "--no-renames",
                      f"--format=%ae%x1f%ct%x1f%(trailers:key={UPLOADED},valueonly)%x1e", ref, "--", path)
        history = [entry.strip("\n").split("\x1f") for entry in out.split("\x1e") if entry.strip()]
        hers = any(email == router and not (rest[1:] and rest[1].strip()) for email, *rest in history)
        seconds = float(history[0][1]) if history and len(history[0]) > 1 and history[0][1] else 0.0
        return ("assistant" if hers or not history else "owner"), seconds

    async def upload(self, origin, space: str, upload, native_id: str) -> None:
        """The owner sent an HTML file with «to pages <Space>» (or «to hub <Space>», the Russian «в хаб <Space>»:
        `channels/telegram.py`; a page made in claude.ai on his phone): code, not the model, puts it in the base as
        <Space>/artifacts/<name>.html
        with the opt-in mark, commits it with a trailer that says it is his, and answers with the page's address. On
        record like any message of his."""
        from .archive import OWNER

        core = self.core
        name = (upload.name or "page.html").strip()
        event, fresh = core.archive.append(OWNER, f"[page to pages {space}: {name}]", channel=origin.name,
                                           native_id=native_id, meta={"hub_page": space},
                                           conversation_id=core.store.conversation(core.default_agent))
        if not fresh and core.archive.answered(event.id):
            return  # delivered again: answered already
        try:
            text = await self.put_page(space, name, upload, time.time())
        except Exception as exc:  # the owner hears of it instead of silence
            log.exception("pages: the page %s was not put in the base", name)
            text = f"Page not published: the router failed ({type(exc).__name__})."
        await core.tell_owner(text, origin=origin, ref=event.id)

    async def put_page(self, space: str, name: str, upload, now: float) -> str:
        core = self.core
        memory = core.memory
        if memory is None:
            return "Page not published: the Base is not connected — there is nowhere to put it."
        spaces = sorted(p.name for p in Path(memory.tree).iterdir()  # a Space is a folder with its showcase
                        if SPACE.fullmatch(p.name) and (p / "AGENTS.md").is_file())
        if space not in spaces:
            return f"Page not published: there is no Space \"{space[:40]}\" in the Base. There are: {', '.join(spaces)}."
        if not name.lower().endswith((".html", ".htm")):
            return "Page not published: this is not an HTML file (.html needed)."
        if upload.refused or upload.size > PAGE_MAX:
            return f"Page not published: the file is over {PAGE_MAX // 1024 // 1024} MB."
        data = await upload.fetch()
        if len(data) > PAGE_MAX:
            return f"Page not published: the file is over {PAGE_MAX // 1024 // 1024} MB."
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "Page not published: the file is not UTF-8."
        text = opted_in(text)
        meta = page_meta(text) or {}
        stem = slug(PurePosixPath(name).stem) or f"page-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}"
        named = meta.get("id")
        if named and not PAGE_ID.fullmatch(named):
            return "Page not published: lifehub:id in the file — only a-z, 0-9 and hyphens."
        async with core.queue:  # one writer: her turns and this commit take turns
            await asyncio.to_thread(memory.sync)
            taken = Path(memory.tree) / space / "artifacts"
            path, number = f"{space}/artifacts/{stem}.html", 1
            while (taken / PurePosixPath(path).name).exists():  # a file of that name is never replaced: it may be
                number += 1                                      # hers, and a new file is his alone
                path = f"{space}/artifacts/{stem}-{number}.html"
            if not PAGE_PATH.fullmatch(path):
                return "Page not published: the file name — only a-z, 0-9 and hyphens."
            commit, refusals = await asyncio.to_thread(
                memory.put, path, text.encode("utf-8"), f"The owner via Telegram: page {path}",
                [f"{UPLOADED}: telegram"])
        if refusals:
            return "Page not published: the Base did not take the file — " + "; ".join(refusals) + "."
        self.poke(now)
        page_id = named or PurePosixPath(path).stem
        said = (f"Page in the Base: {path}. In a couple of minutes it opens at {self.url}/{page_id} "
                "(with the VPN), with its scripts — it is yours.")
        if number > 1:
            said += f" A file {stem}.html was already there — this one went beside it under a new name, the old one " \
                    "is untouched."
            if named:
                said += (f" Both have lifehub:id \"{named}\": until one of them goes or changes its id, the page will "
                         "not come out.")
        return said


VIEWS = {"flight": "refine_flights", "stay": "refine_stays", "ground": "refine_ground"}
# Views of a stored search her option is looked for in, in order: the one she was shown (travel-ops' own bars), then
# the bars as loose as they go by price and by another order (a price order thins near-copies out). A view holds at
# most VIEW_LIMIT cards: a search of ~800 flights sorted by departure showed only the earlier day's, so one loose
# view was not enough. A stay with no rating at all is still hidden by any rating bar.
LOOSE = {"flight": {"max_stops": 3, "max_leg_hours": 72}, "stay": {"min_rating": 0, "max_center_km": 100},
         "ground": {}}
LOOKS = {kind: [{}, {**bars, "sort": "price"}, {**bars, "sort": "rating" if kind == "stay" else "departure"}]
         for kind, bars in LOOSE.items()}
VIEW_LIMIT = 100
PHOTOS = 2  # photos of one stay on the page
STAY_KINDS = {"hotel": "hotel", "apartment": "apartment", "room": "room", "house": "house",
              "shared_room": "bed in a shared room", "other": "stay"}
RIDES = {"train": "train", "bus": "bus", "ferry": "ferry", "van": "minibus"}


def offers(kind: str, view: dict):
    """(card, offer) of every price in a view of travel-ops: a flight's fares by group, a stay's rates, a ride's fares."""
    for card in view.get("cards") or []:
        holders = card.get("groups") or [] if kind == "flight" else [card]
        for holder in holders:
            for offer in holder.get("rates" if kind == "stay" else "fares") or []:
                if isinstance(offer, dict):
                    yield card, offer


def named(who: str, sender: str = "") -> str:
    """A person as «Now» and the tracker page name him: her «who» up to its first aside — never an e-mail address; with
    none, the sender's domain, else «sender»."""
    name = chip(who) if who else ""
    if name and name not in NO_NAME and "@" not in name:
        return name
    return domain(sender) or "sender"


def domain(sender: str) -> str:
    """bank@mail.bank.example -> «mail.bank.example»: who sent it, without the mailbox."""
    host = str(sender or "").rpartition("@")[2].strip().lower()
    return host if "@" in str(sender or "") and "." in host else ""


def chip(who: str) -> str:
    """A sender's name short enough for a chip: her «who» — «Google (no-reply@…)», «CarHire Example (…), Maria
    Example» — up to its first aside; the full one stays on the sender's page. Her «who» may be in either language:
    «via» and «через» both open an aside."""
    name = re.split(r" \(|, | via | через | → | — ", who or "", maxsplit=1)[0].strip() or NO_NAME[0]
    return name if len(name) <= CHIP_CHARS else name[:CHIP_CHARS - 1].rstrip() + "…"



def money(value) -> str:
    """{"amount": "497.00", "currency": "EUR"} -> «497 EUR»."""
    if not isinstance(value, dict):
        return ""
    try:
        amount = Decimal(str(value.get("amount")))
        shown = f"{amount:.0f}" if amount == amount.to_integral() else f"{amount:.2f}"
    except InvalidOperation:
        return ""
    return f"{shown} {value.get('currency') or ''}".strip()


def decimal(number) -> str:
    return f"{number:g}"


def plural(n: int, one: str, few: str, many: str | None = None) -> str:
    """The word for `n`: plural(n, "stop", "stops") by the English rule; with three forms (one, few, many), by the
    Russian one — for Russian words, which the Base still has."""
    n = abs(int(n))
    if many is None:
        return one if n == 1 else few
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def clock_of(iso) -> str:
    """A local time as the source gives it (the airport's, the station's): «22.05 10:15»."""
    try:
        return f"{datetime.fromisoformat(str(iso)):%d.%m %H:%M}"
    except ValueError:
        return str(iso)


def duration(minutes) -> str:
    if not isinstance(minutes, int):
        return "?"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours and minutes else f"{hours} h" if hours else f"{minutes} min"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def front_matter(path: Path) -> dict | None:
    """The YAML head of a Record, or None when there is none or it does not parse."""
    try:
        text = path.read_text(errors="replace")
        if not text.startswith("---\n") or (end := text.find("\n---", 4)) < 0:
            return None
        head = yaml.safe_load(text[4:end])
        return head if isinstance(head, dict) else None
    except (OSError, yaml.YAMLError, ValueError):  # ValueError: a date that is no date, e.g. 2026-02-30
        return None


def timeline(options: list[dict]) -> list[dict]:
    """A trip page's options by day — the day a flight or a ride leaves — and within a day by leg and kind; stays,
    which have no day of their own, last. Options are named by their place in `options`."""
    days: dict[str, dict[tuple, list[int]]] = {}
    for number, option in enumerate(options):
        day = option.get("day") if _day(option.get("day")) else ""
        key = (option.get("leg") or KIND_NAMES.get(option.get("kind"), ""), option.get("kind", ""))
        days.setdefault(day, {}).setdefault(key, []).append(number)
    out = []
    for day in sorted(days, key=lambda d: d or "9999"):
        from . import clock

        moment = _day(day)
        label = f"{clock.WEEKDAYS[moment.weekday()]}, {moment.day} {clock.MONTHS[moment.month - 1]}" if moment else \
            "Stays" if all(kind == "stay" for _, kind in days[day]) else "No date"
        out.append({"day": day, "label": label, "legs": [{"leg": leg, "kind": kind, "name": KIND_NAMES.get(kind, ""),
                                                          "options": numbers}
                                                         for (leg, kind), numbers in days[day].items()]})
    return out


def paragraphs(text: str) -> list[str]:
    """Her summary as paragraphs a phone reads: her own line breaks, and a long block cut after every second or
    third sentence."""
    out = []
    for block in (b.strip() for b in str(text).splitlines()):
        if len(block) <= SUMMARY_CHARS:
            out += [block] if block else []
            continue
        chunk = ""
        for sentence in re.split(r"(?<=[.!?])\s+(?=[«\"(A-ZА-ЯЁ0-9])", block):  # her words may be Russian too
            if chunk and len(chunk) + len(sentence) > SUMMARY_CHARS:
                out.append(chunk)
                chunk = ""
            chunk = f"{chunk} {sentence}".strip()
        out += [chunk] if chunk else []
    return out



def host_of(url: str) -> str:
    """The host a page names an address by; "" when the address is not a sound one."""
    url = sound(url)
    return (urlsplit(url).hostname or "").removeprefix("www.") if url else ""


def sound(url: str) -> str:
    """An https address as a page may hold it, or "" — what Hugo's urls.Parse (Go's net/url) takes the same way
    Python does. Go refuses a bad escape (`/%zz`, `/a%2`) and the whole build would stop on one invitation; Python
    reads `https://evil.com\\@zoom.us/j/1` as a zoom.us address while a browser goes to evil.com. So: no backslash,
    no control or space, every % with two hex digits, the netloc nothing but host[:port] — no user, no password —
    and the host lowercased (an upper-case host or an explicit port is normalised, not refused)."""
    url = str(url or "")
    if not url.lower().startswith("https://") or len(url) > URL_CHARS or "\\" in url \
            or re.search(r"[\x00-\x20\x7f]", url) or re.search(r"%(?![0-9A-Fa-f]{2})", url):
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", host) \
            or parts.netloc.lower() not in (host, f"{host}:{port}"):
        return ""
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return parts._replace(scheme="https", netloc=netloc).geturl()


# Health lines other modules write (core.backup, core.health, health.py), read here in either language: an older
# router or a persisted line may still carry the Russian words.
BACKUP = re.compile(r"^(backup|бэкап) — ")
BACKUP_FAILED = ("failed", "не удался")
CONTAINERS = re.compile(r"^(containers|контейнеры): ")
CONTAINERS_OK = re.compile(r"^(containers: all|контейнеры: все)\b")
NO_NAME = ("no name", "без имени")  # chip's word for a sender with none; the Russian one may be in older data


def backed_up(said: str) -> bool:
    """Whether the backup's health line says it went well: «backup — 5 h ago», not a failure."""
    return bool(BACKUP.match(said)) and not any(word in said for word in BACKUP_FAILED)


def level(share: int) -> str:
    """A limit window's share as a colour: ok below 70 %, warn below 90 %, bad from there."""
    return "ok" if share < 70 else "warn" if share < 90 else "bad"



def midnight(day: date, tz: str) -> float:
    """The moment a local day begins in the zone — a day is not always 24 hours long."""
    from . import clock

    return datetime(day.year, day.month, day.day, tzinfo=clock.zone(tz)).timestamp()


def _local_day(ts: float, tz: str) -> str:
    from . import clock

    return clock.local(ts, tz).date().isoformat()


def _instant(value) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _epoch(value) -> int | None:
    try:
        return round(_instant(value)) if value else None
    except ValueError:
        return None


# --- the owner's pages: what the router reads from an HTML file -------------------------------------------------

PAGE_MAX = 3 * 1024 * 1024    # the builder refuses a bigger page too: libraries and fonts are vendored, not in git
TRIP = "trip-"                # the id of a trip page the router renders begins with this; a base file's never does
JPEG = "data:image/jpeg;base64,"  # a stay's photo, inside the page: the page loads nothing from anywhere
PHOTO_TAG = 200               # what one photo's <img> adds to a page besides its data: address
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
PAGE_PATH = re.compile(r"[a-z0-9][a-z0-9/_.-]*\.html")
PAGES_MAX = 200               # pages published at most; the rest named in pages-refused.json, then the health line
SPACE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
NATURES = ("knowledge", "profile", "journal", "artifacts")
UPLOADED = "Retinue-Upload"   # the trailer of a commit the router made from the owner's own file: his, not hers
OPT_IN = '<meta name="lifehub" content="publish">'
WORDS_PER_MIN = 180
DESCRIPTION_CHARS = 300
PARAGRAPH_CHARS = 60          # a shorter first paragraph is a label, not what the page is about
SILENT = {"script", "style", "template", "noscript", "svg", "head", "title"}
# The owner's file names may be Russian: transliterated for a page's address.
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


class Meta(HTMLParser):
    """What the hub lists about a page, from the page itself: the opt-in, the title (the document's, not an SVG's),
    the description (or the first real paragraph — an eyebrow line is not one), `lifehub:id`, the words a reader
    sees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.publish, self.title, self.description, self.id = False, "", "", ""
        self.first, self.words = "", 0
        self.silent, self.in_title, self.titled, self.in_p = 0, False, False, False
        self.paragraph: list[str] = []

    def handle_starttag(self, tag, attrs):
        named = {name: (value or "") for name, value in attrs}
        if tag == "meta":
            key, content = named.get("name", "").strip().lower(), named.get("content", "").strip()
            if key == "lifehub" and content.lower() == "publish":
                self.publish = True
            elif key == "description":
                self.description = content
            elif key == "lifehub:id":
                self.id = content.lower()
        if tag in SILENT:
            self.silent += 1
        self.in_title = self.in_title or (tag == "title" and not self.titled)
        if tag == "p" and not self.first:
            self.in_p, self.paragraph = True, []

    def handle_startendtag(self, tag, attrs):
        if tag != "svg":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in SILENT:
            self.silent = max(0, self.silent - 1)
        if tag == "title" and self.in_title:
            self.in_title, self.titled = False, True
        if tag == "p" and self.in_p:
            said = " ".join("".join(self.paragraph).split())
            self.in_p, self.first = False, said if len(said) >= PARAGRAPH_CHARS else ""

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.silent:
            return
        self.words += len(re.findall(r"\w+", data))
        if self.in_p:
            self.paragraph.append(data)


def page_meta(text: str) -> dict | None:
    """The index entry's fields of a page that opted in, or None."""
    meta = Meta()
    meta.feed(text)
    meta.close()
    if not meta.publish:
        return None
    about = " ".join((meta.description or meta.first).split())
    return {"id": meta.id, "title": " ".join(meta.title.split())[:200],
            "description": about[:DESCRIPTION_CHARS - 1] + "…" if len(about) > DESCRIPTION_CHARS else about,
            "words": meta.words, "reading_min": max(1, -(-meta.words // WORDS_PER_MIN))}


def opted_in(text: str) -> str:
    """The page with the opt-in mark: as it was when it has one; otherwise the mark goes right after <head>, or after
    the doctype when there is no <head>."""
    if page_meta(text) is not None:
        return text
    head = re.search(r"<head(?:\s[^>]*)?>", text, re.I)
    if head:
        return text[:head.end()] + "\n" + OPT_IN + text[head.end():]
    doctype = re.match(r"\s*<!doctype[^>]*>", text, re.I)
    at = doctype.end() if doctype else 0
    return text[:at] + "\n" + OPT_IN + "\n" + text[at:]


def slug(name: str) -> str:
    """A file's name as a page's address: Latin, digits and hyphens. «Шпаргалка Regex» -> «shpargalka-regex»."""
    plain = "".join(TRANSLIT.get(ch, ch) for ch in name.lower())
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:64].rstrip("-")


def git_bytes(memory, *args: str) -> bytes:
    """Git on the router's copy of the base, as `memory` runs it: no hooks, its own config."""
    return subprocess.run([*memory.repo, *args], env=memory.env, check=True, capture_output=True).stdout


def git_out(memory, *args: str) -> str:
    return subprocess.run([*memory.repo, *args], env=memory.env, capture_output=True).stdout.decode("utf-8", "replace")


# --- a trip page as HTML: what Hugo's trip layout drew, now drawn here --------------------------------------------

TRIP_KINDS = (("flight", "Flights"), ("stay", "Stays"), ("ground", "Overland"))
# One stylesheet in the page itself: the page loads nothing, so no font, no icon, no script. Light or dark as the
# system asks; one column a phone reads without zooming.
TRIP_STYLE = """
:root { color-scheme: light dark; --bg: #fcfcfd; --card: #f9f9fb; --line: #d9d9e0; --text: #1c2024; --muted: #60646c;
  --accent: #3e63dd; --accent-text: #3a5bc7; --accent-soft: #edf2fe; --on-accent: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #111113; --card: #18191b; --line: #363a3f; --text: #edeef0; --muted: #b0b4ba; --accent: #3e63dd;
    --accent-text: #9eb1ff; --accent-soft: #182449; }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; background: var(--bg); }
body { margin: 0; background: var(--bg); color: var(--text); font: 17px/1.5 system-ui, -apple-system, "Segoe UI",
  Roboto, sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 4px; }
h2 { font-size: 1.25rem; margin: 32px 0 8px; }
.muted, .leg, small, .foot { color: var(--muted); }
.leg { margin: 16px 0 8px; font-weight: 600; }
.summary p { margin: 0 0 12px; }
.option { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin: 0 0 12px; }
.option.pick { border-color: var(--accent); }
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.what { font-weight: 600; overflow-wrap: anywhere; }
.chip { display: inline-block; font-size: 0.8125rem; padding: 2px 8px; border-radius: 999px; margin-top: 4px;
  border: 1px solid var(--line); overflow-wrap: anywhere; }
.chip.accent { background: var(--accent-soft); color: var(--accent-text); border-color: transparent; }
.price { font-size: 1.25rem; font-weight: 650; text-align: right; white-space: nowrap; }
.price small { display: block; font-size: 0.8125rem; font-weight: 400; }
ul { margin: 8px 0; padding-left: 1.1em; color: var(--muted); }
.note { border-left: 2px solid var(--line); padding-left: 12px; margin: 8px 0; }
.photos { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }
.photos img { width: 100%; height: auto; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 8px; }
.foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 0.8125rem; margin-top: 8px; }
.btn { display: inline-flex; align-items: center; min-height: 44px; padding: 0 16px; border-radius: 10px;
  background: var(--accent); color: var(--on-accent); font-size: 0.9375rem; font-weight: 600; text-decoration: none;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
"""
PHOTO = re.compile(r"data:image/jpeg;base64,[A-Za-z0-9+/]+=*")  # the only image address a trip page holds


def trip_page(trip: dict, hosts: list[str]) -> str:
    """A trip's page from its data (`Pages.render`): the title, the dates, her summary's paragraphs, the options by
    day as the timeline has them (by kind when it has none), each with what it is, the price, the seller, the times,
    the rating, when the price was seen, her note, a «recommended» mark, the photos, and the site as a button showing
    its host. Every value goes through `html.escape`: what she or a site wrote is text, never markup. No script, no
    event handler, nothing loaded from anywhere; a link is a button only when it is a sound https address on one of
    travel-ops' sites (`hosts`)."""
    from html import escape

    options = [option for option in trip.get("options") or [] if isinstance(option, dict)]
    title = escape(str(trip.get("title") or ""))
    out = ["<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">', OPT_IN,
           f'<meta name="description" content="{escape(" ".join(str(trip.get("summary") or "").split()))}">',
           f"<title>{title}</title>", f"<style>{TRIP_STYLE}</style>", "</head>", "<body>", "<main>",
           f"<h1>{title}</h1>"]
    when = f"{escape(str(trip.get('start') or ''))} — {escape(str(trip.get('end') or ''))}"
    if trip.get("published"):
        when += f" · published {escape(str(trip['published']))}"
    out.append(f'<p class="muted">{when}</p>')
    said = trip.get("paragraphs") or str(trip.get("summary") or "").splitlines()
    out.append('<div class="summary">' + "".join(f"<p>{escape(str(p))}</p>" for p in said if str(p).strip())
               + "</div>")
    if trip.get("timeline"):
        for day in trip["timeline"]:
            out.append(f"<h2>{escape(str(day.get('label') or ''))}</h2>")
            for leg in day.get("legs") or []:
                if leg.get("kind") != "stay":
                    name = escape(str(leg.get("name") or ""))
                    out.append(f'<p class="leg">{name}' + (f" · {escape(str(leg['leg']))}" if leg.get("leg") else "")
                               + "</p>")
                out += [option_card(options[n], hosts) for n in leg.get("options") or []
                        if isinstance(n, int) and 0 <= n < len(options)]
    else:
        for kind, name in TRIP_KINDS:
            these = [option for option in options if option.get("kind") == kind]
            if these:
                out.append(f"<h2>{name}</h2>")
                out += [option_card(option, hosts) for option in these]
    out += ["</main>", "</body>", "</html>", ""]
    return "\n".join(out)


def option_card(option: dict, hosts: list[str]) -> str:
    """One option of a trip page: what, the price in the converted currency first (the seller's under it), the
    facts code took from travel-ops, her note, the photos, how old the price is, and the site as a button."""
    from html import escape

    pick = option.get("pick") is True
    price, converted = str(option.get("price") or ""), str(option.get("converted") or "")
    out = [f'<article class="option{" pick" if pick else ""}">', '<div class="head">',
           f'<div><div class="what">{escape(str(option.get("what") or ""))}</div>'
           + ('<span class="chip accent">recommended</span>' if pick else "") + "</div>",
           '<div class="price">' + escape(converted or price) + (f"<small>{escape(price)}</small>" if converted else "")
           + (f"<small>{escape(str(option['seller']))}</small>" if option.get("seller") else "") + "</div>", "</div>"]
    lines = [str(line) for line in option.get("lines") or [] if str(line).strip()]
    if lines:
        out.append("<ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>")
    if option.get("note"):
        out.append(f'<p class="note">{escape(str(option["note"]))}</p>')
    photos = [photo for photo in option.get("photos") or [] if PHOTO.fullmatch(str(photo))]
    if photos:
        out.append('<div class="photos">' + "".join(f'<img src="{photo}" alt="photo of the option">' for photo in photos)
                   + "</div>")
    out.append(f'<div class="foot"><span>{escape(str(option.get("seen") or ""))}: search again before buying</span>'
               + link_button(str(option.get("link") or ""), hosts) + "</div>")
    out.append("</article>")
    return "".join(out)


def link_button(url: str, hosts: list[str]) -> str:
    """The option's site: a button with its host when the address is a sound https one on a travel-ops site, the
    host as plain text when it is only sound, nothing when it is not even that. Never the address as text."""
    from html import escape

    host = host_of(url)
    if host and linkable(url, hosts):
        return f'<a class="btn" href="{escape(sound(url))}" rel="noopener noreferrer">{escape(host)}</a>'
    return f'<span class="chip">{escape(host)}</span>' if host else ""
