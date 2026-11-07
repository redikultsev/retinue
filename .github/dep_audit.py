"""Known vulnerabilities in the Python dependencies: red when one has a fix released at least two weeks ago.

The age rule is the face's (face/pnpm-workspace.yaml, minimumReleaseAge): nothing younger than two weeks goes in, so
a fix younger than that is a warning on the run, and turns red by itself once it is old enough. The age is read from
PyPI on every run, never kept in a list here.

Reads pip-audit's JSON (`pip-audit -f json`) on stdin.
"""
import datetime
import json
import sys
import urllib.request

AGE = datetime.timedelta(days=14)


def released(name, version):
    """When this version of the package reached PyPI: its first file's upload time."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as answer:
        files = json.load(answer)["urls"]
    return min(datetime.datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")) for f in files)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    red = 0
    for dep in json.load(sys.stdin)["dependencies"]:
        for vuln in dep.get("vulns", []):
            where = f"{dep['name']} {dep['version']}: {vuln['id']}"
            fixes = vuln.get("fix_versions") or []
            if not fixes:
                print(f"::warning::{where} has no fix yet")
                continue
            # The oldest fix is the one a lock could already take.
            first = min(released(dep["name"], v) for v in fixes)
            if now - first >= AGE:
                red += 1
                print(f"::error::{where}, fixed in {', '.join(fixes)} since {first:%Y-%m-%d}: upgrade it in uv.lock")
            else:
                print(f"::warning::{where}, fixed in {', '.join(fixes)} on {first:%Y-%m-%d}, younger than two weeks")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
