"""The multimodal probe with a fake engine: what it sends at each step and what it counts as passed."""

import asyncio
import base64
import importlib.util
import io
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from retinue.engine import EngineResult

spec = importlib.util.spec_from_file_location("multimodal", Path(__file__).parent / "e2e" / "multimodal.py")
multimodal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multimodal)


class Engine:
    def __init__(self):
        self.runs = []

    async def run(self, prompt, session_id, on_text=None, turn_id=None):
        self.runs.append((prompt, session_id))
        answers = {"Какое число": "7314", "Какого цвета": "Жёлтый", "Какой номер": "4711", "Какое число было": "7314 и 4711"}
        last = prompt if isinstance(prompt, str) else prompt[-1]["text"]
        answer = next((a for q, a in sorted(answers.items(), key=lambda x: -len(x[0])) if last.startswith(q)), "не вижу")
        return EngineResult(text=answer, is_error=False, session_id=session_id or f"s{len(self.runs)}")

    async def compact(self, session_id):
        self.runs.append(("/compact", session_id))
        return EngineResult(text="Контекст сжат.", is_error=False, session_id=session_id, compacted=True)


def test_the_probe_sends_a_real_photo_and_pdf_and_resumes_one_session():
    engine = Engine()
    results = asyncio.run(multimodal.run(engine))
    assert [(r["step"], r["ok"]) for r in results] == [
        ("photo", True), ("resume", True), ("pdf", True), ("compact", True), ("after_compact", True),
        ("broken_photo", True)]
    assert [session for _, session in engine.runs] == [None, "s1", "s1", "s1", "s1", None]
    photo, pdf = engine.runs[0][0][1], engine.runs[2][0][1]
    with Image.open(io.BytesIO(base64.b64decode(photo["source"]["data"]))) as img:
        assert img.format == "JPEG" and img.size == (800, 400)
    assert "Invoice 4711" in PdfReader(io.BytesIO(base64.b64decode(pdf["source"]["data"]))).pages[0].extract_text()
