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
    r"^(.*?::.*?)[ \t]+(?:\x1b\[[0-9;]*m)*(PASSED|FAILED|ERROR|SKIPPED|XFAIL)"
    r"(?:\x1b\[[0-9;]*m)*\s*(?:\[\s*\d+%\])?",
    re.MULTILINE,
)

# pytest-xdist parallel output: "[gwN] [NN%] STATUS node_id"
_XDIST_LINE_RE = re.compile(
    r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
    r"(?:\x1b\[[0-9;]*m)*(PASSED|FAILED|ERROR|SKIPPED|XFAIL)(?:\x1b\[[0-9;]*m)*"
    r"\s+(\S+::\S+)",
    re.MULTILINE,
)

# "short test summary info" lines emitted by pytest for every non-passing
# test, even in -q mode.  Format (after ANSI stripping):
#   FAILED path/to/test.py::TestClass::test_name[param] - reason
_SHORT_SUMMARY_RE = re.compile(
    r"^(FAILED|ERROR)\s+(.*?::\S+)",
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

    def extract_per_test(self, run_logs: str) -> dict[str, TestOutcome]:
        per_test: dict[str, TestOutcome] = {}
        for m in _VERBOSE_LINE_RE.finditer(run_logs):
            node_id = m.group(1).strip()
            status = m.group(2)
            per_test[node_id] = TestOutcome(status)

        # pytest-xdist parallel format: "[gwN] [NN%] STATUS node_id"
        for m in _XDIST_LINE_RE.finditer(run_logs):
            status = m.group(1)
            node_id = m.group(2).strip()
            if node_id not in per_test:
                per_test[node_id] = TestOutcome(status)

        clean_logs = _ANSI_RE.sub("", run_logs)
        for m in _SHORT_SUMMARY_RE.finditer(clean_logs):
            status = m.group(1)   # FAILED or ERROR
            node_id = m.group(2).strip()
            if node_id not in per_test:
                per_test[node_id] = TestOutcome(status)

        return per_test

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

        # Reuse extract_per_test instead of duplicating the verbose/xdist/short
        # parsing inline (reference repeated it here).
        per_test = self.extract_per_test(run_logs)
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
        if not self._file_path_matches(test_id, file_path):
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

    @staticmethod
    def _file_path_matches(test_id: str, file_path: str) -> bool:
        """Check if file_path matches the file portion of a pytest node ID.

        Handles the common case where the diff path includes a directory
        prefix (e.g. ``tests/test_foo.py``) that pytest strips when running
        from within that directory (emitting ``test_foo.py::test_bar``).
        """
        if file_path in test_id:
            return True
        # Try suffix matching: check if the pytest node path ends with
        # the same basename or a tail portion of file_path
        node_path = test_id.split("::")[0]
        if node_path.endswith(file_path):
            return True
        if file_path.endswith(node_path):
            return True
        # Check if they share a common suffix (directory removed from one side)
        parts = file_path.split("/")
        for i in range(1, len(parts)):
            tail = "/".join(parts[i:])
            if tail in test_id:
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
    matched_keys: set[str] = set()
    for status, pattern in logs_parser.items():
        if pattern:
            m = None
            for m in re.finditer(pattern, run_logs, re.MULTILINE):
                pass
            if m:
                counts[status] = int(m.group(1))
                matched_keys.add(status)
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

    if not matched_keys:
        return fb or {}

    # Patch up keys where the curated regex found no match (as opposed
    # to matching and capturing 0) but the fallback found a positive
    # value.  This handles partially-broken curated regexes that match
    # some tokens (e.g. PASSED) but not others (e.g. FAILED).
    for key, val in fb.items():
        if key not in matched_keys and val > 0:
            counts[key] = val
    return counts
