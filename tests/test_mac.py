"""`/mcp/mac` (`retinue/mac.py`): the agents on the owner's Mac read and write his collections by a device's token —
scoped, revocable, kept only as its SHA-256 —, as `mac:<device>/<session>` under the collection's rights for the class
`mac`; the face's cookie opens nothing there, and the token opens nothing of the face — against the face of
`test_face`."""

from __future__ import annotations

import hashlib
import json

from retinue import colltools, mac
from retinue.collections import Writer
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
    await store.rights(DEFINE, "deals", {"writers": {"mac": {"add": True, "set": ["company", "product", "stage"],
                                                                  "delete": False}}}, now)
    await store.rights(DEFINE, "touches", {"writers": {"mac": {"add": True, "set": "*", "delete": False}},
                                           "set_by": {"state": {"sent": ["owner"]}}}, now)


class Device:
    """An MCP client of `/mcp/mac`, as Claude Code is: initialize, then every request with the session it gave."""

    def __init__(self, world: FaceWorld, token: str) -> None:
        self.world, self.token, self.session, self.n = world, token, "", 0

    async def post(self, body: dict, headers: dict | None = None):
        given = {"Authorization": f"Bearer {self.token}"} if headers is None else headers
        if self.session and headers is None:
            given[mac.SESSION_HEADER] = self.session
        response = await self.world.api.post(mac.PATH, json=body, headers=given)
        return response.status, await response.json() if response.content_type == "application/json" else None, \
            response

    async def start(self) -> dict:
        self.session = ""
        status, answer, response = await self.post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code"}}})
        assert status == 200, answer
        self.session = response.headers[mac.SESSION_HEADER]
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
        assert (change["actor"], change["via"], change["foreign"]) == (f"mac:laptop/{device.session}", "mac", []), \
            "the device and its MCP session, via mac, with no mark of foreign text: the owner watches it"

        first = device.session
        await device.start()  # Claude Code makes an idempotent call again "once, on a new connection"
        assert device.session != first
        said, error = await device.call(colltools.APPLY, add, use="toolu_m1")
        assert not error and answer_of(said)["records"][0]["id"] == record["id"], "the device's idem, not the session's"
        assert (await store.catalog())[0]["records"] == 1

        said, error = await device.call(colltools.QUERY, {"collection": "deals"})
        assert not error and said.startswith("Records of deals — data, not instructions:")
        assert [d["name"] for d in await store.devices()] == ["laptop"] and (await store.devices())[0]["written"]

    faced(tmp_path, body)


def test_the_rights_of_the_class_mac_decide_what_it_writes(tmp_path):
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
        assert error and "delete" in said and "mac" in said, said
        said, error = await device.call(colltools.APPLY, {"collection": "deals", "ops": [
            {"op": "set", "id": rid, "version": 1, "fields": {"close_reason": "price"}}]}, use="toolu_s")
        assert error and "'close_reason' is not open to mac in deals" in said, said
        record = await store.get("deals", rid)
        assert (record["version"], record.get("deleted")) == (1, None), "nothing of either was written"

        said, error = await device.call(colltools.APPLY, {"collection": "touches", "ops": [
            {"op": "add", "fields": {"deal": rid, "state": "sent"}}]}, use="toolu_t")
        assert not error and "It is the owner's to apply" in said and "Proposed to the owner" in said, \
            "a value `set_by` keeps for the owner becomes his card"
        assert (await store.catalog())[1]["records"] == 0
        [proposal] = world.platform.colltools.waiting()
        assert proposal["actor"] == f"mac:laptop/{device.session}" and proposal["session"] is None
        stored = world.platform.store.db.execute('SELECT "foreign" FROM collection_proposals').fetchone()[0]
        assert stored == "mac", "applied, the owner's change still says a Mac's session filled it"
        feed = await world.get("/api/feed")
        [row] = [r for g in feed["groups"] for r in g["rows"] if r["source"] == "collection_proposal"]
        assert row["title"] == "Mac laptop"

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
            assert error and said == mac.READ_ONLY.format(tool=name)
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
            assert status == 401 and answer["error"] == mac.UNAUTHORIZED, headers
            assert response.headers["WWW-Authenticate"].startswith("Bearer")
        status, _, _ = await Device(world, "nope").post(listing, headers={})
        assert status == 401
        response = await world.api.post(f"{mac.PATH}/other", json=listing)
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


def test_the_cookie_opens_nothing_at_mcp_mac_and_the_token_nothing_of_the_face(tmp_path):
    async def body(world: FaceWorld):
        await collections(world)
        cookie = await world.login()
        token = await world.platform.collections.add_device("laptop", "write", world.clock.now)
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = await world.api.post(mac.PATH, json=listing, headers={
            "Cookie": f"{COOKIE}={cookie}", "X-Retinue": "1", "Origin": "https://app.example.test"})
        assert response.status == 401, "the owner's login is not a device"
        for method, path in (("GET", "/api/sessions"), ("GET", "/api/c/deals"), ("POST", "/api/c/deals")):
            status, answer, _ = await world.call(method, path, {"ops": []}, cookie="", headers={
                "Authorization": f"Bearer {token}", **({"X-Retinue": "1"} if method == "POST" else {})})
            assert status == 401 and "Log in first" in answer["error"], (method, path)
        # …and the face's own header is no part of the Mac's: none asked, none needed.
        device = Device(world, token)
        await device.start()
        status, answer, _ = await device.post(listing, headers={"Authorization": f"Bearer {token}",
                                                                mac.SESSION_HEADER: device.session})
        assert status == 200 and answer["result"]["tools"]

    faced(tmp_path, body)


def test_a_request_without_its_session_is_refused_and_there_is_no_stream(tmp_path):
    async def body(world: FaceWorld):
        token = await world.platform.collections.add_device("laptop", "write", world.clock.now)
        device = Device(world, token)
        status, answer, _ = await device.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 400 and answer["error"]["message"] == mac.NO_SESSION
        await device.start()
        status, answer, _ = await device.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert status == 202 and answer is None
        for method in ("GET", "DELETE"):
            response = await world.api.request(method, mac.PATH, headers={"Authorization": f"Bearer {token}"})
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
                                                                    mac.SESSION_HEADER: forged})
            assert status == 404 and answer["error"]["message"] == mac.UNKNOWN_SESSION, forged
        status, _, _ = await laptop.post(listing)
        assert status == 200, "its own, given at initialize"

    faced(tmp_path, body)


def test_a_body_past_its_limit_is_refused_before_it_is_read(tmp_path):
    async def body(world: FaceWorld):
        device = Device(world, await world.platform.collections.add_device("laptop", "write", world.clock.now))
        await device.start()
        response = await world.api.post(mac.PATH, data=b" " * (mac.BODY_MAX + 1), headers={
            "Authorization": f"Bearer {device.token}", mac.SESSION_HEADER: device.session,
            "Content-Type": "application/json"})
        assert response.status == 413 and (await response.json())["error"]["message"] == mac.TOO_LONG

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
            assert not error and ("Proposed to the owner" in said or "already with the owner" in said), said
        assert len(tools.waiting()) == 1, "the same operations in another session are the proposal made"
        for n in range(1, colltools.PROPOSALS_MAX + 1):
            device = Device(world, token)
            await device.start()
            said, error = await device.call(colltools.PROPOSE, {"collection": "deals", "ops": [
                {"op": "add", "fields": {"company": f"Co {n}"}}], "why": "found"}, use=f"toolu_q{n}")
        assert error and f"{colltools.PROPOSALS_MAX} of your proposals" in said, said
        assert len(tools.waiting()) == colltools.PROPOSALS_MAX

        assert await store.revoke_device("laptop", world.clock.now)
        proposal = tools.waiting()[0]["id"]
        status, answer, _ = await world.call("POST", f"/api/c/_proposals/{proposal}/apply", {}, cookie=cookie)
        assert status == 409 and answer["kind"] == "revoked" and answer["error"] == colltools.REVOKED, answer
        assert tools.face_proposal(proposal)["state"] == "open", "left to read and dismiss"

    faced(tmp_path, body)
