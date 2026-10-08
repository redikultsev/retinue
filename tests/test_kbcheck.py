"""The knowledge base's checks: one module for the router (before it pushes) and the hub (pre-receive). Real git in a
temporary folder; no model, no network."""

import json
import os
import subprocess
import unicodedata
from pathlib import Path

import pytest

from retinue import kbcheck

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- Витрина: выше — правила, ниже — строки. -->"
LINT = """import pathlib, sys
root = pathlib.Path(__file__).resolve().parent.parent
bad = [str(p.relative_to(root)) for p in root.rglob("*.md") if "BROKEN" in p.read_text()]
for b in bad:
    print(f"  ОШИБКА: {b}: сломано")
print(f"ошибок: {len(bad)}")
sys.exit(1 if bad else 0)
"""
RECORD = "---\nid: {id}\nwritten_by: {who}\ncreated: 2026-10-01\nupdated: 2026-10-01\nstatus: open\n---\n\n{body}\n"


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", **(env or {})}).stdout.strip()


def policy(**over) -> kbcheck.Policy:
    raw = {"branch": "main", "writer_uid": -1, "marker": MARKER,
           "writable": ["notes/knowledge/**/*.md", "notes/journal/**/*.md"],
           "lines_below": ["*/AGENTS.md", "MAP.md"],
           "never": ["**/CLAUDE.md", "**/AGENTS.md", "**/SKILL.md", "**/build/**", ".*/**"],
           "excluded": [".scratch/**"], "check": ["python3", "scripts/lint.py"]}
    return kbcheck.Policy.of({**raw, **over})


@pytest.fixture
def base(tmp_path):
    """A base: root rules, a Space with its showcase, a record of the owner's, a journal entry, the lint."""
    repo = tmp_path / "base"
    files = {
        "AGENTS.md": "# Правила базы\n",
        "MAP.md": f"# Карта\n\n{MARKER}\n\n- [a](notes/knowledge/a.md)\n",
        "notes/AGENTS.md": f"# Заметки\n\n## Правила домена\n\n- правило\n\n{MARKER}\n\n## Записи\n\n- [a](knowledge/a.md)\n",
        "notes/knowledge/a.md": RECORD.format(id="a", who="Владелец", body="Факт."),
        "notes/journal/2026-10-01-x.md": RECORD.format(id="2026-10-01-x", who="Claude Code (Opus 5)", body="Решение."),
        "scripts/lint.py": LINT,
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def change(repo, edits: dict, message="edit") -> tuple[str, str]:
    """Commit `edits` (path -> text, None = delete); return (old, new)."""
    old = git(repo, "rev-parse", "HEAD")
    for name, text in edits.items():
        path = repo / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return old, git(repo, "rev-parse", "HEAD")


def verdict(repo, old, new, role=kbcheck.ASSISTANT, **over):
    return kbcheck.check(["git", "-C", str(repo)], old, new, policy(**over), role)


def test_a_glob_star_is_one_folder_and_two_stars_any_depth():
    assert kbcheck.match("notes/knowledge/a.md", "notes/knowledge/**/*.md")
    assert kbcheck.match("notes/knowledge/sub/a.md", "notes/knowledge/**/*.md")
    assert not kbcheck.match("notes/knowledge/a.txt", "notes/knowledge/**/*.md")
    assert kbcheck.match("notes/AGENTS.md", "*/AGENTS.md") and not kbcheck.match("a/b/AGENTS.md", "*/AGENTS.md")
    assert kbcheck.match("notes/knowledge/CLAUDE.md", "**/CLAUDE.md") and kbcheck.match("CLAUDE.md", "**/CLAUDE.md")
    assert kbcheck.match(".claude/skills/x/SKILL.md", ".*/**") and not kbcheck.match("notes/a.md", ".*/**")


def test_the_assistant_writes_records_and_showcase_lines_and_nothing_else(base):
    showcase = (base / "notes/AGENTS.md").read_text()
    good = verdict(base, *change(base, {"notes/knowledge/b.md": RECORD.format(id="b", who="Ассистентка", body="Б."),
                                        "notes/AGENTS.md": showcase + "- [b](knowledge/b.md)\n"}))
    assert good.refusals == [] and good.files == [("M", "notes/AGENTS.md"), ("A", "notes/knowledge/b.md")]
    above = verdict(base, *change(base, {"notes/AGENTS.md": showcase.replace("- правило", "- новое правило")}))
    assert above.refusals == ["notes/AGENTS.md: выше маркера Витрины — правила; ниже можно только строки"]
    for path in ("scripts/lint.py", "AGENTS.md", "notes/knowledge/CLAUDE.md", "notes/knowledge/AGENTS.md",
                 "notes/artifacts/build/x.md", "notes/knowledge/x.py"):
        refused = verdict(base, *change(base, {path: "x\n"}))
        assert refused.refusals == [f"{path}: сюда ассистентке писать нельзя"], path
    gone = verdict(base, *change(base, {"MAP.md": None}))
    assert gone.refusals == ["MAP.md: Витрину нельзя удалить или создать, только дописать строки ниже маркера"]
    owner = verdict(base, *change(base, {"scripts/lint.py": LINT + "# owner\n"}), role=kbcheck.OWNER)
    assert owner.refusals == [], "the owner changes the rules and the scripts"


def test_no_symlink_and_no_executable_file_gets_in(base):
    (base / "notes/knowledge/link.md").symlink_to("/proc/self/environ")
    old, new = change(base, {})
    for role in (kbcheck.ASSISTANT, kbcheck.OWNER):
        out = verdict(base, old, new, role=role)
        assert out.refusals == ["notes/knowledge/link.md: симлинк — в базе только обычные файлы"], role
    (base / "notes/knowledge/link.md").unlink()
    git(base, "add", "-A")
    git(base, "commit", "-q", "-m", "rm")
    (base / "notes/knowledge/run.md").write_text("x\n")
    (base / "notes/knowledge/run.md").chmod(0o755)
    out = verdict(base, *change(base, {}))
    assert out.refusals == ["notes/knowledge/run.md: исполняемый файл — в базе только обычные файлы"]


def test_records_change_and_go_and_an_id_never_changes_while_the_record_lives(base):
    """Notes are notes: anything may be rewritten or deleted, the journal's entries included — the evening list shows
    it. Only a record's name stays: links depend on its id."""
    entry = "notes/journal/2026-10-01-x.md"
    rewritten = verdict(base, *change(base, {entry: (base / entry).read_text().replace("Решение.", "Другое решение.")}))
    assert rewritten.refusals == [] and rewritten.files == [("M", entry)]
    assert verdict(base, *change(base, {entry: None})).refusals == [], "a journal entry may go"
    record = "notes/knowledge/a.md"
    moved_id = verdict(base, *change(base, {record: (base / record).read_text().replace("id: a", "id: a2")}))
    assert moved_id.refusals == [f"{record}: id изменён: a → a2; id — имя Записи, на него ссылаются: он не меняется, "
                                 "пока Запись есть"]
    for role in (kbcheck.ASSISTANT, kbcheck.OWNER):
        assert verdict(base, "HEAD~1", "HEAD", role=role).refusals == moved_id.refusals, role
    git(base, "reset", "-q", "--hard", "HEAD~1")
    text = (base / record).read_text()
    moved = verdict(base, *change(base, {record: None, "notes/knowledge/old/a.md": text.replace("Факт.", "Факт, уточнён.")}))
    assert moved.refusals == [] and moved.files == [("R", "notes/knowledge/old/a.md")], "moved and edited, same id"
    gone = verdict(base, *change(base, {"notes/knowledge/old/a.md": None, "notes/knowledge/b.md": text.replace("id: a", "id: b")}))
    assert gone.refusals == [], "deleted, and a new record next to it (git may call it a move): allowed"


def test_what_is_excluded_never_enters_the_base(base):
    out = verdict(base, *change(base, {".scratch/research/notes.md": "x\n"}), role=kbcheck.OWNER)
    assert out.refusals == [".scratch/research/notes.md: этого в базе не бывает — путь исключён политикой"]


def test_the_owners_records_and_deletions_are_named(base):
    record = "notes/knowledge/a.md"
    out = verdict(base, *change(base, {record: (base / record).read_text() + "Ещё.\n"}))
    assert out.refusals == [] and out.owner_records == [record] and out.deleted == []
    out = verdict(base, *change(base, {record: None}))
    assert out.refusals == [] and out.owner_records == [record] and out.deleted == [record]


def test_the_lint_runs_on_a_copy_of_the_new_tree_and_its_errors_are_the_reason(base, tmp_path):
    out = verdict(base, *change(base, {"notes/knowledge/b.md": "BROKEN\n"}))
    assert out.refusals == ["just check: ОШИБКА: notes/knowledge/b.md: сломано"]
    assert verdict(base, *change(base, {"notes/knowledge/b.md": None})).refusals == [], "a deleted file is gone"
    hung = verdict(base, *change(base, {"notes/knowledge/c.md": "x\n"}),
                   check=["python3", "-c", "import time; time.sleep(5)"], check_timeout=1)
    assert hung.refusals == ["just check: не уложился в 1 с"]


def install_hub(tmp_path, base, **over) -> Path:
    hub = tmp_path / "hub.git"
    git(tmp_path, "clone", "-q", "--bare", str(base), str(hub))
    hook = hub / "hooks" / "pre-receive"
    hook.write_text((ROOT / "retinue" / "kbcheck.py").read_text())
    hook.chmod(0o755)
    raw = json.loads(json.dumps(policy(**over).__dict__))
    (hub / "hooks" / "kb-policy.json").write_text(json.dumps(raw, ensure_ascii=False))
    return hub


def push(clone, *args):
    return subprocess.run(["git", "push", "-q", *args], cwd=clone, capture_output=True, text=True)


def test_the_hub_checks_every_push_with_the_same_rules(base, tmp_path):
    hub = install_hub(tmp_path, base, writer_uid=os.getuid())  # this test's pushes are the assistant's
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(hub), str(clone))
    change(clone, {"scripts/lint.py": LINT + "# x\n"})
    refused = push(clone, "origin", "main")
    assert refused.returncode != 0 and "scripts/lint.py: сюда ассистентке писать нельзя" in refused.stderr
    git(clone, "reset", "-q", "--hard", "origin/main")
    change(clone, {"notes/knowledge/b.md": RECORD.format(id="b", who="Ассистентка", body="Б.")})
    assert push(clone, "origin", "main").returncode == 0
    assert "отказ: только ветка main" in push(clone, "origin", "main:other").stderr
    assert "отказ: main не удаляется" in push(clone, "origin", ":main").stderr
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    change(clone, {"notes/knowledge/c.md": "x\n"})
    assert "отказ: история main не переписывается" in push(clone, "-f", "origin", "main").stderr


def test_the_owner_pushes_rules_and_notes_but_not_what_is_excluded(base, tmp_path):
    hub = install_hub(tmp_path, base)  # writer_uid -1: this test's pushes are the owner's
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(hub), str(clone))
    change(clone, {"AGENTS.md": "# Правила базы, вторая редакция\n"})
    assert push(clone, "origin", "main").returncode == 0, "rules are the owner's"
    entry = "notes/journal/2026-10-01-x.md"
    change(clone, {entry: (clone / entry).read_text().replace("Решение.", "Решение, опечатка исправлена.")})
    assert push(clone, "origin", "main").returncode == 0, "the journal is notes: it may be changed"
    change(clone, {".scratch/x.md": "черновик\n"})
    refused = push(clone, "origin", "main")
    assert refused.returncode != 0 and ".scratch/x.md: этого в базе не бывает" in refused.stderr
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    change(clone, {"notes/knowledge/b.md": "BROKEN\n"})
    assert "just check: ОШИБКА: notes/knowledge/b.md: сломано" in push(clone, "origin", "main").stderr


def test_nothing_in_the_tree_shadows_the_lints_standard_library(base, tmp_path):
    """The lint runs on the pushed tree: a `scripts/re.py` in it must not be what `import re` loads, or a push
    would run its code as whoever checks it."""
    planted = tmp_path / "planted-ran"
    old, new = change(base, {"scripts/re.py": f"open({str(planted)!r}, 'w').write('x')\nfrom sre_compile import *\n"})
    out = verdict(base, old, new, role=kbcheck.OWNER)
    assert out.refusals == [] and not planted.exists()


def test_the_lint_copy_is_a_fresh_folder_only_its_user_can_enter(base, tmp_path):
    seen = tmp_path / "mode"
    probe = ["python3", "-c", f"import os; open({str(seen)!r}, 'w').write(oct(os.stat('..').st_mode & 0o777)); "
             "print(sorted(os.listdir('.')))"]
    out = verdict(base, *change(base, {"notes/knowledge/b.md": "x\n"}), check=probe)
    assert out.refusals == [] and seen.read_text() == "0o700"
    assert "check_dir" not in kbcheck.Policy.__dataclass_fields__, "no shared, reusable copy anyone could plant in"


def stage(repo, files: dict) -> tuple[str, str]:
    """Commit paths a Mac's file system could not hold side by side (A.md and a.md, é in NFC and NFD): straight into
    git's index, never onto the disk."""
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "core.precomposeunicode", "false")  # git on a Mac would make NFD into NFC by itself
    for path, text in files.items():
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=text, capture_output=True,
                              text=True, check=True).stdout.strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    git(repo, "commit", "-q", "-m", "staged")
    return old, git(repo, "rev-parse", "HEAD")


def test_case_unicode_and_hidden_names_do_not_get_past_never(base):
    """On the owner's Mac `claude.md` is CLAUDE.md, and a folder that starts with a dot is nobody's notes."""
    for path in ("notes/knowledge/claude.md", "notes/knowledge/Claude.md", "notes/knowledge/agents.md",
                 "notes/knowledge/SKILL.MD"):
        assert verdict(base, *stage(base, {path: "x\n"})).refusals == [f"{path}: сюда ассистентке писать нельзя"], path
        git(base, "reset", "-q", "HEAD~1")
    for path in ("notes/knowledge/.claude/x.md", "notes/knowledge/.hidden.md"):
        assert verdict(base, *stage(base, {path: "x\n"})).refusals == [
            f"{path}: имя с точки — скрытое, ассистентке сюда нельзя"], path
        git(base, "reset", "-q", "HEAD~1")
    assert kbcheck.match("Notes/Knowledge/A.MD", "notes/knowledge/**/*.md")
    assert kbcheck.match(unicodedata.normalize("NFD", "notes/knowledge/é.md"), "notes/knowledge/é.md")


def test_two_names_that_are_one_file_on_a_mac_are_refused(base):
    out = verdict(base, *stage(base, {"notes/knowledge/A.md": "x\n"}), role=kbcheck.OWNER)
    assert out.refusals == ["notes/knowledge/A.md: на Mac совпадёт с notes/knowledge/a.md — имена отличаются только "
                            "регистром или нормализацией Unicode"]
    git(base, "reset", "-q", "HEAD~1")
    nfc, nfd = "notes/knowledge/é.md", unicodedata.normalize("NFD", "notes/knowledge/é.md")
    _, old = stage(base, {nfc: "x\n"})
    _, new = stage(base, {nfd: "y\n"})
    out = verdict(base, old, new, role=kbcheck.OWNER)
    assert out.refusals == [f"{nfd}: на Mac совпадёт с {nfc} — имена отличаются только регистром или нормализацией "
                            "Unicode"]
    git(base, "reset", "-q", "HEAD~2")
    out = verdict(base, *stage(base, {"Notes/x.md": "x\n"}), role=kbcheck.OWNER)
    assert out.refusals == ["Notes: на Mac совпадёт с notes — имена отличаются только регистром или нормализацией "
                            "Unicode"], "a folder too"
