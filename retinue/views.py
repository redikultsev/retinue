"""Views: what a Space's screen shows of its collections, and the numbers the Router says of them
(docs/collections.md#views). Read only, computed on the collections' thread from each collection's `views`, in one
read transaction with the `rev` they are of — a screen that hears of a newer one reads again.

Three kinds, a closed set (`collections.VIEW_KEYS`):
- `stat`, one number: the records a filter matches, or those whose field changed, this week or this month — with its
  goal from the form and the same number for the period before;
- `board`, the records in columns by an enum, each with the days since that field last changed;
- `queue`, the records a filter matches, the longest silent first, with the days since.

A view that names a field its form lacks shows «field not found» in its place, and the others still show. Weeks and
days are counted in one time zone: the one the Router last told (`POST /owner/zone`), the config's `owner_tz` until it
has. The Router's own numbers (`POST /owner/collections/stat`) come in the zone it names, through the same code as the
screen's — so one zone gives one number in both.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from .collections import (HISTORY, NAME, RECORD_COLUMNS, SPACE, Collections, Refused, Rejected, _json, _path,
                          _record)

if TYPE_CHECKING:
    from .platformd import Platform

VIEW_MAX = 256 * 2**10      # bytes of one answer: a view, or a Space's views
ROWS_MAX = VIEW_MAX - 16 * 2**10  # of them the rows' share; the rest is the views' own words
RECORDS_MAX = 2000          # records one board or queue reads, the oldest first: more than its rows can ever hold
IDS_CHUNK = 500             # ids in one statement's IN (…)
ZONE_KEY = "collections.zone"  # the platform's kv: the zone the Router last told
NOT_FOUND = "field not found"
COLLECTION = "[a-z][a-z0-9_]{0,39}"  # as `collections.NAME`: never a reserved `_files` or `_space`
RECORD_ID = "[0-9A-HJKMNP-TV-Z]{26}"
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
    some) within `span`: a change dated by its `as_of`, else by the day it was made, here."""
    where, params = store._where(collection, form, conditions or None, False)
    sql = [f"SELECT count(DISTINCT id) FROM changes WHERE collection = ? AND {_after(field)} IS NOT NULL",
           f"AND id IN (SELECT id FROM records WHERE {where})"]
    values: list[Any] = [collection, *params]
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


def _refs(store: Collections, form: dict, records: list[dict], fields: list[str] | None) -> dict:
    """For each record, the `ref` fields a view shows by the words of what they name: the first `text` field of the
    form it is in, as that record has it now — so a row says «Acme», not an id. A deleted record, or one without that
    field: none, and the row shows the id."""
    words: dict = {}
    for name in fields or form["fields"]:
        spec = form["fields"][name]
        ids = sorted({r["fields"][name] for r in records if spec["type"] == "ref" and name in r["fields"]})
        target = store._db.execute("SELECT form FROM schemas WHERE collection = ?",
                                   (spec.get("to"),)).fetchone() if ids else None
        text = next((k for k, s in json.loads(target[0])["fields"].items() if s["type"] == "text"),
                    None) if target else None
        if text is None:
            continue
        said = {}
        for at in range(0, len(ids), IDS_CHUNK):
            chunk = ids[at:at + IDS_CHUNK]
            said.update(store._db.execute(
                f"SELECT id, {_path(text)} FROM records WHERE collection = ? AND id IN ({', '.join('?' * len(chunk))})"
                f" AND deleted IS NULL AND {_path(text)} IS NOT NULL", (spec["to"], *chunk)).fetchall())
        for record in records:
            if record["fields"].get(name) in said:
                words.setdefault(record["id"], {})[name] = said[record["fields"][name]]
    return words


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


def _queue(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    fields, rot, since = _shown(form, view), _rot(form, view), view.get("since")
    if since is not None:
        _field(form, since, "date")
    records, total = _records(store, collection, form, _conditions(form, view))
    days, srcs = _marks(store, collection, [r["id"] for r in records], None, when)
    if since is not None:
        days = {r["id"]: dt.date.fromisoformat(r["fields"][since]) for r in records if since in r["fields"]}
    rows, dropped, refs = [], total - len(records), _refs(store, form, records, fields)
    for record in sorted(records, key=_longest(days)):
        row = _row(record, fields, refs, days.get(record["id"]), srcs.get(record["id"]), when, rot, budget)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return {"rot": rot, "fields": fields or list(form["fields"]), "count": total, "rows": rows,
            "dropped": dropped}


KINDS = {"stat": _stat, "board": _board, "queue": _queue}


def _view(store: Collections, collection: str, form: dict, view: dict, when: When, budget: list[int]) -> dict:
    shown = {"name": view["name"], "kind": view["kind"], "title": view.get("title", view["name"])}
    try:
        shown.update(KINDS[view["kind"]](store, collection, form, view, when, budget))
    except Rejected as exc:  # a field the form lacks, a value its enum does not have: this view only
        shown["error"] = exc.reason
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
        {"name": name, "rev": store._rev(name), "views": [made[name, n] for n in range(len(views))]}
        for name, _, views in schemas]}


def one_view(store: Collections, collection: str, name: str, when: When) -> dict | None:
    """One view of a collection: `{name, rev, tz, today, at, view}`, or None when there is no such view."""
    row = store._db.execute("SELECT form, views FROM schemas WHERE collection = ?", (collection,)).fetchone()
    view = next((v for v in json.loads(row[1]) if v["name"] == name), None) if row else None
    if view is None:
        return None
    return {"name": collection, "rev": store._rev(collection), **when.shown(),
            "view": _view(store, collection, json.loads(row[0]), view, when, [ROWS_MAX])}


def stats(store: Collections, space: str, when: When) -> dict:
    """The numbers of a Space — its `stat` views, every collection's — as the Router says them: `{space, tz, today,
    at, stats: [{collection, name, title, value, goal, previous, window, first, last}]}`, or `error` for one."""
    return {"space": space, **when.shown(), "stats": [
        {"collection": name, **_view(store, name, form, view, when, [0])}
        for name, form, views in _schemas(store, space) for view in views if view["kind"] == "stat"]}


def record_view(store: Collections, collection: str, rid: str) -> dict | None:
    """One record, a deleted one included, with its newest HISTORY changes: `{name, rev, record, changes}`, or None."""
    record = store._record(collection, rid)
    if record is None:
        return None
    return {"name": collection, "rev": store._rev(collection), "record": record,
            "changes": store._history(collection, rid, HISTORY)}


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
        return [web.get("/api/c/_space", self.space),
                web.get(f"/api/c/{{collection:{COLLECTION}}}", self.view),
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

    async def owner_zone(self, request: web.Request) -> web.Response:
        """The Router's zone, at its start and whenever the owner's changes: the screen counts its weeks in it."""
        tz = (await _body(request)).get("tz")
        if zone(tz) is None:
            return web.json_response({"error": "tz: an IANA time zone, e.g. Europe/Berlin"}, status=400)
        self.platform.store.set(ZONE_KEY, tz)
        return web.json_response({"tz": tz})

    async def owner_stat(self, request: web.Request) -> web.Response:
        """A Space's numbers in the zone the Router names: the same as its screen's in that zone."""
        body = await _body(request)
        space, tz = body.get("space"), body.get("tz")
        if not isinstance(space, str) or not SPACE.fullmatch(space) or zone(tz) is None:
            return web.json_response({"error": "space: a Space's name; tz: an IANA time zone"}, status=400)
        return await self._answer(stats, space, self._when(tz))


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
