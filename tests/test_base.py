"""The Base in a lane: a clone the platform keeps fresh and commits by the owner's button. Real git in a temporary
folder; the hub runs the same checks as its pre-receive hook, and this test's pushes are the owner's (writer_uid -1),
as the platform's are on the server."""

import asyncio
import os
import stat
import subprocess
import sys

import pytest

from retinue import base as base_module
from retinue.base import BaseRepo, Change, Unavailable
from retinue.config import BaseConfig

from test_kbcheck import LINT, MARKER, RECORD, base, change, git, install_hub  # noqa: F401  (base: a fixture)

OWNER = "Иван <owner@example.com>"


@pytest.fixture
def kb(tmp_path, base):
    """The hub, the lane's /work with an empty clone folder, the platform's repository, the owner's Mac."""
    hub = install_hub(tmp_path, base)
    work = tmp_path / "work"
    (work / "base").mkdir(parents=True)
    cfg = BaseConfig(author=OWNER, hub=str(hub), mount=str(work / "base"), git_dir=str(tmp_path / "platform" / "base.git"))
    repo = BaseRepo(cfg, lane_view=str(work / "base"))
    asyncio.run(repo.refresh())
    mac = tmp_path / "mac"
    git(tmp_path, "clone", "-q", str(hub), str(mac))
    return repo, hub, mac


def run(coro):
    return asyncio.run(coro)


def mac_push(mac, edits, message="с Mac"):
    change(mac, edits, message)
    git(mac, "push", "-q", "origin", "main")


def showcase(*lines: str) -> str:
    return f"# Заметки\n\n## Правила домена\n\n- правило\n\n{MARKER}\n\n## Записи\n\n- [a](knowledge/a.md)\n" + \
        "".join(f"{line}\n" for line in lines)


# --- the clone and refresh ---------------------------------------------------------------------------------------

def test_the_first_refresh_clones_the_hub_and_git_never_enters_the_tree(kb):
    repo, hub, _ = kb
    tree = repo.tree
    assert (tree / "notes/knowledge/a.md").read_text().endswith("Факт.\n") and (tree / "scripts/lint.py").is_file()
    assert not (tree / ".git").exists(), "the repository is the platform's: no hook or config of it in the lane"
    assert stat.S_IMODE((tree / "MAP.md").stat().st_mode) == 0o664, "the lane edits what the platform wrote"
    folder = stat.S_IMODE((tree / "notes").stat().st_mode)
    assert folder & 0o070 == 0o070 and (folder & 0o2000 or sys.platform != "linux"), "setgid, group-writable folders"
    assert repo.changed() == [] and repo._head == git(hub, "rev-parse", "main")
    assert "notes/knowledge/a.md" in repo.snapshot()


def test_a_clean_file_takes_the_hubs_and_the_cheap_look_changes_nothing(kb):
    repo, hub, mac = kb
    assert run(repo.refresh()).updated == [], "the hub did not move: nothing fetched, nothing written"
    mac_push(mac, {"notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="Факт с Mac."),
                   "notes/journal/2026-10-01-x.md": None})
    done = run(repo.refresh())
    assert sorted(done.updated) == ["notes/journal/2026-10-01-x.md", "notes/knowledge/a.md"] and done.held == []
    assert "Факт с Mac." in (repo.tree / "notes/knowledge/a.md").read_text()
    assert not (repo.tree / "notes/journal").exists(), "a folder the hub emptied goes, as git would do"
    assert done.head == git(hub, "rev-parse", "main") and repo.changed() == []


def test_a_dirty_file_is_merged_with_the_hubs_change(kb):
    repo, _, mac = kb
    (repo.tree / "notes/AGENTS.md").write_text(showcase("- строка сессии"))
    text = (mac / "notes/AGENTS.md").read_text().replace("- правило\n", "- правило\n- правило с Mac\n")
    mac_push(mac, {"notes/AGENTS.md": text})
    done = run(repo.refresh())
    merged = (repo.tree / "notes/AGENTS.md").read_text()
    assert done.updated == ["notes/AGENTS.md"] and done.held == []
    assert "- правило с Mac" in merged and "- строка сессии" in merged
    assert repo.changed() == [Change("notes/AGENTS.md", "M")], "the session's line is still its own, uncommitted"


def test_a_conflicting_file_is_held_and_never_stalls_the_rest(kb):
    repo, _, mac = kb
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Факт сессии."))
    mac_push(mac, {"notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="Факт с Mac."),
                   "MAP.md": (mac / "MAP.md").read_text() + "- строка с Mac\n"})
    done = run(repo.refresh())
    assert done.updated == ["MAP.md"] and done.held == ["notes/knowledge/a.md"]
    assert "Факт сессии." in (repo.tree / "notes/knowledge/a.md").read_text(), "never overwritten"
    assert "строка с Mac" in (repo.tree / "MAP.md").read_text()
    # The commit merges against the base the session's edit started from: the hub's change is never undone silently.
    done = run(repo.commit(["notes/knowledge/a.md"], "notes: факт", ""))
    assert done.sha is None and done.conflicts == ["notes/knowledge/a.md"]
    assert "Факт с Mac." in git(mac, "show", "origin/main:notes/knowledge/a.md")
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Факт с Mac."))
    run(repo.refresh())
    assert repo.changed() == [] and run(repo.refresh()).held == [], "put back to the hub's: no longer held"


def test_the_hub_ahead_by_the_routers_push_comes_in_like_the_macs(kb, tmp_path):
    repo, hub, _ = kb
    router = tmp_path / "router"
    git(tmp_path, "clone", "-q", str(hub), str(router))
    change(router, {"notes/knowledge/r.md": RECORD.format(id="r", who="Ассистентка", body="Её.")}, "Ассистентка: r")
    git(router, "push", "-q", "origin", "main")
    assert run(repo.refresh()).updated == ["notes/knowledge/r.md"] and (repo.tree / "notes/knowledge/r.md").is_file()


def test_a_link_on_the_way_is_never_written_through(kb, tmp_path):
    repo, _, mac = kb
    outside = tmp_path / "outside"
    outside.mkdir()
    knowledge = repo.tree / "notes/knowledge"
    for child in knowledge.iterdir():
        child.unlink()
    knowledge.rmdir()
    knowledge.symlink_to(outside, target_is_directory=True)
    mac_push(mac, {"notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="через ссылку"), "MAP.md": (mac / "MAP.md").read_text() + "- x\n"})
    done = run(repo.refresh())
    assert list(outside.iterdir()) == [], "nothing written where the link points"
    assert "notes/knowledge/a.md" in done.held and "MAP.md" in done.updated
    assert Change("notes/knowledge", "A") in repo.changed(), "the link itself is listed, never followed"


# --- commit ------------------------------------------------------------------------------------------------------

def test_a_commit_carries_the_chosen_files_only_and_leaves_the_rest_as_they_are(kb):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Новое."))
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Claude Code (Opus 5.5)", body="Б."))
    (repo.tree / "notes/knowledge/other.md").write_text(RECORD.format(id="o", who="x", body="другая сессия"))
    (repo.tree / "notes/journal/2026-10-01-x.md").unlink()
    done = run(repo.commit(["notes/knowledge/a.md", "notes/knowledge/b.md", "notes/journal/2026-10-01-x.md"],
                           "notes: факт и запись", "Что поменялось.", session="ab12cd34"))
    assert done.sha == git(hub, "rev-parse", "main") and done.conflicts == done.refused == done.left_out == []
    assert sorted((c.status, c.path) for c in done.files) == [
        ("A", "notes/knowledge/b.md"), ("D", "notes/journal/2026-10-01-x.md"), ("M", "notes/knowledge/a.md")]
    message = git(hub, "log", "-1", "--format=%an <%ae>|%cn|%B", "main")
    assert message.startswith(f"{OWNER}|Иван|notes: факт и запись\n\nЧто поменялось.")
    assert "Pushed-by: retinue-platform (кнопка Владельца, сессия ab12cd34)" in message
    assert repo.changed() == [Change("notes/knowledge/other.md", "A")], "another session's file stays uncommitted"
    assert "notes/knowledge/other.md" not in git(hub, "ls-tree", "-r", "--name-only", "main")
    assert repo.diff(["notes/knowledge/other.md"], 10_000).startswith("--- /dev/null\n+++ b/notes/knowledge/other.md")


def test_a_hub_change_since_the_clone_is_merged_in_and_brought_to_the_tree(kb):
    repo, hub, mac = kb
    (repo.tree / "notes/AGENTS.md").write_text(showcase("- строка сессии"))
    mac_push(mac, {"notes/AGENTS.md": (mac / "notes/AGENTS.md").read_text().replace("- правило\n", "- правило\n- с Mac\n"),
                   "MAP.md": (mac / "MAP.md").read_text() + "- с Mac\n"})
    done = run(repo.commit(["notes/AGENTS.md"], "notes: строка", ""))
    assert done.sha and done.files == [Change("notes/AGENTS.md", "M")]
    committed = git(hub, "show", "main:notes/AGENTS.md")
    assert "- с Mac" in committed and "- строка сессии" in committed and git(hub, "log", "-1", "--format=%s",
                                                                              "main^") == "с Mac"
    assert (repo.tree / "notes/AGENTS.md").read_text() == committed + "\n" and "- с Mac" in (repo.tree / "MAP.md").read_text()
    assert repo.changed() == [], "the clone follows the pushed commit"


def test_the_hub_moving_during_the_push_is_retried_on_top(kb, monkeypatch):
    repo, hub, mac = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    real, pushes = repo._run, []

    def racing(*args, **kw):
        if args[:1] == ("push",) and not pushes:
            mac_push(mac, {"notes/knowledge/m.md": RECORD.format(id="m", who="Владелец", body="М.")}, "гонка")
        if args[:1] == ("push",):
            pushes.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(repo, "_run", racing)
    done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert len(pushes) == 2 and done.sha == git(hub, "rev-parse", "main") and done.refused == []
    assert git(hub, "log", "-2", "--format=%s", "main").split("\n") == ["notes: б", "гонка"]
    assert (repo.tree / "notes/knowledge/m.md").is_file() and repo.changed() == []


def test_a_conflict_is_refused_for_that_path_and_the_rest_goes(kb):
    repo, hub, mac = kb
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Сессия."))
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    mac_push(mac, {"notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="Mac.")})
    done = run(repo.commit(["notes/knowledge/a.md", "notes/knowledge/b.md"], "notes: два", ""))
    assert done.conflicts == ["notes/knowledge/a.md"] and done.files == [Change("notes/knowledge/b.md", "A")]
    assert "Сессия." in (repo.tree / "notes/knowledge/a.md").read_text(), "the conflicted file stays as it was"
    assert repo.changed() == [Change("notes/knowledge/a.md", "M")]
    assert "Mac." in git(hub, "show", "main:notes/knowledge/a.md")


def test_what_is_not_the_bases_is_left_out_and_never_committed(kb):
    repo, hub, _ = kb
    for name in ("notes/out/отчёт.md", "notes/inbox/2026-10-27-фото.jpg", ".scratch/x.md", "notes/knowledge/b.md"):
        (repo.tree / name).parent.mkdir(parents=True, exist_ok=True)
        (repo.tree / name).write_text(RECORD.format(id="b", who="x", body="Б."))
    assert repo.changed() == [Change("notes/knowledge/b.md", "A")], "out/, inbox/ and the policy's excluded: unlisted"
    assert set(repo.snapshot()) >= {"notes/knowledge/b.md"} and not any(
        p.startswith((".scratch", "notes/out", "notes/inbox")) for p in repo.snapshot())
    done = run(repo.commit(["notes/out/отчёт.md", ".scratch/x.md", "../etc/passwd", "/etc/passwd", "notes/knowledge/b.md"],
                           "notes: б", ""))
    assert done.files == [Change("notes/knowledge/b.md", "A")]
    assert [line.split(":")[0] for line in done.left_out] == ["notes/out/отчёт.md", ".scratch/x.md", "../etc/passwd",
                                                              "/etc/passwd"]
    assert ".scratch/x.md" not in git(hub, "ls-tree", "-r", "--name-only", "main")


def test_links_fifos_and_folders_are_refused_and_never_block(kb):
    repo, hub, _ = kb
    knowledge = repo.tree / "notes/knowledge"
    (knowledge / "link.md").symlink_to("/etc/passwd")
    os.mkfifo(knowledge / "fifo.md")
    (knowledge / "dir.md").mkdir()
    (knowledge / "dir.md" / "x.md").write_text("x\n")
    listed = {c.path: c.status for c in repo.changed()}
    assert listed["notes/knowledge/link.md"] == "A" and listed["notes/knowledge/fifo.md"] == "A"
    assert repo.snapshot()["notes/knowledge/fifo.md"].startswith("other:")
    done = run(repo.commit(["notes/knowledge/link.md", "notes/knowledge/fifo.md", "notes/knowledge/dir.md"], "x", ""))
    assert done.sha is None and done.refused == [
        "notes/knowledge/link.md: симлинк — в Базе только обычные файлы",
        "notes/knowledge/fifo.md: не обычный файл (FIFO, сокет, устройство)",
        "notes/knowledge/dir.md: папка, а не файл"]
    assert repo.diff(["notes/knowledge/link.md", "notes/knowledge/fifo.md"], 1000) == (
        "diff notes/knowledge/link.md\nсимлинк: не показывается\n"
        "diff notes/knowledge/fifo.md\nне обычный файл: не показывается\n")


def test_hooks_config_and_attributes_in_the_tree_never_run_and_code_never_goes(kb, tmp_path):
    repo, hub, _ = kb
    marker = tmp_path / "ran"
    evil = f"#!/bin/sh\ntouch {marker}\n"
    git_dir = repo.tree / ".git"
    (git_dir / "hooks").mkdir(parents=True)
    for hook in ("pre-commit", "post-commit", "pre-push", "reference-transaction"):
        (git_dir / "hooks" / hook).write_text(evil)
        (git_dir / "hooks" / hook).chmod(0o755)
    (git_dir / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = {git_dir / 'hooks'}\n"
                                    f"[filter \"x\"]\n\tclean = touch {marker}\n\tsmudge = touch {marker}\n")
    (repo.tree / ".gitattributes").write_text("* filter=x diff=x merge=x\n")
    (repo.tree / ".gitmodules").write_text("[submodule \"x\"]\n\tpath = x\n\turl = ext::sh -c touch% /tmp/x\n")
    (repo.tree / "scripts/lint.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    (repo.tree / "notes/knowledge/tool.py").write_text("print('x')\n")
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    assert ".git/config" not in repo.snapshot(), "a .git anywhere is never the Base's"
    done = run(repo.commit([".gitattributes", ".gitmodules", ".git/config", "scripts/lint.py", "notes/knowledge/tool.py",
                            "notes/knowledge/b.md"], "notes: б", ""))
    assert done.files == [Change("notes/knowledge/b.md", "A")]
    assert done.refused == [".gitattributes: скрытый путь — правится только с Mac",
                            ".gitmodules: скрытый путь — правится только с Mac",
                            ".git/config: служебное имя git — в Базу не идёт",
                            "scripts/lint.py: этот файл исполняется при проверке Базы — правится только с Mac",
                            "notes/knowledge/tool.py: этот файл исполняется при проверке Базы — правится только с Mac"]
    run(repo.refresh())
    repo.diff(["notes/knowledge/b.md", ".gitattributes"], 10_000)
    assert not marker.exists(), "nothing of the tree ran: not git's hooks, config, filters, nor the tree's lint"
    assert git(hub, "show", "main:scripts/lint.py") == LINT.strip()


def test_the_hubs_refusal_comes_back_with_its_reasons(kb):
    repo, hub, _ = kb
    before = git(hub, "rev-parse", "main")
    (repo.tree / "notes/knowledge/b.md").write_text("BROKEN\n")
    done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert done.sha is None and done.refused == ["just check: ОШИБКА: notes/knowledge/b.md: сломано"]
    assert git(hub, "rev-parse", "main") == before and repo.changed() == [Change("notes/knowledge/b.md", "A")]


def test_a_path_the_owners_checks_refuse_is_dropped_and_the_rest_goes(kb):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a-renamed", who="Владелец", body="Факт."))
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    done = run(repo.commit(["notes/knowledge/a.md", "notes/knowledge/b.md"], "notes: б", ""))
    assert done.files == [Change("notes/knowledge/b.md", "A")] and len(done.refused) == 1
    assert done.refused[0].startswith("notes/knowledge/a.md: id изменён: a → a-renamed")


def test_nothing_new_commits_nothing(kb):
    repo, hub, _ = kb
    before = git(hub, "rev-parse", "main")
    done = run(repo.commit(["notes/knowledge/a.md"], "notes: ничего", ""))
    assert done.sha is None and done.left_out == ["notes/knowledge/a.md: уже так в Базе"]
    assert run(repo.commit(["notes/knowledge/a.md"], "  ", "")).refused == ["коммит: пустая тема"]
    assert git(hub, "rev-parse", "main") == before


def test_the_diff_is_bounded(kb):
    repo, _, _ = kb
    (repo.tree / "notes/knowledge/a.md").write_text("строка\n" * 5000)
    text = repo.diff(["notes/knowledge/a.md"], 500)
    assert text.startswith("--- a/notes/knowledge/a.md\n+++ b/notes/knowledge/a.md\n") and len(text) < 560
    assert text.endswith("… дифф обрезан\n")


def test_a_clone_folder_the_lane_moved_away_is_refused(kb, tmp_path):
    repo, _, _ = kb
    other = tmp_path / "other"
    other.mkdir()
    repo.lane_view = str(other)
    with pytest.raises(Unavailable, match="restart the platform"):
        run(repo.refresh())
    with pytest.raises(OSError):
        repo.changed()


def test_a_platform_restart_keeps_the_held_bases(kb):
    repo, _, mac = kb
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Сессия."))
    mac_push(mac, {"notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="Mac.")})
    assert run(repo.refresh()).held == ["notes/knowledge/a.md"]
    again = BaseRepo(repo.cfg, repo.lane_view)
    assert run(again.refresh()).held == ["notes/knowledge/a.md"]
    assert run(again.commit(["notes/knowledge/a.md"], "x", "")).conflicts == ["notes/knowledge/a.md"]
    journal = again.db.execute("SELECT kind FROM journal ORDER BY id").fetchall()
    assert [row[0] for row in journal][-2:] == ["refresh", "commit"]


def test_the_git_environment_carries_none_of_the_platforms_secrets(kb, monkeypatch):
    repo, _, _ = kb
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    fresh = BaseRepo(repo.cfg, repo.lane_view)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in fresh.env and fresh.env["TMPDIR"].startswith(repo.cfg.git_dir)
    assert "core.hooksPath=/dev/null" in fresh.repo and not any("--work-tree" in a for a in fresh.repo)


def test_under_the_clone():
    assert base_module.under("base", "base") and base_module.under("base/travel", "base")
    assert not base_module.under("based", "base") and not base_module.under(None, "base")
    assert not base_module.under("", "base")


def test_a_path_inside_the_base():
    assert base_module.clean_path("notes/a.md") == "notes/a.md"
    for bad in ("", "/a", "a//b", "a/../b", "./a", "a\nb", "a\x00b", 5):
        assert base_module.clean_path(bad) is None


def test_git_is_never_given_the_tree(kb, monkeypatch):
    repo, _, _ = kb
    seen = []
    real = subprocess.run

    def spy(argv, *args, **kw):
        seen.append(argv)
        return real(argv, *args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    gits = [argv for argv in seen if argv and argv[0] == "git"]
    assert gits and not any(str(repo.tree) in " ".join(map(str, argv)) for argv in gits)


def test_a_push_the_hub_cannot_take_is_said_once_not_retried_as_a_race(kb):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    folders = [hub / "objects", *[p for p in (hub / "objects").rglob("*") if p.is_dir()]]
    for folder in folders:
        folder.chmod(0o555)
    try:
        done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    finally:
        for folder in folders:
            folder.chmod(0o755)
    assert done.sha is None and len(done.refused) == 1 and done.refused[0].startswith("хаб не принял push: ")
    assert repo.changed() == [Change("notes/knowledge/b.md", "A")], "nothing lost: the file waits for the next press"


def test_what_the_hubs_check_runs_is_refused_even_without_protect(kb):
    repo, _, _ = kb
    repo.cfg.protect = []
    (repo.tree / "scripts/lint.py").write_text("print('x')\n")
    done = run(repo.commit(["scripts/lint.py"], "x", ""))
    assert done.refused == ["scripts/lint.py: этот файл исполняется при проверке Базы — правится только с Mac"]


def test_a_file_changed_after_the_card_is_refused_and_the_rest_goes(kb):
    repo, hub, _ = kb
    a, b = repo.tree / "notes/knowledge/a.md", repo.tree / "notes/knowledge/b.md"
    a.write_text(RECORD.format(id="a", who="Владелец", body="Как на карточке."))
    b.write_text(RECORD.format(id="b", who="x", body="Б."))
    (repo.tree / "notes/knowledge/l.md").symlink_to("/etc/hosts")
    seen = repo.snapshot()
    a.write_text(RECORD.format(id="a", who="Владелец", body="Дописано после."))
    expect = {p: seen[p] for p in ("notes/knowledge/a.md", "notes/knowledge/b.md", "notes/knowledge/l.md")}
    done = run(repo.commit(["notes/knowledge/a.md", "notes/knowledge/b.md", "notes/journal/2026-10-01-x.md"],
                           "notes: б", "Retinue: сессия s1, карточка c1.", expect={**expect, "notes/journal/2026-10-01-x.md": ""}))
    assert done.files == [Change("notes/knowledge/b.md", "A")]
    assert done.refused == ["notes/knowledge/a.md: файл изменился после карточки — открой её заново",
                            "notes/journal/2026-10-01-x.md: файл изменился после карточки — открой её заново"]
    assert repo._digest_now("notes/knowledge/l.md", repo._read("notes/knowledge/l.md")) == seen["notes/knowledge/l.md"]
    assert "Retinue: сессия s1, карточка c1.\n\nPushed-by: retinue-platform (кнопка Владельца)" in git(
        hub, "log", "-1", "--format=%B", "main")


def test_a_file_never_becomes_a_folder_nor_a_folder_a_file(kb):
    repo, hub, mac = kb
    mac_push(mac, {"justfile": "check:\n\t@python3 scripts/lint.py\n"})
    run(repo.refresh())
    for name in ("justfile", "MAP.md"):
        (repo.tree / name).unlink()
        (repo.tree / name).mkdir()
        (repo.tree / name / "x.md").write_text("hi\n")
    journal = repo.tree / "notes/journal"
    for child in journal.iterdir():
        child.unlink()
    journal.rmdir()
    journal.write_text("теперь файл\n")
    done = run(repo.commit(["justfile", "justfile/x.md", "MAP.md", "MAP.md/x.md", "notes/journal"], "x", ""))
    assert done.sha is None and done.files == []
    assert done.refused == [
        "justfile: этот файл исполняется при проверке Базы — правится только с Mac",
        "MAP.md: папка, а не файл",
        "justfile/x.md: на месте папки justfile в Базе файл — в Базу не идёт",
        "MAP.md/x.md: на месте папки MAP.md в Базе файл — в Базу не идёт",
        "notes/journal: на этом месте в Базе папка — в Базу не идёт"]
    files = git(hub, "ls-tree", "-r", "--name-only", "main").split("\n")
    assert "justfile" in files and "MAP.md" in files and "notes/journal/2026-10-01-x.md" in files


def test_a_commit_that_would_touch_an_unchosen_path_is_not_built(kb, monkeypatch):
    repo, hub, _ = kb
    before = git(hub, "rev-parse", "main")
    (repo.tree / "MAP.md").unlink()
    (repo.tree / "MAP.md").mkdir()
    (repo.tree / "MAP.md" / "x.md").write_text("hi\n")
    monkeypatch.setattr(repo, "_listing", lambda rev: ({}, set()))  # the shape check fooled: the last net holds
    done = run(repo.commit(["MAP.md/x.md"], "x", ""))
    assert done.sha is None and done.refused == ["коммит задел бы и другие файлы Базы: MAP.md — не собран"]
    assert git(hub, "rev-parse", "main") == before


def test_while_a_run_can_write_the_tree_nothing_but_a_commits_own_paths_is_written(kb):
    repo, hub, mac = kb
    busy = [True]
    repo.busy = lambda: busy[0]
    mac_push(mac, {"MAP.md": (mac / "MAP.md").read_text() + "- с Mac\n"})
    assert run(repo.refresh()).updated == [] and "с Mac" not in (repo.tree / "MAP.md").read_text(), \
        "a refresh beside a run does nothing at all"
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert done.sha == git(hub, "rev-parse", "main") and repo._head == done.sha
    assert "с Mac" not in (repo.tree / "MAP.md").read_text(), "the hub's other change waits for an idle refresh"
    assert repo.changed() == [], "a file only waiting for the hub's version is nobody's change"
    assert run(repo.commit(["MAP.md"], "x", "")).left_out == ["MAP.md: здесь не менялся"], "never undoes the hub's"
    busy[0] = False
    assert run(repo.refresh()).updated == ["MAP.md"] and "с Mac" in (repo.tree / "MAP.md").read_text()
    assert repo._held() == {} and repo.changed() == []


def test_a_refresh_that_need_not_wait_does_not(kb):
    repo, _, _ = kb
    repo._lock.acquire()
    try:
        assert run(repo.refresh(wait=False)) is None
    finally:
        repo._lock.release()
    assert run(repo.refresh(wait=False)) is not None


def test_a_big_file_is_named_not_diffed_and_digested_by_its_stat(kb):
    repo, _, _ = kb
    (repo.tree / "notes/knowledge/a.md").write_text("строка\n" * 100_000)
    assert repo.diff(["notes/knowledge/a.md"], 10_000).startswith("diff notes/knowledge/a.md\nфайл большой (")
    repo.max_file = 1000
    assert repo.snapshot()["notes/knowledge/a.md"].startswith("big:")


def test_none_in_expect_means_no_file_there(kb):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    (repo.tree / "notes/journal/2026-10-01-x.md").unlink()
    seen = repo.snapshot()
    done = run(repo.commit(["notes/knowledge/b.md", "notes/journal/2026-10-01-x.md"], "x", "",
                           expect={"notes/knowledge/b.md": seen["notes/knowledge/b.md"],
                                   "notes/journal/2026-10-01-x.md": None}))
    assert done.refused == [] and sorted(c.status for c in done.files) == ["A", "D"]


def test_a_push_that_times_out_is_said_and_the_next_press_commits_nothing_twice(kb, monkeypatch):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    real = repo._run

    def landed_but_late(*args, **kw):
        result = real(*args, **kw)
        if args[:1] == ("push",):
            raise subprocess.TimeoutExpired(["git", "push"], base_module.GIT_TIMEOUT_S)
        return result

    monkeypatch.setattr(repo, "_run", landed_but_late)
    done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert done.sha is None and done.refused == ["хаб не ответил, проверь и нажми ещё раз"]
    monkeypatch.setattr(repo, "_run", real)
    again = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert again.sha is None and again.left_out == ["notes/knowledge/b.md: уже так в Базе"]
    assert git(hub, "log", "-2", "--format=%s", "main").split("\n")[0] == "notes: б" and repo.changed() == []
