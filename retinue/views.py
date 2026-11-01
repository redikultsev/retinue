"""Views: what a Space's screen shows of its collections, and the numbers the Router says of them
(docs/collections.md#views). Read only, computed on the collections' thread from each collection's `views`, in one
read transaction with the `rev` they are of — a screen that hears of a newer one reads again.

Five kinds, a closed set (`collections.VIEW_KEYS`):
- `stat`, one number: the records a filter matches, or those whose field changed, this week or this month — with its
  goal from the form and the same number for the period before;
- `board`, the records in columns by an enum, each with the days since that field last changed;
- `queue`, the records a filter matches, the longest silent first (or by a field), with the days since;
- `agenda`, the records whose date falls in the days ahead, today first;
- `chart`, the records counted by the week their date falls in, split by an enum.
A view's `tab` names the tab of the screen it shows in; none, the Space's first.

A view that names a field its form lacks shows «field not found» in its place, and the others still show. Weeks and
days are counted in one time zone: the one the Router last told (`POST /owner/zone`), the config's `owner_tz` until it
has. The Router's own numbers (`POST /owner/collections/stat`, the `stat` views its config names) come in the zone it
names, through the same code as the screen's — so one zone gives one number in both.

The owner writes from the same screen (`POST /api/c/<collection>`): the operations of `apply`, by `owner` via
`screen`, each `set`, `delete` and `revert` at the version his screen showed — one that moved on since is answered
409 with the record as it is and its last change, for the screen to ask «Apply mine» or «Keep». A board moves a
record by its `group`, a queue's `done` sets one field on a row, a view's `add` opens the form's fields; a record's
changes, and the Space's newest (`GET /api/c/_changes`), are each taken back by a `revert`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from .collections import (CHANGE_COLUMNS, HISTORY, NAME, RECORD_COLUMNS, SPACE, ULID, Collections, Refused, Rejected,
                          Writer, _change, _json, _path, _record)

if TYPE_CHECKING:
    from .platformd import Platform

VIEW_MAX = 256 * 2**10      # bytes of one answer: a view, or a Space's views
ROWS_MAX = VIEW_MAX - 16 * 2**10  # of them the rows' share; the rest is the views' own words
RECORDS_MAX = 2000          # records one board or queue reads, the oldest first: more than its rows can ever hold
AGENDA_DAYS = 7             # the days an agenda looks ahead by default, today among them
CHART_WEEKS = 8             # the weeks a chart counts by default, this one among them
RELATED_MAX = 30            # records of other collections that name a record, its sheet lists
IDS_CHUNK = 500             # ids in one statement's IN (…)
ZONE_KEY = "collections.zone"  # the platform's kv: the zone the Router last told
NOT_FOUND = "field not found"
NO_STAT = "no such stat"
STATS_NAMED = 8             # numbers the Router names in one call
CHANGES_SHOWN = 50          # the Space's newest changes one answer lists
OPTIONS_MAX = 500           # records a ref field offers to pick from, the newest changed first
CONFLICTS = ("stale", "after")  # refusals the screen asks about (409): the record moved on since it was shown
SCREEN = Writer("owner", "screen")
COLLECTION = "[a-z][a-z0-9_]{0,39}"  # as `collections.NAME`: never a reserved `_files` or `_space`
RECORD_ID = "[0-9A-HJKMNP-TV-Z]{26}"
STAT_NAME = re.compile(f"{NAME.pattern}/{NAME.pattern}")  # a number the Router names: `<collection>/<view>`
DAY = dt.timedelta(days=1)


def zone(name: object) -> ZoneInfo | None:
    """An IANA time zone by its name (`Europe/Berlin`), or None for anything else."""
    if not isinstance(name, str) or not 0 < len(name) <= 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


@dataclass(frozen=True)
class When:
    """The moment a view is of, and the zone its days are counted in."""

    tz: str
    now: float

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self.now, self.zone).date()

    def shown(self) -> dict:
        return {"tz": self.tz, "today": self.today.isoformat(), "at": self.now}

    def date(self, ts: float) -> dt.date:
        return dt.datetime.fromtimestamp(ts, self.zone).date()

    def start(self, day: dt.date) -> float:
        """The moment `day` begins here."""
        return dt.datetime.combine(day, dt.time(), self.zone).timestamp()


def window(kind: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """The week (Monday to Sunday) or the month that holds `today`: its first day and its last."""
    if kind == "week":
        first = today - dt.timedelta(days=today.weekday())
        return first, first + 6 * DAY
    first = today.replace(day=1)
    return first, (first + 32 * DAY).replace(day=1) - DAY


# --- one view, on the collections' thread --------------------------------------------------------------------------

def _field(form: dict, name: str, *types: str) -> dict:
    spec = form["fields"].get(name)
    if spec is None:
        raise Rejected(f"{NOT_FOUND}: {name!r}")
    if types and spec["type"] not in types:
        raise Rejected(f"{name!r} is a {spec['type']}, not a {' or '.join(types)}")
    return spec


def _conditions(form: dict, view: dict) -> list:
    """The view's filter, its fields found in the form first: one it lacks is «field not found», not a query's
    refusal."""
    given = view.get("filter", [])
    conditions = [given] if isinstance(given, dict) else list(given)
    for condition in conditions:
        name = condition.get("field") if isinstance(condition, dict) else None
        if isinstance(name, str) and name.partition(".")[0] not in form["fields"]:
            raise Rejected(f"{NOT_FOUND}: {name.partition('.')[0]!r}")
    return conditions


def _after(field: str) -> str:
    """A field's value as a change made it, as SQL: like `collections._path`, only a name the form declared."""
    if not NAME.fullmatch(field):
        raise ValueError(field)
    return f"json_extract(after, '$.{field}')"


def _changed(store: Collections, collection: str, form: dict, conditions: list, field: str, to: list,
             span: tuple[dt.date, dt.date] | None, when: When) -> int:
    """How many records the filter matches now whose `field` a change gave a value (one of `to`, when it names
    some) within `span`: a change dated by its `as_of`, else by the day it was made, here. A change taken back, and
    the revert that took it, move nothing: a drag and its «Undo» count none."""
    where, params = store._where(collection, form, conditions or None, False)
    sql = [f"SELECT count(DISTINCT id) FROM changes WHERE collection = ? AND {_after(field)} IS NOT NULL",
           "AND reverts IS NULL AND change_id NOT IN (SELECT reverts FROM changes WHERE collection = ? AND reverts IS "
           f"NOT NULL) AND id IN (SELECT id FROM records WHERE {where})"]
    values: list[Any] = [collection, collection, *params]
    if to:
        sql.append(f"AND {_after(field)} IN ({', '.join('?' * len(to))})")
        values += to
    if span is not None:
        sql.append("AND (as_of BETWEEN ? AND ? OR as_of IS NULL AND ts >= ? AND ts < ?)")
        values += [span[0].isoformat(), span[1].isoformat(), when.start(span[0]), when.start(span[1] + DAY)]
    return store._db.execute(" ".join(sql), values).fetchone()[0]


def _stat(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    count, kind = view.get("count", "records"), view.get("window")
    goal = None
    if "goal" in view:
        goal = form.get("goals", {}).get(view["goal"])
        if goal is None:
            raise Rejected(f"goal not found: {view['goal']!r}")
    conditions = _conditions(form, view)
    if count == "records":
        if kind is not None:
            _field(form, view["date"], "date")

        def value(span: tuple[dt.date, dt.date] | None) -> int:
            dated = [] if span is None else [{"field": view["date"], "op": "between",
                                              "value": [span[0].isoformat(), span[1].isoformat()]}]
            return store._count(collection, conditions + dated or None, None)
    else:
        spec = _field(form, view["field"])
        to = [store._value(view["field"], spec, v, probe=True) for v in view.get("to", [])]

        def value(span: tuple[dt.date, dt.date] | None) -> int:
            return _changed(store, collection, form, conditions, view["field"], to, span, when)
    if kind is None:
        return {"value": value(None), "goal": goal, "previous": None, "window": None, "first": None, "last": None}
    first, last = window(kind, when.today)
    return {"value": value((first, last)), "goal": goal, "previous": value(window(kind, first - DAY)),
            "window": kind, "first": first.isoformat(), "last": last.isoformat()}


def _rot(form: dict, view: dict) -> int | None:
    if "rot" not in view:
        return None
    days = form.get("rot", {}).get(view["rot"])
    if days is None:
        raise Rejected(f"threshold not found: {view['rot']!r}")
    return days


def _shown(form: dict, view: dict) -> list[str] | None:
    for name in view.get("fields", []):
        _field(form, name)
    return view.get("fields") or None


def _said(store: Collections, collection: str | None, ids) -> dict[str, str]:
    """The words of these records of `collection`: the first `text` field of its form, as each has it now — so a ref
    reads «Acme», not an id. A deleted record, or one without that field, is not among them."""
    ids = sorted({i for i in ids if isinstance(i, str)})
    target = store._db.execute("SELECT form FROM schemas WHERE collection = ?",
                               (collection,)).fetchone() if ids else None
    text = next((k for k, s in json.loads(target[0])["fields"].items() if s["type"] == "text"),
                None) if target else None
    said: dict[str, str] = {}
    if text is None:
        return said
    for at in range(0, len(ids), IDS_CHUNK):
        chunk = ids[at:at + IDS_CHUNK]
        said.update(store._db.execute(
            f"SELECT id, {_path(text)} FROM records WHERE collection = ? AND id IN ({', '.join('?' * len(chunk))})"
            f" AND deleted IS NULL AND {_path(text)} IS NOT NULL", (collection, *chunk)).fetchall())
    return said


def _refs(store: Collections, form: dict, records: list[dict], fields: list[str] | None) -> dict:
    """For each record, the `ref` fields a view shows by the words of what they name (`_said`); a deleted record, or
    one without such words: none, and the row shows the id."""
    words: dict = {}
    for name in fields or form["fields"]:
        spec = form["fields"][name]
        if spec["type"] != "ref":
            continue
        said = _said(store, spec["to"], (r["fields"].get(name) for r in records))
        for record in records:
            if record["fields"].get(name) in said:
                words.setdefault(record["id"], {})[name] = said[record["fields"][name]]
    return words


def _title(store: Collections, form: dict, fields: dict, parts: int = 2) -> str | None:
    """A record in a few words, for a list of changes or a ref to pick: its first `parts` text or ref fields that have
    a value, in the form's order, a ref by its record's words — «Acme · Backend». None when it has none."""
    words = []
    for name, spec in form["fields"].items():
        value = fields.get(name)
        if spec["type"] == "text" and isinstance(value, str):
            words.append(value)
        elif spec["type"] == "ref" and (said := _said(store, spec["to"], [value]).get(value)):
            words.append(said)
        if len(words) == parts:
            break
    return " · ".join(words) or None


def _records(store: Collections, collection: str, form: dict, conditions: list) -> tuple[list[dict], int]:
    """The records the filter matches, the oldest RECORDS_MAX of them, and how many it matches in all."""
    where, params = store._where(collection, form, conditions or None, False)
    total = store._db.execute(f"SELECT count(*) FROM records WHERE {where}", params).fetchone()[0]
    return [_record(row) for row in store._db.execute(
        f"SELECT {RECORD_COLUMNS} FROM records WHERE {where} ORDER BY id LIMIT ?", (*params, RECORDS_MAX))], total


def _marks(store: Collections, collection: str, ids: list[str], field: str | None, when: When) -> tuple[dict, dict]:
    """For each of these records: the day its last change was made — of `field`, when one is named (its `as_of`,
    else the day it was made, here) — and the `src` of its last change that had one. Only their own changes are read,
    by the index on a record's changes."""
    if field is not None and not NAME.fullmatch(field):
        raise ValueError(field)
    touched = f"json_type(after, '$.{field}') IS NOT NULL" if field is not None else "1"
    days, srcs = {}, {}
    for at in range(0, len(ids), IDS_CHUNK):
        chunk = ids[at:at + IDS_CHUNK]
        last = (f"SELECT id, {{}} FROM changes WHERE seq IN (SELECT max(seq) FROM changes WHERE collection = ? AND id "
                f"IN ({', '.join('?' * len(chunk))}) AND {{}} GROUP BY id)")
        for rid, as_of, ts in store._db.execute(last.format("as_of, ts", touched), (collection, *chunk)):
            days[rid] = dt.date.fromisoformat(as_of) if as_of else when.date(ts)
        srcs.update(store._db.execute(last.format("src", "src IS NOT NULL"), (collection, *chunk)).fetchall())
    return days, srcs


def _row(record: dict, fields: list[str] | None, refs: dict, since: dt.date | None, src: str | None, when: When,
         rot: int | None, budget: list[int]) -> dict | None:
    """A record as a view shows it — or None when it would take the answer past its size."""
    days = (when.today - since).days if since is not None else None
    row = {"id": record["id"], "version": record["version"],
           "fields": {k: v for k, v in record["fields"].items() if fields is None or k in fields},
           "refs": refs.get(record["id"], {}), "src": src, "days": days,
           "rotten": rot is not None and days is not None and days > rot}
    size = len(_json(row).encode()) + 1
    if size > budget[0]:
        return None
    budget[0] -= size
    return row


def _longest(days: dict):
    """The order a view lists records in: the longest since their day first, those without one last."""
    return lambda record: (record["id"] not in days, days.get(record["id"], dt.date.max), record["id"])


def _board(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    group = view["group"]
    spec, fields, rot = _field(form, group, "enum"), _shown(form, view), _rot(form, view)
    if lost := [v for v in view.get("folded", []) if v not in spec["values"]]:
        raise Rejected(f"{group!r} has no value {', '.join(map(repr, lost))}")
    conditions = _conditions(form, view)
    records, _ = _records(store, collection, form, conditions)
    days, srcs = _marks(store, collection, [r["id"] for r in records], group, when)
    columns: dict = {value: [] for value in spec["values"]}
    for record in records:
        columns.setdefault(record["fields"].get(group), []).append(record)
    counts = {c["value"]: c["count"] for c in store._count(collection, conditions or None, group)}
    refs = _refs(store, form, records, fields)
    folded = set(view.get("folded", []))
    shown, dropped = {}, sum(counts.values()) - len(records)  # past RECORDS_MAX: never read, counted
    for value in sorted(columns, key=lambda v: (v in folded, v is None)):  # what is folded takes what is left
        rows = []
        for record in sorted(columns[value], key=_longest(days)):
            row = _row(record, fields, refs, days.get(record["id"]), srcs.get(record["id"]), when, rot, budget)
            if row is None:
                dropped += 1
            else:
                rows.append(row)
        shown[value] = {"value": value, "count": counts.get(value, 0), "folded": value in folded, "rows": rows}
    order = [*spec["values"], *([None] if None in counts else [])]
    return {"group": group, "rot": rot, "fields": fields or list(form["fields"]), "columns": [shown[v] for v in order],
            "dropped": dropped}


def _sorted(field: str | None, days: dict):
    """A queue's order: by `field` when it names one — the least first, those without it last — then the longest
    since."""
    longest = _longest(days)
    if field is None:
        return longest
    return lambda record: (record["fields"].get(field) is None, record["fields"].get(field), longest(record))


def _queue(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    fields, rot, since, sort = _shown(form, view), _rot(form, view), view.get("since"), view.get("sort")
    if since is not None:
        _field(form, since, "date")
    if sort is not None:
        _field(form, sort, "int", "date", "text", "enum")
    records, total = _records(store, collection, form, _conditions(form, view))
    days, srcs = _marks(store, collection, [r["id"] for r in records], None, when)
    if since is not None:
        days = {r["id"]: dt.date.fromisoformat(r["fields"][since]) for r in records if since in r["fields"]}
    rows, dropped, refs = [], total - len(records), _refs(store, form, records, fields)
    for record in sorted(records, key=_sorted(sort, days)):
        row = _row(record, fields, refs, days.get(record["id"]), srcs.get(record["id"]), when, rot, budget)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"rot": rot, "fields": fields or list(form["fields"]), "count": total, "rows": rows,
            "dropped": dropped, "mark": view.get("mark", "act")}


def _agenda(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """The records whose `date` falls from today to the last of `days`, the soonest first; each row's `days` below
    zero, today's zero."""
    date, fields = view["date"], _shown(form, view)
    _field(form, date, "date")
    shown = fields + [date] if fields and date not in fields else fields  # a row always says its day
    last = when.today + (view.get("days", AGENDA_DAYS) - 1) * DAY
    span = [{"field": date, "op": "between", "value": [when.today.isoformat(), last.isoformat()]}]
    records, total = _records(store, collection, form, _conditions(form, view) + span)
    refs, rows, dropped = _refs(store, form, records, fields), [], total - len(records)
    for record in sorted(records, key=lambda r: (r["fields"][date], r["id"])):
        row = _row(record, shown, refs, dt.date.fromisoformat(record["fields"][date]), None, when, None, budget)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"date": date, "fields": fields or list(form["fields"]), "first": when.today.isoformat(),
            "last": last.isoformat(), "count": total, "rows": rows, "dropped": dropped}


def _chart(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """The records the filter matches counted by the week (Monday to Sunday) their `date` falls in, the last `weeks`
    of them, this one last — each split by the enum `group` names, when it names one."""
    date, group = view["date"], view.get("group")
    _field(form, date, "date")
    values = _field(form, group, "enum")["values"] if group else []
    this, _ = window("week", when.today)
    weeks = [this - 7 * DAY * n for n in range(view.get("weeks", CHART_WEEKS) - 1, -1, -1)]
    conditions = _conditions(form, view)
    counted = []
    for first in weeks:
        span = [{"field": date, "op": "between", "value": [first.isoformat(), (first + 6 * DAY).isoformat()]}]
        if group:
            counts = {c["value"]: c["count"] for c in store._count(collection, conditions + span, group)}
        else:
            counts = {None: store._count(collection, conditions + span, None)}
        counted.append({"first": first.isoformat(), "counts": [{"value": v, "count": n} for v, n in counts.items()]})
    return {"date": date, "group": group, "values": values, "weeks": counted}


KINDS = {"stat": _stat, "board": _board, "queue": _queue, "agenda": _agenda, "chart": _chart}


def _done(store: Collections, form: dict, view: dict) -> dict | None:
    """A queue's press on a row, its field found in the form and its value one the field takes."""
    if "done" not in view:
        return None
    done = view["done"]
    value = store._value(done["field"], _field(form, done["field"]), done["value"], probe=True)
    return {"field": done["field"], "value": value, "label": done["label"]}


def _add(store: Collections, form: dict, view: dict) -> dict | None:
    """A view's «add»: the fields its sheet asks for — the view's, else every one of the form — and those it starts
    with, each found in the form and of its type."""
    if "add" not in view:
        return None
    add = view["add"]
    fields = add.get("fields") or list(form["fields"])
    for name in fields:
        _field(form, name)
    values = {name: store._value(name, _field(form, name), value, probe=True)
              for name, value in add.get("values", {}).items()}
    return {"label": add["label"], "fields": fields, "values": values}


def _view(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    shown = {"name": view["name"], "kind": view["kind"], "title": view.get("title", view["name"]),
             **({"tab": view["tab"]} if "tab" in view else {})}
    try:
        shown.update(KINDS[view["kind"]](store, collection, form, view, when, budget))
        if view["kind"] == "queue":
            shown["done"] = _done(store, form, view)
        if view["kind"] in ("board", "queue"):
            shown["add"] = _add(store, form, view)
    except Rejected as exc:  # a field the form lacks, a value its enum does not have: this view only
        shown = {k: shown[k] for k in ("name", "kind", "title", "tab") if k in shown} | {"error": exc.reason}
    return shown


def _schemas(store: Collections, space: str) -> list[tuple[str, dict, list]]:
    return [(name, json.loads(form), json.loads(views)) for name, form, views in store._db.execute(
        "SELECT collection, form, views FROM schemas WHERE space = ? ORDER BY collection", (space,)).fetchall()]


def space_views(store: Collections, space: str, when: When) -> dict:
    """A Space's collections, each with its rev and its views: `{space, tz, today, at, collections: [{name, rev,
    views}]}` — the stats of every collection first, then the rest, within one VIEW_MAX."""
    schemas, budget, made = _schemas(store, space), [ROWS_MAX], {}
    for first in (True, False):  # every number made before any rows: a long board never crowds a stat out
        for name, form, views in schemas:
            for n, view in enumerate(views):
                if (view["kind"] == "stat") == first:
                    made[name, n] = _view(store, name, form, view, when, budget)
    return {"space": space, **when.shown(), "collections": [
        {"name": name, "rev": store._rev(name), "labels": _labels(form),
         "views": [made[name, n] for n in range(len(views))]}
        for name, form, views in schemas]}


def _labels(form: dict) -> dict[str, dict[str, str]]:
    """Every enum of the form, with the words it gives its values — `{"stage": {"pack_ready": "Ready to send"},
    "origin": {}}` — so the screen puts each value in words, one without them as itself made readable."""
    return {name: dict(spec.get("labels", {})) for name, spec in form["fields"].items() if spec["type"] == "enum"}


def one_view(store: Collections, collection: str, name: str, when: When) -> dict | None:
    """One view of a collection: `{name, rev, tz, today, at, view}`, or None when there is no such view."""
    row = store._db.execute("SELECT form, views FROM schemas WHERE collection = ?", (collection,)).fetchone()
    view = next((v for v in json.loads(row[1]) if v["name"] == name), None) if row else None
    if view is None:
        return None
    return {"name": collection, "rev": store._rev(collection), **when.shown(),
            "view": _view(store, collection, json.loads(row[0]), view, when, [ROWS_MAX])}


def stats(store: Collections, named: list[tuple[str, str]], when: When) -> dict:
    """The numbers the Router names, `(collection, view)` each, as it says them: `{tz, today, at, stats: [{collection,
    name, title, value, goal, previous, window, first, last}]}` in its order — `error` for one, and «no such stat»
    for a name that is not one of a collection's `stat` views."""
    forms: dict = {}
    said = []
    for collection, name in named:
        if collection not in forms:
            row = store._db.execute("SELECT form, views FROM schemas WHERE collection = ?", (collection,)).fetchone()
            forms[collection] = (json.loads(row[0]), json.loads(row[1])) if row else (None, [])
        form, views = forms[collection]
        view = next((v for v in views if v["name"] == name and v["kind"] == "stat"), None)
        said.append({"collection": collection, **(_view(store, collection, form, view, when, [0]) if view else
                                                  {"name": name, "kind": "stat", "title": name, "error": NO_STAT})})
    return {**when.shown(), "stats": said}


def _fields(form: dict) -> dict:
    """A form's fields as the screen asks for them: each one's type, and its values, labels, `to`, `required` and
    `hide` — a field kept for the agents, which a record's sheet leaves out."""
    return {name: {k: v for k, v in spec.items() if k in ("type", "values", "labels", "to", "required", "hide")}
            for name, spec in form["fields"].items()}


def _ref_words(store: Collections, form: dict, values) -> dict[str, str]:
    """The words of every id among `values` (dicts of fields) that one of the form's ref fields names."""
    words: dict[str, str] = {}
    for name, spec in form["fields"].items():
        if spec["type"] == "ref":
            words.update(_said(store, spec["to"], (v.get(name) for v in values if isinstance(v, dict))))
    return words


def record_view(store: Collections, collection: str, rid: str) -> dict | None:
    """One record, a deleted one included, with its newest HISTORY changes: `{name, rev, title, fields, record,
    changes, words}` — its form's fields to show it by and `words` for the ids its refs name, now and in the changes —
    or None."""
    record, form = store._record(collection, rid), store._form(collection)
    if record is None or form is None:
        return None
    changes = store._history(collection, rid, HISTORY)
    values = [record["fields"], *(c["before"] for c in changes), *(c["after"] for c in changes)]
    return {"name": collection, "rev": store._rev(collection), "title": _title(store, form, record["fields"]),
            "fields": _fields(form), "record": record, "changes": changes, "words": _ref_words(store, form, values),
            "related": _related(store, collection, rid)}


def _related(store: Collections, collection: str, rid: str) -> list[dict]:
    """The records of the Space's other collections whose ref names this one — the touches of a deal, its rounds —
    each collection's newest RELATED_MAX by their first date field: `[{name, fields, date, records}]`."""
    space = store._db.execute("SELECT space FROM schemas WHERE collection = ?", (collection,)).fetchone()
    related = []
    for name, form, _ in _schemas(store, space[0]) if space else []:
        refs = [f for f, spec in form["fields"].items() if spec["type"] == "ref" and spec["to"] == collection]
        if not refs:
            continue
        date = next((f for f, spec in form["fields"].items() if spec["type"] == "date"), None)
        where, params = store._where(name, form, [{"field": refs[0], "op": "ref_is", "value": rid}], False)
        order = f"{_path(date)} DESC, id DESC" if date else "id DESC"
        records = [_record(row) for row in store._db.execute(
            f"SELECT {RECORD_COLUMNS} FROM records WHERE {where} ORDER BY {order} LIMIT ?", (*params, RELATED_MAX))]
        if records:
            related.append({"name": name, "fields": _fields(form), "date": date, "ref": refs[0], "records": records})
    return related


def form_view(store: Collections, collection: str) -> dict | None:
    """What a sheet that adds a record asks for: `{name, rev, fields, options}` — the form's fields, and for each ref
    field the records it may name, `[{id, title}]`, the OPTIONS_MAX newest changed of those not deleted. None for no
    such collection."""
    form = store._form(collection)
    if form is None:
        return None
    options = {}
    for name, spec in form["fields"].items():
        if spec["type"] != "ref":
            continue
        target = store._form(spec["to"])
        rows = store._db.execute(f"SELECT {RECORD_COLUMNS} FROM records WHERE collection = ? AND deleted IS NULL "
                                 "ORDER BY updated DESC, id DESC LIMIT ?", (spec["to"], OPTIONS_MAX)) if target else []
        options[name] = [{"id": r["id"], "title": _title(store, target, r["fields"]) or r["id"]}
                         for r in map(_record, rows)]
    return {"name": collection, "rev": store._rev(collection), "fields": _fields(form), "options": options}


def space_changes(store: Collections, space: str, before: str | None) -> dict:
    """The newest CHANGES_SHOWN changes to the records of a Space's collections — before the change `before`, when
    one is named — each with its record's title, version and deleted state now, for its «Undo», and whether a revert
    took it back already: `{space, changes, next, words}`, `next` the change to read on from."""
    forms = {name: json.loads(form) for name, form in store._db.execute(
        "SELECT collection, form FROM schemas WHERE space = ? ORDER BY collection", (space,)).fetchall()}
    if not forms:
        return {"space": space, "changes": [], "next": None, "words": {}}
    sql = (f"SELECT {CHANGE_COLUMNS} FROM changes WHERE collection IN ({', '.join('?' * len(forms))}) "
           "AND id IS NOT NULL")
    params: list[Any] = list(forms)
    if before is not None:
        sql += " AND seq < coalesce((SELECT seq FROM changes WHERE change_id = ?), 0)"
        params.append(before)
    rows = [_change(r) for r in store._db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*params, CHANGES_SHOWN + 1))]
    shown, words = [], {}
    taken = {r[0] for r in store._db.execute(
        f"SELECT reverts FROM changes WHERE reverts IN ({', '.join('?' * len(rows))})",
        [c["change_id"] for c in rows])} if rows else set()
    for change in rows[:CHANGES_SHOWN]:
        form, record = forms[change["collection"]], store._record(change["collection"], change["id"])
        words.update(_ref_words(store, form, [change["before"], change["after"]]))
        shown.append({**change, "title": _title(store, form, record["fields"]) if record else None,
                      "version": record["version"] if record else None,
                      "deleted": record["deleted"] if record else None, "reverted": change["change_id"] in taken})
    return {"space": space, "changes": shown, "words": words,
            "next": shown[-1]["change_id"] if len(rows) > CHANGES_SHOWN else None}


# --- the routes --------------------------------------------------------------------------------------------------

class Views:
    """`/api/c/…` for the face, behind its login (`routes`), and `/owner/…` for the Router, behind its token
    (`owner_routes`)."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def zone(self) -> str:
        """The zone the screen's weeks and days are counted in: the Router's last, else the config's."""
        told = self.platform.store.get(ZONE_KEY)
        return told if zone(told) is not None else self.platform.cfg.owner_tz

    def routes(self) -> list:
        return [web.get("/api/c/_space", self.space), web.get("/api/c/_changes", self.changes),
                web.get(f"/api/c/{{collection:{COLLECTION}}}", self.view),
                web.post(f"/api/c/{{collection:{COLLECTION}}}", self.write),
                web.get(f"/api/c/{{collection:{COLLECTION}}}/_form", self.form),
                web.get(f"/api/c/{{collection:{COLLECTION}}}/{{id:{RECORD_ID}}}", self.record)]

    def owner_routes(self) -> list:
        return [web.post("/owner/zone", self.owner_zone), web.post("/owner/collections/stat", self.owner_stat)]

    async def _answer(self, work, *args) -> web.Response:
        store = self.platform.collections
        try:
            data = await store.read(lambda: work(store, *args))
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if data is None:
            return web.json_response({"error": "No such view or record."}, status=404)
        return web.json_response(data)

    def _when(self, tz: str | None = None) -> When:
        return When(tz or self.zone(), self.platform.clock())

    async def space(self, request: web.Request) -> web.Response:
        """A Space's screen: its collections and their views, as of one moment."""
        name = request.query.get("space", "")
        if not SPACE.fullmatch(name):
            return web.json_response({"error": "space: a Space's name"}, status=400)
        return await self._answer(space_views, name, self._when())

    async def view(self, request: web.Request) -> web.Response:
        """One view again, after the stream said its collection moved on."""
        return await self._answer(one_view, request.match_info["collection"], request.query.get("view", ""),
                                  self._when())

    async def record(self, request: web.Request) -> web.Response:
        return await self._answer(record_view, request.match_info["collection"], request.match_info["id"])

    async def form(self, request: web.Request) -> web.Response:
        return await self._answer(form_view, request.match_info["collection"])

    async def changes(self, request: web.Request) -> web.Response:
        """A Space's newest changes, the owner's and every writer's, each to be taken back."""
        name, before = request.query.get("space", ""), request.query.get("before")
        if not SPACE.fullmatch(name) or before is not None and not ULID.fullmatch(before):
            return web.json_response({"error": "space: a Space's name; before: a change's id"}, status=400)
        return await self._answer(space_changes, name, before)

    async def write(self, request: web.Request) -> web.Response:
        """The owner's write from the screen: `{ops, idem?}`, the operations of `apply` on one collection, by `owner`
        via `screen`. Answers apply's `{name, rev, change_ids, records}`; a refusal is `{error, kind, record,
        change}` — 409 when the record moved on since the screen showed it (`stale`) or a revert's change was
        changed after (`after`), 400 for the rest."""
        body = await _body(request)
        try:
            answer = await self.platform.collections.apply(SCREEN, request.match_info["collection"], body.get("ops"),
                                                           self.platform.clock(), idem=body.get("idem"))
        except Rejected as exc:
            return web.json_response({"error": exc.reason, "kind": exc.kind, "record": exc.record,
                                      "change": exc.change}, status=409 if exc.kind in CONFLICTS else 400)
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response(answer)

    async def owner_zone(self, request: web.Request) -> web.Response:
        """The Router's zone, at its start and whenever the owner's changes: the screen counts its weeks in it."""
        tz = (await _body(request)).get("tz")
        if zone(tz) is None:
            return web.json_response({"error": "tz: an IANA time zone, e.g. Europe/Berlin"}, status=400)
        self.platform.store.set(ZONE_KEY, tz)
        return web.json_response({"tz": tz})

    async def owner_stat(self, request: web.Request) -> web.Response:
        """The numbers the Router names (`views`: `<collection>/<view>` each) in the zone it names: the same as the
        screen's tiles in that zone. `screen_tz` is the zone the screen counts in: another than the Router's — a kv
        restored from an older backup — and the Router tells its own again."""
        body = await _body(request)
        named, tz = body.get("views"), body.get("tz")
        pairs = [tuple(v.split("/")) for v in named if isinstance(v, str) and STAT_NAME.fullmatch(v)] \
            if isinstance(named, list) and 0 < len(named) <= STATS_NAMED else []
        if not pairs or len(pairs) != len(named) or zone(tz) is None:
            return web.json_response({"error": f"views: 1 to {STATS_NAMED} of <collection>/<view>; tz: an IANA time "
                                               "zone"}, status=400)
        screen = self.zone()
        return await self._answer(lambda store, *args: {**stats(store, *args), "screen_tz": screen}, pairs,
                                  self._when(tz))


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
