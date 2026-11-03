"""A session's place on the face's map (`PlatformStore.take_slot`, docs/face.md «The map»): the platform's, not a
device's, so the phone and the laptop agree — the lowest place free in its project when it is made, kept while its
state changes, freed when it is archived, and nothing else moves."""

from __future__ import annotations

from retinue.sessions import PlatformStore


def store(tmp_path) -> PlatformStore:
    made = PlatformStore(str(tmp_path / "platform.sqlite"))
    # a session in the Base runs in its project's folder (`feed.Feed.session_project` sets this on the platform)
    made.project_of = lambda lane, track, cwd: cwd[len("base/"):] if cwd and cwd.startswith("base/") else None
    return made


def slots(s: PlatformStore, *ids: str) -> list[int | None]:
    return [s.face_session(i).get("slot") for i in ids]


def test_a_new_session_takes_the_lowest_free_slot_in_its_project(tmp_path):
    s = store(tmp_path)
    a = s.new_session("notes", "private", "private", 1.0, sphere="travel")
    b = s.new_session("notes", "private", "private", 2.0, sphere="travel")
    w = s.new_session("notes", "private", "private", 3.0, sphere="work")
    acme = s.new_session("notes", "private", "private", 4.0, sphere="work", cwd="base/work/acme")
    loose = s.new_session("notes", "private", "private", 5.0)
    assert slots(s, a, b, w, acme, loose) == [0, 1, 0, 0, 0], "each project counts its own places, a sub-project too"
    s.archive(a, 6.0)
    c = s.new_session("notes", "private", "private", 7.0, sphere="travel")
    assert slots(s, c, b) == [0, 1], "the lowest place free: the one the archived session left"
    for state in ("waiting", "idle", "closed", "active"):
        s.update_session(b, 8.0, state=state)
        assert slots(s, b) == [1], f"{state}: a state changing moves nothing"
    s.set_sphere(w, "travel")
    assert slots(s, w) == [2], "moved to another project: a place there"
    s.db.close()


def test_an_archived_session_frees_its_slot_and_nothing_else_moves(tmp_path):
    s = store(tmp_path)
    a, b, c = (s.new_session("notes", "private", "private", float(n), sphere="travel") for n in range(3))
    sub = s.new_session("notes", "private", "private", 3.0, sphere="travel", sub_of=b)
    assert slots(s, a, b, c, sub) == [0, 1, 2, 3]
    assert s.archive(b, 4.0) == [b, sub]
    assert slots(s, a, b, c, sub) == [0, None, 2, None], "its place and its kin's freed; the others where they were"
    d = s.new_session("notes", "private", "private", 5.0, sphere="travel")
    assert slots(s, d) == [1]
    s.unarchive(b)
    assert slots(s, a, c, d) == [0, 2, 1], "nothing else moves when they come back"
    assert sorted(slots(s, b, sub)) == [3, 4], "back on the map in the lowest places free then"
    s.db.close()
