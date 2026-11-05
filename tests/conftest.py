"""Where a probe may not be skipped. With `RETINUE_PROBES_REQUIRED` set (CI's `probes` job) a probe that did not run is
a failure, whatever the reason, so a missing or wrong CLI turns the job red instead of leaving it green and empty
(docs/platform.md, «Probes»). Elsewhere a skip stays a skip."""

from __future__ import annotations

import os

import pytest

REQUIRED = "RETINUE_PROBES_REQUIRED"
# It needs the owner's login, which a public repository's CI does not hold; it runs on the server before a CLI bump.
# (So does tests/test_lane_container.py, which the job does not collect.)
MAY_SKIP = {"test_the_lane_cli_is_logged_in_with_the_owners_token"}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.skipped and os.environ.get(REQUIRED) and item.get_closest_marker("probe") and item.name not in MAY_SKIP:
        reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else report.longrepr
        report.outcome, report.longrepr = "failed", f"a probe may not be skipped under {REQUIRED}: {reason}"
    return report
