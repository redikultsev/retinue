"""The collections' store: one thread with one connection, a lock past its wait refused at once, a file kept once by
its SHA-256 however often it comes, written before its row."""

from __future__ import annotations

import asyncio
import hashlib
import json
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


# --- rights, keys, idems ------------------------------------------------------------------------------------------

ASSISTANT = Writer("assistant", "assistant", model="claude-example")
MAC = Writer("mac:laptop/s1", "mac")
MAIL_RULE = Writer("code:mail-sent", "mail")
TOUCHES = {"fields": {
    "lead": {"type": "ref", "to": "leads"},
    "state": {"type": "enum", "values": ["drafted", "sent", "unknown"], "required": True},
    "text": {"type": "text"},
}}
TOUCH_RIGHTS = {
    "writers": {
        "session": {"add": True, "set": "*", "delete": "propose"},
        "assistant": {"set": ["state"]},
        "code:mail-sent": {"set": ["state"]},
    },
    "set_by": {"state": {"sent": ["owner", "code:mail-sent"]}},
}


async def with_touches(tmp_path) -> Collections:
    store = await with_leads(tmp_path)
    await store.define(OWNER, "touches", "career", TOUCHES, 1)
    await store.rights(OWNER, "touches", TOUCH_RIGHTS, 1)
    return store


def test_a_new_collection_gets_the_narrow_default_rights(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        assert await sql(store, "SELECT rights FROM schemas WHERE collection = 'leads'") == \
            [(collections._json(collections.DEFAULT_RIGHTS),)]
        rid = (await store.apply(SESSION, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        await store.apply(MAC, "leads", [{"op": "set", "id": rid, "version": 1, "fields": {"stage": "applied"}}], 2)
        with pytest.raises(Rejected, match="a delete by session in leads is the owner's to make: propose it"):
            await store.apply(SESSION, "leads", [{"op": "delete", "id": rid, "version": 2}], 3)
        with pytest.raises(Rejected, match="mac does not delete records in leads"):
            await store.apply(MAC, "leads", [{"op": "delete", "id": rid, "version": 2}], 3)
        with pytest.raises(Rejected, match="assistant does not add records to leads"):
            await store.apply(ASSISTANT, "leads", [add({"company": "Acme"})], 3)
        with pytest.raises(Rejected, match="'stage' is not open to assistant in leads"):
            await store.apply(ASSISTANT, "leads", [{"op": "set", "id": rid, "version": 2,
                                                    "fields": {"stage": "closed"}}], 3)
        with pytest.raises(Rejected, match="code:ats does not"):
            await store.apply(Writer("code:ats", "mail"), "leads", [{"op": "delete", "id": rid, "version": 2}], 3)
        done = await store.apply(OWNER, "leads", [{"op": "delete", "id": rid, "version": 2}], 4)
        assert done["records"][0]["deleted"] == 4, "the owner may do anything"
        await store.close()

    run(body)


def test_a_field_outside_the_writers_rights_is_rejected_and_nothing_is_kept(tmp_path):
    async def body():
        store = await with_touches(tmp_path)
        lead = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        touch = (await store.apply(SESSION, "touches", [add({"lead": lead, "state": "drafted"})], 2))["records"][0]
        ops = [{"op": "set", "id": touch["id"], "version": 1, "fields": {"state": "unknown"}},
               {"op": "set", "id": touch["id"], "version": 2, "fields": {"text": "Hello"}}]
        with pytest.raises(Rejected, match=r"ops\[1\]: 'text' is not open to assistant in touches: propose"):
            await store.apply(ASSISTANT, "touches", ops, 3)
        assert (await store.get("touches", touch["id"]))["version"] == 1, "the allowed first op is not kept"
        done = await store.apply(ASSISTANT, "touches", ops[:1], 3)
        assert done["records"][0]["fields"]["state"] == "unknown"
        await store.close()

    run(body)


def test_only_the_classes_set_by_names_give_a_value(tmp_path):
    async def body():
        store = await with_touches(tmp_path)
        lead = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        with pytest.raises(Rejected, match="'state' becomes 'sent' only by the owner, code:mail-sent: not by session"):
            await store.apply(SESSION, "touches", [add({"lead": lead, "state": "sent"})], 2)
        touch = (await store.apply(SESSION, "touches", [add({"lead": lead, "state": "drafted"})], 2))["records"][0]
        for writer in (SESSION, ASSISTANT):
            with pytest.raises(Rejected, match="only by the owner, code:mail-sent"):
                await store.apply(writer, "touches", [{"op": "set", "id": touch["id"], "version": 1,
                                                       "fields": {"state": "sent"}}], 3)
        done = await store.apply(MAIL_RULE, "touches", [{"op": "set", "id": touch["id"], "version": 1,
                                                         "fields": {"state": "sent"}}], 3)
        assert done["records"][0]["fields"]["state"] == "sent"
        reverted = await store.apply(ASSISTANT, "touches", [{"op": "revert", "change_id": done["change_ids"][0],
                                                             "version": 2}], 4)
        assert reverted["records"][0]["fields"]["state"] == "drafted", "taking sent back is a set to drafted"
        with pytest.raises(Rejected, match="only by the owner"):
            await store.apply(ASSISTANT, "touches", [{"op": "revert", "change_id": reverted["change_ids"][0],
                                                      "version": 3}], 5)
        await store.apply(OWNER, "touches", [{"op": "set", "id": touch["id"], "version": 3,
                                              "fields": {"state": "sent"}}], 5)
        await store.close()

    run(body)


def test_a_revert_needs_the_rights_of_what_it_does(tmp_path):
    async def body():
        store = await with_touches(tmp_path)
        lead = (await store.apply(OWNER, "leads", [add({"company": "Acme"})], 1))["records"][0]["id"]
        done = await store.apply(SESSION, "touches", [add({"lead": lead, "state": "drafted"})], 2)
        with pytest.raises(Rejected, match="a delete by session in touches is the owner's to make"):
            await store.apply(SESSION, "touches", [{"op": "revert", "change_id": done["change_ids"][0],
                                                    "version": 1}], 3)
        with pytest.raises(Rejected, match="assistant does not delete"):
            await store.apply(ASSISTANT, "touches", [{"op": "revert", "change_id": done["change_ids"][0],
                                                      "version": 1}], 3)
        await store.close()

    run(body)


def test_rights_are_the_owners_alone_and_checked_strictly(tmp_path):
    async def body():
        store = await with_touches(tmp_path)
        for writer in (SESSION, ASSISTANT, MAC, MAIL_RULE, Writer("define:2", "define"),
                       Writer("import:" + "0" * 26, "import")):
            with pytest.raises(Rejected, match="rights are the owner's alone"):
                await store.rights(writer, "touches", {"writers": {"assistant": {"add": True, "set": "*",
                                                                                 "delete": True}}}, 2)
        with pytest.raises(Rejected, match="a form is the owner's to apply"):
            await store.define(SESSION, "touches", "career", TOUCHES, 2)
        with pytest.raises(Rejected, match='takes only "fields", "key" and "unique"'):
            await store.define(OWNER, "touches", "career", {**TOUCHES, "rights": TOUCH_RIGHTS}, 2)
        for rights, reason in [
            ([], "rights are a JSON object"),
            ({"writers": {}, "sensitive": []}, "not 'sensitive'"),
            ({"writers": {"owner": {"add": True}}}, "not a writer's class"),
            ({"writers": {"session:abc123": {"add": True}}}, "not a writer's class"),
            ({"writers": {"assistant": {"add": True, "propose": True}}}, 'takes "add", "set" and "delete"'),
            ({"writers": {"assistant": {"add": "yes"}}}, "add is true or false"),
            ({"writers": {"assistant": {"set": ["salary"]}}}, "the form has no field 'salary'"),
            ({"writers": {"assistant": {"set": "all"}}}, "distinct fields of the form"),
            ({"writers": {"assistant": {"delete": 1}}}, 'delete is true, false or "propose"'),
            ({"set_by": {"text": {"x": ["owner"]}}}, "'text' is not an enum of the form"),
            ({"set_by": {"state": {"replied": ["owner"]}}}, "'replied' is not one of its values"),
            ({"set_by": {"state": {"sent": []}}}, "is a list of classes"),
            ({"set_by": {"state": {"sent": ["robot"]}}}, "is a list of classes"),
        ]:
            with pytest.raises(Rejected, match=re.escape(reason)):
                await store.rights(OWNER, "touches", rights, 2)
        with pytest.raises(Rejected, match="no collection 'nowhere'"):
            await store.rights(OWNER, "nowhere", {}, 2)
        before = await sql(store, "SELECT count(*) FROM changes")
        await store.rights(OWNER, "touches", TOUCH_RIGHTS, 3)
        assert await sql(store, "SELECT count(*) FROM changes") == before, "the same rights again: already so"
        await store.rights(OWNER, "touches", {"writers": {"assistant": {"set": ["state"]}}}, 4)
        (change,) = await sql(store, "SELECT op, actor, before, after FROM changes ORDER BY seq DESC LIMIT 1")
        assert change[:2] == ("define", "owner")
        assert json.loads(change[3]) == {"rights": {"writers": {"assistant": {"add": False, "set": ["state"],
                                                                              "delete": False}}, "set_by": {}}}
        assert json.loads(change[2])["rights"]["set_by"] == TOUCH_RIGHTS["set_by"]
        with pytest.raises(Rejected, match="rights do not fit this form"):
            await store.define(OWNER, "touches", "career", {"fields": {"note": {"type": "text"}}}, 5)
        await store.close()

    run(body)


UNIQUE = {"fields": {**LEADS["fields"], "stage": {**LEADS["fields"]["stage"], "index": True}},
          "unique": [["n"]], "key": ["company", "role"]}


async def with_unique(tmp_path) -> Collections:
    store = Collections(str(tmp_path / "collections.sqlite"))
    await store.define(OWNER, "leads", "career", UNIQUE, 1)
    return store


def test_a_unique_value_is_taken_while_its_record_is_not_deleted(tmp_path):
    async def body():
        store = await with_unique(tmp_path)
        first = (await store.apply(SESSION, "leads", [add({"company": "Acme", "n": 7})], 1))["records"][0]
        with pytest.raises(Rejected, match=f"taken: {first['id']} has n 7") as caught:
            await store.apply(SESSION, "leads", [add({"company": "Example Ltd", "n": 7})], 2)
        assert caught.value.record == first, "the record that holds it comes with the refusal"
        with pytest.raises(Rejected, match=r"ops\[1\]: taken"):
            await store.apply(SESSION, "leads", [add({"company": "Example Ltd", "n": 8}),
                                                 add({"company": "Example Org", "n": 8})], 2)
        other = (await store.apply(SESSION, "leads", [add({"company": "Example Ltd"})], 2))["records"][0]
        with pytest.raises(Rejected, match="taken"):
            await store.apply(SESSION, "leads", [{"op": "set", "id": other["id"], "version": 1, "fields": {"n": 7}}], 3)
        await store.apply(OWNER, "leads", [{"op": "delete", "id": first["id"], "version": 1}], 3)
        await store.apply(SESSION, "leads", [{"op": "set", "id": other["id"], "version": 1, "fields": {"n": 7}}], 4)
        with pytest.raises(Rejected, match=f"taken: {other['id']} has n 7"):
            await store.apply(OWNER, "leads", [{"op": "restore", "id": first["id"], "version": 2}], 5)
        await store.apply(SESSION, "leads", [add({"company": "Example Org"}), add({"company": "Example Inc"})], 5)
        assert await sql(store, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'records' "
                                "AND name LIKE 'records:%' ORDER BY name") == [("records:leads:index:stage",),
                                                                               ("records:leads:unique:n",)]
        with pytest.raises(sqlite3.IntegrityError):  # the index holds it even past the store's own check
            await sql(store, "UPDATE records SET data = json_set(data, '$.n', 7) WHERE data NOT LIKE '%\"n\":%'")
        await store.close()

    run(body)


def test_the_unique_check_uses_the_partial_index(tmp_path):
    async def body():
        store = await with_unique(tmp_path)
        seen = []
        await store._run(lambda: store._db.set_trace_callback(seen.append))
        await store.apply(SESSION, "leads", [add({"company": "Acme", "n": 7})], 1)
        (check,) = [q for q in seen if q.startswith("SELECT id FROM records") and "$.n" in q]
        plan = await store._run(lambda: store._db.execute("EXPLAIN QUERY PLAN " + check).fetchall())
        assert any('INDEX records:leads:unique:n' in step[-1] for step in plan), plan
        where, params = await store._run(store._where, "leads", UNIQUE, {"field": "stage", "op": "eq",
                                                                          "value": "applied"}, False)
        plan = await store._run(lambda: store._db.execute(
            f"EXPLAIN QUERY PLAN SELECT count(*) FROM records WHERE {where}", params).fetchall())
        assert any('INDEX records:leads:index:stage' in step[-1] for step in plan), plan
        await store.close()

    run(body)


def test_a_form_the_records_break_is_not_made_unique_and_a_dropped_unique_goes(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        await store.apply(OWNER, "leads", [add({"company": "Acme", "n": 7}), add({"company": "Example Ltd", "n": 7})], 1)
        with pytest.raises(Rejected, match="records of leads share a n: make them distinct"):
            await store.define(OWNER, "leads", "career", {**LEADS, "unique": [["n"]]}, 2)
        assert await sql(store, "SELECT rev FROM schemas WHERE collection = 'leads'") == [(1,)]
        await store.define(OWNER, "leads", "career", {**LEADS, "unique": [["company", "n"]]}, 2)
        await store.define(OWNER, "leads_x", "career", {**LEADS, "unique": [["n"]]}, 2)
        await store.define(OWNER, "leads", "career", LEADS, 3)
        assert await sql(store, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'records' "
                                "AND name LIKE 'records:%'") == [("records:leads_x:unique:n",)]
        for form, reason in [
            ({**LEADS, "unique": [["salary"]]}, "the form has no field 'salary'"),
            ({**LEADS, "unique": [["n"], ["n"]]}, "names each list of fields once"),
            ({**LEADS, "unique": ["n"]}, "unique[0] is a list"),
            ({**LEADS, "key": []}, '"key" is a list'),
            ({**LEADS, "key": ["company", "company"]}, '"key" is a list'),
            ({"fields": {"a": {"type": "text", "index": "yes"}}}, '"index" is true or false'),
        ]:
            with pytest.raises(Rejected, match=re.escape(reason)):
                await store.define(OWNER, "leads", "career", form, 4)
        await store.close()

    run(body)


def test_a_record_matching_another_on_the_key_is_a_possible_duplicate_unless_distinct(tmp_path):
    async def body():
        store = await with_unique(tmp_path)
        first = (await store.apply(SESSION, "leads", [add({"company": "Acme", "role": "Backend"})], 1))["records"][0]
        with pytest.raises(Rejected, match=f"possible duplicate of {first['id']}: the same company, role") as caught:
            await store.apply(SESSION, "leads", [add({"company": " ACME ", "role": "backend"})], 2)
        assert caught.value.record == first
        await store.apply(SESSION, "leads", [add({"company": "Acme"})], 2)  # a key half unknown matches nothing
        second = (await store.apply(SESSION, "leads", [add({"company": "Acme", "role": "Backend"}, distinct=True)],
                                    3))["records"][0]
        third = (await store.apply(SESSION, "leads", [add({"company": "Acme", "role": "Data"})], 4))["records"][0]
        with pytest.raises(Rejected, match="possible duplicate"):
            await store.apply(SESSION, "leads", [{"op": "set", "id": third["id"], "version": 1,
                                                  "fields": {"role": "Backend"}}], 5)
        await store.apply(SESSION, "leads", [{"op": "set", "id": third["id"], "version": 1,
                                              "fields": {"stage": "applied"}}], 5)  # its key unchanged: no check
        with pytest.raises(Rejected, match="distinct is true or false"):
            await store.apply(SESSION, "leads", [add({"company": "Acme"}, distinct="yes")], 6)
        assert second["id"] != first["id"]
        await store.close()

    run(body)


def test_the_same_idem_answers_the_first_call_and_changes_nothing(tmp_path):
    async def body():
        store = await with_leads(tmp_path)
        ops = [add({"company": "Acme"}), add({"company": "Example Ltd"})]
        first = await store.apply(SESSION, "leads", ops, 1, idem="lane-1/tool-7")
        rid = first["records"][0]["id"]
        await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 1, "fields": {"stage": "applied"}}], 2)
        again = await store.apply(Writer("session:other", "session"), "leads", ops, 3, idem="lane-1/tool-7")
        assert again["change_ids"] == first["change_ids"], "the same class of writer: the first call's answer"
        assert again["records"][0]["fields"]["stage"] == "applied", "with its records as they are now"
        assert await sql(store, "SELECT count(*) FROM records") == [(2,)]
        assert (await store.history("leads", rid))[-1]["idem"] == "lane-1/tool-7"
        other = await store.apply(MAC, "leads", ops, 4, idem="lane-1/tool-7")
        assert other["change_ids"] != first["change_ids"], "another class's idems are its own"
        with pytest.raises(Rejected, match="idem is the call's own name"):
            await store.apply(MAC, "leads", ops, 5, idem="")
        with pytest.raises(Rejected, match="'company' is required"):
            await store.apply(MAC, "leads", [add({"role": "Backend"})], 5, idem="mac-2")
        done = await store.apply(MAC, "leads", [add({"company": "Acme"})], 6, idem="mac-2")
        assert done["change_ids"][0], "a refused call leaves no idem behind"
        await store.close()

    run(body)


def test_a_mail_read_again_makes_no_second_change(tmp_path):
    rights = {"writers": {"assistant": {"add": True, "set": ["company", "stage", "role"]}}}

    async def body():
        store = await with_leads(tmp_path)
        await store.rights(OWNER, "leads", rights, 1)
        mail = "mail:work/<abc@example.com>"
        made = await store.apply(ASSISTANT, "leads", [add({"company": "Acme"}, src=mail)], 2)
        rid = made["records"][0]["id"]
        again = await store.apply(ASSISTANT, "leads", [add({"company": "Acme"}, src=mail)], 3)
        assert again["change_ids"] == [None] and again["records"][0]["id"] == rid, "the record that mail made"
        done = await store.apply(ASSISTANT, "leads", [{"op": "set", "id": rid, "version": 1, "src": mail,
                                                       "fields": {"stage": "closed"}}], 4)
        assert done["change_ids"][0]
        await store.apply(OWNER, "leads", [{"op": "set", "id": rid, "version": 2, "fields": {"stage": "interview"}}], 5)
        reread = await store.apply(ASSISTANT, "leads", [{"op": "set", "id": rid, "version": 3, "src": mail,
                                                         "fields": {"stage": "closed", "role": "Backend"}}], 6)
        assert reread["records"][0]["fields"] == {"company": "Acme", "stage": "interview", "role": "Backend"}, \
            "the stage that mail set once is not set again; the role it had not set is"
        assert (await store.history("leads", rid))[0]["after"] == {"role": "Backend"}
        other = await store.apply(ASSISTANT, "leads", [{"op": "set", "id": rid, "version": 4,
                                                        "src": "mail:work/<def@example.com>",
                                                        "fields": {"stage": "closed"}}], 7)
        assert other["records"][0]["fields"]["stage"] == "closed", "another mail is another change"
        await store.close()

    run(body)


# --- reading ------------------------------------------------------------------------------------------------------

async def with_rows(tmp_path) -> tuple[Collections, list[str]]:
    store = await with_leads(tmp_path)
    await store.define(OWNER, "touches", "career", TOUCHES, 1)
    leads = (await store.apply(OWNER, "leads", [
        add({"company": "Acme", "stage": "applied", "seen": "2026-09-01", "remote": True}),
        add({"company": "Example Ltd", "stage": "interview", "seen": "2026-09-10"}),
        add({"company": "Example Org", "stage": "applied", "seen": "2026-09-20", "remote": False}),
        add({"company": "Example Inc"}),
    ], 2))["records"]
    ids = [lead["id"] for lead in leads]
    await store.apply(OWNER, "touches", [add({"lead": ids[0], "state": "sent"}), add({"lead": ids[1], "state": "sent"}),
                                         add({"lead": ids[2], "state": "drafted"})], 3)
    await store.apply(OWNER, "leads", [{"op": "delete", "id": ids[3], "version": 1}], 4)
    return store, ids


def test_query_filters_by_the_plans_conditions(tmp_path):
    async def body():
        store, ids = await with_rows(tmp_path)

        async def found(filter, collection="leads", **kw):
            return [row["id"] for row in (await store.query(collection, filter, **kw))["rows"]]

        assert await found(None) == ids[:3], "deleted records aside, oldest first"
        assert await found(None, deleted=True) == ids
        assert await found({"field": "stage", "op": "eq", "value": "applied"}) == [ids[0], ids[2]]
        assert await found({"field": "stage", "op": "in", "value": ["interview", "closed"]}) == [ids[1]]
        assert await found({"field": "seen", "op": "between", "value": ["2026-09-05", "2026-09-20"]}) == ids[1:3]
        assert await found({"field": "remote", "op": "empty"}) == [ids[1]]
        assert await found({"field": "remote", "op": "not_empty"}) == [ids[0], ids[2]]
        assert await found({"field": "remote", "op": "eq", "value": False}) == [ids[2]]
        assert await found([{"field": "stage", "op": "eq", "value": "applied"},
                            {"field": "remote", "op": "eq", "value": True}]) == [ids[0]], "all hold"
        touches = await store.query("touches", {"field": "lead", "op": "ref_is", "value": ids[1]})
        assert [row["fields"]["lead"] for row in touches["rows"]] == [ids[1]]
        sent_to_applied = await store.query("touches", [{"field": "lead.stage", "op": "eq", "value": "applied"},
                                                        {"field": "state", "op": "eq", "value": "sent"}])
        assert [row["fields"]["lead"] for row in sent_to_applied["rows"]] == [ids[0]], "a ref's field, one level"
        projected = await store.query("leads", {"field": "company", "op": "eq", "value": "Acme"}, fields=["stage"])
        assert projected["rows"][0]["fields"] == {"stage": "applied"} and projected["rows"][0]["version"] == 1
        await store.close()

    run(body)


def test_query_pages_by_rows_and_by_bytes(tmp_path, monkeypatch):
    async def body():
        store, ids = await with_rows(tmp_path)
        page = await store.query("leads", limit=2)
        assert [r["id"] for r in page["rows"]] == ids[:2] and page["next"] == ids[1]
        rest = await store.query("leads", limit=2, after=page["next"])
        assert [r["id"] for r in rest["rows"]] == [ids[2]] and rest["next"] is None
        monkeypatch.setattr(collections, "QUERY_BYTES", 300)
        small = await store.query("leads")
        assert 0 < len(small["rows"]) < 3 and small["next"] == small["rows"][-1]["id"], "cut at the bytes, not lost"
        for kw, reason in [({"limit": 0}, "limit is 1 to 200"), ({"limit": 201}, "limit is 1 to 200"),
                           ({"after": "x"}, "after is the id"), ({"fields": ["salary"]}, "no field 'salary'")]:
            with pytest.raises(Rejected, match=reason):
                await store.query("leads", **kw)
        await store.close()

    run(body)


@pytest.mark.parametrize("filter, reason", [
    ({"field": "stage') OR 1=1 --", "op": "eq", "value": "x"}, "leads has no field \"stage') OR 1=1 --\""),
    ({"field": "lead.x", "op": "eq", "value": "x"}, "leads has no field 'lead'"),
    ({"field": "stage.x", "op": "eq", "value": "x"}, "'stage' is not a ref"),
    ({"field": "stage", "op": "like", "value": "x"}, '"op" is one of eq, in, between'),
    ({"field": "stage", "op": "eq", "value": "hired"}, "'stage' is one of"),
    ({"field": "n", "op": "eq", "value": "1 OR 1=1"}, "'n' is a whole number"),
    ({"field": "stage", "op": "in", "value": []}, "in takes a list of 1 to 200"),
    ({"field": "stage", "op": "between", "value": ["a", "b"]}, "between is for dates"),
    ({"field": "seen", "op": "between", "value": ["2026-09-20", "2026-09-01"]}, "the first not after the last"),
    ({"field": "seen", "op": "empty", "value": None}, "empty takes no value"),
    ({"field": "company", "op": "ref_is", "value": "x"}, "'company' is not a ref"),
    ({"field": "stage", "op": "eq", "value": "applied", "or": []}, "a condition is"),
    ([{"field": "stage", "op": "empty"}] * 17, "up to 16"),
])
def test_a_name_outside_the_form_never_reaches_the_sql(tmp_path, filter, reason):
    async def body():
        store = await with_leads(tmp_path)
        seen = []
        await store._run(lambda: store._db.set_trace_callback(seen.append))
        with pytest.raises(Rejected) as caught:
            await store.query("leads", filter)
        assert reason in caught.value.reason, caught.value.reason
        assert not any("records WHERE" in q for q in seen), "refused before any statement on the records"
        with pytest.raises(Rejected):
            await store.count("leads", filter)
        await store.close()

    run(body)


def test_count_counts_and_groups(tmp_path):
    async def body():
        store, ids = await with_rows(tmp_path)
        assert await store.count("leads") == 3
        assert await store.count("leads", {"field": "stage", "op": "eq", "value": "applied"}) == 2
        assert await store.count("touches", {"field": "lead.stage", "op": "in", "value": ["applied"]}) == 2
        assert await store.count("leads", group="stage") == [{"value": "applied", "count": 2},
                                                              {"value": "interview", "count": 1}]
        assert await store.count("leads", group="remote") == [{"value": None, "count": 1},
                                                               {"value": False, "count": 1},
                                                               {"value": True, "count": 1}]
        with pytest.raises(Rejected, match="group is an enum or a bool of leads"):
            await store.count("leads", group="company")
        with pytest.raises(Rejected, match="there is no collection 'nowhere'"):
            await store.count("nowhere")
        await store.close()

    run(body)
