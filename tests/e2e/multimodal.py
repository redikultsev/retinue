"""Multimodal probe: a photo and a PDF through Retinue's own engine, on the pinned CLI and the real subscription.

Run it inside the assistant's container before the owner sends files (research 54: «smoke test before enabling»):

    docker compose exec -T assistant python - < tests/e2e/multimodal.py

It makes its own picture (the number 7314 on yellow) and its own PDF (invoice 4711), so nothing personal is sent.
Steps: the photo in a new session; a question about it with `resume`; the PDF in the same session; `/compact`;
a question after compaction; a broken picture in a new session. One JSON line per step: `ok` is whether the
answer has what it must have. A fresh, empty CLAUDE_CONFIG_DIR; the assistant's own session is not touched.
"""

import asyncio
import base64
import io
import json
import os
import shutil
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

from retinue.config import EngineConfig
from retinue.engine import ClaudeEngine

SYSTEM_PROMPT = "Ты проверочный ассистент. Отвечай по-русски, коротко, без пояснений."


def photo() -> dict:
    img = Image.new("RGB", (800, 400), (250, 220, 40))
    ImageDraw.Draw(img).text((200, 120), "7314", fill=(0, 0, 0), font=ImageFont.load_default(size=160))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(out.getvalue()).decode()}}


def invoice() -> dict:
    stream = "BT /F1 28 Tf 72 700 Td (Invoice 4711, total 99 EUR) Tj ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>"
               " /Contents 5 0 R >>", f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    table = "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += (f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n{table}"
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{len(out)}\n%%EOF\n").encode()
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(out).decode()}}


def text(words: str) -> dict:
    return {"type": "text", "text": words}


# (step, prompt, whether it resumes the session, what the answer must contain — all of it, lower case, ё as е)
STEPS = [
    ("photo", [text("[вложение #1: фото]"), photo(), text("Какое число на картинке? Ответь только числом.")],
     False, ("7314",)),
    ("resume", "Какого цвета фон картинки? Одним словом.", True, ("желт",)),
    ("pdf", [text("[вложение #2: PDF]"), invoice(), text("Какой номер счёта в PDF? Только число.")], True, ("4711",)),
    ("compact", None, True, ()),
    ("after_compact", "Какое число было на картинке и какой номер у счёта?", True, ("7314", "4711")),
    ("broken_photo", [text("[вложение #3: фото]"),
                      {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "bm90IGEganBlZw=="}},
                      text("Что на картинке?")], False, ()),
]


def model() -> str | None:
    try:
        import yaml
        with open("/agent/agent.yaml") as f:
            return yaml.safe_load(f)["engine"].get("model")
    except OSError:
        return os.environ.get("MODEL")


async def run(engine) -> list[dict]:
    session, results = None, []
    for name, prompt, resume, expect in STEPS:
        started = time.monotonic()
        try:
            if prompt is None:
                result = await engine.compact(session)
            else:
                result = await engine.run(prompt, session if resume else None)
        except Exception as exc:  # a failed step is a result, not the end of the probe
            results.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        session = result.session_id or session
        answer = result.text.lower().replace("ё", "е")
        results.append({"step": name, "ok": not result.is_error and all(word in answer for word in expect),
                        "answer": result.text[:300], "is_error": result.is_error, "compacted": result.compacted,
                        "new_session": result.new_session, "seconds": round(time.monotonic() - started, 1)})
    return results


async def main() -> None:
    folder = tempfile.mkdtemp(prefix="multimodal-")
    try:
        instructions = os.path.join(folder, "CLAUDE.md")
        with open(instructions, "w") as f:
            f.write(SYSTEM_PROMPT)
        cfg = EngineConfig(instructions=instructions, tools=[], disallowed_tools=["Bash", "WebSearch", "WebFetch"],
                           bus_tools=[], config_dir=os.path.join(folder, "claude"), max_turns=2, model=model())
        for line in await run(ClaudeEngine(cfg, folder)):
            print(json.dumps(line, ensure_ascii=False))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
