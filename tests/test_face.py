"""The face API against the platform of `test_platformd` (fake lanes, the router's calls over HTTP): logins by code and
by Telegram's signature, the cookie, the header and origin a POST needs, the rate limit, the snapshot and the events
that follow it, the stream, and a card decided in the face under exactly the rules of a Telegram press."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlencode

import pytest
from aiohttp import DummyCookieJar
from aiohttp.test_utils import TestClient, TestServer

from retinue import face, platformd, sessions
from retinue.face import COOKIE, Face, frames, init_data_ok, normal_code
from test_platformd import (CALL, DAY, HOUR, World, decision, deferred_session, fixture, pre_tool_use,
                            short_wait)  # noqa: F401 — the fixture is autouse

ORIGIN = "https://app.example.test"
BOT_TOKEN = "123456:secret-bot-token"
WEBAPP_KEY = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).hexdigest()  # what setup.sh derives
OWNER = 42
POST = {"X-Retinue": "1", "Origin": ORIGIN}


class FaceWorld(World):
    """The platform with its face on a second test server."""

    async def up(self) -> None:
        await super().up()
        self.cfg.face_origin, self.cfg.webapp_key, self.cfg.owner_tg = ORIGIN, WEBAPP_KEY, OWNER
        self.face = Face(self.platform)
        self.api = TestClient(TestServer(self.face.app()), cookie_jar=DummyCookieJar())
        await self.api.start_server()
        self.cookie = ""

    async def down(self) -> None:
        self.face.close()
        await self.api.close()
        await super().down()

    async def call(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None,
                   cookie: str | None = None):
        given = dict(POST if headers is None and method == "POST" else headers or {})
        if (cookie if cookie is not None else self.cookie):
            given["Cookie"] = f"{COOKIE}={cookie if cookie is not None else self.cookie}"
        response = await self.api.request(method, path, json=body, headers=given)
        return response.status, await response.json(), response

    async def get(self, path: str) -> dict:
        status, data, _ = await self.call("GET", path)
        assert status == 200, data
        return data

    async def login(self) -> str:
        code = (await self.owner("login", {}))["code"]
        status, data, response = await self.call("POST", "/api/auth/code", {"code": code})
        assert status == 200, data
        self.cookie = response.cookies[COOKIE].value
        return self.cookie


def faced(tmp_path, body, **start):
    async def go():
        world = FaceWorld(tmp_path)
        await world.start(**start)
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


def signed(fields: dict, key: str = WEBAPP_KEY) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return urlencode({**fields, "hash": hmac.new(bytes.fromhex(key), check.encode(), hashlib.sha256).hexdigest()})


def init_data(now: float, user: int = OWNER, **extra) -> str:
    return signed({"auth_date": str(int(now)), "query_id": "AAH", "signature": "sig",
                   "user": json.dumps({"id": user, "first_name": "A"}), **extra})


def input_id() -> str:
    return f"face:{uuid.uuid4()}"


# --- logins ---------------------------------------------------------------------------------------------------------

def test_a_code_from_app_logs_in_once_and_is_kept_only_as_its_hash(tmp_path):
    async def body(world: FaceWorld):
        assert (await world.call("GET", "/api/meta"))[0] == 401, "nothing without a login"
        login = await world.owner("login", {})
        code = login["code"]
        assert login["url"] == f"{ORIGIN}/login#{code}"
        assert len(code) == 10 and set(code) <= set(face.CODE_ALPHABET)
        stored = [row[0] for row in world.store.db.execute("SELECT hash FROM login_codes")]
        assert stored == [hashlib.sha256(code.encode()).hexdigest()], "the code itself is nowhere"

        typed = f"{code[:5].lower()}-{code[5:].lower()} "
        status, data, response = await world.call("POST", "/api/auth/code", {"code": typed})
        assert (status, data) == (200, {"ok": True})
        cookie = response.cookies[COOKIE]
        assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict" and cookie["path"] == "/"
        assert int(cookie["max-age"]) == 30 * DAY and "domain" not in cookie.output().lower()
        assert len(cookie.value) >= 43, "32 random bytes"
        [(kept, via)] = world.store.db.execute("SELECT hash, via FROM logins").fetchall()
        assert kept == hashlib.sha256(cookie.value.encode()).hexdigest() and via == "code"

        again, data, _ = await world.call("POST", "/api/auth/code", {"code": code})
        assert again == 401 and data["error"] == face.CODE_FAILED, "single use"
        meta = (await world.call("GET", "/api/meta", cookie=cookie.value))[1]
        assert meta == {"schema": 1, "owner_ok": True, "face_origin": ORIGIN,
                        "tracks": [{"name": "retinue", "lane": "code", "kind": "code"},
                                   {"name": "notes", "lane": "private", "kind": "private"}], "general": "notes",
                        "spheres": [],
                        "speech": False}
        assert (await world.call("GET", "/api/meta", cookie="forged"))[0] == 401

    faced(tmp_path, body)


def test_a_code_expires_after_ten_minutes_and_a_new_one_voids_the_old(tmp_path):
    async def body(world: FaceWorld):
        old = (await world.owner("login", {}))["code"]
        new = (await world.owner("login", {}))["code"]
        assert (await world.call("POST", "/api/auth/code", {"code": old}))[0] == 401, "only the last code works"
        world.clock.now += face.CODE_TTL_S + 1
        assert (await world.call("POST", "/api/auth/code", {"code": new}))[0] == 401, "expired"
        fresh = (await world.owner("login", {}))["code"]
        assert (await world.call("POST", "/api/auth/code", {"code": fresh}))[0] == 200

    faced(tmp_path, body)


def test_without_a_face_origin_app_says_so(tmp_path):
    async def body(world: FaceWorld):
        world.cfg.face_origin = ""
        answer = await world.owner("login", {})
        assert "code" not in answer and "RETINUE_FACE_ORIGIN" in answer["error"]
        assert world.store.db.execute("SELECT COUNT(*) FROM login_codes").fetchone()[0] == 0

    faced(tmp_path, body)


def test_the_mini_app_logs_in_by_telegrams_signature_for_the_owner_only_and_only_fresh(tmp_path):
    async def body(world: FaceWorld):
        now = world.clock.now
        tampered = init_data(now).replace("query_id=AAH", "query_id=AAX")
        for bad in (tampered, init_data(now, user=7), init_data(now - face.INIT_DATA_TTL_S - 1),
                    init_data(now).split("&hash=")[0] + "&hash=" + "%D1%8F" * 64,
                    init_data(now + 3600), signed({"auth_date": str(int(now)), "user": json.dumps({"id": OWNER})},
                                                  key="00" * 32),
                    "", "hash=", "garbage"):
            status, data, _ = await world.call("POST", "/api/auth/telegram", {"init_data": bad})
            assert (status, data["error"]) == (401, face.TELEGRAM_FAILED), bad
        status, data, response = await world.call("POST", "/api/auth/telegram", {"init_data": init_data(now - 60)})
        assert (status, data) == (200, {"ok": True})
        assert world.store.db.execute("SELECT via FROM logins").fetchall() == [("telegram",)]
        world.cookie = response.cookies[COOKIE].value
        assert (await world.call("GET", "/api/sessions"))[0] == 200

    faced(tmp_path, body)


def test_init_data_is_checked_as_telegram_signs_it():
    now = time.time()
    assert init_data_ok(init_data(now), WEBAPP_KEY, OWNER, now)
    assert not init_data_ok(init_data(now), "", OWNER, now), "no key, no Mini App login"
    assert not init_data_ok(init_data(now), WEBAPP_KEY, 0, now), "no owner, no Mini App login"
    assert not init_data_ok(init_data(now) + "&auth_date=1", WEBAPP_KEY, OWNER, now), "a field twice"
    assert not init_data_ok(signed({"auth_date": str(int(now)), "user": "[42]"}), WEBAPP_KEY, OWNER, now)
    assert not init_data_ok(signed({"auth_date": str(int(now)), "user": '{"id": true}'}), WEBAPP_KEY, 1, now)
    assert not init_data_ok(signed({"auth_date": str(int(now))}).split("&hash=")[0] + "&hash=" + "я" * 64,
                            WEBAPP_KEY, OWNER, now), "not a hex hash"
    assert not init_data_ok("user=" + "[" * 100_000 + "&auth_date=1&hash=" + "0" * 64, WEBAPP_KEY, OWNER, now)
    assert normal_code(" ab-cd efgh23 ") == "ABCDEFGH23"


def test_a_post_needs_the_header_and_no_foreign_origin_and_a_get_needs_neither(tmp_path):
    async def body(world: FaceWorld):
        code = (await world.owner("login", {}))["code"]
        assert (await world.call("POST", "/api/auth/code", {"code": code}, headers={"Origin": ORIGIN}))[0] == 403
        assert (await world.call("POST", "/api/auth/code", {"code": code},
                                 headers={"X-Retinue": "1", "Origin": "https://evil.example"}))[0] == 403
        assert (await world.call("POST", "/api/auth/code", {"code": code},
                                 headers={"X-Retinue": "1", "Origin": "null"}))[0] == 403
        assert world.store.db.execute("SELECT used FROM login_codes").fetchone()[0] is None, "refused before use"
        assert (await world.call("POST", "/api/auth/code", {"code": code}, headers={"X-Retinue": "1"}))[0] == 200, \
            "no Origin header: a same-origin client that sends none"
        await world.login()
        status, _, _ = await world.call("POST", "/api/sessions", {"track": "notes", "text": "x", "input_id": input_id()},
                                        headers={"Origin": ORIGIN})
        assert status == 403 and world.store.face_sessions(world.clock.now) == []
        assert (await world.call("GET", "/api/sessions", headers={}))[0] == 200

    faced(tmp_path, body)


def test_ten_failed_logins_close_logins_for_ten_minutes(tmp_path):
    async def body(world: FaceWorld):
        for _ in range(face.FAIL_LIMIT):
            assert (await world.call("POST", "/api/auth/code", {"code": "WRONGWRONG"}))[0] == 401
        code = (await world.owner("login", {}))["code"]
        status, data, _ = await world.call("POST", "/api/auth/code", {"code": code})
        assert status == 429 and "ten minutes" in data["error"], "even the right code"
        assert (await world.call("POST", "/api/auth/telegram", {"init_data": init_data(world.clock.now)}))[0] == 429
        world.clock.now += face.FAIL_BLOCK_S - 1
        assert (await world.call("POST", "/api/auth/code", {"code": code}))[0] == 429
        world.clock.now += 2
        assert world.store.db.execute("SELECT used FROM login_codes").fetchone()[0] is None, "never spent"
        code = (await world.owner("login", {}))["code"]  # the first one has expired meanwhile
        assert (await world.call("POST", "/api/auth/code", {"code": code}))[0] == 200

    faced(tmp_path, body)


def test_failures_spread_over_more_than_ten_minutes_do_not_close_logins(tmp_path):
    async def body(world: FaceWorld):
        for _ in range(face.FAIL_LIMIT - 1):
            await world.call("POST", "/api/auth/code", {"code": "WRONGWRONG"})
        world.clock.now += face.FAIL_WINDOW_S + 1
        assert (await world.call("POST", "/api/auth/code", {"code": "WRONGWRONG"}))[0] == 401
        code = (await world.owner("login", {}))["code"]
        assert (await world.call("POST", "/api/auth/code", {"code": code}))[0] == 200

    faced(tmp_path, body)


def test_a_login_lasts_thirty_days_from_its_last_use_and_logout_ends_it(tmp_path):
    async def body(world: FaceWorld):
        cookie = await world.login()
        world.clock.now += 2 * DAY
        _, _, response = await world.call("GET", "/api/meta")
        assert response.cookies[COOKIE].value == cookie, "the same cookie, set again for 30 more days"
        expires = world.store.login_expires(hashlib.sha256(cookie.encode()).hexdigest(), world.clock.now)
        assert expires == world.clock.now + 30 * DAY
        _, _, quiet = await world.call("GET", "/api/meta")
        assert COOKIE not in quiet.cookies, "at most once a day"
        world.clock.now += 29 * DAY
        assert (await world.call("GET", "/api/meta"))[0] == 200
        world.clock.now += 31 * DAY
        assert (await world.call("GET", "/api/meta"))[0] == 401

        cookie = await world.login()
        status, _, response = await world.call("POST", "/api/auth/logout")
        assert status == 200 and response.cookies[COOKIE]["max-age"] == "0"
        assert (await world.call("GET", "/api/meta", cookie=cookie))[0] == 401

    faced(tmp_path, body)


# --- snapshot and events --------------------------------------------------------------------------------------------

def apply(model: dict, kind: str, data: dict) -> None:
    """A client's reducer: every event an upsert of one whole object by its key."""
    if kind == "session":
        model["sessions"][data["id"]] = data
    elif kind == "item":
        model["items"][(data["session"], data["turn"], data["n"])] = data
    elif kind == "items_reset":
        model["items"] = {k: v for k, v in model["items"].items()
                          if not (k[0] == data["session"] and k[1] == data["turn"] and k[2] >= data["count"])}
    elif kind == "approval":
        model["approvals"][data["id"]] = data
    elif kind == "summary":
        model["summaries"][f"{data['session']}/{data['seq']}"] = data
    elif kind == "input":
        model["inputs"][f"{data['session']}/{data['input_id']}"] = data
    else:
        raise AssertionError(kind)


def replayed(store: sessions.PlatformStore, since: int = 0) -> dict:
    model = {"sessions": {}, "items": {}, "approvals": {}, "summaries": {}, "inputs": {}}
    for _, kind, data in store.events_after(since, 100_000):
        apply(model, kind, json.loads(data))
    return model


def test_a_turn_counts_from_its_start_not_from_the_last_card_it_waited_on(tmp_path):
    """«Working» on «Today»: `turn_started` is set when a turn begins and stays through a card's resume, which moves
    `at`; a new turn after idle sets it again."""
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        started = world.store.face_session(session)["turn_started"]
        world.clock.now += HOUR
        await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        row = world.store.face_session(session)
        assert row["state"] == "active" and row["at"] == world.clock.now
        assert row["turn_started"] == started, "a resume after a card is the same turn"
        world.platform._set_state(session, "idle", world.clock.now)
        world.clock.now += 60
        world.platform._set_state(session, "active", world.clock.now)
        assert world.store.face_session(session)["turn_started"] == world.clock.now, "a new turn"

    faced(tmp_path, body)


def test_the_snapshot_has_the_shapes_the_face_reads(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        listed = await world.get("/api/sessions")
        assert listed["schema"] == 1 and listed["cursor"] == world.store.cursor() > 0
        [row] = listed["sessions"]
        assert row == {"id": session, "track": "retinue", "lane": "code", "kind": "code", "state": "waiting",
                       "created": row["created"], "at": row["at"], "title": "почини", "waiting": 1,
                       "pinned": False, "parent": None, "sphere": None, "main": False, "fork_of": None,
                       "unread": row["unread"], "base": 0, "push": 0, "turn_started": row["created"],
                       "side": False, "sub_of": None, "children": 0, "queued": False}

        snapshot = await world.get(f"/api/sessions/{session}")
        assert set(snapshot) == {"schema", "cursor", "session", "items", "approvals", "summaries", "inputs", "files",
                                 "base_cards", "push_cards", "about"}
        assert snapshot["about"] == world.store.face_about(session)
        assert set(snapshot["about"]) == {"cwd", "context_tokens", "model", "cli_version", "permission_mode", "mcp"}
        assert snapshot["inputs"] == [{"session": session, "input_id": "tg:1", "text": "почини", "at": row["created"],
                                       "turn": 0, "n": 0, "attachments": []}]
        assert snapshot["session"] == row
        assert [(i["turn"], i["n"]) for i in snapshot["items"]] == sorted((i["turn"], i["n"]) for i in snapshot["items"])
        assert all(set(i) == {"session", "turn", "n", "kind", "data"} for i in snapshot["items"])
        assert snapshot["items"][-1]["kind"] == "result"
        [approval] = snapshot["approvals"]
        assert set(approval) == {"id", "session", "tool_use_id", "kind", "state", "tool", "input", "cwd", "digest",
                                 "view", "created", "at"}
        assert approval["tool_use_id"] == CALL["id"]
        assert await world.get(f"/api/approvals/{approval['id']}") == {"approval": approval}, "for /a/<id>"
        assert (await world.call("GET", "/api/approvals/00000000"))[0] == 404
        assert (approval["id"], approval["kind"], approval["state"], approval["tool"]) == \
            (card["approval"], "approval", "shown", "Bash")
        assert approval["input"] == CALL["input"] and approval["view"] == card["data"]["view"]
        assert approval["digest"] == world.store.approval(card["approval"])["digest"]
        assert snapshot["summaries"] == []
        assert (await world.call("GET", "/api/sessions/nope0000"))[0] == 404
        diff = await world.get(f"/api/sessions/{session}/diff")
        assert diff == {"cwd": diff["cwd"], "stat": "", "diff": "", "truncated": False, "approximate_files": []}, \
            "a session that edited nothing"

    faced(tmp_path, body)


def test_a_session_of_a_retired_lane_is_listed_and_readable_and_takes_nothing_in(tmp_path):
    """lane-code and lane-web retired into the room: their sessions stay in the list and open as they were; a message,
    a decision or an answer gets one Russian line and goes nowhere — no run, no traceback."""
    async def body(world: FaceWorld):
        session, _, card = await deferred_session(world)
        await world.down()
        world.cfg = dataclasses.replace(world.cfg, lanes={"private": world.cfg.lanes["private"]})
        await world.up()
        await world.login()
        [row] = (await world.get("/api/sessions"))["sessions"]
        assert (row["id"], row["lane"], row["title"]) == (session, "code", "почини")
        snapshot = await world.get(f"/api/sessions/{session}")
        assert snapshot["items"] and snapshot["approvals"], "its transcript and its card, as they were"
        status, said, _ = await world.call("POST", f"/api/sessions/{session}/input",
                                           {"text": "more", "input_id": input_id()})
        assert status == 200 and said["say"] == platformd.RETIRED
        approval = world.store.approval(card["approval"])
        status, decided, _ = await world.call("POST", f"/api/approvals/{approval['id']}/decide",
                                              {"action": "approve", "digest": approval["digest"]})
        assert status == 200 and decided["toast"] == platformd.RETIRED
        assert world.store.approval(card["approval"])["state"] == "shown" and world.store.queued() == []
        assert (await world.owner("input", {"input_id": "tg:9", "session": session, "text": "hey"})) == \
            {"say": platformd.RETIRED}, "the same line in Telegram"
        assert len(world.lanes["code"].runs()) == 1, "nothing ran"

    faced(tmp_path, body)


def test_a_long_title_is_cut_and_an_empty_first_input_names_the_track(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        quiet = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": ""}))["session"]
        long = (await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "z" * 200}))["session"]
        titles = {s["id"]: s["title"] for s in (await world.get("/api/sessions"))["sessions"]}
        assert titles == {quiet: "notes", long: "z" * 80}
        await world.owner("input", {"input_id": "tg:3", "session": quiet, "text": "now there is one"})
        assert world.store.face_session(quiet)["title"] == "now there is one"

    faced(tmp_path, body)


def test_every_change_the_face_shows_is_an_event_and_replaying_them_gives_the_snapshot(tmp_path):
    """Sessions, items as they grow, a card shown and decided, a summary: a client that starts from nothing and
    applies the events in order holds exactly what the snapshot says."""
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        await world.owner("press", {"button": card["data"]["buttons"][0][1]})
        resume = world.lanes["code"].runs()[-1]
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "allow"
        await world.hook("PostToolUse", resume, pre_tool_use())
        world.lanes["code"].add(session, resume["run"], *fixture("resumed"))
        await world.poll()
        notes = (await world.owner("new", {"input_id": "tg:9", "track": "notes", "text": "a note"}))["session"]

        model = replayed(world.store)
        for sid in (session, notes):
            snapshot = await world.get(f"/api/sessions/{sid}")
            assert model["sessions"][sid] == snapshot["session"]
            assert [v for k, v in sorted(model["items"].items()) if k[0] == sid] == snapshot["items"]
            assert [v for v in model["approvals"].values() if v["session"] == sid] == snapshot["approvals"]
            assert [v for v in model["summaries"].values() if v["session"] == sid] == snapshot["summaries"]
            assert [v for v in model["inputs"].values() if v["session"] == sid] == snapshot["inputs"]
        assert model["approvals"][card["approval"]]["state"] == "executed"
        [summary] = [v for v in model["summaries"].values() if v["session"] == session]
        assert summary["head"] == f"Result · retinue · session {session}" and summary["at"] == world.clock.now
        last = max(k[1] for k in model["items"] if k[0] == session)
        assert summary["turn"] == last > 0, "placed after the turn it ends: the resumed one"
        keys = [key for (key,) in world.store.db.execute("SELECT key FROM events WHERE kind = 'summary'")]
        assert keys == [f"{session}/{summary['seq']}"]
        kinds = [kind for _, kind, _ in world.store.events_after(0, 100_000)]
        assert set(kinds) == {"session", "item", "approval", "summary", "input"}

    faced(tmp_path, body)


def test_about_is_read_from_the_newest_top_level_init_line_and_the_sessions_row(tmp_path):
    """«Summary»: what the platform was told, nothing more — a subagent's init line is not the session's."""
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    assert store.face_about(session) == {"cwd": None, "context_tokens": None, "model": None, "cli_version": None,
                                         "permission_mode": None, "mcp": []}
    lines = [
        {"type": "system", "subtype": "init", "cwd": "/work/old", "model": "claude-old", "session_id": "s"},
        {"type": "system", "subtype": "init", "cwd": "/work/repo", "model": "claude-opus-5-5", "session_id": "s",
         "claude_code_version": "2.1.304", "permissionMode": "default",
         "mcp_servers": [{"name": "retinue", "status": "connected"}, {"name": "github", "status": "failed"}, "junk"]},
        {"type": "system", "subtype": "init", "cwd": "/elsewhere", "model": "sub", "parent_tool_use_id": "toolu_1"},
    ]
    for seq, line in enumerate(lines, 1):
        store.add_raw("code", seq, session, "r1", json.dumps(line), 0.0)
    store.set_context(session, 84_000)
    assert store.face_about(session) == {
        "cwd": "/work/repo", "context_tokens": 84_000, "model": "claude-opus-5-5", "cli_version": "2.1.304",
        "permission_mode": "default", "mcp": [{"name": "retinue", "status": "connected"},
                                              {"name": "github", "status": "failed"}]}


def test_an_unchanged_reprojection_writes_nothing_and_a_shrunk_one_resets_the_turn(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    for seq, line in enumerate(fixture("reads"), 1):
        if (turn := store.add_raw("code", seq, session, "r1", line, 0.0)) is not None:
            store.reproject(session, turn)
    before = store.cursor()
    count = len(store.face_items(session))
    store.reproject(session, 0)
    assert store.cursor() == before, "the same items again tell the face nothing"

    last = store.db.execute("SELECT MAX(id) FROM raw").fetchone()[0]
    store.db.execute("DELETE FROM raw WHERE id = ?", (last,))  # the turn's result line gone: one item fewer
    store.reproject(session, 0)
    events = [(kind, json.loads(data)) for _, kind, data in store.events_after(before)]
    assert events == [("items_reset", {"session": session, "turn": 0, "count": count - 1})]
    model = replayed(store)
    assert [v for _, v in sorted(model["items"].items())] == store.face_items(session)


def test_a_card_never_shown_is_not_the_faces_and_a_courier_card_is(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"))
    session = store.new_session("retinue", "code", "code", 0.0)
    pending = store.add_approval(session, "r1", "t1", "approval", "Bash", {"command": "ls"}, "/work", "", "d", 1.0)
    store.move(pending, ("pending",), "dropped", 2.0)  # a parallel call the CLI dropped before any card
    courier = store.add_approval(session, "r1", "t2", "courier", "Bash", {"command": "git push"}, "/work", "", "", 3.0,
                                 state="dropped", view={"head": ["Courier"], "body": "git push", "status": "x"})
    assert [a["id"] for a in store.face_approvals(session)] == [courier]
    assert [json.loads(d)["id"] for _, k, d in store.events_after(0) if k == "approval"] == [courier]


def test_old_events_are_pruned_by_number_and_a_cursor_before_them_is_gone(tmp_path):
    store = sessions.PlatformStore(str(tmp_path / "p.sqlite"), clock=lambda: 0.0)
    session = store.new_session("retinue", "code", "code", 0.0)
    store.update_session(session, 1.0, state="idle")
    old = store.cursor()
    store.clock = lambda: 8 * DAY
    store.update_session(session, 8 * DAY, state="closed")
    store.prune(8 * DAY)
    assert [i for i, _, _ in store.events_after(0)] == [old + 1]
    assert store.events_gone(0) and store.events_gone(old - 1) and not store.events_gone(old)
    assert store.events_gone(old + 2), "a cursor this database never gave"


# --- the stream -----------------------------------------------------------------------------------------------------

class Reader:
    """An SSE client: frames as dicts, comments as {'comment': ...}."""

    def __init__(self, response) -> None:
        self.response = response

    async def frame(self, timeout: float = 2.0) -> dict:
        raw = await asyncio.wait_for(self.response.content.readuntil(b"\n\n"), timeout)
        frame: dict = {}
        for line in raw.decode().strip("\n").split("\n"):
            if line.startswith(":"):
                frame["comment"] = line[1:].strip()
            else:
                name, _, value = line.partition(": ")
                frame[name] = value
        return frame

    async def events(self, n: int) -> list[dict]:
        out = []
        while len(out) < n:
            frame = await self.frame()
            if "event" in frame:
                out.append(frame)
        return out


async def stream(world: FaceWorld, headers: dict | None = None, query: str = "") -> tuple[int, Reader]:
    headers = {"Cookie": f"{COOKIE}={world.cookie}", **(headers or {})}
    response = await world.api.get(f"/api/events{query}", headers=headers)
    reader = Reader(response)
    if response.status == 200:
        assert await reader.frame() == {"retry": str(face.RETRY_MS)}
    return response.status, reader


def test_a_stream_resumes_after_its_last_event_id_and_one_carries_every_session(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        a = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "the first"}))["session"]
        b = (await world.owner("new", {"input_id": "tg:2", "track": "retinue", "text": "the second"}))["session"]
        rows = world.store.events_after(0, 1000)
        middle = rows[len(rows) // 2][0]
        status, reader = await stream(world, {"Last-Event-ID": str(middle)}, "?since=0")
        assert status == 200
        got = await reader.events(len(rows) - len(rows) // 2 - 1)
        assert [int(f["id"]) for f in got] == [i for i, _, _ in rows if i > middle], "Last-Event-ID wins over since"
        assert [(f["event"], json.loads(f["data"])) for f in got] == \
            [(k, json.loads(d)) for i, k, d in rows if i > middle]

        world.store.update_session(a, world.clock.now, state="idle")
        world.store.update_session(b, world.clock.now, state="idle")
        live = await reader.events(2)
        assert [json.loads(f["data"])["id"] for f in live] == [a, b], "both panels over one connection"
        reader.response.close()

        status, since = await stream(world, query=f"?since={live[0]['id']}")
        assert [f["id"] for f in await since.events(1)] == [live[1]["id"]]
        since.response.close()

    faced(tmp_path, body)


def test_a_stream_from_a_pruned_cursor_is_gone_and_a_bad_one_refused(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "x"})
        old = world.store.cursor()
        world.clock.now += 8 * DAY
        await world.owner("new", {"input_id": "tg:2", "track": "notes", "text": "y"})
        world.platform.tick()
        assert (await stream(world, query="?since=0"))[0] == 410
        assert (await stream(world, {"Last-Event-ID": str(old - 1)}))[0] == 410
        assert (await stream(world, query="?since=abc"))[0] == 400
        assert (await stream(world, query=f"?since={world.store.cursor() + 5}"))[0] == 410
        status, reader = await stream(world, query=f"?since={old}")
        assert status == 200 and len(await reader.events(1)) == 1
        reader.response.close()
        assert (await world.call("GET", "/api/events", cookie=""))[0] == 401

    faced(tmp_path, body)


def test_a_quiet_stream_is_pinged_and_closing_ends_every_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(face, "PING_S", 0.05)

    async def body(world: FaceWorld):
        await world.login()
        _, first = await stream(world)
        _, second = await stream(world)
        assert await first.frame() == {"comment": "ping"} and await second.frame() == {"comment": "ping"}
        assert len(world.face.hub.streams) == 2
        world.face.close()
        for reader in (first, second):
            while True:
                chunk = await asyncio.wait_for(reader.response.content.read(1024), 2)
                if not chunk:
                    break
        assert world.face.hub.streams == {}

    faced(tmp_path, body)


def test_a_stream_that_takes_nothing_holds_back_no_one_and_the_oldest_gives_way_to_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(face, "MAX_STREAMS", 2)

    async def body(world: FaceWorld):
        await world.login()
        _, dead = await stream(world)  # a phone that went away without closing
        _, idle = await stream(world)  # never read again
        status, reader = await stream(world)
        assert status == 200 and len(world.face.hub.streams) == 2, "the cap holds by closing the oldest"
        assert await asyncio.wait_for(dead.response.content.read(), 2) == b"", "the oldest is closed"
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": ""}))["session"]
        start = time.monotonic()
        for i in range(500):
            world.store.update_session(session, world.clock.now + i, state="idle")
        assert time.monotonic() - start < 5
        got = await reader.events(1)
        assert json.loads(got[0]["data"])["id"] == session
        idle.response.close()
        reader.response.close()

    faced(tmp_path, body)


def test_a_stream_ends_with_its_login(tmp_path, monkeypatch):
    monkeypatch.setattr(face, "PING_S", 0.05)

    async def body(world: FaceWorld):
        first = await world.login()
        _, out = await stream(world)
        second = await world.login()
        _, other = await stream(world)
        world.cookie = first
        assert (await world.call("POST", "/api/auth/logout"))[0] == 200
        assert await asyncio.wait_for(out.response.content.read(), 2) == b"", "a logout ends its streams at once"
        assert await other.frame() == {"comment": "ping"}, "another login's stream goes on"
        world.clock.now += 31 * DAY
        assert await asyncio.wait_for(other.response.content.read(), 2) == b"", "an expired login, at the next ping"
        assert world.face.hub.streams == {} and second

    faced(tmp_path, body)


def test_frames_put_each_text_piece_after_the_event_it_followed_and_join_a_sessions_pieces():
    from collections import deque

    rows = [(5, "item", '{"n": 0}'), (6, "item", '{"n": 1}')]
    deltas = deque([(4, "s1", "Hel"), (4, "s1", "lo"), (5, "s2", "Hi"), (5, "s1", "!"), (9, "s1", "later")])
    text, cursor = frames(rows, deltas, 4)
    assert cursor == 6
    assert text == ('event: delta\ndata: {"session": "s1", "text": "Hello"}\n\n'
                    'id: 5\nevent: item\ndata: {"n": 0}\n\n'
                    'event: delta\ndata: {"session": "s2", "text": "Hi"}\n\n'
                    'event: delta\ndata: {"session": "s1", "text": "!"}\n\n'
                    'id: 6\nevent: item\ndata: {"n": 1}\n\n')
    assert list(deltas) == [(9, "s1", "later")], "after an event not read yet: it waits"


def partial(text: str, parent: str | None = None) -> str:
    return json.dumps({"type": "stream_event", "parent_tool_use_id": parent, "session_id": "x",
                       "event": {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}})


def test_partial_lines_reach_the_stream_as_text_and_are_never_stored(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "hello"}))["session"]
        [run] = world.lanes["private"].runs()
        _, reader = await stream(world)
        start = json.dumps({"type": "stream_event", "parent_tool_use_id": None,
                            "event": {"type": "message_start", "message": {}}})
        message = json.dumps({"type": "assistant", "parent_tool_use_id": None,
                              "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello!"}]}})
        world.lanes["private"].add(session, run["run"], start, partial("Hel"), partial("lo"),
                                   partial("someone else's", parent="toolu_sub"), partial("!"), message)
        await world.poll()
        frames_ = []
        while not any(f.get("event") == "item" for f in frames_):
            frames_.append(await reader.frame())
        live = [f for f in frames_ if f.get("event") in ("delta", "item")]
        assert live[0] == {"event": "delta", "data": json.dumps({"session": session, "text": "Hello!"},
                                                                ensure_ascii=False)}, "joined, no id, no subagent"
        assert json.loads(live[-1]["data"])["data"] == {"role": "assistant", "text": "Hello!"}
        stored = [row[0] for row in world.store.db.execute("SELECT line FROM raw")]
        assert stored == [message], "no stream_event in raw"
        assert [i["kind"] for i in world.store.face_items(session)] == ["message"]
        assert all(i["given"] for i in world.lanes["private"].journal), "confirmed to the lane like any line"
        reader.response.close()

    faced(tmp_path, body)


def test_a_partial_line_after_a_deferred_call_leaves_the_card_pressable(tmp_path):
    async def body(world: FaceWorld):
        session, run, card = await deferred_session(world)
        world.lanes["code"].add(session, run["run"], partial("late"))
        await world.poll()
        assert (await world.owner("press", {"button": card["data"]["buttons"][0][1]}))["toast"] == "Allowed."

    faced(tmp_path, body)


# --- what the owner does --------------------------------------------------------------------------------------------

def test_a_card_decided_in_the_face_is_decided_once_and_its_telegram_card_retired(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        approval = card["approval"]
        digest = world.store.approval(approval)["digest"]
        status, wrong, _ = await world.call("POST", f"/api/approvals/{approval}/decide",
                                            {"action": "approve", "digest": "0" * 64})
        assert status == 200 and wrong["toast"] == "The card has expired — no decision taken."
        assert wrong["approval"]["state"] == "shown", "a digest not the card's decides nothing"

        status, decided, _ = await world.call("POST", f"/api/approvals/{approval}/decide",
                                              {"action": "approve", "digest": digest})
        assert status == 200 and decided["toast"] == "Allowed."
        assert decided["approval"]["state"] == "granted"
        assert decided["approval"]["view"]["status"] == "Allowed — running."
        [update] = world.notes("update")
        assert update["approval"] == approval and update["data"] == {"view": decided["approval"]["view"],
                                                                     "buttons": []}
        assert len(world.lanes["code"].runs()) == 2, "one resume"

        again = (await world.call("POST", f"/api/approvals/{approval}/decide", {"action": "reject", "digest": digest}))[1]
        assert again["toast"] == "The card has expired — no decision taken." and again["approval"]["state"] == "granted"
        pressed = await world.owner("press", {"button": card["data"]["buttons"][1][1]})
        assert pressed == {"toast": "The card has expired — no decision taken."}, "the Telegram button lost"
        assert len(world.lanes["code"].runs()) == 2
        resume = world.lanes["code"].runs()[-1]
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "allow"

    faced(tmp_path, body)


def test_a_card_pressed_in_telegram_is_decided_there_and_the_face_hears_it(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        approval = card["approval"]
        digest = world.store.approval(approval)["digest"]
        _, reader = await stream(world, query=f"?since={world.store.cursor()}")
        assert (await world.owner("press", {"button": card["data"]["buttons"][1][1]}))["toast"] == "Denied."
        events = []
        while not any(f["event"] == "session" and json.loads(f["data"])["waiting"] == 0 for f in events):
            events += await reader.events(1)
        [event] = [f for f in events if f["event"] == "approval"]
        assert json.loads(event["data"])["state"] == "rejected", "the card, then its session waiting on nothing"
        decided = (await world.call("POST", f"/api/approvals/{approval}/decide",
                                    {"action": "approve", "digest": digest}))[1]
        assert decided["toast"] == "The card has expired — no decision taken." and decided["approval"]["state"] == \
            "rejected"
        [update] = world.notes("update")  # the press edits its card at once; the note keeps the latest state last
        assert update["data"] == {"view": {**card["data"]["view"], "status": "Denied."}, "buttons": []}
        assert len(world.lanes["code"].runs()) == 2
        reader.response.close()

    faced(tmp_path, body)


def test_a_card_decided_in_the_face_an_hour_after_a_restart_runs_once(tmp_path):
    """E4 from the face."""
    async def body(world: FaceWorld):
        session, _, card = await deferred_session(world)
        digest = world.store.approval(card["approval"])["digest"]
        await world.down()
        world.clock.now += HOUR
        await world.up()
        await world.login()
        decided = (await world.call("POST", f"/api/approvals/{card['approval']}/decide",
                                    {"action": "approve", "digest": digest}))[1]
        assert decided["toast"] == "Allowed."
        resume = world.lanes["code"].runs()[-1]
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) == "allow"
        assert decision(await world.hook("PreToolUse", resume, pre_tool_use())) != "allow"

    faced(tmp_path, body)


def test_decide_refuses_what_is_not_a_decision_on_a_card(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, card = await deferred_session(world)
        path = f"/api/approvals/{card['approval']}/decide"
        assert (await world.call("POST", path, {"action": "open", "digest": "x"}))[0] == 400
        assert (await world.call("POST", path, {"action": "approve"}))[0] == 400
        assert (await world.call("POST", "/api/approvals/00000000/decide", {"action": "approve", "digest": "x"}))[0] \
            == 404
        courier = world.store.add_approval(session, "r", "t", "courier", "Bash", {"command": "git push"}, "/w", "", "",
                                           world.clock.now, state="dropped", view={"head": ["Courier"], "body": "x"})
        answer = (await world.call("POST", f"/api/approvals/{courier}/decide", {"action": "approve", "digest": ""}))[1]
        assert answer["toast"] == "The card has expired — no decision taken."

    faced(tmp_path, body)


def test_the_face_starts_a_session_once_per_input_id_writes_into_it_and_stops_it(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        first = input_id()
        status, started, _ = await world.call("POST", "/api/sessions", {"track": "notes", "text": "a plan", "input_id": first})
        assert status == 200 and started["session"]
        session = started["session"]
        again = (await world.call("POST", "/api/sessions", {"track": "notes", "text": "a plan", "input_id": first}))[1]
        assert again["session"] == session and len(world.lanes["private"].runs()) == 1, "a retried POST"
        assert (await world.call("POST", "/api/sessions", {"track": "nope", "text": "x", "input_id": input_id()}))[0] \
            == 400
        assert (await world.call("POST", "/api/sessions", {"track": "notes", "text": "x", "input_id": "tg:1"}))[0] \
            == 400, "the router's ids are not the face's"

        said = (await world.call("POST", f"/api/sessions/{session}/input", {"input_id": input_id(), "text": "more"}))[1]
        assert said == {"say": "Passed to the session."}
        assert world.lanes["private"].commands[-1] == ("/answer", {"session": session, "prompt": "more"})
        assert (await world.call("POST", f"/api/sessions/{session}/input", {"input_id": input_id(), "text": " "}))[0] \
            == 400
        assert (await world.call("POST", "/api/sessions/00000000/input", {"input_id": input_id(), "text": "x"}))[0] \
            == 404

        stopped = (await world.call("POST", f"/api/sessions/{session}/stop"))[1]
        assert stopped == {"ok": True} and world.lanes["private"].commands[-1] == ("/stop", {"session": session})
        [run] = world.lanes["private"].runs()
        world.lanes["private"].add(session, run["run"], json.dumps({"type": "lane", "event": "stopped"}))
        world.lanes["private"].exited(session, run["run"])
        await world.poll()
        assert not world.store.interrupted(run["run"]), "the owner's stop is not the shutdown's: no resume"
        assert world.store.session(session)["state"] == "idle" and world.store.queued() == []
        assert len(world.lanes["private"].runs()) == 1
        world.lanes["private"].answers["/stop"] = "not_running"
        assert (await world.call("POST", f"/api/sessions/{session}/stop"))[1] == {"ok": False}

    faced(tmp_path, body)


@pytest.mark.parametrize("path", ["/api/meta", "/api/sessions", "/api/sessions/x", "/api/sessions/x/diff"])
def test_every_read_needs_a_login(tmp_path, path):
    async def body(world: FaceWorld):
        status, data, response = await world.call("GET", path)
        assert status == 401 and response.headers["Cache-Control"] == "no-store"

    faced(tmp_path, body)


def test_a_partial_line_given_again_after_a_crash_reaches_no_stream(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "hello"}))["session"]
        [run] = world.lanes["private"].runs()
        message = json.dumps({"type": "assistant", "parent_tool_use_id": None,
                              "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello!"}]}})
        world.lanes["private"].add(session, run["run"], partial("Hello!"), message)
        await world.poll()
        given = []
        world.platform.on_delta = lambda session, text: given.append(text)
        for line in world.lanes["private"].journal:  # the platform died before it confirmed
            line["given"] = False
        await world.poll()
        assert given == [], "its message is already whole in the transcript"
        world.lanes["private"].add(session, run["run"], partial("Next"))
        await world.poll()
        assert given == ["Next"]

    faced(tmp_path, body)


def test_closing_a_stream_stuck_on_a_write_cuts_its_connection_and_an_idle_one_ends_cleanly():
    class Transport:
        closed = False

        def close(self):
            self.closed = True

    class Request:
        def __init__(self):
            self.transport = Transport()

    idle, stuck = face._Stream("l", Request()), face._Stream("l", Request())
    stuck.writing = True
    idle.close()
    stuck.close()
    assert idle.closed and idle.wake.is_set() and not idle.request.transport.closed
    assert stuck.closed and stuck.request.transport.closed


def test_a_pin_is_kept_on_the_session_and_reaches_every_device_as_its_event(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, _ = await deferred_session(world)
        row = world.store.face_session(session)
        cursor = world.store.cursor()
        world.clock.now += 60
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/pin", {"pinned": True})
        assert status == 200 and data["session"]["pinned"] is True
        assert data["session"]["at"] == row["at"], "pinning does not reorder the list by time"
        assert [kind for _, kind, _ in world.store.events_after(cursor)] == ["session"]
        assert replayed(world.store)["sessions"][session] == (await world.get("/api/sessions"))["sessions"][0]
        await world.call("POST", f"/api/sessions/{session}/pin", {"pinned": False})
        assert world.store.face_session(session)["pinned"] is False
        assert (await world.call("POST", f"/api/sessions/{session}/pin", {"pinned": "yes"}))[0] == 400
        assert (await world.call("POST", "/api/sessions/nope0000/pin", {"pinned": True}))[0] == 404
        assert (await world.call("POST", f"/api/sessions/{session}/pin", {"pinned": True},
                                 headers={"Origin": ORIGIN}))[0] == 403, "the header, as every POST"

    faced(tmp_path, body)


def test_a_side_question_from_the_face_starts_a_session_under_the_main_one(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        session, _, _ = await deferred_session(world)
        qid = input_id()
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/side",
                                           {"input_id": qid, "text": " what for? "})
        assert status == 200, data
        side = data["session"]
        row = world.store.face_session(side)
        assert (row["parent"], row["title"], row["track"]) == (session, "what for?", "retinue")
        again = await world.call("POST", f"/api/sessions/{session}/side", {"input_id": qid, "text": "what for?"})
        assert again[1]["session"] == side, "a retried POST is one question"
        assert world.store.session(session)["state"] == "waiting" and \
            world.store.first_input(session) == "почини", "the main session got nothing"
        for bad in ({"input_id": "tg:1", "text": "x"}, {"input_id": input_id(), "text": "  "},
                    {"input_id": input_id()}):
            assert (await world.call("POST", f"/api/sessions/{session}/side", bad))[0] == 400, bad
        assert (await world.call("POST", "/api/sessions/nope0000/side", {"input_id": input_id(), "text": "x"}))[0] \
            == 404

    faced(tmp_path, body)


def test_a_message_with_attachments_from_the_app_goes_in_and_its_picture_comes_back_only_as_a_thumbnail(tmp_path):
    from test_platformd import _png, _wire

    async def body(world: FaceWorld):
        await world.login()
        session = (await world.owner("new", {"input_id": "tg:1", "track": "notes", "text": "hello"}))["session"]
        input_id = f"face:{uuid.uuid4()}"
        photo = _wire("IMG_1.png", _png(), "image/png")
        status, data, _ = await world.call("POST", f"/api/sessions/{session}/input",
                                           {"input_id": input_id, "attachments": [photo]})
        assert status == 200 and data["say"] in ("Accepted.", "Passed to the session.")
        [_, mine] = (await world.get(f"/api/sessions/{session}"))["inputs"]
        assert mine["text"] == "" and mine["attachments"] == [
            {"kind": "image", "name": "IMG_1.png", "size": len(_png()), "what": 'picture "IMG_1.png", 40×20',
             "thumb": True}]

        async def raw(path, cookie=True):
            headers = {"Cookie": f"{COOKIE}={world.cookie}"} if cookie else {}
            response = await world.api.request("GET", path, headers=headers)
            return response.status, await response.read(), response.headers

        status, jpeg, headers = await raw(f"/api/sessions/{session}/inputs/{input_id}/0")
        assert status == 200 and jpeg[:2] == b"\xff\xd8" and jpeg != base64.b64decode(photo["data"])
        assert headers["Content-Type"] == "image/jpeg" and headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store" and headers["Content-Security-Policy"] == "default-src 'none'"
        assert (await raw(f"/api/sessions/{session}/inputs/{input_id}/0", cookie=False))[0] == 401
        assert (await raw(f"/api/sessions/{session}/inputs/{input_id}/1"))[0] == 404
        assert (await raw(f"/api/sessions/00000000/inputs/{input_id}/0"))[0] == 404, "only under its own session"
        assert (await raw(f"/api/sessions/{session}/inputs/x/0"))[0] == 404
        assert (await world.call("POST", f"/api/sessions/{session}/input", {"input_id": f"face:{uuid.uuid4()}"}))[0] \
            == 400, "neither text nor attachments"

    faced(tmp_path, body)


def test_the_apps_new_task_runs_on_the_track_in_the_root_of_the_base():
    """«＋» in the app starts a general session that finds its own context: the track whose directory is the Base,
    on the Base's lane — not simply the first track, which may be a repository's."""
    from retinue.config import BaseConfig, LaneRef, PlatformConfig, Track

    lanes = {name: LaneRef(name, "", "", "/work") for name in ("private", "old")}
    lanes["old"].retired = True
    tracks = [Track("code", "private", "code", "base"), Track("gone", "old", "private", "base"),
              Track("research", "private", "private", "base/research"), Track("notes", "private", "private", "./base/")]
    cfg = PlatformConfig(lanes=lanes, tracks=tracks, base=BaseConfig(author="O <o@example.com>"))
    assert face.general_track(cfg) == "notes", "`./base/` is the Base's root; a code track never is"
    cfg.base = None
    assert face.general_track(cfg) == "research", "no Base: the first private track of a lane still working"
    cfg.tracks = [Track("code", "private", "code", "code")]
    assert face.general_track(cfg) is None, "a repository's track would refuse every «＋»: none, and the app says so"
    cfg.tracks = []
    assert face.general_track(cfg) is None
