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
    assert view.head[:3] == ["Permission · retinue · session 0badcafe", "Tool: Bash", "Folder: /work/retinue"]
    assert view.head[-1] == "Input, 29 characters:" and view.body == "curl https://example.com/?q=1"
    assert any("link https://example.com/?q=1 — with parameters" in line for line in view.head)
    assert actions == (approvals.APPROVE, approvals.REJECT)

    view, _ = approvals.card(approval("Write", {"file_path": "/work/a", "content": "x"}), "0badcafe", "retinue")
    assert view.body == '{\n  "file_path": "/work/a",\n  "content": "x"\n}'


def test_invisible_characters_are_shown_by_their_code():
    view, _ = approvals.card(approval("Bash", {"command": "echo safe‮;rm -rf ~"}), "0badcafe", "retinue")
    assert view.body == "echo safe⟦U+202E⟧;rm -rf ~"
    assert any("invisible characters" in line for line in view.head)


@pytest.mark.parametrize("size, open_only", [(3000, False), (approvals.OPEN_LIMIT, True)])
def test_a_card_over_one_message_offers_only_open(size, open_only):
    view, actions = approvals.card(approval("Bash", {"command": "a" * size}), "0badcafe", "retinue")
    assert (actions == (approvals.OPEN,)) is open_only
    assert len(view.plain()) <= approvals.OPEN_LIMIT
    assert ("a" * size in view.body) is not open_only


def test_the_courier_card_hands_the_whole_command_to_the_owner():
    view = approvals.courier_card(approval("Bash", {"command": "git push origin main"}), "0badcafe", "retinue",
                                  dict(approvals.NEVER)["Bash(git push:*)"])
    assert view.head == ["Courier · retinue · session 0badcafe", "Wants: push commits (git push)",
                         "Folder: /work/retinue"]
    assert view.body == "git push origin main"
    assert view.status == "Not running it: you do this yourself — the whole command is above."


def test_a_bash_card_shows_every_field_of_the_call_not_only_the_command():
    plain = {"tool": "Bash", "input": {"command": "make test", "description": "Run the tests"}}
    assert approvals.input_text(plain) == "make test"
    flagged = {"tool": "Bash", "input": {"command": "ls", "run_in_background": True, "dangerouslyDisableSandbox": True}}
    assert approvals.input_text(flagged).splitlines() == [
        "ls", "— run_in_background: true", "— dangerouslyDisableSandbox: true"]


def test_a_card_says_what_a_resume_after_the_prompt_cache_costs():
    assert approvals.idle_line(54 * 60 + 59, 120_000) is None, "the cache still holds"
    assert approvals.idle_line(55 * 60, 183_400) == \
        "The session is idle 55 min — resuming re-reads the context (~183k tokens) uncached"
    assert approvals.idle_line(3 * 3600 + 59, None) == \
        "The session is idle 3 h — resuming re-reads the context uncached", "no usage known: no number"
    assert approvals.idle_line(2 * 86400, 400) == \
        "The session is idle over a day — resuming re-reads the context uncached", "under 500: no «~0k»"
    assert [approvals.idle_words(s) for s in (3300, 7199, 7200, 86399, 86400)] == \
        ["55 min", "119 min", "2 h", "23 h", "over a day"]
    assert [approvals.idle_stamp(s) for s in (3300, 5399, 5400, 7199, 7200, 10800)] == \
        ["55 min", "55 min", "90 min", "90 min", "2 h", "3 h"], "past the threshold, at 90 min, then hourly"
    idle = approvals.with_idle({"head": ["Permission", "Input, 2 characters:"], "status": ""},
                               "The session is idle 55 min", "55 min")
    assert approvals.without_idle(idle) == {"head": ["Permission", "Input, 2 characters:"], "status": ""}, \
        "a decided card drops the line"
    old = {"head": ["Разрешение", "Сессия простаивает 55 мин — x", "Ввод, 2 знаков:"], "status": "", "idle": "55 мин"}
    assert approvals.without_idle(old) == {"head": ["Разрешение", "Ввод, 2 знаков:"], "status": ""}, \
        "a card stored before the switch to English drops its Russian line too"
    assert approvals.with_idle(old, "The session is idle 2 h — x", "2 h")["head"] == \
        ["Разрешение", "The session is idle 2 h — x", "Ввод, 2 знаков:"], "and the new line replaces it"


def test_the_idle_line_goes_before_the_line_that_names_the_input_and_replaces_an_older_one():
    view = {"head": ["Permission · retinue · session ab", "Tool: Bash", "Input, 9 characters:"], "body": "make test"}
    once = approvals.with_idle(view, "The session is idle 55 min — x", "min")
    assert once["head"] == ["Permission · retinue · session ab", "Tool: Bash", "The session is idle 55 min — x",
                            "Input, 9 characters:"] and once["idle"] == "min" and once["body"] == "make test"
    twice = approvals.with_idle(once, "The session is idle 2 h — x", "2 h")
    assert twice["head"].count("The session is idle 2 h — x") == 1 and len(twice["head"]) == 4
    assert view["head"][-1] == "Input, 9 characters:" and "idle" not in view, "the view it was given is not changed"
