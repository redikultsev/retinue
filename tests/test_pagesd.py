"""The pages service (retinue/pagesd.py): the owner's host behind Traefik's key, a page only in its shell's frame and
sandboxed alike on both sides, what a page keeps saved for that page alone and within its limits, and the public
sites' files written for what is shared — and gone when it is not."""

import asyncio
import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from retinue import pagesd

KEY = "k" * 40
ORIGIN = "https://pages.in.example.com"
OWNER = {"X-Pages-Key": KEY}
FRAME = {**OWNER, "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"}
API = {**OWNER, "X-Pages": "1", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
COURSE = ('<!doctype html><html><head><meta charset="utf-8"><title>Regex course</title></head><body>'
          '<input type="checkbox" id="a"><script>localStorage.setItem("x", "1")</script></body></html>')
NOTES = '<!doctype html><html><head><title>Notes</title></head><body><a href="https://example.org/">out</a></body></html>'


def release(site, name, pages):
    folder = site / "releases" / name
    (folder / "scripts").mkdir(parents=True)
    (folder / "_vendor").mkdir()
    (folder / "_vendor" / "highlight.min.js").write_text("/* hl */")
    listed = []
    for page_id, (text, scripts) in pages.items():
        (folder / ("scripts" if scripts else "") / f"{page_id}.html").write_text(text)
        listed.append({"id": page_id, "title": page_id.title(), "topic": "learning", "updated": "2026-05-22",
                       "scripts": "on" if scripts else "off"})
    (folder / "pages.json").write_text(json.dumps(listed))
    link = site / "current.tmp"
    if link.is_symlink():
        link.unlink()
    os.symlink(f"releases/{name}", link)
    os.replace(link, site / "current")


@pytest.fixture
def service(tmp_path):
    site = tmp_path / "site"
    release(site, "r1", {"course": (COURSE, True), "notes": (NOTES, False)})
    cfg = pagesd.Config(origin=ORIGIN, key=KEY, public_hosts=["pages.example.com", "slides.example.com"],
                        app_origin="https://app.in.example.com", site=site, store=tmp_path / "pages.sqlite",
                        public=tmp_path / "public")
    pages = pagesd.Pages(cfg, pagesd.Store(cfg.store))
    pages.look(0)
    return pages


def call(pages, method, path, headers=None, json_body=None):
    async def go():
        async with TestClient(TestServer(pages.app())) as client:
            response = await client.request(method, path, headers=headers or {}, json=json_body)
            return response.status, dict(response.headers), await response.text()
    return asyncio.run(go())


def test_without_traefiks_key_there_is_no_site(service):
    for path in ("/", "/course", "/raw/course", "/api/share/course"):
        status, headers, _ = call(service, "GET", path, {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"})
        assert status == 404, path
    status, headers, _ = call(service, "GET", "/raw/course", {"X-Pages-Key": "wrong"})
    assert status == 404 and headers["Content-Security-Policy"].endswith("sandbox"), "an error under /raw/ too"


def test_a_page_opens_in_its_shell_sandboxed_the_same_on_both_sides(service):
    status, headers, text = call(service, "GET", "/course", OWNER)
    assert status == 200 and headers["Content-Security-Policy"] == pagesd.SHELL_CSP
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin" and headers["X-Frame-Options"] == "DENY"
    assert f'sandbox="{pagesd.SANDBOX[True]}" src="/raw/course"' in text
    assert 'href="https://app.in.example.com/pages">← Pages' in text and 'id="share"' in text
    status, headers, raw = call(service, "GET", "/raw/course", FRAME)
    policy = headers["Content-Security-Policy"]
    assert status == 200 and policy.endswith(f"sandbox {pagesd.SANDBOX[True]}"), "a permission counts only in both"
    assert "connect-src 'none'" in policy and f"frame-ancestors {ORIGIN}" in policy
    assert f"script-src {ORIGIN}/pages/_vendor/ 'unsafe-inline'" in policy and "'self'" not in policy
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert raw.index("<head>") < raw.index('define("localStorage"') < raw.index('<meta charset="utf-8">')
    assert "})({}, true);</script><meta charset" in raw


def test_the_raw_page_answers_only_its_shells_frame(service):
    for headers in ({**OWNER, "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"},
                    {**OWNER, "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"},  # a page's own frame
                    OWNER):
        status, answer, _ = call(service, "GET", "/raw/course", headers)
        assert status == 403 and answer["Content-Security-Policy"] == pagesd.BARE_CSP


def test_a_page_without_scripts_gets_no_script_and_its_links_open_a_window(service):
    status, headers, raw = call(service, "GET", "/raw/notes", FRAME)
    assert status == 200 and "<script" not in raw and raw.startswith(
        '<!doctype html><html><head><base target="_blank"><title>')
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Content-Security-Policy"].endswith("sandbox allow-popups allow-popups-to-escape-sandbox")
    assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in call(service, "GET", "/notes", OWNER)[2]
    status, _, said = call(service, "PUT", "/api/store/notes", API, {"ops": [{"op": "set", "key": "ls:a", "value": "1"}]})
    assert status == 403 and "keeps nothing" in said


def test_what_a_page_saves_comes_back_with_it_and_only_with_it(service):
    status, _, said = call(service, "PUT", "/api/store/course", API, {"ops": [
        {"op": "set", "key": "ls:progress", "value": '{"done":3}'},
        {"op": "set", "key": "st:note", "value": "</script><script>alert(1)</script>"},
        {"op": "set", "key": "ls:gone", "value": "x"}, {"op": "del", "key": "ls:gone"}]})
    assert status == 200 and json.loads(said)["keys"] == 2
    raw = call(service, "GET", "/raw/course", FRAME)[2]
    assert '"ls:progress":"{\\"done\\":3}"' in raw
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)" in raw and "alert(1)</script>" not in raw
    assert "ls:gone" not in raw
    assert service.store.values("notes") == {}, "another page's values are its own"


def test_a_save_is_the_shells_alone(service):
    body = {"ops": [{"op": "set", "key": "ls:a", "value": "1"}]}
    assert call(service, "PUT", "/api/store/course", {**API, "X-Pages": ""}, body)[0] == 403
    assert call(service, "PUT", "/api/store/course", {**API, "Origin": "null"}, body)[0] == 403, "a sandboxed page"
    assert call(service, "PUT", "/api/store/course", {**API, "Origin": "https://evil.example.org"}, body)[0] == 403
    assert call(service, "PUT", "/api/store/course", {**API, "Sec-Fetch-Site": "cross-site"}, body)[0] == 403
    assert call(service, "PUT", "/api/store/missing", API, body)[0] == 404
    assert service.store.values("course") == {}


@pytest.mark.parametrize("ops, status", [
    ([{"op": "set", "key": "ls:a", "value": "x" * (pagesd.VALUE_MAX + 1)}], 413),
    ([{"op": "set", "key": "cookie:a", "value": "1"}], 400),
    ([{"op": "set", "key": "ls:" + "k" * pagesd.KEY_MAX, "value": "1"}], 400),
    ([{"op": "set", "key": "ls:a", "value": 1}], 400),
    ([], 400),
])
def test_a_save_off_its_shape_or_over_a_limit_is_refused_whole(service, ops, status):
    answer = call(service, "PUT", "/api/store/course", API, {"ops": [{"op": "set", "key": "ls:first", "value": "1"},
                                                                     *ops]} if ops else {"ops": ops})
    assert answer[0] == status and service.store.values("course") == {}, "nothing of a refused save is kept"


def test_a_page_keeps_at_most_its_share_of_bytes(service, monkeypatch):
    monkeypatch.setattr(pagesd, "PAGE_BYTES", 100)
    ok = call(service, "PUT", "/api/store/course", API, {"ops": [{"op": "set", "key": "ls:a", "value": "x" * 50}]})
    over = call(service, "PUT", "/api/store/course", API, {"ops": [{"op": "set", "key": "ls:b", "value": "x" * 60}]})
    assert ok[0] == 200 and over[0] == 413 and "at most" in over[2]
    assert service.store.values("course") == {"ls:a": "x" * 50}


def test_a_shared_page_is_written_out_with_its_values_and_follows_them(service, tmp_path):
    assert call(service, "PUT", "/api/share/course", API, {"host": "evil.example.org"})[0] == 400
    call(service, "PUT", "/api/store/course", API, {"ops": [{"op": "set", "key": "ls:done", "value": "3"}]})
    status, _, said = call(service, "PUT", "/api/share/course", API, {"host": "pages.example.com"})
    assert status == 200 and json.loads(said)["shared"] == [
        {"host": "pages.example.com", "home": False, "url": "https://pages.example.com/course"}]
    site = tmp_path / "public" / "pages.example.com"
    shell = (site / "s" / "course.html").read_text()
    assert 'data-mode="public"' in shell and 'src="/raw/course"' in shell and 'id="share"' not in shell
    raw = (site / "r" / "scripts" / "course.html").read_text()
    assert '"ls:done":"3"' in raw and "}, false);</script>" in raw, "read-only there: a visitor's changes stay his"
    assert (site / "_pages" / "shell.js").is_file() and (site / "pages" / "_vendor" / "highlight.min.js").is_file()
    call(service, "PUT", "/api/store/course", API, {"ops": [{"op": "set", "key": "ls:done", "value": "4"}]})
    service.look(10 ** 10)
    assert '"ls:done":"4"' in (site / "r" / "scripts" / "course.html").read_text(), "the change is out within seconds"
    call(service, "PUT", "/api/share/notes", API, {"host": "slides.example.com", "home": True})
    assert (tmp_path / "public" / "slides.example.com" / "s" / "_home.html").is_file()
    assert (tmp_path / "public" / "slides.example.com" / "r" / "_home.html").is_file()
    status, _, said = call(service, "DELETE", "/api/share/course", API, {"host": "pages.example.com"})
    assert status == 200 and json.loads(said)["shared"] == []
    assert not (site / "s" / "course.html").exists() and not (site / "r" / "scripts" / "course.html").exists()


def test_a_page_that_leaves_the_base_leaves_its_public_site_and_comes_back_with_it(service, tmp_path):
    call(service, "PUT", "/api/share/course", API, {"host": "pages.example.com"})
    release(tmp_path / "site", "r2", {"notes": (NOTES, False)})
    service.look(1)
    site = tmp_path / "public" / "pages.example.com"
    assert not (site / "s" / "course.html").exists() and not (site / "r" / "scripts" / "course.html").exists()
    assert call(service, "GET", "/course", OWNER)[0] == 404
    release(tmp_path / "site", "r3", {"course": (COURSE, True)})
    service.look(2)
    assert (site / "s" / "course.html").is_file(), "the share is kept; the page is out again"


def test_the_sweep_leaves_only_what_is_shared_on_sites_that_exist(service, tmp_path):
    stray = tmp_path / "public" / "old.example.com" / "s"
    stray.mkdir(parents=True)
    (stray / "x.html").write_text("old")
    left = tmp_path / "public" / "pages.example.com" / "s" / "forgotten.html"
    left.parent.mkdir(parents=True, exist_ok=True)
    left.write_text("old")
    service.sweep()
    assert not (tmp_path / "public" / "old.example.com").exists() and not left.exists()


def test_vendored_files_are_the_only_ones_with_cors(service):
    status, headers, text = call(service, "GET", "/pages/_vendor/highlight.min.js", OWNER)
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*" and text == "/* hl */"
    assert call(service, "GET", "/pages/_vendor/../pages.json", OWNER)[0] == 404
    for path in ("/", "/course"):
        assert "Access-Control-Allow-Origin" not in call(service, "GET", path, OWNER)[1]


@pytest.mark.parametrize("env, why", [
    ({"PAGES_ORIGIN": "http://pages.in.example.com", "PAGES_KEY": KEY}, "PAGES_ORIGIN"),
    ({"PAGES_ORIGIN": ORIGIN, "PAGES_KEY": "short"}, "PAGES_KEY"),
    ({"PAGES_ORIGIN": ORIGIN, "PAGES_KEY": KEY, "PAGES_PUBLIC_HOSTS": "pages.in.example.com"}, "public host"),
    ({"PAGES_ORIGIN": ORIGIN, "PAGES_KEY": KEY, "PAGES_PUBLIC_HOSTS": "example.com/x"}, "public host"),
])
def test_the_config_is_refused_off_its_shape(env, why):
    with pytest.raises(SystemExit, match=why):
        pagesd.Config.from_env(env)


def test_frame_js_is_injected_whole():
    raw = pagesd.inject("<p>no head</p>", True, {"ls:a": "1"}, keep=True)
    assert raw.startswith("<script>/*") and "(function (DATA, KEEP) {" in raw and '})({"ls:a":"1"}, true);</script><p>' in raw
    assert "</script" not in (pagesd.STATIC / "frame.js").read_text()
