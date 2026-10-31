"""What the router says unasked: the zone that follows the owner, the base's deadlines, the week of the tracker, a
Mac that has not pushed, and the heartbeat that stays silent — and runs no model — when nothing needs him."""

import asyncio
import dataclasses
import json
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from retinue.archive import Archive
from retinue.core import Core, Reply
from retinue.proactive import Proactive, deadline_line, scan
from retinue.protocol import Store
from retinue.scheduler import Scheduler

from test_core import AGENT, FakeChannel, drain, local  # noqa: F401

ASSISTANT = dataclasses.replace(AGENT, reminders=True)
TODAY = date(2026, 10, 15)


def record(root, path, **head):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}: {value}" for key, value in head.items()]
    file.write_text("---\n" + "\n".join(lines) + "\n---\n\n# Запись\n", encoding="utf-8")


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def test_the_bases_dates_ahead_and_past_closed_records_left_out(tmp_path):
    record(tmp_path, "personal/profile/gym.md", title="Абонемент", until=day(3))
    record(tmp_path, "personal/knowledge/rules.md", title="Правила спортзала", stale_after=day(-2), status="current")
    record(tmp_path, "personal/knowledge/old.md", title="Старое", stale_after=day(-60))
    record(tmp_path, "personal/knowledge/gone.md", title="Заменено", until=day(1), status="superseded")
    record(tmp_path, "misc/artifacts/form.md", title="Заявка на грант", deadline=day(4), status="draft")
    record(tmp_path, "misc/journal/2026-10-01-x.md", title="Сделано", deadline=day(2), status="done")
    record(tmp_path, "personal/profile/far.md", title="Далеко", until=day(20))
    record(tmp_path, ".scratch/notes/x.md", title="Черновик", deadline=day(1))
    record(tmp_path, "sport/profile/old-gym.md", title="Абонемент", until=day(-2))  # closed: no «overdue»
    record(tmp_path, "personal/profile/broken.md", title="Сломано", until="2026-02-30")
    (tmp_path / "personal/profile/garbled.md").write_text("---\nuntil: [\n---\n", encoding="utf-8")
    items, old = scan(tmp_path, TODAY)
    assert [(i.path, i.field) for i in items] == [("personal/knowledge/rules.md", "stale_after"),
                                                  ("personal/profile/gym.md", "until"),
                                                  ("misc/artifacts/form.md", "deadline")]
    assert old == 1, "a check overdue for months is counted, not listed"
    assert [deadline_line(i, TODAY) for i in items] == [
        "- overdue by 2 days (13 October): Правила спортзала — recheck · personal/knowledge/rules.md",
        "- in 3 days (18 October): Абонемент — valid until · personal/profile/gym.md",
        "- in 4 days (19 October): Заявка на грант — deadline · misc/artifacts/form.md"]
    assert scan(tmp_path / "none", TODAY) == ([], 0)
    assert all(i.path != "personal/profile/broken.md" for i in items), "a date that is no date: that file skipped"


NOW = datetime(2026, 10, 7, 11, 5, tzinfo=timezone.utc).timestamp()  # Wednesday, 11:05 UTC


def test_a_new_zone_moves_what_recurs_and_keeps_the_moment_of_a_one_off(tmp_path):
    jobs = Scheduler(Store(str(tmp_path / "r.sqlite")).db)
    jobs.add("таблетки", "2026-10-08T09:00", "чт", NOW, repeat="daily")
    jobs.add("позвонить Х", "2026-10-09T18:00", "пт", NOW)
    jobs.ensure_summary(NOW)
    assert jobs.rezone("Asia/Tokyo", NOW) == (2, 1)
    pills, call = jobs.get(1), jobs.get(2)
    assert (pills.local, pills.tz) == ("2026-10-08T09:00", "Asia/Tokyo")
    assert pills.due == datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp(), "9 in Tokyo"
    assert call.due == datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc).timestamp(), "the same instant"
    assert jobs.line(call, numbered=False) == "Sat 10 October, 03:00 Asia/Tokyo — позвонить Х", "told in his clock"
    summary = jobs.db.execute("SELECT tz, due FROM jobs WHERE kind = 'summary'").fetchone()
    assert summary == ("Asia/Tokyo", datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert jobs.rezone("Asia/Tokyo", NOW) == (0, 1), "the same zone again moves nothing"

    jobs = Scheduler(Store(str(tmp_path / "s.sqlite")).db)
    jobs.add("зарядка", "2026-10-07T15:00", "ср", NOW, repeat="daily")  # 15:00 UTC, still ahead today
    early = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc).timestamp()  # 03:00 UTC, the summary not sent yet
    jobs.ensure_summary(NOW)
    jobs.rezone("Asia/Manila", early)
    assert jobs.get(1).local == "2026-10-08T15:00", "15:00 in Manila has passed today: the next one, not late"
    assert jobs.db.execute("SELECT due FROM jobs WHERE kind = 'summary'").fetchone()[0] == early, \
        "09:00 in Manila has passed and he has had no summary: now"


def test_a_series_firing_while_the_zone_changes_goes_on_in_the_new_zone(tmp_path):
    jobs = Scheduler(Store(str(tmp_path / "r.sqlite")).db)
    jobs.add("таблетки", "2026-10-08T09:00", "чт", NOW, repeat="daily")
    (job,) = jobs.reminders()
    fired = job.due + 5
    assert jobs.claim(job, fired)
    assert jobs.rezone("Asia/Tokyo", fired) == (1, 0)
    jobs.done(job, fired)  # the job as it was taken, in the old zone
    after = jobs.get(job.id)
    assert (after.status, after.local, after.tz) == ("active", "2026-10-09T09:00", "Asia/Tokyo")
    assert after.due == datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc).timestamp(), "9 in Tokyo, not in UTC"


def test_a_zone_is_an_area_and_a_city_wherever_it_is_taken(tmp_path):
    from retinue import clock

    assert [clock.find_zone(n) for n in ("Singapore", "Tokyo", "Japan", "PRC", "W-SU", "EST", "Etc/GMT+3")] == [
        "Asia/Singapore", "Asia/Tokyo", None, None, None, None, None]
    assert clock.find_zone("UTC") == "UTC"
    from zoneinfo import available_timezones
    assert all(clock.valid_zone(found) for found in filter(None, map(clock.find_zone, available_timezones())))
    store = Store(str(tmp_path / "r.sqlite"))
    store.set("owner.tz", "Japan")  # what an older build may have stored
    assert Core([ASSISTANT], store, "owner", proactive=Proactive()).tz == "UTC"


def make_core(tmp_path, ask=None, memory=None, rand=lambda: 1.0, backup=None):
    store = Store(str(tmp_path / "r.sqlite"))
    core = Core([ASSISTANT], store, "owner", ask=ask, archive=Archive(str(tmp_path / "a.sqlite")),
                backup_status=backup, proactive=Proactive(rand=rand))
    core.memory = memory
    return core


def test_the_zone_is_changed_in_words_kept_across_restarts_and_checked(tmp_path):
    async def run():
        core = make_core(tmp_path)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        for mark in ("mail", "telegram", "calendar", "travel", "forwarded", "attachment", "archive"):
            judged = core.turns.open_root("assistant")
            judged.tree.foreign.add(mark)
            ok, text = await core.proactive.zone_tool(ASSISTANT, judged.id, "Asia/Tokyo")
            assert not ok and text.startswith(f"Not changed: this turn had someone else's text in it ({mark})"), mark
        chat = core.turns.open_root("assistant")
        chat.tree.exchanges.add("chat:777")
        assert (await core.proactive.zone_tool(ASSISTANT, chat.id, "Asia/Tokyo"))[0] is False, "a run a chat started"
        assert core.tz == "UTC" and telegram.cards == [], "a letter saying «I'm in Tokyo» moves nothing"
        turn = core.turns.open_root("assistant")
        ok, text = await core.proactive.zone_tool(ASSISTANT, turn.id, "Tokyo")
        assert ok and text.startswith("The zone is now Asia/Tokyo: it is ")
        assert core.tz == core.jobs.tz == "Asia/Tokyo"
        assert telegram.cards[-1][0] == "Zone: UTC → Asia/Tokyo.", "never silent"
        assert core.store.get(f"now.{core.store.conversation('assistant')}") == "1", "her line with the owner's zone is renewed"
        assert (await core.proactive.zone_tool(ASSISTANT, turn.id, "Narnia")) == (
            False, "Not changed: «Narnia» is not a zone. It needs an IANA name, e.g. Asia/Tokyo.")
        assert (await core.proactive.zone_tool(AGENT, turn.id, "Asia/Manila"))[0] is False, "no reminders grant"
        assert (await core.proactive.zone_tool(ASSISTANT, "gone", "Asia/Manila"))[0] is False, "no turn"
        again = make_core(tmp_path)
        assert again.tz == again.jobs.tz == "Asia/Tokyo", "the owner's word outlives router.yaml's zone"

    asyncio.run(run())


TRACKER_HEAD = "---\nid: tracker\nweekly_goal: 7\n---\n\n| # | Date | Item | Detail | Score | Status |\n|---|---|---|---|---|---|\n"


def tracker(*rows):
    return TRACKER_HEAD + "".join(f"| {n} | 2026-10-01 | C{n} | R | N/A | {status} |\n" for n, status in rows)


def fake_memory(tmp_path, history, last_mac):
    """The base: files in a folder, the hub's log as two answers."""
    tree = tmp_path / "kb"
    (tree / "profile").mkdir(parents=True, exist_ok=True)
    (tree / "profile/tracker.md").write_text(history[-1][1], encoding="utf-8")

    def file_at(path, ts):
        known = [text for moment, text in history if moment <= ts]
        return known[-1] if known else None

    return SimpleNamespace(tree=str(tree), cfg=SimpleNamespace(digest_at="21:00"), sync=lambda: None,
                           file_at=file_at, last_foreign_commit=lambda: last_mac, changed_since=lambda ts: {})


def test_the_monday_summary_gets_deadlines_the_trackers_week_and_the_mac(tmp_path, monkeypatch):
    clock = [local(12, 8, 0)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    memory = fake_memory(tmp_path, [
        (local(1, 0), tracker((1, "New"), (2, "Sent"))),
        (local(8, 12), tracker((1, "Sent"), (2, "Sent"), (3, "Sent"), (4, "New"))),
        (local(12, 7), tracker((1, "Sent"), (2, "Lost"), (3, "Sent"), (4, "Sent")))],
        last_mac=local(7, 10))
    record(tmp_path / "kb", "personal/profile/gym.md", title="Абонемент", until="2026-10-20")

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None):
        return Reply("done", "Доброе утро.", [], {})

    async def run():
        core = make_core(tmp_path, ask=fake_ask, memory=memory)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        core.jobs.ensure_summary(clock[0])
        clock[0] = local(12, 9, 0) + 10
        await core.tick(clock[0])
        await drain()
        return core, telegram

    core, telegram = asyncio.run(run())
    (card,) = [c[0] for c in telegram.cards]
    parts = card.split("\n\n")
    assert parts[:4] == ["Доброе утро.", "Deadlines, 14 days:\n- in 8 days (20 October): Абонемент — valid until · "
                         "personal/profile/gym.md",
                         "Tracker for the week: 2 (goal 7).",
                         "Mac has not pushed for 5 days: its last commit in the hub is from 7 October."], \
        "rows 1 and 3 were sent last week; row 4 this Monday morning belongs to this week"
    assert parts[4].startswith("Health for the day:")
    assert asyncio.run(core.proactive.weekly_line(local(12, 9))) == "Tracker for the week: 1 (goal 7).", \
        "the weekly review counts since this Monday"


def test_the_heartbeat_speaks_only_when_something_needs_him(tmp_path, monkeypatch):
    thursday = local(15, 13)
    monkeypatch.setattr(time, "time", lambda: thursday)
    memory = fake_memory(tmp_path, [(local(1, 0), tracker((1, "Sent")))], last_mac=thursday)
    tree = tmp_path / "kb"
    record(tree, "personal/profile/gym.md", title="Абонемент", until="2026-10-17")
    record(tree, "personal/knowledge/rules.md", title="Правила", stale_after="2026-10-16")
    record(tree, "profile/location.md", title="Где", tz="Asia/Tokyo")
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"ok": False, "finished": thursday - 3600, "last_ok": thursday - 3 * 86400,
                                  "error": "repository locked"}))
    asked = []

    async def fake_ask(*args, **kwargs):
        asked.append(args)
        return Reply("done", "x", [], {})

    async def run():
        core = make_core(tmp_path, ask=fake_ask, memory=memory, backup=str(backup))
        core.store.set("proactive.seeded", "2026-10-01")  # not the first heartbeat since the deploy
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        said = await core.proactive.heartbeat(thursday)
        assert said == ["In the Base (profile/location.md) your zone is now Asia/Tokyo. Change it?",
                        "Due soon:\n- in 2 days (17 October): Абонемент — valid until · personal/profile/gym.md",
                        "Thursday, and «Tracker» has zero for the week (goal 7). One today?",
                        "Backup: no backup for 3 d: repository locked."], "stale knowledge waits for the summary"
        assert await core.proactive.heartbeat(thursday + 900) == [], "each is said once"
        assert await core.proactive.heartbeat(local(15, 23)) == [], "quiet hours"
        (_, buttons, _) = telegram.cards[0]
        assert [label for label, _ in buttons] == ["Yes, Asia/Tokyo", "No"]
        await core.press(telegram, buttons[0][1])
        assert core.tz == "Asia/Tokyo" and telegram.cards[-1][0].startswith("The zone is now Asia/Tokyo")
        return core

    asyncio.run(run())
    assert asked == [], "no model run for any of it"


def test_the_first_heartbeat_after_a_deploy_takes_stock_and_bursts_nothing(tmp_path, monkeypatch):
    thursday = local(15, 23)  # deployed late on Thursday: quiet hours, and the stock is taken all the same
    monkeypatch.setattr(time, "time", lambda: thursday)
    memory = fake_memory(tmp_path, [(local(1, 0), tracker((1, "Sent")))], last_mac=thursday)
    record(tmp_path / "kb", "personal/profile/gym.md", title="Абонемент", until="2026-10-17")
    record(tmp_path / "kb", "personal/profile/card.md", title="Карта", until="2026-10-14")

    async def run():
        core = make_core(tmp_path, memory=memory)
        await core.start([FakeChannel("telegram", False)])
        assert await core.proactive.heartbeat(thursday) == []
        assert await core.proactive.heartbeat(local(16, 10)) == [], \
            "Friday morning: no push already due at the deploy, no Thursday nudge late"
        record(tmp_path / "kb", "personal/profile/lease.md", title="Аренда", until="2026-10-21")
        core.proactive.scanned = None
        assert await core.proactive.heartbeat(local(18, 10)) == [
            "Due soon:\n- in 3 days (21 October): Аренда — valid until · personal/profile/lease.md"], \
            "a date that comes due after the deploy is pushed"
        assert await core.proactive.heartbeat(local(22, 13)) == [
            "Thursday, and «Tracker» has zero for the week (goal 7). One today?"], \
            "the next week's Thursday is told"

    asyncio.run(run())


def test_her_own_thought_is_a_run_of_its_own_rare_braked_by_code_and_may_be_silence(tmp_path, monkeypatch):
    from retinue.core import history_line

    clock = [local(15, 12)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    answers, asked = ["«— »", "By the way, the gym pass ends in a week: renew it?"], []

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None):
        asked.append((text, context_id, control, turn_id))
        return Reply("done", answers[len(asked) - 1], [], {})

    async def run():
        core = make_core(tmp_path, ask=fake_ask, rand=lambda: 0.0)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        conversation = core.store.conversation("assistant")
        assert await core.proactive.heartbeat(clock[0]) == [], "she said «—»: nothing sent"
        (text, context_id, control, turn_id) = asked[0]
        assert control == "oneshot" and context_id.startswith("initiative-") and turn_id is None, \
            "outside the conversation's session, and with no turn: nothing she could write"
        assert text.startswith("[Initiative.") and "The conversation over the last day:" in text
        assert core.archive.recent(conversation, 10) == [] and telegram.events == [], "«—» leaves no trace"
        assert core.store.get(f"now.{conversation}") is None, "the conversation's session is untouched"
        clock[0] = local(15, 18)
        await core.proactive.heartbeat(clock[0])
        assert len(asked) == 1, "the day's cap and the gap"
        clock[0] = local(16, 9, 10)
        core.store.db.execute("INSERT INTO jobs (kind, text, local, tz, due, status, created, fired) VALUES "
                              "('summary', '', '2026-10-16T09:00', 'UTC', ?, 'sent', ?, ?)",
                              (local(16, 9), local(16, 9), local(16, 9)))
        await core.proactive.heartbeat(clock[0])
        assert len(asked) == 1, "not within half an hour after the morning summary"
        clock[0] = local(16, 12)
        assert await core.proactive.heartbeat(clock[0]) == ["thought"]
        (event,) = core.archive.recent(conversation, 10)
        assert telegram.cards[-1][0] == "By the way, the gym pass ends in a week: renew it?" and event.meta["initiative"]
        assert "You, on your own initiative (a separate run): By the way" in history_line(event), "her next turn sees it as hers"
        core.archive.append("owner", "привет", conversation_id=conversation, channel="telegram", ts=local(18, 11))
        clock[0] = local(18, 12)
        await core.proactive.heartbeat(clock[0])
        assert len(asked) == 2, "he is talking to her: no thought of her own"
        assert core.proactive.command("/initiative выкл").startswith("Initiative: off — I don't write first")
        clock[0] = local(20, 12)
        await core.proactive.heartbeat(clock[0])
        assert len(asked) == 2, "off is off"
        assert core.store.db.execute("SELECT kind FROM runs").fetchall() == [("initiative",), ("initiative",)]

    asyncio.run(run())


def test_the_initiative_command(tmp_path):
    core = make_core(tmp_path)
    proactive = core.proactive
    assert proactive.command("!initiative") == (
        "Initiative: normal — I write first without a reason about once every day and a half, at most once a day. "
        "Quiet hours 22:00–09:00 (UTC): in them I write nothing but reminders. To change: /initiative less | normal | "
        "more | off, /initiative quiet 23-8.")
    assert proactive.command("!initiative чаще").startswith("Initiative: more")
    assert "Quiet hours 23:00–08:00" in proactive.command("!initiative тихо 23-8")
    assert proactive.command("!initiative тихо завтра").startswith("Quiet hours go like this:")
    assert proactive.command("!initiative louder").startswith("Didn't get that.")
    assert Core([ASSISTANT], core.store, "owner", proactive=Proactive()).proactive.level() == "more", "kept"


def test_the_initiative_command_in_english_and_a_level_kept_in_russian(tmp_path):
    core = make_core(tmp_path)
    proactive = core.proactive
    for word, level in (("less", "less"), ("normal", "normal"), ("more", "more"), ("off", "off")):
        assert proactive.command(f"!initiative {word}").startswith(f"Initiative: {level} — "), word
        assert core.store.get("initiative.level") == level
    assert "Quiet hours 23:00–08:00" in proactive.command("!initiative quiet 23-8")
    assert proactive.command("!initiative quiet tomorrow").startswith("Quiet hours go like this:")
    core.store.set("initiative.level", "реже")  # a store set before the English
    assert proactive.level() == "less"
    assert proactive.command("!initiative").startswith("Initiative: less — I write first without a reason rarely")


def test_a_backup_line_is_an_alert_in_either_language(tmp_path):
    from types import SimpleNamespace

    from retinue.proactive import Proactive as P

    for said, alert in (("no backup for 2 d: x", True), ("backup — 7 h ago, the last run failed: x", True),
                        ("backup status unreadable", True), ("бэкапа нет 2 сут: x", True),
                        ("бэкап — 7 ч назад, последний запуск не удался: x", True), ("статус бэкапа не читается", True),
                        ("backup — 5 h ago", False), ("backup — 5 h ago, with a warning: 1 file unreadable", False)):
        p = P()
        store = {}
        p.core = SimpleNamespace(backup_status=str(tmp_path / "none.json"), backup=lambda now, s=said: s,
                                 store=SimpleNamespace(get=store.get, set=store.__setitem__))
        assert bool(p.backup_alert(0.0)) is alert, said
