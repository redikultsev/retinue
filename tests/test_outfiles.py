"""Files a session hands to the owner from `out/`: what one look at the folder takes and leaves (types, sizes, counts,
times, links followed as the gate follows them and never out of /work), then the platform end to end — a turn ends,
its files become notes for the router and events for the face, each once — and the face's download and HTML view."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time

import pytest

from retinue import outfiles
from retinue.outfiles import Skipped, scan
from test_face import FaceWorld, faced
from test_platformd import DAY, World, fixture, scenario, short_wait  # noqa: F401 — the fixture is autouse

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DONE = json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
                   "result": "Отчёт в out/report.md"})


def put(work, path: str, data: bytes = b"x") -> None:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def work(tmp_path):
    (tmp_path / "work" / "repo").mkdir(parents=True)
    return tmp_path / "work"


@pytest.fixture(autouse=True)
def nothing_is_fresh(monkeypatch):
    """The files of a test are written a moment before the look: none is held back as half written but where a test
    says so (`test_a_file_still_being_written_waits_for_the_next_look`)."""
    monkeypatch.setattr(outfiles, "FRESH_S", -1e9)


def look(work, since: float = 0.0, known=frozenset(), now: float | None = None):
    return scan(str(work), "repo", since, set(known), time.time() if now is None else now)


# --- one look -------------------------------------------------------------------------------------------------------

def test_a_look_takes_the_listed_types_and_says_why_it_left_the_rest(work):
    put(work, "repo/out/report.md", "# Отчёт".encode())
    put(work, "repo/out/charts/a.png", PNG)
    put(work, "repo/out/tool.exe", b"MZ")
    put(work, "repo/out/empty.pdf", b"")
    put(work, "repo/out/.hidden.md", b"x")
    put(work, "repo/out/.cache/x.md", b"x")
    put(work, "repo/notes.md", b"not in out")
    found = look(work)
    assert [(f.path, f.mime) for f in found.found] == [("report.md", "text/markdown; charset=utf-8"),
                                                       ("charts/a.png", "image/png")]
    report = next(f for f in found.found if f.path == "report.md")
    assert report.data == "# Отчёт".encode() and report.sha256 == hashlib.sha256(report.data).hexdigest()
    assert sorted(found.skipped, key=lambda s: s.path) == [Skipped("empty.pdf", "пустой"),
                                                           Skipped("tool.exe", "тип не из списка")]


def test_no_out_folder_is_an_empty_look(work):
    assert look(work) == outfiles.Look()


def test_a_file_not_touched_since_the_last_look_or_with_bytes_already_sent_is_left_silently(work):
    put(work, "repo/out/a.md", b"one")
    assert look(work, since=time.time() + 60) == outfiles.Look(), "older than the session's turns"
    sent = {("a.md", hashlib.sha256(b"one").hexdigest())}
    assert look(work, known=sent) == outfiles.Look(), "the same bytes again"
    put(work, "repo/out/a.md", b"two")
    assert [f.data for f in look(work, known=sent).found] == [b"two"], "changed: handed over again"


def test_the_limits_of_one_turn(work, monkeypatch):
    monkeypatch.setattr(outfiles, "MAX_FILE", 10)
    monkeypatch.setattr(outfiles, "MAX_FILES", 2)
    monkeypatch.setattr(outfiles, "MAX_TURN", 12)
    put(work, "repo/out/a.md", b"123456")
    put(work, "repo/out/b.md", b"12345678901")
    put(work, "repo/out/c.md", b"1234567")
    put(work, "repo/out/d.md", b"1")
    put(work, "repo/out/e.md", b"1")
    found = look(work)
    assert [f.path for f in found.found] == ["a.md", "d.md"]
    assert found.skipped == [Skipped("b.md", "больше 0 МБ"), Skipped("c.md", "за ход больше 0 МБ"),
                             Skipped("e.md", "больше 2 файлов за ход")]


def test_a_look_walks_a_bounded_tree(work, monkeypatch):
    monkeypatch.setattr(outfiles, "MAX_ENTRIES", 3)
    for n in range(5):
        put(work, f"repo/out/{n}.md", b"x")
    put(work, "repo/out/1/2/3/4/deep.md", b"x")
    found = look(work)
    assert [f.path for f in found.found] == ["0.md", "1.md"], "the folder `1` counts too"
    assert found.skipped == [Skipped("out/", "в папке больше 3 файлов и папок — смотрел первые")]
    monkeypatch.setattr(outfiles, "MAX_ENTRIES", 500)
    assert "1/2/3/4/deep.md" not in [f.path for f in look(work).found], "deeper than three folders"


def test_links_are_followed_as_the_gate_follows_them_and_never_out_of_work(work, tmp_path):
    put(work, "repo/docs/report.pdf", b"%PDF-1.7")
    (tmp_path / "secret.md").write_bytes(b"secret")
    out = work / "repo" / "out"
    out.mkdir()
    os.symlink("/work/repo/docs/report.pdf", out / "report.pdf")       # absolute: a lane path
    os.symlink("../docs/report.pdf", out / "again.pdf")                 # relative, inside /work
    os.symlink("/etc/hosts", out / "hosts.md")                          # a lane path out of /work
    os.symlink("../../../secret.md", out / "up.md")                     # walks out of /work
    os.symlink(str(tmp_path / "secret.md"), out / "platform.md")        # the platform's own path: no lane's
    os.symlink("/work/repo/docs", out / "docs")                         # a folder: not walked
    found = look(work)
    assert sorted((f.path, f.data) for f in found.found) == [("again.pdf", b"%PDF-1.7"), ("report.pdf", b"%PDF-1.7")]
    assert sorted(found.skipped, key=lambda s: s.path) == [
        Skipped("hosts.md", "ссылка ведёт за пределы /work"), Skipped("platform.md", "ссылка ведёт за пределы /work"),
        Skipped("up.md", "ссылка ведёт за пределы /work")]


def test_an_out_folder_that_leads_out_of_work_is_not_looked_into(work, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "a.md").write_bytes(b"x")
    os.symlink("/etc", work / "repo" / "out")
    assert look(work).found == []
    (work / "repo" / "out").unlink()
    os.symlink("../../elsewhere", work / "repo" / "out")
    assert look(work) == outfiles.Look(skipped=[Skipped("out/", "папка ведёт за пределы /work — не смотрю")])


def test_a_link_swapped_in_after_the_check_is_refused_not_followed(work, tmp_path):
    """The read opens one directory at a time with O_NOFOLLOW: a component that became a link fails."""
    put(work, "repo/out/a.md", b"x")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "a.md").write_bytes(b"secret")
    root = os.path.realpath(work)
    os.close(outfiles._open(root, ["repo", "out", "a.md"]))
    (work / "repo" / "out" / "a.md").unlink()
    (work / "repo" / "out").rmdir()
    os.symlink(str(tmp_path / "secret"), work / "repo" / "out")
    with pytest.raises(OSError):
        outfiles._open(root, ["repo", "out", "a.md"])
    os.unlink(work / "repo" / "out")
    put(work, "repo/out/b.md", b"x")
    os.symlink(str(tmp_path / "secret" / "a.md"), work / "repo" / "out" / "c.md")
    with pytest.raises(OSError):
        outfiles._open(root, ["repo", "out", "c.md"])


def test_what_was_left_out_reads_as_a_list():
    text = outfiles.notice_text(outfiles.Look(skipped=[Skipped("a_b.exe", "тип не из списка"),
                                                       Skipped("x`y.md", "пустой")]))
    assert text == "Не отправил из out/:\n- `a_b.exe` — тип не из списка\n- x`y.md — пустой"


# --- the platform ---------------------------------------------------------------------------------------------------

async def turn(world: World, session: str, run: dict, *lines: str) -> None:
    world.lanes["code"].add(session, run["run"], *lines)
    world.lanes["code"].exited(session, run["run"])
    await world.poll()


def test_a_turn_hands_over_what_it_left_in_out_once_after_its_summary(tmp_path, monkeypatch):
    """The file times are the real ones, so the platform's clock is set to the real time before each turn's files are
    written; the slack is off, so a file written a moment before the session is not its."""
    monkeypatch.setattr(outfiles, "SLACK_S", 0.0)

    async def body(world: World):
        work = world.tmp / "work"
        put(work, "repo/out/old.md", b"before the session")
        time.sleep(0.05)
        world.clock.now = time.time()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "отчёт"}))["session"]
        [run] = world.lanes["code"].runs()
        put(work, "repo/out/report.md", "# Итог".encode())
        put(work, "repo/out/chart.png", PNG)
        put(work, "repo/out/run.sh", b"echo")
        await turn(world, session, run, fixture("reads")[0], DONE)

        kinds = [n["kind"] for n in world.notes()]
        assert kinds[0] == "summary" and kinds[1:] == ["file", "file", "file_skipped"], "files after the summary"
        files = [n["data"] for n in world.notes("file")]
        assert [(f["path"], f["mime"], f["size"]) for f in files] == [("chart.png", "image/png", len(PNG)),
                                                                      ("report.md", "text/markdown; charset=utf-8",
                                                                       len("# Итог".encode()))]
        [skipped] = world.notes("file_skipped")
        assert skipped["data"]["head"] == f"Файлы · retinue · сессия {session}"
        assert skipped["data"]["text"] == "Не отправил из out/:\n- `run.sh` — тип не из списка"
        got = await world.owner("file", {"id": files[1]["id"]})
        assert got["name"] == "report.md" and base64.b64decode(got["data"]) == "# Итог".encode()
        assert (await world.owner("file", {"id": "nope"}))["error"].startswith("Файла больше нет")

        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "ещё"})
        run2 = world.lanes["code"].runs()[-1]
        world.clock.now = time.time()
        put(work, "repo/out/report.md", "# Итог".encode())  # rewritten, the same bytes
        await turn(world, session, run2, DONE)
        assert len(world.notes("file")) == 2, "the same bytes are not sent again"

        run3_text = "# Итог 2".encode()
        await world.owner("input", {"input_id": "tg:3", "session": session, "text": "поправь"})
        run3 = world.lanes["code"].runs()[-1]
        world.clock.now = time.time()
        put(work, "repo/out/report.md", run3_text)
        await turn(world, session, run3, DONE)
        assert [n["data"]["path"] for n in world.notes("file")] == ["chart.png", "report.md", "report.md"]
        assert world.platform.out.owed() == []

    scenario(tmp_path, body)


def test_a_card_is_not_the_turns_end_and_a_side_session_hands_nothing_over(tmp_path):
    async def body(world: World):
        put(world.tmp / "work", "repo/out/a.md", b"x")
        world.clock.now = time.time() - 60
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        deferred = json.dumps({"type": "result", "subtype": "success", "stop_reason": "tool_deferred",
                               "deferred_tool_use": {"id": "toolu_x", "name": "Bash", "input": {"command": "x"}}})
        await turn(world, session, run, deferred)
        assert world.notes("file") == [], "waiting on a card: the turn goes on"
        side = (await world.platform.side("face:q", session, "что там?"))["session"]
        side_run = world.lanes["code"].runs()[-1]
        assert side_run["session"] == side
        await turn(world, side, side_run, DONE)
        assert world.notes("file") == [] and world.platform.out.owed() == []

    scenario(tmp_path, body)


def test_a_look_that_fails_is_said_and_owed_no_more(tmp_path, monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    async def body(world: World):
        monkeypatch.setattr(outfiles, "scan", broken)
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        await turn(world, session, run, DONE)
        [skipped] = world.notes("file_skipped")
        assert skipped["data"]["text"] == "Файлы из out/ не собраны: ошибка платформы (RuntimeError)."
        assert world.platform.out.owed() == []

    scenario(tmp_path, body)


def test_files_are_pruned_after_thirty_days_with_their_bytes(tmp_path):
    async def body(world: World):
        world.clock.now = time.time()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        put(world.tmp / "work", "repo/out/a.pdf", b"%PDF")
        await turn(world, session, run, DONE)
        [note] = world.notes("file")
        world.clock.now += 31 * DAY
        world.platform.tick()
        assert world.platform.out.content(note["data"]["id"]) is None
        assert world.store.db.execute("SELECT COUNT(*) FROM out_blobs").fetchone()[0] == 0

    scenario(tmp_path, body)


# --- the face ---------------------------------------------------------------------------------------------------------

def test_the_face_lists_downloads_and_frames_a_session_s_files_under_the_sandbox(tmp_path):
    async def body(world: FaceWorld):
        world.clock.now = time.time()  # the file times are the real ones
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        page = b"<!doctype html><title>t</title><script>alert(1)</script><p>hi"
        put(world.tmp / "work", "repo/out/page.html", page)
        put(world.tmp / "work", "repo/out/pic.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>")
        before = world.store.cursor()
        await turn(world, session, run, DONE)
        events = [(kind, json.loads(data)) for _, kind, data in world.store.events_after(before, 1000)
                  if kind == "file"]
        assert [e["path"] for _, e in events] == ["page.html", "pic.svg"]

        snapshot = await world.get(f"/api/sessions/{session}")
        html, svg = snapshot["files"]
        assert html == events[0][1] and set(html) == {"id", "session", "path", "name", "size", "mime", "at"}

        status, response = await raw(world, f"/api/files/{svg['id']}")
        assert status == 200 and await response.read() == b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"
        assert response.headers["Content-Type"] == "image/svg+xml"
        assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert response.headers["Content-Disposition"] == \
            "attachment; filename=\"pic.svg\"; filename*=UTF-8''pic.svg"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

        status, response = await raw(world, f"/api/files/{html['id']}/view")
        assert status == 200 and await response.read() == page
        policy = response.headers["Content-Security-Policy"]
        assert "script-src 'none'" in policy and "; sandbox;" in policy and "allow-same-origin" not in policy
        assert policy.endswith("frame-ancestors https://app.example.test")
        assert "Content-Disposition" not in response.headers
        assert (await raw(world, f"/api/files/{svg['id']}/view"))[0] == 404, "only HTML is viewed"
        assert (await raw(world, "/api/files/nope"))[0] == 404
        assert (await raw(world, f"/api/files/{svg['id']}", cookie=""))[0] == 401, "only after a login"

    faced(tmp_path, body)


async def raw(world: FaceWorld, path: str, cookie: str | None = None):
    given = world.cookie if cookie is None else cookie
    response = await world.api.get(path, headers={"Cookie": f"__Host-retinue={given}"} if given else {})
    return response.status, response


def test_a_download_name_out_of_ascii_keeps_itself_and_a_plain_stand_in():
    from retinue.face import _attachment
    assert _attachment("Отчёт за 2026.pdf") == ("attachment; filename=\"_________2026.pdf\"; "
                                                "filename*=UTF-8''%D0%9E%D1%82%D1%87%D1%91%D1%82%20%D0%B7%D0%B0"
                                                "%202026.pdf")


# --- the router -------------------------------------------------------------------------------------------------------

def bridged(tmp_path, notes, files):
    from test_bridge import make
    ch, bridge, platform = make(tmp_path, notes)

    async def out_file(file_id):
        platform.calls.append(("file", file_id))
        if file_id not in files:
            return {"error": "Файла больше нет: файлы хранятся 30 дней."}
        name, mime, data = files[file_id]
        return {"name": name, "mime": mime, "body": data}

    platform.out_file = out_file
    return ch, bridge, platform


def file_note(seq, file_id, session="ab12cd34"):
    return {"seq": seq, "kind": "file", "session": session, "approval": "", "data": {"id": file_id}}


def test_the_router_sends_each_file_under_the_session_s_last_message_and_maps_it_to_the_session(tmp_path):
    from retinue.bridge import SESSION
    summary = {"seq": 1, "kind": "summary", "session": "ab12cd34", "approval": "",
               "data": {"head": "Итог · retinue · сессия ab12cd34", "text": "Готово."}}
    skipped = {"seq": 4, "kind": "file_skipped", "session": "ab12cd34", "approval": "",
               "data": {"head": "Файлы · retinue · сессия ab12cd34", "text": "Не отправил из out/:\n- `a.exe` — тип"}}
    files = {"f1": ("chart.png", "image/png", PNG), "f2": ("report.pdf", "application/pdf", b"%PDF")}
    ch, bridge, platform = bridged(tmp_path, [summary, file_note(2, "f1"), file_note(3, "f2"), skipped], files)
    assert asyncio.run(bridge.deliver()) == 4
    (m1, p1, _), (m2, p2, f2), (m3, p3, f3), (m4, p4, _) = ch.sent
    assert m1 == "sendMessage" and m4 == "sendMessage" and p4["text"].startswith("Файлы · retinue · сессия ab12cd34")
    assert m2 == "sendPhoto" and f2 == {"photo": ("chart.png", PNG, "image/png")}
    assert m3 == "sendDocument" and f3 == {"document": ("report.pdf", b"%PDF", "application/pdf")}
    assert "caption" not in p2 and "caption" not in p3, "the platform's words go only through _message"
    assert json.loads(p2["reply_parameters"]) == {"message_id": 900, "allow_sending_without_reply": True}
    assert json.loads(p3["reply_parameters"])["message_id"] == 901, "under the session's last message"
    assert ch.store.sent_event(SESSION, "901") == "ab12cd34" and ch.store.sent_event(SESSION, "902") == "ab12cd34"
    assert platform.confirmed == [4]


def test_a_picture_telegram_will_not_take_as_a_photo_goes_as_a_document_and_a_big_one_never_tries(tmp_path,
                                                                                                   monkeypatch):
    from retinue import bridge as bridge_module
    monkeypatch.setattr(bridge_module, "PHOTO_MAX", len(PNG))
    files = {"f1": ("wide.png", "image/png", PNG), "f2": ("big.png", "image/png", PNG + b"x"),
             "f3": ("text.md", "text/markdown; charset=utf-8", b"# x")}
    ch, bridge, _ = bridged(tmp_path, [file_note(1, "f1"), file_note(2, "f2"), file_note(3, "f3")], files)
    real = ch.call

    async def call(method, files=None, **params):
        if method == "sendPhoto":
            ch.sent.append((method, params, files))
            from retinue.channels.telegram import TelegramError
            raise TelegramError("sendPhoto: 400 Bad Request: PHOTO_INVALID_DIMENSIONS", 400)
        return await real(method, files=files, **params)

    ch.call = call
    asyncio.run(bridge.deliver())
    assert [m for m, _, _ in ch.sent] == ["sendPhoto", "sendDocument", "sendDocument", "sendDocument"]
    assert ch.sent[3][2] == {"document": ("text.md", b"# x", "text/markdown")}
    assert "reply_parameters" not in ch.sent[1][1], "no message of the session yet: just sent"


def test_a_file_the_platform_no_longer_keeps_is_said_and_one_that_did_not_go_out_comes_again(tmp_path):
    from retinue.channels.telegram import TelegramError
    files = {"f1": ("a.pdf", "application/pdf", b"%PDF")}
    ch, bridge, platform = bridged(tmp_path, [file_note(1, "gone"), file_note(2, "f1")], files)
    ch.fail = {}
    real = ch.call
    busy = [True]

    async def call(method, files=None, **params):
        if method == "sendDocument" and busy[0]:
            raise TelegramError("sendDocument: 429 Too Many Requests", 429)
        return await real(method, files=files, **params)

    ch.call = call

    async def run():
        with pytest.raises(TelegramError):
            await bridge.deliver()
        assert platform.confirmed == [1]
        busy[0] = False
        assert await bridge.deliver() == 1

    asyncio.run(run())
    assert ch.sent[0][1]["text"] == "Файла больше нет: файлы хранятся 30 дней."
    assert [m for m, _, _ in ch.sent] == ["sendMessage", "sendDocument"]
    assert platform.confirmed == [1, 2]


# --- the review's cases ---------------------------------------------------------------------------------------------

def test_a_name_that_is_not_utf8_is_left_out_and_never_stalls_the_lanes(work, monkeypatch, tmp_path):
    """A name the file system gives with undecodable bytes (`touch $'out/\\xff.md'` on Linux; macOS refuses to make one,
    so the walk is given it) is said by a readable form, never stored."""
    put(work, "repo/out/ok.md", b"x")
    real = os.fwalk

    def fwalk(top, **kw):
        for dirpath, dirnames, filenames, dirfd in real(top, **kw):
            yield dirpath, dirnames, [*filenames, "bad\udcff.md"] if dirpath == top else filenames, dirfd

    monkeypatch.setattr(outfiles.os, "fwalk", fwalk)
    found = look(work)
    assert [f.path for f in found.found] == ["ok.md"]
    assert found.skipped == [Skipped("bad�.md", "имя файла не в UTF-8")]
    assert outfiles.utf8("ok.md") and not outfiles.utf8("bad\udcff.md")


def test_a_hand_over_that_breaks_is_said_once_and_the_lane_goes_on(tmp_path, monkeypatch):
    """Whatever breaks a look — here a name the store cannot take — the look is said and owed no more; the poll still
    confirms the lane's lines and starts what is queued."""
    def poisoned(*args):
        return outfiles.Look(found=[outfiles.Found("bad\udcff.md", "text/markdown", b"x", "0" * 64)])

    async def body(world: World):
        monkeypatch.setattr(outfiles, "scan", poisoned)
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"}))["session"]
        [run] = world.lanes["private"].runs()
        world.lanes["private"].add(session, run["run"], DONE)
        world.lanes["private"].exited(session, run["run"])
        await world.poll()
        [said] = world.notes("file_skipped")
        assert said["data"]["text"] == "Файлы из out/ не собраны: ошибка платформы (UnicodeEncodeError)."
        assert world.platform.out.owed() == [] and world.notes("file") == []
        assert all(line["given"] for line in world.lanes["private"].journal), "the journal confirmed"
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "ещё"})
        assert len(world.lanes["private"].runs()) == 2, "the queue drains"
        await world.poll()
        assert len(world.notes("file_skipped")) == 1, "not said again and again"

    scenario(tmp_path, body)


def test_a_link_to_work_itself_is_not_a_file_and_the_look_goes_on(work):
    out = work / "repo" / "out"
    out.mkdir()
    os.symlink("/work", out / "root.md")
    os.symlink("/work/repo/..", out / "up.md")
    put(work, "repo/out/a.md", b"x")
    found = look(work)
    assert [f.path for f in found.found] == ["a.md"] and found.skipped == []


def test_a_bad_link_is_said_only_while_it_is_new(work):
    out = work / "repo" / "out"
    out.mkdir()
    os.symlink("/etc/hosts", out / "hosts.md")
    assert look(work).skipped == [Skipped("hosts.md", "ссылка ведёт за пределы /work")]
    assert look(work, since=time.time() + 60).skipped == [], "the next turn's look: not said again"


def test_a_file_still_being_written_waits_for_the_next_look(work, monkeypatch):
    monkeypatch.setattr(outfiles, "FRESH_S", 2.0)
    put(work, "repo/out/a.md", b"x")
    assert look(work, now=time.time()) == outfiles.Look(), "written this very moment"
    assert [f.path for f in look(work, now=time.time() + 3).found] == ["a.md"], "the next look takes it"


def test_a_name_cannot_spoof_its_type_with_direction_marks(work):
    put(work, "repo/out/report‮fdp.exe", b"MZ")
    put(work, "repo/out/a⁦b.md", b"x")
    found = look(work)
    assert [f.path for f in found.found] == ["a⟦U+2066⟧b.md"]
    assert found.skipped == [Skipped("report⟦U+202E⟧fdp.exe", "тип не из списка")]
    assert outfiles.shown("a\nb‏") == "a⟦U+000A⟧b⟦U+200F⟧"


def write_call(path: str) -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_w", "name": "Write", "input": {"file_path": path, "content": "x"}}]}})


def bash_call(command: str) -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": command}}]}})


def test_two_sessions_in_one_directory_each_get_the_files_their_own_calls_named(tmp_path):
    """B's turn ends first: A's file, named by A's Write alone, waits for A; a file neither named goes with B, said
    as shared; B's own file is B's. Then A's turn ends and A gets its file."""
    async def body(world: World):
        world.clock.now = time.time() - 10
        a = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "a"}))["session"]
        b = (await world.owner("new", {"input_id": "tg:2", "track": "retinue", "text": "b"}))["session"]
        run_a, run_b = world.lanes["code"].runs()
        work = world.tmp / "work"
        put(work, "repo/out/a.md", b"A")
        put(work, "repo/out/b.pdf", b"%PDF B")
        put(work, "repo/out/nobody.md", b"?")
        world.lanes["code"].add(a, run_a["run"], write_call("/work/repo/out/a.md"))
        world.lanes["code"].add(b, run_b["run"], bash_call("pandoc x.md -o out/b.pdf"), DONE)
        world.lanes["code"].exited(b, run_b["run"])
        await world.poll()
        assert [(n["session"], n["data"]["path"]) for n in world.notes("file")] == [(b, "b.pdf"), (b, "nobody.md")]
        [said] = world.notes("file_skipped")
        assert said["session"] == b and "в папке работали две сессии" in said["data"]["text"]
        assert "`nobody.md`" in said["data"]["text"] and "b.pdf" not in said["data"]["text"]

        world.lanes["code"].add(a, run_a["run"], DONE)
        world.lanes["code"].exited(a, run_a["run"])
        world.clock.now = time.time()
        await world.poll()
        assert [(n["session"], n["data"]["path"]) for n in world.notes("file")][2:] == [(a, "a.md")]

    scenario(tmp_path, body)


def test_a_file_over_the_storage_caps_is_left_out_and_said(tmp_path):
    async def body(world: World):
        world.clock.now = time.time() - 10
        out = world.platform.out
        out.store_cap, out.day_cap = 10, 6
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        put(world.tmp / "work", "repo/out/a.md", b"12345")
        put(world.tmp / "work", "repo/out/b.md", b"123")
        await turn(world, session, run, DONE)
        assert [n["data"]["path"] for n in world.notes("file")] == ["a.md"]
        [said] = world.notes("file_skipped")
        assert said["data"]["text"] == "Не отправил из out/:\n- `b.md` — за сутки больше 0 МБ файлов"
        world.clock.now += 2 * DAY
        out.day_cap = 100
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "ещё"})
        run2 = world.lanes["code"].runs()[-1]
        put(world.tmp / "work", "repo/out/c.md", b"123456")
        await turn(world, session, run2, DONE)
        assert "хранилище файлов платформы заполнено" in world.notes("file_skipped")[-1]["data"]["text"]

    scenario(tmp_path, body)


def test_a_turn_that_ends_during_a_look_is_looked_at_again(tmp_path):
    from retinue.sessions import PlatformStore
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    out = outfiles.OutFiles(store)
    out.owe("s1", 1.0)
    [owed] = out.owed()
    out.owe("s1", 1.0)  # the next turn ended while the first look ran
    out.record({"id": "s1", "track": "t", "lane": "code"}, "repo", outfiles.Look(), 5.0, owed["gen"])
    [again] = out.owed()
    assert again["since"] == 5.0 and again["gen"] == owed["gen"] + 1
    out.record({"id": "s1", "track": "t", "lane": "code"}, "repo", outfiles.Look(), 6.0, again["gen"])
    assert out.owed() == []


def test_a_page_goes_to_telegram_as_an_open_button_never_as_itself(tmp_path):
    files = {"f1": ("page.html", "text/html; charset=utf-8", b"<script>fetch('https://x')</script>"),
             "f2": ("pic.svg", "image/svg+xml", b"<svg/>")}
    notes = [{**file_note(1, "f1"), "data": {"id": "f1", "path": "pages/page.html", "mime": "text/html; charset=utf-8"}},
             {**file_note(2, "f2"), "data": {"id": "f2", "path": "pic.svg", "mime": "image/svg+xml"}}]
    ch, bridge, platform = bridged(tmp_path, notes, files)
    bridge.face_url = "https://app.example.test"
    asyncio.run(bridge.deliver())
    assert [m for m, _, _ in ch.sent] == ["sendMessage", "sendMessage"] and platform.calls == []
    params = ch.sent[0][1]
    assert "<code>page.html</code>" in params["text"]
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть",
                                                            "web_app": {"url": "https://app.example.test/f/f1"}}]]}

    ch, bridge, _ = bridged(tmp_path / "b", notes, files)  # no face: as text
    asyncio.run(bridge.deliver())
    assert [(m, f["document"][0], f["document"][2]) for m, _, f in ch.sent] == [
        ("sendDocument", "page.html.txt", "text/plain"), ("sendDocument", "pic.svg.txt", "text/plain")]


def test_the_face_gives_one_file_s_row_for_the_open_button(tmp_path):
    async def body(world: FaceWorld):
        world.clock.now = time.time() - 10
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "x"}))["session"]
        [run] = world.lanes["code"].runs()
        put(world.tmp / "work", "repo/out/page.html", b"<p>hi")
        await turn(world, session, run, DONE)
        [note] = world.notes("file")
        meta = await world.get(f"/api/files/{note['data']['id']}/meta")
        assert meta["file"]["path"] == "page.html" and meta["file"]["session"] == session
        assert (await raw(world, "/api/files/nope/meta"))[0] == 404

    faced(tmp_path, body)
