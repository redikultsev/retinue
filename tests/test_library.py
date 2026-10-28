"""«Читать»: the hub's list of pages the router pushes to the platform (`library.clean`, `POST /owner/library`), the
face's `/api/library` with what the owner opened and finished, the router's side (`Lifehub.library`,
`PlatformBridge.push_library`)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from retinue import bridge as bridge_module
from retinue import library, lifehub
from retinue.config import Sphere
from test_bridge import make
from test_config import PLATFORM, load_platform
from test_face import FaceWorld, faced
from test_platformd import World, scenario, short_wait  # noqa: F401

HUB = "https://hub.in.example.test"
NOW = 1_000_000.0


def page(page_id: str = "regex-course", **over) -> dict:
    return {"id": page_id, "title": "Курс по Regex", "url": f"{HUB}/pages/{page_id}.html", "updated": "2026-10-27",
            "section": "education", "minutes": 42, **over}


# --- the list: what the router may push ------------------------------------------------------------------------

def test_a_list_is_taken_with_titles_cut_and_invisible_marks_dropped():
    pages, dropped = library.clean(
        {"pages": [page(title="Курс‮ по\nRegex"), page("cv", title="x" * 500, section=None),
                   {k: v for k, v in page("notes").items() if k not in ("section", "minutes")},
                   page("deep", section="2026-plans_v2.x" + "a" * 49)]}, HUB)
    assert dropped == []
    assert pages[0] == {**page(), "title": "Курс по Regex"}
    assert len(pages[1]["title"]) == library.TITLE_CHARS and pages[1]["section"] is None
    assert pages[2]["section"] is None and pages[2]["minutes"] is None
    assert len(pages[3]["section"]) == 64, "a Space may start with a digit and run to 64 characters"
    assert library.clean({"pages": []}, HUB) == ([], [])


@pytest.mark.parametrize("hub, url", [
    ("https://Hub.In.Example.Test", f"{HUB}/pages/regex-course.html"),
    (f"{HUB}:443", f"{HUB}/pages/regex-course.html"),
    (HUB, "https://HUB.in.example.test:443/pages/regex-course.html"),
    (f"{HUB}/", f"{HUB}/pages/regex-course.html"),
])
def test_one_origin_is_one_origin_however_it_is_written(hub, url):
    pages, dropped = library.clean({"pages": [page(url=url)]}, hub)
    assert dropped == [] and pages[0]["url"] == f"{HUB}/pages/regex-course.html", "written canonically"


@pytest.mark.parametrize("bad, why", [
    (page(), "повторяется"),
    (page(id="Regex"), "id"),
    (page(id="../x"), "id"),
    ({**page("x"), "description": "о чём"}, "поля"),
    ({**page("x"), "body": "<html>"}, "поля"),
    ({k: v for k, v in page("x").items() if k != "url"}, "поля"),
    (page("x", url="http://hub.in.example.test/pages/x.html"), "url"),
    (page("x", url="https://evil.example/pages/x.html"), "url"),
    (page("x", url="https://hub.in.example.test.evil.example/pages/x.html"), "url"),
    (page("x", url="https://hub.in.example.test@evil.example/pages/x.html"), "url"),
    (page("x", url="javascript:alert(1)//https://hub.in.example.test/pages/x.html"), "url"),
    (page("x", url=f"{HUB}:8443/pages/x.html"), "url"),
    (page("x", url=f"{HUB}:99999/pages/x.html"), "url"),
    (page("x", url=f"{HUB}/pages/other.html"), "url"),
    (page("x", url=f"{HUB}/pages/x.html?x=1"), "url"),
    (page("x", url=f"{HUB}/pages/x.html?"), "url"),
    (page("x", url=f"{HUB}/pages/x.html#top"), "url"),
    (page("x", url=f"{HUB}/now/"), "url"),
    (page("x", url=["x"]), "url"),
    (page("x", title=""), "title"),
    (page("x", title="\u200b"), "title"),
    (page("x", title=["Regex"]), "title"),
    (page("x", updated="вчера"), "updated"),
    (page("x", updated="2026-13-01"), "updated"),
    (page("x", updated=20261027), "updated"),
    (page("x", section="Образование"), "section"),
    (page("x", section="a" * 65), "section"),
    (page("x", section="-x"), "section"),
    (page("x", minutes=True), "minutes"),
    (page("x", minutes=0), "minutes"),
    (page("x", minutes=1.5), "minutes"),
])
def test_a_page_off_the_contract_is_dropped_and_named_and_the_rest_stand(bad, why):
    pages, dropped = library.clean({"pages": [page(), bad, page("cv")]}, HUB)
    assert [p["id"] for p in pages] == ["regex-course", "cv"]
    [one] = dropped
    assert why in one["why"] and one["id"] in ("x", "regex-course", "?")


@pytest.mark.parametrize("body, why", [
    ([], "pages"),
    ({"pages": "x"}, "pages"),
    ({"pages": [], "bot": "x"}, "pages"),
    ({"pages": [page(), "regex"]}, "объектов"),
    ({"pages": [page(f"p{n}") for n in range(library.PAGES_MAX + 1)]}, "больше"),
])
def test_a_list_off_its_shape_or_over_its_cap_is_refused_whole(body, why):
    with pytest.raises(library.Refused, match=why):
        library.clean(body, HUB)


def test_without_the_hubs_origin_no_list_is_taken():
    for hub in ("", "http://hub.in.example.test", "https://user@hub.in.example.test"):
        with pytest.raises(library.Refused, match="hub_origin"):
            library.clean({"pages": [page()]}, hub)


def test_the_hubs_origin_is_the_platforms_own_config(tmp_path, monkeypatch):
    assert load_platform(tmp_path, monkeypatch).hub_origin == "", "off unless named"
    assert load_platform(tmp_path, monkeypatch, f"hub_origin: {HUB}/\n" + PLATFORM).hub_origin == HUB
    assert load_platform(tmp_path, monkeypatch, "hub_origin: https://Hub.In.Example.Test:443\n" + PLATFORM) \
        .hub_origin == f"{HUB}:443", "any case, a port: the library compares origins normalised"
    for given in ("http://hub.in.example.test", "https://hub.in.example.test/pages", "hub.in.example.test"):
        with pytest.raises(SystemExit, match="hub_origin"):
            load_platform(tmp_path, monkeypatch, f"hub_origin: {given}\n" + PLATFORM)


def test_the_router_pushes_the_list_signed_and_a_refused_one_leaves_the_last_in_place(tmp_path, caplog):
    async def body(world: World):
        assert (await world.post("/owner/library", {"pages": []}, {}))[0] == 401, "the router's token only"
        status, said = await world.post("/owner/library", {"pages": [page()]})
        assert status == 400 and "hub_origin" in said["error"], "no origin to hold the addresses to"
        world.cfg.hub_origin = HUB
        assert await world.owner("library", {"pages": [page(), page("cv")]}) == {"ok": True, "pages": 2,
                                                                                  "dropped": []}
        status, said = await world.post("/owner/library", {"pages": "x"})
        assert status == 400 and "pages" in said["error"]
        status, said = await world.post("/owner/library", {"pages": [page(title="x" * 600_000)]})
        assert status == 413, "a list has no room for a body"
        pages, at = world.platform.library.pages()
        assert [p["id"] for p in pages] == ["regex-course", "cv"] and at == world.clock.now

        said = await world.owner("library", {"pages": [page(), page("evil", url="https://evil.example/x.html")]})
        assert said["pages"] == 1 and said["dropped"][0]["id"] == "evil" and "url" in said["dropped"][0]["why"]
        assert [p["id"] for p in world.platform.library.pages()[0]] == ["regex-course"], "the bad page alone goes"
        assert "library: page evil dropped" in caplog.text

        await world.owner("library", {"pages": []})
        assert world.platform.library.pages()[0] == [], "replaced whole: the hub has no pages any more"

    scenario(tmp_path, body)


# --- the face: the list, «открыл», «дочитал» ------------------------------------------------------------------

def test_the_face_lists_the_pages_with_what_was_opened_and_finished_the_same_on_every_device(tmp_path):
    async def body(world: FaceWorld):
        assert (await world.call("GET", "/api/library"))[0] == 401, "nothing without a login"
        await world.login()
        assert await world.get("/api/library") == {"schema": 1, "hub": "", "at": None, "pages": []}
        world.cfg.hub_origin = HUB
        world.cfg.spheres = [Sphere("education", "Учёба", "notes", "🎓")]
        await world.owner("library", {"pages": [page(), page("notes", section="travel")]})
        got = await world.get("/api/library")
        assert got["hub"] == HUB and got["at"] == world.clock.now
        assert got["pages"][0] == {**page(), "section_title": "Учёба", "section_icon": "🎓", "opened": None,
                                   "done": None}
        assert got["pages"][1]["section_title"] == "travel" and got["pages"][1]["section_icon"] is None, \
            "a Space no sphere names goes by its folder"

        status, said, _ = await world.call("POST", "/api/library/regex-course/open", {}, headers={})
        assert status == 403, "a change needs the app's own header"
        status, said, _ = await world.call("POST", "/api/library/regex-course/open", {})
        assert status == 200 and said["page"]["opened"] == world.clock.now and said["page"]["done"] is None
        world.clock.now += 3600
        status, said, _ = await world.call("POST", "/api/library/regex-course/done", {"done": True})
        assert status == 200 and said["page"]["done"] == world.clock.now
        assert said["page"]["opened"] == world.clock.now - 3600, "finishing keeps when it was opened"
        status, said, _ = await world.call("POST", "/api/library/regex-course/done", {"done": "да"})
        assert status == 400
        status, said, _ = await world.call("POST", "/api/library/regex-course/done", {"done": False})
        assert status == 200 and said["page"]["done"] is None
        status, said, _ = await world.call("POST", "/api/library/nope/open", {})
        assert status == 404, "only a page of the list"
        response = await world.api.post("/api/library/regex-course/delete", json={},
                                        headers={"X-Retinue": "1", "Cookie": f"__Host-retinue={world.cookie}"})
        assert response.status == 404, "open and done only"

        await world.owner("library", {"pages": [page("cv")]})
        assert [p["id"] for p in (await world.get("/api/library"))["pages"]] == ["cv"]
        await world.owner("library", {"pages": [page(), page("cv")]})
        again = (await world.get("/api/library"))["pages"][0]
        assert again["opened"] == world.clock.now - 3600, "the same id published again is the same page"

    faced(tmp_path, body)


# --- the router's side ----------------------------------------------------------------------------------------

def test_the_hubs_list_names_each_page_by_its_full_address_and_no_more(tmp_path):
    hub = lifehub.Lifehub(lifehub.Data(str(tmp_path / "data")), HUB + "/", [],
                          build_status=str(tmp_path / "build.json"))
    assert hub.library() is None, "before the hub first read its pages: nothing, not an empty list"
    hub.data.write("pages.json", [
        {"id": "regex-course", "title": "Курс по Regex", "description": "о чём он", "words": 9000, "reading_min": 42,
         "topic": "education", "updated": "2026-10-27", "author": "owner", "scripts": "on",
         "url": "/pages/regex-course.html", "path": "education/knowledge/regex-course.html", "sha256": "0" * 64},
        {"id": "untitled", "title": "", "description": "", "words": 0, "reading_min": 0, "topic": "travel",
         "updated": "2026-10-20", "author": "assistant", "scripts": "off", "url": "/pages/untitled.html",
         "path": "travel/artifacts/untitled.html", "sha256": "1" * 64}])
    pages = hub.library()
    assert pages == [page(), {"id": "untitled", "title": "untitled", "url": f"{HUB}/pages/untitled.html",
                              "updated": "2026-10-20", "section": "travel"}]
    assert library.clean({"pages": pages}, HUB) == ([page(), {**pages[1], "minutes": None}], []), \
        "the platform takes it"
    (tmp_path / "build.json").write_text(json.dumps({"ok": True, "refused": [{"id": "untitled", "why": "нет <title>"}]}))
    assert [p["id"] for p in hub.library()] == ["regex-course"], "a page the build refused is a 404: not listed"
    (tmp_path / "build.json").write_text("{")
    assert len(hub.library()) == 2, "a build status it cannot read refuses nothing"


def test_the_hubs_list_is_pushed_when_it_changes_and_at_least_hourly(tmp_path, caplog):
    ch, bridge, platform = make(tmp_path)
    pages: list[dict] | None = None
    bridge.library = lambda: pages

    async def library_call(body):
        return await platform.owner("library", body)

    platform.library = library_call
    platform.answers = {"library": {"ok": True}}

    async def run():
        nonlocal pages
        assert not await bridge.push_library(1000.0), "the hub has not read its pages yet"
        pages = [page()]
        assert not await bridge.push_library(1010.0), "looked at every 30 s"
        assert await bridge.push_library(1100.0)
        assert not await bridge.push_library(1200.0), "the same list is not pushed again"
        pages = [page(), page("cv")]
        assert await bridge.push_library(1300.0)
        assert await bridge.push_library(1300.0 + bridge_module.WAITING_PUSH_S), "an hour later, changed or not"
        platform.answers = {"library": {"ok": True, "dropped": [{"id": "cv", "why": "url не страница хаба"}]}}
        pages = [page(), page("cv"), page("y")]
        assert await bridge.push_library(5000.0)
        platform.answers = {"library": {"error": "pages: не список"}}
        pages = [page("x")]
        assert await bridge.push_library(9000.0)
        assert not await bridge.push_library(9100.0), "a refused list is not hammered"

    asyncio.run(run())
    pushed = [body for call, body in platform.calls if call == "library"]
    assert pushed[0] == {"pages": [page()]} and len(pushed) == 5
    assert "refused the hub's pages" in caplog.text and "dropped the hub's page cv: url не страница хаба" in caplog.text


def test_the_list_goes_to_the_platforms_own_address_with_a_short_wait():
    seen = []

    def answer(request):
        seen.append((request.url.path, json.loads(request.content), request.extensions["timeout"]["read"]))
        return httpx.Response(200, json={"ok": True, "pages": 0})

    client = bridge_module.PlatformClient("http://platform:9500", "t", "платформа",
                                          httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    assert asyncio.run(client.library({"pages": []})) == {"ok": True, "pages": 0}
    assert seen == [("/owner/library", {"pages": []}, bridge_module.WAITING_TIMEOUT_S)]
