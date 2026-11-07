"""«What's new»: which releases' lines the owner is shown after an update (retinue/changelog.py)."""

from retinue import changelog

TEXT = """# What's new

## Unreleased

- Not out yet.

## v0.3.0 — 2026-12-01

- Three.
  Carried on.

## v0.2.0 — 2026-11-20

## v0.1.0 — 2026-11-07

- One.
* Also one.
"""


def versions(shown):
    return [r["version"] for r in shown]


def test_the_released_sections_are_read_as_written_and_unreleased_is_not():
    assert changelog.releases(TEXT) == [
        {"version": "v0.3.0", "date": "2026-12-01", "notes": ["Three. Carried on."]},
        {"version": "v0.2.0", "date": "2026-11-20", "notes": []},
        {"version": "v0.1.0", "date": "2026-11-07", "notes": ["One.", "Also one."]},
    ]


def test_an_update_shows_every_release_after_the_one_seen_up_to_the_one_running_newest_first():
    assert versions(changelog.unseen("v0.3.0", "v0.1.0", TEXT)) == ["v0.3.0"], "v0.2.0 has no lines"
    assert versions(changelog.unseen("v0.3.0", "v0.0.9", TEXT)) == ["v0.3.0", "v0.1.0"]
    assert versions(changelog.unseen("v0.2.5", "v0.0.9", TEXT)) == ["v0.1.0"], "nothing past the one running"


def test_nothing_is_shown_once_seen_on_a_checkout_build_or_for_a_release_with_no_section():
    assert changelog.unseen("v0.3.0", "v0.3.0", TEXT) == []
    assert changelog.unseen("v0.1.0", "v0.3.0", TEXT) == [], "a rollback shows nothing"
    assert changelog.unseen("local", None, TEXT) == []
    assert changelog.unseen("v9.0.0", None, TEXT) == []


def test_an_install_that_saw_nothing_yet_is_shown_the_release_it_runs_only():
    assert versions(changelog.unseen("v0.3.0", None, TEXT)) == ["v0.3.0"]
    assert versions(changelog.unseen("v0.3.0", "garbage", TEXT)) == ["v0.3.0"]


def test_the_seen_mark_only_moves_forward_and_versions_compare_as_numbers():
    assert changelog.later("v0.10.0", "v0.9.0") and changelog.later("v0.1.0", None)
    assert not changelog.later("v0.1.0", "v0.1.0") and not changelog.later("v0.1.0", "v0.2.0")
    assert not changelog.later("local", None)


def test_the_running_release_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("RETINUE_VERSION", raising=False)
    assert changelog.running() == "local"
    monkeypatch.setenv("RETINUE_VERSION", "v0.1.0")
    assert changelog.running() == "v0.1.0"


def test_the_repository_changelog_reads():
    text = changelog.read()
    assert "## Unreleased" in text
    assert all(changelog.RELEASE.fullmatch(r["version"]) for r in changelog.releases(text))
