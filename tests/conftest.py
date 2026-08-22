from __future__ import annotations

import os

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail guarded test runs when pytest reports skipped tests."""
    if os.getenv("ELOWYN_FAIL_ON_SKIP") != "1":
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    non_executed = ("skipped", "xfailed", "xpassed")
    if reporter is not None and any(reporter.stats.get(key) for key in non_executed):
        session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)
