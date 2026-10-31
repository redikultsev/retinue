"""The collections' store: one thread with one connection, a lock past its wait refused at once, a file kept once by
its SHA-256 however often it comes, written before its row."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time

import pytest

from retinue import collections
from retinue.collections import Collections, Full, Refused, clean_mime, clean_name


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
