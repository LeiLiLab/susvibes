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

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_VERBOSE_LINE_RE = re.compile(
    r"^(.*?::.*?)\s+(?:\x1b\[[0-9;]*m)*(PASSED|FAILED|ERROR|SKIPPED|XFAIL)"
    r"(?:\x1b\[[0-9;]*m)*\s*(?:\[\s*\d+%\])?",
    re.MULTILINE,
)

# Universal pytest summary line — works for -q (bare), default, and -v/vv
# (=== decorated) output styles.  Applied to ANSI-stripped text.
_PYTEST_SUMMARY_RE = re.compile(
    r"^[= ]*(\d+\s+(?:failed|passed)(?:,\s+\d+\s+\w+)*\s+in\s+[\d.]+s(?:\s*\([^)]*\))?)\s*=*$",
    re.MULTILINE,
)
_PYTEST_COUNT_TOKEN_RE = re.compile(r"(\d+)\s+(failed|passed|skipped|errors?|xfailed)")


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

        _COUNT_KEYS = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL")
        total_from_counts = sum(counts.get(s, 0) for s in _COUNT_KEYS)
        if per_test and counts and total_from_counts > 0:
            coverage = len(per_test) / total_from_counts
            if coverage < 0.5:
                per_test = {}

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
        if file_path not in test_id:
            return False
        suffix = "::" + test_name
        if test_id.endswith(suffix):
            return True
        # pytest.mark.parametrize: "::test_name[param]"
        if (suffix + "[") in test_id:
            return True
        # parameterized.expand: "::test_name_0__desc" or "::test_name_1"
        idx = test_id.find(suffix + "_")
        if idx != -1:
            rest = test_id[idx + len(suffix) + 1:]
            if rest and rest[0].isdigit():
                return True
        return False


def _parse_counts(run_logs: str, logs_parser: dict[str, str]) -> dict[str, int]:
    """Re-use the per-instance logs_parser regexes to extract summary counts.

    Falls back to a universal pytest summary regex when the curated
    ``logs_parser`` patterns produce no matches (e.g. ``-q``-style regexes
    running against ``===``-decorated output produced by the adapter's
    ``PYTEST_ADDOPTS="-v"`` injection).

    Returns an empty dict when nothing matched at all.
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

    # Fallback: parse the standard pytest summary line directly.
    # Strip ANSI codes first — some instances emit colored summary lines.
    fb: dict[str, int] = {}
    clean_logs = _ANSI_RE.sub("", run_logs)
    m = _PYTEST_SUMMARY_RE.search(clean_logs)
    if m:
        summary = m.group(1)
        for tok in _PYTEST_COUNT_TOKEN_RE.finditer(summary):
            num = int(tok.group(1))
            kind = tok.group(2).upper()
            if kind.startswith("ERROR"):
                kind = "ERROR"
            fb[kind] = num

    if not any_match:
        return fb or {}

    # Patch up zero-valued curated keys with fallback values when the
    # fallback found a non-zero count — handles mismatched regexes that
    # only partially match the summary line format.
    for key, val in fb.items():
        if val > 0 and counts.get(key, 0) == 0:
            counts[key] = val
    return counts
