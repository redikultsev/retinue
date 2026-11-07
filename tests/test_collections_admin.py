"""The owner's commands for his collections on the server's terminal: a form only grows, and is tried on the records
before «y» keeps it; rights never come with a form; an import is one batch, kept whole or not at all, and taken back
whole but for the records changed after it; what a command writes, the running platform's thread sees."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import types

import pytest

from retinue import collections, collections_admin, platformd
from retinue.collections import Collections, Rejected, Writer

DEALS = {"space": "work", "form": {"fields": {
    "company": {"type": "text", "required": True},
    "product": {"type": "text"},
    "stage": {"type": "enum", "values": ["lead", "qualified", "lost"]},
}, "key": ["company", "product"]}}


def cli(tmp_path, *argv: str, answer: str = "y", clock: float = 1000.0) -> tuple[int, str]:
    parser = argparse.ArgumentParser(prog="retinue-platform")
    collections_admin.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["collections", *argv])
    said, asked = [], []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return answer

    code = collections_admin.run(args, str(tmp_path / "collections.sqlite"), ask, said.append, lambda: clock)
    return code, "\n".join(said) + ("\n[asked]" if asked else "")


def write(tmp_path, name: str, value) -> str:
    path = tmp_path / name
    if isinstance(value, list):
        path.write_text("".join(json.dumps(line) + "\n" for line in value))
    else:
        path.write_text(json.dumps(value))
    return str(path)


def rows(tmp_path, query: str) -> list:
    db = sqlite3.connect(tmp_path / "collections.sqlite")
    try:
        return db.execute(query).fetchall()
    finally:
        db.close()


def grown(**fields) -> dict:
    return {"space": "work", "form": {**DEALS["form"], "fields": {**DEALS["form"]["fields"], **fields}}}


def test_a_collection_is_made_from_a_file_after_its_diff_and_a_yes(tmp_path):
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS), answer="n")
    assert code == 1 and "[asked]" in said
    assert '+    "company": {' in said and "Nothing applied." in said
    assert rows(tmp_path, "SELECT count(*) FROM schemas") == [(0,)], "nothing kept without a yes"
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    assert code == 0
    assert rows(tmp_path, "SELECT op, actor, via FROM changes") == [("define", "define:1", "define")]
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    assert code == 0 and "[asked]" not in said
    code, said = cli(tmp_path, "list")
    assert said
    code, said = cli(tmp_path, "show", "deals")
    assert code == 0 and '"session": {' in said, "the default rights, shown"
    assert cli(tmp_path, "show", "nowhere")[0] == 1


@pytest.mark.parametrize("form, reason", [
    ({k: v for k, v in DEALS["form"]["fields"].items() if k != "product"}, "field 'product' would be gone"),
    ({**{k: v for k, v in DEALS["form"]["fields"].items() if k != "product"}, "title": {"type": "text"}},
     "field 'product' would be gone"),  # a rename is a field gone and another come
    ({**DEALS["form"]["fields"], "product": {"type": "int"}}, "field 'product' would become another type"),
    ({**DEALS["form"]["fields"], "stage": {"type": "enum", "values": ["lead", "done", "lost"]}},
     "field 'stage' would lose 'qualified'"),
])
def test_a_form_only_grows_until_its_first_case(tmp_path, form, reason):
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "next.json", {"space": "work", "form": {
        **DEALS["form"], "fields": form, "key": ["company"]}}))
    assert code == 1 and reason in said and "(⏸)" in said
    assert "[asked]" not in said
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(1,)]


def test_a_grown_form_is_tried_on_every_record_and_kept_only_when_they_all_fit(tmp_path):
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    cli(tmp_path, "import", "deals", write(tmp_path, "deals.jsonl", [
        {"op": "add", "fields": {"company": "Acme", "stage": "qualified"}},
        {"op": "add", "fields": {"company": "Example Ltd"}}]))
    more = grown(stage={"type": "enum", "values": ["lead", "qualified", "won", "lost"], "index": True},
                 origin={"type": "enum", "values": ["inbound", "outbound"]})
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "more.json", more))
    assert code == 0 and '+        "won",' in said
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(2,)]
    strict = grown(**{**more["form"]["fields"], "origin": {"type": "enum", "values": ["inbound", "outbound"],
                                                            "required": True}})
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "strict.json", strict))
    assert code == 1
    assert "[asked]" not in said, "a form the records do not fit is never asked about"
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(2,)]
    unique = {**more, "form": {**more["form"], "unique": [["stage"]]}}
    cli(tmp_path, "import", "deals", write(tmp_path, "one.jsonl", [{"op": "add", "fields": {"company": "Acme Two",
                                                                                            "stage": "qualified"}}]))
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "unique.json", unique))
    assert code == 1


def test_views_come_with_the_form_and_stay_when_a_file_has_none(tmp_path):
    board = [{"name": "funnel", "kind": "board", "group": "stage"}]
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "views.json", {**DEALS, "views": board}))
    assert code == 0 and "+++ views applied" in said and '"kind": "board"' in said and "form now" not in said
    assert rows(tmp_path, "SELECT views, rev FROM schemas") == [(json.dumps(board, separators=(",", ":")), 2)]
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    assert code == 0, "a file without views keeps the ones there"
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "bad.json", {**DEALS, "views": [{"kind": "map"}]}))
    assert code == 1 and "[asked]" not in said
    assert '"group": "stage"' in cli(tmp_path, "show", "deals")[1]


def test_a_form_changed_since_its_trial_is_not_applied_over(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(Writer("owner", "define"), "deals", "work", DEALS["form"], 1)
        with pytest.raises(Rejected):
            await store.define(Writer("define:3", "define"), "deals", "work", grown(n={"type": "int"})["form"], 2)
        assert (await store.define(Writer("define:2", "define"), "deals", "work", grown(n={"type": "int"})["form"],
                                   2))["form_rev"] == 2
        await store.close()

    asyncio.run(body())


def test_rights_never_come_with_a_form(tmp_path):
    rights = {"writers": {"assistant": {"add": True, "set": "*", "delete": True}}}
    for given in ({**DEALS, "rights": rights}, {**DEALS, "set_by": {}},
                  {"space": "work", "form": {**DEALS["form"], "writers": rights["writers"]}},
                  {"space": "work", "form": {**DEALS["form"], "rights": rights}}):
        code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", given))
        assert code == 1
        assert "collections rights <collection> <file>" in said
    assert rows(tmp_path, "SELECT count(*) FROM schemas") == [(0,)]
    code, said = cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", {**DEALS, "pages": []}))
    assert code == 1


def test_rights_are_set_from_a_file_after_their_diff(tmp_path):
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    rights = {"writers": {"assistant": {"set": ["stage"]}}, "set_by": {"stage": {"lost": ["owner"]}}}
    code, said = cli(tmp_path, "rights", "deals", write(tmp_path, "rights.json", rights), answer="")
    assert code == 1 and '+      "set": [' in said and "Nothing applied." in said
    code, said = cli(tmp_path, "rights", "deals", write(tmp_path, "rights.json", rights))
    assert code == 0
    (rights_kept,), = rows(tmp_path, "SELECT rights FROM schemas")
    assert json.loads(rights_kept)["writers"] == {"assistant": {"add": False, "set": ["stage"], "delete": False}}
    assert rows(tmp_path, "SELECT actor, via FROM changes ORDER BY seq DESC LIMIT 1") == [("owner", "define")]
    code, said = cli(tmp_path, "rights", "deals", write(tmp_path, "rights.json", rights))
    assert code == 0
    code, said = cli(tmp_path, "rights", "deals", write(tmp_path, "bad.json", {"writers": {"owner": {}}}))
    assert code == 1


def test_an_import_is_one_batch_kept_whole_or_not_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 2)  # the refused line in the second transaction
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    lines = [{"op": "add", "fields": {"company": "Acme", "product": "Widgets"}, "as_of": "2026-09-01",
              "src": "file:" + "a" * 64 + "#row=2"},
             {"op": "add", "fields": {"company": "Example Ltd", "stage": "qualified"}, "as_of": "2026-09-02"},
             {"op": "add", "fields": {"company": "Example Org", "stage": "paused"}}]
    code, said = cli(tmp_path, "import", "deals", write(tmp_path, "bad.jsonl", lines))
    assert code == 1
    assert "[asked]" not in said, "the trial run refuses it before anything is asked"
    lines[2]["fields"]["stage"] = "lost"
    code, said = cli(tmp_path, "import", "deals", write(tmp_path, "deals.jsonl", lines), answer="n")
    assert code == 1
    assert rows(tmp_path, "SELECT count(*) FROM records") == [(0,)]
    code, said = cli(tmp_path, "import", "deals", write(tmp_path, "deals.jsonl", lines))
    assert code == 0 and "3 added" in said
    (batch, state), = rows(tmp_path, "SELECT id, state FROM batches")
    assert state == "applied" and f"revert-batch {batch}" in said
    assert rows(tmp_path, "SELECT DISTINCT actor, via, batch FROM changes WHERE op = 'add'") == [
        (f"import:{batch}", "import", batch)]
    assert rows(tmp_path, "SELECT as_of FROM changes WHERE op = 'add' ORDER BY seq") == [
        ("2026-09-01",), ("2026-09-02",), (None,)]
    code, said = cli(tmp_path, "import", "deals", str(tmp_path / "nowhere.jsonl"))
    assert code == 1 and "cannot read" in said
    (tmp_path / "half.jsonl").write_text('{"op": "add", "fields": {"company": "Acme Two"}}\n{nope\n')
    code, said = cli(tmp_path, "import", "deals", str(tmp_path / "half.jsonl"))
    assert code == 1 and "line 2" in said


def test_a_line_refused_after_others_were_kept_takes_them_back(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 1)
    owner = Writer("owner", "screen")

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(owner, "deals", "work", DEALS["form"], 1)
        real, calls = store._import_chunk, []

        def chunk(collection, ops, start, *rest):  # the owner adds Acme from the app between two transactions
            calls.append(start)
            if start == 1:
                store._apply(owner, "deals", [{"op": "add", "fields": {"company": "Acme", "product": "Widgets"}}], 2,
                             None, None)
            return real(collection, ops, start, *rest)

        store._import_chunk = chunk
        with pytest.raises(Rejected):
            await store.import_ops("deals", [{"op": "add", "fields": {"company": "Example Ltd"}},
                                             {"op": "add", "fields": {"company": "Acme", "product": "Widgets"}}], 3)
        assert calls == [0, 1]
        kept = await store.query("deals")
        assert [r["fields"]["company"] for r in kept["rows"]] == ["Acme"], "the owner's record stays, the line goes"
        assert await store._run(lambda: store._db.execute("SELECT kind, state FROM batches ORDER BY id").fetchall()) \
            == [("import", "reverted"), ("revert", "applied")]
        await store.close()

    asyncio.run(body())


def test_an_import_refused_and_taken_back_leaves_no_idem_its_corrected_file_would_meet(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 1)
    owner = Writer("owner", "screen")
    mail = "mail:work/<abc@example.com>"

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(owner, "deals", "work", DEALS["form"], 1)
        real = store._import_chunk

        def chunk(collection, ops, start, *rest):  # the owner adds Globex from the app between two transactions
            if start == 1:
                store._apply(owner, "deals", [{"op": "add", "fields": {"company": "Globex", "product": "Gadgets"}}], 2,
                             None, None)
            return real(collection, ops, start, *rest)

        store._import_chunk = chunk
        lines = [{"op": "add", "fields": {"company": "Acme"}, "src": mail},
                 {"op": "add", "fields": {"company": "Globex", "product": "Gadgets"}}]
        with pytest.raises(Rejected):
            await store.import_ops("deals", lines, 3)
        store._import_chunk = real
        done = await store.import_ops("deals", lines[:1], 4)
        assert done["added"] == 1, "the corrected file adds what the refused one took back"
        assert sorted(r["fields"]["company"] for r in (await store.query("deals"))["rows"]) == ["Acme", "Globex"]
        await store.close()

    asyncio.run(body())


def test_a_batch_taken_back_while_its_import_runs_stops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 1)
    owner = Writer("owner", "screen")

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(owner, "deals", "work", DEALS["form"], 1)
        real = store._import_chunk

        def chunk(collection, ops, start, now, batch, counts):  # another terminal's revert-batch between two chunks
            if start == 1:
                store._revert_batch(owner, batch, now, False)
            return real(collection, ops, start, now, batch, counts)

        store._import_chunk = chunk
        with pytest.raises(Rejected):
            await store.import_ops("deals", [{"op": "add", "fields": {"company": "Acme"}},
                                             {"op": "add", "fields": {"company": "Globex"}}], 2)
        assert (await store.query("deals"))["rows"] == [], "nothing after the revert is written into its batch"
        await store.close()

    asyncio.run(body())


def test_a_batch_taken_back_leaves_the_records_changed_after_it(tmp_path):
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    owner = Writer("owner", "screen")

    async def seed() -> list[dict]:
        store = Collections(str(tmp_path / "collections.sqlite"))
        first = await store.apply(owner, "deals", [{"op": "add", "fields": {"company": "Acme", "stage": "lead"}}],
                                  1)
        await store.close()
        return first["records"]

    (acme,) = asyncio.run(seed())
    cli(tmp_path, "import", "deals", write(tmp_path, "deals.jsonl", [
        {"op": "set", "id": acme["id"], "version": 1, "fields": {"stage": "qualified"}},
        {"op": "add", "fields": {"company": "Example Ltd"}},
        {"op": "add", "fields": {"company": "Example Org"}}]))
    (batch,), = rows(tmp_path, "SELECT id FROM batches")
    (org,), = rows(tmp_path, "SELECT id FROM records WHERE json_extract(data, '$.company') = 'Example Org'")

    async def followup() -> None:  # the owner fixes a line of the import afterwards
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.apply(owner, "deals", [{"op": "set", "id": org, "version": 1, "fields": {"product": "Data"}}], 2)
        await store.close()

    asyncio.run(followup())
    code, said = cli(tmp_path, "revert-batch", batch, answer="n")
    assert code == 1 and f"deals/{org}" in said
    code, said = cli(tmp_path, "revert-batch", batch)
    assert code == 0
    assert rows(tmp_path, "SELECT json_extract(data, '$.company'), json_extract(data, '$.stage'), deleted IS NOT NULL "
                          "FROM records ORDER BY id") == [("Acme", "lead", 0), ("Example Ltd", None, 1),
                                                          ("Example Org", None, 0)]
    assert rows(tmp_path, f"SELECT state FROM batches WHERE id = '{batch}'") == [("applied",)], "not all of it is back"
    assert rows(tmp_path, "SELECT DISTINCT op, actor FROM changes WHERE batch != ? AND batch IS NOT NULL"
                .replace("?", f"'{batch}'")) == [("revert", "owner")]
    code, said = cli(tmp_path, "revert-batch", batch)
    assert code == 1 and f"deals/{org}" in said, "taken again: only the left one, named"

    cli(tmp_path, "import", "deals", write(tmp_path, "one.jsonl", [{"op": "add", "fields": {"company": "Acme Two"}}]))
    (whole,), = rows(tmp_path, "SELECT id FROM batches WHERE kind = 'import' ORDER BY id DESC LIMIT 1")
    code, said = cli(tmp_path, "revert-batch", whole)
    assert code == 0 and "1 changes of 1 records." in said
    assert rows(tmp_path, f"SELECT state FROM batches WHERE id = '{whole}'") == [("reverted",)]
    code, said = cli(tmp_path, "revert-batch", whole)
    assert code == 1
    assert cli(tmp_path, "revert-batch", "nope")[0] == 1


def test_what_a_command_writes_the_running_thread_sees_and_its_ids_go_on_from(tmp_path, monkeypatch):
    left = iter(range(2**40, 0, -1000))  # each id's random part smaller than the last: only the time can order them
    monkeypatch.setattr(collections.secrets, "randbits", lambda bits: next(left))
    store = Collections(str(tmp_path / "collections.sqlite"))
    owner = Writer("owner", "screen")
    asyncio.run(store.open())  # the platform's thread, open before the command ran
    try:
        for n in range(6):
            name = f"deals{n}"
            cli(tmp_path, "define", name, write(tmp_path, "deals.json", DEALS), clock=1000.0)
            done = asyncio.run(store.apply(owner, name, [{"op": "add", "fields": {"company": "Acme"}}], 1000.0))
            (theirs,), = rows(tmp_path, f"SELECT change_id FROM changes WHERE collection = '{name}' AND op = 'define'")
            assert done["change_ids"][0] > theirs, "in the same millisecond, ours still sorts after theirs"
        grown_form = grown(n={"type": "int"})
        cli(tmp_path, "define", "leads0", write(tmp_path, "grown.json", grown_form))
        done = asyncio.run(store.apply(owner, "leads0", [{"op": "add", "fields": {"company": "Acme", "n": 7}}], 1001))
        assert done["records"][0]["fields"]["n"] == 7, "the grown form, at the next call"
    finally:
        asyncio.run(store.close())


def test_everything_is_exported_to_an_empty_folder(tmp_path, monkeypatch):
    cli(tmp_path, "define", "deals", write(tmp_path, "deals.json", DEALS))
    cli(tmp_path, "import", "deals", write(tmp_path, "deals.jsonl", [{"op": "add", "fields": {"company": "Acme"}}]))

    async def upload() -> None:
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.put_file(b"acme invoice", "acme-invoice.pdf", "application/pdf", 1)
        await store.close()

    asyncio.run(upload())
    out = tmp_path / "out"
    code, said = cli(tmp_path, "export", str(out))
    assert code == 0 and "1 collections, 1 records, 2 changes, 1 files" in said
    assert json.loads((out / "schemas.json").read_text())[0]["collection"] == "deals"
    (record,) = [json.loads(line) for line in (out / "deals" / "records.jsonl").read_text().splitlines()]
    assert record["fields"] == {"company": "Acme"}
    assert [json.loads(line)["op"] for line in (out / "deals" / "changes.jsonl").read_text().splitlines()] == [
        "define", "add"]
    assert len((out / "batches.jsonl").read_text().splitlines()) == 1
    (file,) = [json.loads(line) for line in (out / "files.jsonl").read_text().splitlines()]
    assert (out / "attachments" / file["sha256"]).read_bytes() == b"acme invoice"
    code, said = cli(tmp_path, "export", str(out))
    assert code == 1
    assert not (out / ".snapshot.sqlite").exists(), "the copy it read from is gone"
    monkeypatch.setattr(collections.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=2**30))
    code, said = cli(tmp_path, "export", str(tmp_path / "full"))
    assert code == 1 and not (tmp_path / "full").exists()


def test_the_platform_runs_the_command_on_the_file_beside_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(platformd.PlatformConfig, "load",
                        classmethod(lambda cls: types.SimpleNamespace(state_db=str(tmp_path / "platform.sqlite"))))
    monkeypatch.setattr("sys.argv", ["retinue-platform", "collections", "define", "deals",
                                     write(tmp_path, "deals.json", DEALS)])
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    with pytest.raises(SystemExit) as exit_:
        platformd.main()
    assert exit_.value.code == 0
    assert rows(tmp_path, "SELECT collection FROM schemas") == [("deals",)]
    assert not (tmp_path / "platform.sqlite").exists(), "the platform's own file is not opened"


def test_a_device_is_added_with_its_token_shown_once_listed_and_revoked_without_a_question(tmp_path):
    code, said = cli(tmp_path, "device", "add", "laptop", "--scope", "write")
    token = said.splitlines()[-1]
    assert code == 0 and "[asked]" not in said
    assert rows(tmp_path, "SELECT name, scope, revoked FROM devices") == [("laptop", "write", None)]
    assert token not in str(rows(tmp_path, "SELECT * FROM devices")), "the file keeps only the token's SHA-256"
    code, said = cli(tmp_path, "device", "add", "laptop", "--scope", "read")
    assert code == 1
    code, said = cli(tmp_path, "device", "add", "Laptop!", "--scope", "read")
    assert code == 1
    with pytest.raises(SystemExit):
        cli(tmp_path, "device", "add", "phone")  # a scope is always said: read, or write
    code, said = cli(tmp_path, "device", "list")
    assert code == 0 and "1970-01-01" in said and "00:16" in said

    code, said = cli(tmp_path, "device", "revoke", "laptop", clock=2000.0)
    assert code == 0 and said
    assert rows(tmp_path, "SELECT revoked FROM devices") == [(2000.0,)]
    assert cli(tmp_path, "device", "revoke", "laptop")[0] == 0
    code, said = cli(tmp_path, "device", "revoke", "phone")
    assert code == 1
    assert "REVOKED 1970-01-01 00:33 UTC" in cli(tmp_path, "device", "list")[1]

    async def check() -> None:
        store = Collections(str(tmp_path / "collections.sqlite"))
        try:
            assert await store.device(token) is None
        finally:
            await store.close()
    asyncio.run(check())


def test_a_projects_description_is_read_from_its_folder_and_kept_on_yes(tmp_path):
    folder = tmp_path / "billing"
    (folder / "views").mkdir(parents=True)
    (folder / "views" / "burn.html").write_text("<p>burn</p>")
    (folder / "project.yaml").write_text(
        "collections:\n  invoices:\n    form: {fields: {state: {type: enum, values: [open, paid]}}}\n"
        "events:\n  paid: {collection: invoices, when: {field: state, to: [paid]}}\n"
        "views:\n  - {name: burn, kind: code, collection: invoices, file: views/burn.html}\n")
    code, said = cli(tmp_path, "project", "billing", str(folder), answer="n")
    assert code == 1 and "invoices form" in said and "billing events" in said and "Nothing applied." in said
    code, said = cli(tmp_path, "project", "billing", str(folder))
    assert code == 0
    code, said = cli(tmp_path, "project", "billing", str(folder))
    assert code == 0 and "[asked]" not in said
    (folder / "project.yaml").write_text("views:\n  - {name: x, kind: code, collection: invoices, file: ../x.html}\n")
    (tmp_path / "x.html").write_text("<p>outside</p>")
    code, said = cli(tmp_path, "project", "billing", str(folder))
    assert code == 1
