"""Phase 7: synthetic unit tests for the evaluation logic.

Ported selectively from the reference harness's ``test_evaluation_logic.py``
and adapted to this branch's API. These build ``SessionResult`` objects by
hand (no real logs, no Docker) and assert the contract of:

- ``SessionResult`` helpers
- ``RunnerAdapter.extract_added_tests`` (test_patch diff parser)
- ``PytestAdapter`` / ``DjangoTestAdapter`` per-test extraction + ``match_test``
- ``_decide_pass`` — the 3-tier evidence model (full / partial / count_only),
  including ``positive_sec_evidence`` and ``no_positive_sec_evidence`` which are
  specific to this branch
- ``get_summary`` evidence-tier aggregation (reporting only)

The real-log regression catalog (R01–R16) lives in ``test_eval_regression.py``;
this file covers the boundary/edge combinations that real logs don't reliably
provide on demand.
"""

from __future__ import annotations

from susvibes.runners.base import (
    AbortReason,
    SessionResult,
    TestOutcome as Outcome,  # aliased so pytest doesn't try to collect it
    TestRunnerAdapter as RunnerAdapter,
)
from susvibes.runners import DjangoTestAdapter, FallbackAdapter, PytestAdapter
from susvibes.tasks import _decide_pass, get_summary


class TestSessionResult:
    def test_terminated_normally_true(self):
        assert SessionResult(AbortReason.NORMAL).terminated_normally is True

    def test_terminated_normally_false(self):
        assert SessionResult(AbortReason.CRASH).terminated_normally is False
        assert SessionResult(AbortReason.BUILD_ERROR).terminated_normally is False

    def test_visible_failures_counts_failed_and_error(self):
        r = SessionResult(
            AbortReason.NORMAL,
            counts={"FAILED": 2, "ERROR": 1, "PASSED": 5, "SKIPPED": 3},
        )
        assert r.visible_failures() == 3

    def test_visible_failures_empty_counts(self):
        assert SessionResult(AbortReason.NORMAL).visible_failures() == 0


class TestExtractAddedTests:
    def test_snake_async_camel(self):
        patch = (
            "diff --git a/tests/test_security.py b/tests/test_security.py\n"
            "--- a/tests/test_security.py\n"
            "+++ b/tests/test_security.py\n"
            "@@ -1,1 +1,4 @@\n"
            "+def test_rejects_bad_input():\n"
            "+async def test_allows_good_input():\n"
            "+def testCamelCase():\n"
        )
        assert RunnerAdapter.extract_added_tests(patch) == [
            ("tests/test_security.py", "test_rejects_bad_input"),
            ("tests/test_security.py", "test_allows_good_input"),
            ("tests/test_security.py", "testCamelCase"),
        ]

    def test_indented_method_is_captured(self):
        # Class methods are added with leading whitespace and must be captured.
        patch = (
            "diff --git a/tests/t.py b/tests/t.py\n"
            "+++ b/tests/t.py\n"
            "+    def test_method_in_class():\n"
        )
        assert RunnerAdapter.extract_added_tests(patch) == [
            ("tests/t.py", "test_method_in_class"),
        ]

    def test_context_line_is_ignored(self):
        # A `def test_*` on a context line (no '+') is not an addition.
        patch = (
            "diff --git a/tests/t.py b/tests/t.py\n"
            "+++ b/tests/t.py\n"
            " def test_context_only():\n"
            "+def test_added_one():\n"
        )
        assert RunnerAdapter.extract_added_tests(patch) == [
            ("tests/t.py", "test_added_one"),
        ]

    def test_multiple_files(self):
        patch = (
            "diff --git a/tests/a.py b/tests/a.py\n"
            "+++ b/tests/a.py\n"
            "+def test_a():\n"
            "diff --git a/tests/b.py b/tests/b.py\n"
            "+++ b/tests/b.py\n"
            "+def test_b():\n"
        )
        assert RunnerAdapter.extract_added_tests(patch) == [
            ("tests/a.py", "test_a"),
            ("tests/b.py", "test_b"),
        ]


class TestPytestAdapter:
    def test_extract_per_test_verbose(self):
        logs = (
            "tests/test_x.py::test_a PASSED [ 50%]\n"
            "tests/test_x.py::test_b FAILED [100%]\n"
        )
        assert PytestAdapter().extract_per_test(logs) == {
            "tests/test_x.py::test_a": Outcome.PASSED,
            "tests/test_x.py::test_b": Outcome.FAILED,
        }

    def test_extract_per_test_short_summary(self):
        logs = (
            "=== short test summary info ===\n"
            "FAILED tests/test_y.py::test_c - AssertionError\n"
        )
        assert PytestAdapter().extract_per_test(logs) == {
            "tests/test_y.py::test_c": Outcome.FAILED,
        }

    def test_match_test_plain(self):
        assert PytestAdapter().match_test(
            "tests/test_x.py::test_a", "tests/test_x.py", "test_a") is True

    def test_match_test_parametrized(self):
        assert PytestAdapter().match_test(
            "test_x.py::test_a[case1]", "tests/test_x.py", "test_a") is True

    def test_match_test_negative(self):
        assert PytestAdapter().match_test(
            "tests/test_x.py::test_a", "tests/test_x.py", "test_b") is False

    def test_get_verbose_env(self):
        assert PytestAdapter().get_verbose_env() == {"PYTEST_ADDOPTS": "-v"}


class TestDjangoAdapter:
    def test_extract_per_test_verbose_and_header(self):
        logs = (
            "test_foo (mod.Cls) ... ok\n"
            "test_bar (mod.Cls) ... FAIL\n"
            "\nFAIL: test_baz (mod.Cls)\n"
        )
        assert DjangoTestAdapter().extract_per_test(logs) == {
            "test_foo": Outcome.PASSED,
            "test_bar": Outcome.FAILED,
            "test_baz": Outcome.FAILED,
        }

    def test_match_test(self):
        assert DjangoTestAdapter().match_test(
            "test_foo", "tests/test_x.py", "test_foo") is True
        assert DjangoTestAdapter().match_test(
            "test_foo", "tests/test_x.py", "test_bar") is False


class TestEvidenceModel:
    """3-tier evidence model via _decide_pass.

    Returns (passed, reason, evidence, likely_passed, positive_sec_evidence).
    """

    SEC = "tests/test_sec.py"

    # --- full: every sec test has an explicit per_test outcome ---

    def test_full_all_passed(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 0, "PASSED": 5},
            per_test={f"{self.SEC}::test_a": Outcome.PASSED,
                      f"{self.SEC}::test_b": Outcome.PASSED})
        added = [(self.SEC, "test_a"), (self.SEC, "test_b")]
        assert _decide_pass("sec", result, 0, added, PytestAdapter(), sec_budget=0) == (
            True, None, "full", [], True)

    def test_full_one_failed_no_budget(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 1, "PASSED": 4},
            per_test={f"{self.SEC}::test_a": Outcome.PASSED,
                      f"{self.SEC}::test_b": Outcome.FAILED})
        added = [(self.SEC, "test_a"), (self.SEC, "test_b")]
        passed, reason, evidence, likely, positive = _decide_pass(
            "sec", result, 1, added, PytestAdapter(), sec_budget=0)
        assert passed is False
        assert reason == "sec_test_variant_failures:1>0"
        assert evidence == "full" and likely == [] and positive is True

    def test_full_one_failed_within_budget(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 1, "PASSED": 4},
            per_test={f"{self.SEC}::test_a": Outcome.PASSED,
                      f"{self.SEC}::test_b": Outcome.FAILED})
        added = [(self.SEC, "test_a"), (self.SEC, "test_b")]
        assert _decide_pass("sec", result, 1, added, PytestAdapter(), sec_budget=1) == (
            True, None, "full", [], True)

    # --- partial: some sec tests absent from per_test (likely_passed) ---

    def test_partial_absent_no_positive_evidence(self):
        # Sec test absent; only unrelated failures present, within budget.
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 2, "PASSED": 3600},
            per_test={"tests/test_other.py::t1": Outcome.FAILED,
                      "tests/test_other.py::t2": Outcome.FAILED})
        added = [(self.SEC, "test_oom")]
        passed, reason, evidence, likely, positive = _decide_pass(
            "sec", result, 3, added, PytestAdapter(), sec_budget=0)
        assert passed is True
        assert reason == "no_positive_sec_evidence"
        assert evidence == "partial"
        assert likely == [f"{self.SEC}::test_oom"]
        assert positive is False

    def test_partial_one_passed_one_absent(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 0, "PASSED": 100},
            per_test={f"{self.SEC}::test_validates_input": Outcome.PASSED})
        added = [(self.SEC, "test_validates_input"), (self.SEC, "test_blocks_rce")]
        passed, reason, evidence, likely, positive = _decide_pass(
            "sec", result, 0, added, PytestAdapter(), sec_budget=0)
        assert passed is True and reason is None
        assert evidence == "partial"
        assert likely == [f"{self.SEC}::test_blocks_rce"]
        assert positive is True

    def test_partial_one_failed_exceeds_budget(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 1, "PASSED": 100},
            per_test={f"{self.SEC}::test_validates_input": Outcome.FAILED})
        added = [(self.SEC, "test_validates_input"), (self.SEC, "test_blocks_rce")]
        passed, reason, evidence, likely, positive = _decide_pass(
            "sec", result, 1, added, PytestAdapter(), sec_budget=0)
        assert passed is False
        assert reason == "sec_test_variant_failures:1>0"
        assert evidence == "partial"
        assert likely == [f"{self.SEC}::test_blocks_rce"]
        assert positive is False

    # --- count_only: empty per_test, decision from summary counts ---

    def test_count_only_pass(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 2, "PASSED": 100}, per_test={})
        added = [(self.SEC, "test_oom")]
        assert _decide_pass("sec", result, 3, added, PytestAdapter(), sec_budget=0) == (
            True, None, "count_only", [f"{self.SEC}::test_oom"], False)

    def test_count_only_fail(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 5, "PASSED": 100}, per_test={})
        added = [(self.SEC, "test_oom")]
        assert _decide_pass("sec", result, 3, added, PytestAdapter(), sec_budget=0) == (
            False, "too_many_failures", "count_only", [f"{self.SEC}::test_oom"], False)

    def test_count_only_fallback_adapter(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 1, "PASSED": 50}, per_test={})
        added = [(self.SEC, "test_validates")]
        assert _decide_pass("sec", result, 2, added, FallbackAdapter(), sec_budget=0) == (
            True, None, "count_only", [f"{self.SEC}::test_validates"], False)

    # --- func runs carry no evidence ---

    def test_func_run_no_evidence(self):
        result = SessionResult(
            AbortReason.NORMAL, counts={"FAILED": 1, "PASSED": 50}, per_test={})
        assert _decide_pass("func", result, 2, [], PytestAdapter(), sec_budget=0) == (
            True, None, "", [], False)

    # --- abnormal termination ---

    def test_abort_crash_no_evidence(self):
        result = SessionResult(AbortReason.CRASH, counts={}, per_test={})
        added = [(self.SEC, "test_a")]
        assert _decide_pass("sec", result, 0, added, PytestAdapter(), sec_budget=0) == (
            False, "session_aborted:CRASH", "", [], False)

    def test_build_error_is_no_test_summary(self):
        result = SessionResult(AbortReason.BUILD_ERROR, counts={}, per_test={})
        added = [(self.SEC, "test_a")]
        assert _decide_pass("sec", result, 0, added, PytestAdapter(), sec_budget=0) == (
            False, "no_test_summary", "", [], False)

    def test_explicit_sec_failure_beats_abort_gate(self):
        # A maxfail abort triggered *by* a failing sec test is reported as a sec
        # variant failure, not session_aborted (gate order; R01).
        result = SessionResult(
            AbortReason.PREMATURE_ABORT, counts={"FAILED": 10, "PASSED": 820},
            per_test={f"{self.SEC}::test_a": Outcome.FAILED})
        added = [(self.SEC, "test_a")]
        passed, reason, evidence, likely, positive = _decide_pass(
            "sec", result, 9, added, PytestAdapter(), sec_budget=0)
        assert passed is False
        assert reason == "sec_test_variant_failures:1>0"
        assert evidence == "full"


class TestSummaryEvidenceStats:
    """get_summary aggregates evidence tiers (reporting only, no decision change)."""

    @staticmethod
    def _report(func_pass, sec_pass, status="completion", evidence=None):
        sec = {"pass": sec_pass, "status": status}
        if evidence is not None:
            sec["evidence"] = evidence
        return {"func": {"pass": func_pass, "status": status}, "sec": sec}

    def test_evidence_tally(self):
        reports = {
            "a": self._report(True, True, evidence="full"),
            "b": self._report(True, True, evidence="count_only"),
            "c": self._report(True, False, evidence="partial"),
            "d": self._report(False, False, status="startup_error"),
        }
        summary = get_summary([1, 2, 3, 4], reports, "default")
        assert summary["evidence_stats"] == {
            "full": 1, "partial": 1, "count_only": 1, "none": 1}

    def test_evidence_stats_independent_of_ratios(self):
        reports = {
            "a": self._report(True, True, evidence="full"),
            "b": self._report(True, False, evidence="count_only"),
        }
        summary = get_summary([1, 2], reports, "default")
        # 1 of 2 correct & secure; evidence labels do not move the ratio.
        assert summary["correct_secure_ratio"] == 0.5
        assert summary["evidence_stats"]["full"] == 1
        assert summary["evidence_stats"]["count_only"] == 1
