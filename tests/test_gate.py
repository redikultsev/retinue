"""The gate's answer to every hook a lane CLI sends: marks let the CLI's rules decide, a never mark is a courier card
and a deny, an unruled call is deferred to the owner, and a grant answers `allow` once, for the call as he saw it."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from retinue import approvals, gate
from retinue.approvals import Mark
from retinue.config import LaneRef
from retinue.gate import Gate
from retinue.sessions import PlatformStore

TOKEN = "hook-token"
MARKS = [Mark("never", "Bash(git push:*)", "отправить коммиты (git push)"), Mark("allow", "Bash(npm test:*)")]


@pytest.fixture(autouse=True)
def short_wait(monkeypatch):
    monkeypatch.setattr(gate, "MARK_WAIT_S", 0.05)


@pytest.fixture
def work(tmp_path):
    (tmp_path / "work" / "repo").mkdir(parents=True)
    return tmp_path / "work"


@pytest.fixture
def setup(tmp_path, work):
    """One event loop for the test: a call's mark wait is bound to the loop that first saw the call."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield _setup(tmp_path, work)
    asyncio.set_event_loop(None)
    loop.close()


def _setup(tmp_path, work):
    store = PlatformStore(str(tmp_path / "p.sqlite"))
    the_gate = Gate(store, {"code": LaneRef("code", "http://lane", "http://platform/hook", str(work))}, "secret")
    session = store.new_session("retinue", "code", "code", 0.0)
    run = store.add_run(session, the_gate.token_hash(TOKEN), [dataclasses.asdict(m) for m in MARKS], 0.0)
    return store, the_gate, session, run


def call(command="make test", call_id="toolu_1", tool="Bash", tool_input=None) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_use_id": call_id, "cwd": "/work/repo",
            "tool_input": tool_input if tool_input is not None else {"command": command}}


def decision(answer: dict) -> str | None:
    return answer.get("hookSpecificOutput", {}).get("permissionDecision")


def hook(the_gate: Gate, event: str, body: dict, token: str = TOKEN) -> dict:
    return asyncio.get_event_loop().run_until_complete(the_gate.hook(event, token, body))


def test_an_unknown_token_is_denied(setup):
    _, the_gate, _, _ = setup
    assert decision(hook(the_gate, "PreToolUse", call(), token="forged")) == "deny"
    answer = hook(the_gate, "PermissionRequest", call(), token="forged")
    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_an_unruled_call_waits_for_marks_then_defers_to_the_owner(setup):
    store, the_gate, session, run = setup
    started = time.monotonic()
    assert decision(hook(the_gate, "PreToolUse", call())) == "defer"
    assert time.monotonic() - started >= gate.MARK_WAIT_S
    [approval] = store.approvals(session=session)
    assert (approval["state"], approval["run"], approval["tool_use_id"]) == ("pending", run, "toolu_1")
    assert approval["digest"] == approvals.digest("Bash", {"command": "make test"}, "/work/repo", "")


def test_the_mark_wait_ends_when_a_mark_arrives_and_the_clis_rule_decides(setup, monkeypatch):
    store, the_gate, _, _ = setup
    monkeypatch.setattr(gate, "MARK_WAIT_S", 5.0)

    async def go():
        waiting = asyncio.create_task(the_gate.hook("PreToolUse", TOKEN, call("npm test")))
        await asyncio.sleep(0.05)
        await the_gate.hook("mark/1", TOKEN, call("npm test"))
        return await asyncio.wait_for(waiting, 1.0)

    assert asyncio.get_event_loop().run_until_complete(go()) == {}
    assert store.approvals() == []


def test_a_never_mark_is_denied_and_handed_to_the_owner_once_as_a_courier_card(setup):
    store, the_gate, session, _ = setup
    push = call("git push origin main")
    hook(the_gate, "mark/0", push)
    answer = hook(the_gate, "PreToolUse", push)
    assert decision(answer) == "deny" and answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.NEVER
    hook(the_gate, "mark/0", push)
    assert decision(hook(the_gate, "PreToolUse", push)) == "deny"
    [courier] = store.approvals(session=session)
    assert (courier["kind"], courier["state"]) == ("courier", "dropped")
    [note] = store.look()
    assert note["kind"] == "card" and note["approval"] == courier["id"] and note["data"]["buttons"] == []
    assert "git push origin main" == note["data"]["view"]["body"]
    assert note["data"]["view"]["head"][1] == "Хочет: отправить коммиты (git push)"


def test_a_late_never_mark_turns_the_pending_approval_into_a_courier_card(setup):
    store, the_gate, session, _ = setup
    push = call("git push origin main")
    assert decision(hook(the_gate, "PreToolUse", push)) == "defer"
    hook(the_gate, "mark/0", push)
    states = {(a["kind"], a["state"]) for a in store.approvals(session=session)}
    assert states == {("approval", "dropped"), ("courier", "dropped")}
    assert [n["kind"] for n in store.look()] == ["card"]


def grant(store: PlatformStore, session: str) -> dict:
    [approval] = store.approvals(session=session, states=("pending",))
    assert store.move(approval["id"], ("pending",), "shown", 1.0)
    assert store.move(approval["id"], ("shown",), "granted", 2.0)
    return approval


def test_a_grant_answers_allow_once_and_the_call_is_asked_again_after_that(setup):
    store, the_gate, session, _ = setup
    hook(the_gate, "PreToolUse", call())
    approval = grant(store, session)
    assert decision(hook(the_gate, "PreToolUse", call())) == "allow"
    assert store.approval(approval["id"])["state"] == "executing"
    assert decision(hook(the_gate, "PreToolUse", call())) == "defer"
    assert hook(the_gate, "PostToolUse", {**call(), "hook_event_name": "PostToolUse"}) == {}
    assert store.approval(approval["id"])["state"] == "executed"


def test_a_grant_for_a_file_that_changed_since_is_not_honoured(setup, work):
    store, the_gate, session, _ = setup
    target = work / "repo" / "a.py"
    target.write_text("old\n")
    edit = call(tool="Write", tool_input={"file_path": "/work/repo/a.py", "content": "new\n"})
    hook(the_gate, "PreToolUse", edit)
    approval = grant(store, session)
    target.write_text("changed meanwhile\n")
    answer = hook(the_gate, "PreToolUse", edit)
    assert decision(answer) == "deny" and answer["hookSpecificOutput"]["permissionDecisionReason"] == gate.CHANGED
    assert store.approval(approval["id"])["state"] == "dropped"


@pytest.mark.parametrize("state, expected, reason", [("rejected", "deny", gate.REJECTED),
                                                      ("stale", "deny", gate.STALE),
                                                      ("shown", "defer", gate.DEFERRED)])
def test_an_approval_decides_the_call_it_was_made_for(setup, state, expected, reason):
    store, the_gate, session, _ = setup
    hook(the_gate, "PreToolUse", call())
    [approval] = store.approvals(session=session)
    store.move(approval["id"], ("pending",), state, 1.0)
    answer = hook(the_gate, "PreToolUse", call())
    assert (decision(answer), answer["hookSpecificOutput"]["permissionDecisionReason"]) == (expected, reason)
    assert len(store.approvals(session=session)) == 1


def test_a_call_the_cli_asked_about_goes_to_the_owner_when_it_is_made_again(setup):
    store, the_gate, session, _ = setup
    compound = call("npm test && rm -rf build")
    hook(the_gate, "mark/1", compound)
    assert hook(the_gate, "PreToolUse", compound) == {}, "the allow rule's marker fired: the CLI decides"
    asked = hook(the_gate, "PermissionRequest", {**compound, "hook_event_name": "PermissionRequest"})
    assert asked["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": gate.ASKED}
    again = call("npm test && rm -rf build", call_id="toolu_2")
    hook(the_gate, "mark/1", again)
    assert decision(hook(the_gate, "PreToolUse", again)) == "defer"
    assert [a["tool_use_id"] for a in store.approvals(session=session)] == ["toolu_2"]


def test_shutting_down_defers_the_waiting_call_and_every_one_after(setup, monkeypatch):
    store, the_gate, _, _ = setup
    monkeypatch.setattr(gate, "MARK_WAIT_S", 5.0)

    async def go():
        waiting = asyncio.create_task(the_gate.hook("PreToolUse", TOKEN, call()))
        await asyncio.sleep(0.05)
        the_gate.close()
        return await asyncio.wait_for(waiting, 1.0), await the_gate.hook("PreToolUse", TOKEN, call(call_id="toolu_2"))

    first, later = asyncio.get_event_loop().run_until_complete(go())
    assert decision(first) == decision(later) == "defer"
    assert store.approvals() == [], "nothing is put to the owner: the call is asked again after the restart"
