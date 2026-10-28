"""Probe: the pinned lane CLI takes the owner's attachments as `inbox.prepare` makes them — `image` and `document`
blocks in one stream-json user line — and hands them to the model as they are, on the first turn and again on a
resume. Runs against the fake Messages API (no credential), skipped without the pinned binary like every probe.

Whether the model then sees the picture is a property of the API, not of the CLI; it was checked once with a real
login (docs/platform.md, «What a session gets»)."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image
from pypdf import PdfWriter

from fakeapi import FakeApi
from retinue import cliengine, inbox
from test_probes import Lane, pytestmark  # noqa: F401 — the probe mark and the skip without the pinned CLI


class RecordingApi(FakeApi):
    """The fake API that keeps every streamed request's messages."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict] = []

    def reply(self, body: dict) -> bytes:
        self.requests.append(body)
        return super().reply(body)


@pytest.fixture
def api():
    with RecordingApi() as fake:
        yield fake


def _blocks(request: dict) -> list[dict]:
    return [block for message in request["messages"] if message["role"] == "user"
            for block in (message["content"] if isinstance(message["content"], list) else [])]


def test_image_and_document_blocks_reach_the_model_whole_and_stay_in_the_session(tmp_path, api):
    picture = io.BytesIO()
    Image.new("RGB", (64, 32), (255, 200, 0)).save(picture, "PNG")
    writer, document = PdfWriter(), io.BytesIO()
    writer.add_blank_page(100, 100)
    writer.write(document)
    ready = asyncio.run(inbox.prepare("Что на картинке?", [inbox.Given("", "image/png", picture.getvalue()),
                                                           inbox.Given("a.pdf", "application/pdf",
                                                                       document.getvalue())]))
    cliengine.check_content(ready.prompt)
    lane = Lane(tmp_path, api)

    first = lane.run(ready.prompt)
    assert first.rc == 0, first.stderr
    assert not any(line.get("type") == "user" and "image" in str(line) for line in first.lines), \
        "the owner's line is not echoed into the journal"
    sent = _blocks(api.requests[-1])
    image = next(b for b in sent if b["type"] == "image")
    pdf = next(b for b in sent if b["type"] == "document")
    assert image["source"] == ready.prompt[1]["source"], "the platform's JPEG, byte for byte"
    assert pdf["source"] == ready.prompt[2]["source"] and pdf["title"] == "a.pdf"
    assert any(b["type"] == "text" and b["text"].startswith("Что на картинке?") for b in sent)
    with Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))) as img:
        assert img.format == "JPEG" and img.size == (64, 32)

    again = lane.run("А теперь?", resume=first.result["session_id"])
    assert again.rc == 0, again.stderr
    assert any(b["type"] == "image" and b["source"] == image["source"] for b in _blocks(api.requests[-1])), \
        "a resume carries the picture in the session's history"
