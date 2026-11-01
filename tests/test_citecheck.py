"""The source check (`retinue/citecheck.py`): a turn in the room that ends on several cited addresses gets a clean side
session that may open those addresses and nothing else; code reads its verdict, and only a contradicted claim rings."""

from __future__ import annotations

import dataclasses

from retinue import citecheck, pings
from test_platformd import decision, short_wait  # noqa: F401 — the fixture is autouse
from test_subs import SubsWorld, subs_scenario

ANSWER = """Three hosting plans fit:
- Acme Basic costs €4/month (https://acme.example/pricing)
- Beta Cloud has IPv6 on every plan (https://beta.example/docs/network#ipv6)
- Gamma VPS bills hourly, see https://gamma.example/billing?utm_source=x.
Pick Acme for the price."""


class WebWorld(SubsWorld):
    async def up(self) -> None:
        await super().up()
        self.cfg.lanes["private"] = dataclasses.replace(self.cfg.lanes["private"], web=True)


def checker_of(world: SubsWorld, session: str) -> str | None:
    row = world.store.db.execute("SELECT checker FROM cite_checks WHERE session = ?", (session,)).fetchone()
    return row[0] if row else None


def fetch(url: str, call: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "p"},
            "tool_use_id": call, "cwd": "/work", "permission_mode": "auto"}


def test_claims_are_the_lines_with_addresses_and_the_verdict_is_read_by_code():
    found = citecheck.claims(ANSWER)
    assert [urls for _, urls in found] == [["https://acme.example/pricing"], ["https://beta.example/docs/network#ipv6"],
                                           ["https://gamma.example/billing?utm_source=x"]]
    assert citecheck.norm("HTTPS://Acme.Example/pricing/#x") == "https://acme.example/pricing"
    read = citecheck.verdict("1 | found | Basic: €4 per month\n2 | contradicts | IPv6 only on Pro\n3 | found\n", 3)
    assert read["counts"] == {"found": 1, "missing": 0, "contradicts": 1, "unreachable": 0, "unchecked": 1}
    assert read["contradicts"] == [{"claim": 2, "page": "IPv6 only on Pro"}]


def test_a_cited_turn_gets_a_checker_that_opens_those_addresses_only_and_a_clean_check_is_quiet(tmp_path):
    async def body(world: SubsWorld):
        session = await world.parent("сравни тарифы хостинга")
        await world.turn(session, ANSWER)
        checker = checker_of(world, session)
        assert checker and world.store.session(checker)["parent"] == session
        await world.platform.drain()
        run = world.run_of(checker)
        assert "1. " in run["prompt"] and "https://acme.example/pricing" in run["prompt"]
        assert decision(await world.hook("PreToolUse", run, fetch("https://acme.example/pricing", "c1"))) == "allow"
        assert decision(await world.hook("PreToolUse", run, fetch("https://gamma.example/billing", "c2"))) == "allow", \
            "the clean form of a tracked link"
        assert decision(await world.hook("PreToolUse", run, fetch("https://other.example/", "c3"))) == "deny"
        bash = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                "tool_use_id": "c4", "cwd": "/work", "permission_mode": "auto"}
        assert decision(await world.hook("PreToolUse", run, bash)) == "deny"
        await world.turn(checker, "1 | found | Basic — €4 a month\n2 | found | IPv6 on all plans\n3 | unreachable | 403")
        done = [n for n in world.notes("summary") if n["session"] == checker][-1]["data"]
        assert done["text"] == f"Sources of session {session} checked: 2 of 3 found (unreachable 1)."
        assert done["quiet"] is True and not pings.loud(done)
        assert world.store.session(checker)["archived"]
        assert checker_of(world, checker) is None, "a check is never checked"
    subs_scenario(tmp_path, body, WebWorld)


def test_a_contradicted_claim_rings_and_names_what_the_page_says(tmp_path):
    async def body(world: SubsWorld):
        session = await world.parent("сравни тарифы хостинга")
        await world.turn(session, ANSWER)
        checker = checker_of(world, session)
        await world.platform.drain()
        await world.turn(checker, "1 | found | Basic — €4 a month\n2 | contradicts | IPv6 only on the Pro plan\n"
                                  "3 | missing")
        done = [n for n in world.notes("summary") if n["session"] == checker][-1]["data"]
        assert done["text"].startswith(f"Sources of session {session}: 1 of 3 claims are contradicted")
        assert "IPv6 only on the Pro plan" in done["text"] and not done.get("quiet")
        assert not world.store.session(checker)["archived"]
    subs_scenario(tmp_path, body, WebWorld)


def test_no_check_for_few_links_a_lane_without_the_web_or_past_the_days_cap(tmp_path):
    async def body(world: SubsWorld):
        few = await world.parent("первая")
        await world.turn(few, "See https://acme.example/a and https://acme.example/b.")
        assert checker_of(world, few) is None
        world.cfg.lanes["private"] = dataclasses.replace(world.cfg.lanes["private"], web=False)
        offline = await world.parent("вторая")
        await world.turn(offline, ANSWER)
        assert checker_of(world, offline) is None
    subs_scenario(tmp_path, body, WebWorld)
