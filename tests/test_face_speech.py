"""The owner's voice in the app (`/api/speech`): behind the same login and header as every action, one recording at a
time and 20 MB at most, each mode mapped to Scribe's own options, every failure a short line — and the transcript
deleted at ElevenLabs, never logged."""

from __future__ import annotations

import asyncio
import contextlib
import json

import aiohttp
import httpx
import pytest
from aiohttp import web

from retinue import face
from retinue.speech import API, Scribe
from test_face import POST, FaceWorld, faced
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse

PROXY = "http://platform-egress:3128"
HEARD = "Check the KV cache and make an eval for RAG"


class Ears:
    """ElevenLabs as the platform's Scribe sees it: every request kept, the answer from `answer`."""

    def __init__(self, answer=None):
        self.seen: list[httpx.Request] = []
        self.answer = answer or (lambda request: httpx.Response(200, json={"text": HEARD, "transcription_id": "tr-9"}))
        self.release: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        if self.release is not None:
            await self.release.wait()
        return self.answer(request)

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.method == "POST"]


def hear(world: FaceWorld, ears: Ears, key: str = "sk-el", terms=("KV cache", "RAG")) -> None:
    world.cfg.speech.proxy, world.cfg.speech.keyterms = PROXY, list(terms)
    world.face.scribe = Scribe(key, client=httpx.AsyncClient(transport=httpx.MockTransport(ears.handle)),
                               store=world.store, proxy=PROXY)


async def say(world: FaceWorld, body: bytes = b"\x1aE\xdf\xa3webm", mode: str | None = "dictate",
              kind: str = "audio/webm;codecs=opus", headers: dict | None = None, cookie: bool = True):
    given = {**(POST if headers is None else headers), "Content-Type": kind}
    if cookie and world.cookie:
        given["Cookie"] = f"{face.COOKIE}={world.cookie}"
    path = "/api/speech" + (f"?mode={mode}" if mode else "")
    response = await world.api.request("POST", path, data=body, headers=given)
    return response.status, await response.json()


def sent(request: httpx.Request) -> dict[str, list[bytes]]:
    out: dict[str, list[bytes]] = {}
    for part in (b"\r\n" + request.content).split(b"\r\n--")[1:]:
        head, _, value = part.partition(b"\r\n\r\n")
        if b"filename=" in head or not value:
            continue
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        out.setdefault(name, []).append(value.removesuffix(b"\r\n"))
    return out


def test_only_the_owner_from_the_app_page(tmp_path):
    async def body(world: FaceWorld):
        ears = Ears()
        hear(world, ears)
        assert (await say(world, cookie=False))[0] == 401, "no login, no listening"
        await world.login()
        assert (await say(world, headers={}))[0] == 403, "no X-Retinue: not from the app's page"
        assert (await say(world, headers={**POST, "Origin": "https://evil.example"}))[0] == 403
        assert ears.seen == [], "nothing reached ElevenLabs"

    faced(tmp_path, body)


def test_dictation_is_scribes_clean_text_with_the_terms_and_no_language(tmp_path, caplog):
    async def body(world: FaceWorld):
        ears = Ears()
        hear(world, ears)
        await world.login()
        with caplog.at_level("DEBUG"):
            status, data = await say(world, mode=None)
        assert (status, data) == (200, {"text": HEARD}), "dictate is the default; the text only — he edits and sends"
        post, delete = ears.seen
        fields = sent(post)
        assert fields["no_verbatim"] == [b"true"] and fields["keyterms"] == [b"KV cache", b"RAG"]
        assert "language_code" not in fields, "he mixes Russian and English"
        assert b'filename="voice.webm"' in post.content and b"Content-Type: audio/webm" in post.content
        assert (delete.method, str(delete.url)) == ("DELETE", f"{API}/transcripts/tr-9"), "nothing left there"
        assert world.store.get("stt.pending") == "[]", "kept in the platform's store until deleted"
        assert HEARD not in caplog.text and "KV cache" not in caplog.text, "never the words in a log"
        assert world.store.db.execute("SELECT COUNT(*) FROM inputs").fetchone()[0] == 0, "nothing sent anywhere"

    faced(tmp_path, body)


def test_english_is_word_for_word_and_comes_back_as_the_message(tmp_path):
    async def body(world: FaceWorld):
        ears = Ears(lambda request: httpx.Response(200, json={"text": "So, um, I think, uh, it works",
                                                              "transcription_id": "tr-1"}))
        hear(world, ears)
        await world.login()
        status, data = await say(world, mode="en", kind="audio/mp4")
        assert status == 200 and data == {"text": "So, um, I think, uh, it works",
                                          "message": "(by voice, verbatim) So, um, I think, uh, it works"}
        fields = sent(ears.posts()[0])
        assert fields["language_code"] == [b"en"] and fields["no_verbatim"] == [b"false"]
        assert "keyterms" not in fields, "no terms: what he said, not what he meant"
        assert b'filename="voice.m4a"' in ears.posts()[0].content

    faced(tmp_path, body)


def test_what_is_refused_before_anything_is_sent(tmp_path):
    async def body(world: FaceWorld):
        ears = Ears()
        hear(world, ears)
        await world.login()
        assert await say(world, mode="tts") == (400, {"error": "mode: dictate | en"})
        assert (await say(world, kind="application/json"))[0] == 415
        assert (await say(world, body=b""))[0] == 400
        too_big = b"\0" * (face.SPEECH_MAX + 1)
        status, data = await say(world, body=too_big)
        assert status == 413 and "10 minutes" in data["error"], "by its length"

        async def stream():
            for _ in range(face.SPEECH_MAX // face.SPEECH_CHUNK + 2):
                yield b"\0" * face.SPEECH_CHUNK

        response = await world.api.request("POST", "/api/speech", data=stream(), headers={
            **POST, "Content-Type": "audio/webm", "Cookie": f"{face.COOKIE}={world.cookie}"})
        assert response.status == 413, "and by what came, when no length was given"
        assert ears.seen == []

        hear(world, ears, key="")
        status, data = await say(world)
        assert status == 503 and "no ElevenLabs key" in data["error"]
        assert (await world.get("/api/meta"))["speech"] is False, "the app knows before it records"
        hear(world, ears)
        world.cfg.speech.proxy = ""
        status, data = await say(world)
        assert status == 503 and "no way to reach ElevenLabs" in data["error"]
        world.cfg.speech.proxy = PROXY
        assert (await world.get("/api/meta"))["speech"] is True
        assert ears.seen == []

    faced(tmp_path, body)


def test_one_recording_at_a_time(tmp_path):
    async def body(world: FaceWorld):
        ears = Ears()
        ears.release = asyncio.Event()
        hear(world, ears)
        await world.login()
        first = asyncio.create_task(say(world))
        while not ears.posts():
            await asyncio.sleep(0.01)
        status, data = await say(world)
        assert status == 429 and "Already transcribing" in data["error"]
        ears.release.set()
        assert (await first)[0] == 200
        assert (await say(world))[0] == 200, "free again"

    faced(tmp_path, body)


def test_every_failure_is_a_short_line(tmp_path):
    async def body(world: FaceWorld):
        await world.login()
        for answer, status, line in [
            (httpx.Response(402, json={"detail": {"status": "insufficient_credits"}}), 502,
             "Could not transcribe: ElevenLabs is out of credits — top up the balance."),
            (httpx.Response(422, json={"detail": {"status": "invalid_file"}}), 502,
             "Could not transcribe: ElevenLabs did not accept the file."),
            (httpx.Response(200, json={"text": "  ", "transcription_id": "tr-2"}), 422,
             "Could not make out the speech — record again."),
        ]:
            hear(world, Ears(lambda request, answer=answer: answer))
            assert await say(world) == (status, {"error": line})

    faced(tmp_path, body)


def test_a_transcript_left_at_elevenlabs_is_deleted_at_the_next_start(tmp_path):
    async def body(world: FaceWorld):
        ears = Ears()
        world.store.set("stt.pending", json.dumps(["tr-old"]))
        hear(world, ears)
        await world.face._sweep(None)
        await world.face.sweeper
        assert [str(r.url) for r in ears.seen] == [f"{API}/transcripts/tr-old"]
        assert world.store.get("stt.pending") == "[]"

    faced(tmp_path, body)


def test_nginx_lets_a_recording_through_whole_and_unspooled():
    """A recording up to the platform's own cap, streamed as it comes (never spooled to the face's 16 MB /tmp), and
    the app's microphone allowed for the app alone."""
    from pathlib import Path

    from test_deploy import blocks

    nginx = (Path(__file__).parent.parent / "deploy" / "face" / "nginx.conf").read_text()
    server = nginx[nginx.index("server {") + len("server {"):]
    location = dict(blocks(server))["= /api/speech"]
    assert f"client_max_body_size {face.SPEECH_MAX // 2**20}m;" in location
    assert "proxy_request_buffering off;" in location and "proxy_read_timeout 300s;" in location
    assert 'Permissions-Policy "microphone=(self),' in nginx


async def until(check, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not in time")


@contextlib.asynccontextmanager
async def server(world: FaceWorld, kind: str):
    """A coroutine that posts a recording. `cancelled`: the test server, which cancels a handler whose client went
    away; `polled`: the platform's own runner, which does not — the handler has to notice."""
    if kind == "cancelled":
        yield lambda: say(world)
        return
    runner = web.AppRunner(world.face.app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as http:
        async def post():
            async with http.post(f"http://127.0.0.1:{port}/api/speech", data=b"webm", headers={
                    **POST, "Content-Type": "audio/webm", "Cookie": f"{face.COOKIE}={world.cookie}"}) as response:
                return response.status, await response.json()
        yield post
    await runner.cleanup()


@pytest.mark.parametrize("kind", ["cancelled", "polled"])
def test_cancelled_after_elevenlabs_has_it_finishes_in_the_background_and_frees_the_next(tmp_path, kind):
    """«Cancel» once the recording reached ElevenLabs: it is billed anyway, so the call ends — its transcript deleted
    there — but the app's next recording does not wait for it, and nothing comes back."""
    async def body(world: FaceWorld):
        ears = Ears()
        ears.release = asyncio.Event()
        hear(world, ears)
        await world.login()
        async with server(world, kind) as post:
            first = asyncio.create_task(post())
            await until(lambda: ears.posts())
            first.cancel()  # the app aborts its fetch: the connection closes
            await until(lambda: not world.face.hearing)
            [finishing] = world.face.finishing
            ears.release.set()
            await finishing
            assert [r.method for r in ears.seen] == ["POST", "DELETE"], "deleted at ElevenLabs all the same"
            assert world.face.finishing == set()
            assert (await post())[0] == 200

    faced(tmp_path, body)


@pytest.mark.parametrize("kind", ["cancelled", "polled"])
def test_cancelled_while_it_is_being_sent_is_never_sent(tmp_path, kind):
    async def body(world: FaceWorld):
        gate, posted = asyncio.Event(), []

        class Slow(httpx.AsyncBaseTransport):
            """ElevenLabs on a slow line: the request's body is not taken until the gate opens."""

            async def handle_async_request(self, request):
                await gate.wait()
                await request.aread()
                posted.append(request)
                return httpx.Response(200, json={"text": HEARD, "transcription_id": "tr-3"})

        world.cfg.speech.proxy = PROXY
        world.face.scribe = Scribe("sk-el", client=httpx.AsyncClient(transport=Slow()), store=world.store)
        await world.login()
        started = asyncio.Event()
        original = world.face.scribe.transcribe

        async def watched(*args, **kwargs):
            started.set()
            return await original(*args, **kwargs)

        world.face.scribe.transcribe = watched
        async with server(world, kind) as post:
            first = asyncio.create_task(post())
            await started.wait()
            first.cancel()
            await until(lambda: not world.face.hearing)
            gate.set()
            await asyncio.sleep(0.1)
            assert posted == [] and world.face.finishing == set(), "cancelled before ElevenLabs had it"

    faced(tmp_path, body)
