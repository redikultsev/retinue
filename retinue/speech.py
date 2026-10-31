"""Speech to text: ElevenLabs Scribe over plain HTTP, no SDK. Two callers, each with the key and a way out; the
assistant and the lanes see neither. The router transcribes Telegram's voice notes; the owner sees a transcript only
when his voice goes into a platform session (`bridge.PlatformBridge.spoken`): what was heard is said back to him, so a
misheard word shows at once. The platform transcribes what the owner dictates in the app (`face.Face.speech`),
through its own proxy (`platform-egress`).

One synchronous request per file: the bytes as the messenger or the browser gave them (OGG, MP3, MP4, WebM are taken
as they are), no audio-event tags, no timestamps, and by default no language (the owner mixes Russian and English).
The app's dictation adds `no_verbatim` and the owner's terms (`keyterms`); its English mode asks for English, word for
word. Then the transcript is deleted at ElevenLabs: how long they keep it is not published. A transcript whose
deletion failed stays in the caller's store (`stt.pending`) and is deleted again when it starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import IO

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
    """The text finishes «Could not transcribe: …» to the owner."""


class Scribe:
    def __init__(self, key: str, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep, store=None,
                 proxy: str = "") -> None:
        self.key = key
        self.client = client
        self.sleep = sleep
        self.store = store  # the caller's key-value store (`get`/`set`): transcripts still to delete
        self.proxy = proxy  # '' : straight out (the router); the platform's own proxy otherwise

    def _http(self) -> httpx.AsyncClient:
        return self.client or httpx.AsyncClient(timeout=TIMEOUT, proxy=self.proxy or None)

    def pending(self) -> list[str]:
        return json.loads(self.store.get("stt.pending") or "[]") if self.store else []

    def _keep(self, ids: list[str]) -> None:
        if self.store:
            self.store.set("stt.pending", json.dumps(ids))

    async def sweep(self) -> None:
        """Delete the transcripts an earlier run could not: on the router's start."""
        if not self.key or not self.pending():
            return
        http = self._http()
        try:
            for transcription_id in self.pending():
                await self._forget(http, transcription_id)
        finally:
            if self.client is None:
                await http.aclose()

    async def transcribe(self, data: bytes | IO[bytes], filename: str, media_type: str, *, language: str = "",
                         no_verbatim: bool | None = None, keyterms: list[str] | tuple[str, ...] = ()) -> str:
        """The text of the speech in the file; empty when nobody speaks. Raises SpeechError with the reason.
        `language` ('' : Scribe guesses), `no_verbatim` (None : Scribe's default, which keeps fillers and false
        starts) and `keyterms` are sent only when given: the router's request stays as it was."""
        if not self.key:
            raise SpeechError("transcription is not set up — no ElevenLabs key (ELEVENLABS_API_KEY)")
        fields: dict[str, str | list[str]] = {"model_id": MODEL, "tag_audio_events": "false",
                                              "timestamps_granularity": "none"}
        if language:
            fields["language_code"] = language
        if no_verbatim is not None:
            fields["no_verbatim"] = "true" if no_verbatim else "false"
        if keyterms:
            fields["keyterms"] = list(keyterms)  # a repeated form field, one per term
        http = self._http()
        try:
            response = await self._post(http, data, filename, media_type, fields)
            try:
                body = response.json()
            except ValueError:
                raise SpeechError("ElevenLabs answered with no JSON") from None
            if not isinstance(body, dict):
                raise SpeechError("ElevenLabs answered with something else")
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

    async def _post(self, http: httpx.AsyncClient, data: bytes | IO[bytes], filename: str, media_type: str,
                    fields: dict) -> httpx.Response:
        for attempt in range(RETRIES + 1):
            try:
                response = await http.post(API, headers={"xi-api-key": self.key}, data=fields,
                                           files={"file": (filename, data, media_type)}, timeout=TIMEOUT)
            except httpx.HTTPError as exc:
                raise SpeechError(f"ElevenLabs is unavailable ({type(exc).__name__})") from exc
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
            raise SpeechError("ElevenLabs is out of credits — top up the balance")
        if response.status_code == 429:
            raise SpeechError("ElevenLabs is overloaded — send it again a bit later")
        if response.status_code == 401:
            raise SpeechError("ElevenLabs did not accept the key")
        if response.status_code == 422:
            raise SpeechError("ElevenLabs did not accept the file")
        raise SpeechError(f"ElevenLabs answered {response.status_code}")

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
