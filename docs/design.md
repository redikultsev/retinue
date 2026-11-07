# The face's look

How the app looks and why, for whoever changes a screen: read this first, and write here any decision a change makes.
The screens themselves are in [face.md](face.md); the hard rules below are held by `face/src/test/design-guard.test.ts`.

## What a good screen is here

- **You see where to look.** The work done most often comes first and largest; what is rare goes to a tab; what is
  empty is not on screen (a tab of empty queues is left out, empty board columns are one line).
- **Useful space over chrome.** A screen's header is one line — back, its mark and name, its tabs with their counts,
  its status at the right, 48 px. Lists run the full width (up to 90rem); reading columns stop at about 70 characters.
- **Details over the page, on demand.** A record opens as a drawer at the right edge (a bottom sheet on a phone), not
  as a column that is always there. The exception is a screen whose work is going through a queue — «Today»: there
  the opened row *is* the work, so it stands in a pane beside the list (below).
- **Every colour means one thing** across the app (below). Data is brighter than the chrome around it.
- **A phone does another job**: lists of two lines a record, no board, targets of 44 px.

## Colour

The neutrals are warm greys (`face/src/index.css`, dark and light by the system). Three text roles: foreground,
`muted-foreground`, and `muted-foreground` at reduced opacity for the quietest meta. Borders are hairlines.

| Colour | Means | Never |
|---|---|---|
| `primary` (blue) | the primary button, a link, the focus ring | a status, a series that is not the first |
| `warning` (amber) | the owner's move: a queue's act mark, a busy day, a chart's leading series, what waits for him | decoration |
| `destructive` (red) | overdue past a threshold, an error, deleting | a count, a stage, «important» |
| `success` (green) | a goal met | anything else |

A new meaning takes a new colour or none — never a second meaning for one of these.

## Type

The system font for the interface, `ui-monospace` for figures that line up (days, dates in columns, counts, chart
labels). Sizes: 11 (chart labels), 12 (meta, counts), 13 (dense rows, section titles), 14 (body), 15 (screen and
sheet titles), larger only for a page's own headline. Weights 400 and 500; 600 rarely; never above. Sentence case
everywhere — no tracked capitals. Numbers `tabular-nums`. A day is «21 Sep» («21 Sep 2025» in another year).

## Shapes and spacing

A 4 px grid. Rows 40 px (44 on a touch screen), 8–12 inside, 20–28 between groups. Radii: 4–6 small controls and
tags, 8 rows and buttons, 12 cards and lists, the drawer square at its edge. Lists are one surface with dividers
(`LIST` in `lib/utils.ts`), not a card per row.

## Icons

Line icons after Lucide in `components/icons.tsx`, 16 px, one stroke, the colour of their text. No emoji anywhere on
screen: a sphere's emoji (which Telegram shows as it is) is drawn as a line icon by `SphereMark`; an emoji it has no
icon for gets a neutral one.

## A collection's views

- **A queue** is a board of one-line rows whose cells every row shares: a small square for whose move (filled amber
  for the owner's, empty for a wait), the days of silence when it has a threshold («15d», red past it), the last
  touch when it has `latest` («↙ 31 Oct», the arrow amber when it came to the owner), the head, the field beside it
  once the list is wide, the line under it, its enums at the right.
- **A board on the first tab** is a strip: a bar split by its columns, the counts under it; the whole board is on its
  own tab.
- **Days ahead**: a strip of the days, today shaded, busy days amber, their records under it.
- **Numbers beside the work** are a grid of figures; on their own tab, tiles with the goal and the period before.
- **A chart** is drawn at its own width (text never scales), one accent for the first series, a dashed grid, totals
  over the peak and this week only.

## An inbox

A queue the owner works through to its end is a list and a pane (`components/inbox.tsx`, «Today»):

- **The list**, 360 px (400 from 1280), one grid for every row — `8px` mark · `52px` what to do · the title · the time
  in mono, a second line under the title. The rows of every part line up on it, and so do the parts' titles. The row
  picked is a step of surface (`bg-accent`), never a coloured edge.
- **The mark** is the owner's move: amber filled — a session stands until he answers; amber empty — a person or a
  thing waits; nothing — nothing waits on him.
- **The pane**: a top line (where from · how long, «Open the session ↗» at its right), the thing's name at 20 px, its
  content, then its actions — one primary (blue), the rest outlined, a way elsewhere as a link with ↗.
- **A decision** is the brief as a two-column table (label · words; the first line in the foreground), the reasoning
  folded under it, the options as radio buttons with the recommended one picked, and one button that names what it
  sends. No line explaining the mechanics («Undo for 10 s») under it.
- What a row is about opens in the pane itself — a date's Record too — never in a second pane beside it.
- On a narrow screen the list is the screen and a row opens over it with «All» to come back.
- **By action · Operator · Map** is a segmented switch at the list's top, as Tasks' «By when · By goal». «By project»
  is gone: one grouping does not live in two places — the projects are the left panel's. Instead every row's
  second line starts with its project
  («Work · …», «Work / Site · …»). Tasks are not in it — they are their own tab. The operator's is one column of cards
  (720 px), no pane.
- **An operator's card**: the pane's top line over it, its number first in mono («#3»); inside, «You asked: …» in the
  quiet text, the main line at 15 px, then what it needs. An ask's options are full-width outlined rows — the label,
  what it changes under it — the advice first with a blue edge and «Recommended»; a step's command is a mono block with
  «Copy» in its bar; the thing to look at is the files' own row, open. A card answered or gone stays where it was at
  half opacity; nothing moves until «New: N — show».
- **Every answer box has a microphone** beside it (an outlined round 36 px button, 44 on a phone): dictation into the
  box, never a send by itself. While it records it is a small pill — a red dot that breathes (live), the time, stop,
  cancel.
- **Schedule** is the week's hourly grid, a day on a phone ([face.md](face.md#tasks-and-schedule)): no switch at its
  top — the grid's own bar.

## The map

The ask: a map for the operator, where every session stays where it was. What it decides:

- **Flat, and places never move.** Areas are the projects in the left panel's order, auto-filling at 300 px each; two
  tiles to a row; a session's place is the platform's `slot`, so a state change only recolours a tile. A free place is
  kept as blank space; only the next one is drawn, dashed. No 3D, no zoom, no reflow on open.
- **A tile** is 88 px tall at least, a 8 px radius, the list's surface: the top line 12 px (its number in mono, amber,
  while it waits; the relation's icon; the state word), the title 13 px on two lines at most, one line 12 px at the
  foot, a 4 px bar for a plan. The edge says whose move — amber his, blue at work (a 6 px dot that breathes), a done
  tile at 60 % opacity —; the fill stays the surface: one accent per meaning, no coloured fills.
- **The card opens beside, never over**: on a wide screen a pane of 20–24rem is always there, empty with a dashed
  hint until a tile is pressed, so the map does not move; on a phone a sheet.

## A project

The ask: a project should show its passport when it opens, and its screen read as one place rather than a list of
parts; of three prototypes, «A · Passport, then the work» was taken, with the operator mode inside a project too.
What it decides ([face.md](face.md#a-project)):

- **The header is one line**, as every screen's: its mark, its name at 15 px, «project · N sub-projects» quiet, the
  tabs with their counts — the Operator's count amber, his move.
- **It opens on its passport**: one card, the why at 16 px on 24 px lines (the page's only text above 15, its
  headline), at most 72 characters a line; under it a table of two columns, 13 px — the label column quiet and
  104 px, the words in the foreground —: In scope, Not in scope, Rules (their first lines, the rest folded). One
  outlined button, «Edit with a session». Then the owner's things and the sessions as `LIST`s, the sub-projects rows
  of the same list.
- **A project's operator is its tab**, «Cards · Map» the same switch, the same cards and numbers as «Today».
- **Records are one list**: the nature on the second line with the head's words, no folder lines.

## The left panel

Of the prototypes, «4 · Projects with sessions» was taken: a project is a place.

- **A project's row** is a session row's height (30 px, 44 on touch): its mark, its name at 13 px medium, the amber
  count of what waits in it in mono at its right — its «Operator» count, the same number on its page and its parent's —, the fold chevron after it; the row opens the project. A sub-project
  is indented 14 px a level, its sessions with it. «Other» is a quiet 12 px heading, not a place.
- **A computer's session** is a session row with nothing to press but its link: the same dot and title, then on the
  right “waiting” (amber, when it waits on him), a 12 px laptop mark and its time in 11 px muted mono — the mark says
  where it runs without a word per row. Off, its title goes muted and the time says “off”: grey is “not now”, never
  an error.
- **Talk to Retinue** is a column at the right (380 px) on a wide screen, a sheet on a phone: the sidebar's surface,
  a one-line header (mark, «Retinue», «your assistant · also in Telegram» quiet, ×), the conversation, the composer
  (no «＋»: it takes text and dictation). The conversation is the session's look: his words in the bubble (where
  they came from, «In Telegram», quiet under it), hers as Markdown at 15/14 px, her files as the files' own rows
  (mono name, size at the right); the system's words quieter than hers — muted 13 px in a dashed frame, as a
  platform note in a session —, their buttons outlined under them (44 px on a phone); what she is writing below,
  streamed, or «Writing…» with a 6 px blue dot that breathes while she has no words yet.
  The header's microphone and chat icon are ghost icon buttons beside «Log out».

## A plan: tasks, a day, a week

- **A row of a task** is the queue's row: the circle that closes it first, the title and its meta line (goal swatch ·
  how long · by when · from where), its next block in mono at the right, one outlined button («Plan», «Move»).
- **A goal's swatch** is a series colour by the goal's place in the form (`bg-chart-N`), a 2 px-rounded square — never
  a meaning of its own; amber stays the owner's move (planned time in the strip and the week's load bars).
- **A menu over a list** is drawn into the page's top layer at its button (below it, or above when the screen has no
  room) and follows it when the page scrolls: a list's rounded, clipped frame never cuts it.
- **The day** is a list of time rows (the time in a 48 px mono column, start over end), not a grid of hours: a grid is
  unreadable on a laptop beside the sidebar. Free stretches are quiet rows of their own, where a dragged task lands.
- **The week** keeps each day at least 10rem and scrolls sideways rather than squeeze seven columns until words break.

## A session

The ask: a session's conversation and what it produced were mixed in one column, with too much at once; different
things belong in different views. Of three prototypes, B was taken. What it decides ([face.md](face.md#a-session)):

- **Different things, different tabs of their own thing.** A session is «Chat» (the conversation) and «Results N»
  (everything it made: files, pages, commits to the Base, reports, the thing to look at, the diff, the push) — tabs in
  the session's own header, not a section of the app, so a result stays beside its context. A finished session opens on
  «Results», one working or waiting on «Chat».
- **What waits on him is not history.** «Waiting for you · N» stands above the input box with each thing's buttons; the
  conversation keeps a one-line anchor where it came (the amber dot: his move). Buttons sit on the row only when the
  row shows the whole of what they decide; otherwise the row opens the card first.
- **Decided is one line.** A card answered, allowed or set aside is a receipt — an icon, what happened, a chevron —
  that opens to the card. A call a card stands for never shows its raw line by itself.
- **The engineer's view is on demand.** «Details» (folder, model, mode, context, connectors, id) is behind ⓘ, closed by
  default; ids, track and lane are not in the stream or a card's head.
- **One icon per relation** across the app (`components/relation.tsx`): sub-session a branch, fork a turn, side
  question a bubble with «?». A relation is an icon and the other session's title, never a word or an id. A count is a
  number with a chevron, never a relation's icon. The main session's star is muted: blue stays the button and the
  link.
- **A row of «Results»** is the `LIST` row: a 16 px type icon, the name (a path in mono) over what it is (12 px), its
  own button at the right («Undo», «Push · N»), its time in mono, a chevron when it opens.

## Settings

The ask: keys, tokens and the lane's model were set in files and terminal commands; one place for them, each saying
what it is for and whether it is set, a key typed once and never shown again. From GitHub's and Vercel's secrets
([face.md](face.md#settings)):

- **A secret is write-only.** Its row is its name, what it does and «Set · <day>»; «Set» or «Replace» opens a password
  field under it, emptied the moment it is sent. No masked value, no «show», no last four characters.
- **Nothing on a row moves.** The state sits on the title's line; the button keeps its place and width as «Set»,
  «Replace» or «Cancel»; a choice in a list stays what he picked while it saves.
- **A change waits in words.** «After the restart» on the field, one bar on top with what restarts and the one
  «Restart»; a refusal in the host's words, on the row that sent it.
- **Only what can be changed.** A lane's engine is shown, not offered; Remove only where the part is optional, after a
  confirm. The system's own secrets (the bus secret, the lanes' tokens) are setup.sh's, never on this screen.

## Copy

What the owner reads is in his words: a form's `labels` for values, no field names or zones on screen, no trailing
periods on labels, no exclamation marks, a number with what it counts («0 / 7», «This week»).

## Motion

Colour, opacity and transform only, named (`transition-colors`), 120–200 ms; an entrance only where something opens.
A looping animation only on what is live right now (a session working).

## Checking a change

1. `pnpm -C face test` — the design guard among the tests; `pnpm -C face lint`, `typecheck`.
2. The mock (`MOCK_PORT=9611 node mock/server.ts`, then the dev server with `RETINUE_API` pointed at it): the screen
   at 1440 × 900, 768 × 1024 and 375 × 812, short and long content, no horizontal scroll.
3. After deploying, the live app — said in the report as seen, or as not checked.
