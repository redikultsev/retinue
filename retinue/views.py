"""Views: what a Space's screen shows of its collections, and the numbers the Router says of them
(docs/collections.md#views). Read only, computed on the collections' thread from each collection's `views`, in one
read transaction with the `rev` they are of — a screen that hears of a newer one reads again.

Five kinds, a closed set (`collections.VIEW_KEYS`):
- `stat`, one number: the records a filter matches, or those whose field changed, this week or this month — with its
  goal from the form and the same number for the period before;
- `board`, the records in columns by an enum, each with the days since that field last changed;
- `queue`, the records a filter matches, the longest silent first (or by a field), with the days since — and, by its
  `latest`, the newest record of another collection that names each (a deal's last touch);
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
import decimal
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from . import pagesd
from .collections import HISTORY, NAME, SPACE, ULID, UPDATED, Reader, Refused, Rejected, Writer, shown_fields

if TYPE_CHECKING:
    from .platformd import Platform

VIEW_MAX = 256 * 2**10      # bytes of one answer: a view, or a Space's views
ROWS_MAX = VIEW_MAX - 16 * 2**10  # of them the rows' share; the rest is the views' own words
RECORDS_MAX = 2000          # records one board or queue reads, the oldest first: more than its rows can ever hold
AGENDA_DAYS = 7             # the days an agenda looks ahead by default, today among them
CHART_WEEKS = 8             # the weeks a chart counts by default, this one among them
MONTHS = 12                 # the months a `months` view sums by default, this one among them
NO_FRAME = "'none'"         # who may frame a view in code when the app has no origin: no one
RELATED_MAX = 30            # records of other collections that name a record, its sheet lists
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


def _changed(reader: Reader, collection: str, conditions: list, field: str, to: list,
             span: tuple[dt.date, dt.date] | None, when: When) -> int:
    """How many records the filter matches now whose `field` a change gave a value (one of `to`, when it names
    some) within `span`: a change dated by its `as_of`, else by the day it was made, here (`Reader.changed`)."""
    moments = None if span is None else (span[0].isoformat(), span[1].isoformat(), when.start(span[0]),
                                         when.start(span[1] + DAY))
    return reader.changed(collection, conditions or None, field, to, moments)


def _stat(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
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
            return reader.count(collection, conditions + dated or None, None)
    else:
        spec = _field(form, view["field"])
        to = [reader.probe(view["field"], spec, v) for v in view.get("to", [])]

        def value(span: tuple[dt.date, dt.date] | None) -> int:
            return _changed(reader, collection, conditions, view["field"], to, span, when)
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


def _refs(reader: Reader, form: dict, records: list[dict], fields: list[str] | None) -> dict:
    """For each record, the `ref` fields a view shows by the words of what they name (`Reader.words`), a `file` by its
    name; a deleted record, or one without such words: none, and the row shows the id."""
    words: dict = {}
    for name in fields or form["fields"]:
        spec = form["fields"][name]
        if spec["type"] == "file":  # an uploaded file by its name, never its sha256
            said = reader.file_names(r["fields"].get(name) for r in records)
        elif spec["type"] != "ref":
            continue
        else:
            said = reader.words(spec["to"], (r["fields"].get(name) for r in records))
        for record in records:
            if record["fields"].get(name) in said:
                words.setdefault(record["id"], {})[name] = said[record["fields"][name]]
    return words


def _records(reader: Reader, collection: str, conditions: list) -> tuple[list[dict], int]:
    """The records the filter matches, the oldest RECORDS_MAX of them, and how many it matches in all."""
    return (reader.records(collection, conditions or None, limit=RECORDS_MAX),
            reader.count(collection, conditions or None))


def _marks(reader: Reader, collection: str, ids: list[str], field: str | None, when: When) -> tuple[dict, dict]:
    """For each of these records: the day its last change was made — of `field`, when one is named (its `as_of`,
    else the day it was made, here) — and the `src` of its last change that had one."""
    last, srcs = reader.marks(collection, ids, field)
    return {rid: dt.date.fromisoformat(as_of) if as_of else when.date(ts) for rid, (as_of, ts) in last.items()}, srcs


def _row(record: dict, fields: list[str] | None, refs: dict, since: dt.date | None, src: str | None, when: When,
         rot: int | None, budget: list[int], extra: dict | None = None) -> dict | None:
    """A record as a view shows it — or None when it would take the answer past its size."""
    days = (when.today - since).days if since is not None else None
    row = {"id": record["id"], "version": record["version"],
           "fields": {k: v for k, v in record["fields"].items() if fields is None or k in fields},
           "refs": refs.get(record["id"], {}), "src": src, "days": days,
           "rotten": rot is not None and days is not None and days > rot, **(extra or {})}
    size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) + 1
    if size > budget[0]:
        return None
    budget[0] -= size
    return row


def _longest(days: dict):
    """The order a view lists records in: the longest since their day first, those without one last."""
    return lambda record: (record["id"] not in days, days.get(record["id"], dt.date.max), record["id"])


def _board(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    group = view["group"]
    spec, fields, rot = _field(form, group, "enum"), _shown(form, view), _rot(form, view)
    if lost := [v for v in view.get("folded", []) if v not in spec["values"]]:
        raise Rejected(f"{group!r} has no value {', '.join(map(repr, lost))}")
    conditions = _conditions(form, view)
    records, _ = _records(reader, collection, conditions)
    days, srcs = _marks(reader, collection, [r["id"] for r in records], group, when)
    columns: dict = {value: [] for value in spec["values"]}
    for record in records:
        columns.setdefault(record["fields"].get(group), []).append(record)
    counts = {c["value"]: c["count"] for c in reader.count(collection, conditions or None, group)}
    refs = _refs(reader, form, records, fields)
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


def _queue(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    fields, rot, since, sort = _shown(form, view), _rot(form, view), view.get("since"), view.get("sort")
    if since is not None:
        _field(form, since, "date")
    if sort is not None:
        _field(form, sort, "int", "date", "text", "enum")
    records, total = _records(reader, collection, _conditions(form, view))
    days, srcs = _marks(reader, collection, [r["id"] for r in records], None, when)
    if since is not None:
        days = {r["id"]: dt.date.fromisoformat(r["fields"][since]) for r in records if since in r["fields"]}
    rows, dropped, refs = [], total - len(records), _refs(reader, form, records, fields)
    latest = _latest(reader, collection, view["latest"], [r["id"] for r in records]) if "latest" in view else {}
    for record in sorted(records, key=_sorted(sort, days)):
        extra = {"latest": latest[record["id"]]} if record["id"] in latest else None
        row = _row(record, fields, refs, days.get(record["id"]), srcs.get(record["id"]), when, rot, budget, extra)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"rot": rot, "fields": fields or list(form["fields"]), "count": total, "rows": rows,
            "dropped": dropped, "mark": view.get("mark", "act"), "latest": "latest" in view}


def _latest(reader: Reader, collection: str, spec: dict, ids: list[str]) -> dict[str, dict]:
    """A queue's `latest`: for each of its records, the newest record of `spec["collection"]` whose ref names it — by
    that form's first date field — as `{date, value, label, arrow}`: its day, its enum `field`'s value, the form's
    words for it and the arrow the view draws it as. A record nothing names has none."""
    form = reader.form(spec["collection"])
    if form is None:
        raise Rejected(f"no collection {spec['collection']!r}")
    ref = next((f for f, s in form["fields"].items() if s["type"] == "ref" and s["to"] == collection), None)
    date = next((f for f, s in form["fields"].items() if s["type"] == "date"), None)
    if ref is None or date is None:
        raise Rejected(f"{spec['collection']!r} has no ref to {collection!r} and a date field to go by")
    labels = _field(form, spec["field"], "enum").get("labels", {})
    arrows, out = spec.get("arrows", {}), {}
    for rid, fields in reader.latest(spec["collection"], ref, date, ids).items():
        value = fields.get(spec["field"])
        out[rid] = {"date": fields[date], "value": value, "label": labels.get(value) if value else None,
                    "arrow": arrows.get(value) if value else None}
    return out


def _agenda(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """The records whose `date` falls from today to the last of `days`, the soonest first; each row's `days` below
    zero, today's zero."""
    date, fields = view["date"], _shown(form, view)
    _field(form, date, "date")
    shown = fields + [date] if fields and date not in fields else fields  # a row always says its day
    last = when.today + (view.get("days", AGENDA_DAYS) - 1) * DAY
    span = [{"field": date, "op": "between", "value": [when.today.isoformat(), last.isoformat()]}]
    records, total = _records(reader, collection, _conditions(form, view) + span)
    refs, rows, dropped = _refs(reader, form, records, fields), [], total - len(records)
    for record in sorted(records, key=lambda r: (r["fields"][date], r["id"])):
        row = _row(record, shown, refs, dt.date.fromisoformat(record["fields"][date]), None, when, None, budget)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"date": date, "fields": fields or list(form["fields"]), "first": when.today.isoformat(),
            "last": last.isoformat(), "count": total, "rows": rows, "dropped": dropped}


def _chart(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
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
            counts = {c["value"]: c["count"] for c in reader.count(collection, conditions + span, group)}
        else:
            counts = {None: reader.count(collection, conditions + span, None)}
        counted.append({"first": first.isoformat(), "counts": [{"value": v, "count": n} for v, n in counts.items()]})
    return {"date": date, "group": group, "values": values, "weeks": counted}


def _summed(form: dict, view: dict) -> tuple[str | None, str | None]:
    """What a `months` or a `year` view adds up: its `sum` — a whole number or an amount of money — and the unit it
    is in (the money's currency); none, the records counted."""
    name = view.get("sum")
    if name is None:
        return None, None
    spec = _field(form, name, "int", "money")
    return name, spec.get("currency")


def _sums(reader: Reader, collection: str, conditions: list, date: str, total: str | None,
          group: str | None, first: dt.date, last: dt.date) -> dict[tuple[str, str | None], decimal.Decimal]:
    """The records the filter matches whose `date` falls from `first` to `last`, added up by their month
    (`YYYY-MM`) and `group`'s value: their `total` field — exactly, an amount in cents — or one each."""
    span = [{"field": date, "op": "between", "value": [first.isoformat(), last.isoformat()]}]
    return reader.sums(collection, conditions + span, date, total, group)


def _said_sum(amount: decimal.Decimal, unit: str | None) -> str | int:
    """A sum as the answer gives it: an amount of money as its exact string («1250.00»), a count or a whole number
    as a number."""
    return f"{amount:.2f}" if unit else int(amount)


def _month(day: dt.date, back: int) -> dt.date:
    """The first day of the month `back` months before the one `day` is in."""
    n = day.year * 12 + day.month - 1 - back
    return dt.date(n // 12, n % 12 + 1, 1)


def _months(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """Sums by month: the records the filter matches added up by the month their `date` falls in — their `sum`
    field, else one each — the last `months` months, this one last, each split by the enum `group` names."""
    date, group = view["date"], view.get("group")
    _field(form, date, "date")
    total, unit = _summed(form, view)
    values = _field(form, group, "enum")["values"] if group else []
    count = view.get("months", MONTHS)
    firsts = [_month(when.today, n) for n in range(count - 1, -1, -1)]
    last = _month(when.today, -1) - DAY
    sums = _sums(reader, collection, _conditions(form, view), date, total, group, firsts[0], last)
    months = []
    for first in firsts:
        key = first.isoformat()[:7]
        parts = [(value, amount) for (month, value), amount in sums.items() if month == key]
        months.append({"first": first.isoformat(), "totals": [
            {"value": value, "total": _said_sum(amount, unit)}
            for value, amount in sorted(parts, key=lambda p: (p[0] is None, values.index(p[0]) if p[0] in values
                                                               else len(values), str(p[0])))]})
    return {"date": date, "sum": total, "unit": unit, "group": group, "values": values, "months": months}


def _year(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """A year's line: the records the filter matches added up month by month through this year — their `sum`, else
    one each — each month with the running total to its end, beside the same line of the year before; `through`
    the month that holds today."""
    date = view["date"]
    _field(form, date, "date")
    total, unit = _summed(form, view)
    conditions = _conditions(form, view)

    def line(year: int) -> list[dict]:
        sums = _sums(reader, collection, conditions, date, total, None, dt.date(year, 1, 1), dt.date(year, 12, 31))
        running, out = decimal.Decimal(0), []
        for month in range(1, 13):
            amount = sums.get((f"{year:04d}-{month:02d}", None), decimal.Decimal(0))
            running += amount
            out.append({"first": dt.date(year, month, 1).isoformat(), "total": _said_sum(amount, unit),
                        "running": _said_sum(running, unit)})
        return out
    year = when.today.year
    return {"date": date, "sum": total, "unit": unit, "year": year, "through": when.today.month, "months": line(year),
            "previous": line(year - 1)}


KINDS = {"stat": _stat, "board": _board, "queue": _queue, "agenda": _agenda, "chart": _chart, "months": _months,
         "year": _year}


# --- a project's own views, in code ----------------------------------------------------------------------------------

def code_csp(face_origin: str) -> str:
    """The policy a project's view in code is served under: the pages service's sandbox with scripts on
    (`pagesd.SANDBOX`) — an opaque origin, so no cookie, no API and no storage of the app's — its own inline scripts
    and styles and `data:` images and fonts, no network at all (`connect-src 'none'`), no frames, no forms sent, no
    navigation of its own; framed by the app alone. Its data comes from the app, by message."""
    return ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
            "font-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; manifest-src 'none'; "
            "media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {face_origin or NO_FRAME}; sandbox {pagesd.SANDBOX[True]}")


def _code_views(reader: Reader, space: str) -> dict[str, list[dict]]:
    """The views in code of the project `space` names, by their collection: as its description keeps them."""
    found = reader.project(space)
    out: dict[str, list[dict]] = {}
    for view in found["description"].get("views", []) if found else []:
        out.setdefault(view["collection"], []).append(view)
    return out


def _code(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    """A view in code: where its page is, and the data the app gives it by message — the collection's fields and
    their words, and its records (the oldest RECORDS_MAX the answer can hold), each with its ref fields in words."""
    records, total = _records(reader, collection, [])
    refs, rows, dropped = _refs(reader, form, records, None), [], total - len(records)
    for record in records:
        row = _row(record, None, refs, None, None, when, None, budget)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"src": f"/api/c/{collection}/_code/{view['name']}", "sandbox": pagesd.SANDBOX[True],
            "fields": shown_fields(form), "labels": _labels(form), "count": total, "rows": rows, "dropped": dropped}


def _done(reader: Reader, form: dict, view: dict) -> dict | None:
    """A queue's press on a row, its field found in the form and its value one the field takes."""
    if "done" not in view:
        return None
    done = view["done"]
    value = reader.probe(done["field"], _field(form, done["field"]), done["value"])
    return {"field": done["field"], "value": value, "label": done["label"]}


def _add(reader: Reader, form: dict, view: dict) -> dict | None:
    """A view's «add»: the fields its sheet asks for — the view's, else every one of the form — and those it starts
    with, each found in the form and of its type."""
    if "add" not in view:
        return None
    add = view["add"]
    fields = add.get("fields") or list(form["fields"])
    for name in fields:
        _field(form, name)
    values = {name: reader.probe(name, _field(form, name), value) for name, value in add.get("values", {}).items()}
    return {"label": add["label"], "fields": fields, "values": values}


def _view(reader: Reader, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    shown = {"name": view["name"], "kind": view["kind"], "title": view.get("title", view["name"]),
             **({"tab": view["tab"]} if "tab" in view else {})}
    try:
        shown.update({**KINDS, "code": _code}[view["kind"]](reader, collection, form, view, when, budget))
        if view["kind"] == "queue":
            shown["done"] = _done(reader, form, view)
        if view["kind"] in ("board", "queue"):
            shown["add"] = _add(reader, form, view)
    except Rejected as exc:  # a field the form lacks, a value its enum does not have: this view only
        shown = {k: shown[k] for k in ("name", "kind", "title", "tab") if k in shown} | {"error": exc.reason}
    return shown


def space_views(reader: Reader, space: str, when: When) -> dict:
    """A Space's collections, each with its rev and its views: `{space, tz, today, at, collections: [{name, rev,
    views}]}` — the stats of every collection first, then the rest, within one VIEW_MAX."""
    budget, made, code = [ROWS_MAX], {}, _code_views(reader, space)
    schemas = [(s["name"], s["form"], s["views"] + code.get(s["name"], [])) for s in reader.schemas(space)]
    for first in (True, False):  # every number made before any rows: a long board never crowds a stat out
        for name, form, views in schemas:
            for n, view in enumerate(views):
                if (view["kind"] in ("stat", "months", "year")) == first:
                    made[name, n] = _view(reader, name, form, view, when, budget)
    return {"space": space, **when.shown(), "collections": [
        {"name": name, "rev": reader.rev(name), "labels": _labels(form),
         "views": [made[name, n] for n in range(len(views))]}
        for name, form, views in schemas]}


def _labels(form: dict) -> dict[str, dict[str, str]]:
    """Every enum of the form, with the words it gives its values — `{"stage": {"pack_ready": "Ready to send"},
    "origin": {}}` — so the screen puts each value in words, one without them as itself made readable."""
    return {name: dict(spec.get("labels", {})) for name, spec in form["fields"].items() if spec["type"] == "enum"}


def one_view(reader: Reader, collection: str, name: str, when: When) -> dict | None:
    """One view of a collection: `{name, rev, tz, today, at, view}`, or None when there is no such view."""
    schema = reader.schema(collection)
    views = schema["views"] + _code_views(reader, schema["space"]).get(collection, []) if schema else []
    view = next((v for v in views if v["name"] == name), None)
    if view is None:
        return None
    return {"name": collection, "rev": reader.rev(collection), **when.shown(),
            "view": _view(reader, collection, schema["form"], view, when, [ROWS_MAX])}


def stats(reader: Reader, named: list[tuple[str, str]], when: When) -> dict:
    """The numbers the Router names, `(collection, view)` each, as it says them: `{tz, today, at, stats: [{collection,
    name, title, value, goal, previous, window, first, last}]}` in its order — `error` for one, and «no such stat»
    for a name that is not one of a collection's `stat` views."""
    forms: dict = {}
    said = []
    for collection, name in named:
        if collection not in forms:
            schema = reader.schema(collection)
            forms[collection] = (schema["form"], schema["views"]) if schema else (None, [])
        form, views = forms[collection]
        view = next((v for v in views if v["name"] == name and v["kind"] == "stat"), None)
        said.append({"collection": collection, **(_view(reader, collection, form, view, when, [0]) if view else
                                                  {"name": name, "kind": "stat", "title": name, "error": NO_STAT})})
    return {**when.shown(), "stats": said}


def record_view(reader: Reader, collection: str, rid: str) -> dict | None:
    """One record, a deleted one included, with its newest HISTORY changes: `{name, rev, title, fields, record,
    changes, words}` — its form's fields to show it by and `words` for the ids its refs name, now and in the changes —
    or None."""
    record, form = reader.record(collection, rid), reader.form(collection)
    if record is None or form is None:
        return None
    changes = reader.history(collection, rid, HISTORY)
    values = [record["fields"], *(c["before"] for c in changes), *(c["after"] for c in changes)]
    return {"name": collection, "rev": reader.rev(collection), "title": reader.title(form, record["fields"]),
            "fields": shown_fields(form), "record": record, "changes": changes,
            "words": reader.ref_words(form, values), "related": _related(reader, collection, rid)}


def _related(reader: Reader, collection: str, rid: str) -> list[dict]:
    """The records of the Space's other collections whose ref names this one — the touches of a deal, its rounds —
    each collection's newest RELATED_MAX by their first date field: `[{name, fields, date, records}]`."""
    schema = reader.schema(collection)
    related = []
    for other in reader.schemas(schema["space"]) if schema else []:
        name, form = other["name"], other["form"]
        refs = [f for f, spec in form["fields"].items() if spec["type"] == "ref" and spec["to"] == collection]
        if not refs:
            continue
        date = next((f for f, spec in form["fields"].items() if spec["type"] == "date"), None)
        records = reader.records(name, [{"field": refs[0], "op": "ref_is", "value": rid}], limit=RELATED_MAX, by=date,
                                 newest=True)
        if records:
            related.append({"name": name, "fields": shown_fields(form), "date": date, "ref": refs[0],
                            "records": records})
    return related


def form_view(reader: Reader, collection: str) -> dict | None:
    """What a sheet that adds a record asks for: `{name, rev, fields, options}` — the form's fields, and for each ref
    field the records it may name, `[{id, title}]`, the OPTIONS_MAX newest changed of those not deleted. None for no
    such collection."""
    form = reader.form(collection)
    if form is None:
        return None
    options = {}
    for name, spec in form["fields"].items():
        if spec["type"] != "ref":
            continue
        target = reader.form(spec["to"])
        rows = reader.records(spec["to"], limit=OPTIONS_MAX, by=UPDATED, newest=True) if target else []
        options[name] = [{"id": r["id"], "title": reader.title(target, r["fields"]) or r["id"]} for r in rows]
    return {"name": collection, "rev": reader.rev(collection), "fields": shown_fields(form), "options": options}


def space_changes(reader: Reader, space: str, before: str | None) -> dict:
    """The newest CHANGES_SHOWN changes to the records of a Space's collections, before the change `before` when one
    is named (`Reader.space_changes`): `{space, changes, next, words}`."""
    return {"space": space, **reader.space_changes(space, before, CHANGES_SHOWN)}


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
                web.get(f"/api/c/{{collection:{COLLECTION}}}/_code/{{view:{COLLECTION}}}", self.code),
                web.get(f"/api/c/{{collection:{COLLECTION}}}/{{id:{RECORD_ID}}}", self.record)]

    def owner_routes(self) -> list:
        return [web.post("/owner/zone", self.owner_zone), web.post("/owner/collections/stat", self.owner_stat)]

    async def _answer(self, work, *args) -> web.Response:
        store = self.platform.collections
        try:
            data = await store.read(lambda reader: work(reader, *args))
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

    async def code(self, request: web.Request) -> web.Response:
        """A project's view in code, for the app's frame: its page, under the pages service's sandbox with scripts on
        and no network (`code_csp`). It names no record: the app hands it the view's data by message."""
        collection, name = request.match_info["collection"], request.match_info["view"]
        store = self.platform.collections

        def page(reader: Reader) -> str | None:
            schema = reader.schema(collection)
            found = _code_views(reader, schema["space"]).get(collection, []) if schema else []
            return next((v["html"] for v in found if v["name"] == name), None)
        try:
            text = await store.read(page)
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if text is None:
            return web.json_response({"error": "No such view."}, status=404)
        return web.Response(text=text, content_type="text/html", charset="utf-8", headers={
            "Content-Security-Policy": code_csp(self.platform.cfg.face_origin), "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin"})

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
        return await self._answer(lambda reader, *args: {**stats(reader, *args), "screen_tz": screen}, pairs,
                                  self._when(tz))


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
