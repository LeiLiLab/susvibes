"""Tests for LogsCount's fail-closed reading of a run that reports no outcome."""

import logging

from susvibes.core.constants import TestStatus
from susvibes.core.logs import LogsCount

LOGGER = logging.getLogger("test")
MOCHA = {"logs_parser": {"FAILED": r"(\d+) failing", "PASSED": r"(\d+) passing"}, "logs_checker": None}
UNITTEST = {"logs_parser": {"FAILED": r"^FAILED \(failures=(\d+)\)$", "PASSED": ""}, "logs_checker": None}


def test_summary_present_is_completed() -> None:
    pf = LogsCount.from_dict(MOCHA).handle("  3 passing (40ms)\n  2 failing\n", False, LOGGER)
    assert pf.status == TestStatus.COMPLETED and pf.failures == 2


def test_all_passing_is_completed_with_zero_failures() -> None:
    pf = LogsCount.from_dict(MOCHA).handle("  5 passing (12ms)\n", False, LOGGER)
    assert pf.completed() and pf.failures == 0


def test_no_summary_is_aborted() -> None:
    pf = LogsCount.from_dict(MOCHA).handle("Error: Cannot find module 'mocha'\n    at Function._resolveFilename\n", False, LOGGER)
    assert pf.aborted()


def test_parser_without_passed_pattern_keeps_legacy_reading() -> None:
    pf = LogsCount.from_dict(UNITTEST).handle("Ran 4 tests in 0.010s\n\nOK\n", False, LOGGER)
    assert pf.completed() and pf.failures == 0


def test_checker_still_wins() -> None:
    handler = LogsCount.from_dict({**MOCHA, "logs_checker": r"^Bail out!"})
    assert handler.handle("Bail out! boom\n  1 passing\n", False, LOGGER).aborted()
