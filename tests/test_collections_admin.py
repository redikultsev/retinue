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

LEADS = {"space": "career", "form": {"fields": {
    "company": {"type": "text", "required": True},
    "role": {"type": "text"},
    "stage": {"type": "enum", "values": ["evaluated", "applied", "closed"]},
}, "key": ["company", "role"]}}


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
    return {"space": "career", "form": {**LEADS["form"], "fields": {**LEADS["form"]["fields"], **fields}}}


def test_a_collection_is_made_from_a_file_after_its_diff_and_a_yes(tmp_path):
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS), answer="n")
    assert code == 1 and "new collection, space career, form revision 1" in said and "[asked]" in said
    assert '+    "company": {' in said and "Nothing applied." in said
    assert rows(tmp_path, "SELECT count(*) FROM schemas") == [(0,)], "nothing kept without a yes"
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    assert code == 0 and "Applied: leads is at form revision 1." in said
    assert rows(tmp_path, "SELECT op, actor, via FROM changes") == [("define", "define:1", "define")]
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    assert code == 0 and "already has this form" in said and "[asked]" not in said
    code, said = cli(tmp_path, "list")
    assert said == "leads  space career  form revision 1  0 records"
    code, said = cli(tmp_path, "show", "leads")
    assert code == 0 and '"session": {' in said, "the default rights, shown"
    assert cli(tmp_path, "show", "nowhere") == (1, "Refused: there is no collection 'nowhere'")


@pytest.mark.parametrize("form, reason", [
    ({k: v for k, v in LEADS["form"]["fields"].items() if k != "role"}, "field 'role' would be gone"),
    ({**{k: v for k, v in LEADS["form"]["fields"].items() if k != "role"}, "title": {"type": "text"}},
     "field 'role' would be gone"),  # a rename is a field gone and another come
    ({**LEADS["form"]["fields"], "role": {"type": "int"}}, "field 'role' would become another type"),
    ({**LEADS["form"]["fields"], "stage": {"type": "enum", "values": ["evaluated", "sent", "closed"]}},
     "field 'stage' would lose 'applied'"),
])
def test_a_form_only_grows_until_its_first_case(tmp_path, form, reason):
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "next.json", {"space": "career", "form": {
        **LEADS["form"], "fields": form, "key": ["company"]}}))
    assert code == 1 and reason in said and "a form only grows for now" in said and "(⏸)" in said
    assert "[asked]" not in said
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(1,)]


def test_a_grown_form_is_tried_on_every_record_and_kept_only_when_they_all_fit(tmp_path):
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    cli(tmp_path, "import", "leads", write(tmp_path, "leads.jsonl", [
        {"op": "add", "fields": {"company": "Acme", "stage": "applied"}},
        {"op": "add", "fields": {"company": "Example Ltd"}}]))
    more = grown(stage={"type": "enum", "values": ["evaluated", "applied", "interview", "closed"], "index": True},
                 origin={"type": "enum", "values": ["inbound", "outbound"]})
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "more.json", more))
    assert code == 0 and "All 2 records fit it." in said and '+        "interview",' in said
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(2,)]
    strict = grown(**{**more["form"]["fields"], "origin": {"type": "enum", "values": ["inbound", "outbound"],
                                                            "required": True}})
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "strict.json", strict))
    assert code == 1 and "2 of 2 records do not fit it:" in said and "'origin' is required" in said
    assert "[asked]" not in said, "a form the records do not fit is never asked about"
    assert rows(tmp_path, "SELECT rev FROM schemas") == [(2,)]
    unique = {**more, "form": {**more["form"], "unique": [["stage"]]}}
    cli(tmp_path, "import", "leads", write(tmp_path, "one.jsonl", [{"op": "add", "fields": {"company": "Acme Two",
                                                                                            "stage": "applied"}}]))
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "unique.json", unique))
    assert code == 1 and "records of leads share a stage" in said


def test_a_form_changed_since_its_trial_is_not_applied_over(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(Writer("owner", "define"), "leads", "career", LEADS["form"], 1)
        with pytest.raises(Rejected, match="at revision 1, not the one this was tried on"):
            await store.define(Writer("define:3", "define"), "leads", "career", grown(n={"type": "int"})["form"], 2)
        assert (await store.define(Writer("define:2", "define"), "leads", "career", grown(n={"type": "int"})["form"],
                                   2))["form_rev"] == 2
        await store.close()

    asyncio.run(body())


def test_rights_never_come_with_a_form(tmp_path):
    rights = {"writers": {"assistant": {"add": True, "set": "*", "delete": True}}}
    for given in ({**LEADS, "rights": rights}, {**LEADS, "set_by": {}},
                  {"space": "career", "form": {**LEADS["form"], "writers": rights["writers"]}},
                  {"space": "career", "form": {**LEADS["form"], "rights": rights}}):
        code, said = cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", given))
        assert code == 1 and "rights are the owner's alone, set from the server's terminal" in said
        assert "collections rights <collection> <file>" in said
    assert rows(tmp_path, "SELECT count(*) FROM schemas") == [(0,)]
    code, said = cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", {**LEADS, "views": []}))
    assert code == 1 and 'a define file takes "space" and "form", not \'views\'' in said


def test_rights_are_set_from_a_file_after_their_diff(tmp_path):
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    rights = {"writers": {"assistant": {"set": ["stage"]}}, "set_by": {"stage": {"closed": ["owner"]}}}
    code, said = cli(tmp_path, "rights", "leads", write(tmp_path, "rights.json", rights), answer="")
    assert code == 1 and '+      "set": [' in said and "Nothing applied." in said
    code, said = cli(tmp_path, "rights", "leads", write(tmp_path, "rights.json", rights))
    assert code == 0 and "Applied: the rights of leads." in said
    (rights_kept,), = rows(tmp_path, "SELECT rights FROM schemas")
    assert json.loads(rights_kept)["writers"] == {"assistant": {"add": False, "set": ["stage"], "delete": False}}
    assert rows(tmp_path, "SELECT actor, via FROM changes ORDER BY seq DESC LIMIT 1") == [("owner", "define")]
    code, said = cli(tmp_path, "rights", "leads", write(tmp_path, "rights.json", rights))
    assert code == 0 and "already has these rights" in said
    code, said = cli(tmp_path, "rights", "leads", write(tmp_path, "bad.json", {"writers": {"owner": {}}}))
    assert code == 1 and "is not a writer's class" in said


def test_an_import_is_one_batch_kept_whole_or_not_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 2)  # the refused line in the second transaction
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    lines = [{"op": "add", "fields": {"company": "Acme", "role": "Backend"}, "as_of": "2026-09-01",
              "src": "file:" + "a" * 64 + "#row=2"},
             {"op": "add", "fields": {"company": "Example Ltd", "stage": "applied"}, "as_of": "2026-09-02"},
             {"op": "add", "fields": {"company": "Example Org", "stage": "hired"}}]
    code, said = cli(tmp_path, "import", "leads", write(tmp_path, "bad.jsonl", lines))
    assert code == 1 and "Refused: line 3: 'stage' is one of evaluated, applied, closed: not 'hired'" in said
    assert "[asked]" not in said, "the trial run refuses it before anything is asked"
    lines[2]["fields"]["stage"] = "closed"
    code, said = cli(tmp_path, "import", "leads", write(tmp_path, "leads.jsonl", lines), answer="n")
    assert code == 1 and "3 lines into leads: 3 to add, 0 to change, 0 already so." in said
    assert rows(tmp_path, "SELECT count(*) FROM records") == [(0,)]
    code, said = cli(tmp_path, "import", "leads", write(tmp_path, "leads.jsonl", lines))
    assert code == 0 and "3 added" in said
    (batch, state), = rows(tmp_path, "SELECT id, state FROM batches")
    assert state == "applied" and f"revert-batch {batch}" in said
    assert rows(tmp_path, "SELECT DISTINCT actor, via, batch FROM changes WHERE op = 'add'") == [
        (f"import:{batch}", "import", batch)]
    assert rows(tmp_path, "SELECT as_of FROM changes WHERE op = 'add' ORDER BY seq") == [
        ("2026-09-01",), ("2026-09-02",), (None,)]
    code, said = cli(tmp_path, "import", "leads", str(tmp_path / "nowhere.jsonl"))
    assert code == 1 and "cannot read" in said
    (tmp_path / "half.jsonl").write_text('{"op": "add", "fields": {"company": "Acme Two"}}\n{nope\n')
    assert cli(tmp_path, "import", "leads", str(tmp_path / "half.jsonl")) == (1, "Refused: line 2: not a JSON operation")


def test_a_line_refused_after_others_were_kept_takes_them_back(tmp_path, monkeypatch):
    monkeypatch.setattr(collections, "IMPORT_CHUNK", 1)
    owner = Writer("owner", "screen")

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(owner, "leads", "career", LEADS["form"], 1)
        real, calls = store._import_chunk, []

        def chunk(collection, ops, start, *rest):  # the owner adds Acme from the app between two transactions
            calls.append(start)
            if start == 1:
                store._apply(owner, "leads", [{"op": "add", "fields": {"company": "Acme", "role": "Backend"}}], 2,
                             None, None)
            return real(collection, ops, start, *rest)

        store._import_chunk = chunk
        with pytest.raises(Rejected, match=r"line 2: possible duplicate of .* — nothing of the file is kept"):
            await store.import_ops("leads", [{"op": "add", "fields": {"company": "Example Ltd"}},
                                             {"op": "add", "fields": {"company": "Acme", "role": "Backend"}}], 3)
        assert calls == [0, 1]
        kept = await store.query("leads")
        assert [r["fields"]["company"] for r in kept["rows"]] == ["Acme"], "the owner's record stays, the line goes"
        assert await store._run(lambda: store._db.execute("SELECT kind, state FROM batches ORDER BY id").fetchall()) \
            == [("import", "reverted"), ("revert", "applied")]
        await store.close()

    asyncio.run(body())


def test_a_batch_taken_back_leaves_the_records_changed_after_it(tmp_path):
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    owner = Writer("owner", "screen")

    async def seed() -> list[dict]:
        store = Collections(str(tmp_path / "collections.sqlite"))
        first = await store.apply(owner, "leads", [{"op": "add", "fields": {"company": "Acme", "stage": "evaluated"}}],
                                  1)
        await store.close()
        return first["records"]

    (acme,) = asyncio.run(seed())
    cli(tmp_path, "import", "leads", write(tmp_path, "leads.jsonl", [
        {"op": "set", "id": acme["id"], "version": 1, "fields": {"stage": "applied"}},
        {"op": "add", "fields": {"company": "Example Ltd"}},
        {"op": "add", "fields": {"company": "Example Org"}}]))
    (batch,), = rows(tmp_path, "SELECT id FROM batches")
    (org,), = rows(tmp_path, "SELECT id FROM records WHERE json_extract(data, '$.company') = 'Example Org'")

    async def touch() -> None:  # the owner fixes a line of the import afterwards
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.apply(owner, "leads", [{"op": "set", "id": org, "version": 1, "fields": {"role": "Data"}}], 2)
        await store.close()

    asyncio.run(touch())
    code, said = cli(tmp_path, "revert-batch", batch, answer="n")
    assert code == 1 and f"leads/{org}: set by owner" in said and "Takes back 2 changes of 2 records." in said
    code, said = cli(tmp_path, "revert-batch", batch)
    assert code == 0 and "1 left as they are" in said
    assert rows(tmp_path, "SELECT json_extract(data, '$.company'), json_extract(data, '$.stage'), deleted IS NOT NULL "
                          "FROM records ORDER BY id") == [("Acme", "evaluated", 0), ("Example Ltd", None, 1),
                                                          ("Example Org", None, 0)]
    assert rows(tmp_path, f"SELECT state FROM batches WHERE id = '{batch}'") == [("applied",)], "not all of it is back"
    assert rows(tmp_path, "SELECT DISTINCT op, actor FROM changes WHERE batch != ? AND batch IS NOT NULL"
                .replace("?", f"'{batch}'")) == [("revert", "owner")]
    code, said = cli(tmp_path, "revert-batch", batch)
    assert code == 1 and "Nothing of batch" in said and f"leads/{org}" in said, "taken again: only the left one, named"

    cli(tmp_path, "import", "leads", write(tmp_path, "one.jsonl", [{"op": "add", "fields": {"company": "Acme Two"}}]))
    (whole,), = rows(tmp_path, "SELECT id FROM batches WHERE kind = 'import' ORDER BY id DESC LIMIT 1")
    code, said = cli(tmp_path, "revert-batch", whole)
    assert code == 0 and "1 changes of 1 records." in said
    assert rows(tmp_path, f"SELECT state FROM batches WHERE id = '{whole}'") == [("reverted",)]
    code, said = cli(tmp_path, "revert-batch", whole)
    assert code == 1 and "is taken back already" in said
    assert cli(tmp_path, "revert-batch", "nope") == (1, "Refused: no batch 'nope'")


def test_what_a_command_writes_the_running_thread_sees_and_its_ids_go_on_from(tmp_path, monkeypatch):
    left = iter(range(2**40, 0, -1000))  # each id's random part smaller than the last: only the time can order them
    monkeypatch.setattr(collections.secrets, "randbits", lambda bits: next(left))
    store = Collections(str(tmp_path / "collections.sqlite"))
    owner = Writer("owner", "screen")
    asyncio.run(store.open())  # the platform's thread, open before the command ran
    try:
        for n in range(6):
            name = f"leads{n}"
            cli(tmp_path, "define", name, write(tmp_path, "leads.json", LEADS), clock=1000.0)
            done = asyncio.run(store.apply(owner, name, [{"op": "add", "fields": {"company": "Acme"}}], 1000.0))
            (theirs,), = rows(tmp_path, f"SELECT change_id FROM changes WHERE collection = '{name}' AND op = 'define'")
            assert done["change_ids"][0] > theirs, "in the same millisecond, ours still sorts after theirs"
        grown_form = grown(n={"type": "int"})
        cli(tmp_path, "define", "leads0", write(tmp_path, "grown.json", grown_form))
        done = asyncio.run(store.apply(owner, "leads0", [{"op": "add", "fields": {"company": "Acme", "n": 7}}], 1001))
        assert done["records"][0]["fields"]["n"] == 7, "the grown form, at the next call"
    finally:
        asyncio.run(store.close())


def test_everything_is_exported_to_an_empty_folder(tmp_path):
    cli(tmp_path, "define", "leads", write(tmp_path, "leads.json", LEADS))
    cli(tmp_path, "import", "leads", write(tmp_path, "leads.jsonl", [{"op": "add", "fields": {"company": "Acme"}}]))

    async def upload() -> None:
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.put_file(b"acme invoice", "acme-invoice.pdf", "application/pdf", 1)
        await store.close()

    asyncio.run(upload())
    out = tmp_path / "out"
    code, said = cli(tmp_path, "export", str(out))
    assert code == 0 and "1 collections, 1 records, 2 changes, 1 files" in said
    assert json.loads((out / "schemas.json").read_text())[0]["collection"] == "leads"
    (record,) = [json.loads(line) for line in (out / "leads" / "records.jsonl").read_text().splitlines()]
    assert record["fields"] == {"company": "Acme"}
    assert [json.loads(line)["op"] for line in (out / "leads" / "changes.jsonl").read_text().splitlines()] == [
        "define", "add"]
    assert len((out / "batches.jsonl").read_text().splitlines()) == 1
    (file,) = [json.loads(line) for line in (out / "files.jsonl").read_text().splitlines()]
    assert (out / "attachments" / file["sha256"]).read_bytes() == b"acme invoice"
    code, said = cli(tmp_path, "export", str(out))
    assert code == 1 and "is not an empty folder" in said


def test_the_platform_runs_the_command_on_the_file_beside_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(platformd.PlatformConfig, "load",
                        classmethod(lambda cls: types.SimpleNamespace(state_db=str(tmp_path / "platform.sqlite"))))
    monkeypatch.setattr("sys.argv", ["retinue-platform", "collections", "define", "leads",
                                     write(tmp_path, "leads.json", LEADS)])
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    with pytest.raises(SystemExit) as exit_:
        platformd.main()
    assert exit_.value.code == 0
    assert rows(tmp_path, "SELECT collection FROM schemas") == [("leads",)]
    assert not (tmp_path / "platform.sqlite").exists(), "the platform's own file is not opened"
