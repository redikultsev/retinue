# Collections

Records the owner keeps in tables rather than in the Base — what came in, what was paid, who answered — live in one
SQLite file of the platform's, `collections.sqlite`, beside `platform.sqlite` on the `platform-data` volume
(`retinue/collections.py`). For now it holds one thing: the files you upload.

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
  key), `name` (as the device named it: its last path part, without control or bidi characters, at most 200
  characters), `size`, `mime` (as the device said; never sniffed) and `created`.
- **Once.** The same bytes uploaded again are the same file: the answer is the row as it was (`new: false`), the first
  name and time kept, and nothing is written.
- **In order.** The bytes are written to a temporary `.part`, synced and renamed before the row is committed, so no
  row ever names a missing file; a `.part` a crash left behind is removed at the next start, and a blob a crash left
  without its row gets its row with the next upload of it.
- **Out.** A file comes back only as a download: `Content-Disposition: attachment`, `nosniff` and the sandbox policy,
  so opened by itself it is a document of no origin.

| Call | Answer |
|---|---|
| `POST /api/c/_files` (body: the file's bytes; `Content-Type` its type, `X-Name` its name percent-encoded) | `{file: {sha256, name, size, mime, created}, new}`; 413 over 40 MB, 400 when empty, 503 when the store is busy |
| `GET /api/c/_files` | `{schema, files, max}`: the newest 1000 files, the newest first; `max` the largest file taken, in bytes |
| `GET /api/c/_files/{sha256}` | the file's bytes, as a download; 404 for anything else |

Every call is the owner's alone, behind the app's login, and a POST carries `X-Retinue: 1` like every other
([face.md](face.md#logging-in)). A new file goes to every open app as the stream's `upload` event, its row as `data`,
so a file sent from the phone shows on the laptop at once.

For example, from a shell with the app's cookie:

```bash
curl https://app.example.com/api/c/_files -H 'X-Retinue: 1' -H 'Cookie: __Host-retinue=<cookie>' \
  -H 'Content-Type: application/pdf' -H 'X-Name: bank-a-2026-09.pdf' --data-binary @bank-a-2026-09.pdf
```

nginx takes up to 41 MB on `/api/c/_files` and streams it to the platform as it comes, and streams a download back,
never spooling either to its 16 MB `/tmp` ([face.md](face.md#serving)).
