# Mail and calendar

The assistant can read your Gmail and your Google Calendar — read-only, with your own Google Cloud project and
nobody else's app in between. A **collector** (code, no model) reads each mailbox and its calendars every ten
minutes and hands what it found to the router by number; the router keeps it, then confirms it. The assistant
reads each new letter in a run of its own with no tool at all and says what it is and whether to keep it; junk is
dropped and stored nowhere. What is kept goes to the archive verbatim, with its attachments; then she judges, a
few letters at a time, what you lose if you learn of each only at 09:00 — and code decides whether to write to you
now, in the morning summary, or not at all.

```
Google (3 hosts) ◀── mail-egress (Squid: oauth2, gmail, www .googleapis.com) ◀── collector (uid 10002, keys :ro)
                                                                                  ▲  look · keep · confirm upto
router (archive, Telegram) ────────────── mail-router (internal) ─────────────────┘
   └── assistant: triage (no tools, JSON schema) · judgement (her tools, JSON schema)
```

What it never does: send mail, answer an invitation, change or delete anything in your mailbox (the scopes are
read-only), read Spam or drafts, keep a dropped letter. Sending is another process with another token and your
button under every letter — [courier.md](courier.md). The assistant never gets the tokens, the collector never
gets a model, the archive file or the knowledge base.

## Turn it on

### 1. Google Cloud: a project of your own (once)

1. <https://console.cloud.google.com> → project picker → **New project**, any name.
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **Google Auth Platform → Branding** (the OAuth consent screen): app name, your address as support and contact
   e-mail. **Audience**: user type **External**; add your accounts as test users; then **Publish app** →
   *In production*. In *Testing* Google kills the tokens after seven days. The app stays unverified: Google shows
   a warning at consent, which you click through (*Advanced → Go to …*). That is fine for your own use (fewer than
   100 users, all known to you). It does not work with Advanced Protection on the account.
4. **Data access** (scopes): add `…/auth/gmail.readonly`, `…/auth/calendar.events.readonly`,
   `…/auth/calendar.calendarlist.readonly`.
5. **Clients → Create client**: type **Desktop app**. Download its JSON to `~/.config/retinue/google-client.json`
   on your computer (mode 600). This is the only client; every account uses it.

In Google Calendar (web) of each account: **Settings → Event settings → Add invitations to my calendar → Only if
the sender is known**. A stranger's invitation then never lands in the calendar by itself.

### 2. The server

```bash
sudo TELEGRAM_OWNER_ID=123456789 MAIL=1 bash deploy/setup.sh
```

This makes `/srv/retinue/mail/keys` and `/srv/retinue/mail/state` (mode 700, owned by uid 10002 — the collector's
own user), the collector's list of hosts `/srv/retinue/egress/mail-hosts.txt` (yours from then on), a token the
router signs its calls to the collector with, and `collector_url` in `router.yaml`. The tokens never go into the
nightly backup: after a restore you log in again.

### 3. Your computer: one consent per account and kind

```bash
export RETINUE_SSH_HOST=<your server's ssh host>
uv run deploy/mail/login.py --account you@gmail.com --kind gmail
uv run deploy/mail/login.py --account you@gmail.com --kind calendar
```

A browser opens on Google's consent screen; sign in to exactly that account (the script passes it as a hint and
then checks which account the token reads — another one is refused and nothing is written). Gmail and Calendar are
separate tokens on purpose: a password change kills every token with a Gmail scope, and the calendar outlives it.
Google keeps at most 100 tokens per account and client: do not run consents in a loop. The script puts the client
and the token on the server over ssh (`sudo`, mode 600, owned by uid 10002) and prints names, never values.

### 4. Start

```bash
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

Two more containers: `collector` and `mail-egress`. The first sync takes the last 30 days of each mailbox, in and
out, and calendar events from 30 days back on; the backfill is read slowly — live mail first, one model run at a
time, and none while the subscription's window is nearly spent — so it can take a day. You get notifications for
it as for live mail.

## Every day

- **A message now** — when she judged that something is asked of you and waiting costs much. About your private
  matter (kind `private`, below) it says only «Mail: 1 important»; the words come on «Show». Several urgent letters of one sender within an hour
  are one message.
- Under each: **«Don't notify about this»** — this sender's letters of this kind go to the summary from then on;
  **«Always drop …»** — the address and how many letters it sent in 30 days; its letters are dropped by code,
  unread. Only these buttons change the rules; the assistant has no tool for them. `!mail` lists the mailboxes
  and your rules, with a button to take each rule back.
- **The morning summary** names today's meetings (with what the base knows for the important ones) and the mail
  that could wait. Its health line says «mail N of N» with each mailbox's last sync, the calendar, what the day
  kept and dropped by kind, your blocks' counts, and what is still waiting.
- **A login Google refused** (`invalid_grant`, usually after a password change) is told at once, with the command
  to log in again.
- **The memory of an exchange.** Her judgement of a new message sees the exchange above it in a frame of code's
  words: the last 72 hours of the chat or the thread (at least 20 messages, at most 12,000 characters), an album
  as one line, her captions under the photos, a message written to her as one folded line. For a chat, code first
  looks up the new message's words in that chat alone, no model run. A chat is judged on its own: no other person's
  words in that run. She answers each photo of the other person with a caption (`media`); code keeps it as a record
  tied to the file, found by the search, never rewritten. A message written to her rather than to you
  (`to_assistant`) is counted in your morning summary: «Wrote to the assistant rather than to you, in a day: …».
- Dropped letters leave their sender and subject in a technical log for 30 days (the router's database, not the
  archive, not the base; the assistant never sees it), so a drop can be checked:

```bash
sudo docker exec "$(sudo docker ps -qf name=router)" python -c "import sqlite3; db = sqlite3.connect('file:/data/router.sqlite?mode=ro', uri=True); [print(r) for r in db.execute('select datetime(ts, \'unixepoch\'), sender, subject, kind, why from mail_dropped order by ts desc limit 30')]"
```

## Your private matter

Letters of kind `private` are about a matter you keep to yourself: a notification names nothing but a count, a
draft's card waits behind «Show», no draft carries it, and a run somebody else's message started never finds it
outside that exchange. Which matter that is, you say in your own words in the server's config — `memory.private`
in router.yaml — and code puts the line into her triage and judgement runs; without it she goes by the general rule
(a doctor, a lawyer, a private negotiation). `sphere` names the sphere its one row of «Waiting for you» belongs to:

```yaml
memory:
  private:
    about: letters with my lawyer about the lease    # what «private» means for you
    sphere: home                                  # optional
```

## What she may write

The judgement run has her tools: she may write a Record to the knowledge base from a letter (a meeting that was
set, a document's deadline) and move a row of your tracker (docs/proactive.md, «The tracker»). Its commit carries `Foreign-Input: mail` and shows in the evening list with
«Revert».
