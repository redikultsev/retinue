"""What the owner sends a session besides text — a photo, a PDF, any other file — made ready for its lane by code, in
the platform, before any run. Telegram (through the router) and the face hand over the same thing: each file whole,
in base64, with its SHA-256.

- A picture becomes an `image` block: read by the router's own reader (`attachments.isolated` — its worker process,
  its deadline and memory ceiling), turned upright, its long side at most 1568 px, re-encoded as JPEG. A picture that
  does not decode is refused aloud: a block the API rejects would sit in the session and break every later turn.
- A PDF short enough to read whole (`attachments.PDF_PAGES`, `attachments.PDF_BYTES`) becomes a `document` block,
  opened by the same reader first. A longer one, or one that does not open, goes in as a file.
- Anything else goes into the session's own folder as a file, `inbox/<date>-<name>`: the lane host writes it and
  tells the session where (`lane_host.with_saved`). The session reads it with its own tools; nothing here opens it.

What the owner typed comes first, then one line naming what came with it; each picture also keeps a small thumbnail
for the face's transcript. Nothing here is the assistant's: her files never reach a session, and a forwarded file
never comes this way (the router keeps every forward hers).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from PIL import Image

from . import attachments, cliengine
from .attachments import Unreadable, Upload

MAX_FILES = cliengine.INBOX_FILES        # Telegram's album holds ten
MAX_FILE = attachments.MAX_DOWNLOAD     # what the Bot API gives a bot: 20 MB; the face keeps to the same
MAX_TOTAL = cliengine.INBOX_BYTES       # one message's files together
# What the platform's API takes for one message: the files in base64 and the rest of the body.
BODY_MAX = MAX_TOTAL * 4 // 3 + 4 * 1024 * 1024
THUMB_SIDE = 320
THUMB_QUALITY = 70
NAME_CHARS = 80
READERS = 1                             # worker processes at once, whatever the messages: the platform's container
# What every file of one message may take to read, together: below the router's wait for the platform's answer
# (bridge.FILES_TIMEOUT_S, 300 s) and nginx's (300 s). A file not reached by then is said, and left out.
PARSE_BUDGET_S = 240
LAST_READ_S = 5                         # a file is not started with less than this left
# Never in a name the owner reads or a file gets: control characters, and the bidi overrides that make «exe.pdf» of
# «fdp.exe».
UNSHOWN = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
# A worker's address space here: the platform's container is smaller than the router's (deploy/compose.yml).
READER_MEMORY = 1024 * 1024 * 1024
NOT_PASSED = "Not passed to the session"
# Pictures the reader re-encodes; any other image type (SVG, PSD, an icon) is a file like any other.
RASTER = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff", "image/heic", "image/heif"}
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
              "image/heic": ".heic", "application/pdf": ".pdf", "text/plain": ".txt"}


_readers: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _reading() -> asyncio.Semaphore:
    """The one semaphore of the platform's parse workers (READERS), for the running loop."""
    global _readers
    loop = asyncio.get_running_loop()
    if _readers is None or _readers[0] is not loop:
        _readers = (loop, asyncio.Semaphore(READERS))
    return _readers[1]


def clean(name: str) -> str:
    """A name as the owner and the session read it: no control character, no bidi override."""
    return UNSHOWN.sub("", name).strip()


class Refused(Exception):
    """Nothing of this message goes in. The text finishes «Not passed to the session: …»."""


@dataclass
class Given:
    name: str          # the file's own name, as the owner's device gave it; '' for a Telegram photo
    media_type: str
    data: bytes


@dataclass
class Ready:
    """One message for the lane, and what the face shows of it."""

    prompt: str | list[dict]                              # the CLI's content: text, or blocks
    files: list[dict] = field(default_factory=list)       # for the inbox: `cliengine.check_files`
    meta: list[dict] = field(default_factory=list)        # each attachment that went in: {kind, name, size, what}
    thumbs: list[bytes | None] = field(default_factory=list)  # per `meta`: a picture's thumbnail
    refused: list[str] = field(default_factory=list)      # what did not go in, and why

    @property
    def empty(self) -> bool:
        return not self.meta and not (self.prompt if isinstance(self.prompt, str) else True)

    def lane(self, date: str) -> dict:
        """What the queue keeps and the lane host is sent: the content, and the files with their date."""
        return {"prompt": self.prompt, **({"files": self.files, "date": date} if self.files else {})}


def given(raw: object) -> list[Given]:
    """The attachments of one message as an API took them: `[{name, media_type, data, sha256}]`, each whole (its
    digest matches), none empty, each and all within the limits. Refused whole otherwise."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        raise Refused(f"more than {MAX_FILES} attachments in one message")
    out, total = [], 0
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in
                                                 ("name", "media_type", "data", "sha256")):
            raise Refused("an attachment came in the wrong shape")
        name = clean(item["name"])[:255]
        label = f'"{name}"' if name else "photo"
        if len(item["data"]) > MAX_FILE * 4 // 3 + 4:
            raise Refused(f"{label}: over {MAX_FILE // 2**20} MB")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            raise Refused(f"{label}: not base64") from None
        if hashlib.sha256(data).hexdigest() != item["sha256"].lower():
            raise Refused(f"{label}: arrived incomplete — sha256 does not match")
        if not data:
            raise Refused(f"{label}: empty file")
        total += len(data)
        if total > MAX_TOTAL:
            raise Refused(f"attachments together over {MAX_TOTAL // 2**20} MB")
        out.append(Given(name, item["media_type"][:100], data))
    return out


def kind_of(item: Given) -> str:
    """image | pdf | file — by the name, then by the media type."""
    suffix = PurePosixPath(item.name.lower()).suffix
    media = item.media_type.split(";")[0].strip().lower()
    if suffix in attachments.PICTURES or (media in RASTER and not suffix):
        return "image"
    if media == "application/pdf" or suffix == ".pdf":
        return "pdf"
    return "file"


def safe_name(name: str, media_type: str, n: int) -> str:
    """A plain file name for the inbox: the last part of the given one, no control character, no slash, no leading
    dot, at most NAME_CHARS characters and `cliengine.INBOX_NAME_BYTES` bytes, its extension kept; `file-<n>` with
    the media type's extension when nothing is left."""
    base = re.split(r"[/\\]", name)[-1]
    base = UNSHOWN.sub("", base).strip().lstrip(".").strip()
    if not base:
        base = f"file-{n}{EXTENSIONS.get(media_type.split(';')[0].strip().lower(), '')}"
    stem, ext = (base[:-len(PurePosixPath(base).suffix)], PurePosixPath(base).suffix) if PurePosixPath(base).suffix \
        else (base, "")
    ext = ext[:16]
    while stem and (len(stem) + len(ext) > NAME_CHARS or len((stem + ext).encode()) > cliengine.INBOX_NAME_BYTES):
        stem = stem[:-1]
    out = (stem.rstrip() or f"file-{n}") + ext
    return out if cliengine.INBOX_NAME.fullmatch(out) else f"file-{n}"


async def prepare(text: str, items: list[Given]) -> Ready:
    """The message as the session gets it. A picture that cannot be read is said in `refused` and left out; the rest
    goes in. The pictures and PDFs are read by the worker, READERS at a time across every message, all of one
    message's within PARSE_BUDGET_S."""
    text = text.strip()
    if not items:
        return Ready(text)
    deadline = asyncio.get_running_loop().time() + PARSE_BUDGET_S
    done = await asyncio.gather(*(_one(item, n, deadline) for n, item in enumerate(items, 1)))
    blocks, named, ready = [], [], Ready(text)
    for item, (kind, what, data) in zip(items, done):
        if kind == "refused":
            ready.refused.append(what)
            continue
        name = safe_name(item.name, item.media_type, len(ready.meta) + 1)
        if kind == "image":
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.b64encode(data).decode()}})
        elif kind == "pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode(data).decode()},
                           "title": name})
        else:
            ready.files.append({"name": name, "data": base64.b64encode(item.data).decode(),
                                "sha256": hashlib.sha256(item.data).hexdigest()})
            what = f'file "{name}"'
        named.append(f"{what} (in the inbox/ folder, path below)" if kind == "file" else what)
        thumb = await asyncio.to_thread(thumbnail, data) if kind == "image" else None
        ready.meta.append({"kind": kind, "name": item.name or name, "size": len(item.data), "what": what,
                           "thumb": thumb is not None})
        ready.thumbs.append(thumb)
    if not ready.meta:
        return ready
    head = "\n\n".join(filter(None, [text, f"Attached: {'; '.join(named)}."]))
    ready.prompt = [{"type": "text", "text": head}, *blocks]
    return ready


async def _one(item: Given, n: int, deadline: float) -> tuple[str, str, bytes]:
    """(image | pdf | file, how it is named, the bytes the block carries) — or (refused, why, b'')."""
    kind = kind_of(item)
    if kind == "file":
        return "file", "", b""
    upload = Upload("photo" if kind == "image" and not item.name else "document", item.data,
                    item.name or ("" if kind == "image" else f"file-{n}.pdf"),
                    "image/jpeg" if kind == "image" and not item.media_type else item.media_type)
    try:
        async with _reading():
            left = deadline - asyncio.get_running_loop().time()
            if left < LAST_READ_S:
                return "refused", (f"{attachments._parsing(upload)}: its turn did not come — the files of one message get "
                                   f"{PARSE_BUDGET_S} s to parse; send it separately"), b""
            read = await attachments.isolated(upload, READER_MEMORY, left)
    except Unreadable as exc:
        if kind == "pdf":  # a locked or broken PDF is still the owner's file: the session may say what it sees
            return "file", "", b""
        return "refused", str(exc), b""
    if kind == "pdf" and not read.files:  # longer than a block takes: the session reads its pages itself
        return "file", "", b""
    media, data = read.files[0]
    expected = "image/jpeg" if kind == "image" else "application/pdf"
    if media != expected:
        return "refused", f"{upload.label()}: read as {media}", b""
    return kind, read.what, data


def thumbnail(jpeg: bytes) -> bytes | None:
    """A small JPEG of a picture this platform has just re-encoded itself (never of the owner's raw bytes); None if
    even that fails — the face then shows the picture's name alone."""
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            img.thumbnail((THUMB_SIDE, THUMB_SIDE))
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=THUMB_QUALITY)
            return out.getvalue()
    except Exception:
        return None
