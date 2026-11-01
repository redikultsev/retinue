# The face's look

How the app looks and why, for whoever changes a screen: read this first, and write here any decision a change makes.
The screens themselves are in [face.md](face.md); the hard rules below are held by `face/src/test/design-guard.test.ts`.

## What a good screen is here

- **You see where to look.** The work done most often comes first and largest; what is rare goes to a tab; what is
  empty is not on screen (a tab of empty queues is left out, empty board columns are one line).
- **Useful space over chrome.** A screen's header is one line — back, its mark and name, its tabs with their counts,
  its status at the right, 48 px. Lists run the full width (up to 90rem); reading columns stop at about 70 characters.
- **Details over the page, on demand.** A record opens as a drawer at the right edge (a bottom sheet on a phone), not
  as a column that is always there.
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
