"""AskUserQuestion end to end on the platform of `test_platformd` (fake lanes, hooks and the owner's calls over HTTP)
and its face: the gate defers the question, each question comes as a Telegram card with its options as buttons, a
multi-select ticks and «Done», a reply to the card is the owner's own answer, the face answers by the same rules,
and the resume hands the CLI `updatedInput.answers` once — after a restart too. The shapes are the pinned CLI's
(`tests/test_probes.py::test_a_question_is_deferred_and_answered_on_resume`)."""

from __future__ import annotations

import json

from retinue import gate, platformd, questions
from test_face import FaceWorld, faced
from test_platformd import DAY, DEFERRED, HOUR, World, decision, scenario, short_wait  # noqa: F401 — autouse

ASK = {"questions": [
    {"question": "Где жить?", "header": "Жильё", "multiSelect": False,
     "options": [{"label": "Центр", "description": "старый город"}, {"label": "Окраина", "description": "тихо"}]},
    {"question": "Что взять?", "header": "Вещи", "multiSelect": True,
     "options": [{"label": "Зонт", "description": ""}, {"label": "Ноутбук", "description": ""}]}]}
CALL = {"id": "toolu_question_1", "name": "AskUserQuestion", "input": ASK}
LINES = [DEFERRED[0],
         json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": CALL["id"], "name": CALL["name"], "input": ASK}]}}, ensure_ascii=False),
         json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "tool_deferred",
                     "result": "", "session_id": json.loads(DEFERRED[0])["session_id"],
                     "deferred_tool_use": CALL}, ensure_ascii=False)]


def pre_tool_use(call: dict = CALL) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": call["name"], "tool_input": call["input"],
            "tool_use_id": call["id"], "cwd": "/work", "permission_mode": "auto"}


async def asked(world: World) -> tuple[str, dict, list[dict]]:
    """A private session whose first run asked ASK, and the two cards the owner got for it."""
    session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "куда поехать"}))["session"]
    [run] = world.lanes["private"].runs()
    assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer"
    world.lanes["private"].add(session, run["run"], *LINES)
    world.lanes["private"].exited(session, run["run"])
    await world.poll()
    return session, run, world.notes("card")


def labels(card: dict) -> list[str]:
    return [words for words, _ in card["data"]["buttons"]]


def button(card: dict, words: str) -> str:
    return next(b for w, b in card["data"]["buttons"] if w == words)


def test_a_question_is_one_card_per_question_with_its_options_as_buttons(tmp_path):
    async def body(world: World):
        session, run, cards = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        assert approval["state"] == "shown" and world.store.session(session)["state"] == "waiting"
        assert [c["approval"] for c in cards] == [questions.key(approval["id"], 0), questions.key(approval["id"], 1)]
        first, second = cards
        assert first["data"]["view"]["head"] == ["❓ notes · Жильё · 1 of 2"], "one line: no session number to read"
        assert first["data"]["view"]["body"] == "Где жить?\n\n1. Центр — старый город\n2. Окраина — тихо"
        assert first["data"]["view"]["status"] == questions.OPEN_SINGLE
        assert labels(first) == ["Центр", "Окраина"]
        assert labels(second) == ["☐ Зонт", "☐ Ноутбук", "Done"] and second["data"]["view"]["status"] == \
            questions.OPEN_MULTI
        assert world.store.face_session(session)["waiting"] == 1, "a question waits on the owner like a card"
        # The model's call stays deferred while it asks: never left to the classifier, never run unanswered — asked
        # again by a later run of the session (its first run's token is spent with it).
        world.store.add_run(session, world.platform.gate.token_hash("later"), [], world.clock.now)
        assert decision(await world.hook("PreToolUse", {"hook_token": "later"}, pre_tool_use())) == "defer"
    scenario(tmp_path, body)


def test_buttons_answer_each_question_and_the_resume_after_a_restart_hands_the_cli_the_answers_once(tmp_path):
    async def body(world: World):
        session, run, (first, second) = await asked(world)
        pressed = await world.owner("press", {"button": button(first, "Окраина")})
        assert pressed["toast"] == "Accepted. Still to answer: 1."
        assert pressed["view"]["status"] == "Answer: Окраина" and pressed["keep"] == []
        again = await world.owner("press", {"button": button(first, "Центр")})
        assert again == {"toast": "The button no longer works."}, "the question's other buttons die with the answer"
        assert world.lanes["private"].runs() == [run], "no resume while a question has no answer"

        await world.down()  # a press an hour later, after a restart
        world.clock.now += HOUR
        await world.up()
        ticked = await world.owner("press", {"button": button(second, "☐ Зонт")})
        assert [w for w, _ in ticked["keep"]] == ["☑ Зонт", "☐ Ноутбук", "Done"] and ticked["toast"] == ""
        assert [b for _, b in ticked["keep"]] == [b for _, b in second["data"]["buttons"]], "the same buttons"
        # Two fast taps on the message as first shown — before its rewrite reached the phone — both count.
        ticked = await world.owner("press", {"button": button(second, "☐ Ноутбук")})
        assert [w for w, _ in ticked["keep"]] == ["☑ Зонт", "☑ Ноутбук", "Done"]
        untick = await world.owner("press", {"button": button(second, "☐ Ноутбук")})
        retick = await world.owner("press", {"button": button(second, "☐ Ноутбук")})
        assert [w for w, _ in untick["keep"]][1] == "☐ Ноутбук" and [w for w, _ in retick["keep"]][1] == "☑ Ноутбук"
        keep = dict((w, b) for w, b in retick["keep"])
        done = await world.owner("press", {"button": keep["Done"]})
        assert done["toast"] == "Answer passed to the session." and done["view"]["status"] == "Answer: Зонт, Ноутбук"
        [approval] = world.store.approvals(session=session, kind="question")
        assert approval["state"] == "answered"
        updates = [n for n in world.notes("update") if n["approval"] == questions.key(approval["id"], 1)]
        assert updates[-1]["data"] == {"view": done["view"], "buttons": []}, "the last note keeps the answer"

        first_run, resume = world.lanes["private"].runs()
        assert resume["prompt"] is None and resume["cli_session"] == json.loads(DEFERRED[0])["session_id"]
        allowed = await world.hook("PreToolUse", resume, pre_tool_use())
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert allowed["hookSpecificOutput"]["updatedInput"] == {
            **ASK, "answers": {"Где жить?": "Окраина", "Что взять?": "Зонт, Ноутбук"}}
        assert world.store.approval(approval["id"])["state"] == "executing"
        await world.hook("PostToolUse", resume, {**pre_tool_use(), "hook_event_name": "PostToolUse"})
        assert world.store.approval(approval["id"])["state"] == "executed"
        late = await world.owner("press", {"button": keep["☑ Зонт"]})
        assert late == {"toast": "The button no longer works."}, "an answer voids the ticks too"
        again = await world.owner("press", {"button": keep["Done"]})
        assert again == {"toast": "The button no longer works."}, "«Done» acts once"
    scenario(tmp_path, body)


def test_a_reply_to_a_question_card_is_the_owners_own_answer_and_elsewhere_a_message(tmp_path):
    async def body(world: World):
        session, run, (first, second) = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        typed = await world.owner("input", {"input_id": "tg:5", "session": session, "text": "  У моря  ",
                                            "card": questions.key(approval["id"], 1)})
        assert typed == {"say": "Accepted. Still to answer: 1."}
        assert world.store.approval(approval["id"])["view"]["answers"]["1"] == {"value": "У моря", "other": True}
        update = [n for n in world.notes("update") if n["approval"] == questions.key(approval["id"], 1)][-1]
        assert update["data"]["view"]["status"] == "Your answer: У моря" and update["data"]["buttons"] == []
        assert world.store.first_input(session) == "куда поехать", "an answer is not a message into the session"
        twice = await world.owner("input", {"input_id": "tg:6", "session": session, "text": "в горы",
                                            "card": questions.key(approval["id"], 1)})
        assert twice["say"].startswith("This question is already answered")
        done = await world.owner("input", {"input_id": "tg:7", "session": session, "text": "Окраина",
                                           "card": questions.key(approval["id"], 0)})
        assert done == {"say": "Answer passed to the session."}
        redelivered = await world.owner("input", {"input_id": "tg:7", "session": session, "text": "Окраина",
                                                  "card": questions.key(approval["id"], 0)})
        assert redelivered == {"say": "The session already has this message."}, "the router sending it again"
        assert world.store.input_session("tg:7") is None and len(world.lanes["private"].runs()) == 2
        assert [c for c in world.lanes["private"].commands if c[0] == "/answer"] == [], "not a message too"
        [_, resume] = world.lanes["private"].runs()
        allowed = await world.hook("PreToolUse", resume, pre_tool_use())
        assert allowed["hookSpecificOutput"]["updatedInput"]["answers"] == {"Где жить?": "Окраина",
                                                                            "Что взять?": "У моря"}
    scenario(tmp_path, body)


def test_a_new_message_makes_the_question_stale_and_the_model_is_told_so(tmp_path):
    async def body(world: World):
        session, run, (first, second) = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        await world.owner("press", {"button": button(first, "Окраина")})
        said = await world.owner("input", {"input_id": "tg:9", "session": session, "text": "лучше Батуми"})
        assert said["say"] == "Accepted."
        assert world.store.approval(approval["id"])["state"] == "stale"
        retired = [n for n in world.notes("update") if n["approval"] == questions.key(approval["id"], 1)][-1]
        assert retired["data"]["view"]["status"] == "Expired: you wrote a new message"
        assert retired["data"]["buttons"] == []
        assert (await world.owner("press", {"button": button(second, "☐ Зонт")}))["toast"] == \
            "The button no longer works."
        [_, resume] = world.lanes["private"].runs()
        assert resume["prompt"] == "лучше Батуми"
        denied = await world.hook("PreToolUse", resume, pre_tool_use())
        assert decision(denied) == "deny" and denied["hookSpecificOutput"]["permissionDecisionReason"] == gate.STALE
        late = await world.owner("input", {"input_id": "tg:10", "session": session, "text": "и ещё",
                                           "card": questions.key(approval["id"], 1)})
        assert late["say"] != "Answer passed to the session.", "a reply to a stale question is a message into the session"
        assert world.store.first_input(session) == "куда поехать" and world.store.input_session("tg:10") == session
    scenario(tmp_path, body)


def test_an_unanswered_question_expires_in_seven_days_and_is_denied(tmp_path):
    async def body(world: World):
        session, run, cards = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        world.clock.now += 7 * DAY + 1
        world.platform.tick()
        assert world.store.approval(approval["id"])["state"] == "expired"
        assert world.store.session(session)["state"] == "idle"
        for n in (0, 1):
            note = [x for x in world.notes("update") if x["approval"] == questions.key(approval["id"], n)][-1]
            assert note["data"]["view"]["status"] == "Expired: no decision for seven days"
        await world.owner("input", {"input_id": "tg:3", "session": session, "text": "ну что"})
        [_, resume] = world.lanes["private"].runs()
        denied = await world.hook("PreToolUse", resume, pre_tool_use())
        assert denied["hookSpecificOutput"]["permissionDecisionReason"] == gate.UNANSWERED
    scenario(tmp_path, body)


def test_the_face_answers_by_the_same_rules_and_the_telegram_card_follows(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, run, (first, second) = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        url = f"/api/approvals/{approval['id']}/answer"
        shown = (await world.get(f"/api/sessions/{session}"))["approvals"]
        assert [a["kind"] for a in shown] == ["question"] and shown[0]["view"] == questions.open_view()

        for wrong in ({"question": 0, "labels": ["Энск"]}, {"question": 0, "labels": ["Центр", "Окраина"]},
                      {"question": 5, "labels": ["Окраина"]}, {"question": 1, "labels": []}):
            status, said, _ = await world.call("POST", url, {"digest": approval["digest"], **wrong})
            assert status == 200 and said["approval"]["view"]["answers"] == {}, wrong
        status, said, _ = await world.call("POST", url, {"digest": "x" * 64, "question": 0, "labels": ["Окраина"]})
        assert said["toast"] == platformd.STALE and said["approval"]["view"]["answers"] == {}, "bound to the digest"

        status, said, _ = await world.call("POST", url, {"digest": approval["digest"], "question": 0,
                                                         "labels": ["Окраина"]})
        assert status == 200 and said["toast"] == "Accepted. Still to answer: 1."
        assert said["approval"]["view"]["answers"] == {"0": {"value": "Окраина", "other": False}}
        update = [n for n in world.notes("update") if n["approval"] == questions.key(approval["id"], 0)][-1]
        assert update["data"]["view"]["status"] == "Answer: Окраина" and update["data"]["buttons"] == []
        assert (await world.owner("press", {"button": button(first, "Центр")}))["toast"] == \
            "The button no longer works.", "an answer in the face retires the Telegram buttons"

        status, said, _ = await world.call("POST", url, {"digest": approval["digest"], "question": 1,
                                                         "labels": ["Ноутбук", "Зонт", "Зонт"]})
        assert said["toast"] == "Answer passed to the session." and said["approval"]["state"] == "answered"
        assert said["approval"]["view"]["answers"]["1"]["value"] == "Зонт, Ноутбук", "in the options' order, once"
        status, said, _ = await world.call("POST", url, {"digest": approval["digest"], "question": 1, "text": "x"})
        assert said["toast"] == platformd.STALE
    faced(tmp_path, body)


def test_a_side_session_never_asks_a_question(tmp_path):
    """A side session reads and answers; AskUserQuestion is one of its deny rules, so the CLI refuses it and no card
    ever waits for it."""
    from retinue import approvals
    from retinue.config import LaneRef, Track
    marks = approvals.marks_for(Track("notes", "private", "private"), LaneRef("private", "", "", ""), read_only=True)
    assert approvals.Mark("deny", "AskUserQuestion") in marks
    assert approvals.Mark("deny", "AskUserQuestion") not in approvals.marks_for(
        Track("notes", "private", "private"), LaneRef("private", "", "", ""))


def test_a_reply_with_files_to_a_question_card_is_a_message_with_its_files_never_an_answer(tmp_path):
    from test_platformd import _wire

    async def body(world: World):
        session, run, (first, second) = await asked(world)
        [approval] = world.store.approvals(session=session, kind="question")
        said = await world.owner("input", {"input_id": "tg:5", "session": session, "text": "вот план",
                                           "card": questions.key(approval["id"], 1),
                                           "attachments": [_wire("plan.txt", b"plan")]})
        assert said["say"].startswith("Accepted")
        assert "1" not in (world.store.approval(approval["id"])["view"].get("answers") or {}), "not its answer"
        assert world.store.input_session("tg:5") == session
        last = world.lanes["private"].runs()[-1]
        assert last["files"][0]["name"] == "plan.txt" and last["prompt"][0]["text"].startswith("вот план")

    scenario(tmp_path, body)
