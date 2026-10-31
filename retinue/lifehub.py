"""The life hub (`lifehub`): pages about the owner's life on his own server, behind his VPN, for his devices only.
In Retinue "hub" already names the knowledge base's git hub, so in code the site is the lifehub.

The router writes data here and nothing else: JSON checked by a schema, pictures drawn or re-encoded by code. The
pages are built from it by Hugo in a container of its own with no network (deploy/lifehub); the model never writes
HTML, and nothing it sends is markup — Hugo escapes every value. A page is a view of the archive, the base and the
router's tables, not a third store: its numbers are counted by code.
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
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from .render import linkable
from .travel import Refused as TravelRefused, Unavailable as TravelUnavailable

log = logging.getLogger("retinue.lifehub")

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


# --- what the router writes: the status page ----------------------------------------------------------------------

DAY = 86400
WEEK = 7 * DAY
# How a model run is named on the status page. A kind not here is shown as it is.
RUN_KINDS = {"conversation": "conversation", "retry": "reply after the limit", "summary": "morning brief",
             "compact": "compaction", "reminder": "reminder", "price": "price watch", "triage": "letter triage",
             "mail": "mail judgement", "weekly": "weekly review", "initiative": "own thought"}
TOKENS = ("input", "output", "cache: read", "cache: write")
RUN_STATES = {"done": "ok", "error": "failure", "failed": "failure", "rejected": "refused", "limit": "hit the limit"}
WINDOW_NAMES = {"five_hour": "5 hours", "seven_day": "week"}
REFRESH_S = 300           # the pages' data is written this often
AFTER_RUN_S = 60          # and this long after a model run ended: what she just did shows on the status page
DEADLINE_PAST_DAYS = 30   # a deadline that passed this long ago is history, not a deadline
DEADLINES_DOCUMENTS = 8   # coming deadlines of documents (`until`) on the «now» page, besides the overdue ones
DEADLINES_RECHECKS = 8    # and of facts to check again (`stale_after`)
EVENING = 18              # from this local hour the day's events are «Evening»
BLOCK_HOURS = 4           # an own recurring event this long, alone and nowhere, is a busy block («Block»), not a meeting
WAITING_ITEMS = 200       # items that wait for the owner, read for the «now» page at most
WAITING_SHOWN = 5         # and the newest of them under each person
# What the owner is asked to do, the most pressing first; «read» is only to know.
URGENCY = {"reply": 0, "decide": 1, "pay": 2, "attend": 3, "act": 4, "read": 5}
URL = re.compile(r"https?://[^\s<>\"'`]+")
LINKS = 4                 # addresses taken out of one note at most
URL_CHARS = 2000          # an address longer than this is not made a link
# Meeting services: an event's address there is a «join» button showing the host. Matched by the host's end.
MEETINGS = (("zoom.us", "zoom"), ("zoom.com", "zoom"), ("meet.google.com", "meet"), ("teams.microsoft.com", "teams"),
            ("teams.live.com", "teams"), ("whereby.com", "online"), ("meet.jit.si", "online"), ("webex.com", "online"))
# A letter from a robot of a service — a CRM, a shop — is a notice, not a letter that waits for his answer.
NOTICE = re.compile(r"^(no-?reply|notifications?|notify|alerts?|mailer-daemon|do-?not-?reply|robot|bounces?)[@+.-]",
                    re.IGNORECASE)
PARAGRAPH_CHARS = 280     # a summary's paragraph is cut after a sentence past this
KIND_NAMES = {"flight": "Flight", "stay": "Stay", "ground": "Overland"}
NEEDS = {"read": "read", "reply": "reply", "decide": "decide", "attend": "attend", "pay": "pay", "act": "do"}
REVIEW_DAYS = 7           # the review page: her judgements this many days back
REVIEW_ITEMS = 400        # at most this many of them
REVIEW_DROPPED = 300      # and this many lines of the dropped log
REVIEW_PAGE = 25          # cards on one page of the review
REVIEW_DROPPED_PAGE = 50  # lines on one page of the dropped
REVIEW_SENDERS = 40       # senders with a page of their own, the most frequent first
CHIP_CHARS = 28           # a sender's name on a chip: her «who» up to its first aside, then cut
SOURCES = {"telegram": "Telegram", "mail": "mail", "calendar": "calendar"}
REVIEW_MARKS = {"told": "told you at once", "held": "at once, in one message", "later": "in the morning brief",
                "done": "was in the morning brief", "silent": "silently", "waiting": "awaiting judgement"}
DECISIONS = {"told": "told you at once", "held": "at once, in one message with the others from them",
             "later": "in the morning brief", "done": "was in the morning brief", "silent": "silently",
             "kept": "awaiting judgement", "new": "awaiting judgement"}
DRAFTS = {"shown": "draft waits for your button", "hidden": "draft waits for your button", "fixing": "draft being edited",
          "queued": "draft being sent", "sending": "draft being sent", "sent": "draft sent",
          "stale": "draft expired: a new message came", "expired": "draft expired with time",
          "dropped": "you said not to reply", "failed": "draft not sent", "unknown": "draft: whether it went is unknown",
          "link": "draft: send it yourself by the link"}
MAIL_DAYS = 7             # mail that needs the owner, judged this many days back
TRACKER_DAYS = 14         # the tracker page: its meetings this many days ahead
TRACKER_MAIL_DAYS = 30    # and the private matter's letters waiting for his answer, this many days back
TRACKER_NOTES = 300       # a tracker row's notes, cut to this on the page
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


def grouped(number: int) -> str:
    """7785378 -> «7 785 378»: a page shows numbers as text, so that no template formats them."""
    return f"{number:,}".replace(",", " ")


class Lifehub:
    """The router's side of the life hub: what it writes to the data folder and when. Attached to the core like the
    mailroom; reads the router's own tables, the archive and the base, and never calls a model."""

    def __init__(self, data: Data, url: str, link_hosts: list[str], kb: str = "", build_status: str = "") -> None:
        self.data = data
        self.pages_head = ""                  # the hub's commit the pages were last read from
        self.build_status = build_status      # build.json the builder writes after every build; "" — not known
        self.url = url.rstrip("/")            # the site's address as the owner's devices open it
        self.link_hosts = list(link_hosts)    # travel-ops' sites: a link there is clickable on a page too
        self.kb = kb                          # the knowledge base's files: deadlines are read there; "" — none
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
        """Every page's data, each part on its own: a part that fails is logged, the others are written. The base is
        brought to the hub's first when no run is going: what the Mac pushed shows without waiting for her next run."""
        core = self.core
        if core.memory and not core.queue.locked():
            async with core.queue:
                await core._fresh_base()
        for name, part in (("site", self.write_site), ("status", self.write_status), ("now", self.write_now),
                           ("review", self.write_review), ("tracker", self.write_tracker), ("pages", self.write_pages)):
            try:
                result = part(now)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lifehub: %s not written", name)

    def write_site(self, now: float) -> None:
        """What every page needs: the address, and the hosts a link may lead to (the template checks them again)."""
        hosts = list(dict.fromkeys(entry.partition("/")[0].lower() for entry in self.link_hosts))
        self.data.write("site.json", {"url": self.url, "link_hosts": hosts})

    # --- the owner's zone ----------------------------------------------------------------------------------------

    @property
    def tz(self) -> str:
        """The owner's effective time zone: every time on every page is local there. The one place the hub reads
        it. `core.tz` is the zone in force — router.yaml's `owner_tz` at first, then whatever the owner set in words
        (`set_timezone`, kept in the router's state and put back into `core.tz` on start: retinue/proactive.py)."""
        return self.core.tz

    # --- the «now» page ------------------------------------------------------------------------------------------

    async def now(self, now: float) -> dict:
        """The «now» page: what is next with a countdown, who waits for an
        answer (by person, oldest first within each), the rest of today, the evening and tomorrow as a timeline in
        the owner's local time, deadlines overdue and coming, upcoming trips and a row of status chips. Data only:
        local times, titles, the kind of place and a meeting's host — never a meeting's address in text, its
        password or anybody's e-mail."""
        from . import clock

        core = self.core
        today = clock.local(now, self.tz).date()
        trips = []
        for name in self.data.names("trips"):
            trip = self.data.read(name)
            if isinstance(trip, dict) and str(trip.get("end", "")) >= today.isoformat():
                trips.append({"title": trip.get("title", ""), "start": trip.get("start", ""),
                              "end": trip.get("end", ""), "url": f"/trips/{trip.get('id')}/",
                              "days": (_day(trip.get("start")) - today).days if _day(trip.get("start")) else None})
        page = {"updated": self.stamp(now), "ts": round(now), "at": f"{clock.local(now, self.tz):%H:%M}",
                "date": today.isoformat(),
                "zone": clock.label(self.tz), "trips": sorted(trips, key=lambda t: t["start"]),
                "next": None, "today": [], "evening": [], "tomorrow": [], "blocks": [], "calendar": "",
                "reminders": [self.reminder(job, now) for job in core.jobs.reminders(10)],
                "mail": self.waiting(now), "deadlines": self.deadlines(today), "chips": self.chips(now),
                "health": health_words(core.health(now))}
        if core.mail and core.mail.collector is not None:
            try:
                start = midnight(today, self.tz)
                events, failed = await core.mail.collector.agenda(start, midnight(today + timedelta(days=2), self.tz))
                page.update(self.timeline(events, now))
                page["calendar"] = "; ".join(f"not read: {f}" for f in failed)
            except Exception:
                log.exception("lifehub: the agenda was not read")
                page["calendar"] = "calendar not read: the collector is unavailable"
        elif not core.mail:
            page["calendar"] = "calendar not connected"
        return page

    async def write_now(self, now: float) -> None:
        self.data.write("now.json", await self.now(now))

    def timeline(self, events: list[dict], now: float) -> dict:
        """Today, the evening and tomorrow from the calendars, by the moment each begins in the owner's zone. What
        ended is gone; the owner's own recurring blocks with nobody else and nowhere to be — «Block» — are
        named once in `blocks`, not shown as meetings; `next` is the nearest meeting, going on or ahead."""
        from . import clock

        today = clock.local(now, self.tz).date()
        start = midnight(today, self.tz)
        out = {"today": [], "evening": [], "tomorrow": [], "blocks": [], "next": None}
        for event in sorted((e for e in map(lambda e: self.event(e), events) if e), key=lambda e: e["start"]):
            if (not event["all_day"] and event["end"] <= now) or event["end"] <= start:
                continue
            day = max(date.fromisoformat(event["day"]), today)  # begun before today and still on: today's
            if event["block"]:
                if day == today and not any(b["title"] == event["title"] for b in out["blocks"]):
                    out["blocks"].append({"title": event["title"], "time": event["time"], "until": event["until"]})
                continue
            if day == today:
                begins = clock.local(event["start"], self.tz)
                late = not event["all_day"] and begins.date() == today and begins.hour >= EVENING
                out["evening" if late else "today"].append(event)
            elif day == today + timedelta(days=1):
                out["tomorrow"].append(event)
            else:
                continue
            if out["next"] is None and not event["all_day"]:
                out["next"] = {**event, "going": event["start"] <= now}
        return out

    def event(self, event: dict) -> dict | None:
        """One calendar event as the page shows it: local time, title, the kind of place, a meeting's host and the
        address to join it (a link only to a meeting service, shown as its host). No e-mail of anybody."""
        from . import clock

        if str(event.get("status") or "") == "cancelled":
            return None
        begin, finish = event.get("start") or {}, event.get("end") or {}
        try:
            if begin.get("dateTime"):
                start = _instant(begin["dateTime"])
                end = _instant(finish["dateTime"]) if finish.get("dateTime") else start
                all_day, day = False, clock.local(start, self.tz).date()
            elif begin.get("date"):
                day = date.fromisoformat(str(begin["date"]))
                start = midnight(day, self.tz)
                end = midnight(_day(finish.get("date")) or day + timedelta(days=1), self.tz)
                all_day = True
            else:
                return None
        except (TypeError, ValueError):
            return None
        title = str(event.get("summary") or "").strip() or "(no title)"
        place = places(str(event.get("location") or ""), str(event.get("description") or ""))
        alone = int(event.get("attendees") or 0) <= 1
        block = (not all_day and bool(event.get("recurring")) and alone and not place["place"]
                 and (event.get("mine") or not event.get("organizer")) and end - start >= BLOCK_HOURS * 3600)
        return {"title": title[:200], "start": start, "end": end, "all_day": all_day, "day": day.isoformat(),
                "time": "" if all_day else f"{clock.local(start, self.tz):%H:%M}",
                "until": "" if all_day else f"{clock.local(end, self.tz):%H:%M}",
                "minutes": round((end - start) / 60), **place, "block": block,
                "invited": not event.get("mine") and bool(event.get("organizer")),
                "unanswered": not event.get("mine") and event.get("answer") == "needsAction"
                and not event.get("recurring")}

    def reminder(self, job, now: float) -> dict:
        from . import clock

        moment = clock.local(job.due, self.tz)
        return {"ts": round(job.due), "time": f"{moment:%H:%M}", "date": moment.date().isoformat(),
                "day": f"{clock.SHORT[moment.weekday()]} {moment.day} {clock.MONTHS[moment.month - 1]}",
                "text": str(job.text)}

    def waiting(self, now: float) -> dict:
        """What waits for the owner — letters and chats she judged ask something of him — by person: one group per
        sender with how many, the most pressing ask, how long the oldest has waited and the newest words. What only
        asks to be read comes after what asks to act. The private matter's letters are one group that names nothing
        and points at the tracker page."""
        from . import clock
        from .mail import PRIVATE

        out = {"count": 0, "act": 0, "groups": []}
        if not self.core.mail:
            return out
        groups: dict[str, dict] = {}
        for item in self.core.mail.store.needing(now - MAIL_DAYS * DAY, WAITING_ITEMS):
            card, data = item["card"] or {}, item["data"] or {}
            sender = str(item["sender"] or "")
            chat = item["source"] == "telegram" or sender.startswith("tg:")
            private = item["kind"] == PRIVATE or bool(card.get(PRIVATE))
            key = PRIVATE if private else sender or str(card.get("who") or "?")
            who = "Private" if private else named(str(data.get("name") or "") if chat else
                                                     str(card.get("who") or ""), "" if chat else sender)
            ts = float(item["ts"] or item["at"] or now)
            need = str(card.get("needs_now") or "read")
            group = groups.setdefault(key, {
                "key": hashlib.sha256(key.encode()).hexdigest()[:10], "who": who, "private": private,
                "where": "mail" if private else SOURCES["telegram" if chat else "mail"],
                "source": "mail" if private else "telegram" if chat else "mail",
                "action": need, "needs": "", "count": 0, "first": ts, "last": ts, "deadline": "", "items": []})
            group["count"] += 1
            group["first"], group["last"] = min(group["first"], ts), max(group["last"], ts)
            if URGENCY.get(need, 9) < URGENCY.get(group["action"], 9):
                group["action"] = need
            group["deadline"] = group["deadline"] or str(card.get("deadline_now") or "")
            if not private:
                group["items"].append({"text": str(card.get("text") or card.get("summary") or ""), "ts": round(ts),
                                       "needs": NEEDS.get(need, ""), "deadline": str(card.get("deadline_now") or ""),
                                       "age": clock.ago(now - ts)})
        for group in groups.values():
            group["needs"] = NEEDS.get(group["action"], "")
            group["age"] = clock.ago(now - group["first"])
            group["text"] = (f"{group['count']} {plural(group['count'], 'letter', 'letters')} about the private "
                             "matter — on the tracker page" if group["private"] else group["items"][-1]["text"])
            group["items"] = group["items"][::-1][:WAITING_SHOWN]
            group["first"], group["last"] = round(group["first"]), round(group["last"])
        ordered = sorted(groups.values(), key=lambda g: (URGENCY.get(g["action"], 9) >= URGENCY["read"],
                                                         URGENCY.get(g["action"], 9), -g["last"]))
        out.update(count=sum(g["count"] for g in ordered), groups=ordered,
                   act=sum(1 for g in ordered if URGENCY.get(g["action"], 9) < URGENCY["read"]))
        return out

    def chips(self, now: float) -> list[dict]:
        """The row of status chips on «now»: whether anything is broken, the backup, the limit, the hub's build —
        each `ok`, `warn` or `bad`, the details on «Status»."""
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

    # --- the «review» page: what she decided ----------------------------------------------------------------------

    def review(self, now: float) -> dict:
        """A week of her judgements, newest first, sliced by source, by sender and by decision, page by page: where
        from, from whom, her words, where code sent it and what became of a draft; the dropped from the log. The site runs no script, so every slice —
        all, by source, by decision, by sender, the dropped — is its own set of pages of REVIEW_PAGE, cut here."""
        import hashlib

        from . import clock
        from .mail import KINDS

        page = {"updated": self.stamp(now), "days": REVIEW_DAYS, "views": [], "dropped": 0,
                "facets": {"sources": [], "decisions": [], "senders": []}}
        if not self.core.mail:
            page["views"] = cut("all", "All", {}, [])
            return page
        store, since = self.core.mail.store, now - REVIEW_DAYS * DAY
        drafts = self.core.outbox.store.by_reply() if self.core.outbox else {}
        items = []
        for item in store.review(since, REVIEW_ITEMS):
            card, data = item["card"] or {}, item["data"] or {}
            chat = item["source"] == "telegram"
            who = str(data.get("name") or "") if chat else str(card.get("who") or item["sender"] or "")
            moment = clock.local(item["ts"], self.tz)
            mark = "waiting" if item["state"] in ("kept", "new") else item["state"]
            items.append({
                "when": self.stamp(item["ts"]), "time": f"{moment:%H:%M}",
                "day": f"{clock.WEEKDAYS[moment.weekday()]}, {moment.day} {clock.MONTHS[moment.month - 1]}",
                "source": item["source"], "where": SOURCES.get(item["source"], item["source"]),
                "who": who, "who_key": "who-" + hashlib.sha256(str(item["sender"] or who).encode()).hexdigest()[:10],
                "from": "" if chat else str(item["sender"] or ""),
                "text": str(card.get("text") or card.get("summary") or ""),
                "mark": mark, "decision": DECISIONS.get(item["state"], item["state"]),
                "needs": NEEDS.get(card.get("needs_now"), ""), "deadline": str(card.get("deadline_now") or ""),
                "draft": DRAFTS.get(drafts.get(item["event_id"] or ""), "")})
        facets = page["facets"]
        page["views"] += cut("all", "All", {}, items)
        for key, name in SOURCES.items():
            these = [i for i in items if i["source"] == key]
            if these:
                facets["sources"].append({"key": key, "name": name, "count": len(these), "path": f"src-{key}"})
                page["views"] += cut(f"src-{key}", name, {"source": key}, these)
        for key, name in REVIEW_MARKS.items():
            these = [i for i in items if i["mark"] == key]
            if these:
                facets["decisions"].append({"key": key, "name": name, "count": len(these), "path": f"dec-{key}"})
                page["views"] += cut(f"dec-{key}", name, {"decision": key}, these)
        senders: dict[str, list] = {}
        for item in items:
            senders.setdefault(item["who_key"], []).append(item)
        for key, these in sorted(senders.items(), key=lambda kv: -len(kv[1]))[:REVIEW_SENDERS]:
            facets["senders"].append({"key": key, "name": chip(these[0]["who"]), "where": these[0]["where"],
                                      "count": len(these), "path": key})
            page["views"] += cut(key, these[0]["who"] or "no name", {"who": key}, these)
        rows = [{"when": self.stamp(row["ts"]), "from": row["sender"], "subject": row["subject"],
                 "kind": "your rule" if row["why"] == "blocked" else KINDS.get(row["kind"], row["kind"])}
                for row in store.dropped_since(since, REVIEW_DROPPED)]
        page["dropped"], page["dropped_shown"] = store.dropped_count(since), len(rows)
        page["views"] += cut("dropped", "Dropped", {}, rows, key="rows", size=REVIEW_DROPPED_PAGE)
        return page

    def write_review(self, now: float) -> None:
        self.data.write("review.json", self.review(now))

    # --- the tracker page -----------------------------------------------------------------------------------------

    async def tracker(self, now: float) -> dict:
        """The tracker page: the funnel, the rows of the tracker in the base (`Tracker`, the owner's config), its
        meetings in his calendars two weeks ahead, the private matter's letters he has not answered. Read only:
        statuses change in the tracker — by the owner on his computer or by her from a letter."""
        from . import clock
        from .mail import PRIVATE, when

        tracker = Tracker.of(self.core.memory)
        path = Path(self.kb) / tracker.path if self.kb else None
        rows = tracker.rows(path.read_text(encoding="utf-8")) if path and path.is_file() else []
        page = {"updated": self.stamp(now), "title": tracker.title, "path": tracker.path,
                "tracker": bool(rows) or bool(path and path.is_file()),
                "funnel": [{"key": key, "state": label, "count": n,
                            "tone": next((g for g, _, states in tracker.groups if key in states), "other")}
                           for key, label in tracker.states.items() if (n := sum(1 for r in rows if r["key"] == key))],
                "rows": rows, "meetings": [], "letters": [], "notices": []}
        groups = (*tracker.groups, ("other", "Other", ()))
        page["groups"] = [{"key": key, "title": title, "rows": these} for key, title, states in groups
                          if (these := sorted((r for r in rows if r["key"] in states or (not states and not any(
                              r["key"] in s for _, _, s in tracker.groups))), key=lambda r: -score(r["score"])
                              if key == "next" else 0))]
        core = self.core
        if core.mail and core.mail.collector is not None:
            try:
                events, _ = await core.mail.collector.agenda(now, now + TRACKER_DAYS * DAY)
            except Exception:
                log.exception("lifehub: the calendar for the tracker page was not read")
                events = []
            names = [r["item"].casefold() for r in rows if len(r["item"]) >= 3 and r["item"] != "?"]
            for event in events:
                title = str(event.get("summary") or "")
                about = f"{title} {event.get('organizer') or ''}".casefold()
                if any(word in about for word in tracker.meetings) or any(name in about for name in names):
                    shown = self.event(event) or {}
                    page["meetings"].append({"when": when(event, self.tz), "title": title[:200],
                                             "start": shown.get("start"), "time": shown.get("time", ""),
                                             "date": shown.get("day", ""), "place": shown.get("place", ""),
                                             "where": shown.get("where", ""), "join": shown.get("join", ""),
                                             "join_host": shown.get("join_host", "")})
        if core.mail:
            for item in core.mail.store.needing(now - TRACKER_MAIL_DAYS * DAY, WAITING_ITEMS):
                card = item["card"] or {}
                if not (item["kind"] == PRIVATE or card.get(PRIVATE)) or self.answered(item):
                    continue
                sender, ts = str(item["sender"] or ""), float(item["ts"] or item["at"] or 0)
                text, links = unlinked(str(card.get("text") or card.get("summary") or ""))
                page["notices" if NOTICE.match(sender) else "letters"].append({
                    "when": self.stamp(ts), "ts": round(ts), "age": clock.ago(now - ts), "from": sender,
                    "who": named(str(card.get("who") or ""), sender), "domain": domain(sender),
                    "text": text, "links": links, "needs": NEEDS.get(card.get("needs_now"), "")})
        return page

    async def write_tracker(self, now: float) -> None:
        self.data.write("tracker.json", await self.tracker(now))
        self.data.remove("work.json")  # the page's data under its former name

    def answered(self, item: dict) -> bool:
        """Has the owner written in this letter's thread after it — from Gmail himself or by a card?"""
        event = self.core.archive.get(item["event_id"] or "")
        thread = event.meta.get("thread") if event else ""
        if not thread:
            return False
        return self.core.archive.db.execute(
            "SELECT 1 FROM events WHERE kind = 'mail' AND ts > ? AND json_extract(meta, '$.direction') = 'out'"
            " AND json_extract(meta, '$.thread') = ? LIMIT 1", (event.ts, thread)).fetchone() is not None

    def deadlines(self, today: date) -> list[dict]:
        """Dates from the base's Records about the world and about the owner: `until` — a document or a status runs
        out (`document`) — and `stale_after` — a fact to check again (`recheck`). Each kind has its own limit, so
        that twenty re-checks do not push a passport off the page; what is overdue is marked and always kept.
        Superseded knowledge and long past dates are left out."""
        if not self.kb or not os.path.isdir(self.kb):
            return []
        horizon, found = today - timedelta(days=DEADLINE_PAST_DAYS), []
        root = Path(self.kb)
        for path in sorted(root.glob("*/*/**/*.md")):
            relative = path.relative_to(root)
            if relative.parts[1] not in ("profile", "knowledge") or any(p.startswith(".") for p in relative.parts):
                continue
            head = front_matter(path)
            if not head or head.get("status") == "superseded":
                continue
            for field, kind, what in (("until", "document", "valid until"), ("stale_after", "recheck", "recheck")):
                value = head.get(field)
                when = value if isinstance(value, date) else _day(value)
                if when and when >= horizon:
                    found.append({"title": str(head.get("title") or relative.stem)[:120], "date": when.isoformat(),
                                  "kind": kind, "what": what, "overdue": when < today, "days": (when - today).days,
                                  "path": relative.as_posix()})
        found.sort(key=lambda d: d["date"])
        kept = []
        for kind, cap in (("document", DEADLINES_DOCUMENTS), ("recheck", DEADLINES_RECHECKS)):
            these = [d for d in found if d["kind"] == kind]
            kept += [d for d in these if d["overdue"]] + [d for d in these if not d["overdue"]][:cap]
        return sorted(kept, key=lambda d: d["date"])

    def stamp(self, ts: float) -> str:
        from . import clock

        return clock.stamp(ts, self.tz)

    def status(self, now: float) -> dict:
        """The status page: counted by code from the router's tables."""
        from . import charts, clock
        from .archive import ASSISTANT, OWNER, SYSTEM
        from .mail import KINDS

        core = self.core
        runs = core.store.runs_since(now - DAY)
        tally = core.mail.store.tally(now - DAY) if core.mail else {"kept": {}, "dropped": {}, "blocked": {}}
        windows = core.store.windows()
        status = {
            "updated": self.stamp(now),
            "day": {"owner": core.archive.count(OWNER, now - DAY), "answers": core.archive.count(ASSISTANT, now - DAY),
                    "notices": core.archive.count(SYSTEM, now - DAY),
                    "runs": [[RUN_STATES.get(state, state), n] for state, n in sorted(runs.items(), key=lambda x: -x[1])],
                    "reminders": core.jobs.fired_since(now - DAY),
                    "letters": {"kept": sum(tally["kept"].values()),
                                "dropped": sum(tally["dropped"].values()) + sum(tally["blocked"].values())}},
            "limit": [{"window": WINDOW_NAMES[name], "share": round(w["utilization"] * 100),
                       "level": level(round(w["utilization"] * 100)),
                       "resets": self.stamp(w["resets_at"]) if w["resets_at"] else "",
                       "resets_ts": round(w["resets_at"]) if w["resets_at"] else None,
                       "resets_in": clock.ago(w["resets_at"] - now) if w["resets_at"] and w["resets_at"] > now else "",
                       "seen": self.stamp(w["seen"])} for name, w in windows.items()],
            "tokens": [{**row, "name": RUN_KINDS.get(row["kind"], row["kind"]),
                        "cells": [RUN_KINDS.get(row["kind"], row["kind"]), *(grouped(row[k]) for k in (
                            "runs", "input", "output", "cache_read", "cache_write")), f"{row['cost_usd']:.2f}"]}
                       for row in core.store.tokens(now - WEEK)],
            "connectors": (connectors := self.connectors(now)),
            "problems": [c for c in connectors if not c["ok"]],
            "build": self.built(now),
            "sent": [[name, core.outbox.store.sent(now - DAY, channel), core.outbox.store.sent(now - WEEK, channel)]
                     for channel, name in (("mail", "mail"), ("telegram", "Telegram"))] if core.outbox else None,
            "mail": None,
            "ts": round(now),
        }
        status["charts"] = [
            charts.bars("Tokens over 7 days by kind of run", list(TOKENS),
                        [(row["name"], [row["input"], row["output"], row["cache_read"], row["cache_write"]])
                         for row in status["tokens"]]),
            charts.bars("Model runs by day", ["ok", "failures"], self.runs_by_day(now), colours=[1, 4])]
        if core.mail:
            def named(counts: dict) -> list:
                return [[KINDS.get(k, k), n] for k, n in sorted(counts.items(), key=lambda x: -x[1])]

            week = core.mail.store.tally(now - WEEK)
            status["mail"] = {
                "day": {"kept": named(tally["kept"]), "dropped": named(tally["dropped"]),
                        "blocked": [[s, n] for s, n in tally["blocked"].items()]},
                "week": {"kept": named(week["kept"]), "dropped": named(week["dropped"]),
                         "blocked": [[s, n] for s, n in week["blocked"].items()]},
                "rules": [f"drop {sender}" if rule == "block" else
                          f"do not notify at once: {sender}, {KINDS.get(kind, kind)}"
                          for rule, sender, kind in core.mail.store.rules()],
            }
        return status

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
            out.append({"name": "hub", "ok": built["ok"], "state": built["said"].removeprefix("hub ")})
        if getattr(core, "watch", None):
            said = core.watch.health(now)
            out.append({"name": "containers", "ok": bool(CONTAINERS_OK.match(said)),
                        "state": CONTAINERS.sub("", said, count=1)})
        return out

    def health(self, now: float) -> str:
        """The hub's part of the health line, from the builder's build.json: built when, or why not."""
        built = self.built(now)
        return built["said"] if built else ""

    def built(self, now: float) -> dict | None:
        """How the last build went, from build.json the builder writes: ok, when, how long ago, the error, the
        release, and the words for the health line. None when the hub is not set up here."""
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
            return {**out, "said": "hub not built yet"}
        except (OSError, ValueError):
            return {**out, "said": "hub status unreadable"}
        at = float(built.get("at") or 0)
        out.update(at=round(at), ago=clock.ago(now - at), release=str(built.get("release") or ""))
        if not built.get("ok"):
            out["error"] = str(built.get("error") or "no reason recorded").rstrip(". ")
            return {**out, "said": f"hub not updated: {out['error']}"}
        # Pages the build left out: their ids and the check, in the builder's fixed words — never a page's text.
        out["refused"] = [f"{page.get('id')} ({page.get('why')})" for page in built.get("refused") or []
                          if isinstance(page, dict)]
        return {**out, "ok": True, "said": f"hub built {out['ago']} ago" + (
            f"; pages not published: {', '.join(out['refused'][:3])}" if out["refused"] else "")}

    def runs_by_day(self, now: float) -> list[tuple[str, list[int]]]:
        """Model runs of the last seven days by the owner's local date: good ones and failures."""
        from . import clock

        days = []
        for back in range(6, -1, -1):
            moment = clock.local(now - back * DAY, self.tz)
            days.append((moment.date(), f"{clock.SHORT[moment.weekday()]} {moment.day}.{moment.month:02d}"))
        counts = {day: [0, 0] for day, _ in days}
        for ts, state in self.core.store.run_times(now - WEEK):
            day = clock.local(ts, self.tz).date()
            if day in counts:
                counts[day][0 if state in ("done", "limit") else 1] += 1
        return [(label, counts[day]) for day, label in days]

    def write_status(self, now: float) -> None:
        self.data.write("status.json", self.status(now))

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
        self.data.write(f"trips/{page_id}.json", {
            "id": page_id, "title": trip["title"], "summary": trip["summary"], "paragraphs": paragraphs(trip["summary"]),
            "start": trip["start"], "end": trip["end"], "published": self.stamp(now), "published_ts": round(now),
            "options": options, "timeline": timeline(options)})
        self.poke(now)
        if any(option.get("photo_from") for option in options):  # the page goes up now, its photos a little later
            self.pending.append(page_id)
        url = f"{self.url}/trips/{page_id}/"
        return True, (f"Published: {url} (trip_id {page_id} — to update this same page). At the end of your reply to "
                      f"the owner: [details]({url})")

    async def photos(self, page_id: str, travel, now: float) -> None:
        """Photos of the page's stays: travel-ops fetches them from the sites (`stay_photos`; the router has no way
        there, and a page may not load them from there), the router's file worker re-encodes each as a JPEG, and what
        does not decode is dropped. A look that fails leaves the page as it was."""
        from .attachments import Unreadable, Upload, isolated

        name = f"trips/{page_id}.json"
        page = self.data.read(name) or {}
        found: dict[str, list[str]] = {}
        for number, option in enumerate(page.get("options") or [], 1):
            source = option.get("photo_from") or {}
            if not source.get("stay"):
                continue
            try:
                result = await travel.result("stay_photos", {"search_id": source["search_id"],
                                                             "stays": [source["stay"]], "per_stay": PHOTOS})
            except (TravelRefused, TravelUnavailable) as exc:
                log.warning("lifehub: photos of %s not fetched: %s", page_id, exc)
                continue
            names = []
            for block in result.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "image" or len(names) >= PHOTOS:
                    continue
                try:
                    done = await isolated(Upload("photo", base64.b64decode(str(block.get("data", "")))))
                except (Unreadable, ValueError):
                    continue
                names.append(f"photos/{page_id}/{number}-{len(names) + 1}.jpg")
                self.data.write_bytes(names[-1], done.files[0][1])
            if names:
                found[option["link"]] = names
        page = self.data.read(name)  # she may have published it again meanwhile: her new page, these photos
        if found and page:
            for option in page.get("options") or []:
                option["photos"] = found.get(option["link"], option.get("photos") or [])
            self.data.write(name, page)
            self.poke(now)

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

    # --- the owner's own pages: HTML files of the base, served sandboxed under /pages/ -------------------------------

    async def write_pages(self, now: float) -> None:
        if self.core.memory:
            await asyncio.to_thread(self.pages, now)

    def pages(self, now: float) -> bool:
        """Every HTML file in a nature folder of the base that says `<meta name="lifehub" content="publish">`, as the
        hub's `main` has it: its bytes to data/pages/<id>.html, what the «Library» lists to pages.json (with each
        file's sha256: the builder takes a page only with the bytes this list names), what was left out and why to
        pages-refused.json. Scripts run only when no commit of hers ever touched the file (`author`). Read again only
        when `main` moved. Returns whether it read the hub."""
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
                log.warning("lifehub: %r is not a plain page name: not published", path)
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
            author, seconds = self.author(path, ref)
            found.setdefault(page_id, []).append({
                "id": page_id, **meta, "topic": parts[0],
                "updated": clock.local(seconds or now, self.core.tz).date().isoformat(),
                "author": author, "scripts": "on" if author == "owner" else "off",
                "url": f"{PAGES}/{page_id}.html", "path": path,
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
            if PurePosixPath(name).stem not in ids:
                self.data.remove(name)
        self.data.write("pages-refused.json", refused)
        self.data.write("pages.json", pages)
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
        """The owner sent an HTML file with «to hub <Space>» (or the Russian «в хаб <Space>»: `channels/telegram.py`; a
        page made in claude.ai on his phone): code, not the model, puts it in the base as <Space>/artifacts/<name>.html
        with the opt-in mark, commits it with a trailer that says it is his, and answers with the page's address. On
        record like any message of his."""
        from .archive import OWNER

        core = self.core
        name = (upload.name or "page.html").strip()
        event, fresh = core.archive.append(OWNER, f"[page to hub {space}: {name}]", channel=origin.name,
                                           native_id=native_id, meta={"hub_page": space},
                                           conversation_id=core.store.conversation(core.default_agent))
        if not fresh and core.archive.answered(event.id):
            return  # delivered again: answered already
        try:
            text = await self.put_page(space, name, upload, time.time())
        except Exception as exc:  # the owner hears of it instead of silence
            log.exception("lifehub: the page %s was not put in the base", name)
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
        said = (f"Page in the Base: {path}. In a couple of minutes it opens at {self.url}{PAGES}/{page_id}.html "
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


def cut(base: str, title: str, chosen: dict, rows: list, key: str = "items", size: int | None = None) -> list[dict]:
    """One slice of the review as its pages: `base`, `base-p2`, … with the neighbours and every page's number."""
    size = size or REVIEW_PAGE
    pages = max(1, -(-len(rows) // size))
    paths = [base if n == 1 else f"{base}-p{n}" for n in range(1, pages + 1)]
    numbers = [{"page": n, "path": path} for n, path in enumerate(paths, 1)]
    return [{"path": path, "title": title, "kind": key, "page": n, "pages": pages,
             "filter": {"source": chosen.get("source", ""), "decision": chosen.get("decision", ""),
                        "who": chosen.get("who", "")},
             key: runs(rows[(n - 1) * size:n * size]) if key == "items" else rows[(n - 1) * size:n * size],
             "prev": paths[n - 2] if n > 1 else "", "next": paths[n] if n < pages else "", "numbers": numbers}
            for n, path in enumerate(paths, 1)]


def runs(items: list[dict]) -> list[dict]:
    """A page of the review with each message marked `cont` when it goes on from the one above — the same sender,
    the same day: a chat of ten lines reads as one card, not ten."""
    out = []
    for item in items:
        before = out[-1] if out else None
        out.append({**item, "cont": bool(before and before["who_key"] == item["who_key"]
                                         and before["day"] == item["day"])})
    return out


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
        if len(block) <= PARAGRAPH_CHARS:
            out += [block] if block else []
            continue
        chunk = ""
        for sentence in re.split(r"(?<=[.!?])\s+(?=[«\"(A-ZА-ЯЁ0-9])", block):  # her words may be Russian too
            if chunk and len(chunk) + len(sentence) > PARAGRAPH_CHARS:
                out.append(chunk)
                chunk = ""
            chunk = f"{chunk} {sentence}".strip()
        out += [chunk] if chunk else []
    return out


def places(location: str, description: str) -> dict:
    """Where an event is, as a page shows it: `place` — zoom, meet, teams, online (another meeting service), link,
    address or nothing; `where` — an address or a site's host, never an address with its query; `join` — the address
    of a meeting service to go in by (its host in `join_host`). An address with a password stays in the data the
    page links to and never becomes its text."""
    for found in URL.findall(f"{location} {description}"):
        url = sound(found.rstrip(".,;:!?)»>"))
        host = (urlsplit(url).hostname or "") if url else ""
        kind = next((name for suffix, name in MEETINGS if host == suffix or host.endswith("." + suffix)), "")
        if kind:
            return {"place": kind, "where": "", "join": url, "join_host": host}
    found = URL.search(location)
    if found:  # a site, named by its host; an address that is not a sound one is not named at all
        host = host_of(found.group().rstrip(".,;:!?)»>"))
        return {"place": "link" if host else "", "where": host, "join": "", "join_host": ""}
    where = re.sub(r"\s+", " ", location).strip()
    return {"place": "address" if where else "", "where": where[:120], "join": "", "join_host": ""}


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
HEALTH = re.compile(r"^(Health[^:]*|Здоровье за сутки): ")
NO_NAME = ("no name", "без имени")  # chip's word for a sender with none; the Russian one may be in older data


def backed_up(said: str) -> bool:
    """Whether the backup's health line says it went well: «backup — 5 h ago», not a failure."""
    return bool(BACKUP.match(said)) and not any(word in said for word in BACKUP_FAILED)


def health_words(line: str) -> str:
    """The health line without its heading and its full stop: what the «now» page shows."""
    return HEALTH.sub("", line, count=1).rstrip(".")


def level(share: int) -> str:
    """A limit window's share as a colour: ok below 70 %, warn below 90 %, bad from there."""
    return "ok" if share < 70 else "warn" if share < 90 else "bad"


def score(text: str) -> float:
    """«4.1/5» -> 4.1; anything else 0."""
    found = re.match(r"\s*(\d+(?:[.,]\d+)?)", str(text or ""))
    return float(found.group(1).replace(",", ".")) if found else 0.0


def midnight(day: date, tz: str) -> float:
    """The moment a local day begins in the zone — a day is not always 24 hours long."""
    from . import clock

    return datetime(day.year, day.month, day.day, tzinfo=clock.zone(tz)).timestamp()


def _instant(value) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _epoch(value) -> int | None:
    try:
        return round(_instant(value)) if value else None
    except ValueError:
        return None


# --- the owner's pages: what the router reads from an HTML file -------------------------------------------------

PAGE_MAX = 3 * 1024 * 1024    # the builder refuses a bigger page too: libraries and fonts are vendored, not in git
PAGES = "/pages"              # where the hub serves them: nginx gives everything under it the pages' sandbox
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
