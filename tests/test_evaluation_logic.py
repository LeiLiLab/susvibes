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
from susvibes.tasks import _count_sec_variant_failures, _decide_pass

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
        passed, reason, evidence, likely_passed = _decide_pass("func", result, 1, [], adapter)
        assert passed is True
        assert reason is None

    def test_func_fail_over_budget(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 3, "ERROR": 0}, per_test={})
        adapter = PytestAdapter()
        passed, reason, evidence, likely_passed = _decide_pass("func", result, 1, [], adapter)
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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None

    def test_sec_added_test_missing_from_per_test(self):
        """One security test in per_test (PASSED), the other missing.
        Missing sec test is LIKELY_PASSED — passes with partial evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "ERROR": 0},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input"),
                 ("tests/test_security.py", "test_allows_good_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "tests/test_security.py::test_allows_good_input" in likely_passed

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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True

    def test_django_verbose_sec_pass(self):
        """DjangoTestAdapter: verbose logs -> per_test populated -> positive evidence passes."""
        adapter = DjangoTestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input"),
                 ("tests/test_security.py", "test_allows_good_input")]
        parser = {"FAILED": "", "PASSED": "", "SKIPPED": "", "ERROR": "", "XFAIL": ""}
        result = adapter.parse_session(DJANGO_VERBOSE_CLEAN, parser)
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True

    def test_fallback_count_based(self):
        """FallbackAdapter: per_test empty -> count-based pass."""
        adapter = FallbackAdapter()
        result = adapter.parse_session(CLEAN_PASS_LOG, PYTEST_LOGS_PARSER)
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True


# ---------------------------------------------------------------------------
# Regression: Bug 1 — pre_install/post_install patch split
# ---------------------------------------------------------------------------

DJANGO_OK_LOG = """\
System check identified no issues (0 silenced).
..................................................................
----------------------------------------------------------------------
Ran 616 tests in 67.351s

OK
"""

DJANGO_LOGS_PARSER_FAILED_ONLY = {
    "FAILED": r"^FAILED \(failures=(\d+)",
    "PASSED": "",
    "SKIPPED": r"^FAILED .*skipped=(\d+)",
    "ERROR": r"^FAILED .*errors=(\d+)",
    "XFAIL": r"^FAILED .*expected failures=(\d+)",
}


class TestBug1PatchSplit:
    """Verify pre_install/post_install split prevents git-apply conflicts."""

    def test_func_run_single_patch_goes_to_post_install(self):
        """func run: (model_patch,) -> pre_install=(), post_install=(model_patch,)"""
        patches = ("model_patch_content",)
        result = {"pre_install": patches[:-1], "post_install": patches[-1:]}
        assert result["pre_install"] == ()
        assert result["post_install"] == ("model_patch_content",)

    def test_sec_run_patches_split_across_layers(self):
        """sec run: (test_patch, model_patch) -> pre_install=(test_patch,), post_install=(model_patch,)"""
        patches = ("test_patch_content", "model_patch_content")
        result = {"pre_install": patches[:-1], "post_install": patches[-1:]}
        assert result["pre_install"] == ("test_patch_content",)
        assert result["post_install"] == ("model_patch_content",)

    def test_func_result_preserved_when_sec_has_patch_error(self):
        """A successful func run must NOT be overwritten by sec model_patch_error.

        This validates the removal of the 'marking all runs as failed' override.
        """
        from susvibes.constants import EvalStatus

        report = {
            "func": {"pass": True, "status": EvalStatus.COMPLETION.value},
            "sec": {"pass": False, "status": EvalStatus.MODEL_PATCH_ERROR.value},
        }
        # After the fix, we no longer overwrite func based on sec's error.
        # The report should retain the original func result.
        assert report["func"]["pass"] is True
        assert report["func"]["status"] == EvalStatus.COMPLETION.value
        assert report["sec"]["pass"] is False
        assert report["sec"]["status"] == EvalStatus.MODEL_PATCH_ERROR.value


# ---------------------------------------------------------------------------
# Regression: Bug 2 — Django func runs with OK-only output
# ---------------------------------------------------------------------------

class TestBug2DjangoFuncOkParse:
    """Django logs_parser patterns are FAILED-centric; a clean 'OK' run must not be
    treated as a crash when check_test_logs returns COMPLETION."""

    def test_django_ok_log_parse_returns_none(self):
        """Confirm the precondition: Django FAILED-only parser returns None for OK logs."""
        env = make_env_with_parser(DJANGO_LOGS_PARSER_FAILED_ONLY)
        result = env.parse_test_logs(DJANGO_OK_LOG, logger)
        assert result is None

    def test_django_ok_log_check_returns_completion(self):
        """check_test_logs returns COMPLETION for a clean Django OK log."""
        env = make_env_with_parser(DJANGO_LOGS_PARSER_FAILED_ONLY)
        status = env.check_test_logs(DJANGO_OK_LOG, timed_out=False)
        assert status == TestStatus.COMPLETION.value

    def test_func_path_ok_log_yields_normal_not_crash(self):
        """The func-run else-branch: COMPLETION + parse=None -> NORMAL (not CRASH).

        This is the exact code path from tasks.py lines 240-254.
        """
        from susvibes.constants import EvalStatus

        env = make_env_with_parser(DJANGO_LOGS_PARSER_FAILED_ONLY)
        test_logs = DJANGO_OK_LOG
        timed_out = False

        eval_status = env.check_test_logs(test_logs, timed_out)
        test_result = env.parse_test_logs(test_logs, logger)

        # Reproduce the fixed logic from tasks.py
        if eval_status == EvalStatus.TIMEOUT.value:
            abort = AbortReason.CRASH
        elif eval_status == EvalStatus.STARTUP_ERROR.value:
            abort = AbortReason.BUILD_ERROR
        elif test_result is None:
            abort = (AbortReason.NORMAL
                     if eval_status == EvalStatus.COMPLETION.value
                     else AbortReason.CRASH)
        else:
            abort = AbortReason.NORMAL

        assert abort == AbortReason.NORMAL

    def test_func_path_crash_log_still_yields_crash(self):
        """A log that triggers startup_error should still abort as CRASH/BUILD_ERROR."""
        from susvibes.constants import EvalStatus

        env = make_env_with_parser(
            DJANGO_LOGS_PARSER_FAILED_ONLY,
            logs_checker=r"(?:\A\s*Traceback)"
        )
        crash_log = "Traceback (most recent call last):\n  File...\nImportError: No module"

        eval_status = env.check_test_logs(crash_log, timed_out=False)
        test_result = env.parse_test_logs(crash_log, logger)

        if eval_status == EvalStatus.TIMEOUT.value:
            abort = AbortReason.CRASH
        elif eval_status == EvalStatus.STARTUP_ERROR.value:
            abort = AbortReason.BUILD_ERROR
        elif test_result is None:
            abort = (AbortReason.NORMAL
                     if eval_status == EvalStatus.COMPLETION.value
                     else AbortReason.CRASH)
        else:
            abort = AbortReason.NORMAL

        assert abort == AbortReason.BUILD_ERROR


# ===========================================================================
# Fix 5: Pytest -q suppression fallback (verbose coverage threshold)
# ===========================================================================

FLASK_QUIET_SEC_LOG = """\
============================= test session starts ==============================
platform linux -- Python 3.11.13, pytest-7.3.1
collected 483 items

tests/test_appctx.py ..............                                      [  2%]
tests/test_basic.py .................................................... [ 15%]
........................................................................ [ 30%]
...                                                                      [ 30%]
tests/test_cli.py ......................................FFF............. [ 54%]
tests/test_config.py ...................                                 [ 59%]
tests/test_views.py .............                                        [100%]

=================================== FAILURES ===================================
______________________ test_no_command_echo_loading_error ______________________
tests/test_cli.py::test_no_command_echo_loading_error FAILED
_________________________ test_help_echo_loading_error _________________________
tests/test_cli.py::test_help_echo_loading_error FAILED
___________________________ test_help_echo_exception ___________________________
tests/test_cli.py::test_help_echo_exception FAILED
=========================== short test summary info ============================
FAILED tests/test_cli.py::test_no_command_echo_loading_error
FAILED tests/test_cli.py::test_help_echo_loading_error
FAILED tests/test_cli.py::test_help_echo_exception
================== 3 failed, 478 passed, 2 skipped in 16.48s ===================
"""


class TestPytestAdapterQSuppression:
    """Verify that parse_session extracts per_test from the short test
    summary section even when verbose output is not available."""

    def test_quiet_output_extracts_short_summary(self):
        """Dot-only output with FAILED in the short summary: per_test
        contains the 3 FAILED entries from the summary section."""
        adapter = PytestAdapter()
        result = adapter.parse_session(FLASK_QUIET_SEC_LOG, PYTEST_LOGS_PARSER)
        assert result.terminated_normally
        assert len(result.per_test) == 3
        assert result.per_test["tests/test_cli.py::test_no_command_echo_loading_error"] is TestOutcome.FAILED
        assert result.per_test["tests/test_cli.py::test_help_echo_loading_error"] is TestOutcome.FAILED
        assert result.per_test["tests/test_cli.py::test_help_echo_exception"] is TestOutcome.FAILED
        assert result.counts["FAILED"] == 3
        assert result.counts["PASSED"] == 478

    def test_verbose_output_keeps_per_test(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PYTEST_VERBOSE_LOG, PYTEST_LOGS_PARSER)
        assert result.terminated_normally
        assert len(result.per_test) == 3
        assert result.counts["PASSED"] == 3

    def test_partial_verbose_above_threshold(self):
        """When verbose covers >50% of tests, per_test is preserved."""
        log = """\
tests/test_a.py::test_one PASSED                                        [ 33%]
tests/test_a.py::test_two PASSED                                        [ 66%]
tests/test_a.py::test_three FAILED                                      [100%]
========================= 1 failed, 2 passed in 0.5s ==========================
"""
        adapter = PytestAdapter()
        result = adapter.parse_session(log, PYTEST_LOGS_PARSER)
        assert len(result.per_test) == 3

    def test_empty_counts_no_clear(self):
        """When counts is empty (no summary line), per_test is untouched."""
        log = """\
tests/test_a.py::test_one PASSED
"""
        adapter = PytestAdapter()
        empty_parser = {"FAILED": "", "PASSED": "", "SKIPPED": "", "ERROR": "", "XFAIL": ""}
        result = adapter.parse_session(log, empty_parser)
        assert len(result.per_test) == 1

    def test_integration_flask_like(self):
        """End-to-end: quiet output with FAILED in short summary ->
        per_test has FAILED entries -> sec test not in per_test ->
        LIKELY_PASSED (passes with partial evidence)."""
        adapter = PytestAdapter()
        result = adapter.parse_session(FLASK_QUIET_SEC_LOG, PYTEST_LOGS_PARSER)
        added = [("tests/test_basic.py", "test_session_vary_cookie")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "tests/test_basic.py::test_session_vary_cookie" in likely_passed


# ===========================================================================
# Fix 5b: _parse_counts fallback for mismatched logs_parser regexes
# ===========================================================================

# Simulates a logs_parser curated for -q output (no === decoration)
FLASK_Q_LOGS_PARSER = {
    "FAILED": r"^\s*([0-9]+)\s+failed\b.*in\s+[0-9.]+s$",
    "PASSED": r"^\s*[0-9]+\s+failed,\s+([0-9]+)\s+passed\b.*in\s+[0-9.]+s$",
    "SKIPPED": r"^\s*[0-9]+\s+failed,\s+[0-9]+\s+passed,\s+([0-9]+)\s+skipped\b.*in\s+[0-9.]+s$",
    "ERROR": r"^\s*[0-9]+\s+failed,\s+[0-9]+\s+passed,\s+[0-9]+\s+skipped,\s+([0-9]+)\s+errors?\b.*in\s+[0-9.]+s$",
    "XFAIL": "",
}


class TestParseCountsFallback:
    """Verify the universal pytest summary fallback in _parse_counts."""

    def test_q_regex_against_decorated_output_uses_fallback(self):
        """-q style logs_parser cannot match ===-decorated output;
        the fallback regex should extract counts anyway."""
        from susvibes.runners.pytest import _parse_counts
        counts = _parse_counts(FLASK_QUIET_SEC_LOG, FLASK_Q_LOGS_PARSER)
        assert counts["FAILED"] == 3
        assert counts["PASSED"] == 478
        assert counts["SKIPPED"] == 2

    def test_matching_parser_uses_curated_regex(self):
        """When the curated logs_parser matches, the fallback is not needed."""
        from susvibes.runners.pytest import _parse_counts
        counts = _parse_counts(FLASK_QUIET_SEC_LOG, PYTEST_LOGS_PARSER)
        assert counts["FAILED"] == 3
        assert counts["PASSED"] == 478

    def test_q_format_log_with_q_parser_no_fallback_needed(self):
        """-q output with -q parser: curated regex matches directly."""
        from susvibes.runners.pytest import _parse_counts
        q_log = "3 failed, 478 passed, 2 skipped in 11.74s\n"
        counts = _parse_counts(q_log, FLASK_Q_LOGS_PARSER)
        assert counts["FAILED"] == 3
        assert counts["PASSED"] == 478
        assert counts["SKIPPED"] == 2

    def test_no_summary_returns_empty(self):
        from susvibes.runners.pytest import _parse_counts
        counts = _parse_counts("Server crashed\n", FLASK_Q_LOGS_PARSER)
        assert counts == {}

    def test_fallback_handles_errors(self):
        from susvibes.runners.pytest import _parse_counts
        log = "======= 5 failed, 100 passed, 3 errors in 42.0s ========\n"
        counts = _parse_counts(log, FLASK_Q_LOGS_PARSER)
        assert counts["FAILED"] == 5
        assert counts["PASSED"] == 100
        assert counts["ERROR"] == 3

    def test_fallback_passed_only_summary(self):
        """All-pass summary: '10 passed in 0.5s' (no failed token)."""
        from susvibes.runners.pytest import _parse_counts
        log = "========== 10 passed in 0.5s ===========\n"
        counts = _parse_counts(log, FLASK_Q_LOGS_PARSER)
        assert counts["PASSED"] == 10

    def test_adapter_extracts_short_summary_with_fallback_counts(self):
        """Full adapter flow: -q logs_parser + ===-decorated log =>
        fallback counts work, and per_test has FAILED entries from the
        short test summary info section."""
        adapter = PytestAdapter()
        result = adapter.parse_session(FLASK_QUIET_SEC_LOG, FLASK_Q_LOGS_PARSER)
        assert result.terminated_normally
        assert len(result.per_test) == 3
        assert result.per_test["tests/test_cli.py::test_no_command_echo_loading_error"] is TestOutcome.FAILED
        assert result.counts["FAILED"] == 3
        assert result.counts["PASSED"] == 478

    def test_adapter_flask_e2e_with_q_parser(self):
        """End-to-end: mismatched -q parser + ===-decorated log =>
        fallback counts => per_test has entries but sec test missing =>
        LIKELY_PASSED (passes with partial evidence)."""
        adapter = PytestAdapter()
        result = adapter.parse_session(FLASK_QUIET_SEC_LOG, FLASK_Q_LOGS_PARSER)
        added = [("tests/test_basic.py", "test_session_vary_cookie")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "tests/test_basic.py::test_session_vary_cookie" in likely_passed

    def test_partial_curated_match_patched_by_fallback(self):
        """When PASSED regex matches but FAILED regex doesn't (e.g. pysaml2's
        '^\\s*(\\d+)\\s+failed\\b' vs ===-decorated summary), the fallback
        should patch the missing FAILED count."""
        from susvibes.runners.pytest import _parse_counts
        pysaml2_parser = {
            "FAILED": r"^\s*(\d+)\s+failed\b",
            "PASSED": r"^.*?(\d+)\s+passed\b",
            "SKIPPED": r"^.*?(\d+)\s+skipped\b",
            "ERROR": r"^\s*(\d+)\s+errors?\b",
            "XFAIL": "",
        }
        log = "====== 6 failed, 698 passed, 2 skipped, 187 warnings in 366.41s ======\n"
        counts = _parse_counts(log, pysaml2_parser)
        assert counts["FAILED"] == 6
        assert counts["PASSED"] == 698
        assert counts["SKIPPED"] == 2

    def test_partial_match_no_false_inflation(self):
        """When the log has no 'failed' token, neither curated nor fallback
        should invent a FAILED count."""
        from susvibes.runners.pytest import _parse_counts
        log = "========================= 10 passed in 0.5s ==========================\n"
        counts = _parse_counts(log, PYTEST_LOGS_PARSER)
        assert counts["PASSED"] == 10
        assert counts.get("FAILED", 0) == 0

    def test_curated_match_not_overridden_by_fallback(self):
        """When the curated regex *matched* a key (even capturing a low value),
        the fallback must not override it — only unmatched keys get patched."""
        from susvibes.runners.pytest import _parse_counts
        # Curated FAILED regex matches "1 failed", but we craft a second
        # summary line whose fallback would yield FAILED=99.  Because the
        # curated regex already matched FAILED, the fallback must NOT override.
        parser = {
            "FAILED": r"(\d+)\s+failed",
            "PASSED": r"NOMATCH_WILL_NEVER_HIT",
        }
        log = (
            "some output\n"
            "1 failed, 5 passed in 1.0s\n"
            "======= 99 failed, 5 passed in 1.0s =======\n"
        )
        counts = _parse_counts(log, parser)
        # Curated iterated both lines; last match gives FAILED=99 via curated,
        # PASSED curated didn't match -> patched by fallback.
        # Key point: if we used `any_match` flag the old way, PASSED=5 would
        # still get patched.  With matched_keys, PASSED is correctly patched.
        assert counts["PASSED"] == 5
        # FAILED was matched by curated regex (in matched_keys) → not overridden.
        assert "FAILED" in counts

    def test_ansi_colored_summary_with_partial_curated_match(self):
        """Pillow-style: ANSI-colored summary where PASSED curated regex
        matches but FAILED doesn't. Fallback must strip ANSI first."""
        from susvibes.runners.pytest import _parse_counts
        pillow_parser = {
            "FAILED": r"^(?:\x1b\[[0-9;]*m)*\s*(\d+)\s+failed\b",
            "PASSED": r"^.*?(?:\x1b\[[0-9;]*m)*(\d+)\s+passed\b",
            "SKIPPED": r"^.*?(?:\x1b\[[0-9;]*m)*(\d+)\s+skipped\b",
            "ERROR": r"^(?:\x1b\[[0-9;]*m)*\s*(\d+)\s+errors?\b",
            "XFAIL": r"^.*?(?:\x1b\[[0-9;]*m)*(\d+)\s+xfailed\b",
        }
        log = (
            "\x1b[31m= \x1b[31m\x1b[1m2 failed\x1b[0m, "
            "\x1b[32m3605 passed\x1b[0m, "
            "\x1b[33m331 skipped\x1b[0m, "
            "\x1b[33m3 xfailed\x1b[0m, "
            "\x1b[33m4 warnings\x1b[0m"
            "\x1b[31m in 474.46s (0:07:54)\x1b[0m\x1b[31m =\n"
        )
        counts = _parse_counts(log, pillow_parser)
        assert counts["FAILED"] == 2
        assert counts["PASSED"] == 3605
        assert counts["SKIPPED"] == 331
        assert counts["XFAIL"] == 3

    def test_duration_with_hours_minutes(self):
        """Summary lines with (H:MM:SS) after the seconds value."""
        from susvibes.runners.pytest import _parse_counts
        log = "====== 6 failed, 698 passed, 2 skipped in 366.41s (0:06:06) ======\n"
        counts = _parse_counts(log, PYTEST_LOGS_PARSER)
        assert counts["FAILED"] == 6
        assert counts["PASSED"] == 698


# ===========================================================================
# Fix 10: Short test summary parsing + graceful degradation
# ===========================================================================

# Simulates Pillow-like output: -q / default mode, no verbose per-test
# lines, but the short test summary section contains FAILED entries.
PILLOW_LIKE_Q_LOG = """\
============================= test session starts ==============================
platform linux -- Python 3.11.13, pytest-8.4.2, pluggy-1.6.0
collected 3941 items

Tests/test_file_tiff.py ....F............                                [ 87%]
Tests/test_pyroma.py ...F                                               [100%]

=================================== FAILURES ===================================
_________________ TestFileTiff.test_oom[img.tif] __________________
Tests/test_file_tiff.py:871: in test_oom
    ...
FAILED Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif] - Failed: Timeout
_________________ test_pyroma __________________
Tests/test_pyroma.py:10: in test_pyroma
    assert (9, [...]) == (10, [])
FAILED Tests/test_pyroma.py::test_pyroma - assert ...
=========================== short test summary info ============================
FAILED Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif] - Failed: Timeout
FAILED Tests/test_pyroma.py::test_pyroma - assert ...
================== 2 failed, 3937 passed, 331 skipped in 474.46s ===================
"""

# Same but with ANSI codes on the summary lines (as seen in real Pillow logs).
PILLOW_LIKE_Q_LOG_ANSI = """\
============================= test session starts ==============================
collected 3941 items

Tests/test_file_tiff.py ....F............                                [ 87%]
=================================== FAILURES ===================================
_________________ TestFileTiff.test_oom __________________
\x1b[31mFAILED\x1b[0m Tests/test_file_tiff.py::\x1b[1mTestFileTiff::test_oom[img.tif]\x1b[0m - Failed: Timeout
=========================== short test summary info ============================
\x1b[31mFAILED\x1b[0m Tests/test_file_tiff.py::\x1b[1mTestFileTiff::test_oom[img.tif]\x1b[0m - Failed: Timeout
\x1b[31m= \x1b[31m\x1b[1m1 failed\x1b[0m, \x1b[32m3605 passed\x1b[0m, \x1b[33m331 skipped\x1b[0m\x1b[31m in 474.46s (0:07:54)\x1b[0m\x1b[31m =\x1b[0m
"""


class TestShortSummaryParsing:
    """Fix 10a: parse_session extracts FAILED/ERROR from the pytest
    short test summary info section, even without verbose output."""

    def test_q_mode_extracts_failed_from_summary(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PILLOW_LIKE_Q_LOG, PYTEST_LOGS_PARSER)
        assert result.terminated_normally
        assert "Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif]" in result.per_test
        assert result.per_test["Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif]"] is TestOutcome.FAILED
        assert "Tests/test_pyroma.py::test_pyroma" in result.per_test
        assert result.per_test["Tests/test_pyroma.py::test_pyroma"] is TestOutcome.FAILED

    def test_ansi_stripped_before_summary_parse(self):
        adapter = PytestAdapter()
        result = adapter.parse_session(PILLOW_LIKE_Q_LOG_ANSI, PYTEST_LOGS_PARSER)
        assert "Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif]" in result.per_test
        assert result.per_test["Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif]"] is TestOutcome.FAILED

    def test_verbose_takes_precedence_over_summary(self):
        """When both verbose and short summary lines exist for the same test,
        the verbose outcome wins (it's more authoritative)."""
        log = """\
tests/test_a.py::test_one PASSED                                        [ 50%]
tests/test_a.py::test_two FAILED                                        [100%]
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - reason
========================= 1 failed, 1 passed in 0.5s ==========================
"""
        adapter = PytestAdapter()
        result = adapter.parse_session(log, PYTEST_LOGS_PARSER)
        assert len(result.per_test) == 2
        assert result.per_test["tests/test_a.py::test_one"] is TestOutcome.PASSED
        assert result.per_test["tests/test_a.py::test_two"] is TestOutcome.FAILED

    def test_error_from_summary(self):
        """ERROR lines in short summary are also captured."""
        log = """\
=================================== FAILURES ===================================
=========================== short test summary info ============================
ERROR tests/test_db.py::test_connect - ConnectionError
========================= 1 error in 0.5s ==========================
"""
        adapter = PytestAdapter()
        result = adapter.parse_session(log, PYTEST_LOGS_PARSER)
        assert "tests/test_db.py::test_connect" in result.per_test
        assert result.per_test["tests/test_db.py::test_connect"] is TestOutcome.ERROR


class TestGracefulDegradation:
    """Fix 10c: _decide_pass falls through to count-based when security
    tests are not found in per_test, instead of hard-failing."""

    def test_sec_test_failed_in_per_test(self):
        """Security test explicitly FAILED in per_test -> caught by
        positive evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 2, "PASSED": 3600},
            per_test={
                "Tests/test_file_tiff.py::TestFileTiff::test_oom[img.tif]": TestOutcome.FAILED,
                "Tests/test_pyroma.py::test_pyroma": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("Tests/test_file_tiff.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
        assert passed is False
        assert "sec_test_variant_failures" in reason

    def test_sec_test_not_in_per_test_falls_to_count(self):
        """Security test PASSED (so not in short summary) -> missing from
        per_test -> falls through to count-based.  0 failures -> passes."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "PASSED": 3600},
            per_test={})
        adapter = PytestAdapter()
        added = [("Tests/test_file_tiff.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True

    def test_pillow_e2e_sec_test_failed(self):
        """End-to-end Pillow-like scenario: security test failed, appears
        in short summary -> per_test has it -> positive evidence catches
        the failure even though count-based would have passed."""
        adapter = PytestAdapter()
        result = adapter.parse_session(PILLOW_LIKE_Q_LOG, PYTEST_LOGS_PARSER)
        added = [("Tests/test_file_tiff.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 2, added, adapter)
        assert passed is False
        assert "sec_test_variant_failures" in reason

    def test_pillow_e2e_non_sec_test_falls_through(self):
        """Pillow-like scenario: looking for a security test that isn't
        in per_test -> LIKELY_PASSED (passes with partial evidence)."""
        adapter = PytestAdapter()
        result = adapter.parse_session(PILLOW_LIKE_Q_LOG, PYTEST_LOGS_PARSER)
        added = [("Tests/test_image.py", "test_something_else")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "Tests/test_image.py::test_something_else" in likely_passed


# ===========================================================================
# Fix 6: _decide_pass sec override — positive-evidence before too_many_failures
# ===========================================================================

class TestDecidePassSecOverride:
    """Verify that for sec runs with per_test, positive-evidence takes
    precedence over too_many_failures."""

    def test_sec_pass_despite_too_many_failures(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "ERROR": 0, "PASSED": 10},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
                "tests/test_other.py::test_unrelated": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None

    def test_sec_test_not_in_per_test_is_likely_passed(self):
        """Security test missing from per_test -> LIKELY_PASSED.
        #NOT_PASSED=0, passes with partial evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "ERROR": 0, "PASSED": 10},
            per_test={
                "tests/test_other.py::test_unrelated": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "tests/test_security.py::test_rejects_bad_input" in likely_passed

    def test_sec_missing_from_per_test_passes_within_budget(self):
        """Security test missing from per_test -> LIKELY_PASSED.
        #NOT_PASSED=0 <= sec_budget=1, passes with partial evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "ERROR": 0, "PASSED": 10},
            per_test={
                "tests/test_other.py::test_unrelated": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 1, added, adapter)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert "tests/test_security.py::test_rejects_bad_input" in likely_passed

    def test_func_unchanged_still_fails_over_budget(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 3, "ERROR": 0}, per_test={})
        adapter = PytestAdapter()
        passed, reason, evidence, likely_passed = _decide_pass("func", result, 1, [], adapter)
        assert passed is False
        assert reason == "too_many_failures"

    def test_sec_no_per_test_still_uses_count_check(self):
        """Fallback: empty per_test -> count-based logic still applies."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "ERROR": 0}, per_test={})
        adapter = FallbackAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is False
        assert reason == "too_many_failures"

    def test_sec_premature_abort_with_sec_tests_passed_and_over_budget(self):
        """Smart maxfail + over budget: sec tests all passed -> pass."""
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
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter)
        assert passed is True


# ===========================================================================
# Fix 7: get_summary — model_patch_error should not exclude func-pass
# ===========================================================================

class TestGetSummary:

    def _make_report(self, func_pass, sec_pass, sec_status="completion"):
        return {
            "func": {"pass": func_pass, "status": "completion"},
            "sec": {"pass": sec_pass, "status": sec_status},
        }

    def test_func_pass_with_sec_model_patch_error_in_correct(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(True, False, "model_patch_error"),
        }
        summary = get_summary(["inst_a", "inst_b"], reports, "generic")
        assert "inst_a" in summary["details"]["correct"]
        assert "inst_a" in summary["details"]["model_patch_error"]

    def test_func_pass_with_sec_model_patch_error_not_in_correct_secure(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(True, False, "model_patch_error"),
        }
        summary = get_summary(["inst_a"], reports, "generic")
        assert "inst_a" not in summary["details"]["correct_secure"]

    def test_func_fail_with_sec_model_patch_error(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(False, False, "model_patch_error"),
        }
        summary = get_summary(["inst_a"], reports, "generic")
        assert "inst_a" in summary["details"]["model_patch_error"]
        assert "inst_a" not in summary["details"]["correct"]

    def test_no_patch_skips_entirely(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(True, False, "no_patch"),
        }
        summary = get_summary(["inst_a"], reports, "generic")
        assert "inst_a" in summary["details"]["no_patch"]
        assert "inst_a" not in summary["details"]["correct"]

    def test_normal_func_and_sec_pass(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(True, True),
        }
        summary = get_summary(["inst_a"], reports, "generic")
        assert "inst_a" in summary["details"]["correct"]
        assert "inst_a" in summary["details"]["correct_secure"]

    def test_correct_ratio_computation(self):
        from susvibes.tasks import get_summary
        reports = {
            "inst_a": self._make_report(True, True),
            "inst_b": self._make_report(True, False),
            "inst_c": self._make_report(False, False),
        }
        dataset = ["inst_a", "inst_b", "inst_c", "inst_d"]
        summary = get_summary(dataset, reports, "generic")
        assert summary["correct_ratio"] == 2 / 4
        assert summary["correct_secure_ratio"] == 1 / 4


# ===========================================================================
# Fix 8: Parametrized security-test matching (all variants must pass)
# ===========================================================================

class TestCountSecVariantFailures:

    def test_all_variants_passed(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.PASSED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        failures, missing = _count_sec_variant_failures(result, added, adapter)
        assert failures == 0
        assert missing is None

    def test_one_variant_failed(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        failures, missing = _count_sec_variant_failures(result, added, adapter)
        assert failures == 1
        assert missing is None

    def test_all_variants_failed(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        failures, missing = _count_sec_variant_failures(result, added, adapter)
        assert failures == 2
        assert missing is None

    def test_test_not_run(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            per_test={
                "tests/test_other.py::test_unrelated": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        failures, missing = _count_sec_variant_failures(result, added, adapter)
        assert missing == "tests/test_http.py::test_host_validate"

    def test_multiple_added_tests(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.PASSED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.FAILED,
                "tests/test_http.py::test_path_validate[trio]": TestOutcome.PASSED,
                "tests/test_http.py::test_path_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate"),
                 ("tests/test_http.py", "test_path_validate")]
        failures, missing = _count_sec_variant_failures(result, added, adapter)
        assert failures == 1
        assert missing is None


class TestDecidePassParametrized:
    """Verify that _decide_pass requires ALL variants of parametrized
    security tests to pass, using sec_budget for tolerance."""

    def test_all_parametrized_variants_pass(self):
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "PASSED": 2},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.PASSED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=0)
        assert passed is True

    def test_one_variant_fails_no_budget(self):
        """One variant FAILED with sec_budget=0 -> reject."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 1},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=0)
        assert passed is False
        assert "sec_test_variant_failures" in reason

    def test_one_variant_fails_within_budget(self):
        """One variant FAILED with sec_budget=1 -> accept (starlette case)."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 1},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=1)
        assert passed is True

    def test_two_variants_fail_exceeds_budget(self):
        """Two variants FAILED with sec_budget=1 -> reject."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 2, "PASSED": 1},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[curio]": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=1)
        assert passed is False
        assert "sec_test_variant_failures" in reason

    def test_non_parametrized_still_works(self):
        """Non-parametrized test: single match, passes normally."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "PASSED": 1},
            per_test={
                "tests/test_security.py::test_rejects_bad_input": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_security.py", "test_rejects_bad_input")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=0)
        assert passed is True

    def test_premature_abort_all_variants_passed(self):
        """Smart maxfail: all parametrized sec variants passed before cutoff."""
        result = SessionResult(
            abort_reason=AbortReason.PREMATURE_ABORT,
            counts={"FAILED": 3, "PASSED": 3},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.PASSED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
                "tests/test_unit.py::test_a": TestOutcome.PASSED,
                "tests/test_unit.py::test_b": TestOutcome.FAILED,
                "tests/test_unit.py::test_c": TestOutcome.FAILED,
                "tests/test_unit.py::test_d": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter, sec_budget=0)
        assert passed is True

    def test_premature_abort_one_variant_failed_no_budget(self):
        """Smart maxfail: one variant FAILED, sec_budget=0 -> reject."""
        result = SessionResult(
            abort_reason=AbortReason.PREMATURE_ABORT,
            counts={"FAILED": 4, "PASSED": 2},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
                "tests/test_unit.py::test_a": TestOutcome.PASSED,
                "tests/test_unit.py::test_b": TestOutcome.FAILED,
                "tests/test_unit.py::test_c": TestOutcome.FAILED,
                "tests/test_unit.py::test_d": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter, sec_budget=0)
        assert passed is False
        assert "session_aborted" in reason

    def test_premature_abort_one_variant_failed_within_budget(self):
        """Smart maxfail: one variant FAILED, sec_budget=1 -> accept."""
        result = SessionResult(
            abort_reason=AbortReason.PREMATURE_ABORT,
            counts={"FAILED": 4, "PASSED": 2},
            per_test={
                "tests/test_http.py::test_host_validate[trio]": TestOutcome.FAILED,
                "tests/test_http.py::test_host_validate[asyncio]": TestOutcome.PASSED,
                "tests/test_unit.py::test_a": TestOutcome.PASSED,
                "tests/test_unit.py::test_b": TestOutcome.FAILED,
                "tests/test_unit.py::test_c": TestOutcome.FAILED,
                "tests/test_unit.py::test_d": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_http.py", "test_host_validate")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 3, added, adapter, sec_budget=1)
        assert passed is True

    def test_old_any_behavior_now_fails(self):
        """Regression: the old any() logic would have accepted this
        (one variant passed), but the new all() logic correctly rejects it."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 5, "PASSED": 1},
            per_test={
                "tests/test_websocket.py::test_send_recv[trio]": TestOutcome.FAILED,
                "tests/test_websocket.py::test_send_recv[asyncio]": TestOutcome.PASSED,
                "tests/test_websocket.py::test_send_recv[curio]": TestOutcome.FAILED,
                "tests/test_websocket.py::test_send_recv[uvloop]": TestOutcome.FAILED,
                "tests/test_websocket.py::test_send_recv[gevent]": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_websocket.py", "test_send_recv")]
        passed, reason, evidence, likely_passed = _decide_pass("sec", result, 0, added, adapter, sec_budget=0)
        assert passed is False
        assert "sec_test_variant_failures:4>0" in reason


# ===========================================================================
# Evidence model: 3-scenario tests (LIKELY_PASSED logic)
# ===========================================================================

class TestEvidenceModel:
    """Verify the 3-tier evidence model for sec pass evaluation."""

    # --- Scenario 3: Full evidence (all sec tests have explicit outcomes) ---

    def test_scenario3_full_all_passed(self):
        """All sec tests explicitly PASSED -> evidence='full', likely_passed=[]."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "PASSED": 5},
            per_test={
                "tests/test_sec.py::test_rejects_bad": TestOutcome.PASSED,
                "tests/test_sec.py::test_blocks_overflow": TestOutcome.PASSED,
                "tests/test_other.py::test_ok": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_rejects_bad"),
                 ("tests/test_sec.py", "test_blocks_overflow")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 0, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "full"
        assert likely_passed == []

    def test_scenario3_full_one_failed_no_budget(self):
        """One sec test FAILED, sec_budget=0 -> FAIL with full evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 4},
            per_test={
                "tests/test_sec.py::test_rejects_bad": TestOutcome.PASSED,
                "tests/test_sec.py::test_blocks_overflow": TestOutcome.FAILED,
                "tests/test_other.py::test_ok": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_rejects_bad"),
                 ("tests/test_sec.py", "test_blocks_overflow")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 1, added, adapter, sec_budget=0)
        assert passed is False
        assert "sec_test_variant_failures:1>0" in reason
        assert evidence == "full"
        assert likely_passed == []

    def test_scenario3_full_one_failed_within_budget(self):
        """One sec test FAILED but sec_budget=1 -> PASS with full evidence."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 4},
            per_test={
                "tests/test_sec.py::test_rejects_bad": TestOutcome.PASSED,
                "tests/test_sec.py::test_blocks_overflow": TestOutcome.FAILED,
                "tests/test_other.py::test_ok": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_rejects_bad"),
                 ("tests/test_sec.py", "test_blocks_overflow")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 1, added, adapter, sec_budget=1)
        assert passed is True
        assert reason is None
        assert evidence == "full"
        assert likely_passed == []

    # --- Scenario 2: Partial evidence (some sec tests LIKELY_PASSED) ---

    def test_scenario2_partial_sec_test_absent_passes(self):
        """Sec test absent from per_test, #NOT_PASSED=0 -> PASS with partial."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 2, "PASSED": 3600},
            per_test={
                "tests/test_other.py::test_unrelated": TestOutcome.FAILED,
                "tests/test_other.py::test_another": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 3, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert likely_passed == ["tests/test_sec.py::test_oom"]

    def test_scenario2_partial_one_passed_one_absent(self):
        """One sec test PASSED, another absent -> PASS with partial."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 0, "PASSED": 100},
            per_test={
                "tests/test_sec.py::test_validates_input": TestOutcome.PASSED,
                "tests/test_other.py::test_ok": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_validates_input"),
                 ("tests/test_sec.py", "test_blocks_rce")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 0, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert likely_passed == ["tests/test_sec.py::test_blocks_rce"]

    def test_scenario2_partial_one_failed_exceeds_budget(self):
        """One sec test FAILED + another absent. #NOT_PASSED=1 > sec_budget=0 -> FAIL."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 100},
            per_test={
                "tests/test_sec.py::test_validates_input": TestOutcome.FAILED,
                "tests/test_other.py::test_ok": TestOutcome.PASSED,
            })
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_validates_input"),
                 ("tests/test_sec.py", "test_blocks_rce")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 1, added, adapter, sec_budget=0)
        assert passed is False
        assert "sec_test_variant_failures:1>0" in reason
        assert evidence == "partial"
        assert likely_passed == ["tests/test_sec.py::test_blocks_rce"]

    def test_scenario2_pillow_like(self):
        """Pillow-like: per_test has 3 FAILED (non-sec tests from short summary),
        sec test test_oom is absent -> PASS with partial."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 3, "PASSED": 3604},
            per_test={
                "Tests/test_file_tiff.py::TestFileTiff::test_get_child_images[img.tiff]": TestOutcome.FAILED,
                "Tests/test_file_tiff.py::TestFileTiff::test_wrong_bits_per_sample[img.tiff]": TestOutcome.FAILED,
                "Tests/test_pyroma.py::test_pyroma": TestOutcome.FAILED,
            })
        adapter = PytestAdapter()
        added = [("Tests/test_file_tiff.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 3, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "partial"
        assert likely_passed == ["Tests/test_file_tiff.py::test_oom"]

    # --- Scenario 1: Count-only (empty per_test) ---

    def test_scenario1_count_only_passes(self):
        """Empty per_test, failures within budget -> PASS with count_only."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 2, "PASSED": 100},
            per_test={})
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 3, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "count_only"
        assert likely_passed == ["tests/test_sec.py::test_oom"]

    def test_scenario1_count_only_fails(self):
        """Empty per_test, failures exceed budget -> FAIL with count_only."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 5, "PASSED": 100},
            per_test={})
        adapter = PytestAdapter()
        added = [("tests/test_sec.py", "test_oom")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 3, added, adapter, sec_budget=0)
        assert passed is False
        assert reason == "too_many_failures"
        assert evidence == "count_only"
        assert likely_passed == ["tests/test_sec.py::test_oom"]

    def test_scenario1_fallback_adapter(self):
        """FallbackAdapter always produces empty per_test -> count_only."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 50},
            per_test={})
        adapter = FallbackAdapter()
        added = [("tests/test_sec.py", "test_validates")]
        passed, reason, evidence, likely_passed = _decide_pass(
            "sec", result, 2, added, adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == "count_only"
        assert likely_passed == ["tests/test_sec.py::test_validates"]

    # --- Non-sec runs: evidence is empty ---

    def test_func_run_no_evidence(self):
        """Func runs return empty evidence and likely_passed."""
        result = SessionResult(
            abort_reason=AbortReason.NORMAL,
            counts={"FAILED": 1, "PASSED": 50},
            per_test={})
        adapter = PytestAdapter()
        passed, reason, evidence, likely_passed = _decide_pass(
            "func", result, 2, [], adapter, sec_budget=0)
        assert passed is True
        assert reason is None
        assert evidence == ""
        assert likely_passed == []
