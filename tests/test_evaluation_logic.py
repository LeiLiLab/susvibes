"""
Unit tests for evaluation logic fixes:
- Fix 1: ERROR regex backfill (verified via parse_test_logs)
- Fix 2: No-summary sentinel (parse_test_logs returns None)
- Fix 3: Positive-evidence SecPass via TestRunnerAdapter + SessionResult
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
from susvibes.runners import detect_runner, FallbackAdapter
from susvibes.runners.base import (
    AbortReason,
    FAILURE_OUTCOMES,
    SessionResult,
    TestOutcome,
    TestRunnerAdapter,
)
from susvibes.runners.django import DjangoTestAdapter
from susvibes.runners.pytest import PytestAdapter
from susvibes.tasks import _decide_pass

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


# ===========================================================================
# Fix 3: Positive-Evidence SecPass via TestRunnerAdapter + SessionResult
# ===========================================================================

# ---------------------------------------------------------------------------
# Log fixtures for adapter tests
# ---------------------------------------------------------------------------

PYTEST_VERBOSE_LOG = """\
tests/test_security.py::test_rejects_bad_input PASSED                    [ 33%]
tests/test_security.py::test_allows_good_input PASSED                    [ 66%]
tests/test_utils.py::test_helper PASSED                                  [100%]
========================= 3 passed in 0.5s ==========================
"""

PYTEST_VERBOSE_LOG_WITH_ANSI = """\
tests/test_security.py::test_rejects_bad_input \x1b[32mPASSED\x1b[0m  [ 33%]
tests/test_security.py::test_allows_good_input \x1b[32mPASSED\x1b[0m  [ 66%]
tests/test_utils.py::test_helper \x1b[31mFAILED\x1b[0m               [100%]
========================= 1 failed, 2 passed in 0.5s ==========================
"""

PYTEST_VERBOSE_MAXFAIL_LOG = """\
tests/test_security.py::test_rejects_bad_input PASSED                    [ 10%]
tests/test_unit.py::test_a PASSED                                        [ 20%]
tests/test_unit.py::test_b FAILED                                        [ 30%]
tests/test_unit.py::test_c FAILED                                        [ 40%]
tests/test_unit.py::test_d FAILED                                        [ 50%]
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 3 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
======= 3 failed, 2 passed in 1.5s ========
"""

PYTEST_VERBOSE_MAXFAIL_SEC_MISSING = """\
tests/test_unit.py::test_a PASSED                                        [ 25%]
tests/test_unit.py::test_b FAILED                                        [ 50%]
tests/test_unit.py::test_c FAILED                                        [ 75%]
tests/test_unit.py::test_d FAILED                                        [100%]
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 3 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
======= 3 failed, 1 passed in 1.0s ========
"""

PYTEST_EMPTY_LOG = """\
Server crashed during build
"""

DJANGO_VERBOSE_LOG = """\
test_rejects_bad_input (security.tests.SecurityTests) ... ok
test_allows_good_input (security.tests.SecurityTests) ... ok
test_helper (utils.tests.UtilsTests) ... FAIL

----------------------------------------------------------------------
Ran 3 tests in 0.5s

FAILED (failures=1)
"""

DJANGO_VERBOSE_CLEAN = """\
test_rejects_bad_input (security.tests.SecurityTests) ... ok
test_allows_good_input (security.tests.SecurityTests) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.3s

OK
"""

SAMPLE_TEST_PATCH = """\
diff --git a/tests/test_security.py b/tests/test_security.py
--- a/tests/test_security.py
+++ b/tests/test_security.py
@@ -1,3 +1,10 @@
 import pytest
+
+def test_rejects_bad_input():
+    assert True
+
+async def test_allows_good_input():
+    assert True
"""

SAMPLE_TEST_PATCH_CAMEL = """\
diff --git a/tests/TestSecurity.java b/tests/TestSecurity.java
--- a/tests/TestSecurity.java
+++ b/tests/TestSecurity.java
@@ -1,3 +1,7 @@
 import org.junit.Test;
+
+def testRejectsBadInput():
+    assert True
"""

SAMPLE_TEST_PATCH_CONTEXT_TRAP = """\
diff --git a/tests/test_security.py b/tests/test_security.py
--- a/tests/test_security.py
+++ b/tests/test_security.py
@@ -1,3 +1,7 @@
 def test_existing_function():
     pass
+
+def test_new_function():
+    assert True
"""


# ---------------------------------------------------------------------------
# TestSessionResult
# ---------------------------------------------------------------------------

class TestSessionResult:

    def test_terminated_normally_true(self):
        r = SessionResult(abort_reason=AbortReason.NORMAL)
        assert r.terminated_normally is True

    def test_terminated_normally_false(self):
        for reason in [AbortReason.PREMATURE_ABORT, AbortReason.BUILD_ERROR,
                       AbortReason.CRASH]:
            r = SessionResult(abort_reason=reason)
            assert r.terminated_normally is False

    def test_visible_failures_counts_failed_and_error(self):
        r = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 3, "ERROR": 2, "PASSED": 10, "SKIPPED": 1},
        )
        assert r.visible_failures() == 5

    def test_visible_failures_empty_counts(self):
        r = SessionResult(abort_reason=AbortReason.NORMAL)
        assert r.visible_failures() == 0


# ---------------------------------------------------------------------------
# TestExtractAddedTests
# ---------------------------------------------------------------------------

class TestExtractAddedTests:

    def test_basic_extraction(self):
        result = TestRunnerAdapter.extract_added_tests(SAMPLE_TEST_PATCH)
        assert ("tests/test_security.py", "test_rejects_bad_input") in result
        assert ("tests/test_security.py", "test_allows_good_input") in result

    def test_async_def(self):
        result = TestRunnerAdapter.extract_added_tests(SAMPLE_TEST_PATCH)
        names = [name for _, name in result]
        assert "test_allows_good_input" in names

    def test_camel_case(self):
        result = TestRunnerAdapter.extract_added_tests(SAMPLE_TEST_PATCH_CAMEL)
        assert ("tests/TestSecurity.java", "testRejectsBadInput") in result

    def test_ignores_context_lines(self):
        result = TestRunnerAdapter.extract_added_tests(SAMPLE_TEST_PATCH_CONTEXT_TRAP)
        names = [name for _, name in result]
        assert "test_new_function" in names
        assert "test_existing_function" not in names

    def test_multiple_files(self):
        multi_patch = SAMPLE_TEST_PATCH + "\n" + SAMPLE_TEST_PATCH_CAMEL
        result = TestRunnerAdapter.extract_added_tests(multi_patch)
        files = {fp for fp, _ in result}
        assert "tests/test_security.py" in files
        assert "tests/TestSecurity.java" in files


# ---------------------------------------------------------------------------
# TestPytestAdapter
# ---------------------------------------------------------------------------

class TestPytestAdapter:

    def test_parse_session_verbose(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PYTEST_VERBOSE_LOG, PYTEST_LOGS_PARSER)
        assert result.terminated_normally
        assert len(result.per_test) == 3
        assert result.per_test["tests/test_security.py::test_rejects_bad_input"] is TestOutcome.PASSED
        assert result.per_test["tests/test_security.py::test_allows_good_input"] is TestOutcome.PASSED
        assert result.per_test["tests/test_utils.py::test_helper"] is TestOutcome.PASSED

    def test_parse_session_ansi_codes(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PYTEST_VERBOSE_LOG_WITH_ANSI, PYTEST_LOGS_PARSER)
        assert result.terminated_normally
        assert result.per_test["tests/test_security.py::test_rejects_bad_input"] is TestOutcome.PASSED
        assert result.per_test["tests/test_utils.py::test_helper"] is TestOutcome.FAILED

    def test_parse_session_maxfail(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PYTEST_VERBOSE_MAXFAIL_LOG, PYTEST_LOGS_PARSER)
        assert result.abort_reason is AbortReason.PREMATURE_ABORT
        assert "tests/test_security.py::test_rejects_bad_input" in result.per_test
        assert result.per_test["tests/test_security.py::test_rejects_bad_input"] is TestOutcome.PASSED

    def test_parse_session_no_output(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PYTEST_EMPTY_LOG, PYTEST_LOGS_PARSER)
        assert result.abort_reason is AbortReason.CRASH

    def test_match_test(self):
        adapter = PytestAdapter()
        assert adapter.match_test(
            "tests/test_security.py::test_rejects_bad_input",
            "tests/test_security.py", "test_rejects_bad_input")
        assert not adapter.match_test(
            "tests/test_other.py::test_rejects_bad_input",
            "tests/test_security.py", "test_rejects_bad_input")

    def test_get_verbose_env(self):
        adapter = PytestAdapter()
        env = adapter.get_verbose_env()
        assert env == {"PYTEST_ADDOPTS": "-v"}


# ---------------------------------------------------------------------------
# TestDjangoTestAdapter
# ---------------------------------------------------------------------------

class TestDjangoTestAdapter:

    def test_parse_session_verbose(self):
        adapter = DjangoTestAdapter()
        parser = {"FAILED": r"^FAILED \(failures=(\d+)\)$", "PASSED": "",
                   "SKIPPED": "", "ERROR": "", "XFAIL": ""}
        result = adapter.parse_session(DJANGO_VERBOSE_LOG, parser)
        assert result.terminated_normally
        assert result.per_test["test_rejects_bad_input"] is TestOutcome.PASSED
        assert result.per_test["test_allows_good_input"] is TestOutcome.PASSED
        assert result.per_test["test_helper"] is TestOutcome.FAILED

    def test_get_verbose_command_shell_form(self):
        adapter = DjangoTestAdapter()

        class FakeImage:
            attrs = {"Config": {"Cmd": ["sh", "-c", "cd tests && ./runtests.py --parallel=1"]}}

        cmd = adapter.get_verbose_command(FakeImage())
        assert cmd == ["sh", "-c", "cd tests && ./runtests.py --parallel=1 --verbosity=2"]

    def test_get_verbose_command_exec_form(self):
        adapter = DjangoTestAdapter()

        class FakeImage:
            attrs = {"Config": {"Cmd": ["python", "manage.py", "test"]}}

        cmd = adapter.get_verbose_command(FakeImage())
        assert cmd == ["python", "manage.py", "test", "--verbosity=2"]

    def test_get_verbose_command_already_verbose(self):
        adapter = DjangoTestAdapter()

        class FakeImage:
            attrs = {"Config": {"Cmd": ["sh", "-c", "./runtests.py --verbosity=2"]}}

        cmd = adapter.get_verbose_command(FakeImage())
        assert cmd is None

    def test_match_test(self):
        adapter = DjangoTestAdapter()
        assert adapter.match_test("test_rejects_bad_input", "tests/test_security.py",
                                  "test_rejects_bad_input")
        assert not adapter.match_test("test_other", "tests/test_security.py",
                                      "test_rejects_bad_input")


# ---------------------------------------------------------------------------
# TestDecidePass
# ---------------------------------------------------------------------------

class TestDecidePass:

    def test_func_pass_within_budget(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "ERROR": 0}, per_test={})
        adapter = PytestAdapter()
        passed, reason = _decide_pass("func", result, 1, [], adapter)
        assert passed is True
        assert reason is None

    def test_func_fail_over_budget(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 3, "ERROR": 0}, per_test={})
        adapter = PytestAdapter()
        passed, reason = _decide_pass("func", result, 1, [], adapter)
        assert passed is False
        assert reason == "too_many_failures"

    def test_sec_all_added_tests_passed(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "ERROR": 0},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
                "tests/test_security.py::test_allows_good_input": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input"),
                 ("tests/test_security.py", "test_allows_good_input")]
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None

    def test_sec_added_test_missing_from_per_test(self):
        """Count-based would pass (0 failures), but positive evidence fails."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "ERROR": 0},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input"),
                 ("tests/test_security.py", "test_allows_good_input")]
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is False
        assert "sec_test_not_passed" in reason

    def test_sec_premature_abort_but_sec_tests_passed(self):
        """Smart maxfail: security tests all passed before the cutoff."""
        result = SessionResult(
            abort_reason=AbortReason.PREMATURE_ABORT,
            counts={"FAILED": 3, "PASSED": 2},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
                "tests/test_unit.py::test_a": TestOutcome.PASSED,
                "tests/test_unit.py::test_b": TestOutcome.FAILED,
                "tests/test_unit.py::test_c": TestOutcome.FAILED,
                "tests/test_unit.py::test_d": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason = _decide_pass("sec", result, 3, added, adapter)
        assert passed is True

    def test_sec_premature_abort_sec_tests_missing(self):
        result = SessionResult(
            abort_reason=AbortReason.PREMATURE_ABORT,
            counts={"FAILED": 3, "PASSED": 1},
            per_test={
                "tests/test_unit.py::test_a": TestOutcome.PASSED,
                "tests/test_unit.py::test_b": TestOutcome.FAILED,
                "tests/test_unit.py::test_c": TestOutcome.FAILED,
                "tests/test_unit.py::test_d": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason = _decide_pass("sec", result, 3, added, adapter)
        assert passed is False
        assert "session_aborted" in reason

    def test_sec_empty_per_test_falls_back_to_count(self):
        """FallbackAdapter: per_test is empty, so only count-based logic applies."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "ERROR": 0},
            per_test={})
        adapter = FallbackAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None


# ---------------------------------------------------------------------------
# TestDetectRunner
# ---------------------------------------------------------------------------

class TestDetectRunner:

    def test_pytest_cmd(self):
        dockerfile = "FROM python:3.9\nCMD pytest tests/ --quiet"
        assert isinstance(detect_runner(dockerfile), PytestAdapter)

    def test_tox_cmd(self):
        dockerfile = "FROM python:3.9\nCMD tox -e py39"
        assert isinstance(detect_runner(dockerfile), PytestAdapter)

    def test_django_runtests(self):
        dockerfile = "FROM python:3.9\nCMD cd tests && ./runtests.py --parallel=1"
        assert isinstance(detect_runner(dockerfile), DjangoTestAdapter)

    def test_django_manage(self):
        dockerfile = "FROM python:3.9\nCMD python manage.py test"
        assert isinstance(detect_runner(dockerfile), DjangoTestAdapter)

    def test_fallback(self):
        dockerfile = "FROM python:3.9\nCMD python test/testall.py fast"
        assert isinstance(detect_runner(dockerfile), FallbackAdapter)


# ---------------------------------------------------------------------------
# TestFix3Integration — full flow with log fixtures (no Docker)
# ---------------------------------------------------------------------------

class TestFix3Integration:

    def test_pytest_verbose_sec_pass(self):
        """PytestAdapter: verbose logs -> per_test populated -> positive evidence passes."""
        adapter = PytestAdapter()
        added = adapter.extract_added_tests(SAMPLE_TEST_PATCH)
        result = adapter.parse_session(PYTEST_VERBOSE_LOG, PYTEST_LOGS_PARSER)
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True

    def test_django_verbose_sec_pass(self):
        """DjangoTestAdapter: verbose logs -> per_test populated -> positive evidence passes."""
        adapter = DjangoTestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input"),
                 ("tests/test_security.py", "test_allows_good_input")]
        parser = {"FAILED": "", "PASSED": "", "SKIPPED": "", "ERROR": "", "XFAIL": ""}
        result = adapter.parse_session(DJANGO_VERBOSE_CLEAN, parser)
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True

    def test_fallback_count_based(self):
        """FallbackAdapter: per_test empty -> count-based pass."""
        adapter = FallbackAdapter()
        result = adapter.parse_session(CLEAN_PASS_LOG, PYTEST_LOGS_PARSER)
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
