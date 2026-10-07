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


def test_one_reply_covers_several_messages(tmp_path):
    archive = make(tmp_path)
    a, _ = archive.append(OWNER, "первый", conversation_id="c1", channel="telegram", native_id="1", ts=10.0)
    b, _ = archive.append(OWNER, "второй", conversation_id="c1", channel="telegram", native_id="2", ts=11.0)
    c, _ = archive.append(OWNER, "третий", conversation_id="c1", channel="telegram", native_id="3", ts=12.0)
    archive.append(ASSISTANT, "ответ на оба", conversation_id="c1", channel="telegram", ref=b.id,
                   meta={"covers": [a.id, b.id]})
    assert archive.answered(a.id) and archive.answered(b.id), "a merged reply answers every message in it"
    assert not archive.answered(c.id)
    archive.append(OWNER, "[фото] смотри", conversation_id="c1", channel="telegram", native_id="4", ts=13.0,
                   meta={"unsupported": "фото"})
    archive.append(OWNER, "[кнопка] Да", conversation_id="c1", channel="telegram", ref=c.id, ts=14.0)
    assert [e.id for e in archive.unanswered(0)] == [c.id], "a refused photo and a button are not questions"
    assert archive.unanswered(12.5) == []


def test_conversation_window_and_counts(tmp_path):
    archive = make(tmp_path)
    assert not archive.spoke("c1")
    archive.append(OWNER, "старое", conversation_id="c1", channel="telegram", ts=10.0)
    archive.append(ASSISTANT, "ответ", conversation_id="c1", channel="telegram", ts=20.0)
    archive.append(SYSTEM, "напоминание", conversation_id="c1", channel="system", ts=30.0)
    archive.append(OWNER, "другой разговор", conversation_id="c2", channel="telegram", ts=40.0)
    assert archive.spoke("c1") and not archive.spoke("c2")
    assert [e.text for e in archive.since("c1", 15.0, 10)] == ["ответ", "напоминание"]
    assert [e.text for e in archive.since("c1", 0, 1)] == ["напоминание"]
    assert (archive.count(ASSISTANT, 0), archive.count(OWNER, 15.0)) == (1, 1)


def test_attachments_keep_what_the_model_got(tmp_path):
    archive = make(tmp_path)
    photo = archive.attach("telegram:5", "photo", "фото 800×600", "своё", "", [("image/jpeg", b"jpeg-bytes")])
    voice = archive.attach("telegram:5", "voice", "голосовое 0:42", "переслано от Иван", "купи хлеб", [])
    assert photo.mark() == "[вложение #1: фото 800×600 · своё]" and voice.id == 2, "the number the model sees"
    assert [a.id for a in archive.attachments_of("telegram:5")] == [1, 2] and archive.attachments_of("x") == []
    assert archive.read(photo) == [("image/jpeg", b"jpeg-bytes")] and archive.read(voice) == []
    (stored,) = (tmp_path / "attachments").rglob("*.jpg")
    assert stored.parent.name == "telegram_5", "one folder per owner message, named by its archive id"
    reopened = make(tmp_path)
    assert reopened.attachment(2).text == "купи хлеб" and reopened.attachment(2).origin == "переслано от Иван"
    assert reopened.attachment(3) is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.db.execute("DELETE FROM attachments")
    stored.unlink()
    assert archive.read(photo) == [], "a file lost from the disk is skipped, not a crash"
    memory = Archive(":memory:")
    assert memory.read(memory.attach("e", "photo", "фото", "своё", "", [("image/jpeg", b"j")])) == [("image/jpeg", b"j")]
