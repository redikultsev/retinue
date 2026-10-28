"""The Base read in the app (`retinue/spaces.py`): the Showcase's marker and lines, a Record's head, links turned into
the app's routes, the path rules against hostile paths, links, huge files and alias bombs — all read through git
from a synthetic Base, never from a tree — and the API behind the face's login."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from retinue import spaces
from retinue.spaces import BaseTree, Refused, front_matter, record_path, resolve_links, showcase

from test_face import FaceWorld, faced
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse

MARKER = "<!-- Витрина: выше — правила Пространства, ниже — строки. Ассистентка пишет только ниже. -->"

SHOWCASE = f"""# Путешествия

Поездки и транзиты.

## Правила домена

- Цены — только живые.

{MARKER}

## Задачи

- Перепроверить правила [транзита](knowledge/transit.md) до 01.11.
  - сначала аэропорт
- **Собрать** варианты на декабрь.

## Открытые вопросы

- Брать ли транзит через хаб? См. [решение](journal/2026-10-01-hub.md).
- Пока открытых нет — это не вопрос, а строка.

## Записи

**knowledge/**

- [Транзит](knowledge/transit.md) — правило.

```
## не заголовок
```
"""

KNOWLEDGE = """---
written_by: Claude Code (Opus 5)
created: 2026-09-21
updated: 2026-10-02
id: transit
title: Правила транзита
type: knowledge
source: https://example.org/rules
verified: 2026-09-21
stale_after: 2026-12-21
status: current
practice: diverges
colour: зелёный
---

# Правила транзита

Смотри [паспорт](../profile/docs/passport.md), [Витрину](../AGENTS.md), [дом](../../home/AGENTS.md),
[чужое](../../.scratch/x.md), [скрипт](../../scripts/lint.py), [нет файла](missing.md), [сайт](https://example.org),
[js](javascript:alert(1)), [абсолютный](/etc/passwd) и [якорь](#ниже).

`[код](knowledge/transit.md)`
"""

PROFILE = """---
written_by: Владелец
created: 2026-09-01
updated: 2026-09-01
id: passport
title: Загранпаспорт
type: profile
from: 2025-03-01
until: 2030-03-01
learned: 2026-09-01
source: паспорт
---

Паспорт.
"""

DECISION = """---
written_by: Claude Code (Opus 5)
created: 2026-10-01
updated: 2026-10-01
id: 2026-10-01-hub
title: Летим через хаб
type: decision
date: 2026-10-01
---

Потому что дешевле.
"""


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_base(tmp_path, files: dict[str, str | bytes], links: dict[str, str] | None = None):
    """A synthetic Base committed on `main`; the reader gets only its repository."""
    work = tmp_path / "mac"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    for path, content in files.items():
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    for path, to in (links or {}).items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(to, work / path)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "база")
    return work


def reader(work) -> BaseTree:
    return BaseTree(["git", f"--git-dir={work / '.git'}", "-c", "core.hooksPath=/dev/null"],
                    {"PATH": os.environ["PATH"], "HOME": str(work), "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_CONFIG_GLOBAL": "/dev/null"})


BASE = {
    "travel/AGENTS.md": SHOWCASE,
    "travel/knowledge/transit.md": KNOWLEDGE,
    "travel/profile/docs/passport.md": PROFILE,
    "travel/journal/2026-10-01-hub.md": DECISION,
    "travel/journal/2026-09-01-old.md": DECISION.replace("2026-10-01", "2026-09-01"),
    "travel/knowledge/notes.txt": "не Запись",
    "travel/knowledge/.hidden.md": KNOWLEDGE,
    "travel/knowledge/a/b/too-deep.md": KNOWLEDGE,
    "home/AGENTS.md": f"# Дом\n\n{MARKER}\n\n## Открытые вопросы\n\n- Пока открытых нет.\n",
    "scripts/lint.py": "print('lint')\n",
    "MAP.md": "# Карта\n",
    "Bad Name/AGENTS.md": "# Не Пространство\n",
}


@pytest.fixture
def base(tmp_path):
    return reader(make_base(tmp_path, BASE, links={"travel/knowledge/link.md": "/etc/passwd",
                                                    "dots/AGENTS.md": "../MAP.md"}))


# --- parsers ---------------------------------------------------------------------------------------------------------

def test_the_showcase_splits_at_the_marker_and_groups_its_lines_by_heading():
    parsed = showcase(SHOWCASE)
    assert parsed["title"] == "Путешествия" and parsed["marker"]
    assert "Цены — только живые." in parsed["rules"] and "# Путешествия" not in parsed["rules"]
    assert "Задачи" not in parsed["rules"], "the lines below the marker are not rules"
    kinds = [(s["heading"], s["kind"]) for s in parsed["sections"]]
    assert kinds == [("Задачи", "tasks"), ("Открытые вопросы", "questions"), ("Записи", "records")]
    tasks = parsed["sections"][0]["items"]
    assert [i["list"] for i in tasks] == [True, True]
    assert tasks[0]["text"] == "Перепроверить правила [транзита](knowledge/transit.md) до 01.11.\n- сначала аэропорт"
    [question] = parsed["sections"][1]["items"]
    assert question["text"].startswith("Брать ли транзит"), "«Пока … нет» is a placeholder, not a question"
    records = parsed["sections"][2]["items"]
    assert records[0] == {"text": "**knowledge/**", "list": False}
    assert any("## не заголовок" in i["text"] for i in records), "a heading inside a code block is not a section"
    assert spaces.counts(parsed["sections"]) == {"questions": 1, "tasks": 2}


def test_a_showcase_without_the_marker_is_all_rules():
    parsed = showcase("# Без маркера\n\n## Задачи\n\n- одна\n")
    assert parsed["sections"] == [] and not parsed["marker"] and "- одна" in parsed["rules"]


def test_the_head_is_safe_yaml_and_its_dates_are_text():
    head, body = front_matter(KNOWLEDGE)
    assert head["verified"] == "2026-09-21" and head["practice"] == "diverges" and head["colour"] == "зелёный"
    assert body.lstrip().startswith("# Правила транзита")
    for hostile in ["---\n!!python/object/apply:os.system ['echo hi']\n---\nтело\n",
                    "---\nkey: [unclosed\n---\nтело\n",
                    "---\ncreated: 2026-02-30\n---\n",
                    "---\nтолько строка\n---\nтело\n",
                    "без шапки\n"]:
        head, body = front_matter(hostile)
        assert head is None and body == hostile
    bomb = "---\na: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n" \
           "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\nd: [*c, *c, *c, *c, *c, *c, *c, *c, *c]\n---\n"
    assert front_matter(bomb)[0] is None, "an alias bomb is not expanded into the answer"
    long = "---\n" + "k: " + "x" * (spaces.HEAD_MAX + 10) + "\n---\nтело\n"
    assert front_matter(long)[0] is None, "a head past HEAD_MAX is not parsed"


@pytest.mark.parametrize("path", [
    "../x.md", "knowledge/../../x.md", "/etc/passwd.md", "knowledge//x.md", "./x.md", ".scratch/x.md",
    "knowledge/.hidden.md", ".claude/x.md", "knowledge/a/b/x.md", "knowledge/x.txt", "knowledge/x", "knowledge/x\0.md",
    "knowledge/x\n.md", "knowledge\\x.md", "", ".md", "a" * 600 + ".md", None, 7,
])
def test_hostile_record_paths_are_refused(path):
    assert record_path(path) is None


def test_ordinary_record_paths_pass():
    for path in ["knowledge/x.md", "profile/acme/case.md", "AGENTS.md", "journal/2026-10-01-x.md"]:
        assert record_path(path) == path


def test_links_become_app_routes_or_plain_text(base):
    made = base.commit()
    _, record = base.record("travel", "knowledge/transit.md")
    body = record["body"]
    assert "[паспорт](/spaces/travel/r/profile/docs/passport.md)" in body
    assert "[Витрину](/spaces/travel)" in body and "[дом](/spaces/home)" in body
    assert "[сайт](https://example.org)" in body
    for gone in ["чужое", "скрипт", "нет файла", "js", "абсолютный", "якорь"]:
        assert f"[{gone}]" not in body and gone in body, f"{gone}: dropped to its text"
    assert "`[код](knowledge/transit.md)`" in body, "code is left as it is"
    route = lambda p: base.route(made, p)  # noqa: E731
    assert resolve_links("[x](<a b.md>)", "travel/knowledge", route) == "x"
    assert resolve_links("![картинка](pic.png)", "travel/knowledge", route) == "![картинка](pic.png)"
    assert resolve_links("[x](transit.md#часть)", "travel/knowledge", route) == "[x](/spaces/travel/r/knowledge/transit.md)"
    assert resolve_links("[x](../../../etc/passwd)", "travel/knowledge", route) == "x"


def test_hostile_schemes_and_reference_definitions_go_the_same_way(base):
    made = base.commit()
    route = lambda p: base.route(made, p)  # noqa: E731
    text = ("[js](javascript:alert%281%29) [d](data:text/html,<b>x</b>) [v](vbscript:x) [m](mailto:a@example.org)\n\n"
            "[паспорт][p] и [скрипт][s] и [зло][j] и [сайт][w]\n\n"
            "[p]: ../profile/docs/passport.md \"Паспорт\"\n"
            "[s]: ../../scripts/lint.py\n"
            "   [j]: javascript:alert(1)\n"
            "[w]: https://example.org/rules\n"
            "[up]: ../../../../p/1\n")
    out = resolve_links(text, "travel/knowledge", route)
    assert out.startswith("js d v [m](mailto:a@example.org)")
    assert "[p]: /spaces/travel/r/profile/docs/passport.md" in out and "[w]: https://example.org/rules" in out
    for gone in ["javascript", "data:", "vbscript", "lint.py", "../"]:
        assert gone not in out, gone


def test_the_grid_reads_only_the_showcases(tmp_path, monkeypatch):
    tree = reader(make_base(tmp_path, BASE))
    asked: list[str] = []
    real_blobs, real_start = BaseTree.blobs, BaseTree._start
    monkeypatch.setattr(BaseTree, "blobs", lambda self, shas: asked.extend(shas) or real_blobs(self, shas))
    monkeypatch.setattr(BaseTree, "_start", lambda self, sha, n: asked.append(sha) or real_start(self, sha, n))
    made = tree.commit()
    tree.spaces()
    showcases = {made.files[f"{n}/AGENTS.md"][1] for n in made.spaces}
    assert set(asked) == showcases, "no Record is read for the grid"
    asked.clear()
    tree.space("travel")
    assert made.files["travel/knowledge/transit.md"][1] in asked, "a Space's Records are read when it is opened"


def test_a_big_record_gives_its_head_from_its_start(tmp_path, monkeypatch):
    monkeypatch.setattr(spaces, "SMALL", 1024)
    big = KNOWLEDGE + "текст " * 2000
    tree = reader(make_base(tmp_path, {"travel/AGENTS.md": SHOWCASE, "travel/knowledge/transit.md": big}))
    [row] = tree.space("travel")[1]["records"]["knowledge"]
    assert row["title"] == "Правила транзита" and row["verified"] == "2026-09-21" and "big" not in row


# --- the reader --------------------------------------------------------------------------------------------------------

def test_spaces_are_top_folders_with_a_showcase(base):
    head, rows = base.spaces()
    assert len(head) == 40
    assert rows == [{"name": "home", "title": "Дом", "questions": 0, "tasks": 0},
                    {"name": "travel", "title": "Путешествия", "questions": 1, "tasks": 2}], \
        "no folder without AGENTS.md, no name that is not a Space's, no Showcase that is a link"


def test_a_space_lists_its_records_by_nature_with_their_dates(base):
    _, view = base.space("travel")
    records = view["records"]
    assert [r["path"] for r in records["knowledge"]] == ["knowledge/transit.md"], \
        "no .txt, no dotfile, nothing three folders deep, no link"
    [transit] = records["knowledge"]
    assert transit["verified"] == "2026-09-21" and transit["practice"] == "diverges" and transit["folder"] == ""
    assert "source" not in transit and "written_by" not in transit, "a listing carries only what it shows"
    [passport] = records["profile"]
    assert passport["folder"] == "docs" and passport["until"] == "2030-03-01"
    assert [r["path"] for r in records["journal"]] == ["journal/2026-10-01-hub.md", "journal/2026-09-01-old.md"]
    assert records["artifacts"] == []
    tasks = next(s for s in view["sections"] if s["kind"] == "tasks")
    assert "[транзита](/spaces/travel/r/knowledge/transit.md)" in tasks["items"][0]["text"], "Showcase links resolved"
    with pytest.raises(Refused) as no:
        base.space("dots")
    assert no.value.status == 404
    with pytest.raises(Refused):
        base.space("../travel")


def test_a_record_is_its_head_and_body(base):
    _, record = base.record("travel", "knowledge/transit.md")
    assert record["title"] == "Правила транзита" and record["nature"] == "knowledge" and not record["bad_head"]
    assert record["head"]["stale_after"] == "2026-12-21" and record["head"]["colour"] == "зелёный"
    for path in ["knowledge/link.md", "knowledge/.hidden.md", "knowledge/a/b/too-deep.md", "knowledge/notes.txt",
                 "../scripts/lint.py", "knowledge/missing.md"]:
        with pytest.raises(Refused) as refused:
            base.record("travel", path)
        assert refused.value.status == 404, path


def test_a_huge_record_is_listed_but_never_sent(tmp_path):
    big = KNOWLEDGE.replace("id: transit", "id: big") + "x" * (spaces.RECORD_MAX + 1)
    tree = reader(make_base(tmp_path, {"travel/AGENTS.md": SHOWCASE, "travel/knowledge/big.md": big}))
    [row] = tree.space("travel")[1]["records"]["knowledge"]
    assert row["big"] and row["title"] == "big", "not read: its name stands for its title"
    with pytest.raises(Refused) as refused:
        tree.record("travel", "knowledge/big.md")
    assert refused.value.status == 413


def test_a_huge_showcase_is_read_only_up_to_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(spaces, "SHOWCASE_MAX", 2048)
    huge = SHOWCASE + "\n".join(f"- строка {n}" for n in range(5000)) + "\n"
    tree = reader(make_base(tmp_path, {"travel/AGENTS.md": huge}))
    _, view = tree.space("travel")
    assert view["big"] and view["title"] == "Путешествия"
    records = next(s for s in view["sections"] if s["kind"] == "records")
    assert len(records["items"]) < 100, "only the first SHOWCASE_MAX bytes were parsed"


def test_the_cache_follows_the_commit(tmp_path):
    work = make_base(tmp_path, BASE)
    tree = reader(work)
    head, _ = tree.spaces()
    assert tree.space("travel")[1] is tree.space("travel")[1], "cached for the same commit"
    (work / "travel/knowledge/new.md").write_text(KNOWLEDGE.replace("id: transit", "id: new"))
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "ещё")
    head2, view = tree.space("travel")
    assert head2 != head and "knowledge/new.md" in [r["path"] for r in view["records"]["knowledge"]]


def test_the_working_tree_is_never_read(tmp_path):
    work = make_base(tmp_path, BASE)
    (work / "travel/knowledge/transit.md").write_text("правка сессии, не закоммичена")
    _, record = reader(work).record("travel", "knowledge/transit.md")
    assert record["title"] == "Правила транзита"


def test_no_repository_is_unreadable(tmp_path):
    with pytest.raises(spaces.Unreadable):
        reader(tmp_path).spaces()


# --- the API -----------------------------------------------------------------------------------------------------------

class _Base:
    def __init__(self, tree: BaseTree) -> None:
        self.repo, self.env = tree.git, tree.env


def test_the_api_needs_the_login_and_reads_the_base(tmp_path):
    work = make_base(tmp_path, BASE)

    async def body(world: FaceWorld):
        assert (await world.call("GET", "/api/spaces"))[0] == 401, "nothing without a login"
        assert (await world.call("GET", "/api/spaces/travel/r/knowledge/transit.md"))[0] == 401
        await world.login()
        world.platform.base = None
        status, data, _ = await world.call("GET", "/api/spaces")
        assert (status, data["error"]) == (404, spaces.NO_BASE)

        world.platform.base = _Base(reader(work))
        store = world.store
        waiting = store.new_session("notes", "private", "private", 1.0, sphere="travel")
        working = store.new_session("notes", "private", "private", 2.0, sphere="travel")
        store.db.execute("UPDATE sessions SET state = 'waiting' WHERE id = ?", (waiting,))
        store.db.execute("UPDATE sessions SET state = 'active' WHERE id = ?", (working,))
        store.db.commit()
        listed = await world.get("/api/spaces")
        travel = next(s for s in listed["spaces"] if s["name"] == "travel")
        assert travel == {"name": "travel", "title": "Путешествия", "questions": 1, "tasks": 2, "waiting": 1,
                          "working": 1}
        assert next(s for s in listed["spaces"] if s["name"] == "home")["waiting"] == 0

        space = (await world.get("/api/spaces/travel"))["space"]
        assert space["name"] == "travel" and space["counts"] == {"questions": 1, "tasks": 2}
        record = (await world.get("/api/spaces/travel/r/profile/docs/passport.md"))["record"]
        assert record["head"]["written_by"] == "Владелец" and record["body"].strip() == "Паспорт."

        for path in ["/api/spaces/nope", "/api/spaces/travel/r/knowledge/link.md",
                     "/api/spaces/travel/r/knowledge%2F..%2F..%2Fscripts%2Flint.py",
                     "/api/spaces/travel/r/..%2F..%2FMAP.md", "/api/spaces/travel/r/.scratch/x.md"]:
            status, data, _ = await world.call("GET", path)
            assert status == 404 and data["error"], path

        world.platform.base = _Base(reader(tmp_path / "nowhere"))
        status, data, _ = await world.call("GET", "/api/spaces")
        assert (status, data["error"]) == (503, spaces.NOT_READY)

    faced(tmp_path, body)


def test_reading_runs_off_the_event_loop(tmp_path, monkeypatch):
    seen = []
    real = BaseTree._run

    def spy(self, *args, **kw):
        seen.append(_in_loop())
        return real(self, *args, **kw)

    monkeypatch.setattr(BaseTree, "_run", spy)
    work = make_base(tmp_path, BASE)

    async def body(world: FaceWorld):
        await world.login()
        world.platform.base = _Base(reader(work))
        await world.get("/api/spaces/travel")
        assert seen and not any(seen), "git runs in a worker thread"

    faced(tmp_path, body)


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
