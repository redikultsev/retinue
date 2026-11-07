# What the assistant says unasked

Besides the reminders you set, the router writes to you on its own: the morning summary (09:00), the weekly review
(Sunday 18:00), the evening list of her commits and her changes to your collections (21:00), and what this page covers — the time zone that follows you,
deadlines from your knowledge base, the week's numbers of your collections, a computer that has not pushed, and the assistant
speaking first now and then. All of it lives in `retinue/proactive.py`; every state is in the router's SQLite.

## The voice

All of it reaches you in the router's channels: Telegram, and the app's «Talk to Retinue»
([face.md](face.md#talk-to-retinue)), which is one more channel of the same conversation — what she says unasked shows
in both. Who says it is a setting of the install. `voice:` in `router.yaml`:

```yaml
voice:
  engine: assistant    # the default: today's assistant over A2A, unchanged
  # engine: session    # the main session of the root project on the platform; channels: [telegram, app]
```

With `session`, the router's core composes every turn as for the assistant — your message, a reminder, the morning
summary, the weekly review — and writes it into the main session of the root project (`POST /owner/voice`,
`retinue/voice.py`): a session in the root of the Base, started when there is none, resumed when it closed. The call
is held until the turn is over (10 minutes at most); its words come back through the router to the channels you wrote
in and the app, once — not again as the session's ping. `channels` names what it hears: `telegram`, `app` or both; a
message from another channel is answered that it was not heard. A longer turn, or one waiting on a card, says so, and its answer comes as the session's
ping. A turn the schedule started is no one's word (`by: schedule`): it cannot set a follow-up, and a sub-session it
asks for is your card. Someone else's letter (the mail's triage) and an answer in a fixed form are not given to the
session: code takes them, as when the assistant fails. `!compact` is the CLI's own.

The platform's side, `voice:` in `platform.yaml`:

```yaml
spheres:
  - {name: life, title: Life, track: voice}       # the root project: its sessions run in the Base's root
voice:
  sphere: life
  tools: [Read, Grep, Glob, Edit, Write, Skill, TodoWrite, ToolSearch, mcp__retinue]   # optional
  engine: claude-code                             # the lanes' CLI, the only engine yet
```

Its character is a file at the root of the Base (`VOICE.md`) that the root's `AGENTS.md` includes (`@VOICE.md`), so
it is read with the root's rules for every run. Its rights are the install's, out of every session's reach: its
track's `allow`, `deny` and `card` rules, which the gate and the CLI enforce as for any session, and `tools`, the only
tools a session of its sphere may call — any other call is denied at the hook before the gate is asked. Another
engine that reads `AGENTS.md`, Agent Skills and MCP takes the CLI's place in the lanes (`cliengine`); the voice's own
code writes messages and reads summaries and assumes none.

## Your time zone follows you

`owner_tz` in `router.yaml` is only where you start. Tell the assistant «I'm in Ensk» or «switch my zone to
Zurbagan» (Russian works as well): she calls `set_timezone` (granted with `reminders`), and the router keeps the zone in its own state — it
survives restarts and wins over `router.yaml`. Only your own words move it: a run that read anything else — a letter,
a chat, a forward, a file, an archive find — is refused, so «I'm in Tokyo» in someone's mail changes nothing. Every
change is also told by code: «Zone: UTC → Asia/Tokyo.» A zone is Area/City (or UTC); legacy names
such as `Japan` or `PRC` are not taken.

| What | After the change |
|---|---|
| repeating reminders, the morning summary, the weekly review, the evening list | the same wall time in the new zone: «every day at 9» is 9 where you wake up |
| one-off reminders, retries after the limit, price drops | the same instant; their time is named in the new zone's clock |

A repeating reminder whose wall time has already passed today in the new zone waits for its next one. The summary
you have not had today comes at once.

The base can say it too. Put the zone into the header of `profile/location.md` (another Record: `memory.location`
in router.yaml):

```yaml
tz: Asia/Tokyo
```

When the hub has a value that differs from the zone in force, the router asks once, with two buttons. It never
reads the prose of that Record: «Lukomorye until 07.05» is not a zone, and a guess would move your reminders.

## Deadlines from the base

Every Record's header is read (`knowledge/`, `profile/`, `journal/`, `artifacts/`, one level of subfolders; Records
with `status: superseded|done|retired` are skipped):

- `until` — a document, a membership, a status runs out;
- `stale_after` — a fact to check again;
- `deadline` — anything with a closing date: a submission, a form, a payment. This is the one new field; e.g. on
  the Artifact of a grant application: `deadline: 2030-01-31`.

The morning summary gets one block «Deadlines, 14 days» — dates up to 14 days ahead and up to 30 days past, plus a count
of checks overdue for longer. A past `until` is how a profile Record is closed, so it is never listed as overdue — nor is one written on its own
day or after it (`updated` not before `until`): that is the Record closed today, not a document running out;
past `stale_after` and `deadline` are. A Record whose header does not read (a date like `2026-02-30`) is skipped and
logged once. An `until` of a profile Record and any `deadline` are also pushed once, 3 days before
(or as soon as the router sees one nearer).

## The week's numbers

The week is counted in your collections on the platform ([collections.md](collections.md#views)): the `stat` views
you name, each the same tile a Space's screen shows. The router asks the platform for them
(`POST /owner/collections/stat`) in its own zone, and tells the platform that zone at its start and with every change
(`POST /owner/zone`), so the screen's week and Telegram's are one week and the numbers one number. Name them, in
order, in `week` of the server's `owner.yaml` ([memory.md](memory.md#your-own-words-owneryaml)); each is
`<collection>/<view>`, at most 8; none — no numbers are said:

```yaml
week:
  - followups/done          # e.g. «Follow-ups done», the ones you sent yourself, with the form's goal
  - deals/moved             # and «Inbound moved», what moved on deals that came to you
```

Several numbers stand side by side, never summed: what you did yourself and what moved on its own are different
things.

- Monday's summary: «Last week: Follow-ups done — 4 (goal 10); Inbound moved — 2.» — each tile's week before.
- The weekly review: «This week: …» at the end; the assistant sees it among the week's facts.
- Thursday from 12:00: the numbers that have a goal, all still at zero this week (every weekly number, when none has
  one) — one nudge: «Thursday, and «Follow-ups done» (goal 10) is at zero for the week. One today?»

A tile without a window says «(now)», a month's «(this month)»; one the platform cannot count says why — «not
counted: field not found: 'stage'», «no such stat». A platform that does not answer within 10 s is said so —
«This week: not counted — the platform did not answer.» — and the Thursday nudge is asked again at the next beat.

**Upgrading an install.** The tracker in a Record of the base is gone, and with it `memory.tracker`: an `owner.yaml`
that still has `tracker:` is refused by `setup.sh`, and a `router.yaml` with it stops the router. Take the key out, add
`week:` with the views that count what it counted, run `setup.sh` again and restart the router.

## How the week went

The weekly review ends with a block of code's own, after her words — «How the week went»: the platform's tally of the
last seven days (`POST /owner/stats`, [platform.md](platform.md#how-the-work-went)) and the assistant's own runs
(the router's `runs`):

```
How the week went:
- Sessions started: 4 and sub-sessions 2; runs: 11; your messages in them: 30; «Stop»: 1.
- Permission cards: 6 — you allowed 4, denied 1, expired or stale 1; questions: 2, answered 2.
- Gate: 200 answers — by rules 150, by the classifier 40 (stopped 2), to you 6, denied by the gate 2. …
- Tool errors: 5 (Bash — 4, Edit — 1); context compactions: 2; turns ended with an error: 1.
- Turns sent on to run their check: 3; loops: warned 2, denied 1, cut 0; fetches that failed: http_403 — 4, timeout — 1.
- Turns that went badly: 3 (your «Stop» — 1, an error — 1, asked again — 1).
- The assistant: runs 23, failed 1, hit the limit 2.
```

She gets the same lines among the week's facts and may remark on one; she never counts. A platform that does not
answer within 10 s is said so («The platform did not answer — no session count.»); no platform — her runs only. The block
comes with a review that has no goals to read too.

## The base's health, on Mondays

Monday's summary also says what in the base wants tending, by the base's own rules, counted by code from the
files — one line each, the first three Records named and the rest counted, nothing when all is well:

- Records whose `stale_after` passed more than 30 days ago, the most overdue first and by how many days — the
  morning's «Deadlines» only counts them (closed ones left out);
- Records with `practice: unknown` — a rule whose practice nobody has checked (closed ones left out);
- open Tasks (`type: task`, `status: open`) whose `date` is more than 28 days ago;
- showcases (`<Space>/AGENTS.md`) longer than 200 lines — loaded whole into every talk about the Space;
- folders of Records holding more than 30;
- relative links to a `.md` that is not there (a link inside code is an example, not a link).

Once a week, not every morning: the same list each day would be read by nobody.

## «The computer has not pushed for N days»

The summary says so when the hub's last commit not made by the router (`memory.author`) is more than 3 days old.
Merges count: a pull-and-push from the owner's computer is a push. It is the commit's own time, so a commit made days before its
push reads as old.

## The heartbeat and the initiative knob

Every 15 minutes the router runs a checklist in code: a deadline 3 days off, a failed or missing backup, the week's
numbers still at zero by Thursday, a new zone in the base. It writes only when one of them needs you, in its own words, outside
your quiet hours (22:00–09:00 your time by default). Nothing due — nothing said, and no model run. Important mail
is not on the list: the mail room already tells it at once ([mail.md](mail.md)).

The one model run is the assistant's own thought: now and then, at random rather than by the clock, a one-off run
outside your conversation's session (like the morning summary) gets your goals, the base's dates, today's reminders
and the last day's talk, assembled by code. She writes one short message, or answers «—» and nothing is sent and
nothing stays in the conversation. A message that is sent is on record in the conversation, so her next turn sees
it. How often is yours to set:

| `/initiative …` | Her own thought | Thursday nudge |
|---|---|---|
| `less` | about every three days, at most once in 48 h | yes |
| `normal` (default) | about every day and a half, at most once a day | yes |
| `more` | up to twice a day, at least 5 h apart | yes |
| `off` | never | no |

The Russian words (`реже`, `обычно`, `чаще`, `выкл`, `тихо`) are taken too. `/initiative` alone shows the setting;
`/initiative quiet 23-8` moves the quiet hours. Brakes, all in code: a daily cap
of such runs (they are the `initiative` kind on the status page), not while you are talking to her (2 h after your
last message), not within 30 minutes after the morning summary, not while a run is going, not when the subscription limit is hit or the five-hour window is 80 % used.
Deadline pushes, the backup alert and the zone question come whatever the knob says — they are about something that
needs you.

The first heartbeat after the feature is deployed only takes stock: the deadline pushes already due then are not
sent (those dates are in the next summary's «Deadlines»), and the Thursday nudge of that week is skipped when the deploy
day is Thursday or later — no burst on the first day.
