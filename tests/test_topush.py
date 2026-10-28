"""«Запушить»: a session in one of the owner's repositories (`/new code <repo>`, or a call that names its path), the
ping's button once its turn ends with commits the remote has nowhere, the card in Telegram and in the app (the remote
branch and the PR's title by a reply, the diff, one push however often pressed, a branch moved since the card,
workflows refused, a restart mid-push), the pull request through a stand-in GitHub API, the feed's row, the courier
card in the room — against real git: the remote served over smart HTTP with the token (`test_code.GitServer`)."""

from __future__ import annotations

import asyncio
import json
import shutil

from aiohttp import web
from aiohttp.test_utils import TestServer

from retinue import approvals, feed, gate, topush
from retinue.bridge import ping_keyboard
from retinue.config import CodeConfig, CodeRepo, Track
from retinue.lane_host import ensure_clone
from test_code import AUTHOR, TOKEN, Remote, committed, git
from test_face import FaceWorld
from test_platformd import decision, pre_tool_use, short_wait  # noqa: F401 — the fixture is autouse
from test_tobase import result


class FakeGitHub:
    """`POST/GET /repos/o/r/pulls`: the first POST opens PR 7, a later one says it exists."""

    def __init__(self) -> None:
        self.seen: list[tuple] = []

    async def start(self) -> str:
        async def pulls(request: web.Request) -> web.Response:
            body = await request.json() if request.method == "POST" else None
            self.seen.append((request.method, request.headers.get("Authorization"), body, dict(request.query)))
            if request.method == "POST" and sum(1 for s in self.seen if s[0] == "POST") == 1:
                return web.json_response({"html_url": "https://github.com/o/r/pull/7"}, status=201)
            if request.method == "POST":
                return web.json_response({"errors": [{"message": "A pull request already exists"}]}, status=422)
            return web.json_response([{"html_url": "https://github.com/o/r/pull/7"}])
        app = web.Application()
        app.add_routes([web.post("/repos/o/r/pulls", pulls), web.get("/repos/o/r/pulls", pulls)])
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")


class CodeWorld(FaceWorld):
    """The platform with the room's lane `private`, a track `code` there and the repository `r` (its remote over
    HTTP behind the token, its PRs on a stand-in GitHub)."""

    fetched = True

    async def start(self, **start) -> None:
        self.remote = Remote(self.tmp)
        self.github = FakeGitHub()
        self.api_url = await self.github.start()
        await super().start(**start)

    async def up(self) -> None:
        self.cfg.tracks = [t for t in self.cfg.tracks if t.name != "code"] + [Track("code", "private", "private", "")]
        self.cfg.code = CodeConfig(
            repos=[CodeRepo("r", self.remote.url, "owner", secret=TOKEN, api=self.api_url)], lane="private",
            root="code", track="code", author=AUTHOR, mirrors=str(self.tmp / "mirrors"),
            outbox=str(self.tmp / "platform" / "code"))
        await super().up()
        self.platform.code.repos["r"].url = self.remote.url  # GitHub's path for the API: o/r
        self.platform.code.repos["r"].api = self.api_url
        if self.fetched:  # what the platform's loop does at its start: the mirror made
            await asyncio.to_thread(self.platform.code.fetch, "r")

    async def stop(self) -> None:
        await self.platform.topush.idle()
        await self.github.server.close()
        self.remote.server.close()
        await super().stop()

    @property
    def clone(self):
        return self.tmp / "work" / "code" / "r"

    async def new(self, text: str = "r почини вход", input_id: str = "tg:1") -> str:
        said = await self.owner("new", {"input_id": input_id, "track": "code", "text": text})
        assert said["ok"], said
        run = self.lanes["private"].runs()[-1]
        # What the lane host does with the run's `code`: the mirror cloned into the room once.
        assert run["code"] == {"root": "code", "author": AUTHOR, "repos": ["r"]}
        ensure_clone(self.tmp / "work", self.tmp / "mirrors", "code", "r", AUTHOR)
        return said["session"]

    async def end(self, session: str, text: str = "Готово.", *lines: str) -> None:
        run = [r for r in self.lanes["private"].runs() if r["session"] == session][-1]
        self.lanes["private"].add(session, run["run"], *lines, result(text))
        self.lanes["private"].exited(session, run["run"])
        await self.poll()
        await self.platform.topush.idle()  # the recounts run in the background: the poll never waits on git

    def ping(self) -> dict:
        return self.notes("ping")[-1]["data"]


def coded(tmp_path, body):
    async def go():
        world = CodeWorld(tmp_path)
        await world.start()
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


async def opened(world: CodeWorld, *, message: str = "Fix login") -> tuple[str, dict, dict]:
    session = await world.new()
    git("checkout", "-qb", "fix-login", cwd=world.clone)
    committed(world.clone, "src/login.py", "ok\n", message)
    await world.end(session)
    await world.owner("press", {"button": world.ping()["push"][0][1]})
    note = world.notes("card")[-1]
    return session, note, world.platform.topush.card(note["approval"][len(topush.KEY):])


def test_a_session_in_a_repository_gets_the_button_the_card_and_one_push_with_its_pr(tmp_path):
    async def body(world: CodeWorld):
        session, note, card = await opened(world)
        assert world.lanes["private"].runs()[0]["cwd"] == "code/r"
        assert world.store.face_session(session)["push"] == 1
        assert [row[0]["text"] for row in ping_keyboard({**world.ping(), "open": False})["inline_keyboard"]] == \
            ["Запушить · 1"]
        assert note["approval"] == f"{topush.KEY}{card['id']}"
        view = note["data"]["view"]
        assert view["head"] == [f"Запушить · r · code · сессия {session}",
                                f"Ветка: fix-login → retinue/{session}-fix-login (PR в main)", "Заголовок PR: Fix login"]
        body = view["body"].splitlines()
        assert body[0] == f"• {card['look']['sha'][:7]} Fix login" and body[2] == "  A src/login.py"
        assert body[1].startswith("  автор: Владелец <o@example.com>, ") and "коммитил" not in body[1]
        assert view["status"].startswith("Уходит: 1 коммит, ") and "В PR: 1 коммит, 1 файл." in view["status"]
        assert [label for label, _ in note["data"]["buttons"]] == ["Отправить", "Открыть дифф", "Отмена"]
        buttons = dict(note["data"]["buttons"])

        # A reply: one word is the branch, a phrase the PR's title; neither goes into the session.
        assert await world.owner("input", {"input_id": "tg:20", "session": session, "text": "fix/login-flow",
                                           "card": note["approval"]}) == {"say": "Ветка: fix/login-flow"}
        assert await world.owner("input", {"input_id": "tg:21", "session": session, "text": "(голосом, дословно) Чиню вход",
                                           "card": note["approval"]}) == {"say": "Заголовок PR: «Чиню вход»"}
        refused = await world.owner("input", {"input_id": "tg:22", "session": session, "text": "main",
                                              "card": note["approval"]})
        assert refused["say"].startswith("Ветка не подходит")
        assert world.store.input_session("tg:20") is None and world.store.input_session("tg:21") is None

        diff = await world.owner("press", {"button": buttons["Открыть дифф"]})
        assert diff["document"]["name"] == f"push-{card['id']}.diff" and "+ok" in diff["document"]["text"]

        first = await world.owner("press", {"button": buttons["Отправить"]})
        assert first["toast"] == "Отправляю…" and first["keep"] == []
        assert (await world.owner("press", {"button": buttons["Отправить"]}))["toast"] == "Отправляю — подожди."
        await world.platform.topush.idle()
        done = world.platform.topush.card(card["id"])
        assert done["state"] == "pushed" and done["result"]["url"] == "https://github.com/o/r/pull/7"
        assert world.remote.sha("fix/login-flow") == done["look"]["sha"] and world.remote.sha("main") != done["look"]["sha"]
        [(method, auth, sent, _)] = world.github.seen
        assert (method, auth) == ("POST", f"Bearer {TOKEN}")
        assert sent["head"] == "fix/login-flow" and sent["base"] == "main" and sent["title"] == "Чиню вход"
        assert f"Retinue: сессия {session}, карточка {card['id']}" in sent["body"] and "- `Fix login` (" in sent["body"]
        last = [n for n in world.notes("update") if n["approval"] == note["approval"]][-1]
        assert "PR: https://github.com/o/r/pull/7" in last["data"]["view"]["status"] and last["data"]["buttons"] == []
        answer = world.notes("card")[-1]
        assert answer["approval"] == f"{note['approval']}/done" and answer["data"]["buttons"] == []
        assert answer["data"]["view"]["body"] == "PR: https://github.com/o/r/pull/7"
        assert (await world.owner("press", {"button": buttons["Отправить"]}))["toast"] == \
            "Уже отправлено: https://github.com/o/r/pull/7"
        # The mirror learned the branch: nothing unpushed, the ping's button gone.
        assert world.store.face_session(session)["push"] == 0 and not world.ping().get("push")
        assert world.remote.sha("retinue/" + session + "-fix-login") is None
    coded(tmp_path, body)


def test_a_branch_moved_since_the_card_is_shown_again_and_workflows_are_refused_before_any_push(tmp_path):
    async def body(world: CodeWorld):
        session, note, card = await opened(world)
        committed(world.clone, ".github/workflows/ci.yml", "on: push\n", "CI")
        buttons = dict(note["data"]["buttons"])
        moved = await world.owner("press", {"button": buttons["Отправить"]})
        assert moved["toast"] == "Ветка изменилась после карточки — проверь и нажми ещё раз."
        fresh = world.platform.topush.card(card["id"])
        assert fresh["state"] == "shown" and fresh["look"]["ahead"] == 2
        assert fresh["look"]["workflows"] == [".github/workflows/ci.yml"]
        assert topush.WORKFLOWS in [n for n in world.notes("update") if n["approval"] == note["approval"]][-1][
            "data"]["view"]["status"]
        refused = await world.owner("press", {"button": buttons["Отправить"]})
        assert refused == {"toast": topush.WORKFLOWS}
        assert world.remote.sha(fresh["target"]) is None and world.github.seen == []
    coded(tmp_path, body)


def test_uncommitted_work_is_said_and_the_platform_never_commits_it(tmp_path):
    async def body(world: CodeWorld):
        session = await world.new()
        (world.clone / "README.md").write_text("edited, not committed\n")
        await world.end(session)
        assert world.store.face_session(session)["push"] == 0 and not world.notes("ping")
        said = {"toast": (await world.platform.topush.open(session, "r", telegram=True))[0]}
        assert "нет коммитов сверх main" in said["toast"] and "попроси сессию закоммитить" in said["toast"]
        git("checkout", "-qb", "docs", cwd=world.clone)
        git("commit", "-qam", "Docs", cwd=world.clone)
        (world.clone / "notes.txt").write_text("new, untracked")
        toast, card = await world.platform.topush.open(session, "r", telegram=False)
        assert card["dirty"] == ["notes.txt"] and "незакоммиченные" in card["view"]["status"]
        assert git("status", "--porcelain", cwd=world.clone).strip() == "?? notes.txt", "the platform wrote nothing"
    coded(tmp_path, body)


def test_a_restart_mid_push_settles_from_the_remote_and_never_pushes_without_a_press(tmp_path):
    async def body(world: CodeWorld):
        session, note, card = await opened(world)
        db = world.store.db
        db.execute("UPDATE push_cards SET state = 'pushing' WHERE id = ?", (card["id"],))
        db.commit()
        await world.down()
        await world.up()
        db = world.store.db
        await world.platform.topush.recover()
        back = world.platform.topush.card(card["id"])
        assert back["state"] == "shown" and back["result"]["said"] == topush.RESTARTED
        assert world.remote.sha(card["target"]) is None and world.github.seen == []

        # The push had landed before the restart: settled as pushed, the PR opened once.
        git("push", "-q", str(world.remote.bare), f"{card['look']['sha']}:refs/heads/{card['target']}",
            cwd=world.clone)
        db.execute("UPDATE push_cards SET state = 'pushing' WHERE id = ?", (card["id"],))
        db.commit()
        await world.platform.topush.recover()
        done = world.platform.topush.card(card["id"])
        assert done["state"] == "pushed" and done["result"]["url"].endswith("/pull/7")
        assert len(world.github.seen) == 1
    coded(tmp_path, body)


def test_a_first_run_waits_for_the_first_mirror_and_goes_once_it_is_made(tmp_path):
    async def body(world: CodeWorld):
        world.fetched = False
        await world.down()
        shutil.rmtree(world.tmp / "mirrors")
        await world.up()
        said = await world.owner("new", {"input_id": "tg:1", "track": "code", "text": "r почини вход"})
        assert said["ok"] and world.lanes["private"].runs() == [], "queued while the mirror is fetched"
        await asyncio.gather(*world.platform.tasks)
        [run] = world.lanes["private"].runs()
        assert run["cwd"] == "code/r" and run["code"]["repos"] == ["r"] and run["prompt"] == "почини вход"
    coded(tmp_path, body)


def test_a_session_elsewhere_that_names_the_repository_gets_its_button_and_new_code_needs_a_repository(tmp_path):
    async def body(world: CodeWorld):
        said = await world.owner("new", {"input_id": "tg:9", "track": "code", "text": "почини вход"})
        assert not said["ok"] and "Есть: r" in said["say"]
        notes = (await world.owner("new", {"input_id": "tg:10", "track": "notes", "text": "заметка"}))["session"]
        ensure_clone(world.tmp / "work", world.tmp / "mirrors", "code", "r", AUTHOR)
        git("checkout", "-qb", "from-notes", cwd=world.clone)
        committed(world.clone, "x.txt", "x\n", "X")
        call = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "cd /work/code/r && git commit -am X"}}]}})
        await world.end(notes, "Готово.", call)
        assert world.ping()["push"][0][0] == "Запушить · 1"
        rows = (await world.owner("feed", {}))["rows"]
        [row] = [r for r in rows if r["source"] == "push"]
        assert row["session"] == notes and row["essence"] == "Запушить: r · 1" and row["hint"] == feed.PUSH_HINT
    coded(tmp_path, body)


def test_the_app_opens_the_same_card_and_sends_it(tmp_path):
    async def body(world: CodeWorld):
        session = await world.new()
        git("checkout", "-qb", "app", cwd=world.clone)
        committed(world.clone, "a.txt", "a\n", "From the app")
        await world.end(session)
        await world.login()
        status, opened_, _ = await world.call("POST", f"/api/sessions/{session}/push", {})
        assert status == 200 and opened_["card"]["repo"] == "r" and opened_["card"]["commits"][0]["subject"] == \
            "From the app"
        card_id = opened_["card"]["id"]
        status, titled, _ = await world.call("POST", f"/api/push/{card_id}/title", {"title": "Из приложения"})
        assert titled["card"]["title"] == "Из приложения"
        status, diff, _ = await world.call("GET", f"/api/push/{card_id}/diff")
        assert "+a" in diff["diff"]
        status, sent, _ = await world.call("POST", f"/api/push/{card_id}/send", {})
        assert sent["toast"] == "Отправляю…"
        await world.platform.topush.idle()
        status, got, _ = await world.call("GET", f"/api/push/{card_id}")
        assert got["card"]["state"] == "pushed" and got["card"]["url"].endswith("/pull/7")
        snapshot = await world.get(f"/api/sessions/{session}")
        assert snapshot["push_cards"][-1]["id"] == card_id
    coded(tmp_path, body)


def test_in_the_room_a_push_is_a_courier_card_that_points_to_the_button(tmp_path):
    async def body(world: CodeWorld):
        session = await world.new()
        run = world.lanes["private"].runs()[-1]
        push = pre_tool_use({"id": "toolu_push", "name": "Bash", "input": {"command": "git push origin HEAD"}},
                            cwd="/work/code/r")
        marked, answer = await asyncio.gather(world.hook("mark/0", run, push), world.hook("PreToolUse", run, push))
        assert decision(answer) == "deny"
        assert answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.NEVER_PUSH
        [card] = world.notes("card")
        assert "«Запушить»" in card["data"]["view"]["status"]
        # On a lane without the button the old words stay.
        assert approvals.courier_card({"tool": "Bash", "input": {"command": "git push"}, "cwd": "/w"}, session, "t",
                                      approvals.NEVER[0][1]).status.startswith("Не выполняю: это делаешь ты сам")
    coded(tmp_path, body)


def test_the_bridge_puts_the_push_buttons_after_the_base_one_and_links_a_card_to_its_page(tmp_path):
    from retinue.bridge import CARD
    from test_bridge import make

    data = {"head": "Итог", "text": "Готово.", "kind": "decision", "open": True, "place": "code", "title": "t",
            "buttons": [["Да", "p.yes"]], "base": ["В Базу · 1", "p.base"],
            "push": [["Запушить retinue · 2", "p.push1"], ["Запушить site · 1", "p.push2"]]}
    ch, bridge, _ = make(tmp_path, [
        {"seq": 1, "kind": "summary", "session": "ab12cd34", "data": data},
        {"seq": 2, "kind": "card", "session": "ab12cd34", "approval": "push/f00dbabe",
         "data": {"view": {"head": ["Запушить · retinue · code · сессия ab12cd34"], "body": "• abc Fix",
                           "status": "", "link": []}, "buttons": [["Отправить", "p.send"]]}}])
    bridge.face_url = "https://app.example"
    asyncio.run(bridge.deliver())
    (_, first, _), (_, card, _) = ch.sent
    assert [row[0]["text"] for row in first["reply_markup"]["inline_keyboard"]] == [
        "Да", "В Базу · 1", "Запушить retinue · 2", "Запушить site · 1"]
    assert card["reply_markup"]["inline_keyboard"][-1] == [{"text": "Открыть в приложении",
                                                            "web_app": {"url": "https://app.example/p/f00dbabe"}}]
    assert ch.store.sent_event(CARD, "901") == "push/f00dbabe"


def test_a_branch_the_remote_already_has_is_refused_unless_this_session_pushed_it(tmp_path):
    async def body(world: CodeWorld):
        session, note, card = await opened(world)
        git(f"--git-dir={world.remote.bare}", "update-ref", "refs/heads/someone-else", world.remote.sha("main"))
        await world.owner("input", {"input_id": "tg:30", "session": session, "text": "someone-else",
                                    "card": note["approval"]})
        refused = await world.owner("press", {"button": dict(note["data"]["buttons"])["Отправить"]})
        assert refused["toast"].startswith("Ветка someone-else на удалённом уже есть")
        assert world.remote.sha("someone-else") == world.remote.sha("main")
        # Its own earlier branch is fine: the second push of the same room branch goes there, a fast-forward.
        await world.owner("input", {"input_id": "tg:31", "session": session, "text": "fix/mine",
                                    "card": note["approval"]})
        await world.owner("press", {"button": dict(note["data"]["buttons"])["Отправить"]})
        await world.platform.topush.idle()
        sha2 = committed(world.clone, "src/more.py", "more\n", "More")
        toast, again = await world.platform.topush.open(session, "r", telegram=False)
        assert again["target"] == "fix/mine" and [c["subject"] for c in again["commits"]] == ["More"]
        assert (await world.platform.topush.send(world.platform.topush.card(again["id"])))[0] == "Отправляю…"
        await world.platform.topush.idle()
        assert world.remote.sha("fix/mine") == sha2
    coded(tmp_path, body)


def test_a_remote_that_changed_since_the_card_sends_the_card_back(tmp_path):
    async def body(world: CodeWorld):
        session, note, card = await opened(world)
        committed(world.clone, "src/two.py", "2\n", "Two")
        toast, fresh = await world.platform.topush.open(session, "r", telegram=False)
        assert len(fresh["commits"]) == 2
        # Someone pushed the first commit to the remote meanwhile (from the Mac): less would leave than the card says.
        first = fresh["commits"][-1]["sha"]
        git("push", "-q", str(world.remote.bare), f"{first}:refs/heads/from-mac", cwd=world.clone)
        said = await world.platform.topush.send(world.platform.topush.card(fresh["id"]))
        assert said[0].startswith("Удалённый репозиторий изменился после карточки")
        again = world.platform.topush.card(fresh["id"])
        assert again["state"] == "shown" and [c["subject"] for c in again["look"]["commits"]] == ["Two"]
        assert world.remote.sha(again["target"]) is None
    coded(tmp_path, body)


def test_a_count_the_room_refuses_says_why_on_the_ping_and_in_the_app(tmp_path):
    async def body(world: CodeWorld):
        session = await world.new()
        git("checkout", "-qb", "x", cwd=world.clone)
        committed(world.clone, "x.txt", "x\n", "X")
        git("checkout", "-q", "--detach", cwd=world.clone)
        await world.end(session)
        [(label, button)] = world.ping()["push"]
        assert label == "⚠ Запушить: почему нет"
        row = world.store.face_session(session)
        assert row["push"] == 0 and row["push_why"].startswith("r: сессия не на ветке (detached HEAD)")
        said = await world.owner("press", {"button": button})
        assert said["toast"].startswith("r: сессия не на ветке")
        # Back on a branch: the reason goes, the count comes.
        git("checkout", "-q", "x", cwd=world.clone)
        await world.owner("input", {"input_id": "tg:40", "session": session, "text": "ещё"})
        await world.end(session)
        assert world.ping()["push"][0][0] == "Запушить · 1" and "push_why" not in world.store.face_session(session)
    coded(tmp_path, body)
