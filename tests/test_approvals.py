"""Approvals: the digest a button is bound to, the file hash read through the lane's mount, the marks of a run, and
the cards the owner reads."""

from __future__ import annotations

import hashlib

import pytest

from retinue import approvals, cliengine
from retinue.config import LaneRef, Track

LANE = LaneRef(name="code", url="http://lane-code:9600", hook_url="http://platform:9500/hook",
               work_mount="/lanes/code", deny=["WebSearch", "Bash(curl:*)"])
TRACK = Track(name="retinue", lane="code", kind="code", cwd="retinue", allow=["Bash(make:*)", "Read"],
              deny=["Bash(git push:*)"])


def test_the_digest_changes_with_the_tool_the_input_the_directory_and_the_file():
    base = ("Edit", {"file_path": "/work/a.py", "old_string": "1", "new_string": "2"}, "/work/retinue", "h")
    others = ("Write", {"file_path": "/work/a.py", "old_string": "1", "new_string": "3"}, "/work", "h2")
    for i, other in enumerate(others):
        assert approvals.digest(*base[:i], other, *base[i + 1:]) != approvals.digest(*base)
    assert approvals.digest(*base) == approvals.digest("Edit", dict(base[1]), "/work/retinue", "h")


def test_the_file_hash_is_read_through_the_lanes_mount(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"print(1)\n")
    edit = lambda path: approvals.file_hash("Edit", {"file_path": path}, str(tmp_path))  # noqa: E731
    assert edit("/work/src/a.py") == hashlib.sha256(b"print(1)\n").hexdigest()
    assert edit("/work/src/new.py") == "absent"
    assert edit("/etc/x") == edit("/work/../etc/passwd") == edit("/work") == edit("src/a.py") == "outside"
    assert approvals.file_hash("NotebookEdit", {"notebook_path": "/work/src/a.py"}, str(tmp_path)) == \
        edit("/work/src/a.py")
    assert approvals.file_hash("Bash", {"command": "cat /work/src/a.py"}, str(tmp_path)) == ""


def test_a_link_is_followed_as_the_lane_follows_it(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"one")
    (tmp_path / "abs.py").symlink_to("/work/src/a.py")
    (tmp_path / "rel.py").symlink_to("src/a.py")
    (tmp_path / "srcdir").symlink_to("/work/src")
    edit = lambda path: approvals.file_hash("Edit", {"file_path": path}, str(tmp_path))  # noqa: E731
    before = edit("/work/abs.py")
    assert before == edit("/work/rel.py") == edit("/work/srcdir/a.py") == hashlib.sha256(b"one").hexdigest()
    (tmp_path / "src" / "a.py").write_bytes(b"two")
    assert edit("/work/abs.py") != before, "the hash follows the file the lane's link points at"


def test_a_links_absolute_target_is_walked_not_normalized(tmp_path):
    (tmp_path / "deep" / "dir").mkdir(parents=True)
    (tmp_path / "deep" / "a").write_bytes(b"deep")
    (tmp_path / "a").write_bytes(b"top")
    (tmp_path / "sub").symlink_to("deep/dir")
    (tmp_path / "abs").symlink_to("/work/sub/../a")
    assert approvals.file_hash("Edit", {"file_path": "/work/abs"}, str(tmp_path)) == \
        hashlib.sha256(b"deep").hexdigest()


def test_a_file_the_platform_cannot_read_binds_no_grant(tmp_path):
    (tmp_path / "escape").symlink_to("/etc")
    (tmp_path / "up").symlink_to("../x")
    (tmp_path / "loop").symlink_to("loop")
    (tmp_path / "secret.py").write_bytes(b"x")
    (tmp_path / "secret.py").chmod(0)
    for path in ("/work/escape/passwd", "/work/up", "/work/loop", "/work/secret.py"):
        with pytest.raises(approvals.Unbindable):
            approvals.file_hash("Write", {"file_path": path}, str(tmp_path))


def test_a_skill_loads_without_the_classifier_and_runs_nothing():
    """Its text only: inline shell is off in every lane's managed settings (docs/instructions.md)."""
    assert "Skill" in approvals.BASE_ALLOW
    assert approvals.SKILLS == "/lane/skills" and approvals.SKILLS_TOOLS == {"Read", "Grep", "Glob"}


def test_marks_cover_every_rule_once_strictest_first():
    marks = approvals.marks_for(TRACK, LANE)
    rules = [m.rule for m in marks]
    assert len(rules) == len(set(rules))
    kind = {m.rule: m.kind for m in marks}
    assert kind["Bash(git push:*)"] == "never", "a track cannot turn a never rule into a deny"
    assert kind["WebSearch"] == kind["Bash(curl:*)"] == "deny" and kind["Bash(make:*)"] == kind["Read"] == "allow"
    assert all(m.label for m in marks if m.kind == "never")
    assert set(rules) == {r for r, _ in approvals.NEVER} | {*approvals.BASE_ALLOW, *approvals.BASE_DENY, *approvals.WEB,
                                                            *LANE.deny, *TRACK.allow, *TRACK.deny}


def test_a_track_cannot_allow_what_is_denied_or_never():
    track = Track(name="retinue", lane="code", kind="code", cwd="retinue",
                  allow=["WebSearch", "Bash(curl:*)", "Bash(git push:*)"])
    kind = {m.rule: m.kind for m in approvals.marks_for(track, LANE)}
    assert (kind["WebSearch"], kind["Bash(curl:*)"], kind["Bash(git push:*)"]) == ("deny", "deny", "never")


def test_the_web_tools_are_denied_on_every_lane_but_a_web_one_whatever_its_deny_says():
    sealed = LaneRef(name="private", url="http://lane-private:9600", hook_url="http://platform:9500/hook",
                     work_mount="/lanes/private", deny=[])
    web = LaneRef(name="web", url="http://lane-web:9600", hook_url="http://platform:9500/hook", work_mount="/lanes/web",
                  web=True)
    research = Track(name="research", lane="web", kind="private", allow=["WebSearch", "WebFetch", "Edit(./**)"])
    kind = {m.rule: m.kind for m in approvals.marks_for(research, sealed)}
    assert kind["WebSearch"] == kind["WebFetch"] == "deny", "a track's allow never opens the web on a sealed lane"
    kind = {m.rule: m.kind for m in approvals.marks_for(research, web)}
    assert kind["WebSearch"] == kind["WebFetch"] == kind["Edit(./**)"] == "allow"
    assert {"CronCreate", "RemoteTrigger"} <= {rule for rule, k in kind.items() if k == "deny"}
    side = {m.rule: m.kind for m in approvals.marks_for(research, web, read_only=True)}
    assert side["WebFetch"] == "allow" and side["Edit"] == side["Write"] == side["Bash"] == "deny"


def test_a_card_rule_of_the_lane_or_the_track_is_a_card_mark_never_looser_than_a_deny():
    """A card rule beats an allow of the track (the owner sees it whatever the classifier would say); a deny or a
    never rule beats a card rule."""
    lane = LaneRef(name="code", url="http://lane-code:9600", hook_url="http://platform:9500/hook",
                   work_mount="/lanes/code", deny=["Bash(curl:*)"], card=["Bash(git commit:*)"])
    track = Track(name="retinue", lane="code", kind="code", allow=["Bash(git commit:*)", "Bash(make:*)"],
                  card=["Bash(make deploy:*)", "Bash(curl:*)", "Bash(git push:*)"])
    kind = {m.rule: m.kind for m in approvals.marks_for(track, lane)}
    assert kind["Bash(git commit:*)"] == kind["Bash(make deploy:*)"] == "card"
    assert (kind["Bash(curl:*)"], kind["Bash(git push:*)"], kind["Bash(make:*)"]) == ("deny", "never", "allow")
    assert approvals.BASE_CARD == [], "the Base commit button comes in its own part"
    assert {m.rule for m in approvals.marks_for(TRACK, LANE) if m.kind == "card"} == set()


def test_every_rule_in_code_is_one_the_cli_matches():
    for rule in [r for r, _ in approvals.NEVER] + approvals.BASE_ALLOW + approvals.BASE_DENY + approvals.WEB + \
            approvals.BASE_CARD:
        assert cliengine.rule_ok(rule), rule


def approval(tool: str, tool_input: dict) -> dict:
    return {"id": "a1b2c3d4", "tool": tool, "input": tool_input, "cwd": "/work/retinue"}


def test_the_card_shows_the_command_itself_and_what_is_in_it():
    view, actions = approvals.card(approval("Bash", {"command": "curl https://example.com/?q=1", "description": "x"}),
                                   "0badcafe", "retinue")
    assert view.head[:3] == ["Разрешение · retinue · сессия 0badcafe", "Инструмент: Bash", "Папка: /work/retinue"]
    assert view.head[-1] == "Ввод, 29 знаков:" and view.body == "curl https://example.com/?q=1"
    assert any("ссылка https://example.com/?q=1 — с параметрами" in line for line in view.head)
    assert actions == (approvals.APPROVE, approvals.REJECT)

    view, _ = approvals.card(approval("Write", {"file_path": "/work/a", "content": "x"}), "0badcafe", "retinue")
    assert view.body == '{\n  "file_path": "/work/a",\n  "content": "x"\n}'


def test_invisible_characters_are_shown_by_their_code():
    view, _ = approvals.card(approval("Bash", {"command": "echo safe‮;rm -rf ~"}), "0badcafe", "retinue")
    assert view.body == "echo safe⟦U+202E⟧;rm -rf ~"
    assert any("невидимые символы" in line for line in view.head)


@pytest.mark.parametrize("size, open_only", [(3000, False), (approvals.OPEN_LIMIT, True)])
def test_a_card_over_one_message_offers_only_open(size, open_only):
    view, actions = approvals.card(approval("Bash", {"command": "a" * size}), "0badcafe", "retinue")
    assert (actions == (approvals.OPEN,)) is open_only
    assert len(view.plain()) <= approvals.OPEN_LIMIT
    assert ("a" * size in view.body) is not open_only


def test_the_courier_card_hands_the_whole_command_to_the_owner():
    view = approvals.courier_card(approval("Bash", {"command": "git push origin main"}), "0badcafe", "retinue",
                                  dict(approvals.NEVER)["Bash(git push:*)"])
    assert view.head == ["Курьер · retinue · сессия 0badcafe", "Хочет: отправить коммиты (git push)",
                         "Папка: /work/retinue"]
    assert view.body == "git push origin main"
    assert view.status == "Не выполняю: это делаешь ты сам — команда выше целиком."


def test_a_bash_card_shows_every_field_of_the_call_not_only_the_command():
    plain = {"tool": "Bash", "input": {"command": "make test", "description": "Run the tests"}}
    assert approvals.input_text(plain) == "make test"
    flagged = {"tool": "Bash", "input": {"command": "ls", "run_in_background": True, "dangerouslyDisableSandbox": True}}
    assert approvals.input_text(flagged).splitlines() == [
        "ls", "— run_in_background: true", "— dangerouslyDisableSandbox: true"]


def test_a_card_says_what_a_resume_after_the_prompt_cache_costs():
    assert approvals.idle_line(54 * 60 + 59, 120_000) is None, "the cache still holds"
    assert approvals.idle_line(55 * 60, 183_400) == \
        "Сессия простаивает 55 мин — продолжение перечитает контекст (~183 тыс. токенов) без кэша"
    assert approvals.idle_line(3 * 3600 + 59, None) == \
        "Сессия простаивает 3 ч — продолжение перечитает контекст без кэша", "no usage known: no number"
    assert approvals.idle_line(2 * 86400, 400) == \
        "Сессия простаивает больше суток — продолжение перечитает контекст без кэша", "under 500: no «~0 тыс.»"
    assert [approvals.idle_words(s) for s in (3300, 7199, 7200, 86399, 86400)] == \
        ["55 мин", "119 мин", "2 ч", "23 ч", "больше суток"]
    assert [approvals.idle_stamp(s) for s in (3300, 5399, 5400, 7199, 7200, 10800)] == \
        ["55 мин", "55 мин", "90 мин", "90 мин", "2 ч", "3 ч"], "past the threshold, at 90 min, then hourly"
    idle = approvals.with_idle({"head": ["Разрешение", "Ввод, 2 знаков:"], "status": ""}, "Сессия простаивает 55 мин",
                               "55 мин")
    assert approvals.without_idle(idle) == {"head": ["Разрешение", "Ввод, 2 знаков:"], "status": ""}, \
        "a decided card drops the line"


def test_the_idle_line_goes_before_the_line_that_names_the_input_and_replaces_an_older_one():
    view = {"head": ["Разрешение · retinue · сессия ab", "Инструмент: Bash", "Ввод, 9 знаков:"], "body": "make test"}
    once = approvals.with_idle(view, "Сессия простаивает 55 мин — x", "мин")
    assert once["head"] == ["Разрешение · retinue · сессия ab", "Инструмент: Bash", "Сессия простаивает 55 мин — x",
                            "Ввод, 9 знаков:"] and once["idle"] == "мин" and once["body"] == "make test"
    twice = approvals.with_idle(once, "Сессия простаивает 2 ч — x", "2 ч")
    assert twice["head"].count("Сессия простаивает 2 ч — x") == 1 and len(twice["head"]) == 4
    assert view["head"][-1] == "Ввод, 9 знаков:" and "idle" not in view, "the view it was given is not changed"
