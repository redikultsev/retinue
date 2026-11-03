# Events and reactions

A project says what its collections are, what happens in them, and what it does when something happens in another
project — in one file beside its `AGENTS.md`, `project.yaml` (`retinue/project.py`). The platform knows no leads,
stages or links: an event is a project's name for a change of its own records, and what it carries are plain fields.

## The description

```yaml
collections:                    # each defined in the project's Space, as `collections define` does
  invoices:
    form: {fields: {company: {type: text}, amount: {type: money, currency: EUR}, state: {type: enum, values: [open, paid]},
                    date: {type: date}, scan: {type: file}}}
    views: [{name: spent, kind: months, date: date, sum: amount}, {name: year, kind: year, date: date, sum: amount}]
events:
  invoice_paid: {collection: invoices, when: {field: state, to: [paid]}}   # or when: {added: true}; none: by a call
reactions:
  - {on: crm/deal_won, do: add, collection: invoices, fields: {company: {from: company}, state: open}}
  - {on: invoice_paid, do: task, fields: {title: Send the receipt}}
  - {on: invoice_paid, do: send, text: Thank the customer}             # outside the install: only asks the owner
views:
  - {name: burn, kind: code, collection: invoices, title: Burn-down, file: views/burn.html}
```

The file is checked strictly — an unknown key stops it; YAML aliases are refused; rights never come with it — and
applied only by the owner: `retinue-platform collections project <project> <folder>` shows each part's diff, tries
every form on its records and asks `Apply? [y/N]`; or his «Apply» of a proposal. A session proposes a description
with `collection_propose` (`project` and `description` in place of `collection` and `ops`; a view's page as `html`)
and never applies one.

## Events

- **By a change.** Every `apply` that changed records is matched against the events declared on that collection:
  `{added: true}` an add, `{field, to}` a change that gives the field a value (one of `to`, when it names some). Each
  event is logged once by the change that fired it (`events` in `collections.sqlite`). An import, a define and a
  `revert-batch` fire none.
- **By a call.** An event without `when` is fired with `POST /owner/events/fire {actor, project, event, record?,
  idem}` (the Router's token), by a class its `by` names (`by: [assistant, code:<rule>]`), once by its idem.

## Reactions

| `do` | Does |
|---|---|
| `add` | adds a record to `collection`: each field a value, or `{from: <field>}` of the event's record (`id` its id) |
| `task` | the same, into `tasks` |
| `session` | starts a session on `track` with `brief` and the event's record framed as data; its turns are a machine's: its writes go to the owner as cards |
| `ask` | a card in «Waiting for you» with `text` |
| `send`, `pay`, `publish` | never done: a card that it is the owner's to do |

- **The reacting project's rights.** A reaction writes as `project:<the reacting project>` via `event`, a class of its
  own in each collection's rights (`collections rights`): what the owner gave it there, nothing else — never what the
  project that fired the event may do. A refused write is kept in the event's log with its reason.
- **Loops stop.** An event fired by a reaction's write is one deeper than the one it answered; at depth 4 no reaction
  runs, the event is logged as stopped and the owner is asked once.
- The reactions' changes are in the Space's «Changes» with «Undo», and in the evening list beside the rules'.

## New field types and views

| Type | Value |
|---|---|
| `money` | an amount in the field's one `currency` (three capital letters), at most two places: `"12.5"` or `12.5` is kept as `"12.50"`, so sums are exact |
| `file` | an uploaded file's sha256 (`POST /api/c/_files`); a row shows its name |

| Kind | Takes | Shows |
|---|---|---|
| `months` | `date`; `sum`: an `int` or `money` field, none to count; `group`: an enum; `months`: 2–24, 12 by default | `{date, sum, unit, group, values, months: [{first, totals: [{value, total}]}]}`, this month last; money totals as exact strings |
| `year` | `date`; `sum` | `{date, sum, unit, year, through, months: [{first, total, running}], previous}`: this year's running total by month beside last year's |

**A view in code**, where no standard view fits: its page is served at `/api/c/<collection>/_code/<view>` under the
pages service's sandbox with scripts on (`pagesd.SANDBOX`): an opaque origin, its own inline scripts, styles and
`data:` images, `connect-src 'none'`, no frames, no forms sent, framed by the app alone. A page that loads anything
from elsewhere is refused when the description is read. The app opens it in its frame and posts it the view's data —
`{view: {name, title, fields, labels, count, rows, dropped}}` — whenever it loads or the data changes; nothing the
page sends back is read.
