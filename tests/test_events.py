"""Events and reactions between projects: an invoice paid in one project files a follow-up in another, written as the
reacting project with the rights the owner gave it there and never the emitter's; a reaction that would act outside
the install only asks the owner; a chain of events that answer each other stops at its depth; a project's description
is checked strictly and is the owner's to apply."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from retinue import events, project
from retinue.collections import Collections, Rejected, Writer

OWNER = Writer("owner", "screen")
DEFINE = Writer("owner", "define")
NOW = dt.datetime(2026, 9, 16, 12, tzinfo=dt.UTC).timestamp()

BILLING = """
collections:
  invoices:
    form:
      fields:
        company: {type: text, required: true}
        amount: {type: money, currency: EUR}
        state: {type: enum, values: [open, paid]}
        date: {type: date}
  receipts:
    form:
      fields:
        invoice: {type: ref, to: invoices}
        note: {type: text}
events:
  invoice_paid:
    collection: invoices
    when: {field: state, to: [paid]}
reactions:
  - on: invoice_paid
    do: add
    collection: receipts
    fields: {invoice: {from: id}, note: Paid in full}
"""
CRM = """
collections:
  followups:
    form:
      fields:
        company: {type: text}
        invoice: {type: text}
        note: {type: text}
reactions:
  - on: billing/invoice_paid
    do: add
    collection: followups
    fields: {company: {from: company}, invoice: {from: id}, note: Thank them}
  - on: billing/invoice_paid
    do: add
    collection: receipts
    fields: {note: written by crm}
  - on: billing/invoice_paid
    do: send
    text: Send Acme a thank-you letter
"""


def run(body):
    return asyncio.run(body())


class World:
    """The collections with two projects described, and the owner's cards a reaction puts to him."""

    def __init__(self, tmp_path) -> None:
        self.store = Collections(str(tmp_path / "collections.sqlite"))
        self.asked: list[dict] = []
        self.events = events.Events(self.store, lambda: NOW, ask=self.ask)

    async def ask(self, project_name: str, event: dict, text: str, needs: str | None, key: str) -> str:
        self.asked.append({"project": project_name, "event": f"{event['project']}/{event['name']}", "text": text,
                           "needs": needs, "key": key})
        return f"card{len(self.asked)}"

    async def describe(self, name: str, text: str) -> None:
        await project.apply(self.store, name, project.read(text, name), NOW)

    async def grant(self, collection: str, cls: str) -> None:
        await self.store.rights(DEFINE, collection, {"writers": {cls: {"add": True, "set": "*"}}}, NOW)

    async def rows(self, collection: str) -> list[dict]:
        return (await self.store.query(collection))["rows"]

    async def paid(self, invoice: dict) -> dict:
        op = {"op": "set", "id": invoice["id"], "version": invoice["version"], "fields": {"state": "paid"}}
        answer = await self.store.apply(OWNER, "invoices", [op], NOW)
        await self.events.idle()
        return answer["records"][0]


def test_an_event_in_one_project_creates_a_record_in_another(tmp_path):
    async def body():
        world = World(tmp_path)
        await world.describe("billing", BILLING)
        await world.describe("crm", CRM)
        await world.grant("followups", "project:crm")
        await world.grant("receipts", "project:billing")
        invoice = (await world.store.apply(OWNER, "invoices", [{"op": "add", "fields": {
            "company": "Acme", "amount": 1250.5, "state": "open"}}], NOW))["records"][0]
        await world.events.idle()
        assert invoice["fields"]["amount"] == "1250.50", "money is kept to the cent, as a string"
        assert await world.rows("followups") == [], "an open invoice is no event"
        invoice = await world.paid(invoice)
        followups = await world.rows("followups")
        assert [r["fields"] for r in followups] == [{"company": "Acme", "invoice": invoice["id"], "note": "Thank them"}]
        change = (await world.store.history("followups", followups[0]["id"]))[0]
        assert (change["actor"], change["via"], change["src"]) == ("project:crm", "event", f"record:{invoice['id']}")

        again = await world.store.apply(OWNER, "invoices", [{"op": "set", "id": invoice["id"], "version":
                                                             invoice["version"], "fields": {"state": "paid"}}], NOW)
        await world.events.idle()
        assert again["change_ids"] == [None] and len(await world.rows("followups")) == 1, "already paid: no event"
        moved = await world.store.apply(OWNER, "invoices", [{"op": "set", "id": invoice["id"], "version":
                                                             invoice["version"], "fields": {"amount": "99"}}], NOW)
        await world.events.idle()
        assert len(await world.rows("followups")) == 1, "a change of another field fires nothing"

        assert moved["change_ids"][0]
        paid = [c["change_id"] for c in await world.store.history("invoices", invoice["id"])
                if (c["after"] or {}).get("state") == "paid"]
        assert await world.events.after("invoices", paid, 0) == [], "the same change told again fires nothing more"
        logged = await world.events.log("billing")
        assert [(e["name"], e["depth"]) for e in logged] == [("invoice_paid", 0)], "one change, one event, once"
        await world.store.close()

    run(body)


def test_a_reaction_runs_with_the_reacting_projects_rights(tmp_path):
    async def body():
        world = World(tmp_path)
        await world.describe("billing", BILLING)
        await world.describe("crm", CRM)
        await world.grant("receipts", "project:billing")  # billing's reactions write its receipts; crm's write nothing
        invoice = (await world.store.apply(OWNER, "invoices", [{"op": "add", "fields": {
            "company": "Acme", "state": "open"}}], NOW))["records"][0]
        await world.paid(invoice)

        receipts = await world.rows("receipts")
        assert [r["fields"]["note"] for r in receipts] == ["Paid in full"], "only billing's own reaction wrote here"
        assert (await world.store.history("receipts", receipts[0]["id"]))[0]["actor"] == "project:billing"
        assert await world.rows("followups") == [], "crm was given nothing in its own followups yet"
        (event,) = await world.events.log("billing")
        crm = {r["reaction"]: r for r in event["outcome"][0]["reactions"] if r["project"] == "crm"}
        assert "project:crm does not add records to followups" in crm[0]["refused"]
        assert "project:crm does not add records to receipts" in crm[1]["refused"], \
            "the emitter's rights in receipts are not lent to the project that reacts"
        assert crm[2] == {"project": "crm", "reaction": 2, "do": "send", "asked": "card1"}
        assert world.asked == [{"project": "crm", "event": "billing/invoice_paid", "text": "Send Acme a thank-you "
                                "letter", "needs": events.OUTSIDE_NEEDS.format(do="send"), "key": world.asked[0]["key"]}], \
            "outside the install, a reaction only asks the owner"

        with pytest.raises(Rejected, match="the owner's to apply"):  # nor does a project widen itself
            await world.store.put_project(Writer("session:s1", "session"), "crm", {"events": {}, "reactions": [],
                                                                                   "views": []}, NOW)
        await world.store.close()

    run(body)


PING = """
collections:
  pings:
    form: {fields: {n: {type: int}}}
events:
  added: {collection: pings, when: {added: true}}
reactions:
  - {on: pong/added, do: add, collection: pings, fields: {n: 1}}
"""
PONG = """
collections:
  pongs:
    form: {fields: {n: {type: int}}}
events:
  added: {collection: pongs, when: {added: true}}
reactions:
  - {on: ping/added, do: add, collection: pongs, fields: {n: 1}}
"""


def test_an_event_chain_stops_at_its_depth_limit(tmp_path):
    async def body():
        world = World(tmp_path)
        await world.describe("ping", PING)
        await world.describe("pong", PONG)
        await world.grant("pings", "project:ping")
        await world.grant("pongs", "project:pong")
        await world.store.apply(OWNER, "pings", [{"op": "add", "fields": {"n": 0}}], NOW)
        await world.events.idle()

        made = len(await world.rows("pings")) - 1 + len(await world.rows("pongs"))
        assert made == events.DEPTH_MAX, "each project answers the other's add, until the chain is DEPTH_MAX long"
        logged = sorted(await world.events.log(), key=lambda e: e["depth"])
        assert [e["depth"] for e in logged] == list(range(events.DEPTH_MAX + 1))
        assert logged[-1]["outcome"] == [{"stopped": events.DEPTH_MAX, "reactions": [], "asked": "card1"}]
        assert len(world.asked) == 1 and "stopped" in world.asked[0]["text"], "the owner hears of it once"
        await world.store.close()

    run(body)


def test_a_projects_description_is_checked_strictly():
    for bad, why in (("collections: {x: {form: {fields: {a: {type: text}}}, rights: {}}}", "rights are the owner's"),
                     ("writers: {}", "rights are the owner's"),
                     ("hooks: []", "takes collections, events"),
                     ("a: &a [1]\nb: *a", "aliases"),
                     ("reactions: [{on: x/y, do: pay, text: Pay it, collection: z}]", "takes do, on, text"),
                     ("reactions: [{on: 'x/../y', do: ask, text: hi}]", "names an event"),
                     ("reactions: [{on: y, do: add, collection: z, fields: {a: {from: '$x'}}}]", "from"),
                     ("events: {e: {collection: c, when: {field: s, to: [{}]}}}", "when is"),
                     ("views: [{name: v, kind: code, collection: c, html: '<script src=\"https://cdn.example.com/x.js\">"
                      "</script>'}]", "loads nothing from elsewhere"),
                     ("views: [{name: v, kind: code, collection: c, html: '<iframe></iframe>'}]", "frames nothing"),
                     ("views: [{name: v, kind: code, collection: c, file: ../x.html}]", "a page's path")):
        with pytest.raises(Rejected, match=why):
            project.read(bad, "billing")
    kept = project.read("reactions: [{on: paid, do: task, fields: {title: Call them}}]", "billing")
    assert kept["reactions"] == [{"on": "billing/paid", "do": "task", "fields": {"title": "Call them"}}]
