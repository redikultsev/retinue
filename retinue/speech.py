"""Speech to text: ElevenLabs Scribe over plain HTTP, no SDK. Only the router calls it — it has the internet and the
key; the assistant sees neither, and the owner never sees the transcript.

One synchronous request per file: the bytes as the messenger gave them (OGG, MP3, MP4 are taken as they are), no
language (the owner mixes Russian and English), no audio-event tags, no timestamps. Then the transcript is deleted
at ElevenLabs: how long they keep it is not published. A transcript whose deletion failed stays in the router's
store (`stt.pending`) and is deleted again when the router starts.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

log = logging.getLogger("retinue.speech")

API = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"
TIMEOUT = httpx.Timeout(300, connect=10)  # ElevenLabs publishes no latency for files: «seconds to minutes»
RETRIES = 3          # 429 means a concurrency or rate limit, or a busy system: wait and send again
BACKOFF_S = 2.0
MAX_WAIT_S = 30.0
NO_CREDITS = ("insufficient_credits", "quota_exceeded")


class SpeechError(Exception):
    """The text finishes «Не удалось расшифровать: …» to the owner."""


class Scribe:
    def __init__(self, key: str, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep, store=None) -> None:
        self.key = key
        self.client = client
        self.sleep = sleep
        self.store = store  # the router's key-value store (`protocol.Store`): transcripts still to delete

    def pending(self) -> list[str]:
        return json.loads(self.store.get("stt.pending") or "[]") if self.store else []

    def _keep(self, ids: list[str]) -> None:
        if self.store:
            self.store.set("stt.pending", json.dumps(ids))

    async def sweep(self) -> None:
        """Delete the transcripts an earlier run could not: on the router's start."""
        if not self.key or not self.pending():
            return
        http = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            for transcription_id in self.pending():
                await self._forget(http, transcription_id)
        finally:
            if self.client is None:
                await http.aclose()

    async def transcribe(self, data: bytes, filename: str, media_type: str) -> str:
        """The text of the speech in the file; empty when nobody speaks. Raises SpeechError with the reason."""
        if not self.key:
            raise SpeechError("расшифровка не настроена — нет ключа ElevenLabs (ELEVENLABS_API_KEY)")
        http = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await self._post(http, data, filename, media_type)
            try:
                body = response.json()
            except ValueError:
                raise SpeechError("ElevenLabs ответил не JSON") from None
            if not isinstance(body, dict):
                raise SpeechError("ElevenLabs ответил не тем")
            text = str(body.get("text") or "").strip()
            # Never the text itself: the log is not the archive.
            log.info("transcribed %s s, language %s (%s)", body.get("audio_duration_secs"), body.get("language_code"),
                     body.get("language_probability"))
            if body.get("transcription_id"):
                self._keep([*self.pending(), str(body["transcription_id"])])
                await self._forget(http, str(body["transcription_id"]))
            else:
                log.warning("speech to text: no transcription_id, the transcript cannot be deleted")
            return text
        finally:
            if self.client is None:
                await http.aclose()

    async def _post(self, http: httpx.AsyncClient, data: bytes, filename: str, media_type: str) -> httpx.Response:
        for attempt in range(RETRIES + 1):
            try:
                response = await http.post(
                    API, headers={"xi-api-key": self.key},
                    data={"model_id": MODEL, "tag_audio_events": "false", "timestamps_granularity": "none"},
                    files={"file": (filename, data, media_type)}, timeout=TIMEOUT)
            except httpx.HTTPError as exc:
                raise SpeechError(f"ElevenLabs недоступен ({type(exc).__name__})") from exc
            if response.status_code != 429 or attempt == RETRIES:
                break
            retry_after = response.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else BACKOFF_S * 2 ** attempt
            await self.sleep(min(wait, MAX_WAIT_S))
        if response.status_code == 200:
            return response
        try:
            detail = response.json().get("detail") or {}
        except ValueError:
            detail = {}
        status = detail.get("status", "") if isinstance(detail, dict) else ""
        log.warning("speech to text failed: %s %s", response.status_code, status)
        if response.status_code == 402 or status in NO_CREDITS:
            raise SpeechError("кончились кредиты ElevenLabs — пополни баланс")
        if response.status_code == 429:
            raise SpeechError("ElevenLabs перегружен — пришли ещё раз чуть позже")
        if response.status_code == 401:
            raise SpeechError("ElevenLabs не принял ключ")
        if response.status_code == 422:
            raise SpeechError("ElevenLabs не принял файл")
        raise SpeechError(f"ElevenLabs ответил {response.status_code}")

    async def _forget(self, http: httpx.AsyncClient, transcription_id: str) -> None:
        """Delete the transcript at ElevenLabs. A failure costs privacy, not the answer: logged, kept for the next
        sweep. Already gone (404) counts as deleted."""
        try:
            response = await http.delete(f"{API}/transcripts/{transcription_id}", headers={"xi-api-key": self.key})
        except httpx.HTTPError as exc:
            log.warning("transcript %s not deleted: %s", transcription_id, type(exc).__name__)
            return
        if response.status_code >= 300 and response.status_code != 404:
            log.warning("transcript %s not deleted: %s", transcription_id, response.status_code)
            return
        self._keep([pending for pending in self.pending() if pending != transcription_id])
