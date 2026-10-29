"""«Ждут тебя» on the platform of `test_platformd` and its face: one queue by action across the sessions and the
assistant's digest — question cards to answer, approval and courier cards to decide, a summary that left a choice, an
unread turn, the router's pushed digest — the digest's checks and caps, and the rows `/wait` takes."""

from __future__ import annotations

import json
import math

import pytest

from retinue import feed
from retinue.config import Sphere
from test_face import FaceWorld, faced
from test_platformd import DAY, HOUR, World, decision, deferred_session, scenario, short_wait  # noqa: F401
from test_pings import turn
from test_questions import LINES, pre_tool_use

NOW = 1_000_000.0


def item(n: int = 1, **over) -> dict:
    return {"id": f"chat:{n:012x}", "kind": "chat", "who": "Мария", "title": "ответить: нужен ли трансфер",
            "since": NOW - HOUR, "sphere": "travel", **over}


# --- the digest: what the router may push -------------------------------------------------------------------------

def test_a_digest_is_taken_whole_with_names_cut_and_any_address_written_as_such():
    items, bot = feed.clean({"items": [item(1, who="Ivan <ivan@corp.example>‮", sphere=None),
                                       item(2, kind="deadline", title="x" * 500)], "bot": "retinue_bot"}, NOW)
    assert bot == "retinue_bot"
    assert items[0]["who"] == "Ivan <‹адрес›>" and items[0]["sphere"] is None, "no address, no invisible mark"
    assert len(items[1]["title"]) == feed.TITLE_CHARS and items[1]["title"].endswith("…")
    hidden, _ = feed.clean({"items": [item(1, who="ivan@acme\x1f.com", title="пиши ivan＠acme.com"),
                                      item(2, who="ｉｖａｎ@ａｃｍｅ.ｃｏｍ")]}, NOW)
    assert [(i["who"], i["title"]) for i in hidden] == [("‹адрес›", "пиши ‹адрес›"), ("‹адрес›", item()["title"])], \
        "folded and stripped before the scrub"


@pytest.mark.parametrize("body, why", [
    ([], "items"),
    ({"items": "x"}, "items"),
    ({"items": [item(), item()]}, "повторяется"),
    ({"items": [{**item(), "text": "тело письма"}]}, "поля"),
    ({"items": [{**item(), "address": "a@b.example"}]}, "поля"),
    ({"items": [{k: v for k, v in item().items() if k != "who"}]}, "поля"),
    ({"items": [item(kind="mail")]}, "kind"),
    ({"items": [item(id="chat 1")]}, "id"),
    ({"items": [item(id="")]}, "id"),
    ({"items": [item(since="вчера")]}, "since"),
    ({"items": [item(since=True)]}, "since"),
    ({"items": [item(since=math.inf)]}, "since"),
    ({"items": [item(since=NOW + 2 * 400 * DAY)]}, "since"),
    ({"items": [item(sphere="Путешествия")]}, "sphere"),
    ({"items": [item(who=["Мария"])]}, "строки"),
    ({"items": [item()], "bot": "@bot"}, "bot"),
    ({"items": [item(n) for n in range(feed.DIGEST_ITEMS + 1)]}, "больше"),
])
def test_a_digest_off_the_contract_is_refused_whole(body, why):
    with pytest.raises(feed.Refused, match=why):
        feed.clean(body, NOW)


def deadline(n: int = 1, **over) -> dict:
    return item(n, **{"id": f"deadline:{n:012x}", "kind": "deadline", "who": "База",
                      "title": "через 5 дней: Абонемент — действует до", "since": NOW + 2 * DAY, "sphere": "personal",
                      "when": "1970-01-17", "path": "personal/profile/gym.md", **over})


def test_a_deadline_carries_its_day_and_its_records_path_and_nothing_else_does():
    [taken] = feed.clean({"items": [deadline()]}, NOW)[0]
    assert (taken["when"], taken["path"]) == ("1970-01-17", "personal/profile/gym.md")
    [bare] = feed.clean({"items": [{k: v for k, v in deadline().items() if k not in ("when", "path")}]}, NOW)[0]
    assert "when" not in bare and "path" not in bare, "both optional"
    for over, why in [({"when": "17.01.1970"}, "when"), ({"when": "1970-02-30"}, "when"), ({"when": 17}, "when"),
                      ({"path": "../etc/passwd.md"}, "path"), ({"path": "personal/.scratch/x.md"}, "path"),
                      ({"path": "personal/profile/pass.txt"}, "path"), ({"path": "/abs/x.md"}, "path"),
                      ({"path": "personal/a/b/c/d.md"}, "path"), ({"path": "personal/../x.md"}, "path")]:
        with pytest.raises(feed.Refused, match=why):
            feed.clean({"items": [deadline(**over)]}, NOW)
    with pytest.raises(feed.Refused, match="поля"):
        feed.clean({"items": [item(when="1970-01-17")]}, NOW)


def test_the_hubs_chips_are_taken_by_the_contract_or_the_push_is_refused():
    chips = [{"key": "backup", "text": "бэкап 3 ч назад", "level": "ok"}, {"key": "limit", "text": "лимит 91 %",
                                                                           "level": "bad"}]
    assert feed.clean_chips({"items": [], "chips": chips}) == chips
    assert feed.clean_chips({"items": []}) == [], "a router before chips"
    for bad in ([{"key": "secret", "text": "x", "level": "ok"}], [{"key": "limit", "text": "x", "level": "red"}],
                [{"key": "limit", "text": 5, "level": "ok"}], [{**chips[0], "url": "x"}], [chips[0], chips[0]],
                "x", [chips[0]] * 5):
        with pytest.raises(feed.Refused, match="chips"):
            feed.clean_chips({"items": [], "chips": bad})


TOKEN = "0123456789abcdef0123456789abcdef"


def test_a_persons_row_carries_its_token_and_the_digest_its_two_counts():
    [taken] = feed.clean({"items": [item(1, t=TOKEN)]}, NOW)[0]
    assert taken["t"] == TOKEN
    for bad in ("x" * 32, TOKEN.upper(), TOKEN[:-1], 5):
        with pytest.raises(feed.Refused, match="t"):
            feed.clean({"items": [item(1, t=bad)]}, NOW)
    with pytest.raises(feed.Refused, match="поля"):
        feed.clean({"items": [deadline(t=TOKEN)]}, NOW)
    assert feed.clean_counts({"items": [], "closed_today": 5, "expired_week": 2}) == \
        {"closed_today": 5, "expired_week": 2}
    assert feed.clean_counts({"items": []}) == {"closed_today": 0, "expired_week": 0}, "a router before them"
    for bad in (-1, 1.5, True, "5", feed.COUNT_MAX + 1):
        with pytest.raises(feed.Refused, match="closed_today"):
            feed.clean_counts({"items": [], "closed_today": bad})


def test_a_press_on_her_row_goes_to_the_router_as_a_note_checked_by_its_shape_only(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        assert (await world.owner("waiting", {"items": [item(1, t=TOKEN)], "closed_today": 3, "expired_week": 1}))["ok"]
        answer = await world.get("/api/feed")
        [row] = answer["groups"][0]["rows"]
        assert row["t"] == TOKEN and (answer["closed_today"], answer["expired_week"]) == (3, 1)
        for bad in ({"t": TOKEN, "action": "delete"}, {"t": "../x", "action": "done"}, {"action": "done"}):
            status, _, _ = await world.call("POST", "/api/waiting/press", bad)
            assert status == 400, bad
        status, said, _ = await world.call("POST", "/api/waiting/press", {"t": TOKEN, "action": "later"})
        assert status == 200 and said == {"ok": True}
        [note] = [n for n in world.platform.store.look() if n["kind"] == "waiting-press"]
        assert note["data"] == {"t": TOKEN, "action": "later"}, "the router alone knows what the token names"
        status, _, _ = await world.call("POST", "/api/waiting/press", {"t": TOKEN, "action": "done"}, cookie="x")
        assert status == 401, "the owner's login only"

    faced(tmp_path, body)


def test_a_deadline_ahead_is_under_srokami_only_until_its_time_comes(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        chips = [{"key": "build", "text": "хаб собран", "level": "ok"}]
        later = deadline(1, since=world.clock.now + 2 * DAY, when="1970-01-20")
        due = deadline(2, since=world.clock.now - HOUR, when="1970-01-15", path="work/journal/2025-10-01-report.md",
                       sphere="work")
        assert (await world.owner("waiting", {"items": [item(3), later, due], "chips": chips}))["ok"]
        answer = await world.get("/api/feed")
        [assistant] = answer["groups"]
        assert [r["ref"] for r in assistant["rows"]] == [item(3)["id"], due["id"]] and answer["count"] == 2, \
            "the one days ahead waits for no one yet"
        assert [(d["id"], d["when"], d["path"], d["sphere"]) for d in answer["deadlines"]] == [
            (due["id"], "1970-01-15", "work/journal/2025-10-01-report.md", "work"),
            (later["id"], "1970-01-20", "personal/profile/gym.md", "personal")], "by day"
        assert answer["chips"] == chips
        status, said = await world.post("/owner/waiting", {"items": [], "chips": [{"key": "x"}]})
        assert status == 400 and "chips" in said["error"]
        assert (await world.get("/api/feed"))["chips"] == chips, "a refused push leaves the last in place"
        world.clock.now += 3 * DAY
        assert (await world.get("/api/feed"))["count"] == 3, "its time came"

    faced(tmp_path, body)


def test_the_router_pushes_the_digest_signed_and_a_refused_one_leaves_the_last_in_place(tmp_path):
    async def body(world: World):
        assert (await world.post("/owner/waiting", {"items": []}, {}))[0] == 401, "the router's token only"
        assert (await world.owner("waiting", {"items": [item(1), item(2)], "bot": "retinue_bot"})) == \
            {"ok": True, "items": 2}
        status, said = await world.post("/owner/waiting", {"items": [item(3, kind="mail")]})
        assert status == 400 and "kind" in said["error"]
        status, said = await world.post("/owner/waiting", {"items": [item(4, who="x" * 40_000)]})
        assert status == 413, "a digest has no room for a body"
        items, bot, at = world.platform.feed.digest()
        assert [i["id"] for i in items] == [item(1)["id"], item(2)["id"]] and bot == "retinue_bot"
        assert at == world.clock.now

        await world.owner("waiting", {"items": []})
        assert world.platform.feed.digest()[0] == [], "replaced whole: nothing waits any more"

    scenario(tmp_path, body)


# --- the queue ------------------------------------------------------------------------------------------------------

async def asked(world: World) -> str:
    """A private session whose first run asked two questions (test_questions' ASK)."""
    session = (await world.owner("new", {"input_id": "tg:9", "track": "notes", "text": "куда поехать"}))["session"]
    [run] = world.lanes["private"].runs()
    assert decision(await world.hook("PreToolUse", run, pre_tool_use())) == "defer"
    world.lanes["private"].add(session, run["run"], *LINES)
    world.lanes["private"].exited(session, run["run"])
    await world.poll()
    return session


def test_the_feed_groups_by_action_not_by_source(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        assert await world.get("/api/feed") == {"schema": 1, "count": 0, "groups": [], "bot": "",
                                                 "assistant_at": None, "deadlines": [], "chips": [],
                                                 "closed_today": 0, "expired_week": 0}
        decided, _, card = await deferred_session(world)
        world.clock.now += 60
        question = await asked(world)
        world.clock.now += 60
        chose = await turn(world, "\n## Нашёл три отеля\nдальше…\nОтветы: «Бери первый» · «Ещё варианты»",
                           input_id_="tg:50")
        world.clock.now += 60
        read = await turn(world, "Готово: пост в черновиках.", input_id_="tg:51", took=60)
        world.store.set_sphere(read, "reading")
        world.store.set_sphere(decided, "travel")
        world.cfg.spheres = [Sphere("travel", "Путешествия", "notes", "✈️")]
        await world.owner("waiting", {"items": [item(1), item(2, kind="deadline", who="База", sphere="work")],
                                      "bot": "retinue_bot"})

        got = await world.get("/api/feed")
        assert got["count"] == 6 and got["bot"] == "retinue_bot" and got["assistant_at"] == world.clock.now
        assert [g["title"] for g in got["groups"]] == ["Ответить на вопрос", "Решить", "Выбрать", "Прочитать",
                                                       "От ассистентки"]
        answer, decide, choose, unread, assistant = (g["rows"] for g in got["groups"])
        [row] = answer
        assert (row["session"], row["source"], row["essence"]) == (question, "question", "Где жить?")
        assert row["approval"]["kind"] == "question" and row["card"] == f"{row['approval']['id']}/0"
        [row] = decide
        assert (row["session"], row["source"], row["approval"]["id"]) == (decided, "approval", card["approval"])
        assert row["essence"] == "echo ran >> ran.txt" and row["title"] == "почини"
        assert (row["sphere"], row["sphere_title"], row["sphere_icon"]) == ("travel", "Путешествия", "✈️")
        [row] = choose
        choice = row
        assert row["session"] == chose and row["essence"] == "Нашёл три отеля"
        assert row["replies"] == ["Бери первый", "Ещё варианты"] and isinstance(row["summary"], int)
        [row] = unread
        assert (row["session"], row["essence"], row["sphere_title"]) == (read, "Готово: пост в черновиках.", "reading")
        assert [(r["source"], r["title"], r["essence"], r["session"]) for r in assistant] == \
            [("chat", "Мария", "ответить: нужен ли трансфер", None), ("deadline", "База", item()["title"], None)]

        status, said, _ = await world.call("POST", f"/api/sessions/{chose}/input",
                                           {"reply": {"seq": choice["summary"], "n": 1}})
        assert status == 200 and said["say"] == "Принято."
        assert "Выбрать" not in [g["title"] for g in (await world.get("/api/feed"))["groups"]], \
            "a pressed reply leaves «Выбрать»"
        assert world.lanes["private"].runs()[-1]["prompt"] == "Ещё варианты"

    faced(tmp_path, body)


def test_a_courier_card_waits_a_day_or_until_he_writes_into_its_session(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "retinue", "text": "запушь"}))["session"]
        world.store.add_approval(session, "run", "toolu_push", "courier", "Bash", {"command": "git push origin main"},
                                 "/work/repo", "", "", world.clock.now, state="dropped",
                                 view={"head": ["Курьер · retinue"], "body": "git push origin main"})
        [row] = (await world.get("/api/feed"))["groups"][0]["rows"]
        assert (row["action"], row["source"], row["essence"]) == ("decide", "courier", "git push origin main")
        world.clock.now += DAY + 1
        assert (await world.get("/api/feed"))["count"] == 0, "a day later it is history"
        world.clock.now -= DAY
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "запушил сам"})
        assert (await world.get("/api/feed"))["count"] == 0, "he wrote into the session after it"

    faced(tmp_path, body)


def test_wait_gets_the_first_ten_rows_without_inputs_and_the_count_of_the_rest(tmp_path):
    async def body(world: World):
        decided, _, card = await deferred_session(world)
        await world.owner("waiting", {"items": [item(n) for n in range(12)]})
        got = await world.owner("feed", {})
        assert got["count"] == 13 and got["more"] == 3 and len(got["rows"]) == feed.TELEGRAM_ROWS
        first = got["rows"][0]
        assert first == {"action": "decide", "group": "Решить", "source": "approval", "session": decided,
                         "approval": card["approval"], "card": None, "where": "retinue", "title": "почини",
                         "since": first["since"], "essence": "echo ran >> ran.txt"}
        assert "input" not in json.dumps(got), "a card's input stays in the app"
        assert got["rows"][1]["group"] == "От ассистентки" and got["rows"][1]["session"] is None

    scenario(tmp_path, body)


def test_a_ping_with_replies_waits_under_choose_until_typed_over_or_a_newer_ping(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = await turn(world, "Готово.\nОтветы: «Да» · «Нет»")
        [row] = (await world.get("/api/feed"))["groups"][0]["rows"]
        assert (row["action"], row["replies"]) == ("choose", ["Да", "Нет"])
        await world.owner("input", {"input_id": "tg:2", "session": session, "text": "Нет, по-другому"})
        assert (await world.get("/api/feed"))["count"] == 0, "he typed instead: the replies closed"

    faced(tmp_path, body)
