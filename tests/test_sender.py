"""The mail sender with a fake Gmail: it sends exactly the envelope whose digest it is given, each key once, and
never calls a doubtful send a failure. No network, no Google."""

import asyncio
import base64
import dataclasses
import email
import email.policy
import json

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from retinue import courier, google, sender

from test_courier import CHAT, LETTER


def gmail(answers, sent):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            refresh = dict(x.split("=", 1) for x in request.content.decode().split("&"))["refresh_token"]
            if refresh == "dead":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        body = json.loads(request.content)
        sent.append(email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=email.policy.default))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def ask(envelope, key="ab12cd34", digest=None):
    return {"key": key, "envelope": envelope.as_dict(), "digest": digest or courier.digest(envelope)}


def serve(accounts, calls):
    service = sender.Service(courier.Ledger(":memory:"), lambda: accounts, "tok")

    async def run():
        client = TestClient(TestServer(service.app()))
        await client.start_server()
        try:
            out = []
            for body, headers in calls:
                response = await client.post("/send", json=body, headers=headers)
                out.append((response.status, await response.json()))
            status = await (await client.get("/status", headers={"Authorization": "Bearer tok"})).json()
            return out, status
        finally:
            await client.close()

    return asyncio.run(run())


SIGNED = {"Authorization": "Bearer tok"}


def test_a_letter_goes_once_and_only_as_the_owner_confirmed_it():
    answers, sent = [httpx.Response(200, json={"id": "g1", "threadId": "18c2f0a1b2c3d4e5"})], []
    mailbox = google.Google(google.Key("owner@example.org", "send", "rt", "cid", "cs"), gmail(answers, sent))
    swapped = dataclasses.replace(LETTER, to="evil@other.example")
    out, status = serve([mailbox], [
        (ask(LETTER), {}),                                           # unsigned
        (ask(swapped, digest=courier.digest(LETTER)), SIGNED),       # another recipient under the owner's digest
        (ask(LETTER, key="../x"), SIGNED),
        (ask(CHAT), SIGNED),                                         # not this sender's channel
        (ask(LETTER), SIGNED),
        (ask(LETTER), SIGNED),                                       # pressed twice, the router restarted
    ])
    assert out[0] == (401, {"error": "unauthorized"})
    assert out[1] == (400, {"status": "refused", "error": "конверт не совпал с подтверждённым"})
    assert out[2][1]["error"] == "ключ: не тот" and out[3][1]["status"] == "refused"
    assert out[4] == (200, {"status": "sent", "id": "g1", "thread": "18c2f0a1b2c3d4e5"})
    assert out[5] == (200, {"status": "sent", "id": "g1", "thread": "18c2f0a1b2c3d4e5", "repeat": True})
    (letter,) = sent
    assert letter["To"] == "hr@acme.example" and letter["Message-ID"] == "<retinue.ab12cd34@example.org>"
    assert letter.get_content().strip() == LETTER.text
    assert status == {"accounts": ["owner@example.org"], "dead": {}, "day": 1}
    assert "rt" not in json.dumps(status), "names, never values"


def test_a_doubtful_send_is_unknown_forever_and_a_dead_token_is_named():
    answers, sent = [httpx.ReadTimeout("slow")], []
    live = google.Google(google.Key("owner@example.org", "send", "rt", "cid", "cs"), gmail(answers, sent))
    dead = google.Google(google.Key("work@example.org", "send", "dead", "cid", "cs"), gmail([], sent))
    other = dataclasses.replace(LETTER, account="work@example.org")
    nobody = dataclasses.replace(LETTER, account="old@example.org")
    out, status = serve([live, dead], [
        (ask(LETTER, "00000001"), SIGNED), (ask(LETTER, "00000001"), SIGNED),
        (ask(other, "00000002"), SIGNED), (ask(nobody, "00000003"), SIGNED),
    ])
    assert [o[1]["status"] for o in out] == ["unknown", "unknown", "failed", "failed"]
    assert out[1][1]["repeat"] is True and len(sent) == 1, "a timeout after the request left: never sent again"
    assert out[2][1]["error"] == "invalid_grant" and out[3][1]["error"] == "нет ключа отправки для old@example.org"
    assert status["dead"] == {"work@example.org": "invalid_grant"}


def test_a_key_taken_before_a_crash_is_unknown_and_the_sender_needs_its_token(monkeypatch):
    from retinue.config import SenderConfig

    ledger, called = courier.Ledger(":memory:"), []

    async def transport(envelope, key):
        called.append(key)
        return "sent", {"id": "x"}

    ledger.claim("ab12cd34", courier.digest(LETTER), LETTER.to, 0)  # the process died during the call
    done = asyncio.run(courier.send_once(ledger, ask(LETTER), courier.MAIL, transport))
    assert done == {"status": "unknown", "repeat": True} and called == []
    with pytest.raises(SystemExit):
        sender.Service(ledger, list, "")
    monkeypatch.delenv("RETINUE_SENDER_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        SenderConfig.load()
    monkeypatch.setenv("RETINUE_SENDER_TOKEN", "t")
    assert SenderConfig.load() == SenderConfig("/keys", "/state/sender.sqlite", 9400, "t")
