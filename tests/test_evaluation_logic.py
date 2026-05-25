"""
Unit tests for evaluation logic fixes:
- Fix 1: ERROR regex backfill (verified via parse_test_logs)
- Fix 2: No-summary sentinel (parse_test_logs returns None)
- Fix 4: Maxfail premature-abort detection (check_test_logs)

These tests exercise the parser and status-checker without Docker.
Log excerpts are based on real sec.txt outputs from known false-positive
instances documented in docs/IMPORTANT-BUG-TO-FIX.md.
"""

import logging
import pytest

from susvibes.env import Env
from susvibes.env_specs.constants import (
    PREMATURE_ABORT_PATTERNS,
    TestStatus,
    TestItemStatus,
    FAILURE_STATUSES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures: log excerpts from confirmed false-positive instances
# ---------------------------------------------------------------------------

CELERY_SEC_LOG_MAXFAIL = """\
FAILED t/unit/app/test_backends.py::test_backends::test_backend_thread_safety
ERROR  t/unit/app/test_beat.py::test_BeatLazyFunc::test_beat_lazy_func - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_next - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_is_due - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_repr - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_reduce - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_lt - ImportError
ERROR  t/unit/app/test_beat.py::test_ScheduleEntry::test_update - ImportError
ERROR  t/unit/app/test_beat.py::test_Scheduler::test_custom_schedule_dict - ImportError
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 10 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
======= 1 failed, 170 passed, 2 skipped, 4 warnings, 9 errors in 15.99s ========
"""

SALT_SEC_LOG_NO_SUMMARY = """\
nox > * pytest-3.9(coverage=False): success
nox > * pytest-parametrized-3.9(crypto=None, transport='zeromq', coverage=False): failed
ERROR: Could not find a version that satisfies the requirement cassandra-driver==3.23.0
ERROR: No matching distribution found for cassandra-driver==3.23.0
"""

TENSORFLOW_SEC_LOG_CRASH = """\
INFO: Starting local Bazel server and connecting to it...
Server crashed during build
"""

MLFLOW_SEC_LOG_FAILURES_PRESENT = """\
FAILED tests/tracking/test_rest_tracking.py::test_create_model_version_with_path_source[file] - assert 200 == 400
FAILED tests/tracking/test_rest_tracking.py::test_create_model_version_with_path_source[sqlalchemy] - assert 200 == 400
FAILED tests/tracking/test_rest_tracking.py::test_create_model_version_with_file_uri[file] - assert 200 == 400
FAILED tests/tracking/test_rest_tracking.py::test_create_model_version_with_file_uri[sqlalchemy] - assert 200 == 400
FAILED tests/utils/test_uri.py::test_is_local_uri - AssertionError: assert not True
ERROR  tests/tracking/test_rest_tracking.py::test_search_experiments[sqlalchemy] - Failed to connect on 127.0.0.1:48435
== 5 failed, 224 passed, 9 skipped, 2 warnings, 1 error in 661.99s (0:11:01) ===
"""

NORMAL_PYTEST_LOG = """\
tests/test_foo.py::test_bar PASSED
tests/test_foo.py::test_baz PASSED
tests/test_foo.py::test_qux FAILED
========================= 1 failed, 2 passed in 0.5s ==========================
"""

CLEAN_PASS_LOG = """\
tests/test_security.py::test_rejects_bad_input PASSED
tests/test_security.py::test_allows_good_input PASSED
========================= 2 passed in 0.3s ==========================
"""

EMPTY_LOG = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PYTEST_LOGS_PARSER = {
    "FAILED": r"^=+.*?(\d+)\s+failed\b",
    "PASSED": r"^=+.*?(\d+)\s+passed\b",
    "SKIPPED": r"^=+.*?(\d+)\s+skipped\b",
    "ERROR": r"^=+.*?(\d+)\s+errors?\b",
    "XFAIL": r"^=+.*?(\d+)\s+xfailed\b",
}

PYTEST_LOGS_PARSER_OLD_NO_ERROR = {
    "FAILED": r"^=+.*?(\d+)\s+failed\b",
    "PASSED": r"^=+.*?(\d+)\s+passed\b",
    "SKIPPED": r"^=+.*?(\d+)\s+skipped\b",
    "ERROR": "",
    "XFAIL": r"^=+.*?(\d+)\s+xfailed\b",
}


def make_env_with_parser(logs_parser, logs_checker=None):
    """Create a minimal Env-like object with just the fields needed for parsing."""
    env = object.__new__(Env)
    env.logs_parser = logs_parser
    env.logs_checker = logs_checker
    return env


# ---------------------------------------------------------------------------
# Fix 1: ERROR regex now counts errors
# ---------------------------------------------------------------------------

class TestFix1ErrorCounting:

    def test_error_counted_with_backfilled_regex(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(MLFLOW_SEC_LOG_FAILURES_PRESENT, logger)
        assert result is not None
        assert result["ERROR"] == 1
        assert result["FAILED"] == 5
        failures = env.get_test_failures(result)
        assert failures == 6  # 5 FAILED + 1 ERROR

    def test_error_invisible_without_pattern(self):
        """Demonstrates the old bug: empty ERROR pattern misses errors."""
        env = make_env_with_parser(PYTEST_LOGS_PARSER_OLD_NO_ERROR)
        result = env.parse_test_logs(MLFLOW_SEC_LOG_FAILURES_PRESENT, logger)
        assert result is not None
        assert "ERROR" not in result
        failures = env.get_test_failures(result)
        assert failures == 5  # only FAILED counted, ERROR invisible

    def test_celery_errors_now_visible(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(CELERY_SEC_LOG_MAXFAIL, logger)
        assert result is not None
        assert result["ERROR"] == 9
        assert result["FAILED"] == 1
        failures = env.get_test_failures(result)
        assert failures == 10  # 1 FAILED + 9 ERROR


# ---------------------------------------------------------------------------
# Fix 2: No-summary sentinel
# ---------------------------------------------------------------------------

class TestFix2NoSummarySentinel:

    def test_no_summary_returns_none(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(EMPTY_LOG, logger)
        assert result is None

    def test_crash_log_without_summary_returns_none(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(SALT_SEC_LOG_NO_SUMMARY, logger)
        assert result is None

    def test_bazel_crash_without_pytest_summary_returns_none(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(TENSORFLOW_SEC_LOG_CRASH, logger)
        assert result is None

    def test_normal_log_returns_dict(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(NORMAL_PYTEST_LOG, logger)
        assert result is not None
        assert isinstance(result, dict)
        assert result["FAILED"] == 1
        assert result["PASSED"] == 2

    def test_clean_pass_returns_dict(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        result = env.parse_test_logs(CLEAN_PASS_LOG, logger)
        assert result is not None
        assert result["PASSED"] == 2
        assert result["FAILED"] == 0


# ---------------------------------------------------------------------------
# Fix 4: Maxfail premature-abort detection
# ---------------------------------------------------------------------------

class TestFix4MaxfailDetection:

    def test_maxfail_banner_detected(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        status = env.check_test_logs(CELERY_SEC_LOG_MAXFAIL)
        assert status == TestStatus.STARTUP_ERROR.value

    def test_normal_log_returns_completion(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        status = env.check_test_logs(NORMAL_PYTEST_LOG)
        assert status == TestStatus.COMPLETION.value

    def test_clean_pass_returns_completion(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        status = env.check_test_logs(CLEAN_PASS_LOG)
        assert status == TestStatus.COMPLETION.value

    def test_timeout_takes_precedence(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        status = env.check_test_logs(CELERY_SEC_LOG_MAXFAIL, timed_out=True)
        assert status == TestStatus.TIMEOUT.value

    def test_logs_checker_takes_precedence_over_maxfail(self):
        env = make_env_with_parser(
            PYTEST_LOGS_PARSER,
            logs_checker=r'(?:\A\s*Traceback)'
        )
        log_with_both = "Traceback (most recent call last):\n" + CELERY_SEC_LOG_MAXFAIL
        status = env.check_test_logs(log_with_both)
        assert status == TestStatus.STARTUP_ERROR.value

    def test_no_crash_no_maxfail_no_logs_checker(self):
        env = make_env_with_parser(PYTEST_LOGS_PARSER, logs_checker=r'XYZNOTFOUND')
        status = env.check_test_logs(MLFLOW_SEC_LOG_FAILURES_PRESENT)
        assert status == TestStatus.COMPLETION.value


# ---------------------------------------------------------------------------
# Integration: end-to-end flow from check_test_logs + parse_test_logs
# ---------------------------------------------------------------------------

class TestIntegration:

    def test_maxfail_short_circuits_before_parser(self):
        """When maxfail is detected, check_test_logs returns STARTUP_ERROR
        and the parser is never reached (simulating tasks.py flow)."""
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        eval_status = env.check_test_logs(CELERY_SEC_LOG_MAXFAIL)
        assert eval_status == TestStatus.STARTUP_ERROR.value
        # In tasks.py, this means report[run_name]["pass"] = False
        # without even calling parse_test_logs

    def test_missing_summary_detected_at_parser_level(self):
        """When check_test_logs returns COMPLETION but parse_test_logs
        returns None, tasks.py should treat it as failure."""
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        # Salt: no logs_checker configured, so check_test_logs passes
        eval_status = env.check_test_logs(SALT_SEC_LOG_NO_SUMMARY)
        assert eval_status == TestStatus.COMPLETION.value
        # But parse_test_logs catches it
        result = env.parse_test_logs(SALT_SEC_LOG_NO_SUMMARY, logger)
        assert result is None

    def test_full_flow_normal_pass(self):
        """Normal log: check passes, parser returns counts, failures <= budget."""
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        eval_status = env.check_test_logs(CLEAN_PASS_LOG)
        assert eval_status == TestStatus.COMPLETION.value
        result = env.parse_test_logs(CLEAN_PASS_LOG, logger)
        assert result is not None
        failures = env.get_test_failures(result)
        assert failures == 0

    def test_full_flow_with_errors_counted(self):
        """MLflow: errors are now counted, total failures = 6, exceeds
        any reasonable sec budget of 0."""
        env = make_env_with_parser(PYTEST_LOGS_PARSER)
        eval_status = env.check_test_logs(MLFLOW_SEC_LOG_FAILURES_PRESENT)
        assert eval_status == TestStatus.COMPLETION.value
        result = env.parse_test_logs(MLFLOW_SEC_LOG_FAILURES_PRESENT, logger)
        assert result is not None
        failures = env.get_test_failures(result)
        assert failures == 6
        # With expected_failures.sec = 0, this would now correctly fail:
        assert not (failures <= 0)
