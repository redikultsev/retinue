"""The cold start probe with a fake CLI: what it measures and that every run starts from an empty config dir."""

import asyncio
import importlib.util
import os
from pathlib import Path

from claude_agent_sdk import ResultMessage, StreamEvent, SystemMessage

spec = importlib.util.spec_from_file_location("coldstart", Path(__file__).parent / "e2e" / "coldstart.py")
coldstart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coldstart)


def test_measure_one_cold_run():
    seen = {}

    async def fake_cli(prompt, options):
        seen.update(prompt=prompt, options=options, config_dir_exists=os.path.isdir(options.env["CLAUDE_CONFIG_DIR"]))
        yield SystemMessage(subtype="init", data={"apiKeySource": "none", "tools": [], "model": "claude-x"})
        yield StreamEvent(uuid="u1", session_id="s", event={"type": "message_start"})
        yield StreamEvent(uuid="u2", session_id="s",
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "При"}})
        yield ResultMessage(subtype="success", duration_ms=900, duration_api_ms=700, is_error=False, num_turns=1,
                            session_id="s", result="Привет")

    out = asyncio.run(coldstart.measure(coldstart.EPISODES["2"], run=fake_cli))
    options = seen["options"]
    assert seen["prompt"] == "Что ты мне советовал по Еревану?"
    assert options.resume is None and options.tools == [] and options.setting_sources == []
    assert seen["config_dir_exists"] and not os.path.exists(options.env["CLAUDE_CONFIG_DIR"]), "fresh dir, then removed"
    assert out["api_key_source"] == "none" and out["tools"] == [] and out["is_error"] is False
    assert 0 <= out["init_s"] <= out["first_text_s"] <= out["total_s"] and out["api_s"] == 0.7
    assert out["answer_chars"] == 6


def test_summary_counts_only_successful_runs():
    runs = [{"is_error": False, "first_text_s": 2.0, "total_s": 5.0}, {"is_error": False, "first_text_s": 4.0, "total_s": 9.0},
            {"is_error": True, "total_s": 1.0, "error": "limit"}]
    assert coldstart.summary(runs) == {"ok": 2, "failed": 1, "first_text_median_s": 3.0, "first_text_max_s": 4.0,
                                       "total_median_s": 7.0, "total_max_s": 9.0}
    assert coldstart.summary([{"is_error": True}]) == {"ok": 0, "failed": 1}
