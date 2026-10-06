"""Versions: the SDK is pinned exactly, the CLI is the one inside the SDK wheel, the image installs from the lock."""

import re
import tomllib
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk._cli_version import __cli_version__

ROOT = Path(__file__).resolve().parents[1]
CLI_VERSION = "2.1.286"  # change together with the SDK pin, then repeat the subscription trial run


def test_sdk_is_pinned_exactly():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    sdk = next(d for d in deps if d.startswith("claude-agent-sdk"))
    assert re.fullmatch(r"claude-agent-sdk==\d+\.\d+\.\d+", sdk), f"not an exact pin: {sdk}"
    version = sdk.split("==")[1]
    assert claude_agent_sdk.__version__ == version, "the environment runs the pinned SDK"
    assert f'name = "claude-agent-sdk"\nversion = "{version}"' in (ROOT / "uv.lock").read_text()
    assert f'{{ name = "claude-agent-sdk", specifier = "=={version}" }}' in (ROOT / "uv.lock").read_text()


def test_cli_comes_with_the_sdk():
    assert __cli_version__ == CLI_VERSION
    assert any(d.startswith("aiohttp") for d in
               tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]), "bus.py imports it"


def test_image_installs_from_the_lock_file():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "uv sync --frozen" in dockerfile and "COPY pyproject.toml uv.lock" in dockerfile
    assert "pip install" not in dockerfile, "pip would resolve versions again and ignore the lock"
    assert re.search(r"astral-sh/uv:\d+\.\d+\.\d+ ", dockerfile), "uv itself is pinned too"
