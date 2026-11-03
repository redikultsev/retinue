# The plan in Google Calendar

The plan's blocks ([collections.md#blocks](collections.md#blocks)) go to a calendar of the owner's in Google,
«Retinue», and come back from it: a block laid, dragged on the grid, locked or taken off in the app changes its event
within a minute; an event he moves, resizes or deletes in Google changes its block the same way.

## How it is built

- **The calendar writer** (`retinue-calwrite`, `retinue/calwrite.py`) is code, no model, in a container of its own
  (uid 10023, `/srv/retinue/calwrite`), out through the mail proxy to Google's hosts only. Its token is
  `calendar.app.created` with `openid email`: it can make secondary calendars and touch the events of those alone — not
  his other calendars, not his mail. It makes «Retinue» at its first write and keeps the calendar's id.
- **An event is built from a closed set of fields**: the task's title, the block's start and end in the plan's zone,
  `transparency` (a locked block keeps the time busy, a plan's block does not), the calendar's default reminders, a
  line and a link to the app, and the block's id in `extendedProperties.private.retinue_block`. Its id is the block's id
  in hex after `rt` — that is how an event is known as a block's (Google may send a deleted one with its id alone; a
  copy he makes has another id and stays his), and a write that went twice is one event. Never guests or a conference; every write is
  `sendUpdates=none`. A change to an event keeps what he set on it in Google — a colour, his reminders.
- **The router decides** (`retinue/gcal.py`), every 60 s beside the digest's look, keeping in its kv what it last
  wrote of each block (`gcal.sync`):
  1. what changed in Google since its token: its own write coming back (the same etag) is nothing; a move or a resize
     is his — the block takes the event's time and is locked, as a drag on the grid would be; a deletion takes the
     block off the plan (`skipped`, «removed in Google»). Both are written by the rule `code:calsync` and count as a
     sign that he was here;
  2. the blocks begun in the last day or planned for the next 30, planned or passed, against what it wrote: an event
     made, changed or deleted to match. A change refused because he changed the event since (an etag that no longer
     matches) is his edit and goes the way of step 1 — his edit in Google is never written over.
  Each write is kept as it is made, so a look cut short never takes the router's own write for his. A write of the
  blocks that fails keeps nothing of the look: the next one reads the same changes again. Nothing missing from a read
  is taken for deleted: the block's next write asks, and the writer answers what is there. A block taken off and put
  back has its own event back. The sync runs in a task of its own: a slow Google holds neither the digest nor a press.
- **Another calendar** (the writer's state lost, the calendar made again): what the router knew of the old one is
  dropped and every block is written into the new one; nothing is taken off the plan.
- **The collector leaves these events out**: an event whose id is a block's and whose `retinue_block` agrees is not a
  meeting for the busy window, not
  news for the archive, not a line of the morning summary or the week's count.
- **He deleted the calendar**: the plan stops going to Google, a line says so once, and «Make it again» under it
  makes a new one with every block in it. **The key died** (`invalid_grant`): one line, and the plan waits for a new
  sign-in.

## Turning it on

Needs the collector ([mail.md](mail.md)): the writer goes out through the mail proxy.

```bash
sudo TELEGRAM_OWNER_ID=123456789 MAIL=1 CALWRITE=1 bash deploy/setup.sh
```

Then, on your computer, one consent (or `--kind all` for every kind at once):

```bash
uv run deploy/mail/login.py --account you@gmail.com --kind calwrite
```

The Calendar API is already on for the collector. If the consent screen refuses the new scope, add
`calendar.app.created` under the project's Data access in Google Cloud. With the app «In production» and unverified,
Google shows the usual warning, and the token does not expire after a week. The writer
takes the key at the next sync. The blocks show in the calendar apps of that account only.
