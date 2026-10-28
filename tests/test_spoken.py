"""The voice marks come off wherever the owner's words become a name: a session's title, a commit subject, a pull
request's title (`test_topush` replies with the English mark)."""

from retinue.sessions import PlatformStore
from retinue.spoken import SPOKEN_MARK, VERBATIM_MARK, unmarked
from retinue.tobase import subject_of


def test_both_marks_come_off_and_nothing_else():
    assert unmarked("(голосом) Почини вход") == unmarked("(голосом, дословно) Почини вход") == "Почини вход"
    assert unmarked("Почини (голосом) вход") == "Почини (голосом) вход", "only at the start"
    assert unmarked("(голосом, дословно) (голосом) x") == "(голосом) x", "one mark, once"
    assert subject_of(VERBATIM_MARK + "So, um, fix the login") == "So, um, fix the login"
    assert subject_of(SPOKEN_MARK + "Почини вход") == "Почини вход"


def test_a_sessions_title_is_his_words_without_the_mark():
    store = PlatformStore(":memory:")
    for n, mark in enumerate((SPOKEN_MARK, VERBATIM_MARK, "")):
        session = store.new_session("notes", "private", "private", 1.0)
        store.add_input(f"face:{n}", session, mark + "Hello there", 1.0)
        assert store.face_session(session)["title"] == "Hello there"
