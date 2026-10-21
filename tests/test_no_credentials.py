"""Retinue never reads the CLI's stored login or calls the subscription's own endpoints: the token reaches the CLI
through its environment, and that is all. The backup excludes `.credentials.json` from outside `retinue/`."""

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "retinue"
FORBIDDEN = (".credentials.json", "api/oauth/usage", "/api/oauth/")


def test_no_module_mentions_the_clis_credentials_or_its_oauth_endpoints():
    files = [p for p in sorted(PACKAGE.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    found = [(path.relative_to(PACKAGE).as_posix(), word) for path in files
             for word in FORBIDDEN if word.encode() in path.read_bytes()]
    assert not found, found
