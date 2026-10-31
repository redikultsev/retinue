"""Sub-sessions (`retinue/subs.py`): a session the owner started starts sessions of its own through the platform's MCP
server, each call seen by the gate first; the limits, the reports and their delivery, the loop cap, the stop that
takes the family, and the sphere a session names by its last line — against the fake lanes of `test_platformd`."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from retinue import pings, subs
from retinue.config import Sphere
from test_platformd import World, decision, short_wait  # noqa: F401 — the fixture is autouse

SPHERES = [Sphere("travel", "Путешествия", "notes", "✈️"), Sphere("home", "Дом", "notes")]
CLI = "11111111-2222-4333-8444-555555555555"
DEFERRED = "deferred to the owner"


class SubsWorld(World):
    async def up(self) -> None:
        self.cfg.spheres = SPHERES
        self.cfg.lanes["private"] = dataclasses.replace(self.cfg.lanes["private"], max_sessions=4)
        await super().up()

    async def mcp(self, run: dict, body: dict) -> tuple[int, dict | None]:
        response = await self.client.post("/hook/mcp", json=body,
                                          headers={"Authorization": f"Bearer {run['hook_token']}"})
        return response.status, (await response.json() if response.status == 200 else None)

    def run_of(self, session: str) -> dict:
        return [r for lane in self.lanes.values() for r in lane.runs() if r["session"] == session][-1]

    async def tool(self, session: str, name: str, arguments: dict, call: str, *, pre: bool = True,
                   agent: str = "") -> tuple[str, bool]:
        """The CLI's way: PreToolUse to the gate, then — let through — `tools/call` to the server, by the call's id."""
        run = self.run_of(session)
        if pre:
            answer = await self.hook("PreToolUse", run, {
                "hook_event_name": "PreToolUse", "tool_name": subs.PREFIX + name, "tool_input": arguments,
                "tool_use_id": call, "cwd": "/work", "permission_mode": "auto", **({"agent_id": agent} if agent else {})})
            if decision(answer) == "deny":
                return answer["hookSpecificOutput"]["permissionDecisionReason"], True
            if decision(answer) == "defer":
                return DEFERRED, True
        status, data = await self.mcp(run, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": name, "arguments": arguments, "_meta": {"claudecode/toolUseId": call}}})
        assert status == 200, data
        return data["result"]["content"][0]["text"], data["result"]["isError"]

    async def start_child(self, parent: str, title: str, call: str, brief: str = "") -> str:
        said, error = await self.tool(parent, subs.START, {"title": title, "brief": brief or f"сделай {title}"}, call)
        assert not error, said
        return said.split()[1]

    async def turn(self, session: str, text: str, ok: bool = True) -> None:
        """The session's open run says `text` as its turn's end, and its process exits."""
        run = self.run_of(session)
        lane = self.lanes[self.store.session(session)["lane"]]
        lane.add(session, run["run"],
                 json.dumps({"type": "system", "subtype": "init", "session_id": CLI, "cwd": "/work",
                             "permissionMode": "auto", "mcp_servers": [{"name": "retinue", "status": "connected"}]}),
                 json.dumps({"type": "result", "subtype": "success" if ok else "error_during_execution",
                             "is_error": not ok, "stop_reason": "end_turn", "result": text, "session_id": CLI}))
        lane.exited(session, run["run"])
        await self.poll()

    async def parent(self, text: str = "спланируй поездку", sphere: str | None = "travel") -> str:
        body = {"input_id": f"tg:{text}", "track": sphere or "notes", "text": text}
        return (await self.owner("new", body))["session"]


def subs_scenario(tmp_path, body):
    async def go():
        world = SubsWorld(tmp_path)
        await world.start()
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


# --- the pieces -----------------------------------------------------------------------------------------------------

def test_the_sphere_line_is_taken_only_last_or_before_the_replies_and_only_for_a_configured_sphere():
    names = frozenset({"travel", "home"})
    assert subs.sphere_line("Готово.\nСфера: travel", names) == ("Готово.", "travel", "Сфера: travel")
    assert subs.sphere_line("Готово.\n**Сфера:** `home`\nОтветы: «Да»", names) == \
        ("Готово.\nОтветы: «Да»", "home", "**Сфера:** `home`")
    # The instructed English form, before an English replies line too.
    assert subs.sphere_line("Done.\nSphere: travel", names) == ("Done.", "travel", "Sphere: travel")
    assert subs.sphere_line('Done.\n**Sphere:** `home`\nReplies: "Yes"', names) == \
        ('Done.\nReplies: "Yes"', "home", "**Sphere:** `home`")
    for text in ("Готово.\nСфера: finance", "Сфера: travel\nГотово.", "```\nСфера: travel", "Сфера: Travel",
                 "Готово. Сфера: travel", "Сфера: travel home", "Done.\nSphere: finance", "Done.\nsphere: travel",
                 "```\nSphere: travel", "Done. Sphere: travel", "Sphere: travel\nDone."):
        assert subs.sphere_line(text, names) == (text, None, ""), text


def test_the_server_speaks_mcp_over_plain_json():
    async def go():
        async def call(name, arguments, params):
            return f"{name}:{arguments['x']}:{subs.call_id_of(params)}", False
        status, data = await subs.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-11-25"}}, "said", call)
        assert status == 200 and data["result"]["protocolVersion"] == "2025-11-25"
        assert data["result"]["instructions"] == "said" and data["result"]["capabilities"] == {"tools": {}}
        assert (await subs.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "9"}},
                               "", call))[1]["result"]["protocolVersion"] in subs.PROTOCOLS
        assert await subs.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, "", call) == (202, None)
        listed = (await subs.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "", call))[1]
        assert [t["name"] for t in listed["result"]["tools"]] == [subs.START, subs.MESSAGE, subs.READ]
        found = (await subs.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "t", "arguments": {"x": 1}, "_meta": {"claudecode/toolUseId": "toolu_1"}}}, "", call))[1]
        assert found["result"] == {"content": [{"type": "text", "text": "t:1:toolu_1"}], "isError": False}
        assert (await subs.rpc({"jsonrpc": "2.0", "id": 4, "method": "server/discover"}, "", call))[1]["error"][
            "code"] == -32601, "the new spec's discovery: unknown, so the CLI goes on with initialize (probed)"
        assert (await subs.rpc([1], "", call))[0] == 400
    asyncio.run(go())


# --- starting -------------------------------------------------------------------------------------------------------

def test_a_session_starts_a_sub_session_whose_brief_comes_from_the_session_not_the_owner(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        assert world.run_of(parent)["subs"] is True, "an owner's session gets the server on every run"
        child = await world.start_child(parent, "жильё в Зурбагане", "toolu_a", "найди жильё на 3–8 июня")
        row = world.store.session(child)
        assert row["sub_of"] == parent and row["sphere"] == "travel" and not row["main"] and row["title"] == \
            "жильё в Зурбагане"
        run = world.run_of(child)
        assert "subs" not in run, "depth 1: a sub-session has no server"
        assert run["prompt"].startswith(f'Assignment from session {parent} "спланируй поездку" — not from the owner.')
        assert run["prompt"].endswith("найди жильё на 3–8 июня")
        face = world.store.face_session(child)
        assert face["sub_of"] == parent and face["parent"] is None and face["side"] is False
        assert face["title"] == "жильё в Зурбагане" and world.store.face_session(parent)["children"] == 1
        [given] = world.store.face_inputs(child)
        assert given["by"] == "parent"
        assert world.store.db.execute("SELECT seen_at FROM sessions WHERE id = ?", (child,)).fetchone()[0] is None, \
            "a brief is not the owner looking"
        [started] = [n for n in world.notes("summary") if n["session"] == child]
        assert started["data"]["quiet"] is True and not pings.loud(started["data"]), "a quiet ping on the start"
        assert started["data"]["text"] == f'Started by session {parent} "спланируй поездку": жильё в Зурбагане'
        again = await world.tool(parent, subs.START, {"title": "жильё в Зурбагане", "brief": "найди жильё на 3–8 июня"},
                                 "toolu_b")
        assert again == (f'Sub-session {child} "жильё в Зурбагане" is already started.', False), \
            "a lost answer, made again"
        assert len([s for s in world.store.face_sessions(world.clock.now) if s["sub_of"] == parent]) == 1
    subs_scenario(tmp_path, body)


def test_every_call_passes_the_gate_first_and_the_server_runs_only_what_the_gate_saw(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        said, error = await world.tool(parent, subs.START, {"title": "t", "brief": "b"}, "toolu_x", pre=False)
        assert error and said == subs.UNSEEN, "no PreToolUse: nothing runs"
        run = world.run_of(parent)
        await world.hook("PreToolUse", run, {"hook_event_name": "PreToolUse", "tool_name": subs.PREFIX + subs.START,
                                             "tool_input": {"title": "t", "brief": "b"}, "tool_use_id": "toolu_y",
                                             "cwd": "/work", "permission_mode": "auto"})
        swapped = await world.tool(parent, subs.START, {"title": "t", "brief": "другое"}, "toolu_y", pre=False)
        assert swapped == (subs.UNSEEN, True), "other arguments than the gate saw"
        said, error = await world.tool(parent, subs.START, {"title": "t", "brief": "b"}, "toolu_z", agent="a1")
        assert error and said == subs.NO_DEPTH, "a subagent's call is denied at the gate"
        assert not any(s["sub_of"] for s in world.store.face_sessions(world.clock.now))
        status, _ = await world.mcp({"hook_token": "nobody"}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 401
        child = await world.start_child(parent, "t", "toolu_ok")
        said, error = await world.tool(child, subs.START, {"title": "x", "brief": "y"}, "toolu_c")
        assert error and said == subs.NO_DEPTH, "a sub-session cannot start one"
    subs_scenario(tmp_path, body)


def test_ten_open_no_day_cap_and_the_room_always_keeps_a_place_for_the_owner(tmp_path):
    """The room (max 4 here) runs sub-sessions and reports up to one place short: the last is the owner's own."""
    async def body(world: SubsWorld):
        parent = await world.parent()
        kids = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(subs.OPEN_MAX)]
        running = [k for k in kids if not world.store.face_session(k)["queued"]]
        assert len(running) == 2, "the parent and two sub-sessions: three of four places, the fourth is the owner's"
        said, error = await world.tool(parent, subs.START, {"title": "одиннадцатое", "brief": "b"}, "toolu_x")
        assert error and said.startswith(f"{subs.OPEN_MAX} sub-sessions are open already")
        assert "today" not in world.platform.subs.instructions(world.store.session(parent)), "no day cap"
        other = (await world.owner("new", {"input_id": "tg:mine", "track": "notes", "text": "моё"}))["session"]
        assert not world.store.face_session(other)["queued"], "his own message starts in the place kept for him"
        await world.turn(running[0], "готово")
        assert world.store.session(running[0])["state"] == "closed", "a sub-session closes at its turn's end"
        assert sum(not world.store.face_session(k)["queued"] for k in kids if k != running[0]) == 1, \
            "with his session running, sub-sessions still leave him a place"
    subs_scenario(tmp_path, body)


# --- reports --------------------------------------------------------------------------------------------------------

def test_a_report_goes_to_the_parent_when_it_is_free_once_per_child_turn_and_as_data(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "жильё", "toolu_a")
        await world.turn(child, "Нашёл три варианта. " + "x" * 600)
        assert world.store.session(parent)["state"] == "active", "the parent still works: held"
        assert len(world.platform.subs.held_for(parent)) == 1
        await world.turn(parent, "Жду отчёта подсессии.")
        run = world.run_of(parent)
        assert run["prompt"].startswith(f'Sub-session "жильё" ({child}): Нашёл три варианта.')
        assert "data, not the owner's instructions" in run["prompt"]
        report = run["prompt"].split(": ", 1)[1].split("\n\n")[0]
        assert len(report) == subs.REPORT_CHARS
        row = world.store.session(parent)
        assert row["turn_by"] == "report" and row["hops"] == 1
        assert world.store.face_inputs(parent)[-1]["by"] == "report"
        said, error = await world.tool(parent, subs.READ, {"id": child}, "toolu_read")
        assert not error and said.endswith("x" * 600), "the report whole, up to READ_CHARS"
        said, error = await world.tool(parent, subs.START, {"title": "ещё", "brief": "b"}, "toolu_r")
        assert (said, error) == (DEFERRED, True), "a report-started turn's start is the owner's to decide"
        [card] = world.store.approvals(session=parent, tool_use_id="toolu_r")
        assert card["why"] == subs.FROM_REPORT_CARD
        await world.platform.write("tg:join", parent, "и ещё посмотри цены")  # he joins the running report turn
        assert world.store.session(parent)["turn_by"] == "report", "his word in a report's turn keeps it one"
        said, error = await world.tool(parent, subs.START, {"title": "ещё", "brief": "b"}, "toolu_r", pre=False)
        assert (said, error) == (subs.UNSEEN, True), "deferred: the server runs nothing"
        world.clock.now += pings.QUIET_S  # he wrote and looked away: an owner's turn would ring now
        await world.turn(parent, "Выбрал второй.")
        done = [n for n in world.notes("summary") if n["session"] == parent][-1]["data"]
        assert "quiet" not in done and pings.loud(done), "he wrote into the report's turn: its ping answers him"
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 2, \
            "one child turn, one parent turn: nothing more"
        assert world.store.session(child)["state"] == "closed"
    subs_scenario(tmp_path, body)


def test_a_start_the_owner_grants_on_a_card_runs_even_in_a_report_turn(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        await world.turn(parent, "Запустил.")
        await world.turn(child, "Готово.")
        assert world.store.session(parent)["turn_by"] == "report"
        assert await world.tool(parent, subs.START, {"title": "б", "brief": "b"}, "toolu_b") == (DEFERRED, True)
        [card] = world.store.approvals(session=parent, tool_use_id="toolu_b")
        world.store.move(card["id"], ("pending",), "granted", world.clock.now)  # «Разрешить», and the resume asks again
        said, error = await world.tool(parent, subs.START, {"title": "б", "brief": "b"}, "toolu_b")
        assert not error and said.startswith("Sub-session "), said
    subs_scenario(tmp_path, body)


def test_reports_wait_for_their_siblings_and_for_a_limit_and_go_as_one_turn(tmp_path):
    """A sibling still working holds a report up to BATCH_WAIT_S (`test_a_lone_report_waits…` has the timeout)."""
    async def body(world: SubsWorld):
        parent = await world.parent()
        a = await world.start_child(parent, "а", "toolu_a")
        b = await world.start_child(parent, "б", "toolu_b")
        await world.turn(parent, "Запустил две.")
        await world.turn(a, "А готово.")
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 1, "б still works"
        world.store.set("subs.limit_until", str(world.clock.now + 60))
        await world.turn(b, "Б готово.")
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 1, "a limit is waited out"
        world.clock.now += 61
        world.platform.tick()
        await world.platform.drain()
        runs = [r for r in world.lanes["private"].runs() if r["session"] == parent]
        assert len(runs) == 2 and '"а"' in runs[-1]["prompt"] and '"б"' in runs[-1]["prompt"], "both in one turn"
        await world.turn(parent, "Учёл оба.")
        done = [n for n in world.notes("summary") if n["session"] == parent][-1]["data"]
        assert done["quiet"] is True and not pings.loud(done), "a parent's done turn on a report rings nothing"
    subs_scenario(tmp_path, body)


def test_a_lone_report_waits_for_its_working_sibling_at_most_batch_wait(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        a = await world.start_child(parent, "а", "toolu_a")
        await world.start_child(parent, "б", "toolu_b")
        await world.turn(parent, "Запустил две.")
        await world.turn(a, "А готово.")
        world.clock.now += subs.BATCH_WAIT_S - 1
        world.platform.tick()
        await world.platform.drain()
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 1
        world.clock.now += 1
        world.platform.tick()
        await world.platform.drain()
        assert '"а"' in world.run_of(parent)["prompt"]
    subs_scenario(tmp_path, body)


def test_a_report_never_goes_in_while_the_parent_waits_on_a_card_and_the_owner_stopping_a_child_is_his(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        await world.turn(parent, "Запустил.")
        world.platform.set_state(parent, "waiting", world.clock.now)  # its card asks the owner
        await world.turn(child, "Готово.")
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 1
        world.platform.set_state(parent, "idle", world.clock.now)
        await world.poll()
        assert len([r for r in world.lanes["private"].runs() if r["session"] == parent]) == 2
        await world.turn(parent, "Ок.")
        await world.platform.write_into("submsg:x", world.store.session(child), "ещё", by="parent")
        assert await world.platform.stop(child)
        await world.turn(child, "Прервано.", ok=False)
        assert world.store.db.execute("SELECT COUNT(*) FROM sub_reports WHERE child = ?", (child,)).fetchone()[0] == 1, \
            "what a child says after the owner stopped it is no report"
    subs_scenario(tmp_path, body)


def test_stopping_a_parent_stops_its_family(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        kids = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(3)]
        assert await world.platform.stop(parent)
        assert [world.store.session(k)["state"] for k in kids] == ["closed"] * 3
        assert not any(q["session"] in kids for q in world.store.queued()), "the queued one never starts"
        stopped = [s for p, s in world.lanes["private"].commands if p == "/stop"]
        assert {s["session"] for s in stopped} >= set(kids[:2])
        for kid in kids[:2]:
            await world.turn(kid, "Прервано.", ok=False)
        assert world.store.db.execute("SELECT COUNT(*) FROM sub_reports").fetchone()[0] == 0
    subs_scenario(tmp_path, body)


def test_after_many_exchanges_without_the_owner_reports_wait_quietly_for_his_next_message(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        await world.turn(parent, "Запустил.")
        world.store.db.execute("UPDATE sessions SET hops = ? WHERE id = ?", (subs.AUTO_MAX - 1, parent))
        world.store.db.commit()
        await world.turn(child, "Шаг 1.")  # the last report that wakes it
        said, error = await world.tool(parent, subs.MESSAGE, {"id": child, "text": "дальше"}, "toolu_m")
        assert not error, "a parent's message stays allowed past the count"
        await world.turn(parent, "Передал.")
        await world.turn(child, "Шаг 2.")
        assert world.store.session(parent)["state"] == "idle", "this report waits"
        line = [n for n in world.notes("summary") if n["session"] == parent][-1]["data"]
        assert f"{subs.AUTO_MAX} times in a row without you" in line["text"] and line["kind"] == pings.DONE
        assert line["quiet"] is True and not pings.loud(line), "one quiet line, no sound"
        await world.poll()
        assert len([n for n in world.notes("summary") if "times in a row without you" in n["data"]["text"]]) == 1
        await world.platform.write("tg:go", parent, "продолжай")
        await world.turn(parent, "Продолжаю.")
        assert world.store.session(parent)["hops"] == 1, "his word: counted again, the waiting report went in"
        assert "Шаг 2." in world.run_of(parent)["prompt"]
    subs_scenario(tmp_path, body)


def test_the_parent_does_not_write_into_a_child_the_owner_is_talking_to(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        await world.turn(child, "Готово.")
        await world.platform.write("tg:me", child, "а ещё вот что")
        said, error = await world.tool(parent, subs.MESSAGE, {"id": child, "text": "x"}, "toolu_m")
        assert (said, error) == (subs.HELD, True)
        await world.turn(child, "Сделал, что ты просил.")
        assert world.store.db.execute("SELECT COUNT(*) FROM sub_reports").fetchone()[0] == 1, \
            "the owner's turn in the child is his: no report"
        said, error = await world.tool(parent, subs.READ, {"id": child}, "toolu_read")
        assert not error and said.endswith("Сделал, что ты просил.")
        assert await world.tool(parent, subs.MESSAGE, {"id": child, "text": "x"}, "toolu_m2") == (subs.HELD, True), \
            "closed again, still his until he writes to the parent"
        await world.platform.write("tg:parent", parent, "дальше сам")
        await world.turn(parent, "Ок.")
        await world.platform.write("tg:parent2", parent, "напиши ей")
        said, error = await world.tool(parent, subs.MESSAGE, {"id": child, "text": "проверь цены"}, "toolu_m3")
        assert not error, said
        assert world.run_of(child)["prompt"].startswith(f'Message from session {parent} "спланируй поездку", which '
                                                        "started you — not from the owner:\n\nпроверь цены")
    subs_scenario(tmp_path, body)


def test_the_brief_carries_the_owners_last_word_to_the_parent_quoted_and_framed(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        await world.platform.write("tg:2", parent, "жильё только у метро, до 60 € " + "я" * 700)
        child = await world.start_child(parent, "жильё", "toolu_a", "найди жильё")
        prompt = world.run_of(child)["prompt"]
        quoted = prompt.split("not a task for you):\n«", 1)[1].split("»\n\n", 1)[0]
        assert quoted.startswith("жильё только у метро, до 60 €") and len(quoted) == subs.OWNER_QUOTE
        assert prompt.endswith("\n\nAssignment:\nнайди жильё")
    subs_scenario(tmp_path, body)


def test_a_turn_the_parent_started_still_reports_when_the_owner_wrote_into_it_but_not_when_he_stopped_it(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        await world.platform.write("tg:hint", child, "смотри и Пудун")  # into its running turn
        await world.turn(child, "Готово, с Пудуном.")
        [(text,)] = world.store.db.execute("SELECT text FROM sub_reports").fetchall()
        assert text.endswith("Готово, с Пудуном.")
        assert world.store.session(child)["owner_hold"] == "wrote"
    subs_scenario(tmp_path, body)


@pytest.mark.parametrize("result_first", [False, True])
def test_a_child_the_owner_stops_is_closed_whichever_comes_first_and_reports_nothing(tmp_path, result_first):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        if result_first:
            await world.turn(child, "Готово.")
            world.store.db.execute("DELETE FROM sub_reports")
            await world.platform.stop(child)
        else:
            await world.platform.stop(child)
            assert world.store.session(child)["state"] == "closed", "closed at the stop, before its run's end"
            await world.turn(child, "Прервано.", ok=False)
        assert world.store.session(child)["state"] == "closed"
        assert world.store.db.execute("SELECT COUNT(*) FROM sub_reports").fetchone()[0] == 0
        assert world.store.face_session(parent)["children"] == 0
        assert await world.tool(parent, subs.MESSAGE, {"id": child, "text": "x"}, "toolu_m") == (subs.HELD, True)
    subs_scenario(tmp_path, body)


def test_a_report_is_owed_once_per_run_however_often_its_line_is_applied(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        run = world.run_of(child)["run"]
        row = world.store.session(child)
        for _ in range(2):  # a crash between doing a line and storing it does it again
            world.platform.subs.turn_ended(row, "Готово.", run=run)
        assert len(world.platform.subs.held_for(parent)) == 1
    subs_scenario(tmp_path, body)


def test_no_start_while_a_limit_is_waited_out(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        world.store.set("subs.limit_until", str(world.clock.now + 60))
        said, error = await world.tool(parent, subs.START, {"title": "б", "brief": "b"}, "toolu_l")
        assert (said, error) == (subs.LIMIT, True)
        world.clock.now += 61
        said, error = await world.tool(parent, subs.START, {"title": "б", "brief": "b"}, "toolu_l2")
        assert not error, said
    subs_scenario(tmp_path, body)


def test_the_token_of_a_run_that_ended_opens_neither_a_hook_nor_the_server(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        run = world.run_of(parent)
        assert world.platform.gate.passed(run["run"], "toolu_a", subs.PREFIX + subs.START,
                                          {"title": "а", "brief": "сделай а"}) == "passed"
        await world.turn(parent, "Запустил.")
        assert world.platform.gate.passed(run["run"], "toolu_a", subs.PREFIX + subs.START,
                                          {"title": "а", "brief": "сделай а"}) is None, "forgotten with its run"
        status, _ = await world.mcp(run, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 401
        assert decision(await world.hook("PreToolUse", run, {"hook_event_name": "PreToolUse", "tool_name": "Read",
                                                             "tool_input": {"file_path": "/work/x"},
                                                             "tool_use_id": "t", "cwd": "/work"})) == "deny"
        assert child
    subs_scenario(tmp_path, body)


# --- the sphere a session names -----------------------------------------------------------------------------------

@pytest.mark.parametrize("before, line, after", [
    (None, "Сфера: travel", "travel"),
    (None, "Сфера: finance", None),
    ("home", "Сфера: travel", "home"),
    (None, "Sphere: travel", "travel"),
    (None, "Sphere: finance", None),
])
def test_a_session_nobody_placed_names_its_own_sphere_and_its_children_follow(tmp_path, before, line, after):
    async def body(world: SubsWorld):
        parent = await world.parent("найди жильё", sphere=before)
        child = await world.start_child(parent, "жильё", "toolu_a")
        await world.turn(parent, f"Нашёл.\n{line}\nОтветы: «Бронируй»")
        row = world.store.session(parent)
        assert row["sphere"] == after and not row["main"]
        assert world.store.session(child)["sphere"] == after
        ping = [n for n in world.notes("summary") if n["session"] == parent][-1]["data"]
        if after == "travel":
            assert ping["text"] == "Нашёл." and ping["line"] == f"{line}\nОтветы: «Бронируй»"
            assert row["sphere_by"] == "session"
        if after is None and before is None:
            world.store.set_sphere(child, "home")  # the owner places the child himself
            world.store.set_sphere(parent, "travel", by="session")
            assert world.store.session(child)["sphere"] == "home", "the cascade leaves his placing alone"
        world.store.set_sphere(parent, None)
        await world.platform.write("tg:again", parent, "ещё")
        await world.turn(parent, "Ещё.\nСфера: home")
        assert world.store.session(parent)["sphere"] is None, "the owner placed it: the line is ignored"
    subs_scenario(tmp_path, body)


def test_a_queued_child_the_owner_stops_never_runs_and_a_late_line_of_a_stopped_run_is_no_report(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        kids = [await world.start_child(parent, f"дело {n}", f"toolu_{n}") for n in range(3)]
        assert world.store.face_session(kids[2])["queued"]
        await world.platform.stop(kids[2])
        await world.platform.stop(kids[0])
        assert not any(q["session"] == kids[2] for q in world.store.queued()), "its own queue entry went"
        await world.platform.write("tg:go", parent, "продолжай")  # his word to the parent lifts the hold
        await world.turn(kids[0], "Поздний итог.", ok=False)  # the stopped run's end comes after
        await world.turn(kids[1], "Готово.")
        assert not any(kids[2] == r["session"] for r in world.lanes["private"].runs())
        reported = [c for (c,) in world.store.db.execute("SELECT child FROM sub_reports").fetchall()]
        assert reported == [kids[1]], "the stopped run says nothing to the parent, whoever wrote since"
        world.store.enqueue(kids[2], "x", world.clock.now)  # an entry left behind somehow: the drain drops it
        await world.platform.drain()
        assert not any(kids[2] == r["session"] for r in world.lanes["private"].runs())
        assert not world.store.queued() or all(q["session"] != kids[2] for q in world.store.queued())
    subs_scenario(tmp_path, body)


def test_a_refusal_or_an_expiry_without_a_run_is_reported_once_however_often_it_is_applied(tmp_path):
    async def body(world: SubsWorld):
        parent = await world.parent()
        child = await world.start_child(parent, "а", "toolu_a")
        row = world.store.session(child)
        for _ in range(2):
            world.platform.subs.turn_ended(row, "Линия не запустила сессию.", failed=True)
            world.platform.subs.turn_ended(row, "Карточка истекла.", failed=True, run="expired:abc")
        assert len(world.platform.subs.held_for(parent)) == 2
    subs_scenario(tmp_path, body)
