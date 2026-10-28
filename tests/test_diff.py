"""A session's changes (`GET /api/sessions/{id}/diff`): built from its own transcript — the edits that ran — with the
CLI's own patch where it gave one and difflib where it did not; never by running anything in the lane."""

from __future__ import annotations

import json

from retinue import changes, sessions
from test_face import FaceWorld, faced
from test_platformd import fixture, short_wait  # noqa: F401 — the fixture is autouse

CWD = "/work/repo"


def init(cwd: str = CWD) -> dict:
    return {"type": "system", "subtype": "init", "cwd": cwd, "session_id": "s"}


def call(call_id: str, name: str, **args) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": args}]}, "parent_tool_use_id": None}


def result(call_id: str, meta=None, *, error: bool = False, text: str = "ok") -> dict:
    line = {"type": "user", "message": {"role": "user", "content": [
        {"tool_use_id": call_id, "type": "tool_result", "content": text, "is_error": error}]},
        "parent_tool_use_id": None}
    if meta is not None:
        line["tool_use_result"] = meta
    return line


def patch(old_start: int, new_start: int, *lines: str) -> list[dict]:
    old = sum(1 for line in lines if line[0] in " -")
    new = sum(1 for line in lines if line[0] in " +")
    return [{"oldStart": old_start, "oldLines": old, "newStart": new_start, "newLines": new, "lines": list(lines)}]


def diff_of(*lines: dict, cwd: str = CWD, cap: int = changes.CAP) -> dict:
    scan = changes.Scan()
    for line in lines:
        scan.line(line)
    return changes.render(scan.changes, cwd, cap)


def test_an_edit_takes_the_clis_own_patch_with_the_files_real_line_numbers():
    edit = call("t1", "Edit", file_path=f"{CWD}/src/app.py", old_string="b = 2", new_string="b = 3")
    meta = {"filePath": f"{CWD}/src/app.py", "oldString": "b = 2", "newString": "b = 3", "originalFile": "...",
            "structuredPatch": patch(40, 40, " a = 1", "-b = 2", "+b = 3", " c = 4"), "userModified": False}
    got = diff_of(init(), edit, result("t1", meta))
    assert got == {
        "cwd": CWD,
        "stat": " src/app.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
        "diff": "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                "@@ -40,3 +40,3 @@\n a = 1\n-b = 2\n+b = 3\n c = 4\n",
        "truncated": False, "approximate_files": []}


def test_an_edit_without_a_patch_is_rebuilt_by_difflib_and_named_approximate():
    edit = call("t1", "Edit", file_path=f"{CWD}/notes.md", old_string="one\ntwo\nthree", new_string="one\n2\nthree")
    got = diff_of(init(), edit, result("t1"))
    assert got["diff"] == ("diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n"
                           "@@ -1,3 +1,3 @@\n one\n-two\n+2\n three\n")
    assert got["approximate_files"] == ["notes.md"]

    broken = {"filePath": f"{CWD}/notes.md", "structuredPatch": [{"oldStart": 1, "newStart": 1,
                                                                  "lines": ["-two\n+++ b/evil", "+2"]}]}
    assert diff_of(init(), edit, result("t1", broken)) == got, "a patch line that would break the diff is not used"


def test_a_written_new_file_is_all_added_and_one_written_over_is_diffed_against_the_original():
    new = call("t1", "Write", file_path=f"{CWD}/new.txt", content="hello\nworld")
    created = {"type": "create", "filePath": f"{CWD}/new.txt", "content": "hello\nworld", "structuredPatch": [],
               "originalFile": None}
    got = diff_of(init(), new, result("t1", created))
    assert got["diff"] == ("diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n"
                           "@@ -0,0 +1,2 @@\n+hello\n+world\n\\ No newline at end of file\n")
    assert got["approximate_files"] == []

    over = call("t2", "Write", file_path=f"{CWD}/old.txt", content="a\nB\nc\n")
    original = {"type": "update", "filePath": f"{CWD}/old.txt", "content": "a\nB\nc\n", "structuredPatch": [],
                "originalFile": "a\nb\nc\n"}
    got = diff_of(init(), over, result("t2", original))
    assert got["diff"].endswith("@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n") and got["approximate_files"] == []

    blind = diff_of(init(), over, result("t2"))
    assert blind["diff"].endswith("@@ -0,0 +1,3 @@\n+a\n+B\n+c\n") and blind["approximate_files"] == ["old.txt"], \
        "no patch, no original: the whole content as added, and said to be approximate"


def test_only_edits_that_ran_count():
    meta = {"filePath": f"{CWD}/a.py", "structuredPatch": patch(1, 1, "-x", "+y")}
    got = diff_of(init(),
                  call("t1", "Edit", file_path=f"{CWD}/a.py", old_string="x", new_string="y"),
                  result("t1", "Error: String to replace not found in file.", error=True),
                  call("t2", "Edit", file_path=f"{CWD}/b.py", old_string="x", new_string="y"),
                  result("t2", meta | {"filePath": f"{CWD}/b.py"}, error=True, text="The user rejected it"),
                  call("t3", "Edit", file_path=f"{CWD}/c.py", old_string="x", new_string="y"),  # still running
                  call("t4", "Bash", command="echo x > d.py"), result("t4", {"stdout": ""}))
    assert got == {"cwd": CWD, "stat": "", "diff": "", "truncated": False, "approximate_files": []}


def test_two_edits_of_one_file_are_one_section_with_their_hunks_in_the_order_they_ran():
    first = {"filePath": f"{CWD}/a.py", "structuredPatch": patch(10, 10, "-x", "+y")}
    second = {"filePath": f"{CWD}/a.py", "structuredPatch": patch(2, 2, " k", "+z")}
    got = diff_of(init(),
                  call("t1", "Edit", file_path=f"{CWD}/a.py", old_string="x", new_string="y"), result("t1", first),
                  call("t2", "Write", file_path="/etc/hosts", content="h\n"),
                  result("t2", {"type": "create", "filePath": "/etc/hosts", "content": "h\n", "structuredPatch": []}),
                  call("t3", "Edit", file_path=f"{CWD}/a.py", old_string="k", new_string="k\nz"), result("t3", second))
    assert got["diff"].count("diff --git a/a.py b/a.py") == 1
    assert got["diff"].index("@@ -10 +10 @@") < got["diff"].index("@@ -2 +2,2 @@") < got["diff"].index("/etc/hosts")
    assert "diff --git a//etc/hosts b//etc/hosts\n" in got["diff"], "a path outside the directory stays absolute"
    assert got["stat"].splitlines()[-1] == " 2 files changed, 3 insertions(+), 1 deletion(-)"


def test_a_name_never_becomes_a_line_of_the_diff():
    name = f"{CWD}/x\n+++ b/evil"
    got = diff_of(init(), call("t1", "Write", file_path=name, content="a\n"),
                  result("t1", {"type": "create", "filePath": name, "content": "a\n", "structuredPatch": []}))
    assert got["diff"].splitlines()[0] == 'diff --git "a/x\\n+++ b/evil" "b/x\\n+++ b/evil"'


def test_past_the_cap_whole_hunks_are_left_out_and_the_stat_still_counts_everything():
    lines = [init()]
    for n in range(40):
        lines += [call(f"t{n}", "Write", file_path=f"{CWD}/f{n}.txt", content="x" * 100 + "\n"),
                  result(f"t{n}", {"type": "create", "filePath": f"{CWD}/f{n}.txt", "content": "x" * 100 + "\n",
                                   "structuredPatch": []})]
    got = diff_of(*lines, cap=1000)
    assert got["truncated"] is True and len(got["diff"].encode()) <= 1000
    assert got["diff"].endswith("x\n") and got["diff"].count("diff --git") == got["diff"].count("@@ -0,0")
    assert got["stat"].splitlines()[-1] == " 40 files changed, 40 insertions(+)"
    assert diff_of(*lines)["truncated"] is False


def test_a_turn_whose_raw_lines_were_pruned_is_taken_from_its_projection(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    turn = [init("/work"), call("t1", "Edit", file_path="/work/a.py", old_string="x", new_string="y"),
            result("t1", {"filePath": "/work/a.py", "structuredPatch": patch(7, 7, "-x", "+y")}),
            {"type": "result", "subtype": "success", "stop_reason": "end_turn", "result": "done"}]
    for seq, line in enumerate(turn, 1):
        store.add_raw("code", seq, session, "r1", json.dumps(line), 0.0)
    store.reproject(session, 0)
    cwd, found = store.changes_of(session)
    assert cwd == "/work" and changes.render(found, cwd)["diff"].endswith("@@ -7 +7 @@\n-x\n+y\n")

    store.db.execute("DELETE FROM raw")
    cwd, found = store.changes_of(session)
    got = changes.render(found, "/work")
    assert got["diff"].endswith("@@ -1 +1 @@\n-x\n+y\n") and got["approximate_files"] == ["a.py"]


def test_the_api_gives_a_sessions_changes_to_the_owner_only(tmp_path):
    async def body(world: FaceWorld):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        world.lanes["code"].add(session, run["run"], *fixture("edit"))  # CLI 2.1.304: a Write, then an Edit
        world.lanes["code"].exited(session, run["run"])
        await world.poll()
        path = f"/api/sessions/{session}/diff"
        assert (await world.call("GET", path))[0] == 401, "nothing without a login"

        await world.login()
        assert await world.get(path) == {
            "cwd": "/work",
            "stat": " new.txt | 1 +\n old.txt | 2 +-\n 2 files changed, 2 insertions(+), 1 deletion(-)\n",
            "diff": "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n"
                    "@@ -0,0 +1 @@\n+hello\n"
                    "diff --git a/old.txt b/old.txt\n--- a/old.txt\n+++ b/old.txt\n@@ -1 +1 @@\n-one\n+two\n",
            "truncated": False, "approximate_files": []}
        status, data, _ = await world.call("GET", "/api/sessions/nope0000/diff")
        assert status == 404 and data["error"] == "Нет такой сессии."

        notes = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "заметка"}))["session"]
        assert (await world.get(f"/api/sessions/{notes}/diff"))["cwd"] == "", \
            "no line yet: the track's directory as configured (none for notes)"

    faced(tmp_path, body)
