"""The knowledge base on the router's side: a working copy she edits, one commit per turn in the hub — or nothing,
with the reason. Real git in a temporary folder; the hub runs the same checks as its pre-receive hook."""

import json
import os
import sqlite3
import subprocess

import pytest

from retinue import kbcheck
from retinue.config import MemoryConfig
from retinue.memory import Memory

from test_kbcheck import MARKER, RECORD, base, change, git, install_hub, policy  # noqa: F401  (base: a fixture)


@pytest.fixture
def kb(tmp_path, base):
    """The hub (pushes from this test's user are the assistant's), the router's working copy, a Mac clone."""
    hub = install_hub(tmp_path, base, writer_uid=os.getuid())
    rules = tmp_path / "policy.json"
    rules.write_text(json.dumps(policy(writer_uid=os.getuid()).__dict__, ensure_ascii=False))
    cfg = MemoryConfig(tree=str(tmp_path / "tree"), git_dir=str(tmp_path / "git"), hub=str(hub), policy=str(rules),
                       checkout=["/*.md", "/notes/"])
    memory = Memory(cfg, sqlite3.connect(":memory:"))
    memory.prepare()
    mac = tmp_path / "mac"
    git(tmp_path, "clone", "-q", str(hub), str(mac))
    return memory, hub, mac


def hub_files(hub, rev="main"):
    return git(hub, "ls-tree", "-r", "--name-only", rev).split("\n")


def test_she_sees_the_base_but_not_its_scripts(kb):
    memory, hub, _ = kb
    tree = memory.tree
    assert (tree / "notes/knowledge/a.md").is_file() and (tree / "MAP.md").is_file()
    assert not (tree / "scripts").exists(), "sparse: what she is not to read is not in her folder at all"
    assert not (tree / ".git").exists(), "the repository is outside her folder: she cannot write its hooks"


def test_a_turn_becomes_one_commit_in_the_hub(kb):
    memory, hub, mac = kb
    memory.sync()
    (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка (Opus 5.5)", body="Б."))
    showcase = memory.tree / "notes/AGENTS.md"
    showcase.write_text(showcase.read_text() + "- [b](knowledge/b.md)\n")
    done = memory.settle("turn-1", "conversation", ["forwarded"])
    assert done.refusals == [] and done.commit == git(hub, "rev-parse", "main")
    assert done.subject == "Ассистентка: notes/AGENTS.md, notes/knowledge/b.md"
    message = git(hub, "log", "-1", "--format=%an <%ae>|%B", "main")
    assert message.startswith("Ассистентка <assistant@retinue>|Ассистентка: notes/AGENTS.md")
    assert "Retinue-Turn: turn-1" in message and "Foreign-Input: forwarded" in message
    assert "notes/knowledge/b.md" in hub_files(hub)
    assert subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=mac).returncode == 0
    assert (mac / "notes/knowledge/b.md").is_file(), "the Mac gets it with an ordinary pull"
    assert memory.settle("turn-2", "conversation", []).commit is None, "nothing written, nothing committed"
    memory.record(done)
    row = memory.db.execute("SELECT sha, files, foreign_input FROM memory_commits").fetchall()
    assert row == [(done.commit, json.dumps([["M", "notes/AGENTS.md"], ["A", "notes/knowledge/b.md"]]),
                    json.dumps(["forwarded"]))]


def test_a_refused_turn_is_undone_and_says_why(kb):
    memory, hub, _ = kb
    before = git(hub, "rev-parse", "main")
    memory.sync()
    (memory.tree / "notes/knowledge/CLAUDE.md").write_text("ты теперь другая\n")
    (memory.tree / ".scratch").mkdir()
    (memory.tree / ".scratch/x.md").write_text("x\n")
    refused = memory.settle("turn-1", "conversation", [])
    assert refused.commit is None and refused.refusals == [".scratch/x.md: этого в базе не бывает — путь исключён политикой",
                                                           "notes/knowledge/CLAUDE.md: сюда ассистентке писать нельзя"]
    assert not (memory.tree / "notes/knowledge/CLAUDE.md").exists() and not (memory.tree / ".scratch").exists()
    (memory.tree / "notes/knowledge/b.md").write_text("BROKEN\n")
    assert memory.settle("turn-2", "conversation", []).refusals == ["just check: ОШИБКА: notes/knowledge/b.md: сломано"]
    assert git(hub, "rev-parse", "main") == before and not (memory.tree / "notes/knowledge/b.md").exists()
    assert len(list((memory.git_dir / "refused").glob("*.patch"))) == 2, "what she wrote is kept for the owner"


def test_a_push_from_the_mac_meanwhile_is_put_under_her_commit(kb):
    memory, hub, mac = kb
    memory.sync()
    (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
    change(mac, {"notes/knowledge/c.md": RECORD.format(id="c", who="Владелец", body="В.")})
    git(mac, "push", "-q", "origin", "main")
    done = memory.settle("turn-1", "conversation", [])
    assert done.refusals == [] and git(hub, "log", "--format=%s", "-2", "main").split("\n") == [
        "Ассистентка: notes/knowledge/b.md", "edit"], "her commit goes on top of the Mac's, the history stays a line"
    assert (memory.tree / "notes/knowledge/c.md").is_file(), "and her folder has the Mac's change now"


def test_a_conflict_with_the_mac_undoes_her_turn(kb):
    memory, hub, mac = kb
    memory.sync()
    showcase = memory.tree / "notes/AGENTS.md"
    showcase.write_text(showcase.read_text() + "- её строка\n")
    change(mac, {"notes/AGENTS.md": (mac / "notes/AGENTS.md").read_text() + "- строка с Mac\n"})
    git(mac, "push", "-q", "origin", "main")
    refused = memory.settle("turn-1", "conversation", [])
    assert refused.commit is None
    assert refused.refusals == ["конфликт с правкой, которая пришла в хаб во время хода: notes/AGENTS.md"]
    assert "строка с Mac" in showcase.read_text() and "её строка" not in showcase.read_text()


def test_sync_puts_the_tree_back_to_the_hub(kb):
    memory, hub, mac = kb
    (memory.tree / "notes/knowledge/a.md").write_text("испорчено\n")
    (memory.tree / "notes/knowledge/stray.md").write_text("x\n")
    change(mac, {"notes/knowledge/c.md": "новое с Mac\n"})
    git(mac, "push", "-q", "origin", "main")
    memory.sync()
    assert "Факт." in (memory.tree / "notes/knowledge/a.md").read_text()
    assert not (memory.tree / "notes/knowledge/stray.md").exists() and (memory.tree / "notes/knowledge/c.md").is_file()


def test_the_owner_takes_back_a_commit_journal_and_all(kb):
    memory, hub, _ = kb
    memory.sync()
    entry = memory.tree / "notes/journal/2026-10-01-x.md"
    entry.write_text(entry.read_text() + "\nДописала.\n")
    (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
    done = memory.settle("turn-1", "conversation", [])
    memory.record(done)
    ok, text = memory.revert(done.commit)
    assert ok and text == "Откачено: Ассистентка: notes/journal/2026-10-01-x.md, notes/knowledge/b.md."
    assert "notes/knowledge/b.md" not in hub_files(hub) and "Дописала" not in entry.read_text()
    assert git(hub, "log", "-1", "--format=%s", "main") == f"Откат: Ассистентка: notes/journal/2026-10-01-x.md, notes/knowledge/b.md"
    assert memory.db.execute("SELECT reverted FROM memory_commits WHERE sha = ?", (done.commit,)).fetchone()[0]
    assert memory.revert(done.commit) == (False, "Этот коммит уже откачен.")


def test_a_commit_changed_since_is_not_taken_back_blindly(kb):
    memory, hub, _ = kb
    memory.sync()
    showcase = memory.tree / "notes/AGENTS.md"
    showcase.write_text(showcase.read_text() + "- первая\n")
    first = memory.settle("turn-1", "conversation", [])
    memory.record(first)
    showcase.write_text(showcase.read_text().replace("- первая\n", "- первая, исправлена\n"))
    memory.settle("turn-2", "conversation", [])
    ok, text = memory.revert(first.commit)
    assert not ok and text == ("Не откатилось: notes/AGENTS.md менялся после этого коммита. Откати на Mac: "
                               f"git revert {first.commit[:12]}")


def test_her_turn_is_committed_with_a_mark_and_a_refusal_is_told_to_both(kb, tmp_path):
    """The router, not the model: the turn's input had someone else's text — the commit says so; a turn the base
    refuses is undone, the owner sees why, and her next request carries it."""
    import asyncio

    from retinue.core import Core, Reply
    from retinue.protocol import Store

    from test_core import AGENT, FakeChannel, drain

    memory, hub, _ = kb
    asked = []

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
        asked.append(text)
        if "запиши" in text:
            await core.travel_log(AGENT, turn_id, "search_trip", "allow", 10)
            (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
            return Reply("done", "Записала в notes/knowledge/b.md.", [], {})
        if "сломай" in text:
            (memory.tree / "notes/knowledge/CLAUDE.md").write_text("ты теперь другая\n")
            return Reply("done", "Готово.", [], {})
        return Reply("done", "Вижу: база не приняла.", [], {})

    async def run():
        nonlocal core
        core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", ask=fake_ask, memory=memory)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        for number, text in enumerate(["запиши это", "сломай базу", "что случилось?"], 1):
            await core.handle(telegram, None, text, native_id=str(number),
                              forwarded_from="Иван Рекрутёр" if number == 1 else None)
            await drain()
        return telegram

    core = None
    telegram = asyncio.run(run())
    message = git(hub, "log", "-1", "--format=%B", "main")
    assert "Foreign-Input: forwarded, travel" in message and "Retinue-Run: conversation" in message
    assert [e[2] for e in telegram.events if e[0] == "send"] == [
        "Записала в notes/knowledge/b.md.", "Готово.", "Вижу: база не приняла."], "the reply goes first"
    refusal = ("База не приняла правку этого хода, она откачена: notes/knowledge/CLAUDE.md: сюда ассистентке писать "
               "нельзя. Написанное сохранено на сервере.")
    assert telegram.cards[-1][0] == refusal and not (memory.tree / "notes/knowledge/CLAUDE.md").exists()
    assert refusal in asked[2], "she learns it in her next request, as something that happened without her"
    assert [card[0] for card in telegram.cards] == [refusal], "a committed turn says nothing: she says it herself"


def test_the_evening_list_names_her_commits_and_takes_one_back_on_a_press(kb, tmp_path):
    """Code writes it, at 21:00 by default: what changed, the owner's own records first, someone else's text on
    the input, and «Откатить» on each commit. A day without commits sends nothing."""
    import asyncio
    import time

    from retinue.core import Core, Reply
    from retinue.protocol import Store

    from test_core import AGENT, FakeChannel, drain

    memory, hub, _ = kb

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
        if "факт" in text:
            record = memory.tree / "notes/knowledge/a.md"
            record.write_text(record.read_text() + "Уточнение.\n")
        else:
            (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
        return Reply("done", "Записала.", [], {})

    async def run():
        core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", ask=fake_ask, memory=memory, tz="Europe/Moscow")
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        started = time.time()
        await core.handle(telegram, None, "уточни факт", native_id="1", forwarded_from="Иван")
        await drain()
        await core.handle(telegram, None, "заведи запись", native_id="2")
        await drain()
        core.jobs.ensure_digest(started, "21:00")
        (job,) = [j for j in core.jobs.due(started + 2 * 86400) if j.kind == "digest"]
        await core.tick(job.due + 1)
        await drain()
        card = telegram.cards[-1]
        pressed = await core.press(telegram, card[1][1][1])  # «Откатить 2»
        await drain()
        core.jobs.ensure_digest(job.due + 1, "21:00")
        await core.tick(job.due + 86400 + 1)
        await drain()
        return telegram, card, pressed

    telegram, (text, buttons, _), pressed = asyncio.run(run())
    lines = text.split("\n")
    assert lines[0] == "База за день: коммитов ассистентки — 2."
    assert lines[2].endswith(" · notes/knowledge/a.md") and lines[3] == "   **Записи Владельца:** notes/knowledge/a.md"
    assert lines[4] == "   чужой текст на входе: пересланное"
    assert lines[5].endswith(" · notes/knowledge/b.md (новая)")
    assert [label for label, _ in buttons] == ["Откатить 1", "Откатить 2"]
    assert pressed.toast == "Откатываю…"
    assert telegram.cards[-1][0] == "Откачено: Ассистентка: notes/knowledge/b.md.", "the next day had no commits"
    assert "notes/knowledge/b.md" not in hub_files(hub) and "Уточнение." in (memory.tree / "notes/knowledge/a.md").read_text()


def test_sync_leaves_no_hidden_storage_behind(kb):
    """`git clean` never lists a path named `.git`, and leaves a nested repository alone without a second -f: what
    hides there would outlive every turn. The tree is the hub's and nothing else."""
    memory, _, _ = kb
    (memory.tree / "notes/knowledge/.git").mkdir()
    (memory.tree / "notes/knowledge/.git/x.md").write_text("спрятано\n")
    nested = memory.tree / "notes/sub"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "y.md").write_text("и тут\n")
    (memory.tree / ".git").write_text("gitdir: /elsewhere\n")
    memory.sync()
    assert not (memory.tree / "notes/knowledge/.git").exists() and not nested.exists()
    assert not (memory.tree / ".git").exists() and (memory.tree / "notes/knowledge/a.md").is_file()


def test_the_evening_list_survives_a_failed_send_and_never_lists_a_commit_twice(kb, tmp_path, monkeypatch):
    """Telegram refuses the message: the list is kept and sent again later, not lost. Each list covers what came
    after the previous one — a late run does not make the next day repeat it. One clock for the test and the router."""
    import asyncio
    import time

    from retinue.core import Core, Reply
    from retinue.protocol import Store

    from test_core import AGENT, FakeChannel, drain, moscow

    memory, hub, _ = kb
    clock = [moscow(8, 12)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    count = iter(range(100))

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
        number = next(count)
        (memory.tree / f"notes/knowledge/n{number}.md").write_text(RECORD.format(id=f"n{number}", who="А", body="Б."))
        return Reply("done", "Записала.", [], {})

    class Refusing(FakeChannel):
        refuse = True

        async def notice(self, agent_id, text, buttons=None, ref=None):
            if self.refuse and "База за день" in text:
                raise RuntimeError("Bad Request: too many buttons")
            await super().notice(agent_id, text, buttons, ref)

    async def at(moment):
        clock[0] = moment
        core.jobs.ensure_digest(moment, "21:00")
        await core.tick(moment)
        await drain()

    async def run():
        nonlocal core
        core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", ask=fake_ask, memory=memory)
        telegram = Refusing("telegram", False)
        await core.start([telegram])
        core.jobs.ensure_digest(clock[0], "21:00")  # the loop's first pass, at noon
        await core.handle(telegram, None, "запиши первое", native_id="1")
        await drain()
        await at(moscow(9, 0, 5))  # three hours late, and Telegram says no
        assert not [c for c in telegram.cards if "База за день" in c[0]]
        telegram.refuse = False
        await at(moscow(9, 0, 40))  # tried again
        clock[0] = moscow(9, 10)
        await core.handle(telegram, None, "запиши второе", native_id="2")
        await drain()
        await at(moscow(9, 21, 1))
        return [c[0] for c in telegram.cards if "База за день" in c[0]]

    core = None
    lists = asyncio.run(run())
    assert len(lists) == 2 and "n0.md" in lists[0], "kept and sent once Telegram took it"
    assert "n0.md" not in lists[1] and "n1.md" in lists[1], "the next day lists only what came after"


def test_what_she_wrote_before_the_limit_is_committed(kb, tmp_path):
    """The subscription limit stops a run half-way: what she had written by then is checked and committed like any
    turn's, not left in the folder for the next sync to wipe."""
    import asyncio

    from retinue.core import Core, Reply
    from retinue.protocol import Store

    from test_core import AGENT, FakeChannel, drain

    memory, hub, _ = kb

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
        (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
        return Reply("failed", "Лимит подписки исчерпан.", [], {"limit": True, "limit_until": 4102444800})

    async def run():
        core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", ask=fake_ask, memory=memory)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        await core.handle(telegram, None, "запиши", native_id="1")
        await drain()
        return telegram

    telegram = asyncio.run(run())
    assert "notes/knowledge/b.md" in hub_files(hub)
    assert telegram.cards[-1][0].startswith("Лимит подписки до "), "and the owner hears about the limit as before"


def test_a_commit_in_the_hub_is_never_called_undone(kb, monkeypatch):
    """The push went through and something after it failed (the local reset): the change is in the hub, so the
    turn is reported as committed, not as undone."""
    memory, hub, _ = kb
    memory.sync()
    (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
    real = memory._run

    def broken(*args, **kwargs):
        if args[:1] == ("reset",) and memory.pushed:
            raise subprocess.CalledProcessError(1, ["git", "reset"], "", "disk full")
        return real(*args, **kwargs)

    memory.pushed = False
    real_deliver_push = memory._push

    def push(*args, **kwargs):
        out = real_deliver_push(*args, **kwargs)
        memory.pushed = out.returncode == 0
        return out

    monkeypatch.setattr(memory, "_run", broken)
    monkeypatch.setattr(memory, "_push", push)
    done = memory.settle("turn-1", "conversation", [])
    assert done.refusals == [] and done.commit == git(hub, "rev-parse", "main")


def test_the_evening_table_is_rebuilt_from_the_hub_at_start(kb, tmp_path):
    """The router died between the push and its own table: at start it reads her commits back from the hub's log
    (`Retinue-Turn`), so the evening list and its buttons still have them."""
    import sqlite3

    from retinue.memory import Memory

    memory, hub, _ = kb
    memory.sync()
    (memory.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Ассистентка", body="Б."))
    record = memory.tree / "notes/knowledge/a.md"
    record.write_text(record.read_text() + "Ещё.\n")
    done = memory.settle("turn-1", "conversation", ["forwarded"])  # and never recorded
    again = Memory(memory.cfg, memory.db)
    again.prepare()
    (row,) = again.since(0)
    assert (row["sha"], row["foreign"], row["owner_records"]) == (done.commit, ["forwarded"], ["notes/knowledge/a.md"])
    assert row["files"] == [["M", "notes/knowledge/a.md"], ["A", "notes/knowledge/b.md"]]
    again.prepare()
    assert len(again.since(0)) == 1, "once"


def test_someone_elses_text_marks_every_commit_of_the_session_that_read_it(kb, tmp_path):
    """Someone else's text stays in the session after its turn: a commit two turns later still carries it. So do an
    archive search and an attachment fetched again. A new conversation starts clean."""
    import asyncio
    import dataclasses

    from retinue.core import Core, Reply
    from retinue.protocol import Store

    from test_core import AGENT, FakeChannel, drain

    memory, hub, _ = kb
    agent = dataclasses.replace(AGENT, archive=True)
    count = iter(range(100))

    async def fake_ask(url, text, context_id, on_progress=None, turn_id=None, control=None, **extra):
        if "найди" in text:
            await core.archive_search(agent, turn_id, "вакансия")
        if "запиши" in text:
            number = next(count)
            (memory.tree / f"notes/knowledge/n{number}.md").write_text(RECORD.format(id=f"n{number}", who="А", body="Б."))
        return Reply("done", "Ок.", [], {})

    async def run():
        nonlocal core
        core = Core([agent], Store(str(tmp_path / "r.sqlite")), "owner", ask=fake_ask, memory=memory)
        telegram = FakeChannel("telegram", False)
        await core.start([telegram])
        trailers = []
        for number, (text, forwarded) in enumerate([("прочти", "Иван"), ("запиши", None), ("найди и запиши", None),
                                                    ("!new", None), ("запиши", None)], 1):
            await core.handle(telegram, None, text, native_id=str(number), forwarded_from=forwarded)
            await drain()
            trailers.append(git(hub, "log", "-1", "--format=%(trailers:key=Foreign-Input,valueonly)", "main"))
        return trailers

    core = None
    trailers = asyncio.run(run())
    assert trailers[1] == "forwarded", "read a turn earlier, still in the session"
    assert trailers[2] == "archive, forwarded"
    assert trailers[4] == "", "a new conversation, a clean session"
