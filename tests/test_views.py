"""Views of the collections: a stat counts the follow-ups done on outbound deals apart from the inbound deals that
moved, in one time zone for the screen and the Router alike; a board's columns and a queue's rows carry their days
and turn rotten past the form's threshold; a view whose field the form lacks says so and the others still show; an
answer stays within 256 KB; every write — the platform's own or the server's terminal's — reaches the open apps."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

import pytest

from retinue import views
from retinue.collections import Collections, Rejected, Writer, parse_views
from retinue.views import When
from retinue.face import COOKIE
from test_face import POST, faced
from test_platformd import ROUTER, scenario, short_wait  # noqa: F401 — the fixture is autouse

OWNER = Writer("owner", "screen")
DEFINE = Writer("owner", "define")
NOW = dt.datetime(2026, 9, 16, 12, tzinfo=dt.UTC).timestamp()  # a Wednesday: its week is 14–20 September
DAY = 86400
DEALS = {
    "fields": {
        "company": {"type": "text", "required": True},
        "product": {"type": "text"},
        "origin": {"type": "enum", "values": ["inbound", "outbound"]},
        "stage": {"type": "enum", "values": ["lead", "qualified", "proposal", "won", "lost"]},
        "close_reason": {"type": "text"},
    },
    "rot": {"stuck": 7},
}
FOLLOWUPS = {
    "fields": {
        "deal": {"type": "ref", "to": "deals"},
        "direction": {"type": "enum", "values": ["in", "out"]},
        "state": {"type": "enum", "values": ["draft", "done", "unknown"]},
        "date": {"type": "date"},
        "channel": {"type": "text"},
    },
    "goals": {"weekly": 10},
    "rot": {"reply": 2},
}
DEAL_VIEWS = [
    {"name": "moved", "kind": "stat", "title": "Inbound moved", "count": "changed", "field": "stage",
     "filter": {"field": "origin", "op": "eq", "value": "inbound"}, "window": "week"},
    {"name": "funnel", "kind": "board", "title": "Funnel", "group": "stage", "folded": ["lost"],
     "fields": ["company", "product", "close_reason"], "rot": "stuck"},
]
FOLLOWUP_VIEWS = [
    {"name": "done", "kind": "stat", "title": "Follow-ups done", "date": "date", "window": "week", "goal": "weekly",
     "filter": [{"field": "direction", "op": "eq", "value": "out"}, {"field": "state", "op": "eq", "value": "done"},
                {"field": "deal.origin", "op": "eq", "value": "outbound"}]},
    {"name": "waiting", "kind": "queue", "title": "Waiting on me", "since": "date", "rot": "reply",
     "filter": {"field": "state", "op": "eq", "value": "draft"}, "fields": ["deal", "channel"]},
]


def run(body):
    return asyncio.run(body())


async def work(store: Collections) -> None:
    await store.define(DEFINE, "deals", "work", DEALS, NOW - 30 * DAY, views=DEAL_VIEWS)
    await store.define(DEFINE, "followups", "work", FOLLOWUPS, NOW - 30 * DAY, views=FOLLOWUP_VIEWS)


async def add(store: Collections, collection: str, fields: dict, now: float, **extra) -> dict:
    return (await store.apply(OWNER, collection, [{"op": "add", "fields": fields, **extra}], now))["records"][0]


async def stage(store: Collections, record: dict, value: str, now: float, **extra) -> dict:
    op = {"op": "set", "id": record["id"], "version": record["version"], "fields": {"stage": value}, **extra}
    return (await store.apply(OWNER, "deals", [op], now))["records"][0]


async def shown(store: Collections, tz: str = "UTC", now: float = NOW) -> dict:
    """The Space's views by collection and name, as its screen gets them."""
    answer = await store.read(lambda: views.space_views(store, "work", When(tz, now)))
    return {c["name"]: {v["name"]: v for v in c["views"]} for c in answer["collections"]}


def test_follow_ups_on_inbound_deals_count_none_of_the_outbound_ones_done_and_each_inbound_one_that_moved(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        own = await add(store, "deals", {"company": "Acme", "origin": "outbound", "stage": "qualified"}, NOW - 9 * DAY,
                        as_of="2026-09-07")
        await add(store, "followups", {"deal": own["id"], "direction": "out", "state": "done", "date": "2026-09-07"},
                  NOW - 9 * DAY)  # last week's, on an outbound deal
        for company in ("Example Ltd", "Initech", "Globex"):  # three inbound deals, followed up this week
            deal = await add(store, "deals", {"company": company, "origin": "inbound", "stage": "lead"},
                             NOW - 20 * DAY, as_of="2026-08-27")
            await stage(store, deal, "proposal", NOW - DAY)
            await add(store, "followups", {"deal": deal["id"], "direction": "out", "state": "done", "date": "2026-09-15"},
                      NOW - DAY)
        got = await shown(store)
        done, moved = got["followups"]["done"], got["deals"]["moved"]
        assert done == {"name": "done", "kind": "stat", "title": "Follow-ups done", "value": 0, "goal": 10, "previous": 1,
                        "window": "week", "first": "2026-09-14", "last": "2026-09-20"}, "an inbound deal's follow-up is not an outbound one"
        assert (moved["value"], moved["previous"], moved["goal"]) == (3, 0, None), "but each one moved"
        await store.close()

    run(body)


def test_a_change_counts_in_the_week_of_its_as_of_or_of_the_day_it_was_made_in_the_zone(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        sunday_night = dt.datetime(2026, 9, 13, 22, 30, tzinfo=dt.UTC).timestamp()  # Monday morning in Tokyo
        deal = await add(store, "deals", {"company": "Acme", "origin": "inbound"}, NOW - 20 * DAY)
        await stage(store, deal, "proposal", sunday_night)
        assert (await shown(store, "UTC"))["deals"]["moved"]["value"] == 0, "Sunday, last week, in UTC"
        assert (await shown(store, "Asia/Tokyo"))["deals"]["moved"]["value"] == 1, "Monday, this week, in Tokyo"
        dated = await add(store, "deals", {"company": "Initech", "origin": "inbound", "stage": "qualified"}, NOW,
                          as_of="2026-09-08")
        got = (await shown(store))["deals"]["moved"]
        assert got["value"] == 0 and got["previous"] == 2, "a change written today for the 8th counts on the 8th"
        await stage(store, dated, "won", NOW, as_of="2026-09-14")
        assert (await shown(store))["deals"]["moved"]["value"] == 1
        await store.close()

    run(body)


def test_a_queue_reads_only_so_many_records_and_counts_the_rest_and_names_no_deleted_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "RECORDS_MAX", 2)

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        deal = await add(store, "deals", {"company": "Acme"}, NOW)
        for day in ("2026-09-15", "2026-09-14", "2026-09-13"):
            await add(store, "followups", {"deal": deal["id"], "state": "draft", "date": day}, NOW)
        await store.apply(OWNER, "deals", [{"op": "delete", "id": deal["id"], "version": 1}], NOW)
        got = await shown(store)
        queue, board = got["followups"]["waiting"], got["deals"]["funnel"]
        assert (queue["count"], len(queue["rows"]), queue["dropped"]) == (3, 2, 1), "the oldest two, and one more"
        assert queue["rows"][0]["refs"] == {}, "a deleted record lends no words: the row shows its id"
        assert board["dropped"] == 0 and sum(c["count"] for c in board["columns"]) == 0
        await store.close()

    run(body)


def test_a_board_shows_the_days_in_a_column_and_turns_rotten_past_the_forms_threshold(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        old = await add(store, "deals", {"company": "Acme", "product": "Widgets", "stage": "lead"}, NOW - 20 * DAY,
                        src="web:https://shop.example.com/deals/1")
        await stage(store, old, "qualified", NOW - 10 * DAY)
        await add(store, "deals", {"company": "Example Ltd", "stage": "qualified", "close_reason": "-"}, NOW - 3 * DAY)
        await add(store, "deals", {"company": "Initech", "stage": "lost", "close_reason": "no reply"}, NOW,
                  as_of="2026-09-02")
        await add(store, "deals", {"company": "Globex"}, NOW)
        board = (await shown(store))["deals"]["funnel"]
        assert board["group"] == "stage" and board["rot"] == 7 and board["dropped"] == 0
        assert board["fields"] == ["company", "product", "close_reason"], "the view's fields, in its order"
        columns = {c["value"]: c for c in board["columns"]}
        assert [c["value"] for c in board["columns"]] == ["lead", "qualified", "proposal", "won", "lost",
                                                          None], "the enum's order, the unknown last"
        applied = columns["qualified"]["rows"]
        assert [(r["fields"]["company"], r["days"], r["rotten"]) for r in applied] == [
            ("Acme", 10, True), ("Example Ltd", 3, False)], "the longest in the column first"
        assert applied[0]["src"] == "web:https://shop.example.com/deals/1" and set(applied[0]["fields"]) == {
            "company", "product"}, "only the view's fields"
        assert columns["lost"]["folded"] and columns["lost"]["rows"][0]["days"] == 14, "by its as_of"
        assert columns["lost"]["rows"][0]["fields"]["close_reason"] == "no reply"
        assert columns[None]["count"] == 1 and columns[None]["rows"][0]["days"] is None, "no stage, no days"
        assert columns["lead"] == {"value": "lead", "count": 0, "folded": False, "rows": []}
        await store.close()

    run(body)


def test_a_queue_lists_the_longest_silent_first_and_turns_rotten_past_the_threshold(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        deal = await add(store, "deals", {"company": "Acme"}, NOW)
        for date, state in (("2026-09-15", "draft"), ("2026-09-11", "draft"), ("2026-09-10", "done")):
            await add(store, "followups", {"deal": deal["id"], "state": state, "date": date, "channel": "mail"}, NOW)
        await add(store, "followups", {"deal": deal["id"], "state": "draft"}, NOW)
        queue = (await shown(store))["followups"]["waiting"]
        assert queue["count"] == 3 and queue["rot"] == 2 and queue["dropped"] == 0
        assert [(r["days"], r["rotten"]) for r in queue["rows"]] == [(5, True), (1, False), (None, False)]
        assert set(queue["rows"][0]["fields"]) == {"deal", "channel"} and queue["fields"] == ["deal", "channel"]
        assert queue["rows"][0]["refs"] == {"deal": "Acme"}, "a ref by the words of the record it names, not its id"
        await store.close()

    run(body)


def test_a_view_whose_field_the_form_lacks_says_field_not_found_and_the_others_still_show(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        broken = [
            {"name": "phases", "kind": "board", "group": "phase"},
            {"name": "by_owner", "kind": "queue", "filter": {"field": "owner", "op": "empty"}},
            {"name": "shown", "kind": "queue", "fields": ["company", "salary"]},
            {"name": "dated", "kind": "stat", "date": "when", "window": "week"},
            {"name": "aim", "kind": "stat", "goal": "monthly"},
            {"name": "ok", "kind": "stat"},
            {"name": "stale", "kind": "board", "group": "stage", "folded": ["archived"]},
        ]
        await store.define(DEFINE, "deals", "work", DEALS, NOW, views=broken)
        await add(store, "deals", {"company": "Acme"}, NOW)
        got = (await shown(store))["deals"]
        assert {name: view.get("error") for name, view in got.items()} == {
            "phases": "field not found: 'phase'", "by_owner": "field not found: 'owner'",
            "shown": "field not found: 'salary'", "dated": "field not found: 'when'",
            "aim": "goal not found: 'monthly'", "ok": None, "stale": "'stage' has no value 'archived'"}
        assert got["ok"]["value"] == 1 and "rows" not in got["phases"]
        await store.close()

    run(body)


def test_views_are_checked_strictly_in_their_shape(tmp_path):
    for given, reason in [
        ({}, "views are a list"),
        ([{"name": "x", "kind": "table"}], '"kind" is one of stat, board, queue'),
        ([{"name": "x", "kind": "queue", "colour": "red"}], "not 'colour'"),
        ([{"name": "X", "kind": "queue"}], '"name" is lowercase'),
        ([{"name": "x", "kind": "queue"}, {"name": "x", "kind": "stat"}], "another view's"),
        ([{"name": "x", "kind": "board"}], '"group" names the enum'),
        ([{"name": "x", "kind": "board", "group": "a b"}], '"group" names a field'),
        ([{"name": "x", "kind": "stat", "count": "sum"}], '"count" is records or changed'),
        ([{"name": "x", "kind": "stat", "window": "year"}], '"window" is one of week, month'),
        ([{"name": "x", "kind": "stat", "window": "week"}], 'names the "date" field'),
        ([{"name": "x", "kind": "stat", "count": "changed"}], 'names the "field"'),
        ([{"name": "x", "kind": "stat", "field": "stage"}], "a count of changed records'"),
        ([{"name": "x", "kind": "queue", "title": "a‮b"}], '"title" is plain text'),
        ([{"name": "x", "kind": "queue", "fields": ["a", "a"]}], '"fields" is a list'),
        ([{"name": "x", "kind": "queue", "filter": "stage = 1"}], '"filter" is a condition'),
    ]:
        with pytest.raises(Rejected, match=re.escape(reason)):
            parse_views(given)
    assert parse_views(DEAL_VIEWS) == DEAL_VIEWS

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(DEFINE, "deals", "work", DEALS, NOW)
        made = await store.define(DEFINE, "deals", "work", DEALS, NOW, views=DEAL_VIEWS)
        assert made["form_rev"] == 2 and made["views_before"] == [] and made["views_after"] == DEAL_VIEWS
        kept = await store.define(DEFINE, "deals", "work", DEALS, NOW)
        assert kept["unchanged"], "no views given: the ones it has stay"
        changes = await store.read(lambda: store._db.execute(
            "SELECT before, after FROM changes WHERE op = 'define' ORDER BY seq").fetchall())
        assert json.loads(changes[-1][0]) == {"form": DEALS, "views": []}
        assert json.loads(changes[-1][1]) == {"form": DEALS, "views": DEAL_VIEWS}
        await store.close()

    run(body)


def test_an_answer_stays_within_256_kb_its_numbers_first(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        for start in range(0, 400, 200):
            await store.apply(OWNER, "deals", [{"op": "add", "fields": {
                "company": f"Acme {n}", "product": "x" * 2000, "stage": "qualified", "origin": "inbound"}}
                for n in range(start, start + 200)], NOW)
        answer = await store.read(lambda: views.space_views(store, "work", When("UTC", NOW)))
        size = len(json.dumps(answer, ensure_ascii=False).encode())
        board = answer["collections"][0]["views"][1]
        assert size <= views.VIEW_MAX, size
        assert board["dropped"] > 0 and board["columns"][1]["count"] == 400
        assert len(board["columns"][1]["rows"]) + board["dropped"] == 400
        assert answer["collections"][0]["views"][0]["value"] == 400, "the numbers are whole"
        one = await store.read(lambda: views.one_view(store, "deals", "funnel", When("UTC", NOW)))
        assert len(json.dumps(one, ensure_ascii=False).encode()) <= views.VIEW_MAX
        await store.close()

    run(body)


def test_zones_are_iana_names_only():
    assert views.zone("Europe/Berlin") is not None and views.zone("UTC") is not None
    for bad in ("", "Mars/Olympus", "../etc/passwd", "/etc/localtime", "Europe", None, 3, "x" * 100):
        assert views.zone(bad) is None, bad
    assert views.window("week", dt.date(2026, 9, 20)) == (dt.date(2026, 9, 14), dt.date(2026, 9, 20))
    assert views.window("month", dt.date(2026, 2, 10)) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))


# --- through the platform --------------------------------------------------------------------------------------------

def test_the_screen_and_the_router_give_one_number_in_one_zone(tmp_path):
    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        deal = await add(store, "deals", {"company": "Acme", "origin": "inbound"}, NOW - 20 * DAY)
        await stage(store, deal, "proposal", dt.datetime(2026, 9, 13, 22, 30, tzinfo=dt.UTC).timestamp())
        await world.login()

        async def screen() -> dict:
            got = await world.get("/api/c/_space?space=work")
            return {v["name"]: v for c in got["collections"] for v in c["views"]} | {"_": got}

        got = await screen()
        assert got["_"]["tz"] == "UTC" and got["_"]["today"] == "2026-09-16", "the config's zone until told one"
        assert got["moved"]["value"] == 0
        assert (await world.post("/owner/zone", {"tz": "Mars/Olympus"}))[0] == 400
        assert (await world.post("/owner/zone", {"tz": "Asia/Tokyo"}, headers={}))[0] == 401
        assert await world.owner("zone", {"tz": "Asia/Tokyo"}) == {"tz": "Asia/Tokyo"}
        got = await screen()
        assert got["_"]["tz"] == "Asia/Tokyo" and got["moved"]["value"] == 1
        for tz, value in (("Asia/Tokyo", 1), ("UTC", 0)):
            said = await world.owner("collections/stat", {"space": "work", "tz": tz})
            assert said["tz"] == tz and said["today"] == "2026-09-16"
            numbers = {s["name"]: s for s in said["stats"]}
            assert set(numbers) == {"moved", "done"} and numbers["moved"]["value"] == value
            assert numbers["done"]["collection"] == "followups" and numbers["done"]["goal"] == 10
            if tz == "Asia/Tokyo":
                assert numbers["moved"] == {"collection": "deals", **got["moved"]}, "the same tile, word for word"
        assert (await world.post("/owner/collections/stat", {"space": "work"}))[0] == 400, "the Router names it"
        assert (await world.owner("collections/stat", {"space": "travel", "tz": "UTC"}))["stats"] == []

    faced(tmp_path, body)


def test_a_view_and_a_record_are_read_by_the_owner_alone(tmp_path):
    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        deal = await add(store, "deals", {"company": "Acme", "stage": "lead"}, NOW)
        deal = await stage(store, deal, "qualified", NOW)
        assert (await world.call("GET", "/api/c/deals?view=funnel"))[0] == 401
        await world.login()
        got = await world.get("/api/c/deals?view=funnel")
        assert got["name"] == "deals" and got["view"]["kind"] == "board" and got["rev"] is not None
        assert got["rev"] == (await store.read(lambda: store._rev("deals")))
        assert (await world.call("GET", "/api/c/deals?view=nothing"))[0] == 404
        assert (await world.call("GET", "/api/c/nothing?view=funnel"))[0] == 404
        assert (await world.call("GET", "/api/c/_space?space=..%2Fx"))[0] == 400
        record = await world.get(f"/api/c/deals/{deal['id']}")
        assert record["record"] == deal and [c["op"] for c in record["changes"]] == ["set", "add"]
        assert (await world.call("GET", f"/api/c/deals/{'0' * 26}"))[0] == 404
        cookie = {"Cookie": f"{COOKIE}={world.cookie}"}
        assert (await world.api.get("/api/c/deals/..%2F..%2Fx", headers=cookie)).status == 404
        assert (await world.call("GET", "/api/c/_files"))[0] == 200, "the uploads keep their own route"
        assert (await world.api.post("/api/c/deals", headers={**POST, **cookie})).status == 405, "reading, for now"

    faced(tmp_path, body)


def test_every_write_reaches_the_open_apps_as_one_collection_event(tmp_path):
    async def body(world):
        store = world.platform.collections
        before = world.store.cursor()
        await work(store)
        deal = await add(store, "deals", {"company": "Acme"}, NOW)
        same = await store.apply(OWNER, "deals", [{"op": "set", "id": deal["id"], "version": 1,
                                                   "fields": {"company": "Acme"}}], NOW)
        assert same["change_ids"] == [None]
        events = [(kind, json.loads(data)) for _, kind, data in world.store.events_after(before)]
        rev = await store.read(lambda: store._rev("deals"))
        assert [kind for kind, _ in events] == ["collection"] * 3, "two defines and the add; nothing for no change"
        assert events[-1][1] == {"name": "deals", "space": "work", "rev": rev} and rev == same["rev"]

        assert await store.moved() == [], "the first look only takes note"
        other = Collections(str(tmp_path / "collections.sqlite"))  # the server's terminal, from its own process
        await other.import_ops("deals", [{"op": "add", "fields": {"company": f"Initech {n}"}} for n in range(3)], NOW)
        await other.close()
        moved = await store.moved()
        assert moved == [("deals", "work", await store.read(lambda: store._rev("deals")))] and await store.moved() == []
        for name, space, rev in moved:
            world.store.collection_event(name, space, rev)  # what the platform's watch does with them
        assert json.loads(world.store.events_after(before)[-1][2])["rev"] == moved[0][2]
        await store.close()

    scenario(tmp_path, body)
