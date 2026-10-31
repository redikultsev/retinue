#!/usr/bin/env python3
"""The life hub's builder: the `lifehub-build` container, no network, its own user, standard library only.

Every POLL_S it looks at the router's data folder; when the data changed, it builds a new release of two halves:
`hub/` — the site, by Hugo, every page and picture checked (`gate`) — and `pages/` — the owner's own HTML pages from
the base, served by the hub under /pages/ as sandboxed documents (an opaque origin each, the CSP header): each is
rewritten to load nothing but /pages/_vendor/ (`rewrite_page`) and checked (`page_gate`). Only then `current` points at the release — one
rename, so nginx serves the old release or the new one, never half of one. A build or a hub check that fails leaves
`current` where it was and says why in build.json, which the router reads into the health line; a page that fails
its check is left out of the release, named in build.json with the reason. The last KEEP releases stay.

Paths come from the environment, so the same script runs in the tests with a fake Hugo. `build.py vendor` runs once,
when the image is built: it fetches what pages may load (vendor.json) and checks each file's sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

DATA = Path(os.environ.get("LIFEHUB_DATA", "/data"))        # the router's JSON, charts and photos (read-only)
SITE = Path(os.environ.get("LIFEHUB_SITE", "/site"))        # releases/, current, build.json — nginx serves current
SOURCE = Path(os.environ.get("LIFEHUB_SOURCE", "/app/site"))  # the Hugo site: templates, content, CSS
HUGO = os.environ.get("LIFEHUB_HUGO", "/usr/local/bin/hugo")
WORK = Path(os.environ.get("LIFEHUB_WORK", "/tmp/lifehub"))  # a fresh copy of the site and the data per build
POLL_S = 15
RETRY_S = 300            # a failed build is tried again this late even if the data did not change
KEEP = 3                 # releases kept, the live one among them
HUGO_TIMEOUT_S = 120
VENDOR = Path(os.environ.get("LIFEHUB_VENDOR", "/app/vendor"))       # what pages may load, fetched into the image
MANIFEST = Path(os.environ.get("LIFEHUB_MANIFEST", "/app/vendor.json"))  # its addresses, sha256 and the rewrites
# What a release may contain: pages, the stylesheet, pictures, re-encoded photos — and the site's own few scripts
# (static/js: relative times, the glasses theme), each byte for byte as the image ships it. Nothing else that runs.
SUFFIXES = {".html", ".css", ".svg", ".jpg", ".js"}
NEVER = {"script", "style", "iframe", "object", "embed", "form", "base", "frame", "frameset", "applet", "link"}
ADDRESSES = {"href", "src", "action", "formaction", "xlink:href", "poster", "srcset", "data"}
SCRIPT = re.compile(r"/js/([a-z0-9-]{1,40})\.js")  # the one form of <script> a page may have, with `defer`


class Failure(Exception):
    """The build or its check failed; the text goes to build.json and the owner's health line."""


class Gate(HTMLParser):
    """Everything in a page or an SVG that the site's policy would block or that would run: a script, a style, an
    event handler, an address with a scheme that runs, anything loaded from elsewhere. The one script a page may
    have is `<script src="/js/<name>.js" defer></script>` of a file the site ships, empty, nothing else on it."""

    def __init__(self, scripts: frozenset[str] = frozenset()) -> None:
        super().__init__(convert_charrefs=True)
        self.bad: list[str] = []
        self.scripts = scripts         # the names of static/js/*.js the image ships
        self.inside = ""               # in a <script>: its text must be empty

    def handle_starttag(self, tag, attrs):
        if tag == "script" and self.shipped(attrs):
            self.inside = "script"
        elif tag in NEVER and not (tag == "link" and dict(attrs).get("href") == "/css/lifehub.css"):
            self.bad.append(f"<{tag}>")
        for name, value in attrs:
            value = (value or "").strip().lower()
            if name == "style" or name.startswith("on"):
                self.bad.append(f"{tag}[{name}]")
            elif name in ADDRESSES and value.startswith(("javascript:", "data:", "vbscript:", "//")):
                self.bad.append(f"{tag}[{name}={value[:30]}]")
            elif name in ADDRESSES and "://" in value and not (tag == "a" and value.startswith("https://")):
                self.bad.append(f"{tag}[{name}] from elsewhere: {value[:60]}")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.inside = ""

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = ""

    def handle_data(self, data):
        if self.inside and data.strip():
            self.bad.append("<script> with text")

    def shipped(self, attrs) -> bool:
        """`src` of a shipped script and `defer`, nothing more: no type, no inline code, no other address."""
        found = SCRIPT.fullmatch(dict(attrs).get("src") or "")
        return (sorted(name for name, _ in attrs) == ["defer", "src"] and dict(attrs)["defer"] in (None, "")
                and bool(found) and found.group(1) in self.scripts)


def shipped() -> dict[str, bytes]:
    """The site's own scripts as the image ships them: static/js/<name>.js -> bytes."""
    folder = SOURCE / "static" / "js"
    return {p.stem: p.read_bytes() for p in sorted(folder.glob("*.js"))} if folder.is_dir() else {}


def gate(folder: Path, scripts: dict[str, bytes] | None = None) -> list[str]:
    """What is wrong with a built release, file by file; empty when it may go live. A script is let through only
    at js/<name>.js and only byte for byte the site's own (`scripts`, by default what the image ships)."""
    scripts = shipped() if scripts is None else scripts
    problems = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        where = path.relative_to(folder).as_posix()
        if path.suffix not in SUFFIXES:
            problems.append(f"{where}: no such file belongs in the hub")
        elif path.suffix == ".js":
            found = SCRIPT.fullmatch("/" + where)
            if not found or scripts.get(found.group(1)) != path.read_bytes():
                problems.append(f"{where}: a script not the site's own")
        elif path.suffix == ".jpg":
            if not path.read_bytes().startswith(b"\xff\xd8\xff"):
                problems.append(f"{where}: not a JPEG")
        elif path.suffix in (".html", ".svg"):
            check = Gate(frozenset(scripts))
            check.feed(path.read_text(errors="replace"))
            check.close()
            problems += [f"{where}: {bad}" for bad in dict.fromkeys(check.bad)]
    return problems


# --- the owner's pages: rewritten to load nothing from elsewhere, checked, sorted by whether they may run scripts ---

PAGE_MAX = 3 * 1024 * 1024    # a page with its data: pictures; fonts and libraries are vendored, never in the base
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
OPT_IN = ("lifehub", "publish")  # <meta name="lifehub" content="publish">: the owner said this file is a page
SCRIPTS = "scripts"           # pages/scripts/<id>.html may run their inline scripts; nginx serves them by /<id>.html
NEVER_PAGE = {"base", "iframe", "object", "embed", "frame", "frameset", "applet", "portal", "fencedframe"}
# Attributes that make the browser fetch something. On <a> and <area> an address is a link the owner clicks: allowed.
FETCHES = {"src", "srcset", "poster", "data", "background", "href", "xlink:href", "action", "formaction", "ping",
           "manifest", "lowsrc", "dynsrc", "longdesc", "codebase", "archive", "imagesrcset"}
LINKS = {"a", "area"}
HINTS = {"preconnect", "dns-prefetch", "prefetch", "preload", "prerender", "modulepreload"}
GOOGLE_FONTS = ("https://fonts.googleapis.com/css", "http://fonts.googleapis.com/css")
# Comments and CDATA a browser ends where Python's parser does not: what one hides, the other may run.
TRAPS = ("<!-->", "<!--->", "--!>", "<![")
PAGES_MAX = 200               # pages in one release; the rest wait, named in build.json
CSS_OUT = re.compile(r"@import|url\(\s*['\"]?\s*(?!data:)(?:[a-z][a-z0-9+.-]*:|//)", re.I)
# A page's script that does one of these can send what the page shows somewhere: not proof (it can be hidden), a
# signal. Such a page is published with its scripts off and the reason named.
TRIPWIRES = {
    "WebSocket": r"\bWebSocket\b", "EventSource": r"\bEventSource\b",
    "RTCPeerConnection": r"\b(?:webkit)?RTCPeerConnection\b|\bRTCDataChannel\b", "sendBeacon": r"\bsendBeacon\b",
    "window.open": r"\bwindow\s*\.\s*open\b", "navigation": r"\blocation\s*(?:\.\s*href\s*)?=(?!=)|"
    r"\blocation\s*\.\s*(?:assign|replace)\s*\(", "document.cookie": r"\bdocument\s*\.\s*cookie\b",
    "import()": r"\bimport\s*\(", "Worker": r"\b(?:Shared)?Worker\s*\(|\bserviceWorker\b",
    "outside address": r"\bhttps?://|[\"'`]//[a-z0-9-]+\.",
}


class Refused(Exception):
    """A page that does not go live; the text says why, for build.json."""


class Scan(HTMLParser):
    """One pass over a page with where everything is: every start tag (name, attributes, span in the text), every
    script element (span, content), the styles' text, the title. The spans let a rewrite change only what it must.

    Where Python's parser and a browser's could part, this one sees more markup, never less: inside <svg> or <math>
    a browser reads <script> and <style> as markup (an <img> there breaks out into the page), so here too; their
    content is taken from the text as it stands, not from what the parser handed over. The comment forms the two
    read differently are refused before a scan (`TRAPS`)."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.text = text
        self.lines = [0] + [m.end() for m in re.finditer("\n", text)]
        self.tags: list[tuple[str, list, int, int]] = []
        self.scripts: list[tuple[int, int, str]] = []
        self.styles: list[str] = []
        self.title = ""
        self._open: tuple[str, int, int] | None = None   # script or style: name, start of its tag, of its content
        self._title = False
        self._foreign = 0                                # how deep in <svg>/<math>
        self.feed(text)
        self.close()
        if self._open and self._open[0] == "script":  # a script never closed runs to the end of the document
            self.scripts.append((self._open[1], len(text), text[self._open[2]:]))
        elif self._open:
            self.styles.append(text[self._open[2]:])

    def at(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def set_cdata_mode(self, elem, *args, **kwargs):
        if not self._foreign:  # in SVG and MathML a script's or a style's content is markup to a browser
            super().set_cdata_mode(elem, *args, **kwargs)

    def handle_starttag(self, tag, attrs):
        start = self.at()
        end = start + len(self.get_starttag_text() or "")
        self.tags.append((tag, attrs, start, end))
        if tag in ("script", "style") and not self._open:
            self._open = (tag, start, end)
        if tag in ("svg", "math"):
            self._foreign += 1
        self._title = self._title or tag == "title"

    def handle_startendtag(self, tag, attrs):
        start = self.at()
        self.tags.append((tag, attrs, start, start + len(self.get_starttag_text() or "")))

    def handle_endtag(self, tag):
        if self._open and tag == self._open[0]:
            here = self.at()
            content = self.text[self._open[2]:here]
            if tag == "script":
                end = self.text.find(">", here)
                self.scripts.append((self._open[1], len(self.text) if end < 0 else end + 1, content))
            else:
                self.styles.append(content)
            self._open = None
        if tag in ("svg", "math") and self._foreign:
            self._foreign -= 1
        if tag == "title":
            self._title = False

    def handle_data(self, data):
        if self._title and not self._open:
            self.title += data


def outside(value: str) -> bool:
    """An address that leaves the page's origin: with a scheme (but data:) or scheme-relative."""
    value = re.sub(r"[\x00-\x20]", "", value or "").lower()
    return value.startswith("//") or (re.match(r"[a-z][a-z0-9+.-]*:", value) is not None
                                      and not value.startswith("data:"))


def addresses(name: str, value: str) -> list[str]:
    """The addresses an attribute holds: one, or each candidate of a srcset."""
    if name in ("srcset", "imagesrcset"):
        return [part.strip().split(" ")[0] for part in (value or "").split(",") if part.strip()]
    return [value or ""]


def tag_text(tag: str, attrs: list, closing: str) -> str:
    """A start tag written back from its attributes."""
    from html import escape
    return "<" + tag + "".join(f" {name}" if value is None else f' {name}="{escape(value, quote=True)}"'
                               for name, value in attrs) + closing


def rewrite_page(text: str, rules: dict, scripts: bool) -> tuple[str, list[str]]:
    """The page as it goes live, and what was changed that the owner should know. Exact CDN addresses become the
    vendored copies (in attributes and in scripts' strings alike: mermaid is loaded by a script); resource hints go;
    a Google Fonts link becomes /pages/_vendor/fonts.css when every family is vendored, otherwise it goes and the system
    font stays. With `scripts` off every script, event handler and javascript: address goes too."""
    for address, local in (rules.get("rewrite") or {}).items():
        text = text.replace(address, local)
    fonts = rules.get("fonts") or {}
    notes, edits = [], []
    scan = Scan(text)
    for tag, attrs, start, end in scan.tags:
        kept = list(attrs)
        if tag == "link":
            named = dict(attrs)
            rel = set((named.get("rel") or "").lower().split())
            href = (named.get("href") or "").strip()
            if rel & HINTS:
                edits.append((start, end, ""))
                continue
            if href.startswith(GOOGLE_FONTS):
                from urllib.parse import parse_qs, urlsplit
                families = [family.split(":")[0].strip() for family in parse_qs(urlsplit(href).query).get("family", [])]
                if not (families and fonts.get("css") and set(families) <= set(fonts.get("families") or [])):
                    edits.append((start, end, ""))
                    notes.append("fonts not in the hub's set — a system font")
                    continue
                kept = [(n, fonts["css"] if n == "href" else v) for n, v in attrs if n != "crossorigin"]
        if not scripts:
            kept = [(n, v) for n, v in kept if not n.startswith("on")
                    and not (n in FETCHES and re.sub(r"[\x00-\x20]", "", v or "").lower().startswith("javascript:"))]
        if kept != attrs and tag != "script":
            closing = "/>" if text[start:end].rstrip(">").endswith("/") else ">"
            edits.append((start, end, tag_text(tag, kept, closing)))
    if not scripts:
        edits += [(start, end, "") for start, end, _ in scan.scripts]
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, notes


def page_gate(text: str, scripts: bool) -> tuple[list[str], list[str]]:
    """(what keeps the page from going live, the tripwires its scripts hit). The page runs sandboxed (an opaque
    origin) with no network (connect-src 'none'); this is what the policy would not stop or would only stop
    silently: a frame, a <base>, a refresh to elsewhere, anything loaded from another host."""
    bad: list[str] = []
    if any(trap in text for trap in TRAPS):
        return ["a comment or CDATA a browser reads another way (<!-->, <!--->, --!>, <![)"], []
    scan = Scan(text)
    if not any(tag == "meta" and (dict(attrs).get("name") or "").lower() == OPT_IN[0]
               and (dict(attrs).get("content") or "").strip().lower() == OPT_IN[1] for tag, attrs, *_ in scan.tags):
        bad.append(f'no <meta name="{OPT_IN[0]}" content="{OPT_IN[1]}">')
    if not scan.title.strip():
        bad.append("no <title>")
    for tag, attrs, *_ in scan.tags:
        named = {name: value or "" for name, value in attrs}
        if tag in NEVER_PAGE:
            bad.append(f"<{tag}>")
            continue
        if tag == "meta" and named.get("http-equiv", "").strip().lower() in ("refresh", "set-cookie"):
            bad.append(f"<meta http-equiv={named['http-equiv'].strip().lower()}>")
        for name, value in attrs:
            if name == "style" and CSS_OUT.search(value or ""):
                bad.append("style=\"…\": loads from outside")
            if name in FETCHES and not (tag in LINKS and name == "href"):
                if any(outside(a) and not a.strip().lower().startswith("javascript:") for a in addresses(name, value)):
                    bad.append(f"{name}=: loads from outside")  # the name is one of FETCHES: nothing of the page's
    for style in scan.styles:
        if CSS_OUT.search(style):
            bad.append("<style>: loads from outside")
    wires: list[str] = []
    if scripts:
        code = [content for *_, content in scan.scripts]
        code += [value or "" for _, attrs, *_ in scan.tags for name, value in attrs if name.startswith("on")]
        code += [value or "" for _, attrs, *_ in scan.tags for name, value in attrs
                 if name in FETCHES and re.sub(r"[\x00-\x20]", "", value or "").lower().startswith("javascript:")]
        joined = "\n".join(code)
        wires = [name for name, pattern in TRIPWIRES.items() if re.search(pattern, joined)]
    return list(dict.fromkeys(bad)), wires


def publish_pages(data: Path, out: Path, rules: dict) -> tuple[list[dict], list[dict]]:
    """Every page the router listed (data/pages.json, its files in data/pages/), rewritten and checked into `out`:
    `<id>.html` with scripts off, `scripts/<id>.html` with them on. Returns (the pages live, as the hub lists them;
    the ones refused — the router's own refusals (data/pages-refused.json) among them). A refusal names the page's
    id and the check, in fixed words: build.json goes into the health line, which the model reads."""
    def listed(name: str) -> list:
        try:
            found = json.loads((data / name).read_text())
        except (FileNotFoundError, ValueError):
            return []
        return found if isinstance(found, list) else []

    live, refused, seen = [], [], set()
    for entry in listed("pages-refused.json"):
        if isinstance(entry, dict) and isinstance(entry.get("why"), str):
            page_id = str(entry.get("id") or "")
            refused.append({"id": page_id if PAGE_ID.fullmatch(page_id) else "?", "why": entry["why"][:200]})
    for number, entry in enumerate(listed("pages.json")):
        page_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        try:
            if not PAGE_ID.fullmatch(page_id):
                page_id = "?"
                raise Refused("id malformed")
            if page_id in seen:
                raise Refused("id repeated")
            seen.add(page_id)
            if number >= PAGES_MAX:
                raise Refused(f"more than {PAGES_MAX} pages")
            source = data / "pages" / f"{page_id}.html"
            if not source.is_file():
                raise Refused("no file")
            raw = source.read_bytes()
            if len(raw) > PAGE_MAX:
                raise Refused(f"over {PAGE_MAX // 1024 // 1024} MB")
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):  # the router is between two writes
                raise Refused("the file is not the one in pages.json")
            try:
                original = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise Refused("not UTF-8") from None
            scripts = entry.get("scripts") == "on"
            text, notes = rewrite_page(original, rules, scripts)
            bad, wires = page_gate(text, scripts)
            if wires and not bad:  # a signal, not a verdict: the page goes up, its scripts do not run
                scripts = False
                notes.append(f"scripts off: {', '.join(wires)}")
                text, _ = rewrite_page(original, rules, scripts)
                bad, _ = page_gate(text, scripts)
            if bad:
                raise Refused("; ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))
        except Refused as exc:
            refused.append({"id": page_id, "why": str(exc)})
            continue
        target = out / SCRIPTS / f"{page_id}.html" if scripts else out / f"{page_id}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        live.append({**entry, "scripts": "on" if scripts else "off", "notes": notes})
    return live, refused


def vendor_rules() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def fetch_vendor(manifest: Path, dest: Path, fetch=None) -> None:
    """At image build, with network: every file of vendor.json, checked against its sha256 before it is kept."""
    import urllib.request
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=120).read())
    for item in json.loads(manifest.read_text())["files"]:
        name = item["file"]
        if name.startswith("/") or ".." in name.split("/"):
            raise SystemExit(f"vendor: {name}: not a plain path")
        data = fetch(item["from"])
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise SystemExit(f"vendor: {name}: sha256 does not match")
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_bytes(data)


def fingerprint(folder: Path) -> str:
    """The data as it is now: every file's path and bytes. A file being written (.tmp-…) is not there yet."""
    digest = hashlib.sha256()
    if folder.is_dir():
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            relative = path.relative_to(folder).as_posix()
            if any(part.startswith(".") for part in relative.split("/")):
                continue
            digest.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build(now: float, data_hash: str) -> tuple[str, list[dict], list[dict]]:
    """One release from the data as it is: the pages into releases/<stamp>.tmp/pages, Hugo into …/hub with the list
    of the pages that went live, the gate, then live. Returns (release, pages live, pages refused). Raises Failure."""
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(SOURCE, WORK)
    shutil.copytree(DATA, WORK / "assets", dirs_exist_ok=True,  # the pages' HTML is not Hugo's to read
                    ignore=lambda folder, names: [n for n in names if n.startswith(".")
                                                  or (Path(folder) == DATA and n == "pages")])
    releases = SITE / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + f"-{data_hash[:8]}"
    target = releases / f"{name}.tmp"
    shutil.rmtree(target, ignore_errors=True)
    try:
        pages = target / "pages"
        pages.mkdir(parents=True)
        live, refused = publish_pages(DATA, pages, vendor_rules())
        (WORK / "assets" / "pages.json").write_text(json.dumps(live, ensure_ascii=False))
        if VENDOR.is_dir():
            shutil.copytree(VENDOR, pages / "_vendor")
        run = subprocess.run([HUGO, "build", "--source", str(WORK), "--destination", str(target / "hub"), "--noBuildLock",
                              "--cacheDir", str(WORK.parent / "hugo-cache")], capture_output=True, text=True,
                             timeout=HUGO_TIMEOUT_S, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(WORK)})
        if run.returncode != 0:
            said = (run.stderr or run.stdout).strip().splitlines()
            raise Failure(f"Hugo: {said[-1][:300] if said else f'code {run.returncode}'}")
        problems = gate(target / "hub")
        if problems:
            raise Failure(f"page check: {'; '.join(problems[:5])}" + (f" and {len(problems) - 5} more"
                                                                            if len(problems) > 5 else ""))
        os.rename(target, releases / name)
    except subprocess.TimeoutExpired:
        raise Failure(f"Hugo did not finish in {HUGO_TIMEOUT_S} s") from None
    finally:
        shutil.rmtree(target, ignore_errors=True)
    link = SITE / "current.tmp"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(f"releases/{name}", link)  # relative: nginx sees the folder under another path
    os.replace(link, SITE / "current")
    for old in sorted(p for p in releases.iterdir() if p.is_dir())[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return name, live, refused


def report(**status) -> None:
    """build.json, replaced whole: the router reads it."""
    SITE.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=SITE, prefix=".build-")
    with os.fdopen(handle, "w") as file:
        json.dump(status, file, ensure_ascii=False)
    os.chmod(temporary, 0o644)
    os.replace(temporary, SITE / "build.json")


def step(state: dict, now: float) -> bool:
    """One look: build when the data changed (or a failed build is due again). Returns whether it built."""
    data_hash = fingerprint(DATA)
    due = data_hash != state.get("hash") or (state.get("failed") and now - state["failed"] >= RETRY_S)
    if not due and (SITE / "current").exists():
        return False
    state["hash"] = data_hash
    try:
        release, live, refused = build(now, data_hash)
    except (Failure, OSError, subprocess.SubprocessError) as exc:
        state["failed"] = now
        report(ok=False, at=now, error=str(exc)[:500], release=os.readlink(SITE / "current")
               if (SITE / "current").is_symlink() else None)
        print(f"lifehub: build failed: {exc}", file=sys.stderr, flush=True)
        return False
    state["failed"] = 0
    report(ok=True, at=now, error="", release=release, pages=len(live), refused=refused)
    print(f"lifehub: {release} is live, {len(live)} pages" + (f", {len(refused)} refused" if refused else ""),
          flush=True)
    return True


def main() -> None:
    if sys.argv[1:] == ["vendor"]:  # the image's build step: the only time this script reaches the network
        fetch_vendor(MANIFEST, VENDOR)
        return
    state: dict = {}
    print(f"lifehub builder: {DATA} -> {SITE}", flush=True)
    while True:
        step(state, time.time())
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
