"""Cold start probe: how long a short run takes, from starting the CLI to the first words and to the end.

No Retinue code is used, only the Claude Agent SDK, so the same file measures any image. Run it inside an
agent container, where the subscription token already is:

    docker compose exec -T <service> python - < tests/e2e/coldstart.py

Every run gets a fresh, empty CLAUDE_CONFIG_DIR and no `resume`: exactly what a short run looks like.
REPEATS=5 changes the number of runs per episode (default 3).
"""

import asyncio
import json
import os
import shutil
import statistics
import tempfile
import time

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, StreamEvent, SystemMessage, query

# Episodes 1, 2 and 4 from the design review: what the owner really wrote.
EPISODES = {
    "1": "в черногорию из белграда на 22 октября найди билеты",
    "2": "Что ты мне советовал по Еревану?",
    "4": "Напомни в пятницу позвонить Х",
}
SYSTEM_PROMPT = "Ты ассистентка Владельца. Отвечай по-русски, в два-три предложения."


async def measure(prompt: str, run=query) -> dict:
    """One cold run. Times are seconds from the moment the CLI was asked to start."""
    config_dir = tempfile.mkdtemp(prefix="coldstart-")
    options = ClaudeAgentOptions(system_prompt=SYSTEM_PROMPT, tools=[], setting_sources=[], permission_mode="dontAsk",
                                 max_turns=1, include_partial_messages=True, env={"CLAUDE_CONFIG_DIR": config_dir})
    out: dict = {}
    started = time.monotonic()
    try:
        async for message in run(prompt=prompt, options=options):
            elapsed = round(time.monotonic() - started, 2)
            if isinstance(message, SystemMessage) and message.subtype == "init":
                out.update(init_s=elapsed, api_key_source=message.data.get("apiKeySource"),
                           tools=message.data.get("tools"), model=message.data.get("model"))
            elif isinstance(message, StreamEvent):
                delta = message.event.get("delta") or {}
                if delta.get("type") == "text_delta" and "first_text_s" not in out:
                    out["first_text_s"] = elapsed
            elif isinstance(message, ResultMessage):
                out.update(total_s=elapsed, api_s=round(message.duration_api_ms / 1000, 2), is_error=message.is_error,
                           answer_chars=len(message.result or ""))
                if message.is_error:
                    out["error"] = (message.result or "; ".join(message.errors or []))[:300]
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
    return out


def summary(runs: list[dict]) -> dict:
    """Median and worst case over the runs that succeeded."""
    good = [r for r in runs if r.get("is_error") is False and "first_text_s" in r]
    if not good:
        return {"ok": 0, "failed": len(runs)}
    return {"ok": len(good), "failed": len(runs) - len(good),
            "first_text_median_s": statistics.median(r["first_text_s"] for r in good),
            "first_text_max_s": max(r["first_text_s"] for r in good),
            "total_median_s": statistics.median(r["total_s"] for r in good),
            "total_max_s": max(r["total_s"] for r in good)}


async def main() -> None:
    repeats = int(os.environ.get("REPEATS", "3"))
    print(json.dumps({"sdk": claude_agent_sdk.__version__, "proxy": os.environ.get("HTTPS_PROXY", ""),
                      "api_key_in_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
                      "oauth_token_in_env": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}))
    for number, prompt in EPISODES.items():
        runs = []
        for _ in range(repeats):
            runs.append(await measure(prompt))
            print(json.dumps({"episode": number, **runs[-1]}, ensure_ascii=False))
        print(json.dumps({"episode": number, "summary": summary(runs)}))


if __name__ == "__main__":
    asyncio.run(main())
