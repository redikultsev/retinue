"""«To the Base»: who changed what in the Base (windows of runs, overlapping ones, the file tools' cross-check, a restart),
the ping's button, the card in Telegram and in the app (toggles, the subject by a reply, «Open diff», a commit once
whichever place pressed it, a refusal, a restart mid-commit, staleness, expiry), the feed's row and `/wait` — against
a fake of the Base's clone (`base.BaseRepo`'s contract), which part G builds."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from dataclasses import dataclass

from retinue import tobase
from retinue.bridge import CARD, PING, ping_keyboard
from retinue.config import BaseConfig, Sphere, Track
from test_bridge import make
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
                     session: str = "") -> Committed:
        if self.hold is not None:
            await self.hold.wait()
        self.expected.append(dict(expect or {}))
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


def based(tmp_path, body):
    async def go():
        world = BaseWorld(tmp_path)
        await world.start()
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
    based(tmp_path, body)


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
    based(tmp_path, body)


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
    based(tmp_path, body)


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
    based(tmp_path, body)


# --- the ping and the card in Telegram ------------------------------------------------------------------------------

def test_the_ping_gets_the_button_its_press_sends_the_card_and_a_newer_ping_takes_the_button(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "Отель\n"
        await world.end(a, "Добавил.")
        [summary] = world.notes("summary")
        [ping] = world.notes("ping")
        assert ping["data"]["seq"] == summary["seq"] and ping["data"]["base"][0] == "To the Base · 1"
        keyboard = ping_keyboard(ping["data"])
        assert [row[0]["text"] for row in keyboard["inline_keyboard"]] == ["To the Base · 1"]

        pressed = await world.owner("press", {"button": ping["data"]["base"][1]})
        assert pressed == {"toast": "The card is below."}
        [card] = world.notes("card")
        assert card["approval"].startswith(tobase.KEY)
        view = card["data"]["view"]
        assert view["head"] == [f"To the Base · base · session {a}", "Subject: travel: добавь отель"]
        assert view["body"] == "☑ A travel/hotel.md +1 −0"
        assert [label for label, _ in card["data"]["buttons"]] == ["☑ A travel/hotel.md", "Commit",
                                                                    "Open diff", "Cancel"]

        # The next turn: its ping carries the button; the old ping loses it and the open card goes stale.
        await world.owner("input", {"input_id": "tg:2", "session": a, "text": "ещё один"})
        world.fake.tree["travel/second.md"] = "2"
        await world.end(a, "Добавил второй.")
        pings_ = world.notes("ping")
        assert [p["data"]["seq"] for p in pings_ if p["data"].get("base") is None][-1] == summary["seq"]
        assert pings_[-1]["data"]["base"][0] == "To the Base · 2"
        card_id = card["approval"][len(tobase.KEY):]
        assert world.platform.tobase.card(card_id)["state"] == "stale"
        stale = [n for n in world.notes("update") if n["approval"] == card["approval"]][-1]
        assert stale["data"]["buttons"] == [] and "Expired" in stale["data"]["view"]["status"]
        assert (await world.owner("press", {"button": card["data"]["buttons"][1][1]}))["toast"] == tobase.GONE
    based(tmp_path, body)


async def opened(world: BaseWorld, *paths: str, text: str = "добавь отель") -> tuple[str, dict, dict]:
    """A session that changed `paths`, and its card opened from the ping's button: (session, card note, card)."""
    a = await world.new(text, f"tg:{100 + len(world.store.db.execute('SELECT 1 FROM sessions').fetchall())}")
    for p in paths:
        world.fake.tree[p] = f"{p}\n"
    await world.end(a)
    await world.owner("press", {"button": world.notes("ping")[-1]["data"]["base"][1]})
    note = world.notes("card")[-1]
    return a, note, world.platform.tobase.card(note["approval"][len(tobase.KEY):])


def test_toggles_a_subject_by_reply_the_diff_and_one_commit_however_often_pressed(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md", "travel/route.md")
        buttons = dict((label, button) for label, button in note["data"]["buttons"])
        pressed = await world.owner("press", {"button": buttons["☑ A travel/route.md"]})
        assert pressed["toast"] == "" and "☐ A travel/route.md" in pressed["view"]["body"]
        assert [label for label, _ in pressed["keep"]][:2] == ["☑ A travel/hotel.md", "☐ A travel/route.md"]
        assert pressed["keep"][1][1] == buttons["☑ A travel/route.md"], "a toggle stays live, relabelled"

        said = await world.owner("input", {"input_id": "tg:50", "session": a, "text": "(by voice) travel: отель в Зурбагане",
                                           "card": note["approval"]})
        assert said == {"say": "Commit subject: «travel: отель в Зурбагане»"}
        assert world.store.input_session("tg:50") is None, "a subject is no message into the session"
        update = [n for n in world.notes("update") if n["approval"] == note["approval"]][-1]
        assert update["data"]["view"]["head"][1] == "Subject: travel: отель в Зурбагане"

        diff = await world.owner("press", {"button": buttons["Open diff"]})
        assert diff["document"]["name"] == f"base-{card['id']}.diff"
        assert "+++ b/travel/hotel.md" in diff["document"]["text"] and "route" not in diff["document"]["text"]

        world.fake.hold = asyncio.Event()
        first = await world.owner("press", {"button": buttons["Commit"]})
        assert first["toast"] == "Committing…" and first["keep"] == [] and "Committing…" in first["view"]["status"]
        again = await world.owner("press", {"button": buttons["Commit"]})
        assert again == {"toast": "Committing — wait."}
        await world.login()
        status, in_app, _ = await world.call("POST", f"/api/base/{card['id']}/commit", {})
        assert status == 200 and in_app["toast"] == "Committing — wait."
        world.fake.hold.set()
        await world.platform.tobase.idle()
        assert world.fake.commits == [(["travel/hotel.md"], "travel: отель в Зурбагане",
                                       f"Retinue: session {a}, card {card['id']}.")]
        done = world.platform.tobase.card(card["id"])
        assert done["state"] == "committed" and done["result"]["files"] == ["A travel/hotel.md"]
        last = [n for n in world.notes("update") if n["approval"] == note["approval"]][-1]
        assert last["data"]["buttons"] == [] and "Committed: " in last["data"]["view"]["status"]
        assert world.files(a) == ["travel/route.md"], "the file turned off stays on the list"
        assert world.notes("ping")[-1]["data"]["base"][0] == "To the Base · 1"
        later = await world.owner("press", {"button": buttons["Commit"]})
        assert later["toast"] == "Already committed."
        assert len(world.fake.commits) == 1
    based(tmp_path, body)


def test_a_refused_file_stays_and_the_card_says_why(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md", "travel/route.md")
        world.fake.conflicts = {"travel/route.md"}
        commit = dict((label, b) for label, b in note["data"]["buttons"])["Commit"]
        await world.owner("press", {"button": commit})
        await world.platform.tobase.idle()
        done = world.platform.tobase.card(card["id"])
        assert done["state"] == "committed"
        assert done["result"]["refused"] == ["conflict: travel/route.md: the hub already has another edit"]
        assert "Left out, still uncommitted" in done["result"]["said"]
        assert world.files(a) == ["travel/route.md"]

        world.fake.conflicts = {"travel/route.md"}
        await world.owner("press", {"button": world.notes("ping")[-1]["data"]["base"][1]})
        second = world.notes("card")[-1]
        await world.owner("press", {"button": dict((label, b) for label, b in second["data"]["buttons"])["Commit"]})
        await world.platform.tobase.idle()
        failed = world.platform.tobase.card(second["approval"][len(tobase.KEY):])
        assert failed["state"] == "failed" and failed["result"]["said"].startswith("Nothing committed.")
    based(tmp_path, body)


def test_a_file_changed_since_the_card_opened_is_not_committed_unseen_and_a_running_session_waits(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        commit = dict((label, b) for label, b in note["data"]["buttons"])["Commit"]
        world.fake.tree["travel/hotel.md"] = "Отель\nи ещё строка\n"
        pressed = await world.owner("press", {"button": commit})
        assert pressed["toast"].startswith("Files changed after the card was opened: travel/hotel.md")
        assert "+2 −0" in pressed["view"]["body"] and world.fake.commits == []

        await world.owner("input", {"input_id": "tg:7", "session": a, "text": "ещё"})  # a run of it starts
        assert (await world.owner("press", {"button": commit}))["toast"] == tobase.RUNNING
        assert world.fake.commits == []
    based(tmp_path, body)


def test_a_restart_mid_commit_never_commits_again_without_a_press(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        commit = dict((label, b) for label, b in note["data"]["buttons"])["Commit"]
        world.fake.hold = asyncio.Event()
        await world.owner("press", {"button": commit})
        task = world.platform.tobase._commits[card["id"]]
        task.cancel()  # the platform went down mid-commit, before the push
        await asyncio.gather(task, return_exceptions=True)
        await world.down()
        await world.up()
        await world.platform.tobase.recover()
        again = world.platform.tobase.card(card["id"])
        assert again["state"] == "shown" and again["result"]["said"] == tobase.RESTARTED
        assert world.fake.commits == []
        world.fake.hold = None
        await world.owner("press", {"button": commit})
        await world.platform.tobase.idle()
        assert world.platform.tobase.card(card["id"])["state"] == "committed" and len(world.fake.commits) == 1

        # Pushed but not recorded: the clone has it, so the card says it went in, and nothing is pushed again.
        b, note2, card2 = await opened(world, "notes/todo.md", text="заметки")
        world.store.db.execute("UPDATE base_cards SET state = 'committing' WHERE id = ?", (card2["id"],))
        world.store.db.commit()
        world.fake.head["notes/todo.md"] = world.fake.tree["notes/todo.md"]
        await world.platform.tobase.recover()
        settled = world.platform.tobase.card(card2["id"])
        assert settled["state"] == "committed" and settled["result"]["said"].startswith("Committed before the platform restarted")
        assert len(world.fake.commits) == 1 and world.files(b) == []
    based(tmp_path, body)


def test_a_card_expires_and_cancel_keeps_the_files(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        cancel = dict((label, b) for label, b in note["data"]["buttons"])["Cancel"]
        assert (await world.owner("press", {"button": cancel}))["toast"] == "Cancelled."
        assert world.platform.tobase.card(card["id"])["state"] == "cancelled" and world.files(a) == ["travel/hotel.md"]

        await world.owner("press", {"button": world.notes("ping")[-1]["data"]["base"][1]})
        second = world.notes("card")[-1]["approval"][len(tobase.KEY):]
        world.clock.now += 7 * DAY
        world.platform.tick()
        assert world.platform.tobase.card(second)["state"] == "expired"
    based(tmp_path, body)


def test_a_long_list_is_cut_on_the_telegram_card_and_toggles_stop_at_the_cap(tmp_path):
    async def body(world: BaseWorld):
        paths = [f"travel/{'x' * 60}-{n:03}.md" for n in range(80)]
        _, note, card = await opened(world, *paths)
        view = note["data"]["view"]
        assert len(view["body"]) <= tobase.LISTED_CHARS + 100
        assert view["body"].splitlines()[-1].startswith("…and ")
        toggles = [label for label, _ in note["data"]["buttons"] if label[0] in "☑☐"]
        assert len(toggles) == tobase.MAX_TOGGLES and "Toggles are on the first 12" in view["status"]
        assert len(card["files"]) == 80 and [f["add"] for f in card["files"]].count(None) == 80 - tobase.COUNTED
    based(tmp_path, body)


def test_other_sessions_are_counted_not_named_and_a_shared_file_is_marked(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new("a", "tg:1")
        b = await world.new("b", "tg:2")
        world.fake.tree["travel/shared.md"] = "x"
        await world.end(a)
        world.fake.tree["personal/b-only.md"] = "y"
        await world.end(b)
        toast, card = await world.platform.tobase.open(a, telegram=False)
        assert card["files"] == [{"path": "travel/shared.md", "status": "A", "add": 1, "del": 0, "on": True,
                                  "shared": True, "nobody": False}]
        assert card["others"] == {"files": 2, "sessions": 1}
        assert "personal" not in json.dumps(card["view"], ensure_ascii=False), "which files: never"
        assert "⇄" in card["view"]["body"] and "2 more files uncommitted in other sessions (1 session)." in \
            card["view"]["status"]
    based(tmp_path, body)


# --- the app, the feed, `/wait` -------------------------------------------------------------------------------------

def test_the_app_opens_the_same_card_and_its_decisions_rewrite_the_telegram_copy(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md", "travel/route.md")
        await world.login()
        status, data, _ = await world.call("POST", f"/api/sessions/{a}/base", {})
        assert status == 200 and data["card"]["id"] == card["id"], "one card per session, in both places"
        assert (await world.get(f"/api/base/{card['id']}"))["card"]["subject"] == "travel: добавь отель"
        assert [c["id"] for c in (await world.get(f"/api/sessions/{a}"))["base_cards"]] == [card["id"]]
        assert (await world.get("/api/sessions"))["sessions"][0]["base"] == 2

        status, data, _ = await world.call("POST", f"/api/base/{card['id']}/toggle", {"path": "travel/route.md",
                                                                                    "on": False})
        assert status == 200 and [f["on"] for f in data["card"]["files"]] == [True, False]
        status, data, _ = await world.call("POST", f"/api/base/{card['id']}/subject", {"subject": "travel: отель"})
        assert data["card"]["subject"] == "travel: отель" and data["card"]["custom"] is True
        update = [n for n in world.notes("update") if n["approval"] == note["approval"]][-1]
        assert update["data"]["view"]["head"][1] == "Subject: travel: отель" and \
            "☐ A travel/route.md" in update["data"]["view"]["body"]
        events = [json.loads(d) for _, k, d in world.store.events_after(0, 10_000) if k == "base"]
        assert events[-1]["subject"] == "travel: отель"

        diff = await world.get(f"/api/base/{card['id']}/diff")
        assert "travel/hotel.md" in diff["diff"] and "route" not in diff["diff"] and diff["truncated"] is False
        status, data, _ = await world.call("POST", f"/api/base/{card['id']}/commit", {})
        assert data["toast"] == "Committing…" and data["card"]["state"] == "committing"
        await world.platform.tobase.idle()
        assert (await world.get(f"/api/base/{card['id']}"))["card"]["state"] == "committed"
        status, data, _ = await world.call("POST", f"/api/base/{card['id']}/toggle", {"path": "x", "on": "yes"})
        assert status == 400
        assert (await world.call("GET", "/api/base/ffffffff"))[0] == 404
    based(tmp_path, body)


def test_the_feed_has_a_row_to_decide_and_wait_says_how(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "x"
        world.fake.tree["travel/route.md"] = "y"
        await world.end(a)
        rows = [r for r in world.platform.feed.rows(world.clock()) if r["source"] == "base"]
        assert [(r["action"], r["essence"], r["base"], r["session"]) for r in rows] == \
            [("decide", "To the Base: 2 files", 2, a)]
        told = world.platform.feed.telegram(world.clock())
        [row] = [r for r in told["rows"] if r["source"] == "base"]
        assert row["hint"] == "Press «To the Base» under its result or open it in the app." and row["approval"] is None
    based(tmp_path, body)


def test_no_base_configured_changes_nothing(tmp_path):
    async def body(world: BaseWorld):
        world.platform.base = None
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "x"
        await world.end(a)
        assert world.files(a) == [] and world.notes("ping") == []
        assert await world.platform.tobase.open(a, telegram=True) == (tobase.NO_BASE, None)
    based(tmp_path, body)


# --- the bridge ----------------------------------------------------------------------------------------------------

def test_the_bridge_puts_the_button_on_the_ping_takes_it_off_and_links_a_card_to_its_page(tmp_path):
    data = {"head": "Итог", "text": "Готово.", "kind": "done", "place": "base", "title": "t",
            "base": ["To the Base · 2", "p.base"]}
    ch, bridge, platform = make(tmp_path, [
        {"seq": 1, "kind": "summary", "session": "ab12cd34", "data": data},
        {"seq": 2, "kind": "ping", "session": "ab12cd34", "data": {**data, "base": None, "seq": 1}},
        {"seq": 3, "kind": "card", "session": "ab12cd34", "approval": "base/c0ffee00",
         "data": {"view": {"head": ["To the Base · base · session ab12cd34", "Subject: t"], "body": "☑ M a.md", "status": "",
                           "link": []}, "buttons": [["Commit", "p.commit"]]}}])
    bridge.face_url = "https://app.example"
    asyncio.run(bridge.deliver())
    (_, first, _), (_, edited, _), (_, card, _) = ch.sent
    assert [row[0]["text"] for row in first["reply_markup"]["inline_keyboard"]] == ["To the Base · 2"]
    assert ch.store.sent_native(PING, "1") == ["900"] and edited["message_id"] == 900
    assert "reply_markup" not in edited and edited["text"] == first["text"], "the button taken off, the text kept"
    assert card["reply_markup"]["inline_keyboard"][-1] == [{"text": "Open in the app",
                                                            "web_app": {"url": "https://app.example/b/c0ffee00"}}]
    assert ch.store.sent_event(CARD, "902") == "base/c0ffee00"


def test_the_snapshot_failing_costs_the_watch_not_the_run(tmp_path):
    async def body(world: BaseWorld):
        world.fake.broken = True
        a = await world.new()
        assert world.lanes["private"].runs()[-1]["session"] == a, "the run went to its lane"
        world.fake.broken = False
        world.fake.tree["travel/hotel.md"] = "x"
        world.fake.tree["travel/bash.md"] = "y"  # changed by Bash: the tools do not name it
        await world.end(a, "Готово.", *write_call("w1", "/work/base/travel/hotel.md", "x"))
        assert world.files(a) == ["travel/hotel.md"], "its file tools still count"
        assert world.platform.tobase.nobodys() == ["travel/bash.md"], "never invisible: nobody's"
    based(tmp_path, body)


def test_nobodys_files_are_listed_off_on_every_card_and_have_a_feed_row(tmp_path):
    async def body(world: BaseWorld):
        world.fake.tree["MAP.md"] = "changed with no session"
        a = await world.new()
        await world.end(a)  # it changed nothing itself
        assert world.files(a) == [] and world.platform.tobase.nobodys() == ["MAP.md"]
        [row] = [r for r in world.platform.feed.rows(world.clock()) if r["source"] == "base"]
        assert (row["title"], row["session"], row["base"]) == ("Nobody's Base files", a, 1)
        toast, card = await world.platform.tobase.open(a, telegram=True)
        assert [(f["path"], f["on"], f["nobody"]) for f in card["files"]] == [("MAP.md", False, True)]
        assert card["view"]["body"] == "☐ A MAP.md +1 −0 · nobody's" and "«nobody's»" in card["view"]["status"]
        await world.login()
        status, _, _ = await world.call("POST", f"/api/base/{card['id']}/toggle", {"path": "MAP.md", "on": True})
        assert status == 200
        await world.call("POST", f"/api/base/{card['id']}/commit", {})
        await world.platform.tobase.idle()
        assert world.fake.commits[-1][0] == ["MAP.md"] and world.platform.tobase.nobodys() == []
    based(tmp_path, body)


def test_a_run_on_the_lane_waits_for_a_commit_in_flight_and_the_digests_go_to_the_base(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        world.fake.hold = asyncio.Event()
        await world.owner("press", {"button": dict((label, b) for label, b in note["data"]["buttons"])["Commit"]})
        runs = len(world.lanes["private"].runs())
        said = await world.owner("input", {"input_id": "tg:77", "session": a, "text": "ещё"})
        other = await world.new("другая", "tg:78", track="notes")
        assert said["say"] == "Accepted — the session is waiting for room on the lane." and other
        await world.poll()
        assert len(world.lanes["private"].runs()) == runs, "no run on the lane starts while the commit stages"
        world.fake.hold.set()
        await world.platform.tobase.idle()
        assert len(world.lanes["private"].runs()) == runs + 2, "both go once it is done, without a poll"
        assert world.fake.expected == [{"travel/hotel.md": card["files"][0]["digest"]}]
    based(tmp_path, body)


def test_a_file_the_base_finds_moved_under_its_lock_is_refused(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md", "travel/route.md")
        world.fake.hold = asyncio.Event()
        await world.owner("press", {"button": dict((label, b) for label, b in note["data"]["buttons"])["Commit"]})
        world.fake.tree["travel/route.md"] = "moved after the check"
        world.fake.hold.set()
        await world.platform.tobase.idle()
        done = world.platform.tobase.card(card["id"])
        assert done["result"]["refused"] == ["travel/route.md: the file changed after the card"]
        assert world.files(a) == ["travel/route.md"]
    based(tmp_path, body)


def test_a_card_left_committing_is_settled_on_the_clock_once_the_base_reads_again(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        world.store.db.execute("UPDATE base_cards SET state = 'committing' WHERE id = ?", (card["id"],))
        world.store.db.commit()
        world.fake.unreadable = True
        await world.platform.tobase.recover()
        assert world.platform.tobase.card(card["id"])["state"] == "committing"
        world.fake.unreadable = False
        world.platform.tick()
        await world.platform.tobase._recovering
        assert world.platform.tobase.card(card["id"])["state"] == "shown"
    based(tmp_path, body)


def test_a_reply_to_a_card_no_longer_shown_is_taken_and_never_goes_into_the_session(tmp_path):
    async def body(world: BaseWorld):
        a, note, card = await opened(world, "travel/hotel.md")
        await world.owner("press", {"button": dict((label, b) for label, b in note["data"]["buttons"])["Cancel"]})
        said = await world.owner("input", {"input_id": "tg:60", "session": a, "text": "новая тема",
                                           "card": note["approval"]})
        assert said == {"say": tobase.REOPEN} and world.store.input_session("tg:60") is None
    based(tmp_path, body)


def test_a_telegram_copy_stays_under_a_message_and_commits_only_what_it_lists(tmp_path):
    async def body(world: BaseWorld):
        paths = [f"travel/{'x' * 60}-{n:03}.md" for n in range(80)]
        world.fake.conflicts = set(paths[:30])
        _, note, card = await opened(world, *paths)
        listed = len(note["data"]["view"]["body"].splitlines()) - 1
        assert "not committed from Telegram" in note["data"]["view"]["body"].splitlines()[-1]
        await world.owner("press", {"button": dict((label, b) for label, b in note["data"]["buttons"])["Commit"]})
        await world.platform.tobase.idle()
        assert len(world.fake.commits[-1][0]) + 30 == listed, "only the listed files went (the conflicts refused)"
        view = [n for n in world.notes("update") if n["approval"] == note["approval"]][-1]["data"]["view"]
        assert len("\n".join(view["head"]) + view["body"] + view["status"]) < 4000
        assert "…and " in view["status"]
    based(tmp_path, body)


def test_with_the_base_off_its_old_rows_show_nowhere(tmp_path):
    async def body(world: BaseWorld):
        a = await world.new()
        world.fake.tree["travel/hotel.md"] = "x"
        await world.end(a)
        assert world.notes("ping")[-1]["data"]["base"]
        world.platform.base = None
        await world.poll()
        assert world.notes("ping")[-1]["data"]["base"] is None, "the ping's button taken off"
        assert world.store.face_session(a)["base"] == 0
        assert not [r for r in world.platform.feed.rows(world.clock()) if r["source"] == "base"]
    based(tmp_path, body)


def test_a_toast_longer_than_telegram_takes_is_cut(tmp_path):
    ch, bridge, platform = make(tmp_path)
    platform.answers["press"] = {"toast": "x" * 500}
    asyncio.run(bridge.press("q1", "p.x", None))
    [(_, params, _)] = ch.sent
    assert len(params["text"]) == 200 and params["text"].endswith("…")


# --- with the real Base: a hub on disk, the clone, git --------------------------------------------------------------

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


def test_with_the_real_base_a_press_commits_to_the_hub_and_a_file_moved_after_the_card_is_refused(tmp_path, base):
    hub = install_hub(tmp_path, base)

    async def body(world: RealWorld):
        tree = world.platform.base.tree
        a = await world.new("уточни факт", "tg:1")
        (tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Факт, уточнён."))
        await world.end(a)
        assert world.files(a) == ["notes/knowledge/a.md"]
        ping = world.notes("ping")[-1]["data"]
        assert ping["base"][0] == "To the Base · 1"
        await world.owner("press", {"button": ping["base"][1]})
        card = world.notes("card")[-1]
        assert card["data"]["view"]["head"][1] == "Subject: notes: уточни факт"
        await world.owner("press", {"button": dict((label, b) for label, b in card["data"]["buttons"])["Commit"]})
        await world.platform.tobase.idle()
        done = world.platform.tobase.card(card["approval"][len(tobase.KEY):])
        assert done["state"] == "committed", done["result"]
        message = git(hub, "log", "-1", "--format=%B", "main")
        assert message.startswith("notes: уточни факт") and \
            f"Pushed-by: retinue-platform (the owner's button, session {a})" in message
        assert "Факт, уточнён." in git(hub, "show", "main:notes/knowledge/a.md")
        assert world.files(a) == [] and world.notes("ping")[-1]["data"]["base"] is None

        # A file that moves after the card, between the platform's own look and the Base's lock: the Base refuses it.
        await world.owner("input", {"input_id": "tg:2", "session": a, "text": "ещё"})
        (tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Второй раз."))
        await world.end(a)
        await world.owner("press", {"button": world.notes("ping")[-1]["data"]["base"][1]})
        card = world.notes("card")[-1]
        commit = world.platform.base.commit

        async def moving(*args, **kwargs):
            (tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Третий раз."))
            return await commit(*args, **kwargs)

        world.platform.base.commit = moving
        await world.owner("press", {"button": dict((label, b) for label, b in card["data"]["buttons"])["Commit"]})
        await world.platform.tobase.idle()
        refused = world.platform.tobase.card(card["approval"][len(tobase.KEY):])
        assert refused["state"] == "failed" and "the file changed after the card" in refused["result"]["said"]
        assert "Второй раз." not in git(hub, "show", "main:notes/knowledge/a.md")
        assert world.files(a) == ["notes/knowledge/a.md"], "it stays on the list"

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
