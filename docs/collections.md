# Collections

Records the owner keeps in tables rather than in the Base — what came in, what was paid, who answered — live in one
SQLite file of the platform's, `collections.sqlite`, beside `platform.sqlite` on the `platform-data` volume
(`retinue/collections.py`). It holds the files you upload and the records below; nothing writes records yet — the
app, the agents and the server's commands come next, each through the one `apply`.

## One thread, one connection

Every read and write of the file goes through one dedicated thread, holding the file's only connection; the
platform's loop only awaits it. SQLite's busy handler sleeps on the thread that called it, and the loop also serves
the hooks, the gate and the app: a writer that waited there would stall them all. The thread waits at most 2 s for a
lock — the night's backup copy holds one while it copies — and then refuses at once: the app answers 503 “The store is
busy (an import or the night's backup copy) — try again in a moment.” and nothing was written. The file uses a
rollback journal, like every database the backup copies.

The platform makes the file at its start, uploads or not, so the backup can require it ([backup.md](backup.md)).

## Uploads

“Uploads” in the app (`/uploads`, [face.md](face.md#uploads)) takes any file up to 40 MB from any of your devices:
a statement, an export, a screenshot, a photo of a receipt. Nothing is refused for its type, and nothing is opened,
parsed or shown to an agent: a file is kept as it came.

- **Where.** Its bytes go to `attachments/<sha256>` on the same volume, its row to the table `files`: `sha256` (the
  key), `name` (as the device named it: its last path part, without control, bidi, private-use or lone surrogate
  characters, at most 200 characters), `size`, `mime` (as the device said; never sniffed) and `created`.
- **Once.** The same bytes uploaded again are the same file: the answer is the row as it was (`new: false`), the first
  name and time kept, and nothing is written — unless its file was lost (a partial restore, a delete by hand), which
  the same bytes put back.
- **In order.** The bytes are written to a temporary `.part`, synced and renamed, and the folder synced, before the
  row is committed, so no row ever names a missing file, a power cut included; a write that fails removes its `.part`
  at once, one a crash left behind is removed at the next start, and a blob a crash left without its row gets its row
  with the next upload of it. The backup leaves `.part` files out.
- **Room.** An upload that would leave less than 1 GiB free on the volume is refused (507) and nothing is kept: the
  same volume holds the platform's databases, which must still be able to write.
- **Out.** A file comes back only as a download: `Content-Disposition: attachment`, `nosniff` and the sandbox policy,
  so opened by itself it is a document of no origin.

| Call | Answer |
|---|---|
| `POST /api/c/_files` (body: the file's bytes; `Content-Type` its type, `X-Name` its name percent-encoded) | `{file: {sha256, name, size, mime, created}, new}`; 413 over 40 MB, 400 when empty, 503 when the store is busy or stopping, 507 when the disk is nearly full |
| `GET /api/c/_files` | `{schema, files, max}`: the newest 1000 files, the newest first; `max` the largest file taken, in bytes |
| `GET /api/c/_files/{sha256}` | the file's bytes, as a download; 404 for anything else |

Every call is the owner's alone, behind the app's login, and a POST carries `X-Retinue: 1` like every other
([face.md](face.md#logging-in)). A new file goes to every open app as the stream's `upload` event, its row as `data`,
so a file sent from the phone shows on the laptop at once.

For example, from a shell with the app's cookie:

```bash
curl https://app.example.com/api/c/_files -H 'X-Retinue: 1' -H 'Cookie: __Host-retinue=<cookie>' \
  -H 'Content-Type: application/pdf' -H 'X-Name: acme-invoice-2026-09.pdf' --data-binary @acme-invoice-2026-09.pdf
```

nginx takes up to 41 MB on `/api/c/_files` and streams it to the platform as it comes, and streams a download back,
never spooling either to its 16 MB `/tmp` ([face.md](face.md#serving)).

## Records

A collection is a set of records of one form: `leads`, say, with a company, a role and a stage. Its name is lowercase
letters, digits and `_`, starting with a letter (at most 40), so it never meets a reserved `_files`.

| Table | What |
|---|---|
| `schemas` | one row per collection: its Space, its `form`, its `rights`, its `views` (empty for now), `rev` (the form's revision) |
| `records` | each record's current state: `id`, `data` (its fields, one JSON document), `version`, `created`, `updated`, `deleted` |
| `changes` | every change of a record or a form, append-only: triggers abort any `UPDATE` or `DELETE` |
| `batches` | changes that go together (an import, a form's application): `kind`, `state` (`applying`, then `applied`) |
| `idems` | the calls already made under an idem, by the writer's class and the collection, with what they made |

**A form** is `{"fields": {name: {"type": …, "required": true|false, "index": true|false}}, "key": [...],
"unique": [[...], ...]}`, checked strictly: an unknown key stops it, and a form is at most 64 KB. A form is the
owner's to apply (from the server's terminal; an agent's way to propose one comes later), and a `define` that
changes it is kept in `changes` as `{"form": …}` before and after. The types:

| Type | Value |
|---|---|
| `text` | a string of at most 2000 characters, kept without control (but newlines and tabs), bidi or private-use characters |
| `int` | a whole JSON number, within ±2^53 |
| `enum` | one of the field's `values` (1–200, each lowercase letters, digits and `_`) |
| `date` | `YYYY-MM-DD`, a real date |
| `bool` | `true` or `false` |
| `ref` | the id of a record, not deleted, in the collection the field's `to` names |

`null` means unknown: a field set to `null` is left out of the record, and zero or an empty string are values. A
`required` field cannot be left out.

**Keys.** A record's id is its identity; the fields that name it in the world are ordinary fields, and can be
changed.

| In the form | Does |
|---|---|
| `"unique": [["n"], ["account", "ext_id"]]` | one record only holds each combination, among those not deleted: another write of it is refused, `taken: <id> has n 7`, with that record. A partial unique index keeps it, so nothing past the store's own check can break it; a form whose records already break it is refused, and a record that is unknown in one of the fields holds nothing |
| `"key": ["company", "role"]` | a soft key: an `add` (or a `set` of a key field, or a `restore`) matching another record on all of them — text by its letters, case and the spaces around it aside — is refused as `possible duplicate of <id>`, with that record. The writer changes that record instead, or says `"distinct": true` on the operation and it is kept at once |
| `"index": true` on a field | an index on it, for the queries that filter by it |

Indexes are made only for what a form asks, each partial (`… WHERE collection = 'leads' AND deleted IS NULL`), on the
same expression every query uses, so the planner takes it ([partialindex](https://www.sqlite.org/partialindex.html),
[expridx](https://www.sqlite.org/expridx.html)). A form that no longer asks for one drops it.

**Ids.** A record's id, a change's `change_id` and a batch's id are ULIDs the store gives: 26 characters, the time
first, so they sort as they were made. They only grow — in the same millisecond, or under a clock that stepped back,
the next is the last plus one, and the store starts from the file's newest id — so whatever names changes from
outside (a cursor, the evening list) never skips one. `seq` orders the rows within the file only and is never shown.

### One way to write

`apply(writer, collection, ops)` is the only write of a record: 1 to 200 operations on one collection, checked against
its form and kept in one transaction — all of them, or none, with the reason in words a model can correct its call by
(`ops[1]: 'stage' is one of evaluated, applied, closed: not 'hired'`). It answers `{name, rev, change_ids, records}`:
`rev` is the collection's newest `change_id` (what a screen compares to know it is behind), and for each operation the
change it made and the record after it.

| Operation | Takes | Does |
|---|---|---|
| `add` | `fields` | a new record, version 1 |
| `set` | `id`, `version`, `fields` | lays the fields over the record's; `null` makes one unknown |
| `delete` | `id`, `version` | marks the record deleted; it stays, and a `set` on it is refused until a restore |
| `restore` | `id`, `version` | brings a deleted record back |
| `revert` | `change_id`, `version` | sets what that change changed back to what it was, as a change of its own |

- **`version`.** Every operation but `add` gives the version the writer read. Another is refused with the record as it
  is now and its last change, so two writers never overwrite each other unseen.
- **Already so.** A `set` to the values a record has, a `delete` of a deleted one, writes nothing: its change is
  `null` and the version stays.
- **`as_of`** (`YYYY-MM-DD`, optional on all but `revert`) is the date the world became so. A wrong entry is fixed by a
  `set` without it; a change in the world with a known date carries it.
- **`src`** says where it came from, of a closed kind: `mail:<account>/<msgid>`, `tg:<chat>/<msg>`,
  `owner_words:<event>`, `cal:<account>/<event>`, `file:<sha256>#row=<n>`, `record:<id>`, `web:<url>`.
- **The writer** is kept with every change: `actor` (`owner`, `assistant`, `session:<id>`, `mac:<device>/<session>`,
  `code:<rule>`, `import:<batch>`, `define:<rev>`), `via` (`screen`, `session`, `mac`, `assistant`, `mail`, `import`,
  `define`), the model, and the marks of foreign text it had read.
- **A revert** is refused when something has changed the same field since — naming who, what and which change — and
  taking back an `add` deletes the record. A revert is a change like any other, so it is taken back the same way.

Each change keeps what it changed, as it was (`before`) and as it became (`after`); a record's deleted state appears
there as `_deleted`, which no field's name can be.

### Rights

Who may write what is the collection's `rights`, beside its form and apart from it: a form is what an agent may one
day propose, and rights it could widen with one press would be no rights. They are the owner's alone — set by
`rights(owner, collection, rights)`, which the server's terminal calls, never an agent's path — and kept as a
`define` change, `{"rights": …}` before and after.

```json
{
  "writers": {
    "assistant": {"set": ["state"]},
    "code:mail-sent": {"set": ["state"]},
    "session": {"add": true, "set": "*", "delete": "propose"},
    "mac": {"add": true, "set": "*", "delete": false}
  },
  "set_by": {"state": {"sent": ["owner", "code:mail-sent"]}}
}
```

- **`writers`**, for each class of writer — `assistant`, `session`, `mac`, `code:<rule>` — whether it adds records,
  which fields it gives a value (`"*"` all of them; an `add`'s fields too) and whether it deletes and restores:
  `true`, `false`, or `"propose"`, a proposal the owner applies. What a class is not given it does not do: the
  refusal names the field and the class (`'text' is not open to assistant in touches: propose the change to the owner
  instead`). A revert needs the rights of what it does: setting the fields back, or deleting the record an add made.
- **`set_by`**, for a value of an enum: the only classes that give the field it. An agent never marks a message
  `sent` by itself; the owner's button, or the rule that saw it in the sent mail, does.
- **The owner may do anything**, and so may what he runs from the server's terminal (`import:<batch>`,
  `define:<rev>`): they are not listed.
- **A new collection's rights** are narrow: a session adds and sets every field and deletes by a proposal; the Mac
  adds and sets every field and does not delete; the assistant and every `code:` rule do nothing until the owner
  gives them something.

The store checks the writer it is given: whoever calls `apply` names the writer truthfully, from the path the call
came by.

### Once

- **`idem`** names a call: `apply(…, idem="…")` made again by the same class of writer on the same collection —
  a retry after a timeout, a tool call repeated — changes nothing and answers the first call's `change_ids`, with the
  records as they are now. A refused call leaves no idem behind, so it can be made again, corrected. Each change keeps
  the idem of the call that made it.
- **A mail** (`src` `mail:…`) needs none: the store derives one for each field an operation sets, from the class of
  writer, the mail, the record and the field. A mail read again changes nothing it changed before, even when the
  owner has changed it since; a field it had not set is set, and another mail is another change. The n-th `add` from
  one mail is made once.

## Reading

`get(collection, id)` is one record; `query` and `count` read many, through a filter that is data, not a language:
each condition `{"field": …, "op": …, "value": …}`, up to 16 that all hold.

| `op` | `value` | Matches |
|---|---|---|
| `eq` | one value of the field's type | the field is it |
| `in` | 1 to 200 of them | the field is one of them |
| `between` | `["2026-09-01", "2026-09-30"]` | a `date` within them, both included |
| `empty`, `not_empty` | none | the field is unknown, or known |
| `ref_is` | a record's id | a `ref` names it |

A `field` of `lead.stage` follows a `ref` one level, with `eq` or `in` only: the touches whose lead is `applied`. A
field's name is checked against the form before it goes into the statement, and every value is a parameter: a name
the form does not have is refused before any SQL is run on the records.

- **`query(collection, filter, fields, limit, after)`** answers `{rows, next}`: records not deleted (all of them
  with `deleted=True`), the oldest first, each `{id, fields, version, created, updated, deleted}` with only `fields`
  when they are named. At most 200 rows and 60 KB of them — what an agent's tool call can take back — and `next`, when
  there are more, is the id to pass as `after`.
- **`count(collection, filter, group)`** answers how many; with `group` an enum or a bool, how many of each value,
  `[{value, count}]`, the most first, `value` `null` for the records it is unknown in.
