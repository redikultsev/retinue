"""The life hub's Hugo site, read as files: nothing in it runs code, reads the environment, fetches, or writes markup
or style from data. No Hugo here — the build container's own check runs on every build (deploy/lifehub/build.py)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "deploy" / "lifehub" / "site"
TEMPLATES = sorted(p for p in (SITE / "layouts").rglob("*.html")) + sorted((SITE / "content").rglob("*.gotmpl"))


def test_the_site_runs_nothing_reads_nothing_and_fetches_nothing():
    config = tomllib.loads((SITE / "hugo.toml").read_text())
    security = config["security"]
    assert security["enableInlineShortcodes"] is False
    assert security["exec"] == {"allow": ["none"], "osEnv": ["none"]}
    assert security["funcs"] == {"getenv": ["none"]}
    assert security["http"] == {"methods": ["none"], "urls": ["none"], "mediaTypes": ["none"]}
    assert security["node"]["permissions"] == {name: ["none"] for name in (
        "allowAddons", "allowChildProcess", "allowRead", "allowWorker")}
    assert "! ^text/html$" in security["allowcontent"], "an HTML content file is never rendered as it is"
    assert config["markup"]["goldmark"]["renderer"]["unsafe"] is False
    assert config["disableHugoGeneratorInject"] is True and "404" in config["disableKinds"]
    assert config["module"]["hugoVersion"]["min"] == "0.167.0", "the release that escapes {id=...} in a TOC"


def test_no_template_writes_markup_style_or_an_address_from_data():
    """The page policy has no 'unsafe-inline': not one style="" or inline script. Data is escaped: no safe*, no
    markdown rendering of what the router wrote. An address in an href is a page's own permalink, a fixed path, or
    a travel-ops link through the one partial that checks it."""
    forbidden = re.compile(r"style=|<style|<script|\bon[a-z]+=|safeHTML|safeHTMLAttr|safeCSS|safeJS|safeURL|"
                           r"markdownify|RenderString|htmlUnescape|RawContent|GetRemote|getenv|readFile|ReadDir")
    for path in TEMPLATES:
        text = path.read_text()
        assert not forbidden.search(text), f"{path.name}: {forbidden.search(text).group()}"
        for href in re.findall(r'(?:href|src)="([^"]*)"', text):
            fixed = href.startswith("/") and "{{" not in href
            checked = path.name == "link.html" and href == "{{ .url }}"
            assert fixed or checked or href == "{{ .RelPermalink }}", f"{path.name}: {href}"
    link = (SITE / "layouts" / "_partials" / "link.html").read_text()
    assert 'if and .ok (eq $u.Scheme "https") (in ($site.link_hosts | default slice) $u.Host)' in link
    assert '<a href="{{ .url }}" rel="noopener noreferrer">{{ .url }}</a>' in link, "the full address, no referrer"
    css = (SITE / "static" / "css" / "lifehub.css").read_text()
    assert "url(" not in css and "@import" not in css, "nothing loaded from anywhere"
    adapter = (SITE / "content" / "trips" / "_content.gotmpl").read_text()
    assert 'findRE "^[0-9a-f]{16}$" $trip.id' in adapter, "a page's path is its random id and nothing else"


def test_every_page_of_the_first_version_is_there():
    """«Сейчас», trips, status; «Работа», finance and health are «скоро» (decision 6)."""
    base = (SITE / "layouts" / "baseof.html").read_text()
    assert re.findall(r'<a href="(/[a-z/]*)">', base) == ["/", "/trips/", "/status/", "/work/", "/finance/", "/health/"]
    assert '<meta name="referrer" content="no-referrer">' in base and 'href="/css/lifehub.css"' in base
    for name in ("work", "finance", "health"):
        assert "soon: true" in (SITE / "content" / f"{name}.md").read_text()
    assert "layout: status" in (SITE / "content" / "status.md").read_text()
    home = (SITE / "layouts" / "home.html").read_text()
    for part in ("$now.trips", "$now.today", "$now.tomorrow", "$now.reminders", "$now.mail", "$now.deadlines",
                 "$now.health", "без срока"):
        assert part in home, part
    status = (SITE / "layouts" / "status.html").read_text()
    for part in ("$s.limit", "$s.tokens", "$s.connectors", "$s.mail", ".rules", "$s.charts", "оценка CLI"):
        assert part in status, part
