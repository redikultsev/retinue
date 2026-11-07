"""A code rule's hand on one collection (`coderules.Records`), against the platform's own app: a read is every page and
says whether it was all, a write is all or none under its idem, and the rule's own changes are read back and taken
back."""

from __future__ import annotations

import time

from retinue import coderules
from retinue.coderules import WAITING, Records
from test_collections_assistant import platform_of
from test_face import faced
from test_planner import setup
from test_platformd import ROUTER, short_wait  # noqa: F401 — the fixture is autouse


def test_a_rule_reads_every_page_writes_all_or_none_and_takes_back_its_own_change(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(coderules, "PAGE", 2)

    async def run(world):
        world.clock.now = time.time()
        await setup(world, tmp_path)  # the collections `tasks` and `blocks`, with the deploy's rights
        tasks = Records(platform_of(world), WAITING, "tasks")
        five = [{"op": "add", "fields": {"title": f"Task {n}", "state": "open", "ask": f"reminder:{n}"}} for n in range(5)]
        made = await tasks.apply(five, "make:5")
        assert [r["version"] for r in made["records"]] == [1] * 5 and all(made["change_ids"])
        assert await tasks.apply(five, "make:5") == made, "the same write again: the call made once"

        monkeypatch.setattr(coderules, "PAGES", 2)
        cut = await tasks.query([{"field": "state", "op": "eq", "value": "open"}], ["title"])
        assert len(cut) == 4 and not cut.complete, "two pages of two: a read cut short says so"
        monkeypatch.setattr(coderules, "PAGES", 3)
        read = await tasks.query([{"field": "state", "op": "eq", "value": "open"}], ["title"])
        assert len(read) == 5 and read.complete

        first = read[0]
        assert await tasks.apply([{"op": "add", "fields": {"title": "Sixth", "state": "open"}},
                                  {"op": "set", "id": first["id"], "version": first["version"],
                                   "fields": {"goal": "work"}}], "refused") is None
        assert "goal" in tasks.refused, "the refusal's words are kept"
        assert len(await tasks.query([])) == 5, "all or none: the add before it did not happen either"

        renamed = await tasks.apply([{"op": "set", "id": first["id"], "version": first["version"],
                                      "fields": {"title": "Renamed"}}], "rename")
        assert renamed["records"][0]["version"] == 2
        [change] = [c for c in await tasks.changes(world.clock.now - 60) if c["op"] == "set"]
        assert change["id"] == first["id"] and change["actor"] == WAITING
        assert await Records(platform_of(world), "code:plan", "tasks").changes(world.clock.now - 60) == [], \
            "another rule's: none of its own"
        assert await tasks.revert(change["change_id"])
        assert (await world.platform.collections.get("tasks", first["id"]))["fields"]["title"] == first["fields"]["title"]

        missing = Records(platform_of(world), WAITING, "nonesuch")
        assert await missing.query([]) is None and await missing.query([]) is None
    faced(tmp_path, run)
    assert caplog.text.count("nonesuch") == 1, "an unread collection is said once, then quiet"
