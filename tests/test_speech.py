"""Speech to text without the network: what goes to ElevenLabs, what comes back, and every way it fails aloud."""

import asyncio

import httpx
import pytest

from retinue.speech import API, Scribe, SpeechError


def scribe(handler, key="k-test"):
    """A Scribe whose HTTP goes to `handler`; returns (scribe, requests seen, pauses taken)."""
    seen, pauses = [], []

    def record(request):
        seen.append(request)
        return handler(request, len([r for r in seen if r.method == "POST"]))

    async def pause(seconds):
        pauses.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return Scribe(key, client=client, sleep=pause), seen, pauses


def ok(request, attempt):
    if request.method == "DELETE":
        return httpx.Response(200, json={})
    return httpx.Response(200, json={"text": " Привет, это проверка. ", "language_code": "rus",
                                     "language_probability": 0.98, "transcription_id": "tr-1",
                                     "audio_duration_secs": 2.5})


def test_one_request_no_language_then_the_transcript_is_deleted():
    s, seen, _ = scribe(ok)
    text = asyncio.run(s.transcribe(b"OggS...", "voice.ogg", "audio/ogg"))
    assert text == "Привет, это проверка."
    post, delete = seen
    assert (post.method, str(post.url)) == ("POST", API) and post.headers["xi-api-key"] == "k-test"
    body = post.content
    for field, value in (("model_id", b"scribe_v2"), ("tag_audio_events", b"false"),
                         ("timestamps_granularity", b"none")):
        assert f'name="{field}"\r\n\r\n'.encode() + value in body
    assert b'name="language_code"' not in body, "no language: the owner mixes Russian and English"
    assert b'filename="voice.ogg"' in body and b"Content-Type: audio/ogg" in body and b"OggS..." in body
    assert (delete.method, str(delete.url)) == ("DELETE", f"{API}/transcripts/tr-1"), "nothing is left at ElevenLabs"
    assert delete.headers["xi-api-key"] == "k-test"


def test_busy_is_waited_out_and_a_failed_delete_does_not_lose_the_text():
    def busy_twice(request, attempt):
        if request.method == "DELETE":
            return httpx.Response(500)
        if attempt == 1:
            return httpx.Response(429, json={"detail": {"status": "concurrent_limit_exceeded"}})
        if attempt == 2:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": {"status": "system_busy"}})
        return ok(request, attempt)

    s, seen, pauses = scribe(busy_twice)
    assert asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg")) == "Привет, это проверка."
    assert pauses == [2.0, 7.0], "backoff doubles; Retry-After wins when it is given"
    assert [r.method for r in seen] == ["POST", "POST", "POST", "DELETE"]


@pytest.mark.parametrize("status, detail, reason", [
    (429, "rate_limit_exceeded", "ElevenLabs перегружен"),
    (402, "insufficient_credits", "кончились кредиты ElevenLabs"),
    (401, "quota_exceeded", "кончились кредиты ElevenLabs"),
    (401, "invalid_api_key", "ElevenLabs не принял ключ"),
    (422, "invalid_file", "ElevenLabs не принял файл"),
    (500, "", "ElevenLabs ответил 500"),
])
def test_failures_are_told_with_the_reason(status, detail, reason):
    s, seen, pauses = scribe(lambda request, attempt: httpx.Response(status, json={"detail": {"status": detail}}))
    with pytest.raises(SpeechError, match=reason):
        asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg"))
    assert len(seen) == (4 if status == 429 else 1) and len(pauses) == (3 if status == 429 else 0)


def test_no_key_no_request_and_no_network():
    s, seen, _ = scribe(ok, key="")
    with pytest.raises(SpeechError, match="нет ключа ElevenLabs"):
        asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg"))
    assert seen == []

    def down(request, attempt):
        raise httpx.ConnectError("connection refused")

    s, _, _ = scribe(down)
    with pytest.raises(SpeechError, match="ElevenLabs недоступен"):
        asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg"))


def test_a_200_that_is_not_json_is_an_error_not_a_crash():
    s, _, _ = scribe(lambda request, attempt: httpx.Response(200, content=b"<html>proxy</html>"))
    with pytest.raises(SpeechError, match="ElevenLabs ответил не JSON"):
        asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg"))


def test_a_transcript_that_could_not_be_deleted_is_deleted_later(tmp_path, caplog):
    from retinue.protocol import Store

    store = Store(str(tmp_path / "r.sqlite"))
    deletes = []

    def first(request, attempt):
        if request.method == "DELETE":
            deletes.append(str(request.url))
            return httpx.Response(503)
        return ok(request, attempt)

    s, _, _ = scribe(first)
    s.store = store
    assert asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg")) == "Привет, это проверка."
    assert s.pending() == ["tr-1"], "kept in the router's store until ElevenLabs confirms the deletion"

    def later(request, attempt):
        deletes.append(str(request.url))
        return httpx.Response(404 if request.url.path.endswith("tr-2") else 200)

    again, _, _ = scribe(later)
    again.store = store
    store.set("stt.pending", '["tr-1", "tr-2"]')
    asyncio.run(again.sweep())
    assert again.pending() == [] and deletes[-2:] == [f"{API}/transcripts/tr-1", f"{API}/transcripts/tr-2"], \
        "swept on start: deleted, or already gone"

    s, _, _ = scribe(lambda request, attempt: httpx.Response(200, json={"text": "без номера"}))
    with caplog.at_level("WARNING", logger="retinue.speech"):
        assert asyncio.run(s.transcribe(b"x", "voice.ogg", "audio/ogg")) == "без номера"
    assert "no transcription_id" in caplog.text
