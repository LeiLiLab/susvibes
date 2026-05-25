"""Runner registry and FallbackAdapter.

detect_runner() inspects the Dockerfile CMD to pick the right adapter.
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

    return FallbackAdapter()
