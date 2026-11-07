"""A project's passport (`retinue/passport.py`): its schema, the check of its structure, the hub refusing a push that
breaks one, and the app's reading of it. Asserts are on problem codes, parts, paths and parsed values — never on what
a sentence says."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from retinue import kbcheck, passport
from retinue.spaces import MARKER, showcase

ROOT = Path(__file__).resolve().parents[1]

GOOD = """# Garden

@VOICE.md

## Why

Roses that bloom every summer without the owner remembering to water them (owner, 2026-05-01).

## Scope

- In: soil tests and the watering timer (assumption)
- Out: buying plants → ledger (owner, 2026-05-01)
- Size: one evening a month of his time (assumption)
- Ceiling: `results/map.md` (assumption)

## Files

- `README.md` — the garden's state; read first
- `knowledge/` — what grows where; read before planting
- `journal/` — the history of sessions

## Rules

- Water before 9 am.

## Checks

- Every bed in `README.md` has a date of its last soil test.
"""

FILES = {"VOICE.md", "README.md", "knowledge", "journal", "results", "results/map.md"}


def present(rel: str) -> bool:
    return rel.rstrip("/") in FILES


def codes(text: str, exists=present) -> set[tuple[str, str]]:
    return {(p.code, p.part) for p in passport.parse(text, exists).problems}


def test_a_passport_by_the_schema_passes_and_reads_as_data():
    told = passport.parse(GOOD, present)
    assert told.ok, [p.as_dict() for p in told.problems]
    assert told.title == "Garden" and told.imports == ["VOICE.md"]
    assert [(s["key"], s["mark"]) for s in told.scope] == [
        ("in", {"by": "assumption"}), ("out", {"by": "owner", "date": "2026-05-01"}),
        ("size", {"by": "assumption"}), ("ceiling", {"by": "assumption"})]
    assert told.scope[1]["text"] == "buying plants" and told.scope[1]["where"] == "ledger"
    assert [f["path"] for f in told.files] == ["README.md", "knowledge/", "journal/"]
    assert len(told.rules) == 1 and len(told.checks) == 1 and told.other == []


@pytest.mark.parametrize("part", ["Why", "Scope", "Files"])
def test_each_required_part_missing_is_a_problem(part):
    lines = GOOD.split("\n## ")
    text = "\n## ".join(chunk for chunk in lines if not chunk.startswith(part))
    assert ("missing", part) in codes(text)


def test_the_optional_parts_may_be_left_out():
    text = GOOD.split("\n## Rules")[0]
    assert codes(text) == set()


def test_a_part_the_schema_does_not_name_text_outside_the_parts_and_a_nested_heading_are_problems():
    unknown = GOOD.replace("## Rules", "## History\n\n- it rained\n\n## Rules")
    assert ("unknown", "History") in codes(unknown)
    assert passport.parse(unknown).other[:2] == ["## History", ""]
    stray = GOOD.replace("@VOICE.md", "@VOICE.md\n\nSome words under the title.")
    assert ("stray", "title") in codes(stray)
    prose = GOOD.replace("- Water before 9 am.", "- Water before 9 am.\n\nA paragraph in the rules.")
    assert ("stray", "Rules") in codes(prose)
    nested = GOOD.replace("## Rules\n", "## Rules\n\n### Morning\n")
    assert ("nested", "Rules") in codes(nested)
    sub = GOOD.replace("- Water before 9 am.", "- Water before 9 am.\n  - and after 6 pm")
    assert ("nested", "Rules") in codes(sub)


def test_parts_go_in_order_and_once():
    swapped = GOOD.replace("## Why", "## TMP").replace("## Scope", "## Why").replace("## TMP", "## Scope")
    assert ("order", "Why") in codes(swapped)
    twice = GOOD + "\n## Rules\n\n- again\n"
    assert ("twice", "Rules") in codes(twice)


def test_why_is_one_marked_paragraph_within_its_length():
    unmarked = GOOD.replace(" (owner, 2026-05-01).", ".", 1)
    assert ("mark", "Why") in codes(unmarked)
    two = GOOD.replace("remembering to water them", "remembering\n\nto water them")
    assert ("count", "Why") in codes(two)
    long = GOOD.replace("Roses", "Roses " + "x" * 900)
    assert ("long", "Why") in codes(long)
    listed = GOOD.replace("Roses that", "- Roses that")
    assert ("stray", "Why") in codes(listed)


def test_scope_lines_have_a_key_a_mark_and_an_out_says_where():
    assert ("mark", "Scope") in codes(GOOD.replace("- In: soil tests and the watering timer (assumption)",
                                                   "- In: soil tests"))
    assert ("where", "Scope") in codes(GOOD.replace("buying plants → ledger", "buying plants"))
    assert ("key", "Scope") in codes(GOOD.replace("- Size:", "- Budget:"))
    no_out = GOOD.replace("- Out: buying plants → ledger (owner, 2026-05-01)\n", "")
    assert ("count", "Scope") in codes(no_out)
    four_in = GOOD.replace("- In:", "- In: a (assumption)\n- In: b (assumption)\n- In: c (assumption)\n- In:", 1)
    assert ("count", "Scope") in codes(four_in)
    bad_date = GOOD.replace("(owner, 2026-05-01)\n", "(owner, May)\n")
    assert ("mark", "Scope") in codes(bad_date)


def test_every_path_a_passport_names_is_in_the_project():
    told = passport.parse(GOOD.replace("`knowledge/`", "`gone.md`"), present)
    assert [(p.code, p.part, p.subject) for p in told.problems] == [("path", "Files", "gone.md")]
    outside = codes(GOOD.replace("`knowledge/`", "`../ledger/README.md`"), lambda rel: True)
    assert ("path", "Files") in outside
    assert ("path", "imports") in codes(GOOD.replace("@VOICE.md", "@PERSONA.md"))
    assert ("path", "Scope") in codes(GOOD.replace("`results/map.md`", "`results/none.md`"))
    assert ("stray", "Files") in codes(GOOD.replace("- `journal/` — the history of sessions", "- journal"))


def test_the_limits_hold_a_passport_to_a_screen_or_two():
    big = GOOD.replace("- Water before 9 am.", "\n".join(f"- Rule {n} " + "y" * 200 for n in range(12)))
    assert ("size", "passport") in codes(big + "\n".join("- " + "z" * 200 for _ in range(10)))
    many = GOOD.replace("- Water before 9 am.", "\n".join(f"- Rule {n}." for n in range(13)))
    assert ("count", "Rules") in codes(many)
    long_rule = GOOD.replace("- Water before 9 am.", "- " + "w" * 301)
    assert ("long", "Rules") in codes(long_rule)
    assert ("long", "title") in codes(GOOD.replace("# Garden", "# " + "G" * 81))


def test_the_head_holds_only_the_keys_the_schema_names():
    assert codes("---\nid: garden-7f3a\n# a comment\ncode: [example.org/acme/garden]\n---\n" + GOOD) == set()
    assert ("head", "head") in codes("---\nid: garden\nowner: x\n---\n" + GOOD)
    assert ("head", "head") in codes("---\nid: garden\n" + GOOD)


def test_no_title_is_a_problem_and_the_passport_ends_at_the_marker():
    assert ("title", "title") in codes(GOOD.replace("# Garden\n", ""))
    below = passport.above(GOOD + f"\n{MARKER} ниже — строки -->\n\n## Tasks\n\n- one\n")
    assert codes(below) == set()


def write(root: Path, files: dict) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def tree(tmp_path) -> Path:
    base = tmp_path / "base"
    write(base, {"AGENTS.md": GOOD, "VOICE.md": "v", "README.md": "r", "knowledge/a.md": "a", "journal/j.md": "j",
                 "results/map.md": "m",
                 "garden/AGENTS.md": GOOD, "garden/VOICE.md": "v", "garden/README.md": "r",
                 "garden/knowledge/a.md": "a", "garden/journal/j.md": "j", "garden/results/map.md": "m",
                 "garden/roses/AGENTS.md": "# Roses\n",
                 "Templates/AGENTS.md": "# not a project: the folder's name\n",
                 ".hidden/AGENTS.md": "# not a project\n"})
    return base


def test_the_check_lists_every_project_of_a_base_and_fails_when_one_fails(tmp_path, capsys):
    base = tree(tmp_path)
    assert passport.projects(str(base)) == ["", "garden", "garden/roses"]
    assert passport.main([str(base)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[:2] for line in lines if line.startswith(("ok", "FAIL"))] == [
        ["ok", "."], ["ok", "garden"], ["FAIL", "garden/roses"]]
    write(base, {"garden/roses/AGENTS.md": GOOD, "garden/roses/VOICE.md": "v", "garden/roses/README.md": "r",
                 "garden/roses/knowledge/a.md": "a", "garden/roses/journal/j.md": "j",
                 "garden/roses/results/map.md": "m"})
    assert passport.main([str(base)]) == 0


# --- the hub ------------------------------------------------------------------------------------------------------


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def commit(repo: Path, edits: dict) -> tuple[str, str]:
    old = git(repo, "rev-parse", "HEAD")
    for name, text in edits.items():
        if text is None:
            (repo / name).unlink()
        else:
            write(repo, {name: text})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "edit")
    return old, git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    base = tree(tmp_path)
    git(base, "init", "-q", "-b", "main")
    git(base, "add", "-A")
    git(base, "commit", "-q", "-m", "start")
    return base


def refusals(repo: Path, old: str, new: str) -> list[str]:
    return kbcheck.check(["git", "-C", str(repo)], old, new, kbcheck.Policy.of({"marker": MARKER}),
                         kbcheck.OWNER).refusals


def test_a_change_that_breaks_a_passing_passport_is_refused(repo):
    old, new = commit(repo, {"garden/AGENTS.md": GOOD.replace("## Files", "## Map")})
    assert [r.split(":")[0] for r in refusals(repo, old, new)] == ["garden/AGENTS.md"]
    commit(repo, {"garden/AGENTS.md": GOOD})
    old, new = commit(repo, {"garden/README.md": None})
    assert [r.split(":")[0] for r in refusals(repo, old, new)] == ["garden/AGENTS.md"], "its Files name README.md"


def test_a_new_or_already_broken_passport_is_not_refused_by_the_hub(repo):
    old, new = commit(repo, {"garden/tulips/AGENTS.md": "# Tulips\n"})
    assert refusals(repo, old, new) == [], "a new project's skeleton: its first session fills it"
    old, new = commit(repo, {"garden/roses/AGENTS.md": "# Roses\n\nstill nothing\n"})
    assert refusals(repo, old, new) == [], "it failed before: the check lists it, the hub lets it through"
    old, new = commit(repo, {"garden/AGENTS.md": GOOD.replace("Water before", "Water the roses before")})
    assert refusals(repo, old, new) == []


def test_the_hubs_hook_imports_the_schema_installed_beside_it(repo, tmp_path):
    hub = tmp_path / "hub.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(hub))
    (hub / "hooks" / "pre-receive").write_text((ROOT / "retinue" / "kbcheck.py").read_text(encoding="utf-8"),
                                               encoding="utf-8")
    (hub / "hooks" / "pre-receive").chmod(0o755)
    (hub / "hooks" / "passport.py").write_text((ROOT / "retinue" / "passport.py").read_text(encoding="utf-8"),
                                               encoding="utf-8")
    policy = {"branch": "main", "writer_uid": -1, "marker": MARKER}
    (hub / "hooks" / "kb-policy.json").write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "-b", "main", str(hub), str(clone))
    commit(clone, {"garden/knowledge/a.md": None, "garden/README.md": None})
    pushed = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, capture_output=True, text=True)
    assert pushed.returncode != 0 and "garden/AGENTS.md" in pushed.stderr
    git(clone, "reset", "-q", "--hard", "origin/main")
    commit(clone, {"garden/knowledge/b.md": "b"})
    assert subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, capture_output=True).returncode == 0


# --- the app ------------------------------------------------------------------------------------------------------


def test_the_app_reads_the_passport_by_the_schema_with_its_problems():
    parsed = showcase(GOOD + f"\n{MARKER} -->\n\n## Задачи\n\n- one\n", exists=present)
    told = parsed["passport"]
    assert told["problems"] == [] and told["in_scope"] == "soil tests and the watering timer"
    assert told["not_in_scope"] == "buying plants → ledger"
    assert [s["key"] for s in told["scope"]] == ["in", "out", "size", "ceiling"]
    assert [f["path"] for f in told["files"]] == ["README.md", "knowledge/", "journal/"]
    assert parsed["rules"] == "- Water before 9 am." and [s["kind"] for s in parsed["sections"]] == ["tasks"]
    old = showcase("# Old\n\n## Why\n\nA reason.\n\n## Rules of the domain\n\n- Live prices only.\n")
    assert {p["code"] for p in old["passport"]["problems"]} >= {"mark", "unknown", "missing"}
    assert "- Live prices only." in old["rules"], "what the schema does not name still shows among the rules"
