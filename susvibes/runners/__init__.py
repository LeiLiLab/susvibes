"""Runner registry and FallbackAdapter.

detect_runner() inspects the Dockerfile CMD to pick the right adapter.
When the CMD is ambiguous, detect_runner_from_output() can refine the
choice based on actual test output content.

FallbackAdapter wraps the original check_test_logs + parse_test_logs
path for instances whose test runner is unrecognized.
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


class FallbackAdapter(TestRunnerAdapter):
    """Pass-through adapter that keeps the existing count-based logic.

    ``per_test`` is always empty so ``_decide_pass`` falls back to
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
        from susvibes.env_specs.constants import (
            FAILURE_STATUSES,
            PREMATURE_ABORT_PATTERNS,
            TestStatus,
        )

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
            return SessionResult(abort_reason=AbortReason.CRASH)

        return SessionResult(
            abort_reason=AbortReason.NORMAL, per_test={}, counts=counts
        )


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

    if pytest_score >= 3 and pytest_score > django_score:
        return PytestAdapter()
    if django_score >= 3 and django_score > pytest_score:
        return DjangoTestAdapter()

    # Lower threshold: even 1-2 strong pytest signals (node_id patterns)
    if pytest_score >= 1:
        return PytestAdapter()
    if django_score >= 1:
        return DjangoTestAdapter()

    return FallbackAdapter()
