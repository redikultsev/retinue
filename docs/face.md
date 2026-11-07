# The face: the platform from your phone

The **face** is the platform's app for your phone: the list of sessions by track, a live transcript, the approval
cards with their two buttons, a new session, two sessions side by side on a wide screen. It is a web app (a PWA you
can add to the home screen) and the same page opened from a card in Telegram as a Mini App. It is reachable only
through WireGuard, from your own devices.

```
phone ── WireGuard ── Traefik (allow list: your devices; TLS; adds the key) ── face (nginx: the app; /api/*, /mcp/device on)
                                                    └── face-platform (internal) ── platform 172.16.96.10:9600
```

The **face API** is `/api/*` — and `/mcp/device`, the collections for the agents on your computer, by a device's token instead
of the login ([collections.md](collections.md#from-the-owners-computer)) — on the platform's second port (9600), on its own address on its own network
`face-platform`, where only the face's nginx is besides the platform. The router, the lanes' hooks and the lanes stay
on 9500 and never see that network. nginx in front adds nothing secret: who may do what is decided by the face API
alone.

## Turning it on

It is deployed exactly like the [pages](pages.md): a public name for the certificate, split DNS inside the VPN,
Traefik letting through your devices only.

### 1. DNS: a public record for the certificate (once)

Let's Encrypt checks the name over plain HTTP, so the name needs a public A record pointing at the server. At your
DNS provider: `app.in.<your domain>` → the server's public address (the same as `pages.in.…`). Wait until it resolves
from outside:

```bash
dig +short @1.1.1.1 app.in.example.com          # the server's public address
```

Nothing else is public: Traefik answers anyone who is not one of your devices with 404, over HTTPS and plain HTTP
alike. The name itself is public — every certificate goes to the CT logs.

### 2. Your devices reach it through the VPN

Split DNS (the VPN's `10.8.0.1`) already answers `10.8.0.1` for every `*.in` name, so there is nothing to add on the
server. A device must use it — VPN on, DNS `10.8.0.1` (see [pages.md](pages.md), step 2) — otherwise it resolves
the public record, goes over the internet and gets 404.

```bash
dig +short app.in.example.com                   # on the device, VPN on: 10.8.0.1
```

### 3. The server

The platform must be on (`PLATFORM=1`, [platform.md](platform.md)) and the bot token filled in `stack.env`.

```bash
cd /opt/retinue && sudo git pull
sudo TELEGRAM_OWNER_ID=<id> FACE=1 FACE_HOST=app.in.example.com bash /opt/retinue/deploy/setup.sh
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

The devices: give them once, `DEVICES=10.8.0.2/32,10.8.0.3/32` — kept in `/srv/retinue/devices.txt`, the one list
for the face and the pages (an earlier version's list, the life hub's or the face's own, is taken over). A new device:
edit the list and run `setup.sh` again.

`setup.sh` keeps the address in `/srv/retinue/face/host` (later runs need no switch) and adds the compose profile
`face`; makes a key in `secrets.env` and renders `/etc/dokploy/traefik/dynamic/face.yml` (your devices, the key
Traefik adds; root's, 600) and `/srv/retinue/face/key.conf` (nginx's check of it); puts `face_url` in `router.yaml`
and `FACE_HOST`, `RETINUE_FACE_ORIGIN`, `TELEGRAM_OWNER_ID` and `RETINUE_WEBAPP_KEY` in `stack.env`. The last is
derived from `TELEGRAM_BOT_TOKEN` on every run — a new bot token, run `setup.sh` again — and never printed. The
image is built from `face/` with the exact pnpm of its `package.json` and its lockfile.

### 4. Use it

- **Telegram:** the bot's menu button “Open” (beside the input) opens the app as a Mini App, logged in by
  Telegram's signature; so does “Open” under the answer to `/app`, which also carries a link and a code for a
  browser. The approval cards carry “Open in the app” (the Mini App). The router sets the menu button at every
  start (`setChatMenuButton` for the owner's chat only, the same call each time); with no `face_url` it puts
  Telegram's default back.
- **iPhone, as an app:** VPN on, open `https://app.in.example.com/` in Safari → **Share** → **Add to Home Screen** →
  open it from the Home Screen. Its cookies are not Safari's: send `/app` and type the code into the app.

### What the platform reads

The face API starts only when the platform has an origin for it. The platform reads, from its environment (compose
passes all four; `RETINUE_FACE_HOST` is the platform's static address on `face-platform`):

| Name | What |
|---|---|
| `RETINUE_FACE_ORIGIN` | `https://<face host>` exactly as the browser loads the app (no path, no trailing slash). Without it the face API does not start and `/app` says so. |
| `RETINUE_WEBAPP_KEY` | `HMAC_SHA256(key="WebAppData", msg=<bot token>)` in hex — the key Telegram signs a Mini App's data with. Derived once by `setup.sh`; the platform never holds the bot token. Without it the Mini App cannot log in; codes still work. |
| `TELEGRAM_OWNER_ID` | your numeric Telegram id: the only user a Mini App login is for. |
| `RETINUE_FACE_HOST` | the IP address the face API listens on: the platform's own address on `face-platform`. Default `127.0.0.1` — reachable from nowhere. Never `0.0.0.0`: the platform also sits on the lanes' networks, and a lane must not reach the face (not even to spend the login limit). |

and `face_port` from `platform.yaml` (default 9600; it must differ from `listen_port`), and `pages_origin`
(`https://pages.in.<your domain>`, the pages service's address): the only origin a page of “Pages” may open on — without it
the router's list of pages is refused and “Pages” stays empty ([below](#pages)). The router takes `face_url:
https://<face host>` from `router.yaml`: with it, `/app` logs you in (without it, `/app` is the
assistant's, as before), and every approval card gets the button “Open in the app”.

## Logging in

- **From the chat:** send `/app` to the bot. It answers with a link `https://<face host>/login#<code>` and the code
  itself. Open the link, or — in a PWA installed on an iPhone, whose cookies are not Safari's — type the code. The code
  is 10 characters (no 0/O or 1/I/L; case, spaces and dashes do not matter), works once, for 10 minutes; a new `/app`
  voids the code before it.
- **From a card:** “Open in the app” opens that card in Telegram's Mini App, which logs in by Telegram's
  signature of who opened it: valid for 5 minutes, and only for you.

Either sets a cookie that lasts 30 days from the last time the app was used; logging out (`POST
/api/auth/logout`) ends it.

## What it guarantees

- **Nothing without a login.** Every `/api/*` but `/api/auth/*` answers 401 without a live cookie. The cookie
  (`__Host-retinue`: `HttpOnly; Secure; SameSite=Strict; Path=/`) is 32 random bytes; the platform keeps only its
  SHA-256, as it keeps only the SHA-256 of a code. A copy of the platform's database logs no one in.
- **No action from another page.** Every POST needs the header `X-Retinue: 1` and, when the browser sends an
  `Origin`, exactly `RETINUE_FACE_ORIGIN`; otherwise 403 and nothing happens. A page on another site cannot send that
  header without the browser asking first, and the face API answers no such question. A GET never changes anything.
- **No guessing.** Ten failed logins (a wrong, used or expired code; a Mini App login that does not check out) in
  ten minutes close every login — yours too — for ten minutes (429). The count is the platform's, not per address:
  behind nginx every request comes from the same one.
- **The same decision as in Telegram.** A card decided in the app obeys exactly the rules of a Telegram press: only
  while the session still waits on that very call, bound to the digest of the call as you saw it, and of a press in
  Telegram and a press in the app exactly one decides — the other gets “The card has expired — no decision taken.” A
  decision in the app rewrites the Telegram card with its status and takes its buttons away; one in Telegram reaches
  the app at once. Pressed an hour later, after a restart, the call runs once.
- **The whole input.** The app shows a card's input whole, however long — a Telegram message stops at 4096.
- **A question** (the session's AskUserQuestion, [platform.md](platform.md#questions)) is a card of its own, in the
  transcript in place of the question, under “Waiting for you” and in the Mini App: each question with its options as
  full-width buttons, the description on a second line. A tap sends a single choice; a multi-select is ticked and
  sent by “Done”; “Own answer…” opens a field for your own words. The same rules as a Telegram press, per question:
  an answer in the app rewrites that question's Telegram message with the answer and takes its buttons away; one in
  Telegram shows in the app. The last answer resumes the session.
- **No answer kept.** Every answer is `Cache-Control: no-store`; the app's service worker caches only the app itself.

## How the app stays current

The app reads a **snapshot** (`GET /api/sessions`, `GET /api/sessions/{id}`) as of a number, the **cursor**, then
opens one stream (`GET /api/events`, Server-Sent Events) from that number. Every change the app would show — a
session, a transcript item, a card, a summary — is written to the platform's table `events` in the same transaction
as the change itself, each event the whole object, so the app converges whatever it missed in between. A dropped
stream resumes from its last event; one away longer than the 7 days events are kept gets 410 and reloads its snapshot.
One stream carries every session, so two panels side by side need one connection. A quiet stream gets `: ping` every
15 seconds.

While a turn runs, the text the model is writing comes on the same stream as it is written
(`--include-partial-messages`). It is never stored: the lane deletes those lines from its journal once the platform
has them, the platform passes them on and forgets them, and the message's own transcript item replaces them. If the
platform dies between taking such lines and confirming them, the lane gives them again; the platform drops every one
older than the last line it already stored, so a finished message never comes back as a draft. The pieces of a
message still being written when it died do come again — the app reloads its snapshot after a restart anyway.

Each event is `id: <number>`, `event: <kind>` and `data: <the object itself>` — the bare object, the same shape as
in the snapshot, never wrapped in an envelope. The kinds and their keys:

| `event` | key | `data` |
|---|---|---|
| `session` | the session's id | a session, as in `GET /api/sessions` (with `pinned` and `parent`) |
| `item` | `<session>/<turn>/<n>` | `{session, turn, n, kind, data}` |
| `items_reset` | `<session>/<turn>` | `{session, turn, count}`: that turn's items from `count` on are gone |
| `approval` | the approval's id | a card, as in the snapshot |
| `summary` | `<session>/<seq>` | `{session, seq, head, text, at, turn, kind, took_s, auto}`: every summary its own object; `turn` is the turn it ends; a ping's `kind` (`done` · `decision` — a card of the session waits on you — · `failed`) and `took_s` ([platform.md](platform.md#pings)); `auto`, the card of the platform's own commit after that turn |
| `input` | `<session>/<input_id>` | `{session, input_id, text, at, turn, n, attachments}`: your message; it goes before the item (`turn`, `n`) the session had not yet written (`null` on old ones: not shown); `attachments`: `[{kind: image\|pdf\|file, name, size, what, thumb}]` |
| `file` | the file's id | `{id, session, path, name, size, mime, at}`: a file the session handed over ([below](#a-sessions-files)) |
| `proposal` | the proposal's id | `{id, session, tool_use_id, title, brief, created, at, state, child, from_report}`: a sub-session the session proposed ([platform.md](platform.md#proposals)); `state` `open` · `started` (`child` the sub-session) · `declined` · `expired` (past its week: the platform's clock sends the event); `from_report`: proposed in a turn a report started, which its card says |
| `upload` | the file's SHA-256 | `{sha256, name, size, mime, created}`: a file uploaded through `/api/c/_files` (nothing in the app listens yet) |
| `collection` | the collection's name | `{name, space, rev}`: a collection changed — its records, form, views or rights; `space` its Space, `rev` its newest change ([collections.md](collections.md#views)) |
| `collection_proposal` | the proposal's id | a write to a collection a session proposed, as `GET /api/c/_proposals` lists it: made, applied, dismissed or expired ([collections.md](collections.md#proposals)) |
| `delta` | none, no `id` | `{session, text}`: text being written, never replayed |
| `chat` | the row's id | a row of «Talk to Retinue», as in `GET /api/chat`: written or rewritten; the app keeps the one with the higher `rev` |
| `chat-draft` | none, no `id` | `{text, at}`: what the assistant is writing in «Talk to Retinue», the whole text so far — only the newest goes out; never replayed, over after 30 s without a new one or when her row comes |

An approval is `{id, session, tool_use_id, kind, state, tool, input, cwd, digest, view, created, at}`; `kind` is
`approval`, `courier` or `question`, and a question's `view` is `{answers: {<n>: {value, other}}, picks, status}`
(its questions are in `input`). A card no one
was ever shown (a parallel call the CLI dropped before any card) is not the face's: no event, no snapshot entry.

A slow phone holds back nothing: each stream reads the events from SQLite at its own pace, the platform only wakes
it; a stream that takes nothing for 30 seconds is closed (the app reconnects). At most 16 are open: a seventeenth
closes the oldest, so connections of a phone that went away without closing them never lock you out. A stream ends
with its login — at once on logout, and at the next ping once its 30 days are over.

## API

| Call | Answer |
|---|---|
| `POST /api/auth/code {code}` · `POST /api/auth/telegram {init_data}` · `POST /api/auth/logout` | `{ok}` and the cookie |
| `GET /api/meta` | `{schema, owner_ok, tracks: [{name, lane, kind}], general, spheres: [{name, title, icon, track}], face_origin, speech, version, whats_new: [{version, date, notes}]}` — `general`: the track «＋» starts a session on (the root of the Base, below); `version`: the release running (`local` from a checkout); `whats_new`: CHANGELOG.md's releases the owner has not seen, newest first (retinue/changelog.py) |
| `POST /api/whats-new/seen` | `{version}` → `{ok}` — «Got it» under «What's new»: seen up to `version`; it only moves forward |
| `GET /api/sessions` | `{schema, cursor, sessions}` — every open session, those closed in the last 30 days and every sphere's main session, latest first; a session row carries `sphere`, `main`, `fork_of`, `unread`, `base` (its Base files not committed yet) `turn_started` (when its running turn began: idle, closed or new → active; a resume after a card keeps it), `side` (a side question: its `parent`), `sub_of` (a sub-session: the session that started it), `children` (its open sub-sessions), `queued` (waiting for room in the lane: the row and the session's header say “waiting for room on the lane”), `project` (the Base folder it runs in, on the Base's lane: the left panel's project) and `slot` (its place on [the map](#the-map) among its project's live sessions) — [platform.md](platform.md#sub-sessions); an input carries `by` (`parent`, `report`, `platform` — the platform's own word to the session, shown as a note “From the platform”, never his bubble) when the owner did not write it |
| `GET /api/sessions/{id}` | `{schema, cursor, session, items, approvals, summaries, inputs, files, base_cards, push_cards, proposals, about}` — `proposals`: every sub-session it proposed, oldest first, whatever became of it; `about`: `{cwd, context_tokens, model, cli_version, permission_mode, mcp: [{name, status}]}` for “Details” ([below](#details)), from the session's row and its newest top-level `init` line; null (or `[]`) where nothing said it |
| `GET /api/events?since=<cursor>` | the stream; `Last-Event-ID` wins over `since` |
| `POST /api/sessions {input_id, track, sphere?, text}` | `{session, say}`; `input_id` is `face:<uuid4>`, so a retried POST starts one session; with `sphere` and no `track`, the sphere's track |
| `POST /api/sessions {input_id, sphere, main: true, text}` | `{session, say}`: into the sphere's main session, as `/<sphere> text` ([platform.md](platform.md#spheres)) — a new one, made main, when the sphere has none; 400 with the reason when it did not go in (its lane retired, its files refused) |
| `POST /api/proposals/{id}/start` · `/dismiss` | `{proposal, say?}`: “Start” — the sub-session started with the proposal's brief, once however often pressed; 409 with the reason when it cannot start now (a limit, 10 open, its session archived, set aside, past its week) and the proposal stays as it was. “Not now” — set aside; the session is not told |
| `POST /api/sessions/{id}/input {input_id, text, attachments?}` | `{say}`; `attachments`: `[{name, media_type, data (base64), sha256}]`, at most 10 files and 30 MB, 20 MB a file — text, files or both ([platform.md](platform.md#what-a-session-gets-from-you)); `POST /api/sessions` takes them too |
| `GET /api/sessions/{id}/inputs/{input_id}/{n}` | a picture you sent, as the platform's 320 px JPEG of its own re-encoding (never your bytes); 404 for anything else |
| `POST /api/sessions/{id}/stop` | `{ok}`: the lane interrupts the running turn |
| `POST /api/sessions/{id}/pin {pinned}` | `{session}`: the pin is the session's row (`pinned`), so every device agrees; its `at` stays |
| `POST /api/sessions/{id}/archive` | `{session}`: “Archive” — the session, its sub-sessions and side questions end where they stand (queue dropped, run stopped, cards stale) and leave every list (`archived`); a turn of it beginning again, a message from you, brings it back ([platform.md](platform.md#archive)) |
| `GET /api/computers` | `{computers, off_s}` — the owner's computers' own sessions ([above](#from-the-computers)): per device `{device, at, off, sessions}`, each session `{id, title, folder, project, state, at, link?}` as its device last reported it (`state`: `working`, `waiting`, `done`; `at` in seconds); a revoked device is left out |
| `GET /api/archived` | `{schema, sessions}` — “Archived”: the archived sessions, the latest first, 200 at most, each a session row with `archived_at`; kin archived with its session is not listed apart |
| `POST /api/sessions/{id}/unarchive {main?}` | `{session}`: “Undo” in the toast “Archived.” — the session and the kin archived with it back in every list; their stopped turns stay stopped. `main: true`: it was its sphere's main session, and is again unless another one is by now |
| `POST /api/sessions/{id}/side {input_id, text}` | `{session, say}`: a side question — see below |
| `POST /api/sessions/{id}/sphere {sphere}` | `{session}`: a sphere's name, or `null` for none; the session is no sphere's main session after it |
| `POST /api/sessions/{id}/main {main}` | `{session}`: the sphere's main session — marking one unmarks the sphere's previous one; 400 without a sphere |
| `POST /api/sessions/{id}/seen` | `{session}`: you looked at it — nothing in it is unread |
| `POST /api/sessions/{id}/fork {input_id, text}` | `{session, say}`: “Fork” — see below; 400 with the reason when it cannot be forked now |
| `GET /api/approvals/{id}` | `{approval}` — one card, for the link “Open in the app” opens (`/a/<id>`); 404 for a card never shown |
| `POST /api/approvals/{id}/decide {action: approve\|reject, digest}` | `{toast, approval}` |
| `POST /api/approvals/{id}/answer {digest, question, labels \| text}` | `{toast, approval}`: one question of a question card answered — the options picked (exactly one for a single choice), or your own text |
| `GET /api/sessions/{id}/diff` | `{cwd, stat, diff, truncated, approximate_files}` — see below |
| `GET /api/files/{id}` | a handed-over file's bytes, as a download — see below |
| `GET /api/files/{id}/view` | a handed-over HTML file, for the app's sandboxed frame — see below; a PDF (one that starts as one), inline for the browser's own viewer, framed only by the app, as a turn ending's artifact opens ([platform.md](platform.md#turn-endings)) |
| `GET /api/files/{id}/meta` | `{file}` — one file's row, for the page `/f/<id>` that Telegram's “Open” opens |
| `GET /api/feed` | `{schema, count, groups: [{action, title, rows}], bot, assistant_at, deadlines, chips, closed_today, expired_week}` — “Waiting for you”, “Deadlines” and the status chips, [below](#the-feed) |
| `POST /api/waiting/press {t, action: done\|dismissed\|later\|open}` | `{ok}`: “Done”, “Not a task”, “Tomorrow” or “Undo” on a person's row, by its token; the router applies it ([platform.md](platform.md#the-assistants-digest)). “Plan” on a row is a plan press: `place` with its `t`, or a reminder's `ref` |
| `GET /api/plan` | `{schema, now, tz, zone, tasks: {rows, rev, space, goals, missing, blocks}, calendar: {events, at, failed, stale}, reminders, plan: {at, plans, recent, holds, said, window, activities, meals}}` — what [Tasks and Schedule](#tasks-and-schedule) show: the open tasks, the blocks planned or passed from yesterday to two weeks on (and the closed tasks they name), the busy window and the day plan's state the Router last pushed, the digest's reminders (the people's asks are the feed's rows, `lib/asks.ts`) |
| `POST /api/plan/press {action, day?, task?, plan?, block?, minutes?, opens?, closes?, zone?, activity?, activities?}` | `{ok}`: «Plan my day» (`plan`), «Plan» to a day (`place`), «Undo» (`undo`), «Now» (`now`: an activity by its name; `busy`, 30/60/120; `off`: not working today), its × (`free`), × on a block (`remove`), the plan's settings (`hours`: two `HH:MM` and a zone; `activities`: up to 12 of `{title, minutes, from?, to?}`) — a `plan-press` note for the Router ([collections.md](collections.md#blocks)) |
| `POST /api/waiting/detail {ref, t?}` | `{detail}`: [a row's pane](#her-rows-pane) of “From the assistant” — a person's row by its id and token, a draft by its id — asked of the router and relayed, never kept; 410 when the row is not in the digest any more or the router no longer knows it, 504 when the router said nothing within 12 s, 502 when what it said was off the contract, 429 while 8 already wait |
| `GET /api/chat` | `{schema, rows, draft}` — «Talk to Retinue» ([below](#talk-to-retinue)): the assistant's conversation, its newest 200 rows oldest first, each `{id, at, by: owner\|her\|notice, text, rev, via?, pending?, buttons?: [[label, id]], files?: [{id, name, size, mime, why?}], toast?}` (`id` the router's archive id; `rev` grows with each change); `draft`: what she is writing now, `{text, at}`, or null |
| `POST /api/chat {input_id, text}` | `{ok, row}`: your message, kept as your row (`pending` until the router has it) and journaled for the router; the same `input_id` (`face:<uuid4>`) again is the same message; 400 for files («send files to the bot in Telegram») or a text over 20,000 characters |
| `POST /api/chat/press {id, row}` | `{ok}`: a button of that row, for the router to decide; 410 when the row no longer carries it |
| `GET /api/chat/files/{id}` | one of her files, as a download under the sandbox policy (`/api/files/{id}`'s), never served as a page; 404 for anything else |
| `GET /api/stats` | `{schema, now, weeks: [tally…]}` — the platform's tally (`signals.tally`) of the last eight seven-day spans, the newest first, for “Numbers” ([below](#numbers)) |
| `GET /api/base/{id}` | `{card}` — the platform's own commit to the Base after a turn ([base.md](base.md#the-commit-after-a-turn)): its line (`said`), files, sha, whether “Undo” can be pressed |
| `GET /api/base/{id}/diff` | `{diff, truncated}`: what the commit changed, at most 1 MB |
| `POST /api/approvals/{id}/done` | `{toast, approval}`: “Done” on a courier card — `dropped → done`, out of “Waiting for you”; 400 for any other card |
| `POST /api/base/{id}/undo` | `{toast, card}`: “Undo” of that commit, by the rules of a Telegram press |
| `POST /api/sessions/{id}/push {repo?}` | `{toast, card}`: “Push” — a repository's push card opened, or its open one brought up to date ([code.md](code.md#the-button)); `card` null and the reason when there is nothing to push |
| `GET /api/push/{id}` · `/diff` | `{card}` for the page `/p/<id>`; `{diff, truncated}`: merge-base → the card's tip, at most 1 MB |
| `POST /api/push/{id}/target {target}` · `/title {title}` · `/send` · `/cancel` | `{toast, card}`: the remote branch, the PR's title, “Push”, “Cancel” — by the rules of a Telegram press |
| `GET /api/spaces` | `{head, spaces: [{name, title, questions, tasks, waiting, working}], projects: [{name, title, parent, questions, tasks}]}` — the Base's Spaces as its `main` commit has them; `waiting`/`working` count the sessions of the sphere of that name; `projects`: every project at any depth, the one it is in (`""` the root) — the left panel's ([base.md](base.md#reading-the-base-in-the-app)) |
| `GET /api/spaces/{space}` | `{head, space: {name, title, passport: {why, in_scope, not_in_scope, scope, files, rules, checks, imports, problems}, rules, marker, sections: [{heading, kind, items: [{text, list}]}], counts, records: {knowledge, profile, journal, artifacts}}}`; 404 for no such Space |
| `GET /api/spaces/{space}/r/{path}` | `{head, record: {space, path, nature, title, head, bad_head, body}}`; 404 for a path the rules refuse, 413 for a Record over 512 KB; 404 “The Base is not connected.” with no Base, 503 while it cannot be read |
| `GET /api/settings` | `{schema, ready, at, pending, restarting, restarted, lanes: [{name, engine, engine_label, model, effort, running: {model, effort}}], models: [{id, label}], efforts: [{id, label}], keys: [{name, group, title, what, where, hint, clear, on, set, changed}], git: [{key, repos, set, changed}], project_keys: [{name, project, set, changed}], done: {<id>: {ok, at, why?, services?}}}` — [Settings](#settings): never a value; `ready` false while the host's service is not installed; a lane's `model` is the file's, `running` what it runs with until the restart |
| `POST /api/settings/key {name, value \| clear}` · `/git {key, value}` · `/project_key {name, value}` · `/lane {lane, model?, effort?}` · `/restart {}` | 202 `{id}`: the request dropped for the host, its answer later in `done`; 400 with the words when it is off the lists, before anything is dropped; 503 while the host's service is not installed |
| `POST /api/speech?mode=dictate\|en` (body: the recording, `Content-Type` its type) | `{text}`; in `en` also `{message}`, the text as the session gets it, “(by voice, verbatim) …” — [Voice](#voice) |
| `GET /api/library` | `{schema, hub, at, pages: [{id, title, url, updated, section, section_title, section_icon, minutes, opened, done}]}` — “Pages”, [below](#pages) |
| `POST /api/library/{id}/open` · `/done {done}` | `{page}`: you opened the page; “Finished” (`done: true`) or taken back (`false`); 404 for a page not in the list |
| `POST /api/c/_files` · `GET /api/c/_files` · `GET /api/c/_files/{sha256}` | the store of a collection's attachments (the app has no screen for it: attaching to a row comes with the collections' files): a file in, any type up to 40 MB, `{file, new}`; the list `{schema, files, max}`; a file back as a download — [collections.md](collections.md#uploads) |
| `GET /api/c/_space?space=` · `GET /api/c/{collection}?view=` · `GET /api/c/{collection}/{id}` | a Space's collections and their views `{space, tz, today, at, collections}`; one view again `{name, rev, …, view}`; one record with its form's fields, its newest changes and the words of its refs `{name, rev, title, fields, record, changes, words}` — [collections.md](collections.md#views) |
| `GET /api/c/_proposals?space=` · `POST /api/c/_proposals/{id}/apply` · `/dismiss` | the writes the sessions proposed `{proposals}`, a Space's alone; the owner's «Apply» or «Dismiss» `{proposal, answer?}` (409 for one decided or a record that moved on) — [collections.md](collections.md#proposals) |
| `POST /api/c/{collection}` · `GET /api/c/{collection}/_form` · `GET /api/c/_changes?space=&before=` | the owner's write `{ops, idem}` — `apply`'s answer, or `{error, kind, record, change}` (409 when the record moved on since it was shown); what an add asks for `{name, rev, fields, options}`; a Space's newest changes `{space, changes, next, words}` — [collections.md](collections.md#from-the-screen) |

## Voice

Voice is **input only**: you speak, the answers stay text (reading is faster than listening). There is no read-aloud and no live conversation.

- **🎙** is in the [composer](#the-composer) — a session's and «＋»'s. Tap to record, tap again to stop; 10 minutes at most — then it stops
  by itself and says so. While the permission prompt is up the box says “Waiting for the microphone…”; while it records, the time,
  a level meter, “Cancel” and the stop button; then “Transcribing…” with “Cancel”.
- **A recording never outlives what shows it.** “Cancel”, leaving the screen («＋» included: another tab, back),
  another session drops it: the microphone is let go — also when it is granted only after the prompt
  was left — nothing is sent and nothing kept. A result that comes back after that is dropped too: it reaches only the
  screen and session that started it. “Cancel” while it is transcribed: if the recording is still being sent to
  ElevenLabs, the platform cancels the call — never sent, never paid; once ElevenLabs has it all, it is paid, and the
  platform lets the call end in the background only to delete the transcript there. The text never reaches the app,
  and your next recording does not wait for it.
- **Dictation** (the default): the text lands in the field at the cursor — what you typed stays around it — for you
  to edit and send yourself. Scribe drops fillers and false starts (`no_verbatim`), guesses the language (you mix
  Russian and English) and expects your terms (`speech.keyterms` in `platform.yaml`, [platform.md](platform.md#voice)).
- **«EN»** beside 🎙: English, word for word. Scribe is told the language and keeps every «uh» and slip, with no terms;
  the text is sent at once, unedited, as “(by voice, verbatim) <transcript>”, after what the box holds — its text and
  files — as one message: into the session, or from «＋» as a new session's first message (what you typed or attached
  is never dropped). When it cannot go — a message still being sent, a failed send — it lands in the field with the
  reason, for you to send; it is never lost. In a title,
  a commit subject or a pull request's title the mark is taken off, as «(голосом) » is (`retinue/spoken.py`). The session answers it with its corrections block, as your English-practice rule in
  the memory snapshot says; no prompt of the platform's changes for it.
- **The recording** is the browser's own: MediaRecorder at 64 kbit/s, WebM/Opus in Chrome and Firefox, MP4/AAC in
  Safari (the first type the browser takes from that list, feature-detected), sent as it is — no conversion in the
  app or on the server. ElevenLabs takes both (probed: WebM/Opus and MP4/AAC, 200 each).
- **On the platform** (`POST /api/speech`): behind the same login, header and origin as every action; one recording at
  a time (a second gets 429 “Already transcribing…”, unless the first was cancelled); 20 MB at most — 10 minutes even at 256 kbit/s, which no browser
  uses for a voice — read in pieces, so a longer body stops at 20 MB (413). The bytes stay in memory, never on disk;
  the transcript is deleted at ElevenLabs once read, and neither the audio nor the text is logged.
- **Errors** are one line: no key (“Voice is not set up…”, 503 — the app knows from `/api/meta` `speech: false` and
  says it before recording), no credits (“ElevenLabs is out of credits”), too long (413), speech not made out (422),
  ElevenLabs refusing the file or unreachable (502 with its reason).
- **Where there is no microphone** — no `getUserMedia` or MediaRecorder, or the permission refused, as may be the
  case in Telegram's web view on iOS — 🎙 says “The microphone is not available here — send the bot a voice message or open
  the app from the Home Screen” and records nothing. A voice note to the bot still goes into a session
  ([platform.md](platform.md#tracks-and-sessions)). The face's `Permissions-Policy` allows the microphone for the app
  itself and nothing else.

**Turning it on** (the platform side is in [platform.md](platform.md#voice)): `ELEVENLABS_API_KEY` in
`/srv/retinue/stack.env` (the router's, already there for voice notes), then `sudo bash deploy/setup.sh` again (it adds
`api.elevenlabs.io` to `/srv/retinue/egress/platform-hosts.txt`), then
`sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build face platform && sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml restart platform-egress`.

## The shell

On a phone the app is five tabs at the bottom — **Today · Operator · ＋ · Projects · Pages** — and on a wide
screen (≥ 1024 px) three columns, as in Claude's desktop app (`face/src/components/shell.tsx`). «Projects» is the tree
of projects with their sessions (`/sessions`); a Space opens from its row. On a wide screen the left column has
«Today», «Operator» (with the amber count of the requests that wait), «Pages», «Spaces» and «Numbers» on top.

The lines between the columns are dragged to resize them: the left column from 200 to 440 px (dragged past its
minimum it folds away, and dragged out it comes back), the right panel from 260 px to 60 % of the window, the middle
never under 360 px. Each column's width is remembered in the browser (`localStorage`, `retinue-columns`), the
“Details” and an open pane each their own.

| Route | What |
|---|---|
| `/` | goes to `/today`, its hash along (a Mini App's signed data rides in it) |
| `/today` | “Today”, the home screen ([below](#today)); an old `?mode=operator` or `?mode=map` goes to `/operator` |
| `/operator` | “Operator”, the sessions' requests as cards, or the map with `?view=map` ([below](#the-operator)) |
| `/new` | «＋»: a new task ([below](#a-new-task)); a tab on a phone, “New task” in the left column on a wide screen |
| `/sessions` | the projects and their sessions ([below](#in-the-app)); on a wide screen the left column has them, the middle says so |
| `/spaces`, `/spaces/<space>`, `/spaces/<space>/p/<path>`, `/spaces/<space>/r/<path inside the Space>` | the Spaces of the Base, a project's page ([below](#a-project)), a Record ([base.md](base.md)) |
| `/pages` | your HTML pages: to read, interactive ones, small sites ([pages.md](pages.md)); `/read`, its old name, leads here |
| `/week` | “Numbers”: how the work went ([below](#numbers)); in the left column on a wide screen, a link on “Today” on a phone |
| `/s/<id>`, `/a/<id>`, `/f/<id>`, `/p/<id>`, `/login` | as before: a session, a card, a file, a push card, the login |

- **The tab bar** stands under the tabs' own screens, not under a session (it has its own input box and “Back”, which
  goes back where you came from — the list when opened by a link) nor under a card opened from Telegram. It keeps
  above the home indicator (the safe area) and steps aside while the keyboard is up: on a touch screen, a text field
  has the focus and the visual viewport is more than 150 px shorter than the page was with no field focused (iOS and
  Telegram's web view shrink only the visual viewport, Android Chrome the layout too). A focus alone (the keyboard
  closed by the back gesture) or a pinch zoom (`scale` ≠ 1) does not hide it; it comes back on blur or when the
  viewport does. “Today” carries the count of what waits for you.
- **The page never scrolls; its parts do.** The shell is the window's height (`h-dvh`); a header, a nav, a tab bar
  and a composer stay where they are, and only the part with the content scrolls: the list of sessions, a page's
  middle, the right panel.
- **«＋»** is `/new`, a page in its own place, not a sheet over another ([below](#a-new-task)).
- **On a wide screen**, the left column (272 px by default): on top, fixed, the places — “New task” (`/new`), “Today”
  with the count, “Pages”, “Spaces”, “Numbers” — then “Projects”: the projects as places, the Base's tree, each with
  its sessions under it ([below](#projects-and-their-sessions)), the one part of it that scrolls. Its header, as the
  phone's, has the microphone and «Talk to Retinue» ([below](#talk-to-retinue)). Starting a session in a sphere is the composer's sphere picker, the Spaces are their page: the column holds
  only where you go and what runs (Apple's HIG: two levels at most; NN/g: the rest one step away). The middle: the page. The right is one panel with tabs: a
  session's [“Details”](#details) (one session in the middle; closed until the header's ⓘ opens it, and the device
  remembers) and, when one is open, a Record, a session's diff or a handed-over file — the URL's `pane`
  (`r:<space>/<path>`, `d:<session>`, `f:<file id>`), so a reload or a link keeps it and “Back” closes it; it stays
  while a session's tabs or panels change. The pane's tab is the active one; “Details” closes it. A code session's “Results” opens its diff
  there; a deadline of “Today” opens its Record there (on a phone, as its own page) — `RecordPage` gets `pane` and
  brings no header. A Record's path follows the platform's rule: one to three Latin parts inside the Space, no
  dot-led part, no space, `?`, `#` or `%`, a `.md` file.

### A new task

«＋» (`/new`): as a new chat in Claude's apps, the place where a session would be becomes an empty one (no sheet, no
“To”, no “Continue”, no “from the Base…”):

- **On a wide screen** the greeting, the status, the nearest deadline and the composer stand together in the middle
  of the page, as an empty chat does in Claude's apps; **on a phone** the composer stays at the bottom, under the thumb
  and above the keyboard.
- **The middle** is calm: a greeting for the time of day on the device's clock (“Good morning” from 5, “Good afternoon”
  from 12, “Good evening” from 18, “Good night” from 23), then how things stand — “N waiting · N working” (the
  «Today N», the running sessions; “Quiet: nothing waiting” when both are nought) — a tap opening “Today”, and the
  nearest deadline of the feed, when there is one.
- **The [composer](#the-composer)**: on a desktop it has the focus at once, on a phone it waits for a tap (the
  keyboard does not jump up). Under it, **the sphere picker**: “Auto” (the default) and one chip per sphere. With a
  sphere chosen, under the chips: **“Main session” | “New session”** and a line saying where it goes — “Into «<the
  main session's title>»”, “Starts the main session of ✈️ Travel” when the sphere has none, “A separate session in ✈️
  Travel”.
- **Sending** with a sphere chosen goes, as in Claude's desktop app, into **that sphere's main session** (`main:
  true`, the path of `/<sphere> text`; one is started and made main when the sphere has none) and opens it; with “New
  session” it starts a separate session on that sphere's track, in that sphere. A task begun elsewhere (“Ask the
  assistant”) opens with “New session” chosen: it always gets a session of its own. With “Auto” it starts a
  new **general** session: on `meta.general`, the track whose directory is the root of
  the Base on the Base's lane (`base`, `./base` and `base/` are one; with no Base, the first private track; never a
  repository's track — `retinue/face.py` `general_track`), with no sphere. With no such track `general` is null and
  the page says “There is nowhere to start a new session…” with the arrow off; before `/api/meta` has answered the arrow is off
  too, so nothing goes to no track. It reads `CLAUDE.md` → `AGENTS.md` there and finds its own context and sphere; the session opens at once
  (`/s/<id>`). A Record is not attached by hand any more: the session finds the Base's Records itself.
- What you typed and did not send waits for your next visit (this tab's `sessionStorage`), with the sphere and the
  choice; sent, it is gone. A message the main session did not take (its lane retired, its files refused) stays typed,
  with the reason.

### The composer

One box (`face/src/components/composer.tsx` `ComposerBox`) for a new task, a session's replies and the list's peek,
made after what the good chat apps share (studied from public sources):

- **A rounded box, text on top, controls under it.** Three lines tall at least on a phone (two in the peek) — twice
  the old one-line field — growing with the text to 40 % of the visible height (the keyboard taken off: the visual
  viewport), then scrolling inside. Measured by hand, since `field-sizing: content` is not in Safari. Vercel's
  open-source chatbot template grows its field the same way, `min-h-16`/`min-h-24` to `max-h-48`
  ([prompt-input.tsx](https://github.com/vercel/ai-chatbot/blob/c2f8235e1f3ea903ad8b7f61447c4f74164b5c58/components/ai-elements/prompt-input.tsx)).
  A tap anywhere on the box puts the cursor in the text.
- **«+» on the left, one menu**: “Photo” (the photo library), “Camera” (`capture`, a touch screen only), “File” — as
  Claude's «+» in the lower left of its box ([Claude help](https://support.claude.com/en/articles/8241126-uploading-files-to-claude))
  and Gemini's plus with Camera / Gallery / Files ([Samsung's guide](https://www.samsung.com/uk/support/mobile-devices/how-to-use-google-gemini-on-your-galaxy-phone/)).
  The menu takes the focus when it opens, ↑ ↓ Home End move in it, and Esc or a pick gives the focus back to «+».
  Attached items are chips above the text, each with its ×; pasting a file attaches it too. The platform's limits are
  checked before anything uploads (`lib/attach.ts`); a message may be files alone.
- **🎙 and «EN» on the right, then the send arrow**, as Claude's mobile apps put the microphone on the right of the
  field and send with the arrow ([Claude help](https://support.claude.com/en/articles/10065434-using-dictation-on-the-claude-mobile-apps)).
  The arrow is grey and disabled while there is nothing to send; “Stop” stands beside it while a turn runs. While
  recording, the controls give way to the [recording bar](#voice) — “Stop” stays — and the text stays in sight,
  read-only. Every control is a 44 px target whatever its visible size (the chips' × too).
- **One id per message**, kept only for a retry of the same message (the same text and files): a send that failed in
  the app but reached the platform answers the retry “already got it”, while anything different — an English message
  after it — goes under a new id and is never swallowed.
- **Enter** sends on a desktop, Shift+Enter is a new line, and a composing IME never sends (as ChatGPT and the Vercel
  template); on a touch screen Enter is a new line and only the arrow sends (the keyboard's key says “return”).
- **The keyboard and the safe area**: on a phone the tab bar steps aside for the keyboard ([above](#the-shell)); under
  a session the box keeps above the home indicator (`env(safe-area-inset-bottom)`) and drops that inset while the
  keyboard is up, since iOS keeps reporting it there. iOS Safari has no `interactive-widget`
  ([caniuse](https://caniuse.com/mdn-html_elements_meta_name_viewport_interactive-widget)) and shrinks only the visual
  viewport ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/VisualViewport)): the growth cap reads it. In
  Telegram's web view the app loads no Telegram script, so `--tg-safe-area-inset-*` is not read; the plain `env()`
  inset applies.

### Today

Three tabs under a header of one line, 48 px: the day, the tabs — «Today N» (what waits here: the feed's count less
the people's rows, which are Tasks'), «Tasks N» (the open ones and the people's asks no task names yet) and «Schedule»
(the week as an hourly grid, [below](#tasks-and-schedule)) — and the hub's status at the right: what is fine is one
green dot (its words in the tooltip), what is off is said («limit 89 %»). The address says the tab: `/today`,
`?tab=tasks`, `?tab=schedule`; an old link to the day, the week or the grid's first name (`?tab=day`, `?tab=week`,
`?tab=plan`) lands on «Schedule»; an old `?mode=operator` or `?mode=map` lands on «Operator», a place of its own, and
an old `?mode=project` on «Today». On a phone the title gives its room to the tabs, and the week's numbers are an icon.

«Today» is an inbox (`components/inbox.tsx`): the list at the left — 360 px, 400 from 1280 — and the row opened whole at the
right — side by side only where «Today» itself has 820 px (`useRoomFor`): beside the open chat it has less, and the
row opens over the list, «All» going back, as on a phone; nothing opens a dialog. A switch at the top of the list says how it is shown — **By action** (the default),
**Operator**, **Map** — and the pick is kept on the device (`localStorage`), so the next visit opens as it was left.
The tasks are never among what waits: they have their tab. «By project» was a fourth way: it repeated
the left panel's projects, so a device that kept it opens by action.

- **By action**: the feed's groups in order, as below; every row's second line starts with its project — «Work · …»,
  a sub-project «Work / Site · …» (the nearest project of its folder, else its sphere; nothing for one of neither).
- **Operator**: [the sessions' requests as cards](#the-operator), one column, no pane.
- **Map**: [every live session in its place](#the-map), by project.

**The list**, every row on one grid: a mark, what you do, who or what, how long — and a second line under the title
when there is one. The mark says whose move: a filled amber square — a session stands until you answer; an empty one —
a person or a thing waits; none — nothing waits on you. What you do is a word: Answer, Allow, Run, Push, Start, Apply,
Reply, Read for the sessions' rows; her rows by her ask — Pay, Do, Reply, Decide, Attend, Send for a draft — the prefix
taken off her line («pay: …»), and a due she gave in a column of its own (one the router masked, «‹number›», is left
out). Under what waits, each part only when it has something:

- **“Working”**: each session running a turn — «Runs», its title, how long it runs (since `turn_started`), its last
  line: the text being written, else the newest item that says something. Its transcript is read once (at most five),
  then the stream keeps it.
- **“Done since you looked”**: sessions that finished since you last had the app in front of you and that you have not
  read — when you last left is kept on the device (`localStorage`); after more than a minute away a new visit starts.
- **“Dates”**: the base's dates of the next seven days and the overdue, as the router pushed them — «today»,
  «tomorrow», else «4 Nov»; red when overdue.

Each thing once: a finished session under “Done” is not again among what waits, nor a base's date under “Dates”. At
the list's end: «Closed today M · ran out unmarked this week N», and when her list is older than two hours, so.

**The pane** shows the row picked — the first at the start; once a row is gone (answered, closed) the one now in its
place. Its top line says where it is from and how long it waits, with «Open the session ↗» when it has one:

- **a question**: the question large; the brief — in the question's own text after its first line, or in what the session wrote
  just before asking — when in the brief's
  shape (labelled lines «**Recommend:** …», `**Why:**`, `**Cost:**`, `**Risk:**`, `**Not checked:**`: the lanes'
  instructions ask for it, and a question without it is sent back to the session once) — as a table, its whole text under «The session's full reasoning»; a text not in that shape
  its first paragraph — where a session puts its answer — and the rest folded the same way. Then the options to pick (radio buttons; checkboxes for a multi-select), the
  recommended one picked at the start, «(Recommended)» shown as a mark; «Or answer in your own words»; one button —
  «Answer · <the pick>», «Send your answer» once words are written. 1–9 pick, Enter answers, ⌘Enter in the words;
- **an approval or a courier card, a proposed sub-session, a proposed write, a push**: the card itself, with its
  buttons;
- **a session whose turn ended with a word for you** (`ending`, [docs/platform.md](platform.md)): its card, the same as
  [the operator's](#the-operator);
- **a session waiting for your message, or a turn to read**: its last summary, and a box whose text goes into it as
  your next message;
- **her draft**: [what it answers and its text](#her-rows-pane), «Send in Telegram»; a person's row is not in this
  list — it is Tasks' «From people»;
- **“Working”**: «Now» — its last line — and the lines before it, «Open the session», «Stop»;
- **“Done since you looked”**: its last summary and a reply box;
- **a date**: its day, and the Record itself under it — the same view as in Spaces, with «Discuss».

J / K or the arrows go down and up the list, E marks her row done — never while you type. On a screen narrower than
1024 the list takes the screen, a row opens over it, and «All» comes back.

### The operator

«Operator», a place of its own (`/operator`, `pages/operator.tsx`, `components/operator.tsx`, `ending-card.tsx`,
`lib/operator.ts`), as cards or as [the map](#the-map) — «Cards · Map», the pick kept on the device, the same switch a
project's «Operator» tab has. «Today» is the inbox by action alone. Only the sessions' requests,
one card each in the feed's order — what waits first, a finished turn last — answered from the card without opening
the session. A card's top line: its number while it waits («#3»), the project, the session — its title opens the
session's own screen, the whole conversation —, what it wants, how long, «Open the session ↗». The map's card for a
session with no request has its title as that way in too. A session whose turn ended with a word for you (`ending` on its feed row — the shape of the
platform's `turn_ending`) is drawn by it, «You asked: …» — your request in your words — first:

- **ask**: what was found and what the advice rests on; the options as buttons, each with what it changes, the advice
  first («Recommended»); or quick answers; why it is yours to decide, and what happens with no answer;
- **approve**: what will be done and where, «Allow» · «Don't»;
- **look**: the thing opened in place — a picture as itself, a PDF or a page in its frame, by the URL the platform
  serves it at (`src`); a link to a page elsewhere; what to look at; «Looks right»;
- **step**: your own step — what, where, the command or text in a block with «Copy», what to send back; «Done»;
- **done**: what came out and what is next, and whose move.

Every answer — a button, a box's words — goes to the session as your next message, and the card says it in place
(«You: …»). A session of one of your computers ([from the computers](#from-the-computers)) is a card too, drawn by its
ending the same way, «on laptop» on its top line: it runs on that computer and is answered there — its options show
what each changes, without buttons, and «Answer on laptop ↗» opens the session there by its `claude://` link; one that
waits with no ending says so. With nothing to show, the page says what it looked at: no session waits, or how many
sessions your computer lists, or that it has not reported for a while. A row with a card of its own (a question, an approval, a proposal, a push) shows that card. A card waits
unless its turn is only to read — a finished one (`done`, or a turn to read with no word for you); a thing to look at
waits, though the feed lists it under “Read” — and that one rule (`lib/waiting.ts` `waits`) gives the numbers, the
amber mark on «Today», a tile's «waits for you» and every count of a project. A waiting card
keeps its number while it waits; a freed number is not given to another card for half an hour (kept on the device).
Nothing jumps while you read: a new card is counted at the top («New: N — show»), one answered or gone stays in its
place, dimmed, until you lay the list out again or come back after a minute away. No key answers a card here: one
press would reach them all. Every answer box has a microphone beside it: dictation into the box, to read and send.
The same cards, filtered to a project and its sub-projects and keeping «Today»'s numbers, are that project's
«Operator» tab ([below](#a-project)).

### The map

«Map» on «Operator», and «Cards · Map» on a project's «Operator» (`components/map.tsx`, `lib/map.ts`): every live session
as a tile in a place it keeps for its whole life, so the eye finds it where it was and its progress is easy to follow.
Flat on purpose: stable places are what spatial memory uses (Robertson et al., Data Mountain, 1998), a third
dimension does not help (Cockburn & McKenzie, 2002).

- **The areas** are the projects in the left panel's order — the spheres as the platform lists them, then the others
  A–Z, «Other» last —, a sub-project an area of its own right after its project («Travel / Lisbon in May»); they fill
  the width at 300 px and more each, two tiles to a row.
- **The places are the platform's** (a session's `slot`, `retinue/sessions.py` `take_slot`): the lowest place free in
  its project when it is made, kept while its state changes, freed when it is archived and taken again, the lowest
  free then, when it comes back («Undo», «Restore», or a turn of it beginning) — the phone and the laptop
  agree. One dashed place shows where the next one will appear. A session from before the slots comes after them.
- **A tile**: its operator number in mono while it waits («#3», the numbers of «Operator» and of the microphone), the
  relation's icon for a sub-session, fork or side question, «waits for you» · «working» · «done», the title, and one
  line — what it needs («Answer: which host», «Allow: publish the post», «Your step: …») or, working, its plan's step
  («3 of 5 · …») with a thin bar. An amber edge is your move, a blue one at work (its dot breathing), a done tile quiet.
- **A press** opens its card — the operator's own, with its answer box and microphone — in a pane beside the map, which
  is always there so the map never moves; on a phone, or beside the open chat when the map has under 720 px, the card is
  a sheet over it. The arrows go from tile to tile,
  Enter opens, Esc closes. «3 — book it» to the header's microphone answers #3 from here too.

### Tasks and Schedule

What to do, and when ([collections.md](collections.md#tasks)): one read for both tabs (`GET /api/plan`), again
once a minute and at once when `tasks` changes; every write is the owner's write of `tasks` through the views' editor —
its «Undo» in the toast, its question when the record moved on —, and a task's title opens its record in the drawer.
Days and times are the owner's zone's (the platform's, as the Router last told it).

- **Tasks**: «By when» or «By goal»; a box that adds a task by its title; **From people** — a person's ask the digest
  carries (the feed's row with its token, `lib/asks.ts`: a press takes it off at once, here and in «Tasks N»), or a
  reminder of the next seven days, that no open task names yet: the circle is «Done», «Plan» a day of
  the week (the Router makes its task and lays it, one press), «⋯» «Tomorrow» and «Not a task»; its title opens what it
  is about — her words, the sender, «Open in Gmail» — in a sheet; **To plan** —
  the open tasks with no block ahead, the due soonest first; **Planned** — those with one, the soonest first; **Done
  today**. A row: the circle that closes it (or opens it again), its title, its goal, how long, by when — its «no later than» with
  the time if it has one, red once passed —, «short 1 h» in red while a plan says that much does not fit before the
  deadline, where it came from, its next block, and «Plan» («Move» once planned).
- **«Plan»**: a day of the next seven — the Router puts the task at the first free time of that day's window — or an
  exact time, the owner's own block (`locked`); «Take off the day».
- **Schedule** is the week as an hourly grid — [FullCalendar](https://fullcalendar.io) 7.1.3 (MIT), its own chunk of
  ~85 kB gzipped fetched when the tab opens; one day on a screen under 768 px. Times are the owner's zone's; the plan's
  window ([collections.md](collections.md#blocks)) is the lit hours, the hours shown widened to any meeting outside it.
  Meetings are grey (a dashed edge for free time), whole-day events above the hours, holds shaded, the blocks in their
  goal's colour, a passed one faded. Over the grid: «Plan my day», «Plan my week» (seven windows from the day's, one
  plan, one «Undo»), «Undo plan» while the owner's plan is the newest, «Now» (his activities with their length, then Busy 30 min, 1 h, 2 h, then «Not working today»; «Lunch till 14:45 ×»
  while held), the places the meals keep hatched, the window's hours (a quiet button: the plan's settings — opens, closes, zone, and his activities — in a sheet), «Short before the deadline: …» when a plan from today on could not fit a task in time (it opens Tasks),
  what the last press came to, and how old the calendar is when it is old. A block dragged (15-minute
  steps) is the owner's, `locked`; its end pulled is its new length, `locked` too — the Router counts what he laid
  himself as the task's time ([collections.md](collections.md#blocks)); nothing is dropped into the past, and a block
  under way keeps its start but can be made longer. A press on a block opens its menu — the keyboard's and the touch
  screen's way: its length (30 min, 1, 1.5, 2 h) or «Take off the plan». Tasks get their time on «Tasks» («Plan») or
  from «Plan my day/week», not by dragging onto the grid: the bar's «Not planned N» counts the open tasks with nothing
  ahead and opens «Tasks». The plans of the last day come with who asked
  for each (`recent`): the assistant's of the last quarter-hour are lit with a dashed edge, and one made while the grid
  is open is said once with its «Undo». Every write's «Undo» is in its toast.

Without the collection the tabs say how it is set up.

### The look

Compact, calm and dense, after the Code tab of Claude's desktop app. What was taken from where — a ready, licensed package by default (the grid is FullCalendar), a pattern redrawn on
the face's own stack where none fits (native `<details>`, no Radix, no icon package):

- **Tool calls as one-line steps** — an icon, a verb, the target in monospace, a status only when it is an error or
  still running; a tap opens “Input” and “Result”/“Error”. The shape of [Vercel AI
  Elements](https://github.com/vercel/ai-elements)' `Tool` (Apache-2.0; its header, parameters and result blocks) and
  [prompt-kit](https://github.com/ibelick/prompt-kit)'s `Steps`/`Tool` (MIT). Both are built on Radix Collapsible and
  lucide-react, which the face does not carry, so they are redrawn, not copied.
- **Runs folded** — consecutive calls with nothing between them (no message, no card, no owner's message, no summary)
  are one line “8 steps · Bash 3 · read 3 · github · search_issues · 1 error”, as Claude's desktop app folds a
  turn's steps (AI Elements' `ChainOfThought`, [assistant-ui](https://github.com/assistant-ui/assistant-ui)'s tool
  groups, MIT; assistant-ui needs its own runtime, so only the pattern). The last run of a running turn stays open; once
  the turn moves on it folds. A call with a card is never folded: the card is the owner's. A subagent is one step,
  its own calls folded inside it the same way.
- **Thinking** is not in the platform's transcript (the projection keeps no thinking blocks, `retinue/transcript.py`),
  so there is nothing to fold; “Working…” pulses while a turn runs.
- **The reading column** is ~720 px; the owner's messages are quiet bubbles (`--bubble`), the model's plain markdown.
  The latest TodoWrite plan is pinned above the input box as one line “2 of 4 · Fixing …”, opening to the list (AI
  Elements' `Task`/`Plan`); it hides once every step is done.
- **Cards** (approval, question, a proposed sub-session) keep their full input and both buttons but sit in a denser card
  with a warm edge; the question's options are buttons with their description under the label. While one waits it is
  in “Waiting for you” above the input box ([below](#a-session)), and the conversation keeps one line where it came;
  decided, it is a one-line receipt. Every control is a 44 px target on a touch screen (`pointer-coarse:` sizes, or the
  `.hit` class that grows the box invisibly) while it looks small.
- Looked at and not taken: [CopilotKit](https://github.com/CopilotKit/CopilotKit) (a runtime and its own chat),
  [Lobe UI](https://github.com/lobehub/lobe-ui) (MIT, antd-based until v6 — not Tailwind), [Open
  WebUI](https://github.com/open-webui/open-webui) and [LibreChat](https://github.com/danny-avila/LibreChat) (their
  collapsed tool and status rows confirmed the pattern; whole apps, nothing to lift).

### How the screens are drawn

One header and one list on every tab, after the design guidance the change was checked against — Apple's HIG
(buttons), the Web Interface Guidelines, Anthropic's design critique, accessibility and UX-copy skills, and
`cuellarfr/design-skills` (interaction design, elevation):

- **The header** (`components/page-header.tsx`): a sticky band whose title stands on the content's left edge — its
  inner box has the page's width (`max-w-3xl`, Week's `max-w-4xl`, a Record's `68ch`) and padding (`PAGE_X`).
- **A list** is one surface with dividers (`LIST`), not a card per row; a row that is a link has `LIST_ROW`.
- **A row's actions**: at most one labelled button (the frequent action), the rest as icons with an `aria-label` and a
  tooltip; muted at rest, full contrast on the row's hover or focus — never hover-only — and a 44 px target on a touch
  screen (`.hit`). A page's “Finished” on “Pages” is a ring that fills with a check.

### Numbers

“Numbers” (`/week`, `pages/week.tsx`; it was “Week” until «Today» got a Week tab) is how the work went, from the platform's own tally
([platform.md](platform.md#how-the-work-went)): `GET /api/stats` answers its last eight seven-day spans, the newest
first, each ending where the next starts. Six tiles — sessions (with sub-sessions), runs, your messages, approval
cards, gate answers, tool errors — each with this week's number, the change from the week before and an eight-week
line whose points name their week on hover; a rise in tool errors is marked. Under them, one-hue bars: how the gate
answered (and its most frequent reasons), how the cards ended, tool errors by tool, compactions, failed turns and your
stops. The screen compares and draws; it never counts. Gate and error counts start with the platform's journal, so the
weeks before it read nought.

### A session

`/s/<id>` (`components/session-panel.tsx`), as the owner chose it on 2 November 2026 (variant B of the prototype:
“the session and its artifacts all mixed together… a bit too much of everything”, then “B is better”): the conversation and what the
session made are two tabs of the session, not one stream and not a section of the app.

- **The header** is one line: the state's dot, the title, under it the session it hangs under — its relation's icon
  and that session's title, a link (never an id, never “fork of”) — then the tabs **Chat · Results N**, the state's
  word at the right, the side question, the menu and **ⓘ “Details”**. No track, lane or id in it. On a phone the tabs
  are a second row under the title, each half the width.
- **One icon per relation, everywhere** (`components/relation.tsx`: the sidebar, the header, “Details”, the cards): a
  sub-session is a branch (`GitBranch`), a fork a turn (`CornerDownRight`), a side question a bubble with a question
  mark (`MessageQuestion`). The sidebar's fold is a chevron and the number under it; the main session's star is muted,
  not the link blue. The diff and “Push” have icons of their own (a file with ±, an up arrow).
- **The tab it opens on**: a finished session (idle or closed) with anything made opens on “Results”, one working or
  waiting on “Chat”; a link to a turn (`?turn=N`) opens on “Chat”. The choice is made once its snapshot is in and kept
  while you read; a tab pressed is the URL's `tab`. An old `?tab=files` lands on “Results”; `?tab=changes` is a code
  session's diff on a phone (opened from “Results”, with “‹ Results” above it).
- **Chat** is the conversation: your messages, the model's answers, the folded steps ([above](#the-look)), and one-line
  receipts where something was made or decided, each opening to the whole thing: “Wrote travel/journal/… and 1 more ›”
  (the turn's commit to the Base: its card with the files, the diff and “Undo”), “2 sub-sessions reported: … ›” (the
  reports under it, dashed, each sub-session a link), “You answered «Where to stay?» — Centre ›”, “Allowed `git tag …`
  ›”, “Denied …”, “Yours to run: …” (a courier card), “Started the sub-session «…» ›”, “Set aside …”. The raw line of a
  call a card stands for (`retinue · propose_subsession {…}`, the Bash call of an approval) is never shown by itself: it
  opens inside its receipt. A summary's head loses the platform's track and id: “Result · 15:31”. “Push · N” is no longer
  under the last summary — it is in “Results” and the menu.
- **Waiting for you · N** (`components/waiting-dock.tsx`) stands above the plan's line and the input box while anything
  in this session waits on you: a card to allow, a question, a sub-session it proposed, what its turn ending asks (an
  ask, an approval, your step — the feed's `ending:<session>` row). One row each, oldest first, with its own buttons
  when the row shows the whole of what it decides: a one-line command of up to 160 characters with nothing hidden in it
  and no other field (“Allow” · “Deny”), a short brief of your own turn (“Start” · “Not now”). Otherwise the row offers
  “Review”, “Show the brief”, “Answer” or “Open”, which opens the whole card under it first — nothing is allowed or
  started unseen. The conversation keeps one line where each came, “● Waiting for you below: …”, which opens its row.
- **Results N** (`components/results.tsx`, `lib/results.ts`) is one list (`LIST`), a row per thing, newest first, its
  type's icon first: what stands now on top — “N commits not pushed” with “Push · N” (or why it cannot), the diff
  “+27 −3 · Code changes · 3 files” (the right pane on a wide screen, `?tab=changes` on a phone) — then the files from
  `out/` (an HTML one is a page) opening in the right pane or `/f/<id>`, each commit to the Base with “Undo” on its row
  and its card on a tap, each sub-session's report (its first line; a tap opens it under the row with its sub-session a
  link), the thing its turn ending asked you to look at (opened over the page), a PR it opened (on its host). The tab
  shows once there is anything in it. The pages of “Pages” carry no session, so they are not here.
- **The plan** lives only in the pinned line above the input box; the old footer of “Summary” is gone.

### Details

Where a session runs and how (`components/session-summary.tsx`), for when you want to know: closed until the header's
ⓘ opens it — the right panel on a wide screen (the device remembers, `retinue-details-open`; the old “Summary” was open
by default and its key is not read), a sheet on a phone. Only what the platform knows; a line with nothing known is
left out:

- **Where it runs** — state (and “waiting for room on the lane”), folder (`about.cwd`), track and lane, model, the
  permission mode, the tokens the main conversation's last request carried (`context_tokens`; the model's window is not
  known to the platform, so there is no meter), Claude Code's version, the sphere (main or not), when it started, its id.
- **Related** — the session it hangs under and its sub-sessions, side questions and forks, each by its relation's icon,
  with their state, “queued” for a queued one.
- **Connectors** — the MCP servers of its newest run with their status.

`about` is read with the session's snapshot; it does not stream, so “Context” is as of opening the session. What the
session made is “Results”, its plan the line above the input box.

### A project

A project's page (`/spaces/<space>`, `/spaces/<space>/p/<path>`, `pages/spaces.tsx` `ProjectScreen`) opens on what it
is — its passport first; of three prototypes, «A · Passport, then the work» was taken. A header of one line: its mark, its name, «project · N sub-projects», and its tabs — **Overview**, the
views its collections name, **Records N**, **Files N** (each left out while empty) and **Operator N** (always: the way
into its requests; N what waits, amber).

- **Overview**: the **passport** card — why the project is, at 16 px; then a table of two columns: «In scope», «Not in
  scope» (where those things go instead) and «Rules», their first lines joined, «All rules» folding the rest; «Edit
  with a session» starts a session in the project's folder whose first message asks to edit the passport (its
  `AGENTS.md` above the marker, by its schema: [base.md](base.md#a-projects-passport)); without one the card says so and
  the button writes one. Then **Yours** — its Open questions, each with «Discuss», and its Tasks, each with «Plan» (a
  session to plan it), one list —; a Space's collections' first views (the work: strips, queues, the days ahead, the
  numbers) and its proposals; **Sessions** — its own sessions (a state dot: amber waiting, blue at work; the relation's
  icon; state · when) and its sub-projects as rows of their own («sub-project · N sessions · N questions», the amber
  count), «New»; the Showcase's other sections folded; a Space's changes.
- **Records**: every Record in one list — its title, and on the second line its nature and what its head says
  («Knowledge · verified 21 Sep · practice diverges», a warning amber) —, a search past six.
- **Files**: every file of its own, opened in place.
- **Operator**: «Cards · Map» — [the operator's cards](#the-operator) or [the map](#the-map) of this project and its
  sub-projects only, with «Today»'s numbers.

### Talk to Retinue

The app's header (the left column's top on a wide screen, the phone's on its tabs) has two more buttons
(`components/talk.tsx`, `lib/talk.ts`):

- **The microphone**: one press records, the next transcribes (the platform's speech, [above](#voice)). A phrase that
  starts with numbers — «1 — the recommended one, 2 — yes» — answers the operator's cards by their numbers: the app
  reads only the owner's own words for addresses, a number then a dash (an em or en dash, or a hyphen between spaces)
  at the start or after a comma, a semicolon, a full stop or a new line, never a model's text. It shows each card and
  the words it will get, and sends them on «Send» as each session's next message; a card of the platform's own (a
  question, an approval, a proposal, a push) is decided by its buttons and is only named. Any other phrase opens the
  chat with it in the box, to read and send.
- **«Talk to Retinue»**: a chat beside every screen — a column at the right on a wide screen (380 px, resized as the
  others), a sheet on a phone — with the assistant: one more channel of the router's, the same conversation and memory
  as the bot in Telegram ([platform.md](platform.md#the-routers-mirror), `retinue/chat.py`). It shows the
  conversation's newest 200 rows: your messages — written here, or in Telegram (marked «In Telegram» under them) —,
  her answers as Markdown with her files as downloads (up to 20 MB each; a bigger one is named with why), and the
  system's words — a reminder, a refusal, a card — quieter, in a dashed frame, their buttons under them. A press goes
  to the router, which decides as for a Telegram press; the card comes back rewritten (its choice under it, the
  buttons it keeps) with what the press said as a toast, and a press in Telegram rewrites it here too. What she is
  writing shows below as she writes it, the whole text so far, until her answer comes. Your message is your row at
  once, «Sending…» until the router has it; the same message sent twice (a retry) is one. The box takes text and
  dictation — files go to the bot for now — and `/new`, `/help` and the bot's other commands as in Telegram. Her
  answer to a message from here comes here (and in Telegram only when you also wrote there meanwhile); what she says
  unasked — reminders, the morning summary — comes in both. Open or closed as the device left it (`localStorage`,
  `retinue-chat-open`).

## In the app

- **Home: “Today”** ([above](#today)), with the feed “Waiting for you” ([below](#the-feed)). Its count, «Today N» — the
  feed's count less the people's asks (`lib/waiting.ts`; the cards the sessions wait on until the feed is in) —, is on
  the tab, the left column, the tab title, the app badge and on «New». The platform's `count` stays
  every row's: `/wait` in Telegram, which has no «Tasks», counts the asks too.
- <a id="projects-and-their-sessions"></a>**The list is the projects and their sessions** (of the prototypes,
  «4 · Projects with sessions»): each project is a place — its row, its mark and its name, opens its page
  ([below](#a-project)) —, the Base's tree as a file manager's: its Spaces on top, every project inside its project
  at any depth, sessions or not, the project's own sessions under its row, then its sub-projects. A Space opens by
  itself; a sub-project is folded until you open it. The column has no state filter and no numbers: what waits for you
  is the amber dot — on the session's row, and on a folded row it is inside —, and the count is «Operator»'s
  ([below](#the-operator)). A session belongs to the nearest project of the Base folder it runs in (its row's
  `project`), else to its sphere ([platform.md](platform.md#spheres)) when that sphere is a Base folder; one of neither
  is under «Other», last. A session that hangs under another stays under it, in that one's project. The Spaces come in
  the Spaces page's order (`platform.yaml`'s spheres, then the others A–Z), a project's sub-projects A–Z. The projects
  at any depth are `/api/spaces`' `projects`, read once a visit; with no Base the spheres stand for the Spaces. A
  project's chevron folds its sessions and sub-projects. In a project its main session comes first (a star), then the
  pinned, then the others, newest first; a fork hangs under
  the session it came from (“fork: …”, a fork of a fork one step further, three at most), a side question under the
  session it asks about (`parent`, a read-only run), a sub-session (`sub_of`) under the session that started it — so
  a sphere reads as in Claude Code's sidebar: sphere → its main session → its sub-sessions. Each session is one
  30 px line (44 px on a touch screen): a state dot (waiting, working), its title cut to the line, and on the right
  small marks — “waiting”, ↑ commits not pushed (a PR icon once a push opened one), the Base files not filed, a
  queued clock, the unread dot, each mark's number in its tooltip; pin, peek, menu and “Open beside” show on hover with a mouse and stay on a touch screen (the pin there only once
  pinned; “Pin” is in the menu; “Archive” at its foot takes the session out of every list at once, no question — the toast “Archived.” has “Undo”
  for ten seconds, which brings it back, and the page too if it was open). The list's last row, **“Archived”**, opens under
  it: each archived session with its sphere and when it was archived, its title
  opening it, and “Restore”, which puts it back in every list (the main mark is set again in its menu). A sphere's heading (small, muted) folds
  the sphere; «⌄» on a row with rows under it folds them (the tooltip says when sub-sessions are open, `children`); the
  device remembers both. A fold never hides what waits on you — a folded row and a folded project carry the amber dot
  — nor the session on screen (its project and parent stay open). A row is “side question: …”
  when the platform marks it `side`, or — a platform before `side` — whenever it has a `parent`. A waiting session
  shows its cards under its row on a phone. The «＋» of a sphere (on hover) starts a
  session in it: a box for what to do and nothing to choose — the sphere is the one pressed, the track is the sphere's
  own, and how to work is the session's to decide (⌘/Ctrl+Enter starts it); a sphere with no session yet is started from the composer's sphere picker. A **dot** marks a session that said
  something since you last looked — a summary or a new card; opening it on a visible screen, writing into it (here or
  in Telegram) or deciding its card clears it, on every device (`seen_at`, one per session). The pin on a row pins it
  on the platform. The arrow opens a **peek** under the row: the session's last words (the text being written, else
  its last message, else its last summary) and a reply box, without leaving the list.
- <a id="from-the-computers"></a>**From the computers.** The Claude Code sessions the owner runs on his own computers
  are in the same list, under the same projects, beside the room's ([collections.md](collections.md#its-sessions)):
  each computer with a device's token reports them, and the app reads `/api/computers` every 15 s while it is on
  screen (`lib/computers.ts`). Such a row is a session row's height: the state dot (waiting, working), its title, on
  the right “waiting” when it waits on you, a laptop mark and its last activity; its tooltip says the computer and
  the folder it runs in. It goes under the nearest project of the Base folder its computer gave, else under «Other» —
  never a project made up from its folder's top. It opens nothing here: on the computer itself, its link opens the
  session there (`claude://…`, the one link a device may give). It has no pin, peek or menu, no cards in «Today» and
  its question stays on the computer, which alone can answer it. Its dot is a room session's. A computer that has
  not reported for 2 minutes is off: its rows stay where they are, greyed, “off” on the right and «laptop off · seen
  14:05» in the tooltip, neither waiting nor working.
- **A session's menu** («⋯», on its row and in its header): its sphere, “Make main in the sphere” / “No longer
  main”, and **“Fork…”** with a box for the fork's first message. A fork is a new session that starts from a
  copy of this one's conversation and goes its own way, on the same track, lane and sphere and with the track's full
  rights — unlike a side question, which only reads. The session it came from is not touched. It cannot be made from a
  session that works or waits on a card, or from a side question ([platform.md](platform.md#spheres)).
- **A side question** (“Side question”, the round-bubble button of a session, /btw): a question about a session
  that does not go into it. The platform starts a session of its own on the same track and lane, under the same
  rules plus a read-only one — Edit, Write, MultiEdit, NotebookEdit and Bash denied: it answers, it changes nothing.
  Its first prompt is the question, then an excerpt of the asked session marked as data — its first message and its
  last 20 items that say something, each line built by code from the projection (who said what, which tool ran on
  what — never a model's summary), cut to 16 000 characters, the newest kept, every `<` and `>` written as `‹ ›` so
  nothing in it opens or closes a block — then the question again. Nothing is ever written into the asked session. A side session
  has `parent` and the asked session's sphere, and is listed under it (one level: a question from a side session
  hangs under the same main one). Its summaries read “Result · … · question about <id>”.
- **Your messages** are inline where they came, with what you attached: a picture as its thumbnail, a PDF or a file
  by its name and size. The composer's «+» attaches photos, PDFs and files (or paste one); the app checks the
  platform's limits before it uploads, and a message may be files alone.
- **Sub-sessions' reports** (`by: report`) are not your bubble: one line “N sub-sessions reported: …” in the session's
  flow, opening to a dashed block, each report under its branch icon and “Sub-session: <title>”, a link that opens it, its summary whole (“Its turn was cut short.” when it was);
  the platform's closing line for the model is left out. The head is cut only where the platform writes it —
  `Sub-session "<title>" (<8 hex>): ` at a line's start.
- **A proposed sub-session** ([platform.md](platform.md#proposals)) waits in “Waiting for you” ([above](#a-session)),
  a line where its call was: “Sub-session: <title>”, the brief as plain text (the model's words, never rendered), “Start”
  and “Not now” — a long brief, or one a report's turn proposed, opens by “Show the brief” before the buttons appear.
  The session goes on meanwhile. One a report's turn proposed says so in amber while it waits («…the report may have
  read someone else's text»). Decided, it is a receipt whose card says “started” with “Open the sub-session”, “set
  aside”, or “expired”. The same card is the row's action under “Decide”.
- **Summaries** are inline, each at the end of the turn it was said for. One that only repeats the turn's last message
  is a divider with its header; one that says more (an error, a restart, a limit) is shown whole. A ping's only
  buttons are the Base's line: a session that needs your choice asks with a question card. Under every summary whose
  turn wrote to the Base, one line — “Wrote <its newest file> and N more ›” once the app has the commit's card (until
  then the platform's “Written to the Base: <files> · <sha7>.”), or “Not written: <why>. The session was asked to fix
  it.” ([base.md](base.md#the-commit-after-a-turn)): a tap opens that commit's card
  — the line, the files, “Show diff” (the commit's own diff; its lines take comments, below), “Undo” while it can be
  undone, the badge “Written”, “Not written” or “Undone”. There is nothing to press to commit.
- **“Push”** ([code.md](code.md#the-button)): “Push · N” in “Results”, in the menu and in the feed opens a
  repository's push card (the event `push`): the branch it goes to and the PR's title in fields (saved first by
  “Push”), every commit that leaves with its whole message and its own files, what leaves in all (commits,
  bytes), the PR's files with +/−, what is not committed and the workflows the token cannot push; “Push · N
  commits”, “Show diff” (plain text, commit by commit; a file's lines take comments (below)), “Cancel”; once pushed, “Open PR” (a link only to github.com or
  gitlab.com). `/p/<id>` shows it alone.
- **Comments on a diff** (`components/remarks.tsx`, `lib/remarks.ts`): in every diff of the app — “Changes” of a code
  session, a Base card's diff (the card “In the Base” of a commit made by itself too), a push's diff — a tap on a line
  opens a box under it; “Add comment” leaves the comment under its line (a tap again edits it, “Remove” drops it) and in
  the session's tray: “N comments · Send”, sticky under the diff, the comments listed with their lines when opened.
  “Send” puts them all to the session as one message through its input — as a message typed there: queued behind a
  running turn, refused or taken by an archived session as the input does — under one key, so a retry after a lost
  answer is not sent twice:

  ```
  Comments on the diff (2):

  Comment on acme/gate.py:84: use the lane's path here
  >     path = tool_input.get("file_path")

  Comment on acme/gate.py:86: (the removed line) why is this gone?
  >     return digest
  ```

  Each line's number is the new side's, the old side's for a removed line; the quote is the line as the diff showed it,
  one line, cut at 300 characters — text in the message, never rendered or run. The comments wait per session in this
  browser's localStorage (`retinue.remarks.<session>`; without it, in memory for the load), so a closed diff or a reload
  keeps them; a send clears only those it sent.
- **“Spaces”** (`/spaces`, `pages/spaces.tsx`): the Base, read only ([base.md](base.md#reading-the-base-in-the-app)).
  The grid has a tile per Space — the sphere's mark and title when a sphere has its name, else the Showcase's title —
  with “N waiting”, “N working” (from the live sessions, as the list counts them) and “N questions” (its Open questions).
  A sphere's mark is the config's emoji, which Telegram shows as it is; the app draws it as a line icon of its own set
  (`SphereMark`, `components/icons.tsx`: 💼 a briefcase, ✈️ a plane, 📚 a book… — an emoji it has no icon for is a
  neutral one), so no screen shows emoji as icons. A Space (`/spaces/<space>`) is a screen of tabs under one line —
  “← [briefcase] Work  Now 5 · Numbers · Partners · Base 1”, with “1 waiting” at its right — the tab it was left on kept
  while the app is open; a tab whose views are all queues, empty now (drafts with none waiting), is left out until one
  has a row. **Now**, the first, holds its collections' views ([collections.md](collections.md#views),
  `components/views/`) that name no `tab`: first what waits for you, “Proposed 2”, the writes to its collections the
  sessions proposed, a row each (`components/collection-proposal-card.tsx`; a card in the feed) with what each
  operation would change (“Hooli · deleted”, “Initech · Stage: Qualified → Proposal”; five, then “Show all 7”), the
  session's words, “Yours to apply: …” when its rights left it to you, “Apply” and “Dismiss” — a record that moved on
  since says “Changed by the assistant 2 min ago: … Dismiss it, or ask the session again.” and leaves only “Dismiss”;
  then each `board` as a strip: a bar split by its columns, each part as wide as its count, and under it the columns
  in the enum's order, each “17 Evaluating”, the empty ones said once together (“Screening, Interviews, Offer 0”), the
  folded ones at the end — a column pressed lists its cards below it; then each `queue` (“Waiting on me 5”) as a board
  of lines, one per record, its cells in columns every line shares: a small square for the owner's move (an empty one
  for a `wait` queue), its days of silence when the queue has a `rot` (“15d”, red — amber for a wait — past the
  threshold, “overdue” said to a screen reader), which way its `latest` went and when (“↙ 31 Oct”), its head, the field
  beside it once the list is wide, the line under it, its enums in the form's words at the right; a phone shows two
  lines a record, three records and “Show all 5”. Beside them on a wide screen, in a column that stays in view (after
  them on a phone): “Next 7 days”, every `agenda` of the tab on a strip of the days, today shaded, a day with records
  marked, and under it those records, each its day and its words; and “This week” (the stats' window, “26 Oct – 1 Nov”
  at the right; “Numbers” when they count different ones), the `stat`s as figures two to a row, “0 / 7” under a goal.
  A tab a view names (**Numbers**, **Partners**) shows its `stat`s as tiles (“3 of 10” with a bar to the goal and
  “met” once it is, “this week, 26 Oct–1 Nov”, “last week: 7”; two to a row on a phone, four on a wide screen), its
  `chart`s — a column a week drawn at the chart's own width, stacked by the enum's values, the first in the accent and
  the rest in quiet steps, on a dashed grid, the total over the peak and over this week, each week's numbers in its
  tooltip, a legend with each series' sum, and every number in a table for a screen reader — and its boards whole:
  the columns side by side, narrowing so that five fit, with each card's days in it (“stuck · 9 days” past the
  threshold), the folded values below as closed lists; on a phone a switch of columns, opened at the first with a
  stuck card; fewer than three records are a list, each with its column's name. A day is “21 Sep” everywhere (“21 Sep
  2025” in another year). **Base**, the last, is the Showcase: the Open questions, each with “Discuss”; the Tasks; the
  Records by nature (Knowledge, Profile, Journal, Artifacts; a subfolder as a small label), each with the two or three
  words that matter for it (“verified 21 Sep · practice diverges”; a Profile's “until 1 Mar 2030”; “stale since” when
  it is), and a search over their titles; the Showcase's other sections and its rules, folded; the sphere's sessions
  (waiting first, then working, then the rest of the week) with “＋ New”; and, folded, “Changes”, the Space's newest
  changes with who, when and “Undo”. A view the platform could not make says why in its place (“Revenue: not
  counted”, the reason on hover); an empty one says since when its collection is quiet (“Nothing here — no change
  since 28 Oct.”, “No data yet: nothing was written here.”), never nothing. The views are read with the Showcase, again
  when the app comes back to the front and after a resync; the stream's `collection` event reads that collection's
  views again once its `rev` is past the one shown, and a collection of this Space the screen did not have (a define
  brought it in), or one that left it, reads the Space anew. A Space without views — or a platform without the route —
  opens on its Base alone.
  **The owner writes from the views** ([collections.md](collections.md#from-the-screen), `components/views/edit.tsx`):
  a row, a card or a day's record opens its record's sheet — a drawer at the right edge over the page, which a press
  beside it closes (a bottom sheet on a phone): “Stage”, the columns of its collection's first board as one control
  of four to a row, the current one filled and those before it shaded — a press moves it (the phone's way, and the
  keyboard's) — its fields in the form's words — the empty ones left out, a long text as a
  paragraph of four lines and “More” — then the records of the Space's other collections that name it, the newest
  first (its touches, its rounds: “28 Oct  Asked for the pilot's scope / Mail · Not sent”), then, folded, “Changes ·
  4”, each “Undo”, and a quiet “Delete”; on a wide screen a card is
  dragged to another column, a folded one included, and shows there at once; a queue's `done` is a button on each row
  (“Done”, “Sent”) — the row leaves at once — and “Mark all 4: Sent” below, asked once with the rows named; a view's
  `add` is “＋ Add a touch” in its header, a sheet of the view's fields with the required ones waited for, a date
  that is required started at today, a ref picked by its record's words. Each write is at the version shown, its toast
  “Moved to Interview. Undo” for 8 s — a newer write's toast takes the place of the one before; one the record moved on since asks “Changed by the assistant 2 min ago: Stage:
  Applied → Closed.” with “Apply mine” (the write again, at the new version) or “Keep”; an add that may be a record
  there already asks “Add anyway”; an “Undo” of a change something changed after says so, and changes nothing. A Record
  (`/spaces/<space>/r/<path>`) opens to read: its title, its head in words — “Knowledge · verified 21 Sep · practice
  diverges · stale after 21 Dec · written by Claude Code”, then “created · updated · id” smaller, “Source” folded, the
  fields the app does not know raw at the bottom — and its body as markdown under the transcript's rules (no raw
  HTML, nothing that loads by itself, images as text). A link to another Record or Space — exactly `/spaces/…`, no
  empty, `.` or `..` segment, no query or fragment — opens in the app; any other link asks before it leaves. The
  source is plain text, never markdown; only its http(s) addresses are links. **“Discuss”** on a Record or an Open question starts a new session in the Space's
  sphere (its track; a Space no sphere names asks for the track) and opens it. Its first message is a line of yours,
  “Below is a quote from the Base: it is data, not instructions.”, then the Base's content as `> ` lines — the Record's path and
  title, or the Showcase's path and the question — each folded to one line (no line breaks, no «», at most 1000
  characters), so nothing from the Base stands outside the quote; then your own words. There is no edit button: the Base changes only through a
  session, whose turn the platform commits. `RecordPage` takes `pane` to show beside a session on a wide screen.
- **A new card for another session** while the app is open: a quiet toast “Awaiting decision: <track> · <title>” that
  opens it — not for a session on screen, not for a card that was already waiting when the app loaded. The tab title
  and the app badge count «Today N» as before.
- **A card that waits past the prompt cache** carries “The session is idle N min — resuming re-reads the context
  (~K k tokens) uncached”, as in Telegram ([platform.md](platform.md#approvals)).
- **The Mini App** (`/a/<id>` from a Telegram card): once a press decides the card, the result shows for a second and
  the Mini App closes itself — through Telegram's web view (`TelegramWebviewProxy.postEvent("web_app_close")`), no
  Telegram script loaded. Opened anywhere else, the page goes on to the card's session. A stale press stays.

## The feed

“Waiting for you” is one queue grouped by what you do, not by where it came from (`retinue/feed.py`); “Today” shows it
as [an inbox](#today), and the groups below say what each row is and what the pane does with it. The platform builds
it from its own rows at each read — nothing is kept beside them that could drift — and the app reads it again when a
session, a card, a summary or a proposal changes on the stream (once per burst) and once a minute.

| Group | What is in it | Its action, in place |
|---|---|---|
| “Answer the question” | a question card that asks, with what its session wrote just before asking (`said`) | in the pane on “Today”: [the question](#today), its brief, the options, «Answer» |
| “Decide” | an approval card that waits; a courier card of the last 24 h whose session you have not written to since; a sub-session a session proposed, of the last 7 days (“Sub-session: <title>”, [platform.md](platform.md#proposals)); a write to a collection a session proposed, of the last 7 days (“deals: 2 changes proposed”, [collections.md](collections.md#proposals)) | “Allow” / “Deny”; a courier's “Copy command”; “Start” / “Not now” under the proposal's brief; “Apply” / “Dismiss” under what a proposed write would change. A call longer than 4 lines or 240 characters shows its one line and “Show and decide” first, a brief longer than 6 lines or 400 characters “Show the brief”: nothing is decided unseen |
| “Reply” | a session waiting for your message with no card shown | its last summary and a reply box |
| “Read” | a session with a dot — a summary or a card since you last looked (`unread`, [Spheres](platform.md#spheres)) — of the last 7 days | its last summary and a reply box |
| “From the assistant” | what waits in her world: the router's digest ([platform.md](platform.md#the-assistants-digest)) — a base's date from three days before it | a person's row or a draft: [its pane](#her-rows-pane) — «Done», the way to the thing, «Not a task», «Tomorrow»; a base's date: «Open the Record». Sending and deciding stay in Telegram, under her cards' buttons ([mail.md](mail.md#closing-what-waits)) |

Each row: what you do, the session's title (her rows: who), how long it has waited and its one line — the question,
the command, the summary's first line, her title. A card is the same object
as everywhere (`approval`), decided by the same rules; the app shows its newest copy, so a press in Telegram shows at
once. A row is `{key, action, source, session, track, sphere, sphere_title, sphere_icon, project, title, since, essence, ref}`
plus `approval` and `card` (a question's `<approval>/<n>`) on a card's row, `said` on a question's (Markdown, cut at
a line's end before 4000 characters),
`proposal` (the object of the `proposal` event) on a
proposal's row (`source: proposal`), `t` (the row's token) on a person's row of hers. `project` is a session's folder under the Base
(`work/acme`); `null` for hers, the Base's root or a folder outside it.
A pressed row leaves Tasks at once and stays hidden while the feed still has its `t`, ten minutes at most — a press
that did not go through brings it back; the toast “Done · Undo” stays 10 seconds, and “Undo” sends `open`
with the same `t`. `closed_today` and `expired_week` count what closed today and what ran out unmarked in the week.
`bot` is the bot's username as the router last pushed it; `assistant_at` when it last pushed — a list older than two
hours says so. `deadlines`: the digest's dates, `{id, when, title, sphere, sphere_title, sphere_icon, path}` by day —
the next week's and the overdue, also those days ahead that wait for no one yet (“Deadlines” on “Today”). `chips`: the
hub's status chips `{key, text, level}` as the router last pushed them (`[]` before). A sphere's title and icon come from `platform.yaml`'s spheres when they are there.


### Her row's pane

A person's row of hers or a draft opens in “Today”'s pane. What the digest already says shows at once: who, where it came from, the sphere, how long it waits, her
one line. The rest only the router has, so the pane asks for it (`POST /api/waiting/detail`) and fills in a moment
later, “Asking the assistant…” meanwhile:

- **what they ask** — each item still open under the row, newest first, at most 10 (“And N older — in Telegram.”):
  her ask (Reply, Decide, Pay, Attend, Do), her words about it, its deadline in her words, when it came — with every
  address, link, phone number and run of four digits masked as in the digest. Never the letter's text, its verbatim
  quote or the sender's address;
- **a draft's text** with its subject, and until when its «Send» waits (or that it goes by its link), and what it
  answers;
- the mailbox it came to (“to you@example.com”), and a line on what to do by her most pressing ask.

Under it, «Done» first, then the way to where the thing is — the router builds it from the ids it holds, never from her words, and
the platform and the app each draw only these shapes: **“Open in Gmail”**
(`https://mail.google.com/mail/?authuser=<mailbox>#all/<threadId>`: the thread in the mailbox it came to), **“Open
the chat”** (`https://t.me/<username>`; a person with no @username has no link that opens the chat — Telegram's
`tg://user?id=` is for bots and its apps ignore it — so the pane says so), **“Send in Telegram”** first on a draft,
whose card is there, and **“Ask the assistant”**: a session of its own about the row, not her Telegram chat — the
“New” opens with the row's context as the new task's text (where it came from and who, the list's
line, her words, the thread's link) and the cursor under it for your question, in the row's sphere when the platform
has one by that name, else “Auto” (`startNew`: this tab's sessionStorage, like any draft of “New”). Then “Not a task” and “Tomorrow”. Inside the Mini App a t.me link goes to Telegram itself (`web_app_open_tg_link`) and
Gmail to its browser (`web_app_open_link`), each from the tap itself.

When the router is silent the pane says “The assistant's side did not answer in time.” with “Try again”; when the row is out of date
it says so, drops the buttons and reads the feed again. Nothing of it is kept: not by the platform (it relays
the router's answer to the request that waits for it — [platform.md](platform.md#the-assistants-digest)), not by the
app once another row is opened.

## Pages

`/pages` (once “Read”, `/read`) lists your HTML pages as the pages service serves them ([pages.md](pages.md)) — by
section (the Space they live in, titled and iconed as its sphere when `platform.yaml` names one, “No section” last; in
each, the latest changed first), with a search over titles and sections. On top, **“Continue”**: the pages you opened
and have not marked “Finished”, the last opened first. A row says the reading time, the day the page changed, and when
you opened it — or “finished”, or “changed since you finished it” when it changed on a later day than you finished it.

**When it lists nothing, it says why** (`lib/library.ts` `libraryState`): the pages service is not set up (no
`pages_origin`), it has never sent its list, its list is late (none for two hours, twice its hourly push: it may be
down — the pages it last sent stay listed under that line), or it sent a list with no page. Each line names the next
step. The mock shows each: `MOCK_PAGES_OFF=1`, `MOCK_PAGES_DOWN=1`, `MOCK_PAGES_NONE=1`.

**Where the list comes from.** The router owns the pages and pushes the list to the platform (`POST /owner/library`,
the router's token, as the assistant's digest): `{pages: [{id, title, url, updated, section?, minutes?}]}`, replacing
the one there whole, looked at every 30 s, pushed when it changed and at least hourly (the mirror's `library`, [platform.md](platform.md#the-routers-mirror)). No
description, no body, no path in the base. The platform checks it again (`library.clean`). A push off its shape
(not a list of objects) or over a cap (200 pages, 512 KB) is refused whole (400) and the last list stays. A page off
the contract is dropped, named in the answer's `dropped: [{id, why}]` and in both logs, and the rest go up: an id
`a-z 0-9 -`, each once; a title of at most 200 characters, invisible characters dropped; a day `YYYY-MM-DD`; a
section named as a Space is (`a-z 0-9 _ . -`, from a letter or digit, at most 64); whole minutes; and `url` the
page's address on the pages service, `<pages_origin>/<id>` with no query or fragment, `pages_origin` being
`platform.yaml`'s, not the push's. Both origins are compared normalised — the host in lower case, `:443` dropped — and
the address is stored in that form. The platform never calls the router.

**How a page opens.** Never inside the app: no frame, no fetch. A row is a link to the pages service's own origin with
`target="_blank"` and `rel="noopener noreferrer"`; inside the Mini App the app hands it to Telegram instead
(`openLink`, as `web_app_open_link` through `TelegramWebviewProxy` — no Telegram script loaded). The page runs there,
in its shell's sandboxed frame, and never shares an origin, a cookie or a window with the app; its bar has “← Pages”
back here. The app draws a link only when the address is https, on that origin and a page's id as its whole path —
the platform's rule, checked a second time.

**What you read is the platform's row** (`library_reads`: the page's id, when you last opened it, when you said
“Finished”), so the phone and the desktop agree. What the page itself keeps — its ticks, its progress — is the pages
service's ([pages.md](pages.md#what-a-page-keeps)), not the app's. A page that leaves the list and comes back under the
same id keeps both. The app reads the list when the screen opens and again whenever it comes back to the front.

## Settings

`/settings`, the gear beside «Log out» in the header: what the stack reads and you set, in one place —
the lane's model and effort, your Claude subscription, ElevenLabs, the Telegram bot and Telegram Business, the
repositories' tokens and the projects' keys. Each row says what it is for, whether it is set («Set · 7 Nov», «Set»,
«Not set»; amber when Retinue needs it) and, opened, where to get it.

**A key is typed once.** «Set» or «Replace» opens a password field the browser neither fills nor keeps; «Save» sends
it and the field is emptied at once, whatever the answer. Nothing shows it again — not this screen, not the API, not a
log: the server reports whether a key is set and when Settings changed it, never the key, a part of it or its hash.
«Remove» is there only for an optional part's key (ElevenLabs, Telegram Business), after a confirm.

**Who writes it.** The platform cannot: the stack's environment, the git tokens and the projects' keys are root's
files, `platform.yaml` is mounted read-only, and it must never read a secret back. So each change is a request, one
JSON file of mode 600 the platform drops into `/srv/retinue/settings/inbox` (its own folder, mode 700) and root's
`retinue-settings.service` takes out at once — started by `retinue-settings.path` — reading it without following a
link, deleting it before it looks inside, and checking it against its own closed lists
(`deploy/settings/retinue-settings.py`):

- a key of the stack: its line in `stack.env`, replaced as setup.sh does. A new bot token also makes a new
  `RETINUE_WEBAPP_KEY` when the app is on, as setup.sh derives it;
- a repository's token: piped into `deploy/code/set-git-token.sh` itself, so Settings is that script's form and not a
  second store ([code.md](code.md#tokens));
- a project's key: `RETINUE_PROJECT_KEY_<NAME>` in `egress/project-keys.env`;
- a lane's `model` and `effort`: their lines in that lane's block of `platform.yaml`, from `cliengine.MODELS` and
  `EFFORTS` alone ([platform.md](platform.md#a-lanes-model)). A lane written on one line in the file is yours to edit
  by hand; Settings says so.

What it did comes back in `/srv/retinue/settings/status/status.json` (the platform's to read alone, mode 600, mounted
read-only): set or not, when
Settings changed it, the lanes' values, the services waiting for a restart, how each request ended. The service also
runs when you edit one of those files by hand, so the screen never says what the file no longer does.

**A restart is your press.** A saved change is written at once and used after its services restart: the bar on top
says «Restart to apply your changes» and which (Telegram, the assistant, the sessions and this app). «Restart»
recreates those services and no other (`docker compose up -d --no-deps --force-recreate`); a running turn stops and
goes on by itself, and the app reconnects. A failed restart says why, and the changes keep waiting. A lane's new model
applies from each session's next turn after it; a run that was going is held to the model it started on.

**Not installed yet.** A server set up before Settings has no service, and the screen says to run setup.sh once more;
until then nothing can be changed here. Settings writes the files `setup.sh` reads and the stack started with
`docker compose -p retinue --env-file /srv/retinue/stack.env`; on Dokploy, whose Environment is its own copy, keys are
still set there.

## A session's changes

`GET /api/sessions/{id}/diff` answers what the session's own edits changed, for any session, code or private:

```json
{"cwd": "/work/repo", "stat": " src/app.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
 "diff": "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -40,3 +40,3 @@\n ...",
 "truncated": false, "approximate_files": []}
```

- **Built from the transcript, never in the lane.** The platform reads the session's own lines: every Edit, Write,
  MultiEdit and NotebookEdit call whose result came and is not an error (a call refused, failed or still running is
  not a change). Nothing is run in the work tree — not `git diff` either: in a tree the session controls, git runs
  the repository's own configuration (`core.fsmonitor`, a filter's clean driver, a textconv), and it would run
  outside the sandbox with the lane host's rights. So a change made by Bash (`sed -i`, `git checkout`) is not here.
- **Line numbers are the CLI's.** The line carrying a call's result carries the CLI's `tool_use_result`; for an edit
  its `structuredPatch` gives each hunk with the file's real `oldStart/oldLines/newStart/newLines`. A Write of a new
  file (`type: create`) is its whole content added, under `new file mode` and `--- /dev/null`; a Write over a file
  with no patch is diffed against the `originalFile` the CLI gives.
- **Otherwise rebuilt, and said so.** Without a patch — an older CLI, a turn whose raw lines were pruned after 90
  days and only the transcript is left — the hunk is rebuilt with difflib from the call itself (`old_string` against
  `new_string`; a Write's whole content as added). Its numbers then count from the snippet, not the file, and the
  file is listed in `approximate_files`.
- **One section per file.** Several changes of a file make one `diff --git` section, their hunks in the order they
  ran, each numbered as the file was at that call. A path under `cwd` is shown relative to it, any other absolute
  (`a//etc/hosts`); a name with a control character, quote or backslash is C-quoted as git does.
- `cwd`: the directory the session ran in, as its CLI said at start; before it said anything, the track's `cwd` as
  configured. `stat`: like `git diff --stat`, over every change. `diff`: at most 1 MB — past it whole hunks are left
  out and `truncated` is `true`.

## A session's files

What a session saves under `out/` of its directory comes to you at the end of its turn — in Telegram under its
summary ([platform.md](platform.md#files-from-a-session)) and in the app, a row each in the session's “Results”
([above](#a-session)), opening in the right pane or as `/f/<id>` with its size, its time and “Download”.

- **A picture** shows as itself (`<img>`: an SVG there runs nothing). **Markdown** opens rendered by the transcript's
  own renderer — no raw HTML, every image written as its address in text, so nothing loads from the network.
  **Text, CSV, JSON** open as text, the first 200 000 characters. A PDF, a document or an archive is a download.
- **An HTML page** opens only in a frame, sandboxed twice: the frame's `sandbox` attribute allows nothing, and
  `GET /api/files/{id}/view` carries its own policy — `default-src 'none'; script-src 'none'; style-src
  'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; sandbox;
  frame-ancestors <the face's origin>`, the pages' rules for a page the assistant made ([pages.md](pages.md)). The page gets an
  opaque origin: no cookie, no API, no storage of the app's, no script; its own inline styles and `data:` images,
  nothing else. A page that needs a script or a font from the web shows without them.
- **A download** (`GET /api/files/{id}`) is served as the type its extension has on the platform's list, never what
  the file says, with `nosniff`, `Content-Disposition: attachment` and the policy `default-src 'none'; sandbox`: a
  file opened by itself — an SVG, an HTML page — is a document of no origin, never the app's.
- **`/f/<id>`**: one file on its own, its page or text open at once — what “Open” under a handed-over HTML page or
  SVG in Telegram opens as the Mini App (such a file never goes to Telegram as itself, [platform.md](platform.md#files-from-a-session)).
- The bytes are the platform's own copy, taken at the end of the turn: the session writing over the file later
  changes nothing here. Files are kept 30 days, as long as a closed session stays in the list.

## Serving

`deploy/face/nginx.conf`, in the image (`nginx-unprivileged`, the public pages' pin; read-only, no capabilities, its
pid and temporary files on a tmpfs):

- The app's routes (`/`, `/login`, `/today`, `/sessions`, `/read`, `/spaces`, `/spaces/<space>`,
  `/spaces/<space>/r/<path>`, `/s/<id>`, `/a/<id>`, `/f/<id>`, `/p/<id>`) answer `index.html` — the
  service worker keeps exactly these as the shell; any other path that is not a file is 404. `/assets/*` (hashed by Vite) are cached for a year; `index.html`, `sw.js` and the manifest are `no-cache`, so a
  new deploy is seen at the next load.
- `/api/*` goes to `172.16.96.10:9600` with the browser's `Cookie`, `Origin` and `X-Retinue`, never Traefik's key;
  bodies up to 1 MB — but a message into a session or a new session (`/api/sessions`, `/api/sessions/<id>/input`):
  up to 44 MB, streamed to the platform as it comes (`proxy_request_buffering off`), never spooled to the
  container's 16 MB `/tmp`, and answered within 5 minutes (the platform reads the pictures first). A recording
  (`/api/speech`): up to 20 MB, streamed the same way, answered within 5 minutes. An upload (`/api/c/_files`): up to 41 MB,
  streamed the same way; a file back (`/api/c/_files/<sha256>`) is streamed out unbuffered. `/api/events` is passed unbuffered, not compressed, for up to an hour of silence (the platform
  pings every 15 s).
- Headers: the policy of `face/README.md` word for word (`default-src 'self'`, `script-src 'self'`, `style-src 'self'
  'unsafe-inline'`, `frame-ancestors 'none'`, …), HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer`, `Permissions-Policy` with the microphone for the app itself (the [voice](#voice)) and everything else off,
  `X-Robots-Tag: noindex`. One exception: `/api/files/<id>/view` gets neither the app's policy nor
  `X-Frame-Options` (maps by `$uri`: an empty value adds no header), so the app can frame it; its own policy from the
  platform — the sandbox, `frame-ancestors` the face's origin — is all it carries.
- Without the key Traefik adds, every request is 404: a container on Traefik's network that is not Traefik reaches
  nothing.

**Why `frame-ancestors 'none'`.** Telegram on iPhone and Android, and the desktop apps, open a Mini App in a web view
of their own, where this page is the top document: the policy does not apply. Telegram Web (`web.telegram.org`)
frames it in an iframe of another site — and there the login cookie (`SameSite=Strict`) is neither set nor sent, and
Safari blocks it anyway, so allowing that frame would make the app load and then fail every call. The Mini App works
in the phone apps and the desktop apps; in Telegram Web, open the app by its link instead.

## In a browser

`pnpm e2e` opens the face in Chromium against the mock (`face/e2e/`, `face/playwright.config.ts`) at a phone's
(390 px, touch), a tablet's (768 px) and a desktop's (1280 px) width: the login code opens «Today», a session shows
its transcript and its summary, its changes their diff, the sessions list and the Spaces open, a task plans from its
menu and «Not working today» says what it came to — each with no sideways scroll and no error in the page. It starts its own mock and Vite (ports 9611 and 4173), so a `pnpm dev` already
running is never in the way. Once on a new machine: `pnpm exec playwright install chromium`. Run it before a deploy
that touches the face, beside `typecheck`, `lint` and `test`; it does not replace looking at a new screen yourself.

## Operator checks

1. `/app` in the chat: the link opens the app logged in; the same link again says the code did not fit. “Open”
   under it, and the menu button beside the chat's input, open “Today” in the Mini App without a code.
2. A card in Telegram, “Open in the app”: the Mini App shows the same card; “Allow” there — the Telegram card
   now reads “Allowed — running.” without buttons, and its own “Allow” says the card is stale.
3. A question in Telegram (ask a session to “ask me with options where to go”), “Open in the app”: answer
   it in the Mini App — the Telegram message now reads “Answer: …” without buttons, the Mini App closes, and the
   session goes on with the answer.
4. `curl -X POST https://<face host>/api/sessions` from a device on the tunnel: 403 (no header); with the header and
   no cookie: 401.
5. Only your devices: from a device not on the list, or with the VPN off,
   `curl -s -o /dev/null -w '%{http_code}\n' https://app.in.example.com/` → 404 (and `http://…` → 404, no redirect).
   From the phone over WireGuard: the app opens and `/app`'s code logs in.
6. No lane reaches the face API — not on its address, not on the platform's name:

   ```bash
   cd /opt/retinue
   for lane in lane-private; do
     sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec "$lane" python -c \
       "import socket
   for host in ('172.16.96.10', 'platform'):
       try: socket.create_connection((host, 9600), 3); print(host, 'REACHED')
       except OSError as e: print(host, 'no:', e)"
   done
   ```

   Every line says `no:` — `172.16.96.10` is not routable from a lane's network (unreachable or timed out), and on
   the lane's network the platform does not listen on 9600 (refused). `REACHED` anywhere: stop the face
   (`docker compose stop face platform`) and look at `RETINUE_FACE_HOST`.
7. `sudo docker compose … ps face` is `healthy`; `docker network inspect retinue_face-platform` lists two
   containers, the face and the platform.
7. A session that saved `out/page.html` with a `<script>`: in “Files” the page opens in its frame without the
   script's effect; `curl -sI` of `/api/files/<id>/view` from the phone's tunnel shows one `Content-Security-Policy`
   ending in `frame-ancestors https://<face host>` and no `X-Frame-Options`; of `/api/files/<id>`, the app's policy,
   `default-src 'none'; sandbox` and `Content-Disposition: attachment`.
9. “Waiting for you”: with a card waiting, the app's home lists it under “Decide” and «Today» in the left column shows the count; `/wait` in
   the chat names the same row, and a reply to that line goes into the session. Within a minute of the router's start
   the platform has her digest: a letter or a chat she judged to need you shows under “From the assistant”, and “Open
   in Telegram” opens the bot's chat; `docker compose logs router | grep "assistant's digest"` is empty (no refusal).
10. Voice: in the installed app (from the home screen), 🎙 in a session, say a sentence with «KV cache» in it, stop —
   the text is in the box, not sent. «EN», a sentence with an «uh» — it is sent at once as “(by voice, verbatim) …”
   with the «uh», and the session answers with corrections. `docker compose logs platform-egress | grep elevenlabs`
   shows `TCP_TUNNEL/200 … api.elevenlabs.io:443`; `docker compose logs platform | grep -i transcrib` shows a duration
   and a language, never the words. In the Mini App (Telegram on the phone): 🎙 either records the same way or says
   “The microphone is not available here…” — either is right; note which.
