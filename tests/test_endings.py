"""Turn endings (`retinue/endings.py`): a session declares what its turn leaves the owner on the platform's own server
(`turn_ending`), off the shape it is refused with what to fix, the feed carries it on the session's row and places the
row by its kind, a new turn clears it, and an artifact is served from the session's own folder only — against the
fake lanes of `test_platformd` with the face of `test_face`."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from retinue import endings, outfiles
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse
from test_subs import SubsFaceWorld, subs_scenario

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
ASK = {"kind": "ask", "request": "Find a flat in Lisbon for June, under 1500 a month",
       "why_owner": "goal", "options": [{"label": "Take Alfama", "effect": "I write to the landlord today"},
                                        {"label": "Keep looking", "effect": "Two more days on the search"}],
       "context": "Two flats fit; Alfama is 1400 and free from the 3rd, Graça is 1450 and needs a visit first.",
       "without": "I keep looking."}


@pytest.fixture(autouse=True)
def nothing_is_fresh(monkeypatch):
    monkeypatch.setattr(outfiles, "FRESH_S", -1e9)


def rows(data: dict) -> list[dict]:
    return [r for g in data["groups"] for r in g["rows"]]


def row_of(data: dict, session: str) -> dict:
    [row] = [r for r in rows(data) if r["session"] == session]
    return row


async def ended(world: SubsFaceWorld, ending: dict, call: str = "toolu_e", track: str = "notes") -> str:
    """A session the owner started whose turn declares `ending` and ends."""
    session = (await world.owner("new", {"input_id": f"tg:{call}", "track": track, "text": "find a flat"}))["session"]
    said, error = await world.tool(session, endings.TOOL, ending, call)
    assert not error, said
    world.clock.now += 60  # the turn ends after his message: news he has not seen
    await world.turn(session, "Two flats fit.")
    return session


def test_a_turn_ending_is_stored_and_shown_on_the_sessions_feed_row(tmp_path):
    async def body(world: SubsFaceWorld):
        await world.login()
        session = await ended(world, ASK)
        data = await world.get("/api/feed")
        row = row_of(data, session)
        assert (row["action"], row["source"], row["key"]) == ("answer", "session", f"ending:{session}"), \
            "an ask with no card is a question to answer"
        assert row["ending"] == ASK, "carried whole, as declared"
        assert row["essence"] == ASK["context"], "the line says what the advice rests on"
        assert [g["title"] for g in data["groups"]] == ["Answer the question"]

        done = await ended(world, {"kind": "done", "request": "Check the lease", "what": "The lease is fine.",
                                   "next": "Sign it", "next_by": "you"}, "toolu_d")
        row = row_of(await world.get("/api/feed"), done)
        assert (row["action"], row["ending"]["kind"], row["essence"]) == ("read", "done", "The lease is fine.")
        await world.call("POST", f"/api/sessions/{done}/seen", {})
        assert [r for r in rows(await world.get("/api/feed")) if r["session"] == done] == [], \
            "a result read leaves «Read» as any turn there"
        approve = await ended(world, {"kind": "approve", "request": "Book it", "why_owner": "money",
                                      "will": "Pay the deposit of 300", "items": ["acme.example: deposit"]}, "toolu_a")
        assert row_of(await world.get("/api/feed"), approve)["action"] == "decide"
    subs_scenario(tmp_path, body, SubsFaceWorld)


def test_an_ask_without_why_owner_is_refused_with_what_to_fix(tmp_path):
    async def body(world: SubsFaceWorld):
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "find a flat"}))["session"]
        said, error = await world.tool(session, endings.TOOL, {k: v for k, v in ASK.items() if k != "why_owner"},
                                       "toolu_1")
        assert error and said.startswith("Not recorded — why_owner:") and "goal, fact, money, secret, external" in said
        for n, (bad, fix) in enumerate([
                ({"kind": "note", "request": "x"}, "kind: one of ask, approve, look, step, done"),
                ({**ASK, "request": ""}, "request:"),
                ({k: v for k, v in ASK.items() if k != "request"}, "request:"),
                ({"kind": "approve", "request": "x", "will": "y"}, "why_owner:"),
                ({k: v for k, v in ASK.items() if k != "context"}, "context:"),
                ({"kind": "look", "request": "x"}, "artifact:"),
                ({"kind": "step", "what": "Sign the form"}, "what and where:"),
                ({"kind": "done", "request": "x"}, "what:"),
                ({**ASK, "options": [{"label": "A"}]}, "options:"),
                ({**ASK, "colour": "red"}, "Unknown field(s): colour"),
                ({"kind": "look", "request": "x", "artifact": {"path": "a", "url": "https://acme.example"}},
                 "artifact: {path or url, title} — exactly one of path and url")]):
            said, error = await world.tool(session, endings.TOOL, bad, f"toolu_b{n}")
            assert error and said.startswith(f"Not recorded — {fix}"), (bad, said)
        world.clock.now += 60
        await world.turn(session, "Two flats fit.")
        assert "ending" not in row_of(await world.get("/api/feed"), session), "nothing refused is kept"
    subs_scenario(tmp_path, body, SubsFaceWorld)


def test_a_new_turn_clears_the_ending(tmp_path):
    async def body(world: SubsFaceWorld):
        await world.login()
        session = await ended(world, ASK)
        assert row_of(await world.get("/api/feed"), session)["ending"]["kind"] == "ask"
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "Take Alfama"})
        assert [r for r in rows(await world.get("/api/feed")) if r["session"] == session] == [], "it works now"
        world.clock.now += 60
        await world.turn(session, "Written to the landlord.")
        row = row_of(await world.get("/api/feed"), session)
        assert (row["action"], "ending" in row) == ("read", False), "the turn after his answer ended with none"
    subs_scenario(tmp_path, body, SubsFaceWorld)


def test_a_step_ending_makes_a_row_in_the_step_group(tmp_path):
    async def body(world: SubsFaceWorld):
        await world.login()
        step = {"kind": "step", "what": "Sign the lease in the bank's app", "where": "acme bank app, Documents",
                "expect": "Say when it is signed", "wait": False}
        session = await ended(world, step)
        data = await world.get("/api/feed")
        [group] = data["groups"]
        assert (group["action"], group["title"]) == ("step", "Your step")
        [row] = group["rows"]
        assert (row["session"], row["essence"], row["ending"]) == (session, step["what"], step)
        [line] = (await world.owner("feed", {}))["rows"]
        assert (line["action"], line["group"], line["essence"]) == ("step", "Your step", step["what"])
    subs_scenario(tmp_path, body, SubsFaceWorld)


def test_an_artifact_outside_the_sessions_folder_is_refused(tmp_path):
    async def body(world: SubsFaceWorld):
        world.clock.now = time.time()  # the file times are the real ones
        await world.login()
        work = world.tmp / "work"
        for path, data in (("secret.png", PNG), ("repo/notes.md", b"# x"), ("repo/out/chart.png", PNG),
                           ("repo/out/lease.pdf", PDF), ("repo/out/.hidden.png", PNG)):
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_bytes(data)
        os.symlink(work / "secret.png", work / "repo" / "out" / "link.png")
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "a report"}))["session"]

        def look(path: str) -> dict:
            return {"kind": "look", "request": "A chart of the prices", "artifact": {"path": path, "title": "Chart"}}

        for n, (path, why) in enumerate([
                ("/work/secret.png", "outside your folder /work/repo"),
                ("../secret.png", "outside your folder /work/repo"),
                ("/etc/passwd", "outside your folder /work/repo"),
                ("out/link.png", "a link that leads outside your folder /work/repo"),
                ("out/.hidden.png", "a hidden file"),
                ("out/missing.png", "no such file"),
                ("notes.md", "the app shows a file only from out/ of your folder — save a copy as "
                             "/work/repo/out/notes.md")]):
            said, error = await world.tool(session, endings.TOOL, look(path), f"toolu_{n}")
            assert error and said.startswith(f"Not recorded — artifact {path!r}: {why}"), said
        ending = {**look("out/chart.png"), "artifacts": [{"path": "/work/repo/out/lease.pdf", "title": "Lease"},
                                                         {"url": "https://acme.example/flat", "title": "The flat"}]}
        said, error = await world.tool(session, endings.TOOL, ending, "toolu_ok")
        assert not error, said
        world.clock.now += 60
        await world.turn(session, "The chart is in out/.")
        row = row_of(await world.get("/api/feed"), session)
        chart, (lease, page) = row["ending"]["artifact"], row["ending"]["artifacts"]
        files = {f["path"]: f["id"] for f in (await world.get(f"/api/sessions/{session}"))["files"]}
        assert chart == {"path": "out/chart.png", "title": "Chart", "src": f"/api/files/{files['chart.png']}",
                         "kind": "image"}
        assert lease == {"path": "/work/repo/out/lease.pdf", "title": "Lease",
                         "src": f"/api/files/{files['lease.pdf']}/view", "kind": "pdf"}
        assert page == {"url": "https://acme.example/flat", "title": "The flat", "src": "https://acme.example/flat",
                        "kind": "link"}
        response = await world.api.get(lease["src"], headers={"Cookie": f"__Host-retinue={world.cookie}"})
        assert response.status == 200 and await response.read() == PDF, "the PDF opens in place"
        assert response.headers["Content-Disposition"] == "inline"
        assert response.headers["Content-Security-Policy"] == "frame-ancestors https://app.example.test"
    subs_scenario(tmp_path, body, SubsFaceWorld)


def test_the_lane_instructions_say_to_end_a_turn_with_turn_ending():
    common = " ".join((Path(__file__).parent.parent / "deploy" / "lanes" / "instructions" / "common.md").read_text()
                      .split())
    assert f"call `{endings.TOOL}`" in common and "once, last, just before the summary" in common
    for kind in endings.KINDS:
        assert f"`{kind}`:" in common, kind
    assert "Every kind but `step` carries `request`" in common and "decide yourself and end with `done`" in common
    assert endings.TOOL in endings.INSTRUCTIONS, "the server's own instructions name it too"
