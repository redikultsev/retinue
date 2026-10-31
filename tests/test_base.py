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
    assert "Pushed-by: retinue-platform (the owner's button, session ab12cd34)" in message
    assert repo.changed() == [Change("notes/knowledge/other.md", "A")], "another session's file stays uncommitted"
    assert "notes/knowledge/other.md" not in git(hub, "ls-tree", "-r", "--name-only", "main")
    assert repo.diff(["notes/knowledge/other.md"], 10_000).startswith("--- /dev/null\n+++ b/notes/knowledge/other.md")



def test_discard_puts_the_chosen_files_back_as_head_has_them_and_only_as_the_card_saw_them(kb):
    repo, hub, _ = kb
    head = git(hub, "rev-parse", "main")
    before = (repo.tree / "notes/AGENTS.md").read_text()
    (repo.tree / "notes/AGENTS.md").write_text(before + "- строка сессии\n")
    (repo.tree / "notes/knowledge/new.md").write_text(RECORD.format(id="n", who="x", body="новое"))
    (repo.tree / "notes/journal/2026-10-01-x.md").unlink()
    (repo.tree / "notes/knowledge/other.md").write_text(RECORD.format(id="o", who="x", body="другая сессия"))
    seen = repo.snapshot()
    chosen = ["notes/AGENTS.md", "notes/knowledge/new.md", "notes/journal/2026-10-01-x.md"]
    done = run(repo.discard({p: seen.get(p) for p in chosen}))
    assert sorted(done.files) == sorted(chosen) and done.refused == []
    assert (repo.tree / "notes/AGENTS.md").read_text() == before
    assert not (repo.tree / "notes/knowledge/new.md").exists() and (repo.tree / "notes/journal/2026-10-01-x.md").exists()
    assert repo.changed() == [Change("notes/knowledge/other.md", "A")], "what was not chosen stays as it is"
    assert git(hub, "rev-parse", "main") == head, "nothing is committed"

    (repo.tree / "notes/knowledge/other.md").write_text("изменён после карточки")
    done = run(repo.discard({"notes/knowledge/other.md": seen["notes/knowledge/other.md"], "../x": None}))
    assert done.files == [] and len(done.refused) == 2
    assert (repo.tree / "notes/knowledge/other.md").read_text() == "изменён после карточки", "never clobbered"


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
        "notes/knowledge/link.md: a symlink — the Base holds only plain files",
        "notes/knowledge/fifo.md: not a plain file (FIFO, socket, device)",
        "notes/knowledge/dir.md: a folder, not a file"]
    assert repo.diff(["notes/knowledge/link.md", "notes/knowledge/fifo.md"], 1000) == (
        "diff notes/knowledge/link.md\nsymlink: not shown\n"
        "diff notes/knowledge/fifo.md\nnot a plain file: not shown\n")


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
    assert done.refused == [".gitattributes: a hidden path — edited only from the Mac",
                            ".gitmodules: a hidden path — edited only from the Mac",
                            ".git/config: a git internal name — does not go to the Base",
                            "scripts/lint.py: this file runs in the Base's check — edited only from the Mac",
                            "notes/knowledge/tool.py: this file runs in the Base's check — edited only from the Mac"]
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
    assert done.refused[0].startswith("notes/knowledge/a.md: id changed: a → a-renamed")


def test_nothing_new_commits_nothing(kb):
    repo, hub, _ = kb
    before = git(hub, "rev-parse", "main")
    done = run(repo.commit(["notes/knowledge/a.md"], "notes: ничего", ""))
    assert done.sha is None and done.left_out == ["notes/knowledge/a.md: already so in the Base"]
    assert run(repo.commit(["notes/knowledge/a.md"], "  ", "")).refused == ["commit: empty subject"]
    assert git(hub, "rev-parse", "main") == before


def test_the_diff_is_bounded(kb):
    repo, _, _ = kb
    (repo.tree / "notes/knowledge/a.md").write_text("строка\n" * 5000)
    text = repo.diff(["notes/knowledge/a.md"], 500)
    assert text.startswith("--- a/notes/knowledge/a.md\n+++ b/notes/knowledge/a.md\n") and len(text) < 560
    assert text.endswith("… diff cut\n")


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
    assert done.sha is None and len(done.refused) == 1 and done.refused[0].startswith("the hub did not accept the push: ")
    assert repo.changed() == [Change("notes/knowledge/b.md", "A")], "nothing lost: the file waits for the next press"


def test_what_the_hubs_check_runs_is_refused_even_without_protect(kb):
    repo, _, _ = kb
    repo.cfg.protect = []
    (repo.tree / "scripts/lint.py").write_text("print('x')\n")
    done = run(repo.commit(["scripts/lint.py"], "x", ""))
    assert done.refused == ["scripts/lint.py: this file runs in the Base's check — edited only from the Mac"]


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
                           "notes: б", "Retinue: session s1, card c1.", expect={**expect, "notes/journal/2026-10-01-x.md": ""}))
    assert done.files == [Change("notes/knowledge/b.md", "A")]
    assert done.refused == ["notes/knowledge/a.md: the file changed after the card — open it again",
                            "notes/journal/2026-10-01-x.md: the file changed after the card — open it again"]
    assert repo._digest_now("notes/knowledge/l.md", repo._read("notes/knowledge/l.md")) == seen["notes/knowledge/l.md"]
    assert "Retinue: session s1, card c1.\n\nPushed-by: retinue-platform (the owner's button)" in git(
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
        "justfile: this file runs in the Base's check — edited only from the Mac",
        "MAP.md: a folder, not a file",
        "justfile/x.md: the Base has a file where the folder justfile would be — does not go to the Base",
        "MAP.md/x.md: the Base has a file where the folder MAP.md would be — does not go to the Base",
        "notes/journal: the Base has a folder in this place — does not go to the Base"]
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
    assert done.sha is None and done.refused == ["the commit would also touch other files of the Base: MAP.md — not built"]
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
    assert run(repo.commit(["MAP.md"], "x", "")).left_out == ["MAP.md: not changed here"], "never undoes the hub's"
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
    assert repo.diff(["notes/knowledge/a.md"], 10_000).startswith("diff notes/knowledge/a.md\nlarge file (")
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


@pytest.mark.parametrize("verdict", ["push rejected (owner); the rules are kb-policy.json in the hub",
                                     "push не принят (Владелец); правила — kb-policy.json в хабе"])
def test_the_hooks_refusal_is_read_in_either_language(kb, monkeypatch, verdict):
    """The hub's pre-receive says why in English; a hub still on the older hook says it in Russian."""
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="x", body="Б."))
    real = repo._run

    def refused(*args, **kw):
        if args[:1] == ("push",):
            said = f"remote: notes/knowledge/b.md: the reason\nremote: {verdict}\n"
            return subprocess.CompletedProcess(["git", *args], 1, b"", said.encode())
        return real(*args, **kw)

    monkeypatch.setattr(repo, "_run", refused)
    done = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert done.sha is None and done.refused == ["notes/knowledge/b.md: the reason"]


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
    assert done.sha is None and done.refused == ["the hub did not answer — check and press again"]
    monkeypatch.setattr(repo, "_run", real)
    again = run(repo.commit(["notes/knowledge/b.md"], "notes: б", ""))
    assert again.sha is None and again.left_out == ["notes/knowledge/b.md: already so in the Base"]
    assert git(hub, "log", "-2", "--format=%s", "main").split("\n")[0] == "notes: б" and repo.changed() == []


# --- the platform's own commit after a turn, and its «Undo» ---------------------------------------------------------

def test_the_platforms_own_commit_takes_only_what_the_assistant_may_write(kb):
    repo, hub, _ = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Claude Code (Opus 5.5)", body="Б."))
    (repo.tree / "notes/knowledge/a.md").write_text(RECORD.format(id="a", who="Владелец", body="Правка сессии."))
    (repo.tree / "notes/AGENTS.md").write_text(showcase("- [b](knowledge/b.md)").replace("- правило\n", "- новое\n"))
    done = run(repo.commit(["notes/knowledge/b.md", "notes/knowledge/a.md", "notes/AGENTS.md"], "notes: b", "",
                           session="ab12cd34", role="assistant"))
    assert done.sha == git(hub, "rev-parse", "main")
    assert [(c.status, c.path) for c in done.files] == [("A", "notes/knowledge/b.md")]
    assert any(r.startswith("notes/knowledge/a.md: a Record you wrote") for r in done.refused)
    assert any(r.startswith("notes/AGENTS.md: ") for r in done.refused), "a Showcase above its marker is the owner's"
    assert "Pushed-by: retinue-platform (the platform's own commit after a turn, session ab12cd34)" in \
        git(hub, "log", "-1", "--format=%B", "main")
    assert sorted(c.path for c in repo.changed()) == ["notes/AGENTS.md", "notes/knowledge/a.md"], "they wait for the button"


def test_undo_puts_back_what_the_commit_changed_in_a_new_commit(kb):
    repo, hub, _ = kb
    old = (repo.tree / "notes/journal/2026-10-01-x.md").read_text()
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Claude Code (Opus 5.5)", body="Б."))
    (repo.tree / "notes/journal/2026-10-01-x.md").write_text(old + "\nДописано.\n")
    done = run(repo.commit(["notes/knowledge/b.md", "notes/journal/2026-10-01-x.md"], "notes: b", "", role="assistant"))
    assert len(done.files) == 2
    undone = run(repo.revert(done.sha, ["notes/knowledge/b.md", "notes/journal/2026-10-01-x.md"], "notes: b", "ab12cd34"))
    assert undone.sha == git(hub, "rev-parse", "main") and undone.refused == []
    assert git(hub, "rev-parse", "main~1") == done.sha, "history is never rewritten: the undo is a commit on top"
    assert git(hub, "log", "-1", "--format=%s", "main") == "Undo: notes: b"
    assert not (repo.tree / "notes/knowledge/b.md").exists() and \
        (repo.tree / "notes/journal/2026-10-01-x.md").read_text() == old
    assert repo.changed() == []


def test_undo_never_touches_a_file_changed_since(kb):
    repo, hub, mac = kb
    (repo.tree / "notes/knowledge/b.md").write_text(RECORD.format(id="b", who="Claude Code (Opus 5.5)", body="Б."))
    (repo.tree / "notes/knowledge/c.md").write_text(RECORD.format(id="c", who="Claude Code (Opus 5.5)", body="В."))
    done = run(repo.commit(["notes/knowledge/b.md", "notes/knowledge/c.md"], "notes: b, c", "", role="assistant"))
    git(mac, "pull", "-q")
    mac_push(mac, {"notes/knowledge/b.md": RECORD.format(id="b", who="Владелец", body="Б с Mac.")})
    run(repo.refresh())
    (repo.tree / "notes/knowledge/c.md").write_text(RECORD.format(id="c", who="Claude Code (Opus 5.5)", body="В2."))
    undone = run(repo.revert(done.sha, ["notes/knowledge/b.md", "notes/knowledge/c.md"], "notes: b, c"))
    assert undone.sha is None
    assert sorted(undone.refused) == ["notes/knowledge/b.md: changed here since — not undone",
                                      "notes/knowledge/c.md: changed here since — not undone"]
    assert "Б с Mac." in (repo.tree / "notes/knowledge/b.md").read_text()
