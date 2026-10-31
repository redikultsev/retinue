"""«Pages»: the owner's HTML pages in the face — the pages service's list (docs/pages.md), by section, with what he
has opened and finished.

The router knows the list and pushes it here (`POST /owner/library`) whole each time it changes and at least hourly;
the platform never calls the router, as with the assistant's digest (`feed.py`). It is minimal by contract: an id, a
title, the page's address, its Space, the day it changed, its reading time — no body, no description. A page is never
fetched or framed by the face: it opens on the pages service's own origin, in a tab of its own, so its scripts stay as
far from the face's login as from any other site. The address is therefore the one field that could do harm, and it
is held to the page's address there, `<pages_origin>/<id>`, the origin coming from the platform's own config, not the
push. A page off the contract is dropped and named; the rest of the list stands.

What the owner did with a page — opened it, said he finished it — is the platform's own row (`library_reads`), so a
phone and a desktop agree. A page on another origin cannot report its scroll position, so «where he stopped» is when
it was last opened plus his own «Finished»; what the page itself keeps (its ticks, its progress) is the pages
service's.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .courier import line
from .feed import spheres

if TYPE_CHECKING:
    from .sessions import PlatformStore

PAGES_MAX = 200               # the router publishes at most this many (`pages.PAGES_MAX`)
BYTES = 512 * 1024            # a push of 200 pages with long Cyrillic titles, JSON-escaped, fits with room to spare
KEYS = {"id", "title", "url", "updated", "section", "minutes"}
REQUIRED = {"id", "title", "url", "updated"}
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # `pages.PAGE_ID`: the page's path on the pages service
SECTION = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")  # a Space: the first folder of a page's path (`pages.PAGE_PATH`)
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TITLE_CHARS = 200
MINUTES_MAX = 100_000
DEFAULT_PORTS = {"https": 443}


class Refused(ValueError):
    """A list not shaped as the contract says: refused whole, so the one on record stays as it was."""


def origin(url: str) -> str:
    """`https://<host>[:port]` as one origin is written however it was typed: the host in lower case, the default
    port gone; '' for anything that is not an https address of a host alone (a user, a password, a bad port)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or \
            parts.password is not None:
        return ""
    keep = f":{port}" if port is not None and port != DEFAULT_PORTS["https"] else ""
    return f"https://{parts.hostname.lower()}{keep}"


def clean(body: object, pages_origin: str) -> tuple[list[dict], list[dict]]:
    """The router's list, checked: `{pages: [{id, title, url, updated, section?, minutes?}]}`, every `url` the page's
    address on the pages service (`<pages_origin>/<id>`, both origins normalised). A list off its shape or over a cap is
    refused whole (`Refused`); a page off the contract is dropped, and named with the reason, so one bad page does
    not hide the rest. Returns the pages kept, each `url` written canonically, and the dropped `[{id, why}]`."""
    home = origin(pages_origin)
    if not home:
        raise Refused("pages_origin is not set in platform.yaml: nothing to check the pages' addresses against")
    if not isinstance(body, dict) or set(body) != {"pages"} or not isinstance(body["pages"], list):
        raise Refused("pages: not a list")
    if len(body["pages"]) > PAGES_MAX:
        raise Refused(f"pages: more than {PAGES_MAX}")
    if not all(isinstance(page, dict) for page in body["pages"]):
        raise Refused("pages: not a list of objects")
    pages, dropped, seen = [], [], set()
    for page in body["pages"]:
        page_id = page.get("id")
        named = page_id if isinstance(page_id, str) and PAGE_ID.fullmatch(page_id) else "?"
        why = _wrong(page, home, seen)
        if why:
            dropped.append({"id": named, "why": why})
            continue
        seen.add(page_id)
        pages.append({"id": page_id, "title": line(page["title"], TITLE_CHARS), "url": f"{home}/{page_id}",
                      "updated": page["updated"], "section": page.get("section"), "minutes": page.get("minutes")})
    return pages, dropped


def _wrong(page: dict, home: str, seen: set) -> str:
    """Why one page is off the contract, or ''."""
    if not KEYS >= set(page) or not REQUIRED <= set(page):
        return "wrong fields"
    page_id = page["id"]
    if not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id):
        return "id malformed"
    if page_id in seen:
        return "id repeated"
    url = page["url"]
    if not isinstance(url, str):
        return "url not a string"
    parts = urlsplit(url)
    if origin(url) != home or parts.path != f"/{page_id}" or parts.query or parts.fragment or "?" in url \
            or "#" in url:
        return f"url is not the page's address {home}/{page_id}"
    if not isinstance(page["title"], str) or not line(page["title"], TITLE_CHARS):
        return "title empty or not a string"
    if not isinstance(page["updated"], str) or not DAY.fullmatch(page["updated"]) or not _day(page["updated"]):
        return "updated is not a date YYYY-MM-DD"
    section = page.get("section")
    if section is not None and (not isinstance(section, str) or not SECTION.fullmatch(section)):
        return "section is not a Space's name"
    minutes = page.get("minutes")
    if minutes is not None and (type(minutes) is not int or not 0 < minutes <= MINUTES_MAX):
        return "minutes is not a number of minutes"
    return ""


def _day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


class Library:
    def __init__(self, store: PlatformStore, cfg=None) -> None:
        self.store, self.cfg = store, cfg
        store.db.execute("CREATE TABLE IF NOT EXISTS library_pages (id INTEGER PRIMARY KEY CHECK (id = 1), "
                         "pages TEXT NOT NULL, at REAL NOT NULL)")
        store.db.execute("CREATE TABLE IF NOT EXISTS library_reads (page TEXT PRIMARY KEY, opened REAL, done REAL)")
        store.db.commit()

    @property
    def origin(self) -> str:
        return origin(getattr(self.cfg, "pages_origin", "") or "")

    def keep(self, pages: list[dict], now: float) -> None:
        """The list as pushed, replacing the one before it whole. What he read of a page that left it stays: the
        same id published again is the same page."""
        self.store.db.execute("INSERT OR REPLACE INTO library_pages (id, pages, at) VALUES (1, ?, ?)",
                              (json.dumps(pages, ensure_ascii=False), now))
        self.store.db.commit()

    def pages(self) -> tuple[list[dict], float | None]:
        row = self.store.db.execute("SELECT pages, at FROM library_pages WHERE id = 1").fetchone()
        return (json.loads(row[0]), row[1]) if row else ([], None)

    def face(self) -> dict:
        """`GET /api/library`: every page with its section's title and icon (a Space named as a sphere is), when he
        last opened it and when he said he finished it; the list's age."""
        pages, at = self.pages()
        reads = {page: (opened, done) for page, opened, done in
                 self.store.db.execute("SELECT page, opened, done FROM library_reads")}
        return {"origin": self.origin, "at": at, "pages": [self._shown(page, reads) for page in pages]}

    def _shown(self, page: dict, reads: dict) -> dict:
        known = spheres(self.cfg).get(page["section"]) if page.get("section") else None
        opened, done = reads.get(page["id"], (None, None))
        return {**page, "section_title": known["title"] if known else page.get("section"),
                "section_icon": known["icon"] if known else None, "opened": opened, "done": done}

    def page(self, page_id: str) -> dict | None:
        """One page of the list as the face shows it, or None when the list has no such page."""
        found = next((page for page in self.pages()[0] if page["id"] == page_id), None)
        if found is None:
            return None
        row = self.store.db.execute("SELECT opened, done FROM library_reads WHERE page = ?", (page_id,)).fetchone()
        return self._shown(found, {page_id: row} if row else {})

    def opened(self, page_id: str, now: float) -> dict | None:
        """He opened the page: it goes to «Continue» until he finishes it."""
        if self.page(page_id) is None:
            return None
        self.store.db.execute("INSERT INTO library_reads (page, opened) VALUES (?, ?) "
                              "ON CONFLICT (page) DO UPDATE SET opened = excluded.opened", (page_id, now))
        self.store.db.commit()
        return self.page(page_id)

    def finished(self, page_id: str, done: bool, now: float) -> dict | None:
        """«Finished», or taken back."""
        if self.page(page_id) is None:
            return None
        self.store.db.execute("INSERT INTO library_reads (page, done) VALUES (?, ?) "
                              "ON CONFLICT (page) DO UPDATE SET done = excluded.done", (page_id, now if done else None))
        self.store.db.commit()
        return self.page(page_id)
