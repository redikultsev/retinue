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


LANE_CLI_VERSION = "2.1.304"  # the lanes' own CLI, separate from the SDK's: bump it with the probes run against it


def test_the_lane_cli_is_pinned_in_the_engine_the_lock_and_the_lane_image_alike():
    from retinue import cliengine
    assert cliengine.CLI_VERSION == LANE_CLI_VERSION
    lock = [line.split() for line in (ROOT / "deploy" / "lanes" / "cli.lock").read_text().splitlines()]
    assert sorted(arch for arch, *_ in lock) == ["linux-arm64", "linux-x64"]
    for arch, version, url, integrity in lock:
        assert version == LANE_CLI_VERSION
        assert url == (f"https://registry.npmjs.org/@anthropic-ai/claude-code-{arch}/-/"
                       f"claude-code-{arch}-{LANE_CLI_VERSION}.tgz")
        assert re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", integrity), arch
    dockerfile = (ROOT / "deploy" / "lanes" / "Dockerfile").read_text()
    assert "deploy/lanes/cli.lock" in dockerfile and f"fetch-cli.sh {Path(cliengine.CLI_PATH).parent}" in dockerfile, \
        "the image installs the locked CLI where the lane host runs it"


def test_the_lanes_run_in_auto_and_never_in_bypass():
    """Server sessions approve as the owner's Mac does, by auto's classifier."""
    from retinue import cliengine
    prefix = list(cliengine.PREFIX)
    assert prefix[prefix.index("--permission-mode") + 1] == "auto"
    # Asks reach the lane host, which denies them (`cliengine.deny_control`); without a host AskUserQuestion is off.
    assert prefix[prefix.index("--permission-prompt-tool") + 1] == "stdio" and "--permission-prompts" not in prefix
    assert "auto" not in cliengine.FORBIDDEN and "bypassPermissions" in cliengine.FORBIDDEN
    assert "--dangerously-skip-permissions" in cliengine.FORBIDDEN
