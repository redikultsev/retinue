"""The Telegram Business gateway without Telegram: only the owner's connection, his chosen chats as items, whose each
message is; a reply only inside the 24-hour window, plain, as a reply, once. Fake ids, no network."""

import asyncio
import dataclasses

from aiohttp.test_utils import TestClient, TestServer

from retinue import courier, tgbusiness

OWNER, ANNA, BOT = 42, 777, 999
NOW = 1_760_000_000


def gateway(answers=None):
    calls = []
    g = tgbusiness.Gateway(tgbusiness.State(":memory:"), "t", OWNER)

    async def call(method, **params):
        calls.append((method, params))
        answer = (answers or {}).get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    g.call = call
    return g, calls


def message(text, sender=ANNA, mid=10, connection="c1", **extra):
    return {"business_connection_id": connection, "message_id": mid, "date": NOW, "text": text,
            "from": {"id": sender}, "chat": {"id": ANNA, "type": "private", "first_name": "Анна", "username": "anna_x"},
            **extra}


CONNECTION = {"id": "c1", "user": {"id": OWNER}, "is_enabled": True, "rights": {"can_reply": True, "can_read_messages": False}}


def test_only_the_owners_connection_and_his_chosen_chats_become_items():
    g, calls = gateway({"getBusinessConnection": lambda p: {**CONNECTION, "id": p["business_connection_id"],
                                                             "user": {"id": 5 if p["business_connection_id"] == "x" else OWNER}}})

    async def run():
        await g.consume({"update_id": 1, "business_connection": {**CONNECTION, "id": "s", "user": {"id": 5}}})
        await g.consume({"update_id": 2, "business_connection": CONNECTION})
        await g.consume({"update_id": 3, "business_message": message("Привет! Когда созвон?")})
        await g.consume({"update_id": 4, "business_message": message("В 7", sender=OWNER, mid=11)})
        await g.consume({"update_id": 5, "business_message": message("Буду.", sender=OWNER, mid=12,
                                                                      sender_business_bot={"id": BOT})})
        await g.consume({"update_id": 6, "edited_business_message": message("Привет! Когда созвон завтра?",
                                                                             edit_date=NOW + 60)})
        await g.consume({"update_id": 7, "business_message": message("чужой", connection="x", mid=13)})
        await g.consume({"update_id": 8, "business_message": {**message("группа", mid=14),
                                                              "chat": {"id": -100, "type": "group"}}})
        await g.consume({"update_id": 9, "business_message": message("", mid=15, photo=[{}], caption="фото")})
        await g.consume({"update_id": 10, "business_message": message("после правки", connection="c2", mid=16)})

    asyncio.run(run())
    items = g.state.look()
    assert [(i["ref"], i["data"]["direction"]) for i in items] == [
        ("777/10", "in"), ("777/11", "own"), ("777/12", "bot"), (f"777/10/e{NOW + 60}", "in"), ("777/15", "in"),
        ("777/16", "in")], "another user's connection and a group: nothing"
    first = items[0]["data"]
    assert (first["chat"], first["name"], first["username"], first["text"]) == ("777", "Анна", "anna_x",
                                                                                "Привет! Когда созвон?")
    assert items[4]["data"]["media"] == "photo" and items[4]["data"]["text"] == "фото"
    assert g.state.connection() == {"id": "c2", "rights": ["can_reply"], "enabled": True}, "a new id: asked, kept"
    assert [c[1]["business_connection_id"] for c in calls] == ["x", "c2"]
    assert g.state.last_in("777") == NOW and g.state.get("offset") == "11"
    assert g.state.confirm(items[2]["seq"]) == 3 and len(g.state.look()) == 3


def envelope(**changes):
    return dataclasses.replace(courier.Envelope(channel="telegram", account=str(OWNER), to=str(ANNA),
                                                text="В 7, до встречи.", name="Анна", reply_to_message=10), **changes)


def ask(env, key="ab12cd34"):
    return {"key": key, "envelope": env.as_dict(), "digest": courier.digest(env)}


def test_a_reply_goes_only_in_the_window_as_plain_text_and_once():
    g, calls = gateway({"sendMessage": {"message_id": 501}})
    g.state.set("connection", '{"id": "c1", "rights": ["can_reply"], "enabled": true}')
    ledger = courier.Ledger(":memory:")

    def send(env, key, now):
        async def transport(e, k):
            return await g.deliver(e, k, now=now)
        return asyncio.run(courier.send_once(ledger, ask(env, key), courier.TELEGRAM, transport, now=now))

    assert send(envelope(), "00000001", NOW) == {"status": "failed", "error": "window"}, "no incoming message yet"
    g.state.saw_in(str(ANNA), NOW)
    assert send(envelope(), "00000002", NOW + 24 * 3600 - 60) == {"status": "failed", "error": "window"}
    assert send(envelope(), "00000003", NOW + 3600) == {"status": "sent", "id": "501"}
    assert send(envelope(), "00000003", NOW + 3600)["repeat"] is True
    (method, params), = calls
    assert method == "sendMessage" and params == {
        "business_connection_id": "c1", "chat_id": ANNA, "text": "В 7, до встречи.",
        "link_preview_options": {"is_disabled": True},
        "reply_parameters": {"message_id": 10, "allow_sending_without_reply": False}}, "no parse_mode: the text as is"
    assert send(envelope(account="5"), "00000004", NOW)["error"] == "бот не подключён к аккаунту Владельца"

    async def boom(method, **params):
        raise tgbusiness.TelegramError(502, "Bad Gateway")
    g.call = boom
    assert send(envelope(text="Ещё."), "00000005", NOW + 60) == {"status": "unknown", "error": "502 Bad Gateway"}

    async def gone(method, **params):
        raise tgbusiness.TelegramError(400, "Bad Request: message to be replied not found")
    g.call = gone
    assert send(envelope(text="Ещё раз."), "00000006", NOW + 60)["status"] == "failed"


def test_the_router_reaches_the_gateway_with_its_token_and_sees_rights_by_name(monkeypatch):
    import pytest

    from retinue.config import GatewayConfig

    g, _ = gateway()
    g._keep({**CONNECTION, "rights": {"can_reply": True, "can_delete_all_messages": True}})
    g.state.add("777/10", {"chat": "777", "text": "x"})

    async def run():
        client = TestClient(TestServer(tgbusiness.Service(g, courier.Ledger(":memory:"), "tok").app()))
        await client.start_server()
        try:
            signed = {"Authorization": "Bearer tok"}
            unsigned = (await client.post("/items", json={})).status
            items = await (await client.post("/items", json={}, headers=signed)).json()
            status = await (await client.get("/status", headers=signed)).json()
            return unsigned, items, status
        finally:
            await client.close()

    unsigned, items, status = asyncio.run(run())
    assert unsigned == 401 and items["items"][0]["source"] == "telegram" and items["items"][0]["account"] == "telegram"
    assert status["connected"] is True and status["rights"] == ["can_delete_all_messages", "can_reply"]
    with pytest.raises(SystemExit):
        tgbusiness.Service(g, courier.Ledger(":memory:"), "")
    monkeypatch.setenv("RETINUE_GATEWAY_TOKEN", "g")
    monkeypatch.setenv("TELEGRAM_BUSINESS_TOKEN", "b")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "not a number")
    with pytest.raises(SystemExit):
        GatewayConfig.load()
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    assert GatewayConfig.load().owner_id == 42


def test_the_other_persons_file_is_served_by_the_id_an_item_carried_and_nothing_else():
    """A photo, a voice note, a document of the other person's: the item carries the file's id and size; the router
    asks for it by that id and gets the bytes the gateway took from Telegram. His own files and unknown ids — no."""
    import base64

    import httpx

    g, calls = gateway({"getFile": lambda p: {"file_id": p["file_id"], "file_size": 3, "file_path": f"x/{p['file_id']}"}})
    g._keep(CONNECTION)
    g.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"JPG") if request.url.path == "/file/bott/x/big-photo"
        else httpx.Response(404)))

    async def run():
        await g.consume({"update_id": 1, "business_message": message("", mid=20, caption="вот",
                                                                     photo=[{"file_id": "small"}, {"file_id": "big-photo",
                                                                                                   "file_size": 3}])})
        await g.consume({"update_id": 2, "business_message": message("", mid=21, voice={"file_id": "v1", "duration": 7,
                                                                                         "mime_type": "audio/ogg"})})
        await g.consume({"update_id": 3, "business_message": message("", sender=OWNER, mid=22,
                                                                     document={"file_id": "mine", "file_name": "cv.pdf"})})
        client = TestClient(TestServer(tgbusiness.Service(g, courier.Ledger(":memory:"), "tok").app()))
        await client.start_server()
        try:
            signed = {"Authorization": "Bearer tok"}
            got = await (await client.post("/file", json={"id": "big-photo"}, headers=signed)).json()
            mine = await client.post("/file", json={"id": "mine"}, headers=signed)
            gone = await (await client.post("/file", json={"id": "v1"}, headers=signed)).json()
            return got, mine.status, (await mine.json()), gone
        finally:
            await client.close()

    got, mine_status, mine, gone = asyncio.run(run())
    photo, voice, own = (item["data"] for item in g.state.look())
    assert photo["file"] == {"kind": "photo", "id": "big-photo", "name": "", "type": "", "size": 3, "duration": 0}
    assert voice["file"]["kind"] == "voice" and voice["file"]["duration"] == 7 and "file" not in own, "his own: no"
    assert base64.b64decode(got["data"]) == b"JPG"
    assert mine_status == 400 and mine == {"error": "такого файла не было в сообщениях"}
    assert gone["error"].startswith("Telegram не отдал файл (404"), "the reason, never the address with the token"
    assert all(method == "getFile" for method, _ in calls[-2:])
