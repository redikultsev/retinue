"""The owner's attachments to a session: what the platform takes (`inbox.given`), what it makes of each file
(`inbox.prepare`) and what the lane will write to the CLI (`cliengine.check_content`, `check_files`)."""

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from retinue import attachments, cliengine, inbox
from retinue.cliengine import Refused


def png(width=800, height=400, color=(255, 200, 0)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def pdf(pages=2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def wire(name: str, data: bytes, media_type: str = "", sha: str | None = None) -> dict:
    return {"name": name, "media_type": media_type, "data": base64.b64encode(data).decode(),
            "sha256": sha or hashlib.sha256(data).hexdigest()}


def refusal(raw) -> str:
    with pytest.raises(inbox.Refused) as caught:
        inbox.given(raw)
    return str(caught.value)


# --- what the platform takes ---------------------------------------------------------------------------------------

def test_attachments_are_taken_whole_and_within_the_limits_or_not_at_all():
    assert inbox.given(None) == []
    assert inbox.given([wire("a.txt", b"hi", "text/plain")]) == [inbox.Given("a.txt", "text/plain", b"hi")]
    assert refusal([wire("a.txt", b"hi", sha="0" * 64)]) == "«a.txt»: пришло не целым — sha256 не сходится"
    assert refusal([{**wire("a.txt", b"hi"), "data": "не base64"}]) == "«a.txt»: не base64"
    assert refusal([wire("", b"")]) == "фото: пустой файл"
    assert refusal([wire(f"{n}.txt", b"x") for n in range(11)]) == "вложений больше 10 в одном сообщении"
    assert refusal([{"name": "a"}]) == "вложение пришло не того вида"
    assert refusal("a.txt") == "вложений больше 10 в одном сообщении"


def test_one_too_big_or_all_together_too_big_is_refused(monkeypatch):
    monkeypatch.setattr(inbox, "MAX_FILE", 10)
    monkeypatch.setattr(inbox, "MAX_TOTAL", 15)
    assert refusal([wire("a.bin", b"x" * 40)]) == "«a.bin»: больше 0 МБ"
    assert refusal([wire("a.bin", b"x" * 9), wire("b.bin", b"y" * 9)]) == "вложения вместе больше 0 МБ"


@pytest.mark.parametrize("name, media_type, n, safe", [
    ("отчёт за май.xlsx", "", 1, "отчёт за май.xlsx"),
    ("../../etc/passwd", "", 1, "passwd"),
    ("C:\\Users\\me\\a.txt", "", 1, "a.txt"),
    (".env", "", 2, "env"),
    ("...", "image/png", 3, "file-3.png"),
    ("", "image/jpeg", 1, "file-1.jpg"),
    ("a\x00b\nc.md", "", 1, "abc.md"),
    ("я" * 200 + ".pdf", "", 1, "я" * 76 + ".pdf"),
])
def test_an_inbox_name_is_a_plain_file_name_and_keeps_its_extension(name, media_type, n, safe):
    out = inbox.safe_name(name, media_type, n)
    assert out == safe and cliengine.INBOX_NAME.fullmatch(out)
    assert len(out.encode()) <= cliengine.INBOX_NAME_BYTES


# --- what each file becomes ----------------------------------------------------------------------------------------

def test_a_picture_becomes_a_jpeg_block_a_short_pdf_a_document_and_anything_else_a_file():
    items = [inbox.Given("", "image/jpeg", png()), inbox.Given("счёт.pdf", "application/pdf", pdf()),
             inbox.Given("data.csv", "text/csv", b"a,b\n1,2\n"), inbox.Given("scan.heic", "", png(100, 50))]
    ready = asyncio.run(inbox.prepare("  посмотри  ", items))
    head, photo, document, scan = ready.prompt
    assert head == {"type": "text", "text": "посмотри\n\nПриложено: фото 800×400; PDF «счёт.pdf», 2 стр.; файл "
                                            "«data.csv» (в папке inbox/, путь ниже); картинка «scan.heic», 100×50."}
    with Image.open(io.BytesIO(base64.b64decode(photo["source"]["data"]))) as img:
        assert (photo["type"], img.format, img.size) == ("image", "JPEG", (800, 400))
    assert document["type"] == "document" and document["title"] == "счёт.pdf"
    assert base64.b64decode(document["source"]["data"]) == items[1].data
    assert scan["source"]["media_type"] == "image/jpeg", "every picture is the platform's own JPEG"
    assert ready.files == [{"name": "data.csv", "data": base64.b64encode(b"a,b\n1,2\n").decode(),
                            "sha256": hashlib.sha256(b"a,b\n1,2\n").hexdigest()}]
    assert [m["kind"] for m in ready.meta] == ["image", "pdf", "file", "image"]
    assert ready.meta[2] == {"kind": "file", "name": "data.csv", "size": 8, "what": "файл «data.csv»", "thumb": False}
    with Image.open(io.BytesIO(ready.thumbs[0])) as thumb:
        assert thumb.size == (320, 160)
    assert ready.thumbs[1:3] == [None, None] and ready.refused == []
    cliengine.check_content(ready.prompt)
    cliengine.check_files(ready.files, "2026-10-27")
    assert ready.lane("2026-10-27") == {"prompt": ready.prompt, "files": ready.files, "date": "2026-10-27"}


def test_a_picture_that_does_not_decode_is_refused_and_the_rest_goes_in():
    ready = asyncio.run(inbox.prepare("", [inbox.Given("broken.png", "image/png", b"\x89PNG not really"),
                                           inbox.Given("notes.md", "", b"# hi")]))
    assert ready.refused == ["картинка «broken.png»: картинку не удалось открыть"]
    assert [m["name"] for m in ready.meta] == ["notes.md"] and len(ready.prompt) == 1
    nothing = asyncio.run(inbox.prepare("", [inbox.Given("broken.png", "image/png", b"nope")]))
    assert nothing.empty and nothing.refused


def test_a_long_or_locked_pdf_goes_in_as_a_file_for_the_session_to_read_itself(monkeypatch):
    long = asyncio.run(inbox.prepare("", [inbox.Given("book.pdf", "application/pdf", pdf(21))]))
    assert [b["type"] for b in long.prompt] == ["text"] and long.files[0]["name"] == "book.pdf"
    broken = asyncio.run(inbox.prepare("", [inbox.Given("x.pdf", "application/pdf", b"%PDF-1.4 broken")]))
    assert broken.files[0]["name"] == "x.pdf" and broken.refused == []


@pytest.mark.parametrize("name, media_type, kind", [
    ("", "image/jpeg", "image"), ("IMG_1.HEIC", "", "image"), ("a.png", "application/octet-stream", "image"),
    ("logo.svg", "image/svg+xml", "file"), ("a.psd", "image/vnd.adobe.photoshop", "file"),
    ("a.PDF", "", "pdf"), ("", "application/pdf", "pdf"), ("a.txt", "image/png", "file"), ("a.zip", "", "file"),
])
def test_what_is_a_picture_a_pdf_or_a_file(name, media_type, kind):
    assert inbox.kind_of(inbox.Given(name, media_type, b"x")) == kind


def test_text_alone_stays_a_plain_prompt():
    assert asyncio.run(inbox.prepare(" привет ", [])).prompt == "привет"


# --- what the lane writes to the CLI -------------------------------------------------------------------------------

JPEG = base64.b64encode(b"\xff\xd8\xff").decode()


def test_content_blocks_of_three_kinds_pass_and_go_into_one_stream_json_line():
    blocks = [{"type": "text", "text": "смотри"},
              {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG}},
              {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": JPEG},
               "title": "a.pdf"}]
    assert cliengine.check_content(blocks) is blocks and cliengine.check_content("текст") == "текст"
    line = cliengine.user_line(blocks)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert b'"content":[{"type":"text","text":"\xd1\x81\xd0\xbc' in line


@pytest.mark.parametrize("blocks", [
    [], None, 5, [{"type": "text", "text": ""}], [{"type": "text", "text": "x", "cache_control": {}}],
    [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": JPEG}}],
    [{"type": "image", "source": {"type": "url", "url": "https://example.com/x.jpg"}}],
    [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "a b"}}],
    [{"type": "document", "source": {"type": "base64", "media_type": "text/plain", "data": JPEG}}],
    [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": JPEG},
      "title": "a\nb"}],
    [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
    [{"type": "text", "text": "x"}] * 25,
])
def test_any_other_block_is_refused(blocks):
    with pytest.raises(Refused):
        cliengine.check_content(blocks)


def test_blocks_over_their_size_are_refused(monkeypatch):
    monkeypatch.setattr(cliengine, "CONTENT_B64", 4)
    with pytest.raises(Refused, match="больше"):
        cliengine.check_content([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                              "data": JPEG + JPEG}}])


@pytest.mark.parametrize("files, date", [
    ([{"name": "../x", "data": JPEG, "sha256": "0" * 64}], "2026-10-27"),
    ([{"name": ".bashrc", "data": JPEG, "sha256": "0" * 64}], "2026-10-27"),
    ([{"name": "a/b", "data": JPEG, "sha256": "0" * 64}], "2026-10-27"),
    ([{"name": "a", "data": JPEG, "sha256": "0" * 64}], "27.10.2026"),
    ([{"name": "a", "data": JPEG, "sha256": "zz"}], "2026-10-27"),
    ([{"name": "a", "data": JPEG, "sha256": "0" * 64, "path": "/etc"}], "2026-10-27"),
    ([], "2026-10-27"),
])
def test_inbox_files_off_their_shape_are_refused(files, date):
    with pytest.raises(Refused):
        cliengine.check_files(files, date)


# --- the reader: its environment, its time, its process ----------------------------------------------------------

def test_the_parse_worker_gets_no_secret_of_its_parent(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    env = attachments.worker_env(inbox.READER_MEMORY)
    assert set(env) <= {"PATH", "LANG", "MALLOC_ARENA_MAX", "RETINUE_PARSE_MEMORY"}
    assert env["RETINUE_PARSE_MEMORY"] == str(inbox.READER_MEMORY) and "secret" not in str(env)
    # and the real worker still reads a picture with it
    ready = asyncio.run(inbox.prepare("", [inbox.Given("", "image/jpeg", png())]))
    assert ready.prompt[1]["type"] == "image"


def test_the_platform_and_the_router_are_not_dumpable_where_prctl_exists():
    code = ("import ctypes, sys\nfrom retinue.attachments import undumpable\nok = undumpable()\n"
            "print(ok, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) if ok else -1)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == (["True", "0"] if sys.platform == "linux" else ["False", "-1"])
    for module in ("platformd", "router"):
        source = (Path(__file__).parent.parent / "retinue" / f"{module}.py").read_text()
        main = source.split("\ndef main() -> None:\n", 1)[1]
        assert "attachments.undumpable()" in main.split("Config.load(")[0], f"{module} at the start of main"


@pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is Linux's")
def test_the_worker_offers_itself_to_the_oom_killer_first():
    request = json.dumps({"kind": "document", "name": "a.txt", "media_type": "", "data": ""})
    code = "from retinue import attachments\nattachments.worker()\nprint(open('/proc/self/oom_score_adj').read())"
    out = subprocess.run([sys.executable, "-c", code], input=request, capture_output=True, text=True).stdout
    assert out.strip().endswith("1000")


def test_a_message_s_files_are_read_within_one_budget_and_the_rest_said_by_name(monkeypatch):
    monkeypatch.setattr(inbox, "PARSE_BUDGET_S", 0)
    ready = asyncio.run(inbox.prepare("смотри", [inbox.Given("a.png", "image/png", png()),
                                                 inbox.Given("b.txt", "", b"b")]))
    assert ready.refused == ["картинка «a.png»: не дошла очередь — на разбор файлов одного сообщения 0 с; "
                             "пришли его отдельно"]
    assert [m["name"] for m in ready.meta] == ["b.txt"], "a file needs no reading"


def test_names_lose_control_characters_and_bidi_overrides_everywhere():
    raw = "счёт‮exe.pdf\n\x07"
    [item] = inbox.given([wire(raw, pdf())])
    assert item.name == "счётexe.pdf"
    ready = asyncio.run(inbox.prepare("", [item]))
    assert ready.meta[0]["name"] == "счётexe.pdf" and "‮" not in ready.meta[0]["what"]
    assert inbox.safe_name("a⁦b⁩.txt", "", 1) == "ab.txt"
    for name in ("a‮b", "a⁧b", "a\nb"):
        assert not cliengine.INBOX_NAME.fullmatch(name) and not cliengine.TITLE.fullmatch(name)
    assert inbox.kind_of(inbox.Given("IMG_2.HEIF", "", b"x")) == "image"
