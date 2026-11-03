"""Projects of any depth (`retinue/spaces.py`, docs/base.md «Projects»): any folder of the Base with an `AGENTS.md`
is a project, projects nest; a session started in one runs in its folder and gets the rules of every level from the
Base's root down to it and none of a sibling (`retinue/instructions.py`); its screen lists all its files and its
sub-projects, and the app opens its pictures, PDFs, recordings and films — through git, never through the tree."""

from __future__ import annotations

import asyncio

import pytest

from retinue import instructions, spaces
from retinue.config import BaseConfig
from retinue.spaces import BaseTree, Refused, cwd_project, project_cwd, project_of, project_path, sub_projects

from test_face import FaceWorld, faced, input_id
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse
from test_spaces import make_base, reader
from test_spheres import BaseWorld, finished

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(2000)
M4A = b"\x00\x00\x00\x18ftypM4A " + bytes(500)

# The root, a Space, a project in it, one three folders deep; a sibling at every level and a project below.
BASE = {
    "AGENTS.md": "# The Base\n\nroot rules\n",
    "acme/AGENTS.md": "# Acme\n\nacme rules\n",
    "acme/CLAUDE.md": "acme claude file\n",
    "acme/clients/AGENTS.md": "# Clients\n\nclients rules\n",
    "acme/clients/anna/AGENTS.md": "# Anna Petrova\n\nanna rules\n\n<!-- Витрина: ниже — строки. -->\n\n"
                                   "## Открытые вопросы\n\n- Renew the contract?\n",
    "acme/clients/anna/photo.png": PNG,
    "acme/clients/anna/scan.pdf": PDF,
    "acme/clients/anna/fake.pdf": "<html><script>alert(1)</script></html>",
    "acme/clients/anna/media/call.mp4": MP4,
    "acme/clients/anna/media/voice.m4a": M4A,
    "acme/clients/anna/notes.txt": "plain notes\n",
    "acme/clients/anna/page.html": "<p>a page</p>",
    "acme/clients/anna/knowledge/terms.md": "---\ntitle: Terms\n---\n\nThe terms. See [the case](../cases/AGENTS.md).\n",
    "acme/clients/anna/data.bin": bytes(64),
    "acme/clients/anna/.secret/key.txt": "hidden\n",
    "acme/clients/anna/cases/AGENTS.md": "# Cases\n\nbelow rules\n",
    "acme/clients/anna/cases/brief.pdf": PDF,
    "acme/clients/anna/cases/deep/AGENTS.md": "# Deep\n",
    "acme/clients/boris/AGENTS.md": "# Boris\n\nsibling rules\n",
    "acme/suppliers/AGENTS.md": "# Suppliers\n\nsupplier rules\n",
    "acme/suppliers/x.md": "supplier\n",
    "scripts/lint.py": "print('lint')\n",
}
LINKS = {"acme/clients/anna/link.png": "/etc/passwd", "acme/clients/anna/up.pdf": "../../../scripts/lint.py"}
DEEP = "acme/clients/anna"


def git_of(tree: BaseTree) -> dict:
    return {"repo": tree.git, "env": tree.env}


# --- «done when»: a session three folders deep -----------------------------------------------------------------------

def test_a_project_three_folders_deep_gets_every_levels_rules_and_no_siblings(tmp_path):
    """Started from the project's screen, the session runs in the project's folder of the clone; the lane host's
    rules for that folder are every AGENTS.md from the Base's root down to it — a CLAUDE.md on the way included, not
    instead — and none of a sibling at any level, none of a project below; a fork runs where it ran."""
    tree = reader(make_base(tmp_path, BASE))

    async def body(world: BaseWorld):
        world.base.repo, world.base.env = tree.git, tree.env
        await world.login()
        status, answer, _ = await world.call("POST", "/api/sessions", {
            "input_id": input_id(), "track": "base", "project": DEEP, "text": "Draft the renewal letter"})
        assert status == 200, answer
        session = answer["session"]
        run = world.lanes["private"].runs()[-1]
        assert run["cwd"] == f"base/{DEEP}" and world.store.session(session)["cwd"] == f"base/{DEEP}"
        assert world.base.refreshes == 1, "a session in a project is a session in the Base: refreshed before"

        await finished(world, session)
        fork = await world.platform.spheres.fork("face:f1", session, "and another way?")
        assert fork["ok"], fork
        assert world.store.session(fork["session"])["cwd"] == f"base/{DEEP}", "a fork runs where its parent ran"

        for wrong, why in [({"project": "acme/clients/nobody"}, "No such project."),
                           ({"project": "acme/../scripts"}, "project: not a project's folder"),
                           ({"project": "acme", "track": "retinue"}, "does not run in the Base's projects")]:
            status, answer, _ = await world.call("POST", "/api/sessions", {
                "input_id": input_id(), "track": "base", "text": "x", **wrong})
            assert status == 400 and why in answer["error"], (wrong, answer)

        # The lane host composes the rules for that run's directory, the same files laid out in a lane's /work.
        work = tmp_path / "work"
        for path, content in BASE.items():
            target = work / "base" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        text = instructions.compose(work / run["cwd"], work, "n0nce")
        loaded = [line.removeprefix("<<<repo-file n0nce ").removesuffix(">>>") for line in text.splitlines()
                  if line.startswith("<<<repo-file n0nce ")]
        assert loaded == [str(work / "base" / p) for p in ("AGENTS.md", "acme/CLAUDE.md", "acme/AGENTS.md",
                                                           "acme/clients/AGENTS.md", f"{DEEP}/AGENTS.md")]
        for rule in ("root rules", "acme claude file", "acme rules", "clients rules", "anna rules"):
            assert rule in text, rule
        for other in ("sibling rules", "supplier rules", "below rules"):
            assert other not in text, f"{other}: a sibling's or a project's below"

    async def go():
        world = BaseWorld(tmp_path)
        await world.start()
        try:
            await body(world)
        finally:
            await world.stop()
    asyncio.run(go())


# --- «done when»: the project's screen -------------------------------------------------------------------------------

class _Base:
    tree_rel = "base"

    def __init__(self, tree: BaseTree) -> None:
        self.repo, self.env = tree.git, tree.env


async def raw(world: FaceWorld, path: str, headers: dict | None = None):
    """A file's answer as bytes, with the login."""
    from retinue.face import COOKIE
    response = await world.api.request("GET", path, headers={**(headers or {}), "Cookie": f"{COOKIE}={world.cookie}"})
    return response.status, response


def test_a_projects_screen_lists_all_its_files_and_sub_projects(tmp_path):
    work = make_base(tmp_path, BASE, links=LINKS)

    async def body(world: FaceWorld):
        assert (await world.call("GET", f"/api/projects/{DEEP}"))[0] == 401, "nothing without a login"
        assert (await world.call("GET", f"/api/base/{DEEP}/photo.png"))[0] == 401
        await world.login()
        world.platform.base = _Base(reader(work))
        mine = world.store.new_session("notes", "private", "private", 1.0, cwd=f"base/{DEEP}")
        boris = world.store.new_session("notes", "private", "private", 2.0, cwd="base/acme/clients/boris")

        answer = await world.get(f"/api/projects/{DEEP}")
        project = answer["project"]
        assert project["path"] == DEEP and project["title"] == "Anna Petrova" and project["parent"] == "acme/clients"
        assert [(f["path"], f["kind"]) for f in project["files"]] == [
            ("AGENTS.md", "markdown"), ("data.bin", "other"), ("fake.pdf", "pdf"), ("knowledge/terms.md", "markdown"),
            ("media/call.mp4", "video"), ("media/voice.m4a", "audio"), ("notes.txt", "text"), ("page.html", "html"),
            ("photo.png", "image"), ("scan.pdf", "pdf")], \
            "every file of its own, at any depth: no dot folder, no link, nothing of a sub-project"
        assert next(f for f in project["files"] if f["path"] == "photo.png")["size"] == len(PNG)
        assert project["projects"] == [{"name": f"{DEEP}/cases", "title": "Cases", "questions": 0, "tasks": 0}], \
            "its sub-projects: the one below it, not the one below that"
        assert project["counts"] == {"questions": 1, "tasks": 0}
        assert [r["path"] for r in project["records"]["knowledge"]] == ["knowledge/terms.md"]
        assert answer["sessions"] == [mine], "the sessions started in its folder, not a sibling's"

        clients = (await world.get("/api/projects/acme/clients"))["project"]
        assert [p["name"] for p in clients["projects"]] == ["acme/clients/anna", "acme/clients/boris"]
        assert clients["files"] == [{"path": "AGENTS.md", "size": len(BASE["acme/clients/AGENTS.md"]),
                                     "kind": "markdown"}]
        root = (await world.get("/api/projects"))["project"]
        assert root["path"] == "" and [p["name"] for p in root["projects"]] == ["acme"]
        grid = (await world.get("/api/spaces"))["spaces"]
        assert [s["name"] for s in grid] == ["acme"], "the Spaces: the projects right below the root"
        space = await world.get("/api/spaces/acme")
        assert [p["name"] for p in space["space"]["projects"]] == ["acme/clients", "acme/suppliers"]
        every = (await world.get("/api/spaces"))["projects"]
        assert [(p["name"], p["title"], p["parent"]) for p in every] == [
            ("acme", "Acme", ""), ("acme/clients", "Clients", "acme"), (DEEP, "Anna Petrova", "acme/clients"),
            (f"{DEEP}/cases", "Cases", DEEP), (f"{DEEP}/cases/deep", "Deep", f"{DEEP}/cases"),
            ("acme/clients/boris", "Boris", "acme/clients"), ("acme/suppliers", "Suppliers", "acme")], \
            "beside the grid, every project at any depth with the one it is in: the app's list of projects"
        assert next(p for p in every if p["name"] == DEEP)["questions"] == 1

        # A session's row names the project it runs in — its folder in the Base, on the Base's lane.
        assert "project" not in next(s for s in (await world.get("/api/sessions"))["sessions"] if s["id"] == mine), \
            "no Base configured: no project"
        world.cfg.base = BaseConfig(author="Anna Petrova <anna@example.com>")
        rows = {s["id"]: s for s in (await world.get("/api/sessions"))["sessions"]}
        assert rows[mine]["project"] == DEEP and rows[boris]["project"] == "acme/clients/boris"
        elsewhere = world.store.new_session("retinue", "code", "code", 3.0, cwd=f"base/{DEEP}")
        assert "project" not in world.store.face_session(elsewhere), "off the Base's lane: no project"
        world.cfg.base = None

        # The files themselves: what the commit holds, as their types, opened in place.
        status, response = await raw(world, f"/api/base/{DEEP}/photo.png")
        assert status == 200 and await response.read() == PNG
        assert response.headers["Content-Type"] == "image/png" and response.headers["Content-Disposition"] == "inline"
        assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        status, response = await raw(world, f"/api/base/{DEEP}/media/call.mp4", {"Range": "bytes=0-99"})
        assert status == 206 and response.headers["Content-Range"] == f"bytes 0-99/{len(MP4)}"
        assert await response.read() == MP4[:100] and response.headers["Content-Type"] == "video/mp4"
        status, response = await raw(world, f"/api/base/{DEEP}/data.bin")
        assert status == 200 and response.headers["Content-Disposition"].startswith("attachment;")
        assert response.headers["Content-Type"] == "application/octet-stream"
        status, response = await raw(world, f"/api/base-view/{DEEP}/scan.pdf")
        assert status == 200 and await response.read() == PDF and response.headers["Content-Type"] == "application/pdf"
        assert response.headers["Content-Security-Policy"] == f"frame-ancestors {world.cfg.face_origin}"

    faced(tmp_path, body)


def test_a_projects_files_are_refused_outside_the_base_through_a_link_hidden_or_disguised(tmp_path):
    """The same refusals as a Record's: no path out of the Base, no link (one that points out, one that points to a
    file inside), no dot folder, nothing outside every project; and the frame takes only a page or a real PDF."""
    work = make_base(tmp_path, BASE, links=LINKS)

    async def body(world: FaceWorld):
        await world.login()
        world.platform.base = _Base(reader(work))
        for path in [f"/api/base/{DEEP}/link.png", f"/api/base/{DEEP}/up.pdf", f"/api/base-view/{DEEP}/up.pdf",
                     f"/api/base/{DEEP}/.secret/key.txt", f"/api/base/{DEEP}/%2Esecret/key.txt",
                     f"/api/base/{DEEP}/..%2F..%2F..%2Fscripts%2Flint.py", "/api/base/%2Fetc%2Fpasswd",
                     f"/api/base/{DEEP}/missing.png", f"/api/base-view/{DEEP}/fake.pdf",
                     f"/api/base-view/{DEEP}/photo.png", f"/api/base-view/{DEEP}/page.html", "/api/projects/acme/.hidden",
                     "/api/projects/acme/clients/nobody",
                     "/api/projects/..%2Fetc"]:
            status, data, _ = await world.call("GET", path)
            assert status == 404 and data["error"], path

    faced(tmp_path, body)
    (tmp_path / "noroot").mkdir()
    tree = reader(make_base(tmp_path / "noroot", {k: v for k, v in BASE.items() if k != "AGENTS.md"}))
    with pytest.raises(Refused) as refused:
        tree.file("scripts/lint.py")
    assert refused.value.status == 404, "a file in no project is not served"
    with pytest.raises(Refused):
        tree.project("")


def test_a_huge_file_is_listed_but_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(spaces, "FILE_MAX", 1000)
    tree = reader(make_base(tmp_path, {"acme/AGENTS.md": "# Acme\n", "acme/film.mp4": MP4}))
    [_, film] = tree.project("acme")[1]["files"]
    assert film == {"path": "film.mp4", "size": len(MP4), "kind": "video", "big": True}
    with pytest.raises(Refused) as refused:
        tree.file("acme/film.mp4")
    assert refused.value.status == 413


# --- the reader --------------------------------------------------------------------------------------------------------

def test_records_and_links_belong_to_the_nearest_project(tmp_path):
    tree = reader(make_base(tmp_path, BASE))
    made = tree.commit()
    assert tree.route(made, f"{DEEP}/AGENTS.md") == "/spaces/acme/p/clients/anna"
    assert tree.route(made, "acme/clients") == "/spaces/acme/p/clients"
    assert tree.route(made, "acme/AGENTS.md") == "/spaces/acme"
    assert tree.route(made, f"{DEEP}/knowledge/terms.md") == "/spaces/acme/r/clients/anna/knowledge/terms.md"
    assert tree.route(made, "AGENTS.md") is None, "the root's Showcase has no page of its own"
    _, record = tree.record("acme", "clients/anna/knowledge/terms.md")
    assert record["project"] == DEEP and record["nature"] == "knowledge" and record["title"] == "Terms"
    assert "[the case](/spaces/acme/p/clients/anna/cases)" in record["body"]
    assert tree.record("acme", "suppliers/x.md")[1]["project"] == "acme/suppliers"
    with pytest.raises(Refused):
        tree.record("acme", "clients/anna/cases/brief.pdf")
    assert "clients/anna/knowledge/terms.md" not in str(tree.space("acme")[1]["records"]), \
        "a sub-project's Records are its own, not its Space's"


def test_project_paths_and_the_sessions_folder():
    projects = frozenset({"", "acme", "acme/clients", "acme/clients/anna", "orphan/deep"})
    assert project_of(projects, "acme/clients/anna/media") == "acme/clients/anna"
    assert project_of(projects, "acme/x") == "acme" and project_of(projects, "scripts") == ""
    assert project_of(projects - {""}, "scripts") is None
    assert sub_projects(projects, "") == ["acme", "orphan/deep"] and sub_projects(projects, "acme") == ["acme/clients"]
    assert sub_projects(projects - {""}, None) == ["acme", "orphan/deep"]
    for bad in ["../x", "a//b", "a/.b", ".a", "A", "a b", "a/" * 9 + "a", "/a", None, 7]:
        assert project_path(bad) is None, bad
    assert project_path("") == "" and project_path("acme/clients/anna") == "acme/clients/anna"
    assert project_cwd("base", "acme/clients") == "base/acme/clients" and project_cwd("base/", "") == "base"
    assert cwd_project("base", "base/acme/clients") == "acme/clients" and cwd_project("base", "base") == ""
    assert cwd_project("base", "basement/x") is None and cwd_project("base", "") is None
