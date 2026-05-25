"""PytestAdapter — handles pytest, tox, hatch, and make-based test invocations.

Covers ~140 / 200 dataset instances.
"""

from __future__ import annotations

import re

from susvibes.env_specs.constants import PREMATURE_ABORT_PATTERNS
from susvibes.runners.base import (
    AbortReason,
    SessionResult,
    TestOutcome,
    TestRunnerAdapter,
)

_VERBOSE_LINE_RE = re.compile(
    r"^(.*?::.*?)\s+(?:\x1b\[[0-9;]*m)*(PASSED|FAILED|ERROR|SKIPPED|XFAIL)"
    r"(?:\x1b\[[0-9;]*m)*\s*(?:\[\s*\d+%\])?",
    re.MULTILINE,
)


class PytestAdapter(TestRunnerAdapter):
    runner_id = "pytest"

    def get_verbose_env(self) -> dict[str, str]:
        return {"PYTEST_ADDOPTS": "-v"}

    def parse_session(
        self,
        run_logs: str,
        logs_parser: dict[str, str],
        timed_out: bool = False,
        logs_checker: str | None = None,
    ) -> SessionResult:
        if timed_out:
            return SessionResult(abort_reason=AbortReason.CRASH)

        if logs_checker and re.search(logs_checker, run_logs, re.MULTILINE):
            return SessionResult(abort_reason=AbortReason.BUILD_ERROR)

        has_maxfail = any(
            re.search(p, run_logs, re.MULTILINE)
            for p in PREMATURE_ABORT_PATTERNS
        )

        per_test: dict[str, TestOutcome] = {}
        for m in _VERBOSE_LINE_RE.finditer(run_logs):
            node_id = m.group(1).strip()
            status = m.group(2)
            per_test[node_id] = TestOutcome(status)

        counts = _parse_counts(run_logs, logs_parser)

        if not counts and not per_test:
            abort = AbortReason.CRASH
        elif has_maxfail:
            abort = AbortReason.PREMATURE_ABORT
        else:
            abort = AbortReason.NORMAL

        return SessionResult(abort_reason=abort, per_test=per_test, counts=counts)

    def match_test(
        self, test_id: str, file_path: str, test_name: str
    ) -> bool:
        return test_id.endswith("::" + test_name) and file_path in test_id


def _parse_counts(run_logs: str, logs_parser: dict[str, str]) -> dict[str, int]:
    """Re-use the per-instance logs_parser regexes to extract summary counts.

    Returns an empty dict when no configured pattern matched anywhere,
    mirroring the ``None`` sentinel from ``Env.parse_test_logs``.
    """
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
        return {}
    return counts
