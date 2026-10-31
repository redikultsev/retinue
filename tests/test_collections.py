"""The collections' store: one thread with one connection, a lock past its wait refused at once, a file kept once by
its SHA-256 however often it comes, written before its row."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
import time

import pytest

from retinue import collections
from retinue.collections import Collections, Full, Refused, Rejected, Ulids, Writer, clean_mime, clean_name


def run(body):
    return asyncio.run(body())


def test_every_call_goes_through_one_thread_with_one_connection(tmp_path, monkeypatch):
    opened = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        opened.append((threading.get_ident(), kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(collections.sqlite3, "connect", connect)
    store = Collections(str(tmp_path / "collections.sqlite"))
    assert not (tmp_path / "collections.sqlite").exists(), "nothing opened before the first call"

    async def body():
        await store.open()
        await asyncio.gather(*(store.put_file(f"file {n}".encode(), f"{n}.txt", "text/plain", n) for n in range(8)))
        await store.files()
        await store.file("0" * 64)
        await store.close()

    run(body)
    assert len(opened) == 1, "one connection for every read and write"
    (thread, kwargs), = opened
    assert thread == store.thread and thread != threading.get_ident(), "opened in the store's thread, not the loop's"
    assert kwargs["isolation_level"] is None and kwargs["timeout"] == collections.BUSY_S
    assert (tmp_path / "attachments").is_dir()


def test_a_lock_past_the_wait_is_refused_at_once_and_nothing_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "BUSY_S", 0.2)
    path = tmp_path / "collections.sqlite"
    store = Collections(str(path))

    async def body():
        await store.open()
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN EXCLUSIVE")  # an import, or anything else holding the file
        started = time.monotonic()
        with pytest.raises(Refused, match="try again"):
            await store.put_file(b"%PDF-1.7", "a.pdf", "application/pdf", 1)
        assert time.monotonic() - started < 2, "refused after the short wait, not 30 s"
        with pytest.raises(Refused):
            await store.files()
        other.execute("ROLLBACK")
        reader = sqlite3.connect(path, isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM files").fetchall()  # the night's copy holds a shared lock: a commit cannot pass
        with pytest.raises(Refused):
            await store.put_file(b"%PDF-1.7", "a.pdf", "application/pdf", 2)
        reader.execute("ROLLBACK")
        assert await store.files() == [], "the refused write left nothing, and the thread is not stuck in it"
        row, new = await store.put_file(b"%PDF-1.7", "a.pdf", "application/pdf", 3)
        assert new and row["created"] == 3, "the same call again goes through"
        await store.close()

    run(body)


def test_the_same_bytes_are_one_file_kept_by_their_sha256(tmp_path):
    store = Collections(str(tmp_path / "collections.sqlite"))
    data = b"date,amount\n2026-01-02,-12.50\n"
    sha = hashlib.sha256(data).hexdigest()

    async def body():
        first, new = await store.put_file(data, "export.csv", "text/csv", 10)
        assert new and first == {"sha256": sha, "name": "export.csv", "size": len(data), "mime": "text/csv",
                                 "created": 10}
        again, new = await store.put_file(data, "export (1).csv", "text/csv", 20)
        assert not new and again == first, "the second upload changes nothing: the first name and time stay"
        assert (tmp_path / "attachments" / sha).read_bytes() == data
        assert list((tmp_path / "attachments").iterdir()) == [tmp_path / "attachments" / sha]
        await store.put_file(b"\x89PNG", "IMG_0001.PNG", "image/png", 30)
        assert [f["name"] for f in await store.files()] == ["IMG_0001.PNG", "export.csv"], "the newest first"
        found, path = await store.file(sha)
        assert found == first and path == tmp_path / "attachments" / sha
        assert await store.file("../" + sha[3:]) is None and await store.file("f" * 64) is None
        with pytest.raises(ValueError):
            await store.put_file(b"", "empty", "text/plain", 1)
        with pytest.raises(ValueError):
            await store.put_file(b"x" * (collections.FILE_MAX + 1), "big", "text/plain", 1)
        await store.close()

    run(body)


def test_a_blob_without_its_row_is_written_again_and_a_cut_one_is_swept(tmp_path):
    """A crash between the blob and its row leaves a blob no row names: the next upload of it just adds the row.
    A blob cut while it was written is a `.part` the next start removes."""
    blobs = tmp_path / "attachments"
    blobs.mkdir()
    data = b"receipt"
    sha = hashlib.sha256(data).hexdigest()
    (blobs / sha).write_bytes(b"rec")  # torn: the wrong size
    (blobs / f".{sha}.0000.part").write_bytes(b"re")
    store = Collections(str(tmp_path / "collections.sqlite"))

    async def body():
        await store.open()
        assert not list(blobs.glob(".*.part"))
        row, new = await store.put_file(data, "receipt.jpg", "image/jpeg", 1)
        assert new and (blobs / sha).read_bytes() == data
        await store.close()

    run(body)


def test_a_lost_blob_is_put_back_by_the_same_bytes(tmp_path):
    """A row whose blob is gone (a partial restore, a delete by hand): uploading the same bytes writes it again."""
    store = Collections(str(tmp_path / "collections.sqlite"))
    data = b"invoice"
    blob = tmp_path / "attachments" / hashlib.sha256(data).hexdigest()

    async def body():
        first, _ = await store.put_file(data, "invoice.pdf", "application/pdf", 1)
        blob.unlink()
        again, new = await store.put_file(data, "invoice.pdf", "application/pdf", 2)
        assert not new and again == first and blob.read_bytes() == data
        blob.write_bytes(b"inv")  # the wrong size
        await store.put_file(data, "invoice.pdf", "application/pdf", 3)
        assert blob.read_bytes() == data
        await store.close()

    run(body)


def test_a_failed_write_or_a_nearly_full_disk_keeps_nothing(tmp_path, monkeypatch):
    store = Collections(str(tmp_path / "collections.sqlite"))
    blobs = tmp_path / "attachments"

    def fsync(fd):
        raise OSError(28, "No space left on device")

    async def body():
        await store.open()
        with monkeypatch.context() as patch:
            patch.setattr(collections.os, "fsync", fsync)
            with pytest.raises(OSError):
                await store.put_file(b"a statement", "a.pdf", "application/pdf", 1)
        assert not list(blobs.iterdir()), "the cut .part is removed at once, not at the next start"
        with monkeypatch.context() as patch:
            patch.setattr(collections, "FREE_MIN", 2**62)
            with pytest.raises(Full, match="nearly full"):
                await store.put_file(b"a statement", "a.pdf", "application/pdf", 2)
        assert not list(blobs.iterdir()) and await store.files() == []
        await store.close()
        with pytest.raises(Refused, match="try again"):  # an upload still on its way while the platform stops
            await store.put_file(b"late", "late.txt", "text/plain", 3)

    run(body)


def test_names_and_types_are_kept_clean():
    assert clean_name("C:\\Users\\x\\statement.pdf") == "statement.pdf"
    assert clean_name("../../etc/passwd") == "passwd"
    assert clean_name("bill\u202egpj.exe") == "billgpj.exe", "no bidi override to disguise a name"
    assert clean_name("a\x00b\nc.txt") == "abc.txt"
    assert clean_name("caf\udcc3\udca9.txt") == "caf.txt", "a header's raw bytes that were not UTF-8: no lone surrogate"
    assert clean_name("\ue000x.txt") == "x.txt"
    assert clean_name("  ") == "file" and clean_name("") == "file"
    assert len(clean_name("x" * 500)) == collections.NAME_MAX
    assert clean_name("Отчёт за март.pdf") == "Отчёт за март.pdf"
    assert clean_mime("Application/PDF; charset=binary") == "application/pdf"
    assert clean_mime("text/html<script>") == "application/octet-stream"
    assert clean_mime("") == "application/octet-stream"


# --- records ------------------------------------------------------------------------------------------------------

OWNER = Writer("owner", "screen")
SESSION = Writer("session:abc123", "session", model="claude-example", foreign=("mail",))
LEADS = {"fields": {
    "n": {"type": "int"},
    "company": {"type": "text", "required": True},
    "role": {"type": "text"},
    "stage": {"type": "enum", "values": ["evaluated", "applied", "interview", "closed"]},
    "seen": {"type": "date"},
    "remote": {"type": "bool"},
}}
NOTES = {"fields": {"lead": {"type": "ref", "to": "leads"}, "text": {"type": "text"}}}


async def with_leads(tmp_path) -> Collections:
    store = Collections(str(tmp_path / "collections.sqlite"))
    await store.define(OWNER, "leads", "career", LEADS, 1)
    await store.define(OWNER, "notes", "career", NOTES, 1)
    return store


async def sql(store: Collections, query: str) -> list:
    """A query on the store's own thread, as everything that touches its connection runs."""
    return await store._run(lambda: store._db.execute(query).fetchall())


def add(fields: dict, **extra) -> dict:
    return {"op": "add", "fields": fields, **extra}


def test_a_record_is_added_set_and_kept_with_its_change(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        done = await store.apply(SESSION, "leads", [add({"company": "Acme", "role": "Backend", "stage": "evaluated",
                                                         "n": 7, "remote": True, "seen": "2026-09-01"},
                                                        src="web:https://jobs.example.com/acme/1")], 100)
        (record,), (cid,) = done["records"], done["change_ids"]
        assert collections.ULID.fullmatch(record["id"]) and record["version"] == 1 and record["deleted"] is None
        assert record["fields"] == {"company": "Acme", "role": "Backend", "stage": "evaluated", "n": 7,
                                    "remote": True, "seen": "2026-09-01"}
        assert done["name"] == "leads" and done["rev"] == cid, "the event's rev is the collection's newest change"
        assert await store.get("leads", record["id"]) == record

        done = await store.apply(OWNER, "leads", [{"op": "set", "id": record["id"], "version": 1, "as_of": "2026-09-03",
                                                   "fields": {"stage": "applied", "role": None}}], 200)
        after = done["records"][0]
        assert after["version"] == 2 and after["updated"] == 200 and after["created"] == 100
        assert after["fields"] == {"company": "Acme", "stage": "applied", "n": 7, "remote": True, "seen": "2026-09-01"}, \
            "null makes a field unknown: it is left out"
        set_, add_ = await store.history("leads", record["id"])
        assert set_["op"] == "set" and set_["before"] == {"stage": "evaluated", "role": "Backend"}
        assert set_["after"] == {"stage": "applied", "role": None} and set_["as_of"] == "2026-09-03"
        assert (set_["actor"], set_["via"], set_["model"], set_["foreign"]) == ("owner", "screen", None, [])
        assert add_["op"] == "add" and add_["before"] is None and add_["after"] == record["fields"]
        assert (add_["actor"], add_["via"], add_["model"], add_["foreign"]) == \
            ("session:abc123", "session", "claude-example", ["mail"])
        assert add_["src"] == "web:https://jobs.example.com/acme/1" and add_["as_of"] is None
        assert "seq" not in add_, "the file's own order is never shown"
        await store.close()

    run(body)


def test_a_set_that_changes_nothing_writes_nothing(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        done = await store.apply(OWNER, "leads", [add({"company": "Acme", "stage": "applied"})], 1)
        rid, rev = done["records"][0]["id"], done["rev"]
        again = await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 1,
                                                    "fields": {"stage": "applied", "role": None}}], 2)
        assert again["change_ids"] == [None] and again["rev"] == rev, "already so"
        assert again["records"][0]["version"] == 1 and len(await store.history("leads", rid)) == 1
        gone = await store.apply(OWNER, "leads", [{"op": "delete", "id": rid, "version": 1}], 3)
        twice = await store.apply(OWNER, "leads", [{"op": "delete", "id": rid, "version": 2}], 4)
        assert gone["change_ids"][0] and twice["change_ids"] == [None]
        await store.close()

    run(body)


@pytest.mark.parametrize("op, reason", [
    (add({"company": "Acme", "salary": 1}), "leads has no field 'salary'; its fields: n, company, role"),
    (add({"company": 5}), "'company' is text, a string"),
    (add({"company": "Acme", "n": 1.5}), "'n' is a whole number"),
    (add({"company": "Acme", "n": True}), "'n' is a whole number"),
    (add({"company": "Acme", "n": 2**60}), "'n' is a whole number"),
    (add({"company": "Acme", "stage": "hired"}), "'stage' is one of evaluated, applied, interview, closed: not 'hired'"),
    (add({"company": "Acme", "seen": "2026-02-30"}), "'seen' is a date, YYYY-MM-DD"),
    (add({"company": "Acme", "seen": "1 Sep 2026"}), "'seen' is a date"),
    (add({"company": "Acme", "remote": 1}), "'remote' is true or false"),
    (add({"company": "Acme", "role": "x" * 2001}), "at most 2000 characters: this is 2001"),
    (add({"role": "Backend"}), "'company' is required"),
    (add({"company": None}), "'company' is required"),
    (add({}), "fields is an object of at least one field"),
    (add({"company": "Acme"}, as_of="yesterday"), "as_of is the date the world became so"),
    (add({"company": "Acme"}, src="https://example.com"), "src is mail:"),
    (add({"company": "Acme"}, src="mail:a/\u202eb"), "src is mail:"),
    (add({"company": "Acme"}, version=1), "add takes"),
    ({"op": "merge"}, '"op" is one of add, set, delete, restore, revert'),
    ("add", '"op" is one of'),
])
def test_a_wrong_field_or_value_is_rejected_with_its_reason(tmp_path, op, reason):
    async def body():
        store = await with_leads(tmp_path)
        with pytest.raises(Rejected) as caught:
            await store.apply(OWNER, "leads", [add({"company": "Acme"}), op], 1)
        assert caught.value.reason.startswith("ops[1]: ") and reason in caught.value.reason, caught.value.reason
        assert await sql(store, "SELECT count(*) FROM records") == [(0,)], "the valid first op is not kept"
        assert await sql(store, "SELECT count(*) FROM changes WHERE op != 'define'") == [(0,)]
        await store.close()

    run(body)


def test_text_is_kept_without_control_or_bidi_characters(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        done = await store.apply(OWNER, "leads", [add({"company": "Ac\u202eme\x00", "role": "line one\nline two\u200b"})], 1)
        assert done["records"][0]["fields"] == {"company": "Acme", "role": "line one\nline two"}
        await store.close()

    run(body)


def test_a_call_the_store_cannot_place_is_rejected(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        with pytest.raises(Rejected, match="no collection 'people'"):
            await store.apply(OWNER, "people", [add({"name": "Anna Petrova"})], 1)
        with pytest.raises(Rejected, match="1 to 200"):
            await store.apply(OWNER, "leads", [], 1)
        with pytest.raises(Rejected, match="1 to 200"):
            await store.apply(OWNER, "leads", [add({"company": "Acme"})] * 201, 1)
        for writer in (Writer("root", "screen"), Writer("session:a b", "session"), Writer("owner", "telegram"),
                       Writer("owner", "screen", model="x\ny"), Writer("owner", "screen", foreign=("Mail!",))):
            with pytest.raises(Rejected):
                await store.apply(writer, "leads", [add({"company": "Acme"})], 1)
        assert await sql(store, "SELECT count(*) FROM records") == [(0,)]
        await store.close()

    run(body)


def test_a_record_too_big_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "RECORD_MAX", 1024)
    async def body():
        store = await with_leads(tmp_path)
        rid = (await store.apply(OWNER, "leads", [add({"company": "é" * 500})], 1))["records"][0]["id"]
        with pytest.raises(Rejected, match="at most 1 KB"):
            await store.apply(OWNER, "leads", [add({"company": "é" * 600})], 2)
        with pytest.raises(Rejected, match="at most 1 KB"):
            await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 1, "fields": {"role": "x" * 100}}], 2)
        await store.close()

    run(body)


def test_a_version_that_is_not_the_records_comes_back_with_the_record(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        rid = (await store.apply(OWNER, "leads", [add({"company": "Acme", "stage": "evaluated"})], 1))["records"][0]["id"]
        await store.apply(SESSION, "leads", [{"op": "set", "id": rid, "version": 1, "fields": {"stage": "applied"}}], 2)
        for version in (1, None, True, "2"):
            with pytest.raises(Rejected, match="is at version 2") as caught:
                await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": version,
                                                    "fields": {"stage": "closed"}}], 3)
            assert caught.value.record["fields"]["stage"] == "applied" and caught.value.record["version"] == 2
            assert caught.value.change["actor"] == "session:abc123" and caught.value.change["after"] == {"stage": "applied"}
        with pytest.raises(Rejected, match="no record 'nope'"):
            await store.apply(OWNER, "leads", [{"op": "set", "id": "nope", "version": 1, "fields": {"stage": "closed"}}], 3)
        done = await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 2, "fields": {"stage": "closed"}},
                                                  {"op": "set", "id": rid, "version": 3, "fields": {"n": 4}}], 4)
        assert [r["version"] for r in done["records"]] == [3, 4], "ops in one call see each other"
        await store.close()

    run(body)


def test_delete_is_soft_and_restore_brings_the_record_back(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        rid = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        gone = (await store.apply(OWNER, "leads", [{"op": "delete", "id": rid, "version": 1}], 2))["records"][0]
        assert gone["deleted"] == 2 and gone["fields"] == {"company": "Acme"} and gone["version"] == 2
        with pytest.raises(Rejected, match="is deleted: restore it first"):
            await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 2, "fields": {"n": 1}}], 3)
        with pytest.raises(Rejected, match="no such record"):
            await store.apply(OWNER, "notes", [add({"lead": rid})], 3)
        back = (await store.apply(OWNER, "leads", [{"op": "restore", "id": rid, "version": 2}], 4))["records"][0]
        assert back["deleted"] is None and back["version"] == 3
        restore, delete, _ = await store.history("leads", rid)
        assert (delete["before"], delete["after"]) == ({"_deleted": None}, {"_deleted": 2})
        assert (restore["before"], restore["after"]) == ({"_deleted": 2}, {"_deleted": None})
        note = await store.apply(OWNER, "notes", [add({"lead": rid, "text": "Spoke to Anna Petrova"})], 5)
        assert note["records"][0]["fields"]["lead"] == rid
        with pytest.raises(Rejected, match="there is no such record as 'x'"):
            await store.apply(OWNER, "notes", [add({"lead": "x"})], 6)
        await store.close()

    run(body)


def test_a_revert_takes_a_change_back_unless_something_changed_it_since(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        rid = (await store.apply(OWNER, "leads", [add({"company": "Acme", "stage": "evaluated"})], 1))["records"][0]["id"]
        stage = (await store.apply(SESSION, "leads", [{"op": "set", "id": rid, "version": 1,
                                                       "fields": {"stage": "closed"}}], 2))["change_ids"][0]
        role = (await store.apply(SESSION, "leads", [{"op": "set", "id": rid, "version": 2,
                                                      "fields": {"role": "Backend"}}], 3))["change_ids"][0]
        await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 3, "fields": {"role": "Platform"}}], 4)
        with pytest.raises(Rejected, match="changed after it: role by owner") as caught:
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": role, "version": 4}], 5)
        assert caught.value.change["after"] == {"role": "Platform"}, "the card names who changed it, and to what"
        done = await store.apply(OWNER, "leads", [{"op": "revert", "change_id": stage, "version": 4}], 6)
        record, revert = done["records"][0], (await store.history("leads", rid, 1))[0]
        assert record["fields"] == {"company": "Acme", "stage": "evaluated", "role": "Platform"}, "only its field"
        assert (revert["op"], revert["reverts"], revert["before"], revert["after"]) == \
            ("revert", stage, {"stage": "closed"}, {"stage": "evaluated"})
        again = await store.apply(OWNER, "leads", [{"op": "revert", "change_id": revert["change_id"], "version": 5}], 7)
        assert again["records"][0]["fields"]["stage"] == "closed", "an undo is undone the same way"
        with pytest.raises(Rejected, match="changed after it: stage"):
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": stage, "version": 6}], 8)
        with pytest.raises(Rejected, match="change not found"):
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": "0" * 26, "version": 6}], 8)
        with pytest.raises(Rejected, match="change not found"):
            await store.apply(OWNER, "notes", [{"op": "revert", "change_id": stage, "version": 6}], 8)
        with pytest.raises(Rejected, match="is at version 6"):
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": role}], 8)
        await store.close()

    run(body)


def test_reverting_an_add_deletes_the_record_and_a_delete_restores_it(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        done = await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1)
        rid, added = done["records"][0]["id"], done["change_ids"][0]
        gone = (await store.apply(OWNER, "leads", [{"op": "revert", "change_id": added, "version": 1}], 2))["records"][0]
        assert gone["deleted"] == 2
        undo = (await store.history("leads", rid, 1))[0]["change_id"]
        back = (await store.apply(OWNER, "leads", [{"op": "revert", "change_id": undo, "version": 2}], 3))["records"][0]
        assert back["deleted"] is None
        with pytest.raises(Rejected, match="changed after it: deleted"):
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": added, "version": 3}], 4)
        other = (await store.apply(OWNER, "leads", [add({"company": "Example Ltd"})], 5))["records"][0]["id"]
        delete = (await store.apply(OWNER, "leads", [{"op": "delete", "id": other, "version": 1}], 6))["change_ids"][0]
        back = (await store.apply(OWNER, "leads", [{"op": "revert", "change_id": delete, "version": 2}], 7))["records"][0]
        assert back["deleted"] is None and back["version"] == 3
        await store.close()

    run(body)


def test_reverting_a_set_to_a_required_value_it_did_not_have_is_rejected(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        rid = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        await store.define(OWNER, "leads", "career", {"fields": {**LEADS["fields"],
                                                                 "origin": {"type": "enum", "values": ["inbound"]}}}, 2)
        cid = (await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 1,
                                                   "fields": {"origin": "inbound"}}], 3))["change_ids"][0]
        await store.define(OWNER, "leads", "career", {"fields": {**LEADS["fields"], "origin": {
            "type": "enum", "values": ["inbound"], "required": True}}}, 4)
        with pytest.raises(Rejected, match="'origin' is required"):
            await store.apply(OWNER, "leads", [{"op": "revert", "change_id": cid, "version": 2}], 5)
        await store.close()

    run(body)


def test_changes_are_append_only(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1)
        await store.close()

    run(body)
    db = sqlite3.connect(tmp_path / "collections.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE changes SET actor = 'owner'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM changes")
    assert db.execute("SELECT count(*) FROM changes").fetchone() == (3,), "two defines and the add"


def test_ulids_only_grow(tmp_path):
    ulids = Ulids()
    made = [ulids.next(1000.0) for _ in range(1000)] + [ulids.next(999.0), ulids.next(1000.5)]
    assert made == sorted(made) and len(set(made)) == len(made), "the same millisecond, a clock that stepped back"
    assert all(collections.ULID.fullmatch(u) for u in made)
    assert Ulids(made[-1]).last == ulids.last and Ulids(made[-1]).next(0) > made[-1]
    assert ulids.next(1_700_000_000)[:10] == "01HF7YAT00", "the time first, in Crockford's base 32"

    async def first():
        store = await with_leads(tmp_path)
        cid = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 2_000_000_000))["change_ids"][0]
        await store.close()
        return cid

    last = asyncio.run(first())
    reopened = Collections(str(tmp_path / "collections.sqlite"))

    async def second():
        cid = (await reopened.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["change_ids"][0]
        await reopened.close()
        return cid

    assert asyncio.run(second()) > last, "a restart under an earlier clock does not go back"


def test_a_form_is_checked_strictly(tmp_path):
    store = Collections(str(tmp_path / "collections.sqlite"))

    async def body():
        for form, reason in [
            ([], "a form is a JSON object"),
            ({"fields": {"a": {"type": "text"}}, "rights": {}}, 'takes only "fields"'),
            ({"fields": {}}, "at least one field"),
            ({"fields": {"A": {"type": "text"}}}, "a field's name"),
            ({"fields": {"a": {"type": "money"}}}, '"type" is one of text, int, enum, date, bool, ref'),
            ({"fields": {"a": {"type": "text", "values": ["x"]}}}, "a text takes no 'values'"),
            ({"fields": {"a": {"type": "text", "required": "yes"}}}, '"required" is true or false'),
            ({"fields": {"a": {"type": "enum", "values": ["x", "x"]}}}, '"values" is a list'),
            ({"fields": {"a": {"type": "enum", "values": ["Sent"]}}}, '"values" is a list'),
            ({"fields": {"a": {"type": "ref"}}}, '"to" names the collection'),
            ({"fields": {"a": {"type": "ref", "to": "nowhere"}}}, "there is no collection 'nowhere'"),
            ({"fields": {"a": {"type": "text", "x": "y" * 70000}}}, "at most 64 KB"),
        ]:
            with pytest.raises(Rejected, match=re.escape(reason)):
                await store.define(OWNER, "leads", "career", form, 1)
        with pytest.raises(Rejected, match="a collection's name"):
            await store.define(OWNER, "_files", "career", LEADS, 1)
        with pytest.raises(Rejected, match="not a Space's name"):
            await store.define(OWNER, "leads", "../career", LEADS, 1)
        defined = await store.define(OWNER, "leads", "career", LEADS, 1)
        assert defined == {"name": "leads", "rev": (await sql(store, "SELECT max(change_id) FROM changes"))[0][0]}
        assert await store.define(OWNER, "own", "career", {"fields": {"up": {"type": "ref", "to": "own"}}}, 1)
        await store.define(OWNER, "leads", "jobs", LEADS, 2)
        assert await sql(store, "SELECT space, rev FROM schemas WHERE collection = 'leads'") == [("jobs", 2)]
        await store.close()

    run(body)


def test_batches_hold_the_changes_that_go_together(tmp_path):
    importer = Writer("import:" + "0" * 26, "import")

    async def body():
        store = await with_leads(tmp_path)
        batch = await store.start_batch("import", importer, 1)
        await store.apply(importer, "leads", [add({"company": "Acme"}, src="file:" + "a" * 64 + "#row=2")], 2,
                          batch=batch)
        (rid,), = await sql(store, "SELECT id FROM records")
        assert (await store.history("leads", rid))[0]["batch"] == batch
        await store.finish_batch(batch, 3)
        with pytest.raises(Rejected, match="is applying"):
            await store.apply(importer, "leads", [add({"company": "Acme"})], 4, batch=batch)
        with pytest.raises(Rejected, match="is applying"):
            await store.finish_batch(batch, 4)
        with pytest.raises(Rejected, match="not 'export'"):
            await store.start_batch("export", importer, 4)
        await store.close()

    run(body)


def test_a_write_under_a_lock_is_refused_within_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "BUSY_S", 0.2)
    path = tmp_path / "collections.sqlite"
    async def body():
        store = await with_leads(tmp_path)
        await store.open()
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(Refused, match="try again"):
            await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1)
        with pytest.raises(Refused):
            await store.get("leads", "x")
        assert time.monotonic() - started < 2
        other.execute("ROLLBACK")
        done = await store.apply(OWNER, "leads", [add({"company": "Acme"})], 2)
        assert done["records"][0]["version"] == 1, "the same call again goes through"
        await store.close()

    run(body)
