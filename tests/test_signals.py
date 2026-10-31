"""How the work went, counted: the gate journals every answer it gives — the tool and how it went, never the input —
the engine's adapter reads tool errors and compactions off a session's lines, `/owner/stats` tallies a week of it, and
the router words that tally as «How the week went» in the weekly review and the base's health in Monday's summary."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from retinue import signals
from retinue.proactive import base_health, week_lines
from retinue.sessions import PlatformStore

from test_gate import auto, call, setup, short_wait, work  # noqa: F401
from test_gate import hook as gate_hook
from test_platformd import DEFERRED, World, scenario  # noqa: F401
from test_proactive import make_core, record

TODAY = date(2030, 3, 11)


def journal(store: PlatformStore) -> list[tuple]:
    return store.db.execute("SELECT kind, tool, what, call FROM signals ORDER BY id").fetchall()


def test_the_gate_journals_how_each_call_went_and_never_its_input(setup):
    store, the_gate, _, _ = setup
    gate_hook(the_gate, "PreToolUse", call("make secret-target", call_id="toolu_1"))  # a fallback run: to the owner
    gate_hook(the_gate, "PreToolUse", auto(call("make test", call_id="toolu_2")))      # nothing names it: classifier
    denied = {**auto(call("git remote set-url origin https://example.org/x", call_id="toolu_3")),
              "reason": "[Remote Repoint] the command points origin at https://example.org/x"}
    gate_hook(the_gate, "PermissionDenied", denied)
    gate_hook(the_gate, "PreToolUse", call("ls", call_id="toolu_4"), token="forged")  # no run: nothing to count
    assert journal(store) == [("gate", "Bash", "defer: Waiting for the owner's decision", "toolu_1"),
                              ("gate", "Bash", "classifier", "toolu_2"),
                              ("gate", "Bash", "blocked: [Remote Repoint]", "toolu_3")]
    kept = json.dumps(store.db.execute("SELECT * FROM signals").fetchall(), ensure_ascii=False)
    assert "secret-target" not in kept and "example.org" not in kept, "the input and the classifier's prose stay out"


def test_a_journal_that_fails_never_holds_a_call_back(setup):
    store, the_gate, _, _ = setup
    store.db.execute("DROP TABLE IF EXISTS signals")
    assert gate_hook(the_gate, "PreToolUse", auto(call("make test"))) == {}


def test_the_adapter_reads_tool_errors_and_compactions_and_nothing_else():
    failed = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "exit 1"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]}}
    assert signals.from_line(failed) == [(signals.TOOL_ERROR, "toolu_1")]
    assert signals.from_line({"type": "system", "subtype": "compact_boundary",
                              "compact_metadata": {"trigger": "auto"}}) == [(signals.COMPACTION, "")]
    for line in ({"type": "system", "subtype": "init"}, {"type": "assistant", "message": {"content": "x"}},
                 {"type": "result", "is_error": True}, {"type": "user", "message": {"content": "привет"}}):
        assert signals.from_line(line) == []


def test_a_signal_with_a_key_is_kept_once():
    store = PlatformStore(":memory:")
    signals.prepare(store, 0.0)
    for _ in range(2):
        signals.add(store, signals.COMPACTION, "s1", 10.0, key="code:7:0")
    assert len(journal(store)) == 1


def test_old_signals_go_when_the_platform_starts():
    store = PlatformStore(":memory:")
    signals.prepare(store, 0.0)
    signals.add(store, signals.OWNER_STOP, "s1", 0.0)
    signals.prepare(store, signals.KEEP_DAYS * 86400 + 1)
    assert journal(store) == []


def test_the_week_is_tallied_from_the_journal_and_the_platforms_own_tables(tmp_path):
    async def body(world: World):
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "почини"}))["session"]
        [run] = world.lanes["code"].runs()
        await world.hook("PreToolUse", run, auto(call("make test", call_id="toolu_9")))
        tool_failed = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9",
                                                                "is_error": True, "content": "exit 2"}]}}
        compacted = {"type": "system", "subtype": "compact_boundary"}
        failed = {"type": "result", "subtype": "error_max_turns", "is_error": True, "stop_reason": None}
        world.lanes["code"].add(session, run["run"], DEFERRED[0], json.dumps(tool_failed), json.dumps(compacted),
                                json.dumps(failed))
        await world.poll()
        await world.poll()  # the same lines given again add nothing
        stats = await world.owner("stats", {"since": world.clock.now - 3600, "until": world.clock.now + 1})
        assert (stats["sessions"], stats["runs"], stats["owner"]) == (1, 1, {"messages": 1, "stops": 0})
        assert stats["engine"] == {"tool_errors": 1, "tool_errors_by_tool": {"Bash": 1}, "compactions": 1,
                                   "run_errors": 1}
        assert stats["gate"]["calls"] == 1 and stats["gate"]["by_decision"] == {"classifier": 1}
        status, said = await world.post("/owner/stats", {"since": "вчера"})
        assert status == 400 and "since" in said["error"]
        assert (await world.post("/owner/stats", {"since": 0}, {}))[0] == 401, "the router's token only"

    scenario(tmp_path, body)


def test_the_week_in_words_names_each_count_and_says_nothing_of_what_is_zero():
    stats = {"sessions": 4, "sub_sessions": 2, "runs": 11, "owner": {"messages": 30, "stops": 1},
             "engine": {"tool_errors": 5, "tool_errors_by_tool": {"Bash": 4, "Edit": 1}, "compactions": 2,
                        "run_errors": 1},
             "gate": {"calls": 200, "by_decision": {"rule": 150, "classifier": 40, "blocked": 2, "defer": 6, "deny": 2},
                      "reasons": {"deny: the owner denied it": 2}},
             "cards": {"approval": {"shown": 6, "granted": 4, "rejected": 1, "answered": 0, "open": 0, "lapsed": 1},
                       "question": {"shown": 2, "answered": 2, "granted": 0, "rejected": 0, "open": 0, "lapsed": 0},
                       "courier": 0}}
    assert week_lines(stats, {"done": 20, "error": 1, "limit": 2}) == [
        "- Sessions started: 4 and sub-sessions 2; runs: 11; your messages in them: 30; «Stop»: 1.",
        "- Permission cards: 6 — you allowed 4, denied 1, expired or stale 1; questions: 2, answered 2.",
        "- Gate: 200 answers — by rules 150, by the classifier 40 (stopped 2), to you 6, denied by the gate 2. "
        "Most often: «deny: the owner denied it» — 2.",
        "- Tool errors: 5 (Bash — 4, Edit — 1); context compactions: 2; turns ended with an error: 1.",
        "- The assistant: runs 23, failed 1, hit the limit 2."]


def test_the_weekly_block_says_when_the_platform_did_not_answer(tmp_path):
    async def unreachable(call, body, timeout):
        raise OSError("down")

    async def run():
        core = make_core(tmp_path)
        core.store.db.execute("INSERT INTO runs (ts, agent_id, conversation_id, kind, status) "
                              "VALUES (?, 'assistant', 'c', 'conversation', 'done')", (1_000_000.0,))
        assert await core.proactive.work_week(1_000_000.0 + 60) == "How the week went:\n" \
            "- The assistant: runs 1, failed 0.", "no platform: her runs only"
        core.platform = unreachable
        return await core.proactive.work_week(1_000_000.0 + 60)

    assert asyncio.run(run()) == ("How the week went:\n- The platform did not answer — no session count.\n"
                                  "- The assistant: runs 1, failed 0.")


def test_the_bases_health_counts_what_its_rules_say_needs_tending(tmp_path, monkeypatch):
    monkeypatch.setattr("retinue.proactive.FOLDER_RECORDS", 2)
    monkeypatch.setattr("retinue.proactive.SHOWCASE_LINES", 3)
    record(tmp_path, "home/knowledge/boiler.md", title="Котёл", practice="unknown")
    record(tmp_path, "home/knowledge/old-boiler.md", title="Котёл", practice="unknown", status="superseded")
    record(tmp_path, "home/knowledge/meter.md", title="Счётчик", practice="matches")
    record(tmp_path, "home/journal/2030-01-02-ask-acme.md", type="task", status="open", date="2030-01-02")
    record(tmp_path, "home/journal/2030-03-01-fresh.md", type="task", status="open", date="2030-03-01")
    record(tmp_path, "home/journal/2030-01-01-closed.md", type="task", status="done", date="2030-01-01")
    (tmp_path / "home/AGENTS.md").write_text(
        "# Дом\n\n- [Котёл](knowledge/boiler.md)\n- [Нет такой](knowledge/gone.md)\n"
        "- `[пример](example.md)` и [сайт](https://example.com/a.md)\n", encoding="utf-8")
    record(tmp_path, ".scratch/knowledge/x.md", practice="unknown")
    assert base_health(tmp_path, TODAY) == [
        "- Practice unknown (practice: unknown): 1 — home/knowledge/boiler.md",
        "- Tasks open longer than 28 days: 1 — home/journal/2030-01-02-ask-acme.md",
        "- Showcases longer than 3 lines — time to split: 1 — home/AGENTS.md — 5",
        "- Folders with more than 2 Records — time to group: 2 — home/journal — 3, home/knowledge — 3",
        "- Broken links: 1 — home/AGENTS.md → knowledge/gone.md"]
    assert base_health(tmp_path / "none", TODAY) == []


def test_a_tidy_base_has_no_health_lines(tmp_path):
    record(tmp_path, "home/knowledge/meter.md", title="Счётчик", practice="matches")
    (tmp_path / "home/AGENTS.md").write_text("# Дом\n\n- [Счётчик](knowledge/meter.md)\n", encoding="utf-8")
    assert base_health(tmp_path, TODAY) == []


def test_the_weekly_review_ends_with_the_week_of_work_and_she_sees_it_among_the_facts(tmp_path, monkeypatch):
    from retinue.core import GOALS, Reply

    from test_core import FakeChannel, drain, local

    asked, sunday = [], local(11, 18, 0) + 10
    monkeypatch.setattr("time.time", lambda: sunday)
    tree = tmp_path / "kb"
    (tree / GOALS).parent.mkdir(parents=True)
    (tree / GOALS).write_text("---\nid: goals\n---\n\n# Цели\n\n- Дописать книгу до марта\n", encoding="utf-8")
    memory = SimpleNamespace(tree=str(tree), cfg=SimpleNamespace(digest_at="21:00"), sync=lambda: None,
                             changed_since=lambda ts: {}, file_at=lambda path, ts: None)

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None):
        asked.append(text)
        return Reply("done", "A steady week.", [], {})

    async def stats(call, body, timeout):
        assert call == "stats" and body["until"] - body["since"] == 7 * 86400
        return {"sessions": 2, "runs": 3, "owner": {"messages": 5, "stops": 0},
                "engine": {"tool_errors": 0, "compactions": 0, "run_errors": 0}, "gate": {}, "cards": {}}

    async def run():
        core = make_core(tmp_path, ask=fake_ask, memory=memory)
        core.platform = stats
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        core.jobs.ensure_weekly(sunday - 60)
        await core.tick(sunday)
        await drain()
        return telegram

    telegram = asyncio.run(run())
    work = ("How the week went:\n- Sessions started: 2; runs: 3; your messages in them: 5.\n"
            "- Tool errors: 0; context compactions: 0; turns ended with an error: 0.")
    (card,) = [c[0] for c in telegram.cards]
    assert card.startswith("A steady week.") and card.endswith(work), "code's block after her words"
    assert "  - Sessions started: 2; runs: 3; your messages in them: 5." in asked[0], "she reads the same numbers"
