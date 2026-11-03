"""Views of the collections: a stat counts the follow-ups done on outbound deals apart from the inbound deals that
moved, in one time zone for the screen and the Router alike; a board's columns and a queue's rows carry their days
and turn rotten past the form's threshold; a view whose field the form lacks says so and the others still show; an
answer stays within 256 KB; every write — the platform's own or the server's terminal's — reaches the open apps. The
owner writes from the screen at the version it showed: one that moved on since is a conflict with who moved it, a
change is taken back from its record or the Space's changes, and a queue's press and a view's «add» are held to the
form."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

import pytest

from retinue import pagesd, project, views
from retinue.collections import Collections, Rejected, Writer, parse_views
from retinue.views import When
from retinue.face import COOKIE
from test_face import faced
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
    answer = await store.read(lambda reader: views.space_views(reader, "work", When(tz, now)))
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
        last = await store.read(lambda reader: reader.change(reader.rev("deals")))
        assert last["op"] == "define" and last["before"] == {"form": DEALS, "views": []}
        assert last["after"] == {"form": DEALS, "views": DEAL_VIEWS}
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
        answer = await store.read(lambda reader: views.space_views(reader, "work", When("UTC", NOW)))
        size = len(json.dumps(answer, ensure_ascii=False).encode())
        board = answer["collections"][0]["views"][1]
        assert size <= views.VIEW_MAX, size
        assert board["dropped"] > 0 and board["columns"][1]["count"] == 400
        assert len(board["columns"][1]["rows"]) + board["dropped"] == 400
        assert answer["collections"][0]["views"][0]["value"] == 400, "the numbers are whole"
        one = await store.read(lambda reader: views.one_view(reader, "deals", "funnel", When("UTC", NOW)))
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
            said = await world.owner("collections/stat", {"views": ["deals/moved", "followups/done"], "tz": tz})
            assert said["tz"] == tz and said["today"] == "2026-09-16"
            assert [s["name"] for s in said["stats"]] == ["moved", "done"], "in the Router's order"
            moved, done = said["stats"]
            assert moved["value"] == value and done["collection"] == "followups" and done["goal"] == 10
            if tz == "Asia/Tokyo":
                assert moved == {"collection": "deals", **got["moved"]}, "the same tile, word for word"
        said = await world.owner("collections/stat", {"views": ["deals/funnel", "deals/gone", "trips/moved"],
                                                      "tz": "UTC"})
        assert [s.get("error") for s in said["stats"]] == [views.NO_STAT] * 3, "a board, no view, no collection"
        for bad in ({"tz": "UTC"}, {"views": [], "tz": "UTC"}, {"views": ["deals"], "tz": "UTC"},
                    {"views": ["deals/moved/x"], "tz": "UTC"}, {"views": ["deals/moved"]},
                    {"views": ["deals/moved"] * 9, "tz": "UTC"}, {"views": "deals/moved", "tz": "UTC"}):
            assert (await world.post("/owner/collections/stat", bad))[0] == 400, bad

    faced(tmp_path, body)


def test_the_router_says_in_telegram_the_numbers_the_screen_shows(tmp_path):
    """Two numbers side by side — the follow-ups he sent himself on outbound deals, and the inbound deals that
    moved — each the screen's own tile in the Router's zone, never one sum. The Router tells the
    platform its zone; the screen and the Telegram line then say the same numbers."""
    from retinue.core import Core
    from retinue.proactive import Proactive
    from retinue.protocol import Store
    from test_collections_assistant import platform_of
    from test_core import AGENT, FakeChannel

    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        own = await add(store, "deals", {"company": "Acme", "origin": "outbound"}, NOW - 20 * DAY)
        theirs = await add(store, "deals", {"company": "Globex", "origin": "inbound"}, NOW - 20 * DAY)
        for n, day in enumerate(("2026-09-14", "2026-09-15", "2026-09-08")):
            await add(store, "followups", {"deal": own["id"], "direction": "out", "state": "done", "date": day},
                      NOW - n * DAY)
        for _ in range(3):  # his three replies on a deal that came to him: none a follow-up of his own
            await add(store, "followups", {"deal": theirs["id"], "direction": "out", "state": "done",
                                           "date": "2026-09-15"}, NOW)
        await stage(store, theirs, "qualified", dt.datetime(2026, 9, 13, 22, 30, tzinfo=dt.UTC).timestamp())
        core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", tz="Asia/Tokyo",
                    proactive=Proactive(week=["followups/done", "deals/moved"]))
        core.platform = platform_of(world)
        await core.start([FakeChannel("telegram", False)])
        await core.proactive.tell_zone()
        await world.login()
        screen = await world.get("/api/c/_space?space=work")
        tiles = {v["name"]: v for c in screen["collections"] for v in c["views"]}
        assert screen["tz"] == "Asia/Tokyo" and (tiles["done"]["value"], tiles["moved"]["value"]) == (2, 1), \
            "Monday 07:30 in Tokyo is this week there"
        line = await core.proactive.weekly_line(NOW)
        assert line == (f"This week: Follow-ups done — {tiles['done']['value']} (goal {tiles['done']['goal']}); "
                        f"Inbound moved — {tiles['moved']['value']}.")
        assert line == "This week: Follow-ups done — 2 (goal 10); Inbound moved — 1."
        assert await core.proactive.week_line(last=True) == (
            f"Last week: Follow-ups done — {tiles['done']['previous']} (goal 10); Inbound moved — "
            f"{tiles['moved']['previous']}.") == "Last week: Follow-ups done — 1 (goal 10); Inbound moved — 0."
        core.tz = "UTC"  # the owner moved: the next beat tells the platform, and both count in UTC
        await core.proactive.tell_zone()
        screen = await world.get("/api/c/_space?space=work")
        moved = next(v for c in screen["collections"] for v in c["views"] if v["name"] == "moved")
        assert screen["tz"] == "UTC" and moved["value"] == 0
        assert await core.proactive.weekly_line(NOW) == "This week: Follow-ups done — 2 (goal 10); Inbound moved — 0."

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
        assert got["rev"] == (await store.read(lambda reader: reader.rev("deals")))
        assert (await world.call("GET", "/api/c/deals?view=nothing"))[0] == 404
        assert (await world.call("GET", "/api/c/nothing?view=funnel"))[0] == 404
        assert (await world.call("GET", "/api/c/_space?space=..%2Fx"))[0] == 400
        record = await world.get(f"/api/c/deals/{deal['id']}")
        assert record["record"] == deal and [c["op"] for c in record["changes"]] == ["set", "add"]
        assert (await world.call("GET", f"/api/c/deals/{'0' * 26}"))[0] == 404
        cookie = {"Cookie": f"{COOKIE}={world.cookie}"}
        assert (await world.api.get("/api/c/deals/..%2F..%2Fx", headers=cookie)).status == 404
        assert (await world.call("GET", "/api/c/_files"))[0] == 200, "the uploads keep their own route"

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
        rev = await store.read(lambda reader: reader.rev("deals"))
        assert [kind for kind, _ in events] == ["collection"] * 3, "two defines and the add; nothing for no change"
        assert events[-1][1] == {"name": "deals", "space": "work", "rev": rev} and rev == same["rev"]

        assert await store.moved() == [], "the first look only takes note"
        other = Collections(str(tmp_path / "collections.sqlite"))  # the server's terminal, from its own process
        await other.import_ops("deals", [{"op": "add", "fields": {"company": f"Initech {n}"}} for n in range(3)], NOW)
        await other.close()
        moved = await store.moved()
        assert moved == [("deals", "work", await store.read(lambda reader: reader.rev("deals")))] and await store.moved() == []
        for name, space, rev in moved:
            world.store.collection_event(name, space, rev)  # what the platform's watch does with them
        assert json.loads(world.store.events_after(before)[-1][2])["rev"] == moved[0][2]
        await store.close()

    scenario(tmp_path, body)


# --- the screen writes -----------------------------------------------------------------------------------------------

WRITING = [
    {"name": "waiting", "kind": "queue", "title": "Waiting on me", "fields": ["deal", "channel"],
     "filter": {"field": "state", "op": "eq", "value": "draft"},
     "done": {"field": "state", "value": "done", "label": "Done"},
     "add": {"label": "Add a follow-up", "fields": ["deal", "date", "state", "channel"], "values": {"state": "draft"}}},
]


async def written(world, collection: str, *ops, idem: str | None = None, **call):
    """The owner's write from the screen: its status and its answer."""
    status, data, _ = await world.call("POST", f"/api/c/{collection}", {"ops": list(ops), "idem": idem}, **call)
    return status, data


def test_the_owner_writes_from_the_screen_at_the_version_it_showed_and_a_moved_record_is_a_conflict(tmp_path):
    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        await store.rights(DEFINE, "followups", {"writers": {"session": {"add": True, "set": "*"}},
                                                 "set_by": {"state": {"done": ["owner", "code:mail-sent"]}}}, NOW)
        add_op = {"op": "add", "fields": {"company": "Acme", "stage": "lead"}}
        assert (await written(world, "deals", add_op))[0] == 401, "behind the login"
        await world.login()
        assert (await written(world, "deals", add_op, headers={"Cookie": f"{COOKIE}={world.cookie}"}))[0] == 403, \
            "a POST without the app's header is not the app's"
        before = world.store.cursor()
        status, made = await written(world, "deals", add_op, idem="face:1")
        assert status == 200 and made["name"] == "deals" and made["change_ids"][0] == made["rev"]
        deal = made["records"][0]
        assert (await written(world, "deals", add_op, idem="face:1"))[1]["change_ids"] == made["change_ids"], \
            "a press sent twice is written once"
        change = (await store.history("deals", deal["id"]))[0]
        assert (change["actor"], change["via"]) == ("owner", "screen")
        events = [json.loads(data) for _, kind, data in world.store.events_after(before) if kind == "collection"]
        assert events and all(e == {"name": "deals", "space": "work", "rev": made["rev"]} for e in events)

        moved = await store.apply(Writer("session:s2", "session"), "deals",
                                  [{"op": "set", "id": deal["id"], "version": 1, "fields": {"stage": "lost"}}], NOW + 120)
        mine = {"op": "set", "id": deal["id"], "version": 1, "fields": {"stage": "proposal"}}
        status, refused = await written(world, "deals", mine)
        assert status == 409 and refused["kind"] == "stale" and refused["record"]["version"] == 2
        assert refused["change"]["actor"] == "session:s2" and refused["change"]["after"] == {"stage": "lost"}
        status, applied = await written(world, "deals", {**mine, "version": refused["record"]["version"]})
        assert status == 200 and applied["records"][0]["fields"]["stage"] == "proposal", "«Apply mine»"
        assert moved["change_ids"][0] != applied["change_ids"][0]

        follow = (await written(world, "followups", {"op": "add", "fields": {"deal": deal["id"], "state": "draft"}}))[1]
        draft = follow["records"][0]
        session = Writer("session:s1", "session")
        with pytest.raises(Rejected, match="only by the owner, code:mail-sent"):
            await store.apply(session, "followups", [{"op": "set", "id": draft["id"], "version": 1,
                                                      "fields": {"state": "done"}}], NOW)
        status, done = await written(world, "followups", {"op": "set", "id": draft["id"], "version": 1,
                                                          "fields": {"state": "done"}})
        assert status == 200 and done["records"][0]["fields"]["state"] == "done", "what the owner alone sets"
        status, refused = await written(world, "followups", {"op": "set", "id": draft["id"], "version": 2,
                                                             "fields": {"state": "sent"}})
        assert status == 400 and refused["kind"] is None and "'state' is one of" in refused["error"]
        assert (await written(world, "nothing", add_op))[0] == 400
        assert (await world.call("POST", "/api/c/deals", {"ops": "add"}))[0] == 400

    faced(tmp_path, body)


def test_an_add_that_may_be_a_record_there_already_is_asked_about_and_added_when_it_is_another(tmp_path):
    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await store.define(DEFINE, "deals", "work", {**DEALS, "key": ["company"]}, NOW)
        await world.login()
        first = (await written(world, "deals", {"op": "add", "fields": {"company": "Acme"}}))[1]["records"][0]
        status, refused = await written(world, "deals", {"op": "add", "fields": {"company": " acme "}})
        assert status == 400 and refused["kind"] == "duplicate" and refused["record"]["id"] == first["id"]
        status, made = await written(world, "deals", {"op": "add", "fields": {"company": "acme"}, "distinct": True})
        assert status == 200 and made["records"][0]["id"] != first["id"], "«Add anyway»"

    faced(tmp_path, body)


def test_a_move_taken_back_moves_nothing_in_the_count(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        deal = await add(store, "deals", {"company": "Acme", "origin": "inbound", "stage": "lead"}, NOW - 20 * DAY)
        moved = await store.apply(OWNER, "deals", [{"op": "set", "id": deal["id"], "version": 1,
                                                    "fields": {"stage": "proposal"}}], NOW - DAY)
        assert (await shown(store))["deals"]["moved"]["value"] == 1
        await store.apply(OWNER, "deals", [{"op": "revert", "change_id": moved["change_ids"][0], "version": 2}], NOW)
        assert (await shown(store))["deals"]["moved"]["value"] == 0, "a mistaken drag and its «Undo» count none"
        await store.close()

    run(body)


def test_a_change_is_taken_back_from_its_record_or_the_spaces_changes_unless_changed_after(tmp_path):
    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        await store.define(DEFINE, "trips", "travel", {"fields": {"place": {"type": "text"}}}, NOW)
        await store.apply(OWNER, "trips", [{"op": "add", "fields": {"place": "Lisbon"}}], NOW)
        deal = await add(store, "deals", {"company": "Acme", "product": "Widgets", "stage": "lead"}, NOW)
        follow = await add(store, "followups", {"deal": deal["id"], "state": "draft", "channel": "mail"}, NOW)
        deal = await stage(store, deal, "qualified", NOW + 60)
        await world.login()

        record = await world.get(f"/api/c/followups/{follow['id']}")
        assert record["title"] == "Acme · mail", "a ref by its record's words, then the first text"
        assert record["fields"]["deal"] == {"type": "ref", "to": "deals"}
        assert record["fields"]["state"] == {"type": "enum", "values": ["draft", "done", "unknown"]}
        assert record["words"] == {deal["id"]: "Acme"}

        feed = await world.get("/api/c/_changes?space=work")
        assert [(c["collection"], c["op"]) for c in feed["changes"]] == [("deals", "set"), ("followups", "add"),
                                                                          ("deals", "add")], "not another Space's"
        newest = feed["changes"][0]
        assert newest["title"] == "Acme · Widgets" and newest["version"] == 2 and newest["deleted"] is None
        assert feed["next"] is None and feed["words"] == {deal["id"]: "Acme"}
        revert = {"op": "revert", "change_id": newest["change_id"], "version": newest["version"]}
        status, undone = await written(world, "deals", revert)
        assert status == 200 and undone["records"][0]["fields"]["stage"] == "lead"
        status, again = await written(world, "deals", {"op": "revert", "change_id": undone["change_ids"][0],
                                                       "version": undone["records"][0]["version"]})
        assert status == 200 and again["records"][0]["fields"]["stage"] == "qualified", "an undo undone"
        taken = {c["change_id"]: c["reverted"] for c in (await world.get("/api/c/_changes?space=work"))["changes"]}
        assert taken[newest["change_id"]] and taken[undone["change_ids"][0]], "each one taken back says so: no «Undo»"
        assert not taken[again["change_ids"][0]]

        added = next(c for c in feed["changes"] if c["op"] == "add" and c["collection"] == "deals")
        status, refused = await written(world, "deals", {"op": "revert", "change_id": added["change_id"],
                                                         "version": again["records"][0]["version"]})
        assert status == 409 and refused["kind"] == "after" and refused["change"]["actor"] == "owner"
        assert "changed after it: stage" in refused["error"]

        monkey = views.CHANGES_SHOWN
        views.CHANGES_SHOWN = 2
        try:
            page = await world.get("/api/c/_changes?space=work")
            assert len(page["changes"]) == 2 and page["next"] == page["changes"][-1]["change_id"]
            rest = await world.get(f"/api/c/_changes?space=work&before={page['next']}")
            assert rest["changes"][0]["change_id"] < page["next"]
        finally:
            views.CHANGES_SHOWN = monkey
        assert (await world.call("GET", "/api/c/_changes?space=work&before=x"))[0] == 400
        assert (await world.get("/api/c/_changes?space=nowhere")) == {"space": "nowhere", "changes": [], "next": None,
                                                                       "words": {}}

    faced(tmp_path, body)


def test_a_queues_press_and_a_views_add_are_held_to_the_form_and_an_add_offers_the_records_a_ref_may_name(tmp_path):
    for given, reason in [
        ([{"name": "x", "kind": "queue", "done": {"field": "state", "value": "done"}}], '"done" is {"field"'),
        ([{"name": "x", "kind": "queue", "done": {"field": "state", "value": [], "label": "Done"}}], '"done" is'),
        ([{"name": "x", "kind": "queue", "done": {"field": "state", "value": "done", "label": "x" * 41}}], '"done" is'),
        ([{"name": "x", "kind": "board", "group": "stage", "done": {}}], "not 'done'"),
        ([{"name": "x", "kind": "stat", "add": {"label": "Add"}}], "not 'add'"),
        ([{"name": "x", "kind": "queue", "add": {"fields": ["a"]}}], '"add" is {"label"'),
        ([{"name": "x", "kind": "queue", "add": {"label": "Add", "colour": "red"}}], '"add" is'),
        ([{"name": "x", "kind": "queue", "add": {"label": "Add", "values": {"a": {"b": 1}}}}], '"add" is'),
    ]:
        with pytest.raises(Rejected, match=re.escape(reason)):
            parse_views(given)

    async def body(world):
        world.clock.now = NOW
        store = world.platform.collections
        await work(store)
        lost = [{**WRITING[0], "name": "lost", "done": {"field": "status", "value": "done", "label": "Done"}},
                {**WRITING[0], "name": "wrong", "add": {"label": "Add", "values": {"state": "sent"}}}]
        await store.define(DEFINE, "followups", "work", FOLLOWUPS, NOW, views=WRITING + lost)
        deals = [await add(store, "deals", {"company": name, "product": "Widgets"}, NOW + n)
                 for n, name in enumerate(["Acme", "Initech"])]
        await store.apply(OWNER, "deals", [{"op": "delete", "id": deals[0]["id"], "version": 1}], NOW + 5)
        got = await shown(store)
        waiting = got["followups"]["waiting"]
        assert waiting["done"] == {"field": "state", "value": "done", "label": "Done"}
        assert waiting["add"] == {"label": "Add a follow-up", "fields": ["deal", "date", "state", "channel"],
                                  "values": {"state": "draft"}}
        assert got["followups"]["lost"] == {"name": "lost", "kind": "queue", "title": "Waiting on me",
                                            "error": "field not found: 'status'"}
        assert "'state' is one of draft, done, unknown" in got["followups"]["wrong"]["error"]
        assert got["deals"]["funnel"]["add"] is None and "add" not in got["deals"]["moved"]

        await world.login()
        form = await world.get("/api/c/followups/_form")
        assert list(form["fields"]) == list(FOLLOWUPS["fields"]) and form["fields"]["date"] == {"type": "date"}
        assert form["options"] == {"deal": [{"id": deals[1]["id"], "title": "Initech · Widgets"}]}, \
            "the deleted one is not offered"
        assert (await world.call("GET", "/api/c/nothing/_form"))[0] == 404

    faced(tmp_path, body)


def test_an_agenda_lists_the_days_ahead_soonest_first_and_a_chart_counts_each_week_by_an_enum(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        deals = {**DEALS, "fields": {**DEALS["fields"], "call": {"type": "date"}, "found": {"type": "date"}}}
        await store.define(DEFINE, "deals", "work", deals, NOW, views=[
            {"name": "calls", "kind": "agenda", "title": "Calls", "date": "call", "days": 3, "fields": ["company"]},
            {"name": "weekly", "kind": "chart", "title": "New deals", "date": "found", "group": "origin", "weeks": 3,
             "tab": "Numbers"},
        ])
        for company, call, found, origin in (("Acme", "2026-09-18", "2026-09-15", "inbound"),
                                             ("Globex", "2026-09-16", "2026-09-14", "outbound"),
                                             ("Initech", "2026-09-19", "2026-09-08", "inbound"),
                                             ("Umbrella", "2026-09-15", "2026-08-30", "inbound")):
            await add(store, "deals", {"company": company, "call": call, "found": found, "origin": origin}, NOW)
        got = (await shown(store))["deals"]
        calls = got["calls"]
        assert [(r["fields"]["company"], r["days"]) for r in calls["rows"]] == [("Globex", 0), ("Acme", -2)], \
            "today and the two days after it, the soonest first; yesterday's and the fourth day's are not ahead"
        assert (calls["first"], calls["last"], calls["count"]) == ("2026-09-16", "2026-09-18", 2)
        assert calls["rows"][0]["fields"] == {"company": "Globex", "call": "2026-09-16"}, \
            "a row says its day, though the view's fields do not name it: the screen puts it on that day"
        assert calls["fields"] == ["company"]
        weekly = got["weekly"]
        assert weekly["tab"] == "Numbers" and weekly["values"] == ["inbound", "outbound"]
        assert [(w["first"], {c["value"]: c["count"] for c in w["counts"]}) for w in weekly["weeks"]] == [
            ("2026-08-31", {}), ("2026-09-07", {"inbound": 1}), ("2026-09-14", {"inbound": 1, "outbound": 1})], \
            "the last three weeks, this one last; the 30th of August is in the week before them"
        await store.close()

    run(body)


def test_a_queue_goes_by_its_sort_field_says_its_mark_and_the_screen_has_the_forms_words_for_its_values(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        deals = {**DEALS, "fields": {**DEALS["fields"], "priority": {"type": "int"},
                                     "stage": {"type": "enum", "values": ["lead", "qualified", "won"],
                                               "labels": {"lead": "New lead"}}}}
        await store.define(DEFINE, "deals", "work", deals, NOW, views=[
            {"name": "next", "kind": "queue", "sort": "priority", "mark": "wait", "tab": "Leads",
             "fields": ["company"]}])
        for company, priority in (("Acme", 2), ("Globex", None), ("Initech", 1)):
            await add(store, "deals", {"company": company, **({"priority": priority} if priority else {})}, NOW)
        answer = await store.read(lambda reader: views.space_views(reader, "work", When("UTC", NOW)))
        assert answer["collections"][0]["labels"] == {"origin": {}, "stage": {"lead": "New lead"}}
        queue = answer["collections"][0]["views"][0]
        assert [r["fields"]["company"] for r in queue["rows"]] == ["Initech", "Acme", "Globex"], \
            "the least priority first, the one without it last"
        assert (queue["mark"], queue["tab"]) == ("wait", "Leads")
        await store.close()

    run(body)


def test_a_records_sheet_lists_the_records_of_other_collections_that_name_it_the_newest_first(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await work(store)
        acme = await add(store, "deals", {"company": "Acme"}, NOW)
        other = await add(store, "deals", {"company": "Globex"}, NOW)
        for date, deal in (("2026-09-10", acme), ("2026-09-14", acme), ("2026-09-12", other)):
            await add(store, "followups", {"deal": deal["id"], "date": date, "channel": "mail"}, NOW)
        sheet = await store.read(lambda reader: views.record_view(reader, "deals", acme["id"]))
        (followups,) = sheet["related"]
        assert (followups["name"], followups["ref"], followups["date"]) == ("followups", "deal", "date")
        assert [r["fields"]["date"] for r in followups["records"]] == ["2026-09-14", "2026-09-10"]
        assert followups["fields"]["direction"]["values"] == ["in", "out"]
        assert "hide" not in sheet["fields"]["company"]
        lone = await store.read(lambda reader: views.record_view(reader, "followups", followups["records"][0]["id"]))
        assert lone["related"] == [], "nothing names a follow-up"
        await store.close()

    run(body)


def test_a_queues_rows_carry_the_newest_record_that_names_them_its_day_value_and_arrow(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        followups = {**FOLLOWUPS, "fields": {**FOLLOWUPS["fields"],
                                             "direction": {"type": "enum", "values": ["in", "out"],
                                                           "labels": {"in": "They wrote"}}}}
        await store.define(DEFINE, "deals", "work", DEALS, NOW, views=[
            {"name": "open", "kind": "queue", "fields": ["company"],
             "latest": {"collection": "followups", "field": "direction", "arrows": {"in": "in", "out": "out"}}},
            {"name": "lost", "kind": "queue", "fields": ["company"],
             "latest": {"collection": "followups", "field": "channel"}}])
        await store.define(DEFINE, "followups", "work", followups, NOW)
        acme = await add(store, "deals", {"company": "Acme"}, NOW)
        await add(store, "deals", {"company": "Globex"}, NOW)
        for date, direction in (("2026-09-10", "out"), ("2026-09-14", "in"), ("2026-09-12", "out")):
            await add(store, "followups", {"deal": acme["id"], "date": date, "direction": direction}, NOW)
        await add(store, "followups", {"deal": acme["id"], "direction": "out"}, NOW)  # no day: not the newest
        got = (await shown(store))["deals"]
        rows = {r["fields"]["company"]: r for r in got["open"]["rows"]}
        assert rows["Acme"]["latest"] == {"date": "2026-09-14", "value": "in", "label": "They wrote", "arrow": "in"}
        assert "latest" not in rows["Globex"], "nothing names it"
        assert got["open"]["latest"] is True, "the screen keeps the column for every row"
        assert got["lost"]["error"] == "'channel' is a text, not a enum"
        await store.close()

    run(body)


def test_the_new_view_keys_and_an_enums_labels_are_checked_in_their_shape(tmp_path):
    for given, reason in [
        ([{"name": "x", "kind": "queue", "tab": ""}], '"tab" is plain text'),
        ([{"name": "x", "kind": "queue", "tab": "x" * 25}], '"tab" is plain text'),
        ([{"name": "x", "kind": "queue", "mark": "urgent"}], '"mark" is one of act, wait'),
        ([{"name": "x", "kind": "queue", "sort": "a b"}], '"sort" names a field'),
        ([{"name": "x", "kind": "agenda"}], 'names the date field'),
        ([{"name": "x", "kind": "agenda", "date": "d", "days": 0}], '"days" is a whole number from 1 to 31'),
        ([{"name": "x", "kind": "chart", "date": "d", "weeks": 27}], '"weeks" is a whole number from 2 to 26'),
        ([{"name": "x", "kind": "chart", "date": "d", "fields": ["a"]}], "not 'fields'"),
        ([{"name": "x", "kind": "stat", "mark": "act"}], "not 'mark'"),
        ([{"name": "x", "kind": "queue", "latest": {"collection": "t"}}], '"latest" is {"collection"'),
        ([{"name": "x", "kind": "queue", "latest": {"collection": "t", "field": "d", "arrows": {"in": "left"}}}],
         '"latest" is {"collection"'),
        ([{"name": "x", "kind": "board", "group": "g", "latest": {"collection": "t", "field": "d"}}], "not 'latest'"),
    ]:
        with pytest.raises(Rejected, match=re.escape(reason)):
            parse_views(given)

    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        with pytest.raises(Rejected, match=re.escape('"hide" is true or false')):
            await store.define(DEFINE, "deals", "work", {"fields": {"n": {"type": "int", "hide": "yes"}}}, NOW)
        await store.define(DEFINE, "deals", "work", {"fields": {"n": {"type": "int", "hide": True}}}, NOW)
        sheet = await store.read(lambda reader: views.form_view(reader, "deals"))
        assert sheet["fields"]["n"] == {"type": "int", "hide": True}, "the screen is told which fields to leave out"
        for labels, reason in (({"won": "Won"}, '"labels" gives some of its values words'),
                               ({"lead": ""}, '"labels" gives some of its values words'),
                               ([], '"labels" gives some of its values words')):
            form = {"fields": {"stage": {"type": "enum", "values": ["lead"], "labels": labels}}}
            with pytest.raises(Rejected, match=re.escape(reason)):
                await store.define(DEFINE, "leads", "work", form, NOW)
        await store.close()

    run(body)


# --- sums by month, a year's line, money and files ------------------------------------------------------------------

LEDGER = {
    "fields": {
        "what": {"type": "text"},
        "amount": {"type": "money", "currency": "EUR"},
        "kind": {"type": "enum", "values": ["rent", "food"]},
        "date": {"type": "date"},
        "receipt": {"type": "file"},
    },
}
LEDGER_VIEWS = [
    {"name": "spent", "kind": "months", "title": "Spent by month", "date": "date", "sum": "amount", "group": "kind",
     "months": 3},
    {"name": "count", "kind": "months", "date": "date"},
    {"name": "year", "kind": "year", "title": "This year", "date": "date", "sum": "amount"},
]


def test_sums_by_month_and_a_years_line_count_to_the_cent(tmp_path):
    async def body():
        store = Collections(str(tmp_path / "collections.sqlite"))
        await store.define(DEFINE, "ledger", "home", LEDGER, NOW, views=LEDGER_VIEWS)
        for amount, kind, date in (("0.10", "food", "2026-09-01"), (0.2, "food", "2026-09-15"),
                                   ("900", "rent", "2026-09-01"), ("12.5", "food", "2026-08-31"),
                                   ("100", "rent", "2025-03-03"), ("7", "food", "2026-06-30")):
            await add(store, "ledger", {"amount": amount, "kind": kind, "date": date}, NOW)
        answer = await store.read(lambda reader: views.space_views(reader, "home", When("UTC", NOW)))
        got = {v["name"]: v for v in answer["collections"][0]["views"]}
        spent = got["spent"]
        assert (spent["unit"], spent["values"]) == ("EUR", ["rent", "food"])
        assert spent["months"] == [
            {"first": "2026-07-01", "totals": []},
            {"first": "2026-08-01", "totals": [{"value": "food", "total": "12.50"}]},
            {"first": "2026-09-01", "totals": [{"value": "rent", "total": "900.00"}, {"value": "food", "total": "0.30"}]},
        ], "the last three months, this one last, each split in the enum's order, in exact cents"
        assert [m["totals"] for m in got["count"]["months"]][-4:] == [[{"value": None, "total": 1}], [], [
            {"value": None, "total": 1}], [{"value": None, "total": 3}]], "no sum: the records counted"
        year = got["year"]
        assert (year["year"], year["through"], year["unit"]) == (2026, 9, "EUR")
        assert [m["running"] for m in year["months"]][5:9] == ["7.00", "7.00", "19.50", "919.80"]
        assert year["previous"][2] == {"first": "2025-03-01", "total": "100.00", "running": "100.00"}
        for bad, reason in (("12.345", "at most two places"), (True, "at most two places"), ("1e3", "two places")):
            with pytest.raises(Rejected, match=reason):
                await add(store, "ledger", {"amount": bad}, NOW)
        with pytest.raises(Rejected, match="there is no such file"):
            await add(store, "ledger", {"receipt": "0" * 64}, NOW)
        file, _ = await store.put_file(b"%PDF-1.7 receipt", "acme-receipt.pdf", "application/pdf", NOW)
        await add(store, "ledger", {"what": "Lunch", "receipt": file["sha256"]}, NOW)
        await store.define(DEFINE, "ledger", "home", LEDGER, NOW, views=[
            {"name": "all", "kind": "queue", "fields": ["what", "receipt"],
             "filter": {"field": "receipt", "op": "not_empty"}}])
        (row,) = (await store.read(lambda reader: views.one_view(reader, "ledger", "all", When("UTC", NOW))))["view"]["rows"]
        assert row["refs"] == {"receipt": "acme-receipt.pdf"}, "a file by its name, never its sha256"
        with pytest.raises(Rejected, match="would become another type"):
            await store.define(DEFINE, "ledger", "home", {"fields": {**LEDGER["fields"], "amount": {
                "type": "money", "currency": "USD"}}}, NOW)
        for given, reason in (([{"name": "x", "kind": "months"}], "names the date field"),
                              ([{"name": "x", "kind": "months", "date": "d", "months": 25}], "from 2 to 24"),
                              ([{"name": "x", "kind": "year", "date": "d", "group": "g"}], "not 'group'")):
            with pytest.raises(Rejected, match=re.escape(reason)):
                parse_views(given)
        await store.define(DEFINE, "ledger", "home", LEDGER, NOW, views=[
            {"name": "bad", "kind": "year", "date": "date", "sum": "what"}])
        bad = (await store.read(lambda reader: views.one_view(reader, "ledger", "bad", When("UTC", NOW))))["view"]
        assert bad["error"] == "'what' is a text, not a int or money", "only a number or money adds up"
        await store.close()

    run(body)


PAGE = ("<!doctype html><title>Burn</title><p id=n></p><script>addEventListener('message', e => "
        "{ document.getElementById('n').textContent = e.data.view.rows.length })</script>")
BILLING = """
collections:
  invoices:
    form:
      fields:
        company: {type: text}
        amount: {type: money, currency: EUR}
views:
  - {name: burn, kind: code, collection: invoices, title: Burn-down, tab: Money}
"""


def test_a_projects_own_view_gets_its_data_and_no_network(tmp_path):
    async def body(world):
        store = world.platform.collections
        given = project.read(BILLING.replace("tab: Money}", "tab: Money, file: views/burn.html}"), "billing",
                             {"views/burn.html": PAGE.encode()}.get)
        await project.apply(store, "billing", given, NOW)
        await add(store, "invoices", {"company": "Acme", "amount": "10"}, NOW)
        src = "/api/c/invoices/_code/burn"
        assert (await world.api.request("GET", src)).status == 401, "behind the app's login"
        await world.login()
        got = await world.get("/api/c/_space?space=billing")
        (code,) = [v for c in got["collections"] for v in c["views"] if v["kind"] == "code"]
        assert (code["name"], code["title"], code["tab"], code["src"]) == ("burn", "Burn-down", "Money", src)
        assert [r["fields"] for r in code["rows"]] == [{"company": "Acme", "amount": "10.00"}], "its data, from the app"
        assert code["fields"]["amount"] == {"type": "money", "currency": "EUR"}
        assert "html" not in code, "the page itself is not in the data"
        assert code["sandbox"] == pagesd.SANDBOX[True] and "allow-same-origin" not in code["sandbox"], \
            "the frame's sandbox: the pages service's, scripts on, an opaque origin"
        again = await world.get("/api/c/invoices?view=burn")
        assert again["view"]["rows"] == code["rows"], "read again by name, as any view"

        response = await world.api.request("GET", src, headers={"Cookie": f"{COOKIE}={world.cookie}"})
        assert response.status == 200 and await response.text() == PAGE
        assert response.headers["Content-Type"].startswith("text/html")
        policy = dict(d.strip().split(" ", 1) for d in response.headers["Content-Security-Policy"].split(";"))
        assert policy["default-src"] == "'none'" and policy["connect-src"] == "'none'", "no network at all"
        assert policy["script-src"] == "'unsafe-inline'", "its own scripts run, nothing loaded from anywhere"
        assert policy["sandbox"] == pagesd.SANDBOX[True] and policy["frame-ancestors"] == world.cfg.face_origin
        assert all(policy[d] == "'none'" for d in ("frame-src", "worker-src", "form-action", "base-uri"))
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        missing = await world.api.request("GET", "/api/c/invoices/_code/other",
                                          headers={"Cookie": f"{COOKIE}={world.cookie}"})
        assert missing.status == 404
        with pytest.raises(Rejected, match="loads nothing from elsewhere"):
            project.read(BILLING.replace("tab: Money}", "html: '<img src=\"https://example.com/p.png\">'}"), "billing")

    faced(tmp_path, body)
