"""The pages service (`retinue-pages`): the owner's HTML pages, with what they keep, and the ones he shares.

The builder (`deploy/pages/build.py`) checks the pages the router lists — the base's pages and the router's trip
pages — and switches its `current` release; this process serves that release to the owner's devices on its own host
(`PAGES_ORIGIN`, behind the VPN and Traefik's key) and keeps, in its own SQLite, what each page saves. It shares
nothing with the face: another origin, no cookie, no API of the app's, no network of the platform's.

A page is never a document of this origin. `/<id>` is the **shell** — this module's own page and script, the page's
title, a bar for the owner — and the page itself is `/raw/<id>` in the shell's frame, sandboxed twice (the frame's
`sandbox` attribute and the response's CSP `sandbox`, the same permissions in both): an opaque origin, no network
(`connect-src 'none'`), nothing loaded but `/pages/_vendor/`. Its storage comes back by the shell: the service puts
the page's saved values and `frame.js` first in the page — a `localStorage`, a `sessionStorage` and Claude artifacts'
`window.storage` — and every change goes over a MessageChannel to the shell, which saves it for its own page id
(`PUT /api/store/<id>`). The page never names a page: its id is the shell's address. `/raw/<id>` answers only a frame
of this origin loading it (`Sec-Fetch-Dest: iframe`, `Sec-Fetch-Site: same-origin`), so a page that points its frame
at another page's raw address gets 403, not that page's values.

**Sharing** (`/api/share/<id>`, the owner's bar): a page goes on a public site — one of `PAGES_PUBLIC_HOSTS`, at
`/<id>` or as the site's home page `/`. The internet never reaches this process: the public sites are a static nginx
of their own (`deploy/pages/public.conf`) serving the files this process writes into `PAGES_PUBLIC` — each shared
page's shell and its raw page with its values in it, written again within seconds of every change of the page or of
what it keeps (`export`), and deleted when the owner stops sharing it or the page leaves the base. There, the values
are read-only: a visitor's changes live in his frame for his visit.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

log = logging.getLogger("retinue.pages")

STATIC = Path(__file__).with_name("pages_static")
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
ORIGIN = re.compile(r"https://[a-z0-9.-]+(?::\d{1,5})?")
HOME = "_home"                 # a public site's home page: its file name; an id never starts with "_"
SCOPES = ("ls:", "st:", "sh:")  # localStorage, window.storage, window.storage shared (frame.js); sessionStorage is not kept
KEY_MAX = 512                  # characters of a key, its scope included
VALUE_MAX = 1024 * 1024        # bytes of one value
PAGE_KEYS = 2000               # keys of one page
PAGE_BYTES = 5 * 1024 * 1024   # bytes of one page's keys and values
STORE_BYTES = 500 * 1024 * 1024  # of every page's
OPS_MAX = 2000                 # changes in one save
BODY_MAX = 6 * 1024 * 1024     # a save's body
POLL_S = 5                     # how often the builder's `current` is looked at
EXPORT_DELAY_S = 2             # a shared page is written out this long after its last change
# The page's two sandboxes, word for word the same in the frame's attribute and the response's policy: a permission
# counts only when both grant it. Scripts on: scripts, forms (their submit event; sending one is `form-action 'none'`)
# and alert/confirm — never popups or navigation: links go through the shell. Scripts off: nothing runs, so a link may
# open a new window by itself (`<base target="_blank">`, the service's own).
SANDBOX = {True: "allow-scripts allow-forms allow-modals", False: "allow-popups allow-popups-to-escape-sandbox"}
SHELL_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
             "frame-src 'self'; worker-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; "
             "frame-ancestors 'none'; object-src 'none'")
BARE_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
HEAD = (re.compile(r"<head(?:\s[^>]*)?>", re.I), re.compile(r"<html(?:\s[^>]*)?>", re.I))


def raw_csp(origin: str, scripts: bool) -> str:
    """The policy of a page in its frame. Every source names the vendored folder with its host, never `'self'`: what
    `'self'` means to an opaque origin is not settled across browsers."""
    vendor = f"{origin}/pages/_vendor/"
    script = f"{vendor} 'unsafe-inline'" if scripts else "'none'"
    return (f"default-src 'none'; script-src {script}; style-src {vendor} 'unsafe-inline'; img-src {vendor} data:; "
            f"font-src {vendor} data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; manifest-src 'none'; "
            f"base-uri 'none'; form-action 'none'; frame-ancestors {origin}; sandbox {SANDBOX[scripts]}")


class Full(ValueError):
    """A save over a limit: refused whole, the page's values as they were."""


@dataclass
class Config:
    origin: str                         # the owner's host, https://pages.in.example.com
    key: str                            # what Traefik adds to every request it lets through to this process
    public_hosts: list[str] = field(default_factory=list)  # the public sites, pages.example.com first
    app_origin: str = ""                # the face, for the bar's «← Pages»; "" — no such link
    site: Path = Path("/site")          # the builder's releases; `current` is the live one
    store: Path = Path("/store/pages.sqlite")
    public: Path = Path("/public")      # what the public nginx serves, one folder per host
    port: int = 8080

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        origin = env.get("PAGES_ORIGIN", "").rstrip("/").lower()
        if not ORIGIN.fullmatch(origin):
            raise SystemExit(f"pages: PAGES_ORIGIN {origin!r} is not https://<host>")
        key = env.get("PAGES_KEY", "")
        if len(key) < 32:
            raise SystemExit("pages: PAGES_KEY is missing or short — setup.sh makes it")
        hosts = [h.strip().lower() for h in env.get("PAGES_PUBLIC_HOSTS", "").split(",") if h.strip()]
        for host in hosts:
            if not HOST.fullmatch(host) or host == origin.removeprefix("https://"):
                raise SystemExit(f"pages: public host {host!r} is not a host name of its own")
        app = env.get("PAGES_APP_ORIGIN", "").rstrip("/").lower()
        if app and not ORIGIN.fullmatch(app):
            raise SystemExit(f"pages: PAGES_APP_ORIGIN {app!r} is not https://<host>")
        return cls(origin=origin, key=key, public_hosts=list(dict.fromkeys(hosts)), app_origin=app,
                   site=Path(env.get("PAGES_SITE", "/site")), store=Path(env.get("PAGES_STORE", "/store/pages.sqlite")),
                   public=Path(env.get("PAGES_PUBLIC", "/public")), port=int(env.get("PAGES_PORT", "8080")))


class Store:
    """What every page keeps, and which pages are shared where. One writer: this process."""

    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS store (page TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                                              updated REAL NOT NULL, PRIMARY KEY (page, key));
            CREATE TABLE IF NOT EXISTS shares (host TEXT NOT NULL, slug TEXT NOT NULL, page TEXT NOT NULL,
                                               created REAL NOT NULL, PRIMARY KEY (host, slug));
        """)

    def values(self, page: str) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM store WHERE page = ? ORDER BY key", (page,)))

    def apply(self, page: str, ops: list, now: float) -> dict:
        """A save: every change or none. Raises Full over a limit, ValueError on a change off its shape."""
        if not isinstance(ops, list) or not 0 < len(ops) <= OPS_MAX:
            raise ValueError(f"ops: a list of 1 to {OPS_MAX} changes")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for op in ops:
                key = op.get("key") if isinstance(op, dict) else None
                if not (isinstance(key, str) and key.startswith(SCOPES) and len(key) <= KEY_MAX):
                    raise ValueError("a change's key is not a page's key")
                if op.get("op") == "set" and isinstance(op.get("value"), str):
                    if len(op["value"].encode()) > VALUE_MAX:
                        raise Full(f"one value is at most {VALUE_MAX // 1024} KB")
                    self.db.execute("INSERT INTO store (page, key, value, updated) VALUES (?, ?, ?, ?) ON CONFLICT"
                                    "(page, key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                                    (page, key, op["value"], now))
                elif op.get("op") == "del":
                    self.db.execute("DELETE FROM store WHERE page = ? AND key = ?", (page, key))
                else:
                    raise ValueError("a change is {op: set, key, value} or {op: del, key}")
            keys, size = self.db.execute("SELECT count(*), coalesce(sum(length(CAST(key AS BLOB)) + "
                                         "length(CAST(value AS BLOB))), 0) FROM store WHERE page = ?",
                                         (page,)).fetchone()
            if keys > PAGE_KEYS:
                raise Full(f"a page keeps at most {PAGE_KEYS} keys")
            if size > PAGE_BYTES:
                raise Full(f"a page keeps at most {PAGE_BYTES // 1024 // 1024} MB")
            if self.size() > STORE_BYTES:
                raise Full("the pages' store is full")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        return {"keys": keys, "bytes": size}

    def size(self) -> int:
        return self.db.execute("SELECT coalesce(sum(length(CAST(key AS BLOB)) + length(CAST(value AS BLOB))), 0) "
                               "FROM store").fetchone()[0]

    def shares(self, page: str | None = None) -> list[tuple[str, str, str]]:
        """(host, slug, page) — of one page, or every share."""
        if page is None:
            return list(self.db.execute("SELECT host, slug, page FROM shares ORDER BY host, slug"))
        return list(self.db.execute("SELECT host, slug, page FROM shares WHERE page = ? ORDER BY host, slug", (page,)))

    def share(self, page: str, host: str, slug: str, now: float) -> str | None:
        """Shares the page there; returns the page that was there before, if another."""
        before = self.db.execute("SELECT page FROM shares WHERE host = ? AND slug = ?", (host, slug)).fetchone()
        self.db.execute("INSERT INTO shares (host, slug, page, created) VALUES (?, ?, ?, ?) ON CONFLICT (host, slug) "
                        "DO UPDATE SET page = excluded.page, created = excluded.created", (host, slug, page, now))
        return before[0] if before and before[0] != page else None

    def unshare(self, page: str, host: str, slug: str) -> bool:
        return self.db.execute("DELETE FROM shares WHERE host = ? AND slug = ? AND page = ?",
                               (host, slug, page)).rowcount > 0


class Release:
    """The builder's live release: its pages.json and files, read again when `current` moves."""

    def __init__(self, site: Path) -> None:
        self.site = site
        self.name = ""
        self.pages: dict[str, dict] = {}

    def refresh(self) -> bool:
        """Reads the release if `current` points elsewhere now. Returns whether it changed."""
        try:
            name = os.readlink(self.site / "current")
        except OSError:
            name = ""
        if name == self.name:
            return False
        pages: dict[str, dict] = {}
        try:
            listed = json.loads((self.site / name / "pages.json").read_text()) if name else []
        except (OSError, ValueError):
            log.warning("pages: release %s has no readable pages.json", name)
            listed = []
        for entry in listed if isinstance(listed, list) else []:
            if isinstance(entry, dict) and PAGE_ID.fullmatch(str(entry.get("id", ""))):
                pages[entry["id"]] = entry
        self.name, self.pages = name, pages
        log.info("pages: release %s, %d pages", name or "none", len(pages))
        return True

    @property
    def root(self) -> Path:
        return self.site / self.name

    def body(self, page_id: str) -> tuple[str, bool] | None:
        """The page's text as the builder left it, and whether its scripts run."""
        entry = self.pages.get(page_id)
        if not entry:
            return None
        scripts = entry.get("scripts") == "on"
        path = self.root / ("scripts" if scripts else "") / f"{page_id}.html"
        try:
            return path.read_text(encoding="utf-8"), scripts
        except OSError:
            return None

    def title(self, page_id: str) -> str:
        return str(self.pages.get(page_id, {}).get("title") or page_id)


def inject(text: str, scripts: bool, values: dict[str, str], keep: bool) -> str:
    """The page with the service's part first in its head: with scripts, frame.js and the page's values; without,
    `<base target="_blank">`, so a link opens a window of its own (the frame may not navigate away)."""
    if scripts:
        data = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
        data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        code = (STATIC / "frame.js").read_text().strip().replace("})(DATA, KEEP);", f"}})({data}, {str(keep).lower()});")
        part = f"<script>{code}</script>"
    else:
        part = '<base target="_blank">'
    found = HEAD[0].search(text) or HEAD[1].search(text)
    return text[:found.end()] + part + text[found.end():] if found else part + text


def shell(title: str, page: str, raw: str, scripts: bool, owner: bool, app_origin: str = "") -> str:
    """The page around a page: its title, for the owner a bar (back to the app, saved or not, «Share»), the frame."""
    t = html.escape(title)
    bar = ""
    if owner:
        back = f'<a class="back" href="{html.escape(app_origin)}/pages">← Pages</a>' if app_origin else ""
        bar = (f'<header class="bar">{back}<h1>{t}</h1><span id="saved" role="status" aria-live="polite"></span>'
               '<button id="share" type="button" aria-expanded="false" aria-controls="sharing">Share</button>'
               '<div id="sharing" hidden><p>Anyone with the link sees this page and what it keeps; your changes '
               'show there within seconds. Visitors\' changes are not kept.</p><ul data-list hidden></ul>'
               '<form hidden><select aria-label="Public site"></select><label><input type="checkbox"> as the '
               'site\'s home page</label><button type="submit">Share</button></form>'
               '<p data-said role="status"></p></div></header>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<title>{t}</title><link rel="stylesheet" href="/_pages/shell.css">'
            '<script src="/_pages/shell.js" defer></script></head>'
            f'<body data-page="{html.escape(page)}" data-mode="{"owner" if owner else "public"}">{bar}'
            '<a id="blocked" target="_blank" rel="noopener noreferrer" hidden></a>'
            f'<iframe id="page" title="{t}" sandbox="{SANDBOX[scripts]}" src="{html.escape(raw)}"></iframe>'
            '</body></html>')


def write(path: Path, data: str | bytes) -> None:
    """A file replaced whole, readable by the public nginx (another user)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(handle, "wb") as file:
        file.write(data.encode() if isinstance(data, str) else data)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class Pages:
    """The service: the owner's host, the store, the public sites' files."""

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.release = Release(cfg.site)
        self.dirty: dict[str, float] = {}       # a shared page whose values changed: when, until it is written out
        self.task: asyncio.Task | None = None

    # --- the public sites' files -------------------------------------------------------------------------------------

    def url(self, host: str, slug: str) -> str:
        return f"https://{host}/" + ("" if slug == HOME else slug)

    def export(self, page: str | None = None) -> None:
        """Writes out every share of the page (or every share): its shell and its raw page with its values; a share
        whose page is not live has no files. Each host's folder has the shell's script and style and the vendored
        files."""
        for host, slug, page_id in self.store.shares(page):
            folder = self.cfg.public / host
            found = self.release.body(page_id)
            raws = {True: folder / "r" / "scripts" / f"{slug}.html", False: folder / "r" / f"{slug}.html"}
            if found is None:
                for path in (folder / "s" / f"{slug}.html", *raws.values()):
                    unlink(path)
                continue
            text, scripts = found
            write(raws[scripts], inject(text, scripts, self.store.values(page_id), keep=False))
            unlink(raws[not scripts])
            write(folder / "s" / f"{slug}.html", shell(self.release.title(page_id), page_id,
                                                       f"/raw/{slug}", scripts, owner=False))

    def sweep(self) -> None:
        """Every public file no share names goes; every public host has the shell's files and the vendored ones of
        the live release."""
        shared = {(host, slug) for host, slug, _ in self.store.shares()}
        for folder in (self.cfg.public.iterdir() if self.cfg.public.is_dir() else []):
            if folder.name not in self.cfg.public_hosts:
                shutil.rmtree(folder, ignore_errors=True)
                continue
            for path in [*folder.glob("s/*.html"), *folder.glob("r/*.html"), *folder.glob("r/scripts/*.html")]:
                if (folder.name, path.stem) not in shared:
                    unlink(path)
        for host in self.cfg.public_hosts:
            folder = self.cfg.public / host
            for name in ("shell.js", "shell.css"):
                write(folder / "_pages" / name, (STATIC / name).read_bytes())
            vendor = self.release.root / "_vendor"
            if self.release.name and vendor.is_dir():
                fresh = folder / "pages" / "_vendor.tmp"
                shutil.rmtree(fresh, ignore_errors=True)
                shutil.copytree(vendor, fresh)
                for path in [fresh, *fresh.rglob("*")]:
                    os.chmod(path, 0o755 if path.is_dir() else 0o644)
                old = folder / "pages" / "_vendor"
                if old.exists():
                    os.replace(old, folder / "pages" / "_vendor.old")
                os.replace(fresh, old)
                shutil.rmtree(folder / "pages" / "_vendor.old", ignore_errors=True)
                os.chmod(folder / "pages", 0o755)

    def look(self, now: float) -> None:
        """One turn of the loop: a new release is read and every share written out again; a shared page whose
        values changed is written out EXPORT_DELAY_S after its last change."""
        if self.release.refresh():
            self.export()
            self.sweep()
        for page, at in list(self.dirty.items()):
            if now - at >= EXPORT_DELAY_S:
                del self.dirty[page]
                self.export(page)

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.look, time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pages: a look failed")
            await asyncio.sleep(1 if self.dirty else POLL_S)

    # --- the owner's host --------------------------------------------------------------------------------------------

    @web.middleware
    async def guard(self, request: web.Request, handler) -> web.StreamResponse:
        """Only what Traefik let through (its key), and every answer with its headers — an error's too: a response
        under /raw/ without its sandbox would be a document of this origin."""
        if not hmac.compare_digest(request.headers.get("X-Pages-Key", ""), self.cfg.key):
            response: web.StreamResponse = web.Response(status=404, text="Not found")
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.json_response({"error": exc.text or exc.reason}, status=exc.status) \
                    if request.path.startswith("/api/") else web.Response(status=exc.status, text=exc.reason)
            except Exception:
                log.exception("pages: %s %s failed", request.method, request.path)
                response = web.Response(status=500, text="Internal error")
        headers = response.headers
        policy = headers.pop("X-Raw-Policy", "")
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Robots-Tag"] = "noindex, nofollow"
        if request.path.startswith("/raw/"):
            headers["Content-Security-Policy"] = policy or BARE_CSP
            headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/pages/_vendor/") and response.status == 200:
            headers["Content-Security-Policy"] = BARE_CSP
            headers["Access-Control-Allow-Origin"] = "*"  # a sandboxed page's fetch of a font is cross-origin
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Content-Security-Policy"] = SHELL_CSP
            headers["Cross-Origin-Opener-Policy"] = "same-origin"
            headers["X-Frame-Options"] = "DENY"
            headers.setdefault("Cache-Control", "no-cache")
        return response

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.guard], client_max_size=BODY_MAX)
        app.router.add_get("/", self.index)
        app.router.add_get("/_pages/{name}", self.asset)
        app.router.add_get("/pages/_vendor/{path:.+}", self.vendor)
        app.router.add_get("/raw/{page}", self.raw)
        app.router.add_put("/api/store/{page}", self.save)
        app.router.add_get("/api/share/{page}", self.shared)
        app.router.add_put("/api/share/{page}", self.share)
        app.router.add_delete("/api/share/{page}", self.unshare)
        app.router.add_get("/{page}", self.page)
        return app

    def live(self, request: web.Request) -> str:
        page = request.match_info["page"]
        if not PAGE_ID.fullmatch(page) or page not in self.release.pages:
            raise web.HTTPNotFound(text="No such page")
        return page

    async def index(self, request: web.Request) -> web.Response:
        rows = sorted(self.release.pages.values(), key=lambda p: (str(p.get("updated", "")), p["id"]), reverse=True)
        items = "".join(f'<li><a href="/{html.escape(p["id"])}">{html.escape(str(p.get("title") or p["id"]))}'
                        f'<span>{html.escape(str(p.get("topic") or ""))}</span></a></li>' for p in rows)
        return web.Response(content_type="text/html", charset="utf-8", text=(
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>Pages</title><link rel="stylesheet" '
            f'href="/_pages/shell.css"></head><body><main class="index"><h1>Pages</h1><ul>{items}</ul></main>'
            '</body></html>'))

    async def asset(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in ("shell.js", "shell.css"):
            raise web.HTTPNotFound()
        return web.Response(body=(STATIC / name).read_bytes(), charset="utf-8",
                            content_type="text/javascript" if name.endswith(".js") else "text/css")

    async def vendor(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*", path) or not self.release.name:
            raise web.HTTPNotFound()
        file = self.release.root / "_vendor" / path
        if not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file)

    async def page(self, request: web.Request) -> web.Response:
        page = self.live(request)
        found = self.release.body(page)
        if found is None:
            raise web.HTTPNotFound(text="No such page")
        return web.Response(content_type="text/html", charset="utf-8", text=shell(
            self.release.title(page), page, f"/raw/{page}", found[1], owner=True, app_origin=self.cfg.app_origin))

    async def raw(self, request: web.Request) -> web.Response:
        """The page in the shell's frame — only there: a frame of this origin loading it, never another page's
        frame pointed here, never a window of its own."""
        if (request.headers.get("Sec-Fetch-Dest") != "iframe"
                or request.headers.get("Sec-Fetch-Site") != "same-origin"):
            raise web.HTTPForbidden(text="A page opens in its shell: /<id>")
        page = self.live(request)
        found = self.release.body(page)
        if found is None:
            raise web.HTTPNotFound(text="No such page")
        text, scripts = found
        return web.Response(content_type="text/html", charset="utf-8",
                            text=inject(text, scripts, self.store.values(page), keep=True),
                            headers={"X-Raw-Policy": raw_csp(self.cfg.origin, scripts)})

    async def body(self, request: web.Request) -> dict:
        """An API call's JSON, from the shell alone: its header, its origin, a same-origin fetch."""
        if request.headers.get("X-Pages") != "1":
            raise web.HTTPForbidden(text="X-Pages: 1 is required")
        if request.headers.get("Origin", self.cfg.origin).rstrip("/").lower() != self.cfg.origin:
            raise web.HTTPForbidden(text="another origin")
        if request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            raise web.HTTPForbidden(text="another site")
        if request.method == "GET":
            return {}
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="the body is not JSON") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="the body is not an object")
        return data

    async def save(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        page = self.live(request)
        if self.release.pages[page].get("scripts") != "on":
            raise web.HTTPForbidden(text="this page runs no script: it keeps nothing")
        try:
            done = await asyncio.to_thread(self.store.apply, page, data.get("ops"), time.time())
        except Full as exc:
            raise web.HTTPRequestEntityTooLarge(max_size=PAGE_BYTES, actual_size=0, text=str(exc)) from None
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        if self.store.shares(page):
            self.dirty[page] = time.time()
        return web.json_response({"ok": True, **done}, headers={"Cache-Control": "no-store"})

    def where(self, data: dict) -> tuple[str, str]:
        host = str(data.get("host") or "").lower()
        if host not in self.cfg.public_hosts:
            raise web.HTTPBadRequest(text="not one of the public sites")
        return host, HOME if data.get("home") else ""

    def state(self, page: str) -> dict:
        return {"hosts": self.cfg.public_hosts,
                "shared": [{"host": host, "home": slug == HOME, "url": self.url(host, slug)}
                           for host, slug, _ in self.store.shares(page) if host in self.cfg.public_hosts]}

    async def shared(self, request: web.Request) -> web.Response:
        await self.body(request)
        return web.json_response(self.state(self.live(request)), headers={"Cache-Control": "no-store"})

    async def share(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        page = self.live(request)
        host, slug = self.where(data)
        before = self.store.share(page, host, slug or page, time.time())
        if before:
            log.info("pages: %s replaces %s on %s/%s", page, before, host, slug or page)
        await asyncio.to_thread(self.export, page)
        log.info("pages: %s shared on %s", page, self.url(host, slug or page))
        return web.json_response(self.state(page), headers={"Cache-Control": "no-store"})

    async def unshare(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        page = self.live(request)
        host, slug = self.where(data)
        if self.store.unshare(page, host, slug or page):
            await asyncio.to_thread(self.sweep)
            log.info("pages: %s no longer shared on %s", page, self.url(host, slug or page))
        return web.json_response(self.state(page), headers={"Cache-Control": "no-store"})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = Config.from_env()
    cfg.store.parent.mkdir(parents=True, exist_ok=True)
    pages = Pages(cfg, Store(cfg.store))

    async def run() -> None:
        runner = web.AppRunner(pages.app(), access_log=None, shutdown_timeout=2)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
        pages.task = asyncio.create_task(pages.loop())
        log.info("pages on :%d for %s; public sites: %s", cfg.port, cfg.origin, ", ".join(cfg.public_hosts) or "none")
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        pages.task.cancel()
        await asyncio.to_thread(pages.look, time.time() + EXPORT_DELAY_S)  # what changed goes out before it stops
        await runner.cleanup()

    asyncio.run(run())
