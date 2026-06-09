"""Runner registry and FallbackAdapter.

detect_runner() inspects the Dockerfile CMD to pick the right adapter.
When the CMD is ambiguous, detect_runner_from_output() can refine the
choice based on actual test output content.

FallbackAdapter wraps the original count-based path for instances whose
test runner is unrecognized.
"""

from __future__ import annotations

import re

from susvibes.runners.base import (
    AbortReason,
    SessionResult,
    TestRunnerAdapter,
)
from susvibes.runners.django import DjangoTestAdapter
from susvibes.runners.pytest import PytestAdapter


# Heuristic patterns for output-based detection
_PYTEST_OUTPUT_MARKERS = re.compile(
    r"(?:"
    r"::.*(?:PASSED|FAILED|ERROR)"  # node_id::test PASSED/FAILED
    r"|=+\s+\d+\s+(?:passed|failed)"  # === N passed, M failed ===
    r"|=+\s+short test summary"  # === short test summary info ===
    r"|=+\s+FAILURES\s+=+"  # === FAILURES ===
    r"|collected\s+\d+\s+item"  # collected N items
    r"|^FAILED\s+\S+::\S+"  # FAILED path::test (short summary)
    r")",
    re.MULTILINE,
)

_DJANGO_OUTPUT_MARKERS = re.compile(
    r"(?:"
    r"^test\w+\s+\([^)]+\)\s+\.\.\.\s+(?:ok|FAIL|ERROR|skipped)"  # verbose single-line
    r"|^Ran\s+\d+\s+tests?\s+in"  # Ran N tests in ...
    r"|^FAILED\s+\((?:failures|errors)="  # FAILED (failures=N, errors=M)
    r"|^OK\s*$"  # OK (Django success)
    r"|^OK\s+\(skipped="  # OK (skipped=N)
    r")",
    re.MULTILINE,
)

# Zope/ztest format: "Total: N tests, M failures, K errors..." or
# "Ran N tests with M failures, K errors..."
_ZOPE_OUTPUT_MARKERS = re.compile(
    r"(?:"
    r"Total:\s+\d+\s+tests?,\s+\d+\s+failures?"  # Total: N tests, M failures
    r"|Ran\s+\d+\s+tests?\s+with\s+\d+\s+failures?"  # Ran N tests with M failures
    r"|Tearing down left over layers"  # Zope layer teardown
    r"|Set up\s+\S+\s+in\s+[\d.]+"  # Set up Layer in N.NNN seconds
    r")",
    re.MULTILINE,
)

# Zope summary line regex for count extraction
_ZOPE_SUMMARY_RE = re.compile(
    r"(?:Total:|Ran)\s+(\d+)\s+tests?,?\s+"
    r"(?:with\s+)?(\d+)\s+failures?,?\s*"
    r"(\d+)\s+errors?",
)

_ZOPE_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class FallbackAdapter(TestRunnerAdapter):
    """Pass-through adapter that keeps the existing count-based logic.

    ``per_test`` is always empty so the sec decision falls back to
    count-only mode for the sec run.
    """
    runner_id = "fallback"

    def parse_session(
        self,
        run_logs: str,
        logs_parser: dict[str, str],
        timed_out: bool = False,
        logs_checker: str | None = None,
    ) -> SessionResult:
        # On this branch PREMATURE_ABORT_PATTERNS lives in env_specs.constants;
        # FAILURE_STATUSES/TestStatus are unused here, so they are not imported.
        from susvibes.env_specs.constants import PREMATURE_ABORT_PATTERNS

        if timed_out:
            return SessionResult(abort_reason=AbortReason.CRASH)

        if logs_checker and re.search(logs_checker, run_logs, re.MULTILINE):
            return SessionResult(abort_reason=AbortReason.BUILD_ERROR)

        if any(
            re.search(p, run_logs, re.MULTILINE)
            for p in PREMATURE_ABORT_PATTERNS
        ):
            return SessionResult(abort_reason=AbortReason.PREMATURE_ABORT)

        counts: dict[str, int] = {}
        any_match = False
        for status, pattern in logs_parser.items():
            if pattern:
                m = None
                for m in re.finditer(pattern, run_logs, re.MULTILINE):
                    pass
                if m:
                    counts[status] = int(m.group(1))
                    any_match = True
                else:
                    counts[status] = 0

        if not any_match:
            # Try Zope/ztest summary format before declaring CRASH
            clean_logs = _ZOPE_ANSI_RE.sub("", run_logs)
            zope_counts = _parse_zope_counts(clean_logs)
            if zope_counts is not None:
                return SessionResult(
                    abort_reason=AbortReason.NORMAL, per_test={},
                    counts=zope_counts,
                )
            return SessionResult(abort_reason=AbortReason.CRASH)

        return SessionResult(
            abort_reason=AbortReason.NORMAL, per_test={}, counts=counts
        )


def _parse_zope_counts(clean_logs: str) -> dict[str, int] | None:
    """Parse Zope/ztest summary format into counts dict.

    Handles both per-layer summaries and the final "Total:" line.
    Returns None if no Zope summary was found.
    """
    all_matches = _ZOPE_SUMMARY_RE.findall(clean_logs)
    if not all_matches:
        return None

    # Use the last match (which is typically the "Total:" line)
    total_tests, failures, errors = all_matches[-1]
    return {
        "FAILED": int(failures),
        "ERROR": int(errors),
        "PASSED": int(total_tests) - int(failures) - int(errors),
    }


def detect_runner(dockerfile: str) -> TestRunnerAdapter:
    """Pick an adapter based on the CMD in the Dockerfile."""
    cmd_matches = re.findall(r"CMD\s+(.*)", dockerfile)
    cmd = " ".join(cmd_matches).lower()

    if any(
        x in cmd
        for x in ["pytest", "py.test", "tox", "hatch", "inv test"]
    ):
        return PytestAdapter()

    if any(
        x in cmd
        for x in ["runtests.py", "manage.py test", "manage.py migrate"]
    ):
        return DjangoTestAdapter()

    # Additional heuristics on the full Dockerfile content
    dockerfile_lower = dockerfile.lower()

    # RUN pip install pytest / RUN pytest in Dockerfile body
    if re.search(r"(?:pip\s+install.*pytest|run\s+.*pytest)", dockerfile_lower):
        return PytestAdapter()

    # Script-based test invocations that use pytest under the hood
    if re.search(r"cmd\s+.*(?:script/test|scripts/test|make\s+test)", dockerfile_lower):
        # Ambiguous — could be pytest or custom. Check for pytest deps.
        if "pytest" in dockerfile_lower or "requirements-test" in dockerfile_lower:
            return PytestAdapter()

    return FallbackAdapter()


def detect_runner_from_output(
    dockerfile: str,
    run_logs: str,
    *,
    sample_size: int = 5000,
) -> TestRunnerAdapter:
    """Pick an adapter using both Dockerfile CMD and actual test output.

    First tries the Dockerfile-based detection. If it returns FallbackAdapter,
    inspects the test output for recognizable patterns to upgrade to a more
    specific adapter.
    """
    adapter = detect_runner(dockerfile)
    if adapter.runner_id != "fallback":
        return adapter

    # Sample the output (beginning + end) for pattern matching
    if len(run_logs) > sample_size * 2:
        sample = run_logs[:sample_size] + "\n" + run_logs[-sample_size:]
    else:
        sample = run_logs

    # Strip ANSI codes for cleaner matching
    clean_sample = re.sub(r"\x1b\[[0-9;]*m", "", sample)

    pytest_score = len(_PYTEST_OUTPUT_MARKERS.findall(clean_sample))
    django_score = len(_DJANGO_OUTPUT_MARKERS.findall(clean_sample))
    zope_score = len(_ZOPE_OUTPUT_MARKERS.findall(clean_sample))

    if pytest_score >= 3 and pytest_score > django_score:
        return PytestAdapter()
    if django_score >= 3 and django_score > pytest_score:
        return DjangoTestAdapter()

    # Lower threshold: even 1-2 strong pytest signals (node_id patterns)
    if pytest_score >= 1:
        return PytestAdapter()
    if django_score >= 1:
        return DjangoTestAdapter()

    # Zope/ztest detection — stays as FallbackAdapter but the Zope count
    # parsing in parse_session will now handle it correctly
    if zope_score >= 2:
        return FallbackAdapter()

    return FallbackAdapter()
