"""The Base and the sessions: who changed what in the Base (windows of runs, overlapping ones, the file tools' cross-check,
a restart), and the platform's own commit after a turn — everything the session changed, said in one line under the
ping with «Undo», what kept a file out told to the session once and then to the owner, a restart mid-commit — against
a fake of the Base's clone (`base.BaseRepo`'s contract) and against the real one."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from dataclasses import dataclass

from retinue import tobase
from retinue.bridge import ping_keyboard, ping_parts
from retinue.config import BaseConfig, Sphere, Track
from test_face import FaceWorld
from test_kbcheck import RECORD, base, git, install_hub  # noqa: F401 — base: a fixture
from test_platformd import CLI_SESSION, DAY, short_wait  # noqa: F401 — the fixture is autouse


@dataclass
class Change:
    path: str
    status: str


@dataclass
class Committed:
    sha: str | None
    files: list
    conflicts: list
    refused: list
    left_out: list


class FakeBase:
    """The clone as G's contract has it: a tree the lane edits and the HEAD it was cloned at."""
    tree_rel = "base"
    lane = "private"

    def __init__(self) -> None:
        self.head: dict[str, str] = {}
        self.tree: dict[str, str] = {}
        self.blind: set[str] = set()       # paths the snapshot misses (a stale stat cache)
        self.conflicts: set[str] = set()   # paths the hub changed meanwhile: refused
        self.commits: list[tuple[list[str], str, str]] = []
        self.hold: asyncio.Event | None = None
        self.broken = False
        self.unreadable = False
        self.expected: list[dict] = []
        self.frozen = False                # the commit takes nothing: a test of who changed what keeps its lists
        self.reverted: list[tuple[str, list[str]]] = []
        self.marked: list[str] = []        # written with conflict markers, not yet taken
        self.check: str | None = None      # the Base's own check refuses the commit with this line

    def take_marked(self) -> list[str]:
        taken, self.marked = self.marked, []
        return taken

    def changed(self) -> list[Change]:
        if self.unreadable:
            raise OSError("git status failed")
        out = []
        for path in sorted(self.head.keys() | self.tree.keys()):
            if path not in self.head:
                out.append(Change(path, "A"))
            elif path not in self.tree:
                out.append(Change(path, "D"))
            elif self.head[path] != self.tree[path]:
                out.append(Change(path, "M"))
        return out

    def snapshot(self) -> dict[str, str]:
        if self.broken:
            raise OSError("the tree is gone")
        return {p: hashlib.sha256(t.encode()).hexdigest() for p, t in self.tree.items() if p not in self.blind}

    def diff(self, paths: list[str], limit: int) -> str:
        out = []
        for p in paths:
            out += difflib.unified_diff(self.head.get(p, "").splitlines(True), self.tree.get(p, "").splitlines(True),
                                        f"a/{p}", f"b/{p}")
        return "".join(out)[:limit]

    async def refresh(self, wait: bool = True):
        return None

    async def commit(self, paths: list[str], subject: str, body: str, expect: dict | None = None,
                     session: str = "", role: str = "owner") -> Committed:
        if self.hold is not None:
            await self.hold.wait()
        if self.frozen:
            return Committed(None, [], [], [], [f"{p}: not changed here" for p in paths])
        self.expected.append(dict(expect or {}))
        if self.check:
            return Committed(None, [], [], [f"just check: {self.check}"], [])
        now = {p: hashlib.sha256(t.encode()).hexdigest() for p, t in self.tree.items()}
        moved = [p for p in paths if expect is not None and now.get(p, "") != (expect.get(p) or "")]
        going = [p for p in paths if p not in self.conflicts and p not in moved]
        files = [c for c in self.changed() if c.path in going]
        for p in going:
            if p in self.tree:
                self.head[p] = self.tree[p]
            else:
                self.head.pop(p, None)
        self.commits.append((going, subject, body))
        sha = hashlib.sha1(repr(self.commits).encode()).hexdigest() if going else None
        return Committed(sha, files, [f"{p}: the hub already has another edit" for p in paths if p in self.conflicts],
                         [f"{p}: the file changed after the card" for p in moved], [])

    async def revert(self, sha: str, paths: list[str], subject: str, session: str = "") -> Committed:
        self.reverted.append((sha, list(paths)))
        for p in paths:
            self.tree.pop(p, None)
            self.head.pop(p, None)
        return Committed("u" + sha[:39], [Change(p, "D") for p in paths], [], [], [])

    def shown(self, sha: str, limit: int) -> str:
        return f"commit {sha}\n"[:limit]

    async def discard(self, expect: dict, wait: bool = False):
        now = {p: hashlib.sha256(t.encode()).hexdigest() for p, t in self.tree.items()}
        done = Discarded([], [])
        for p, digest in expect.items():
            if now.get(p, "") != (digest or ""):
                done.refused.append(f"{p}: changed after the card was opened")
                continue
            if p in self.head:
                self.tree[p] = self.head[p]
            else:
                self.tree.pop(p, None)
            done.files.append(p)
        return done


@dataclass
class Discarded:
    files: list
    refused: list


class BaseWorld(FaceWorld):
    """The platform with a `base` track (lane private, folder `base`), the travel sphere on it, and the fake clone."""

    async def start(self, **start) -> None:
        self.fake = FakeBase()
        (self.tmp / "work" / "base").mkdir(parents=True, exist_ok=True)
        await super().start(**start)

    async def up(self) -> None:
        self.cfg.tracks = [t for t in self.cfg.tracks if t.name != "base"] + [Track("base", "private", "private",
                                                                                     "base")]
        self.cfg.spheres = [Sphere("travel", "Путешествия", "base", "✈️")]
        self.cfg.base = BaseConfig(author="Владелец <o@example.com>", mount=str(self.tmp / "work" / "base"),
                                   git_dir=str(self.tmp / "platform" / "base.git"))
        await super().up()
        self.platform.base = self.fake

    async def new(self, text: str = "добавь отель", input_id: str = "tg:1", track: str = "base") -> str:
        return (await self.owner("new", {"input_id": input_id, "track": track, "text": text}))["session"]

    def run_of(self, session: str) -> dict:
        return [r for r in self.lanes["private"].runs() if r["session"] == session][-1]

    async def end(self, session: str, text: str = "Готово.", *lines: str, exit_: bool = True) -> None:
        run = self.run_of(session)
        self.lanes["private"].add(session, run["run"], *lines, result(text))
        if exit_:
            self.lanes["private"].exited(session, run["run"])
        await self.poll()
        await self.platform.tobase.idle()  # the platform's own commit after the run, if any

    def files(self, session: str) -> list[str]:
        return self.platform.tobase.paths(session)

    def base_press(self, label_start: str, notes: list[dict] | None = None) -> str:
        """The id of the newest button whose label starts so, in the notes' buttons."""
        for note in reversed(notes if notes is not None else self.notes()):
            data = note["data"]
            for label, button in (data.get("buttons") or []) + ([data["base"]] if data.get("base") else []):
                if label.startswith(label_start):
                    return button
        raise AssertionError(label_start)


def result(text: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                       "stop_reason": "end_turn", "session_id": CLI_SESSION})


def write_call(call_id: str, path: str, content: str) -> list[str]:
    """A Write and its result as the CLI journals them."""
    return [json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": "Write", "input": {"file_path": path, "content": content}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]},
                "tool_use_result": {"type": "create", "filePath": path, "content": content}})]


def bash_call(call_id: str, command: str) -> list[str]:
    """A Bash call and its result as the CLI journals them."""
    return [json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": ""}]}})]


def based(tmp_path, body, frozen: bool = False):
    async def go():
        world = BaseWorld(tmp_path)
        await world.start()
        world.fake.frozen = frozen
        try:
            return await body(world)
        finally:
            await world.platform.tobase.idle()
            await world.stop()
    return asyncio.run(go())


# --- who changed what ---------------------------------------------------------------------------------------------

def test_a_run_gets_what_changed_in_its_window_and_overlapping_runs_both_get_the_overlap(tmp_path):
    async def body(world: BaseWorld):
        world.fake.head = world.fake.tree.copy()
        world.fake.tree["MAP.md"] = "before"  # dirty before any run: no one's
        a = await world.new("добавь отель", "tg:1")
        b = await world.new("поправь маршрут", "tg:2")
        world.fake.tree["travel/f.md"] = "both saw it"
        await world.end(b)
        assert world.files(b) == ["travel/f.md"]
        assert world.files(a) == [], "a window is closed only by its own run's mark"
        world.fake.tree["home/g.md"] = "after b ended"
        await world.end(a)
        assert world.files(a) == ["home/g.md", "travel/f.md"]
        assert world.files(b) == ["travel/f.md"], "a run that ended takes nothing more"
        assert world.store.face_session(a)["base"] == 2 and world.store.face_session(b)["base"] == 1

        # A third run of a, with nothing changed: its list stays as it was.
        await world.owner("input", {"input_id": "tg:3", "session": a, "text": "ещё"})
        await world.end(a)
        assert world.files(a) == ["home/g.md", "travel/f.md"]
    based(tmp_path, body, frozen=True)


def test_the_file_tools_cross_check_what_the_snapshot_missed_and_a_session_outside_the_clone_gets_nothing(tmp_path):
    async def body(world: BaseWorld):
        world.fake.blind = {"travel/hotel.md"}
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "Отель"
        world.fake.tree["travel/seen.md"] = "both"
        await world.end(a, "Готово.", *write_call("w1", "/work/base/travel/hotel.md", "Отель"),
                        *write_call("w2", "/work/base/travel/seen.md", "both"),
                        *write_call("w3", "/work/elsewhere.md", "not the Base's"))
        rows = dict(world.store.db.execute("SELECT path, via FROM base_files WHERE session = ?", (a,)).fetchall())
        assert rows == {"travel/hotel.md": "tool", "travel/seen.md": "both"}

        # A notes session (its folder /work, not the clone's) that edits the Base gets its file too.
        notes = await world.new("заметка", "tg:9", track="notes")
        world.fake.tree["travel/other.md"] = "x"
        await world.end(notes, "Готово.", *write_call("w4", "/work/base/travel/other.md", "x"))
        assert world.files(notes) == ["travel/other.md"]
        # A session on another lane never: the Base is not there.
        code = await world.new("код", "tg:10", track="retinue")
        world.fake.tree["home/x.md"] = "x"
        run = world.lanes["code"].runs()[-1]
        world.lanes["code"].add(code, run["run"], result("Готово."))
        world.lanes["code"].exited(code, run["run"])
        await world.poll()
        assert world.files(code) == []
    based(tmp_path, body, frozen=True)


def test_a_file_a_run_wrote_by_bash_is_its_alone_and_runs_that_only_saw_it_change_get_nothing(tmp_path):
    async def body(world: BaseWorld):
        early = await world.new("сравни тарифы", "tg:1")
        writer = await world.new("обнови даты поездки", "tg:2")
        late = await world.new("ревью кода", "tg:3")
        world.fake.tree["MAP.md"] = "новые даты"
        world.fake.tree["home/AGENTS.md"] = "новые даты"
        await world.end(early, "Готово.", *bash_call("b0", "cat MAP.md home/AGENTS.md"))
        assert world.files(early) == ["MAP.md", "home/AGENTS.md"], "no run named them yet: its window saw them change"
        await world.end(writer, "Готово.", *bash_call("b1", "python3 - <<'EOF'\nfor name in ('MAP.md', 'home/AGENTS.md'):"
                                                            "\n    p = pathlib.Path(name)\n    p.write_text('x')\nEOF"))
        assert world.files(writer) == ["MAP.md", "home/AGENTS.md"]
        assert dict(world.store.db.execute("SELECT path, via FROM base_files WHERE session = ?", (writer,))) == \
            {"MAP.md": "bash", "home/AGENTS.md": "bash"}
        assert world.files(early) == [] and world.store.face_session(early)["base"] == 0, \
            "the writer takes them from a run that only saw them change"
        await world.end(late, "Готово.", *bash_call("b2", "grep -n дата MAP.md > /dev/null; cat home/AGENTS.md"))
        assert world.files(late) == [], "nor does a run that ends after the writer's mark get them"
    based(tmp_path, body, frozen=True)


def test_bash_writes_needs_a_write_and_the_file_named_whole():
    root = "/work/base"
    for command in ("sed -i 's/a/b/' MAP.md", "echo x >> /work/base/MAP.md", "cd home && perl -pi -e 's/a/b/' x.md",
                    "python3 - <<'EOF'\npathlib.Path('home/x.md').write_text('y')\nEOF", "mv /tmp/new.md home/x.md"):
        path = "MAP.md" if "MAP" in command else "home/x.md"
        assert tobase.bash_writes(command, path, root), command
    for command, path in (("cat MAP.md 2>/dev/null", "MAP.md"), ("sed -n 1,9p home/x.md", "home/x.md"),
                          ("cat travel/AGENTS.md > /tmp/out", "AGENTS.md"), ("grep -c a MAP.md > /dev/null", "MAP.md"),
                          ("echo y > /tmp/x.md; cat home/y.md", "home/x.md")):
        assert not tobase.bash_writes(command, path, root), command


def test_a_turn_inside_one_run_is_marked_at_its_result_and_a_restart_keeps_the_window(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new()
        world.fake.tree["travel/one.md"] = "1"
        await world.end(a, exit_=False)  # the run lives on: the owner's next message goes into it
        assert world.files(a) == ["travel/one.md"]
        await world.down()
        world.fake.tree["travel/two.md"] = "2"
        await world.up()
        await world.end(a, "И второе.")
        assert world.files(a) == ["travel/one.md", "travel/two.md"]
        assert world.store.db.execute("SELECT COUNT(*) FROM base_runs").fetchone()[0] == 0, "an ended run is done"
    based(tmp_path, body, frozen=True)


def test_a_file_committed_or_refreshed_leaves_every_list(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("a", "tg:1")
        b = await world.new("b", "tg:2")
        world.fake.tree["travel/f.md"] = "x"
        await world.end(a)
        await world.end(b)
        assert world.files(a) == world.files(b) == ["travel/f.md"]
        world.fake.head["travel/f.md"] = "x"  # the hub came with the same content: refreshed, clean
        world.clock.now += tobase.PRUNE_S
        await world.poll()
        assert world.files(a) == world.files(b) == []
        # Dirty again by b's next run: a's list does not come back.
        await world.owner("input", {"input_id": "tg:3", "session": b, "text": "ещё"})
        world.fake.tree["travel/f.md"] = "y"
        await world.end(b)
        assert world.files(a) == [] and world.files(b) == ["travel/f.md"]
    based(tmp_path, body, frozen=True)


# --- the ping and the card in Telegram ------------------------------------------------------------------------------

def test_the_snapshot_failing_costs_the_watch_not_the_run(tmp_path):
    async def body(world: BaseWorld):
        world.fake.broken = True
        a = await world.new()
        assert world.lanes["private"].runs()[-1]["session"] == a, "the run went to its lane"
        world.fake.broken = False
        world.fake.tree["travel/hotel.md"] = "x"
        world.fake.tree["travel/bash.md"] = "y"  # changed by Bash: the tools do not name it
        await world.end(a, "Готово.", *write_call("w1", "/work/base/travel/hotel.md", "x"))
        assert world.fake.commits[-1][0] == ["travel/hotel.md", "travel/bash.md"], \
            "its file tools still count, and a file no session claims goes with the next commit: never left behind"
    based(tmp_path, body)


def platform_inputs(world: BaseWorld, session: str) -> list[str]:
    return [t for (t,) in world.store.db.execute("SELECT text FROM inputs WHERE session = ? AND by = 'platform' "
                                                 "ORDER BY at, rowid", (session,))]


def keyboard_labels(ping: dict) -> list[str]:
    keyboard = ping_keyboard(ping) or {"inline_keyboard": []}
    return [b["text"] for row in keyboard["inline_keyboard"] for b in row]


def test_after_a_turn_everything_the_session_changed_is_committed_said_in_one_line_with_undo(tmp_path):
    async def body(world: BaseWorld):
        world.fake.head = {"travel/old.md": "старое", "travel/AGENTS.md": "витрина"}
        world.fake.tree = {**world.fake.head, "MAP.md": "до всех"}  # dirty before any run: no session's
        a = await world.new("добавь отель", "tg:1")
        world.fake.tree["travel/hotel.md"] = "отель"
        world.fake.tree["travel/AGENTS.md"] = "витрина\n- [Отель](hotel.md)"
        del world.fake.tree["travel/old.md"]
        await world.end(a)
        went = world.fake.commits[-1][0]
        assert sorted(went) == ["MAP.md", "travel/AGENTS.md", "travel/hotel.md", "travel/old.md"], \
            "added, edited, deleted, and the file no session claimed"
        assert world.files(a) == [] and world.fake.changed() == []
        ping = world.notes("ping")[-1]["data"]
        [card] = world.platform.tobase.cards(a)
        sha = card["result"]["sha"]
        line = ping["base_line"]
        assert ", ".join(sorted(went)[:3]) in line and "+1" in line and sha[:7] in line
        assert len(keyboard_labels(ping)) == 1 and "4" in keyboard_labels(ping)[0], "no «To the Base» to press: it is written"
        assert ping_parts(ping)[-1].endswith(f"🗂 {ping['base_line']}")
        face = world.platform.tobase.face(card)
        assert face["state"] == "committed" and face["said"] == ping["base_line"] and face["undo"] is True
        assert (await world.platform.tobase.diff(card)).startswith("commit "), "the diff is the commit's own"
        assert platform_inputs(world, a) == [], "nothing for the session to fix"

        said = await world.owner("press", {"button": ping["undo"][1]})
        assert said["toast"].startswith("Undone:")
        assert world.fake.reverted == [(sha, went)]
        assert world.platform.tobase.card(card["id"])["state"] == "undone"
        assert world.notes("ping")[-1]["data"]["undo"] is None, "the ping loses «Undo»"
        assert (await world.owner("press", {"button": ping["undo"][1]}))["toast"] == "Already undone."
    based(tmp_path, body)


def test_a_file_two_sessions_changed_goes_once_when_the_lane_is_idle(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("одно", "tg:1")
        b = await world.new("другое", "tg:2")
        world.fake.tree["travel/both.md"] = "обе"
        await world.end(b)
        assert world.fake.commits == [], "a run is still open on the lane: whose a file is, is not known yet"
        await world.end(a, exit_=False)  # a turn ended, the run still open: nothing is committed yet
        assert world.fake.commits == []
        world.lanes["private"].exited(a, world.run_of(a)["run"])
        await world.poll()
        await world.platform.tobase.idle()
        assert [c[0] for c in world.fake.commits] == [["travel/both.md"]]
        assert world.files(a) == world.files(b) == []
    based(tmp_path, body)


def test_what_kept_files_out_goes_to_the_session_once_and_then_to_the_owner(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("добавь отель", "tg:1")
        world.fake.tree["travel/hotel.md"] = "отель"
        world.fake.check = "ОШИБКА: travel/hotel.md: нет строки в витрине travel/AGENTS.md"
        await world.end(a)
        refused = f"just check: {world.fake.check}"
        [asked] = platform_inputs(world, a)
        assert asked == tobase.REFUSED_ASK.format(lines=f"- {refused}")
        ping = world.notes("ping")[-1]["data"]
        assert ping["base_line"] == f"Not written: {refused}. {tobase.ASKED}" and ping["undo"] is None
        assert world.platform.tobase.cards(a)[-1]["state"] == "failed" and world.files(a) == ["travel/hotel.md"]

        await world.end(a, "Не вышло.")  # its turn on the platform's message: the same refusal again
        assert len(platform_inputs(world, a)) == 1, "told once"
        assert world.notes("ping")[-1]["data"]["base_line"] == f"Not written: {refused}. {tobase.NEEDS_YOU}"

        world.fake.check = None  # the owner fixed it, or the session did on his word
        await world.owner("input", {"input_id": "tg:2", "session": a, "text": "строку добавил, пиши"})
        await world.end(a)
        assert "travel/hotel.md" in world.notes("ping")[-1]["data"]["base_line"]
        assert world.store.db.execute("SELECT COUNT(*) FROM base_asks").fetchone()[0] == 0, "asked afresh next time"
    based(tmp_path, body)


def test_a_file_marked_with_both_versions_goes_to_the_sessions_that_changed_it(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("одно", "tg:1")
        b = await world.new("другое", "tg:2")
        world.fake.tree["travel/hotel.md"] = "x\n"
        await world.end(a, "Готово.", *write_call("w1", "/work/base/travel/hotel.md", "x\n"))
        world.fake.tree["travel/other.md"] = "y\n"
        await world.end(b, "Готово.", *write_call("w2", "/work/base/travel/other.md", "y\n"))
        world.fake.marked = ["travel/hotel.md", "travel/other.md", "travel/nobody.md"]
        await world.poll()
        assert platform_inputs(world, a) == [tobase.MERGE_ASK.format(files="travel/hotel.md")]
        assert platform_inputs(world, b) == [tobase.MERGE_ASK.format(files="travel/other.md")]
        await world.poll()
        assert len(platform_inputs(world, a)) == 1, "told once per marking"
    based(tmp_path, body, frozen=True)


def test_a_run_on_the_lane_waits_for_a_commit_in_flight_and_the_digests_go_to_the_base(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("добавь отель", "tg:1")
        world.fake.tree["travel/hotel.md"] = "отель"
        world.fake.hold = asyncio.Event()
        run = world.run_of(a)
        world.lanes["private"].add(a, run["run"], result("Готово."))
        world.lanes["private"].exited(a, run["run"])
        await world.poll()
        runs = len(world.lanes["private"].runs())
        said = await world.owner("input", {"input_id": "tg:77", "session": a, "text": "ещё"})
        other = await world.new("другая", "tg:78", track="notes")
        assert said["say"] and other
        await world.poll()
        assert len(world.lanes["private"].runs()) == runs, "no run on the lane starts while the commit stages"
        world.fake.hold.set()
        await world.platform.tobase.idle()
        assert len(world.lanes["private"].runs()) == runs + 2, "both go once it is done, without a poll"
        digest = hashlib.sha256("отель".encode()).hexdigest()
        assert world.fake.expected == [{"travel/hotel.md": digest}]
    based(tmp_path, body)


def test_a_commit_a_restart_cut_is_settled_on_the_clock_once_the_base_reads_again(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("добавь отель", "tg:1")
        world.fake.tree["travel/hotel.md"] = "отель"
        session = world.store.session(a)
        card = world.platform.tobase._new(session, await world.platform.tobase._files(
            ["travel/hotel.md"], {"travel/hotel.md": "A"}, world.fake.snapshot()), world.clock.now)
        world.store.db.commit()
        world.fake.head["travel/hotel.md"] = "отель"  # it went in before the restart
        world.fake.unreadable = True
        await world.platform.tobase.recover()
        assert world.platform.tobase.card(card["id"])["state"] == "committing"
        world.fake.unreadable = False
        world.platform.tick()
        await world.platform.tobase._recovering
        done = world.platform.tobase.card(card["id"])
        assert done["state"] == "committed"
    based(tmp_path, body, frozen=True)


def test_a_card_from_before_is_closed_and_an_old_to_the_base_button_says_it_is_gone(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("добавь отель", "tg:1")
        world.store.db.execute("INSERT INTO base_cards (id, session, state, files, fallback, created, at) "
                               "VALUES ('c0ffee00', ?, 'shown', '[]', 'travel: x', 0, 0)", (a,))
        world.store.db.commit()
        await world.down()
        await world.up()
        assert world.platform.tobase.card("c0ffee00")["state"] == "expired"
        button = world.store.add_button("base/open", "open", a, world.clock.now + 60)
        assert (await world.owner("press", {"button": button}))["toast"] == tobase.GONE
    based(tmp_path, body, frozen=True)


def test_no_base_configured_changes_nothing(tmp_path):
    async def body(world: BaseWorld):
        world.platform.base = None
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "x"
        await world.end(a)
        assert world.files(a) == [] and world.fake.commits == []
        assert world.notes("ping") == [] and "base_line" not in world.notes("summary")[-1]["data"]
    based(tmp_path, body)


class RealWorld(BaseWorld):
    """The platform with its own `BaseRepo` (part G) over a real hub in a temporary folder."""

    hub = None

    async def up(self) -> None:
        self.cfg.tracks = [t for t in self.cfg.tracks if t.name != "base"] + [Track("base", "private", "private",
                                                                                     "base")]
        self.cfg.spheres = []
        self.cfg.base = BaseConfig(author="Иван <owner@example.com>", hub=str(self.hub),
                                   mount=str(self.tmp / "work" / "base"), git_dir=str(self.tmp / "platform" / "base.git"))
        await FaceWorld.up(self)
        await self.platform.base.refresh()

    async def start(self, **start) -> None:
        (self.tmp / "work" / "base").mkdir(parents=True, exist_ok=True)
        await FaceWorld.start(self, **start)


def test_with_the_real_base_the_platform_commits_a_new_record_and_undo_reverts_it_on_the_hub(tmp_path, base):
    hub = install_hub(tmp_path, base)

    async def body(world: RealWorld):
        tree = world.platform.base.tree
        a = await world.new("запиши факт", "tg:1")
        (tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Claude Code (Opus 5.5)", body="Б."))
        await world.end(a)
        message = git(hub, "log", "-1", "--format=%B", "main")
        assert "Pushed-by: retinue-platform" in message
        # why the Record changed: the turn's summary, and the way back to that turn in the app
        summary = world.notes("summary")[-1]["data"]
        assert f"\n\n{tobase.why(summary)}\n\nRetinue: session " in message
        assert f"Retinue: session {a}, after turn {summary['turn']}." in message
        assert f"/s/{a}?turn={summary['turn']}" in message
        assert world.files(a) == []
        ping = world.notes("ping")[-1]["data"]
        assert "1" in ping["undo"][0]
        await world.owner("press", {"button": ping["undo"][1]})
        assert git(hub, "log", "-1", "--format=%s", "main").startswith("Undo: ")
        assert "notes/knowledge/b.md" not in git(hub, "ls-tree", "-r", "--name-only", "main")
        assert not (tree / "notes/knowledge/b.md").exists()

    async def go():
        world = RealWorld(tmp_path)
        world.hub = hub
        await world.start()
        try:
            await body(world)
        finally:
            await world.platform.tobase.idle()
            await world.stop()
    asyncio.run(go())


def test_a_commits_why_is_the_summary_clean_and_cut_at_a_line():
    assert tobase.why(None) == "" and tobase.why({"text": 3}) == ""
    assert tobase.why({"text": "Done.\x1b[31m  \nNot checked: the price.\x00"}) == "Done.[31m\nNot checked: the price."
    text = "\n".join(f"line {k} " + "x" * 40 for k in range(100))
    cut = tobase.why({"text": text}, limit=500)
    assert len(cut) <= 502 and cut.endswith("\n…") and cut.split("\n")[-2].startswith("line ")
    assert tobase.why({"text": "y" * 900}, limit=500) == "y" * 500 + "\n…"

