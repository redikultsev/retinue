"""`/mcp/device` (`retinue/device.py`): the agents on the owner's computer read and write his collections by a device's token —
scoped, revocable, kept only as its SHA-256 —, as `device:<device>/<session>` under the collection's rights for the class
`device`; the face's cookie opens nothing there, and the token opens nothing of the face — against the face of
`test_face`."""

from __future__ import annotations

import hashlib
import json

import pytest

from retinue import colltools, computers, feed
from retinue import device as endpoint
from retinue import project as project_mod
from retinue.collections import Rejected, Writer, among, granted, idem_scope, parse_form, parse_rights, writer_class
from retinue.face import COOKIE
from test_face import FaceWorld, faced
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse

DEFINE = Writer("owner", "define")
DEALS = {"fields": {"company": {"type": "text", "required": True}, "product": {"type": "text"},
                    "stage": {"type": "enum", "values": ["lead", "qualified", "won", "lost"]},
                    "close_reason": {"type": "text"}},
         "key": ["company", "product"]}
TOUCHES = {"fields": {"deal": {"type": "ref", "to": "deals"},
                      "state": {"type": "enum", "values": ["drafted", "sent"]}}}


async def collections(world: FaceWorld) -> None:
    store, now = world.platform.collections, world.clock.now
    await store.define(DEFINE, "deals", "work", DEALS, now)
    await store.define(DEFINE, "touches", "work", TOUCHES, now)
    # deals by the class's old name, as rights kept before #66 have it: it counts as `device`
    await store.rights(DEFINE, "deals", {"writers": {"mac": {"add": True, "set": ["company", "product", "stage"],
                                                                  "delete": False}}}, now)
    await store.rights(DEFINE, "touches", {"writers": {"device": {"add": True, "set": "*", "delete": False}},
                                           "set_by": {"state": {"sent": ["owner"]}}}, now)


class Device:
    """An MCP client of `/mcp/device`, as Claude Code is: initialize, then every request with the session it gave."""

    def __init__(self, world: FaceWorld, token: str, path: str = endpoint.PATH) -> None:
        self.world, self.token, self.path, self.session, self.n = world, token, path, "", 0

    async def post(self, body: dict, headers: dict | None = None):
        given = {"Authorization": f"Bearer {self.token}"} if headers is None else headers
        if self.session and headers is None:
            given[endpoint.SESSION_HEADER] = self.session
        response = await self.world.api.post(self.path, json=body, headers=given)
        return response.status, await response.json() if response.content_type == "application/json" else None, \
            response

    async def start(self) -> dict:
        self.session = ""
        status, answer, response = await self.post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code"}}})
        assert status == 200, answer
        self.session = response.headers[endpoint.SESSION_HEADER]
        return answer["result"]

    async def tools(self) -> list[str]:
        status, answer, _ = await self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 200, answer
        return [t["name"] for t in answer["result"]["tools"]]

    async def call(self, name: str, arguments: dict, use: str | None = None) -> tuple[str, bool]:
        self.n += 1
        params = {"name": name, "arguments": arguments, **({"_meta": {"claudecode/toolUseId": use}} if use else {})}
        status, answer, _ = await self.post({"jsonrpc": "2.0", "id": self.n + 1, "method": "tools/call",
                                             "params": params})
        assert status == 200, answer
        return answer["result"]["content"][0]["text"], answer["result"]["isError"]


def answer_of(said: str) -> dict:
    return json.loads(said.split("\n", 1)[1])


def test_a_device_lists_its_tools_and_writes_as_itself_once_however_often_the_call_comes(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        store = world.platform.collections
        token = await store.add_device("laptop", "write", world.clock.now)
        device = Device(world, token)
        started = await device.start()
        assert started["serverInfo"]["name"] == "retinue" and "Collections:" in started["instructions"]
        assert await device.tools() == list(colltools.NAMES), "the four, the same as a session's"

        said, error = await device.call(colltools.DESCRIBE, {"collection": "deals"})
        assert not error and json.loads(said)["you"] == {"add": True, "set": ["company", "product", "stage"],
                                                         "delete": False,
                                                         "only_by_others": {}}
        add = {"collection": "deals", "ops": [{"op": "add", "fields": {"company": "Acme", "stage": "lead"}}]}
        said, error = await device.call(colltools.APPLY, add, use="toolu_m1")
        assert not error and said.startswith("Written: 1 change."), said
        [record] = answer_of(said)["records"]
        [change] = await store.history("deals", record["id"])
        assert (change["actor"], change["via"], change["foreign"]) == (f"device:laptop/{device.session}", "device", []), \
            "the device and its MCP session, via device, with no mark of foreign text: the owner watches it"

        first = device.session
        await device.start()  # Claude Code makes an idempotent call again "once, on a new connection"
        assert device.session != first
        said, error = await device.call(colltools.APPLY, add, use="toolu_m1")
        assert not error and answer_of(said)["records"][0]["id"] == record["id"], "the device's idem, not the session's"
        assert (await store.catalog())[0]["records"] == 1

        said, error = await device.call(colltools.QUERY, {"collection": "deals"})
        assert not error
        assert [d["name"] for d in await store.devices()] == ["laptop"] and (await store.devices())[0]["written"]

    faced(tmp_path, body)


def test_the_rights_of_the_class_device_decide_what_it_writes(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        await collections(world)
        store, now = world.platform.collections, world.clock.now
        device = Device(world, await store.add_device("laptop", "write", now))
        await device.start()
        said, _ = await device.call(colltools.APPLY, {"collection": "deals", "ops": [
            {"op": "add", "fields": {"company": "Acme"}}]}, use="toolu_a")
        rid = answer_of(said)["records"][0]["id"]

        said, error = await device.call(colltools.APPLY, {"collection": "deals", "ops": [
            {"op": "delete", "id": rid, "version": 1}]}, use="toolu_d")
        assert error and "delete" in said and "device" in said, said
        said, error = await device.call(colltools.APPLY, {"collection": "deals", "ops": [
            {"op": "set", "id": rid, "version": 1, "fields": {"close_reason": "price"}}]}, use="toolu_s")
        assert error and "close_reason" in said, said
        record = await store.get("deals", rid)
        assert (record["version"], record.get("deleted")) == (1, None), "nothing of either was written"

        said, error = await device.call(colltools.APPLY, {"collection": "touches", "ops": [
            {"op": "add", "fields": {"deal": rid, "state": "sent"}}]}, use="toolu_t")
        assert not error, "a value `set_by` keeps for the owner becomes his card"
        assert (await store.catalog())[1]["records"] == 0
        [proposal] = world.platform.colltools.waiting()
        assert proposal["actor"] == f"device:laptop/{device.session}" and proposal["session"] is None
        stored = world.platform.store.db.execute('SELECT "foreign" FROM collection_proposals').fetchone()[0]
        assert stored == "device", "applied, the owner's change still says a computer's session filled it"
        feed = await world.get("/api/feed")
        [row] = [r for g in feed["groups"] for r in g["rows"] if r["source"] == "collection_proposal"]
        assert row["title"] == "Computer laptop"

    faced(tmp_path, body)


def test_a_token_that_only_reads_lists_and_serves_the_reads_alone(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        store = world.platform.collections
        device = Device(world, await store.add_device("projection", "read", world.clock.now))
        await device.start()
        assert await device.tools() == [colltools.DESCRIBE, colltools.QUERY]
        for name in (colltools.APPLY, colltools.PROPOSE):
            said, error = await device.call(name, {"collection": "deals", "ops": [
                {"op": "add", "fields": {"company": "Acme"}}], "why": "x"}, use=f"toolu_{name}")
            assert error and said == endpoint.READ_ONLY.format(tool=name)
        assert (await store.catalog())[0]["records"] == 0 and world.platform.colltools.waiting() == []
        said, error = await device.call(colltools.QUERY, {"collection": "deals"})
        assert not error

    faced(tmp_path, body)


def test_no_token_a_wrong_one_or_a_revoked_one_opens_nothing_and_the_file_keeps_only_its_hash(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        store = world.platform.collections
        token = await store.add_device("laptop", "write", world.clock.now)
        assert await store._run(lambda: store._db.execute("SELECT token_sha256 FROM devices").fetchall()) == [
            (hashlib.sha256(token.encode()).hexdigest(),)]
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": token}, {"Authorization": "Bearer "}):
            status, answer, response = await Device(world, token).post(listing, headers=headers)
            assert status == 401 and answer["error"] == endpoint.UNAUTHORIZED, headers
            assert response.headers["WWW-Authenticate"].startswith("Bearer")
        status, _, _ = await Device(world, "nope").post(listing, headers={})
        assert status == 401
        response = await world.api.post(f"{endpoint.PATH}/other", json=listing)
        assert response.status == 401, "a path the app has not is still the token's first"

        device = Device(world, token)
        await device.start()
        assert await device.tools()
        assert await store.revoke_device("laptop", world.clock.now + 1) is True
        assert await store.revoke_device("laptop", world.clock.now + 2) is False
        status, answer, _ = await device.post(listing)
        assert status == 401, "revoked: its next call, the session it had included, opens nothing"
        assert await store.device(token) is None

    faced(tmp_path, body)


def test_the_cookie_opens_nothing_at_mcp_device_and_the_token_nothing_of_the_face(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        cookie = await world.login()
        token = await world.platform.collections.add_device("laptop", "write", world.clock.now)
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = await world.api.post(endpoint.PATH, json=listing, headers={
            "Cookie": f"{COOKIE}={cookie}", "X-Retinue": "1", "Origin": "https://app.example.test"})
        assert response.status == 401, "the owner's login is not a device"
        for method, path in (("GET", "/api/sessions"), ("GET", "/api/c/deals"), ("POST", "/api/c/deals")):
            status, answer, _ = await world.call(method, path, {"ops": []}, cookie="", headers={
                "Authorization": f"Bearer {token}", **({"X-Retinue": "1"} if method == "POST" else {})})
            assert status == 401, (method, path)
        # …and the face's own header is no part of the owner's computer's: none asked, none needed.
        device = Device(world, token)
        await device.start()
        status, answer, _ = await device.post(listing, headers={"Authorization": f"Bearer {token}",
                                                                endpoint.SESSION_HEADER: device.session})
        assert status == 200 and answer["result"]["tools"]

    faced(tmp_path, body)


def test_a_request_without_its_session_is_refused_and_there_is_no_stream(tmp_path):
    async def body(world: FaceWorld):
        token = await world.platform.collections.add_device("laptop", "write", world.clock.now)
        device = Device(world, token)
        status, answer, _ = await device.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 400 and answer["error"]["message"] == endpoint.NO_SESSION
        await device.start()
        status, answer, _ = await device.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert status == 202 and answer is None
        for method in ("GET", "DELETE"):
            response = await world.api.request(method, endpoint.PATH, headers={"Authorization": f"Bearer {token}"})
            assert response.status == 405 and response.headers["Allow"] == "POST"

    faced(tmp_path, body)


def test_a_session_is_one_the_server_gave_that_device(tmp_path):
    async def body(world: FaceWorld):
        store, now = world.platform.collections, world.clock.now
        laptop = Device(world, await store.add_device("laptop", "write", now))
        other = Device(world, await store.add_device("desk", "write", now))
        await laptop.start()
        await other.start()
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        one_off = laptop.session[:-1] + ("0" if laptop.session[-1] != "0" else "1")  # not a 1 in 16 chance of itself
        for forged in ("forged_session", other.session, one_off):
            status, answer, _ = await laptop.post(listing, headers={"Authorization": f"Bearer {laptop.token}",
                                                                    endpoint.SESSION_HEADER: forged})
            assert status == 404 and answer["error"]["message"] == endpoint.UNKNOWN_SESSION, forged
        status, _, _ = await laptop.post(listing)
        assert status == 200, "its own, given at initialize"

    faced(tmp_path, body)


def test_a_body_past_its_limit_is_refused_before_it_is_read(tmp_path):
    async def body(world: FaceWorld):
        device = Device(world, await world.platform.collections.add_device("laptop", "write", world.clock.now))
        await device.start()
        response = await world.api.post(endpoint.PATH, data=b" " * (endpoint.BODY_MAX + 1), headers={
            "Authorization": f"Bearer {device.token}", endpoint.SESSION_HEADER: device.session,
            "Content-Type": "application/json"})
        assert response.status == 413 and (await response.json())["error"]["message"] == endpoint.TOO_LONG

    faced(tmp_path, body)


def test_the_models_idem_is_its_sessions_and_a_tool_calls_the_devices(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        store = world.platform.collections
        token = await store.add_device("laptop", "write", world.clock.now)
        one, two = Device(world, token), Device(world, token)
        await one.start()
        await two.start()
        add = {"op": "add", "fields": {"company": "Acme", "stage": "lead"}}
        said, error = await one.call(colltools.APPLY, {"collection": "deals", "ops": [add], "idem": "add-1"})
        assert not error and said.startswith("Written: 1 change."), said
        made = answer_of(said)["records"][0]["id"]
        other = {"op": "add", "fields": {"company": "Globex", "stage": "lead"}}
        said, error = await two.call(colltools.APPLY, {"collection": "deals", "ops": [other], "idem": "add-1"})
        assert not error and said.startswith("Written: 1 change."), "another conversation's add-1 is another call"
        said, error = await one.call(colltools.APPLY, {"collection": "deals", "ops": [add], "idem": "add-1"})
        assert not error and answer_of(said)["records"][0]["id"] == made, "its own add-1 again is the call made"
        assert (await store.catalog())[0]["records"] == 2

    faced(tmp_path, body)


def test_a_devices_proposals_are_limited_in_all_its_sessions_and_a_revoked_ones_are_not_applied(tmp_path):
    async def body(world: FaceWorld):
        cookie = await world.login()
        await collections(world)
        store, tools = world.platform.collections, world.platform.colltools
        token = await store.add_device("laptop", "write", world.clock.now)
        first = Device(world, token)
        await first.start()
        said, _ = await first.call(colltools.APPLY, {"collection": "deals", "ops": [
            {"op": "add", "fields": {"company": "Acme"}}]}, use="toolu_a")
        rid = answer_of(said)["records"][0]["id"]
        touch = {"collection": "touches", "ops": [{"op": "add", "fields": {"deal": rid, "state": "sent"}}]}
        for n in range(colltools.PROPOSALS_MAX):  # a new session for each: the limit is the device's
            device = Device(world, token)
            await device.start()
            said, error = await device.call(colltools.PROPOSE, {**touch, "why": f"sent {n}"},
                                            use=f"toolu_p{n}")
            assert not error, said
        assert len(tools.waiting()) == 1, "the same operations in another session are the proposal made"
        for n in range(1, colltools.PROPOSALS_MAX + 1):
            device = Device(world, token)
            await device.start()
            said, error = await device.call(colltools.PROPOSE, {"collection": "deals", "ops": [
                {"op": "add", "fields": {"company": f"Co {n}"}}], "why": "found"}, use=f"toolu_q{n}")
        assert error and str(colltools.PROPOSALS_MAX) in said, said
        assert len(tools.waiting()) == colltools.PROPOSALS_MAX

        assert await store.revoke_device("laptop", world.clock.now)
        proposal = tools.waiting()[0]["id"]
        status, answer, _ = await world.call("POST", f"/api/c/_proposals/{proposal}/apply", {}, cookie=cookie)
        assert status == 409 and answer["kind"] == "revoked" and answer["error"] == colltools.REVOKED, answer
        assert tools.face_proposal(proposal)["state"] == "open", "left to read and dismiss"

    faced(tmp_path, body)


# --- the device's own sessions and the project tree (retinue/computers.py) ---------------------------------------------

def report_of(n: int, sessions: list) -> dict:
    return {"jsonrpc": "2.0", "id": n, "method": endpoint.SESSIONS, "params": {"sessions": sessions}}


def test_a_device_reports_its_sessions_and_the_app_lists_them_until_it_goes_off(tmp_path, caplog):
    async def body(world: FaceWorld):
        store = world.platform.collections
        token = await store.add_device("laptop", "read", world.clock.now)
        device = Device(world, token)
        await device.start()
        assert endpoint.SESSIONS not in await device.tools() and endpoint.PROJECTS not in await device.tools(), \
            "the device's own methods are never listed among the tools a session sees"
        rows = [{"id": "local_a1", "title": "Garden plan\nsecond line", "folder": "/home/anna/garden",
                 "project": "garden/roses/", "state": "waiting", "at": world.clock.now - 5,
                 "link": "claude://claude.ai/epitaxy/local_a1"},
                {"id": "local_b2", "title": "Scratch", "folder": "/tmp", "state": "working", "at": world.clock.now}]
        status, answer, _ = await device.post(report_of(2, rows))
        assert status == 200 and answer["result"] == {"taken": 2, "again_s": computers.BEAT_S}, \
            "any scope: the device's sessions are its own, not the owner's records"

        assert (await world.call("GET", "/api/computers"))[0] == 401, "the app's login, as every /api/*"
        await world.login()
        [laptop] = (await world.get("/api/computers"))["computers"]
        assert (laptop["device"], laptop["off"]) == ("laptop", False)
        first, second = laptop["sessions"]
        assert first == {"id": "local_a1", "title": "Garden plan second line", "folder": "/home/anna/garden",
                         "project": "garden/roses", "state": "waiting", "at": world.clock.now - 5,
                         "link": "claude://claude.ai/epitaxy/local_a1", "slot": 0}
        assert second["project"] == "" and "link" not in second, "no project: the list's «Other»"
        assert "Garden plan" not in caplog.text and "/home/anna" not in caplog.text, "titles and folders: no log"

        status, answer, _ = await device.post(report_of(3, rows[1:]))
        assert answer["result"]["taken"] == 1
        assert [s["id"] for s in (await world.get("/api/computers"))["computers"][0]["sessions"]] == ["local_b2"], \
            "a report is the whole list: one that left is gone"

        world.clock.now += computers.OFF_S + 1
        [laptop] = (await world.get("/api/computers"))["computers"]
        assert laptop["off"] is True and laptop["sessions"], "off: the rows stay, the app greys them"

        await store.revoke_device("laptop", world.clock.now)
        assert (await world.get("/api/computers"))["computers"] == [], "a revoked device's rows go at once"

    faced(tmp_path, body)


def test_a_report_the_platform_does_not_take_keeps_the_list_before(tmp_path):
    async def body(world: FaceWorld):
        token = await world.platform.collections.add_device("laptop", "write", world.clock.now)
        device = Device(world, token)
        status, _, _ = await device.post(report_of(1, []), headers={"Authorization": f"Bearer {token}"})
        assert status == 400, "a session first, as for a tool"
        await device.start()
        good = {"id": "local_a1", "title": "Plan", "state": "done", "at": 1.0}
        assert (await device.post(report_of(2, [good])))[1]["result"]["taken"] == 1
        bad = [
            {"sessions": "nope"},
            {"sessions": [good] * (computers.SESSIONS_MAX + 1)},
            {"sessions": [good, good]},
            {"sessions": [{**good, "state": "sleeping"}]},
            {"sessions": [{**good, "id": "../x"}]},
            {"sessions": [{**good, "at": "yesterday"}]},
            {"sessions": [{**good, "project": "../etc"}]},
            {"sessions": [{**good, "link": "https://example.com/x"}]},
            {"sessions": [{**good, "link": "javascript:alert(1)"}]},
        ]
        for n, params in enumerate(bad, start=3):
            status, answer, _ = await device.post({"jsonrpc": "2.0", "id": n, "method": endpoint.SESSIONS,
                                                   "params": params})
            assert status == 200 and answer["error"]["code"] == -32602 and answer["error"]["message"], params
        listed = world.platform.computers.listing(world.clock.now)
        assert [s["id"] for s in listed[0]["sessions"]] == ["local_a1"], "the list before stays"

    faced(tmp_path, body)


def test_a_device_reads_the_project_tree_its_sidebar_is_filed_by(tmp_path):
    from test_spaces import _Base, make_base, reader

    work = make_base(tmp_path, {"AGENTS.md": "# Base\n", "garden/AGENTS.md": "# Garden\n",
                                "garden/roses/AGENTS.md": "# Roses\n", "garden/roses/knowledge/soil.md": "Loam.\n",
                                "ledger/AGENTS.md": "# Ledger\n"})

    async def body(world: FaceWorld):
        token = await world.platform.collections.add_device("laptop", "read", world.clock.now)
        device = Device(world, token)
        await device.start()
        world.platform.base = None
        status, answer, _ = await device.post({"jsonrpc": "2.0", "id": 2, "method": endpoint.PROJECTS})
        assert answer["error"]["message"] == endpoint.NO_BASE
        world.platform.base = _Base(reader(work))
        status, answer, _ = await device.post({"jsonrpc": "2.0", "id": 3, "method": endpoint.PROJECTS})
        assert status == 200 and answer["result"]["head"]
        assert answer["result"]["projects"] == [
            {"path": "garden", "title": "Garden", "parent": ""},
            {"path": "garden/roses", "title": "Roses", "parent": "garden"},
            {"path": "ledger", "title": "Ledger", "parent": ""}]

    faced(tmp_path, body)


def test_a_computers_turn_endings_are_cards_in_the_feed(tmp_path):
    async def body(world: FaceWorld):
        token = await world.platform.collections.add_device("laptop", "read", world.clock.now)
        device = Device(world, token)
        await device.start()
        now = world.clock.now
        asks = {"kind": "ask", "request": "Pick a plan", "why_owner": "goal", "context": "Two found",
                "options": [{"label": "A", "effect": "cheaper"}, {"label": "B", "effect": "faster"}]}
        done = {"kind": "done", "request": "Fix it", "what": "Fixed", "next": "Nothing", "next_by": "me",
                "artifacts": [{"path": "C:\\work\\out\\shot.png"}, {"url": "https://example.com/page", "title": "Page"}]}
        rows = [{"id": "local_q", "title": "Plan", "state": "waiting", "at": now - 60, "project": "garden",
                 "link": "claude://claude.ai/epitaxy/local_q", "ending": asks},
                {"id": "local_d", "title": "Fix", "state": "done", "at": now - 30, "ending": done},
                {"id": "local_w", "title": "Waits", "state": "waiting", "at": now - 10},
                {"id": "local_x", "title": "Bad", "state": "done", "at": now - 5, "ending": {"kind": "ask"}},
                {"id": "local_r", "title": "Runs", "state": "working", "at": now, "ending": done},
                {"id": "local_q2", "title": "Quiet", "state": "done", "at": now}]
        status, answer, _ = await device.post(report_of(2, rows))
        assert status == 200 and answer["result"]["taken"] == 6
        assert [x["id"] for x in answer["result"]["left_out"]] == ["local_x"], "an ending off the shape is named"

        await world.login()
        feed = await world.get("/api/feed")
        by_key = {r["key"]: (g["action"], r) for g in feed["groups"] for r in g["rows"]}
        assert set(by_key) == {"ending:laptop/local_q", "ending:laptop/local_d", "reply:laptop/local_w"}, \
            "at work, quiet, or its ending left out with nothing to say: no row"
        action, q = by_key["ending:laptop/local_q"]
        assert action == "answer" and q["session"] is None and q["project"] == "garden"
        assert q["computer"] == {"device": "laptop", "id": "local_q", "link": "claude://claude.ai/epitaxy/local_q"}
        assert q["ending"]["options"] == asks["options"]
        action, d = by_key["ending:laptop/local_d"]
        shot, page = d["ending"]["artifacts"]
        assert action == "read" and shot["title"] == "shot.png" and shot["src"] is None and shot["on_computer"]
        assert page["src"] == "https://example.com/page" and page["kind"] == "link"
        assert by_key["reply:laptop/local_w"][0] == "reply"

        world.clock.now += computers.OFF_S + 1
        feed = await world.get("/api/feed")
        assert not [r for g in feed["groups"] for r in g["rows"] if r.get("computer")], "an off computer: no rows"

    faced(tmp_path, body)


def test_the_owners_answer_to_a_computers_session_reaches_that_computer_until_it_is_delivered(tmp_path, caplog):
    async def body(world: FaceWorld):
        store = world.platform.collections
        laptop, desktop = Device(world, await store.add_device("laptop", "read", world.clock.now)), \
            Device(world, await store.add_device("desktop", "read", world.clock.now))
        await laptop.start()
        await desktop.start()
        now = world.clock.now
        asks = {"kind": "ask", "request": "Pick a plan", "context": "Two found",
                "options": [{"label": "A", "effect": "cheaper"}, {"label": "B", "effect": "faster"}]}
        rows = [{"id": "local_q", "title": "Plan", "state": "waiting", "at": now - 60, "ending": asks},
                {"id": "local_w", "title": "Waits", "state": "waiting", "at": now - 10},
                {"id": "local_r", "title": "Runs", "state": "working", "at": now}]
        assert "answers" not in (await laptop.post(report_of(2, rows)))[1]["result"], "none given: none handed over"
        await desktop.post(report_of(2, [{"id": "local_q", "title": "Other", "state": "waiting", "at": now}]))
        await world.login()

        def answer(device: str, session: str, text: object):
            return world.call("POST", f"/api/computers/{device}/sessions/{session}/answer", {"text": text})

        status, given, _ = await answer("laptop", "local_q", "  A  ")
        assert status == 200 and given["answer"]
        for device, session, text, code in [("laptop", "local_r", "go", 400), ("laptop", "local_gone", "go", 404),
                                            ("laptop", "local_w", "  ", 400),
                                            ("laptop", "local_w", "x" * (computers.ANSWER_MAX + 1), 400),
                                            ("nobody", "local_q", "go", 404)]:
            assert (await answer(device, session, text))[0] == code, (device, session, text)
        assert (await world.call("POST", "/api/computers/laptop/sessions/local_w/answer", {"text": "go"},
                                 headers={}))[0] in (401, 403), "the app's guard, as every POST"

        feed = await world.get("/api/feed")
        by_key = {r["key"]: r for g in feed["groups"] for r in g["rows"]}
        assert by_key["ending:laptop/local_q"]["answered"] == "A", "the card says his answer instead of asking again"
        assert "answered" not in by_key["ending:desktop/local_q"], "one computer's answer is that computer's alone"

        result = (await laptop.post(report_of(3, rows)))[1]["result"]
        [handed] = result["answers"]
        assert {k: handed[k] for k in ("id", "session", "text")} == {"id": given["answer"], "session": "local_q",
                                                                      "text": "A"} and handed["at"]
        assert "answers" not in (await desktop.post(report_of(3, [{"id": "local_q", "title": "Other",
                                                                   "state": "waiting", "at": now}])))[1]["result"]
        assert (await laptop.post(report_of(4, rows)))[1]["result"]["answers"] == [handed], \
            "again on every report until it is delivered"

        _, newer, _ = await answer("laptop", "local_q", "B")
        [replaced] = (await laptop.post(report_of(5, rows)))[1]["result"]["answers"]
        assert (replaced["id"], replaced["text"]) == (newer["answer"], "B"), "a newer answer replaces the one before"
        delivered = {**report_of(6, rows), "params": {"sessions": rows, "delivered": [newer["answer"]]}}
        assert "answers" not in (await laptop.post(delivered))[1]["result"], "delivered: not handed over again"
        assert "answered" not in {r["key"]: r for g in (await world.get("/api/feed"))["groups"]
                                  for r in g["rows"]}["ending:laptop/local_q"]

        await answer("laptop", "local_w", "go on")
        assert "answers" not in (await laptop.post(report_of(7, rows[:1])))[1]["result"], \
            "its session left the list: the answer goes"
        bad = {**report_of(8, rows), "params": {"sessions": rows, "delivered": "all"}}
        assert (await laptop.post(bad))[1]["error"]["message"] == computers.NOT_DELIVERED

        await store.revoke_device("laptop", world.clock.now)
        assert (await answer("laptop", "local_q", "D"))[0] == 404, "a revoked device's sessions take no answer"
        assert "go on" not in caplog.text and "Pick a plan" not in caplog.text, "his words: no log"

    faced(tmp_path, body)


def test_the_old_address_is_the_same_endpoint_for_the_clients_set_up_with_it(tmp_path):
    """`/mcp/mac`, the endpoint's old name (#66): a client set up with it keeps working, and writes under the new
    name; it opens to a device's token alone, as the new address does."""
    async def body(world: FaceWorld):
        await collections(world)
        store = world.platform.collections
        token = await store.add_device("laptop", "write", world.clock.now)
        old = Device(world, token, "/mcp/mac")  # as written in the clients set up before #66, not taken from the code
        assert endpoint.PATH != "/mcp/mac"
        await old.start()
        assert await old.tools() == list(colltools.NAMES)
        add = {"collection": "deals", "ops": [{"op": "add", "fields": {"company": "Acme"}}]}
        said, error = await old.call(colltools.APPLY, add, use="toolu_o1")
        assert not error, said
        [change] = await store.history("deals", answer_of(said)["records"][0]["id"])
        assert change["actor"] == f"device:laptop/{old.session}" and change["via"] == "device"
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, answer, _ = await old.post(listing, headers={})
        assert status == 401 and answer["error"] == endpoint.UNAUTHORIZED

    faced(tmp_path, body)


def test_a_computers_session_keeps_its_place_on_the_map_while_it_is_listed():
    machines = computers.Computers()
    row = lambda sid, project="": {"id": sid, "title": sid, "state": "done", "at": 1.0, "project": project}  # noqa: E731
    slots = lambda: {r["id"]: r["slot"] for r in machines.listing(2.0)[0]["sessions"]}  # noqa: E731
    machines.report("laptop", {"sessions": [row("a"), row("b"), row("c", "garden")]}, 1.0)
    assert slots() == {"a": 0, "b": 1, "c": 0}, "the lowest place free in its project"
    machines.report("laptop", {"sessions": [row("d"), row("b"), row("c", "garden")]}, 2.0)
    assert slots() == {"d": 0, "b": 1, "c": 0}, "one that left frees its place; the others stay put"
    machines.report("laptop", {"sessions": [row("d"), row("b", "garden"), row("c", "garden")]}, 3.0)
    assert slots() == {"d": 0, "b": 1, "c": 0}, "moved to another project: the lowest place free there"


def test_rights_events_and_the_feed_read_the_old_class_name_as_device():
    """Rights, events and writers kept before #66 say `mac`: each reads as `device`, and what is kept anew says
    `device` only."""
    form = parse_form(DEALS)
    kept = parse_rights({"writers": {"mac": {"add": True, "set": ["company"], "delete": False}},
                         "set_by": {"stage": {"won": ["owner", "mac"]}}}, form)
    assert kept == {"writers": {"device": {"add": True, "set": ["company"], "delete": False}},
                    "set_by": {"stage": {"won": ["owner", "device"]}}}, "kept anew: the new name"
    for twice in ({"writers": {"mac": {"add": True}, "device": {"add": True}}},
                  {"writers": {}, "set_by": {"stage": {"won": ["mac", "device"]}}}):
        with pytest.raises(Rejected, match="twice"):
            parse_rights(twice, form)

    old = {"writers": {"mac": {"add": True, "set": "*", "delete": False}}, "set_by": {}}  # as kept before
    assert granted(old, "device") == {"add": True, "set": "*", "delete": False}
    assert granted(old, "session") == {} and among("device", ["owner", "mac"]) and not among("session", ["mac"])
    assert writer_class("mac:laptop/s1") == writer_class("device:laptop/s1") == "device"
    assert idem_scope("device:laptop/s1") == "device:laptop", "a device's idems are its own, whatever its session"

    described = project_mod.parse({"events": {"won": {"collection": "deals", "by": ["owner", "mac"]}}}, "work")
    assert described["events"]["won"]["by"] == ["owner", "device"]
    with pytest.raises(Rejected, match="each once"):
        project_mod.parse({"events": {"won": {"collection": "deals", "by": ["mac", "device"]}}}, "work")

    assert feed._proposer("mac:air/s1") == feed._proposer("device:air/s1") == "Computer air"
