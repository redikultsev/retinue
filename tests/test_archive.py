"""Archive: append-only events, ids from content, recent turns of a conversation, full-text search."""

import sqlite3

import pytest

from retinue.archive import ASSISTANT, OWNER, SYSTEM, Archive


def make(tmp_path):
    return Archive(str(tmp_path / "archive.sqlite"))


def test_append_only_and_ids(tmp_path):
    archive = make(tmp_path)
    first, fresh = archive.append(OWNER, "привет", conversation_id="c1", channel="telegram", native_id="10")
    assert fresh and first.id == "telegram:10", "the messenger's own id is the archive id"
    again, fresh = archive.append(OWNER, "привет", conversation_id="c1", channel="telegram", native_id="10")
    assert not fresh and again.id == first.id and again.ts == first.ts, "a second delivery adds nothing"
    reply, _ = archive.append(ASSISTANT, "здравствуй", conversation_id="c1", channel="telegram", ref=first.id, ts=5.0)
    same, _ = archive.append(ASSISTANT, "здравствуй", conversation_id="c1", channel="telegram", ref=first.id, ts=6.0)
    assert len(reply.id) == 20 and reply.id != same.id, "without a native id the id is a hash of kind, time, text"
    assert archive.get(reply.id).ref == first.id
    assert archive.answered(first.id) and not archive.answered(reply.id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.db.execute("UPDATE events SET text = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.db.execute("DELETE FROM events")
    with pytest.raises(ValueError):
        archive.append("robot", "x", conversation_id="c1", channel="telegram")


def test_survives_reopen(tmp_path):
    make(tmp_path).append(OWNER, "запомни: Ереван", conversation_id="c1", channel="telegram", meta={"a": 1})
    archive = make(tmp_path)
    assert archive.coverage()[0] == 1
    assert archive.search("ереван")[0][0].meta == {"a": 1}


def test_recent_leaves_out_the_queue(tmp_path):
    archive = make(tmp_path)
    a, _ = archive.append(OWNER, "первый", conversation_id="c1", channel="telegram", native_id="1")
    b, _ = archive.append(OWNER, "второй", conversation_id="c1", channel="telegram", native_id="2")
    archive.append(OWNER, "третий", conversation_id="c1", channel="telegram", native_id="3")
    archive.append(ASSISTANT, "ответ на первый", conversation_id="c1", channel="telegram", ref=a.id)
    archive.append(SYSTEM, "другой разговор", conversation_id="c2", channel="telegram")
    assert [e.text for e in archive.recent("c1", 10, before=b.id)] == ["первый", "ответ на первый"]
    assert [e.text for e in archive.recent("c1", 2)] == ["третий", "ответ на первый"]
    assert archive.recent("c1", 10, before=a.id)[0].text == "ответ на первый"


def test_search(tmp_path):
    archive = make(tmp_path)
    advice, _ = archive.append(ASSISTANT, "По Еревану советую: Каскад, Матенадаран и вернисаж в выходные.",
                               conversation_id="c1", channel="telegram", ts=100.0)
    archive.append(OWNER, "Найди билеты в Черногорию", conversation_id="c1", channel="telegram", ts=200.0)
    question, _ = archive.append(OWNER, "Что ты советовала по Еревану?", conversation_id="c2", channel="telegram",
                                 ts=300.0)
    hits = archive.search("Ереван", exclude=question.id)
    assert [e.id for e, _ in hits] == [advice.id], "the question itself is not an answer"
    assert "Каскад" in hits[0][1]
    assert archive.search("что советовала по еревану", exclude=question.id)[0][0].id == advice.id, "endings differ"
    assert archive.search("ЧЕРНОГОРИЯ")[0][0].text.startswith("Найди билеты"), "case and endings do not matter"
    assert archive.search("самолёт") == [] and archive.search('"" ( *') == [] and archive.search("") == []
    count, first, last = archive.coverage()
    assert (count, first, last) == (3, 100.0, 300.0)
