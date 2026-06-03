"""DjangoTestAdapter — handles Django's runtests.py and manage.py test.

Covers ~33 / 200 dataset instances.
"""

from __future__ import annotations

import re

from susvibes.runners.base import (
    AbortReason,
    SessionResult,
    TestOutcome,
    TestRunnerAdapter,
)

_DJANGO_VERBOSE_RE = re.compile(
    r"^(test\w+)\s+\([^)]+\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)",
    re.MULTILINE,
)

_STATUS_MAP = {
    "ok": TestOutcome.PASSED,
    "FAIL": TestOutcome.FAILED,
    "ERROR": TestOutcome.ERROR,
    "skipped": TestOutcome.SKIPPED,
}


class DjangoTestAdapter(TestRunnerAdapter):
    runner_id = "django"

    def extract_per_test(self, run_logs: str) -> dict[str, TestOutcome]:
        per_test: dict[str, TestOutcome] = {}
        for m in _DJANGO_VERBOSE_RE.finditer(run_logs):
            name, status = m.group(1), m.group(2)
            per_test[name] = _STATUS_MAP[status]
        return per_test

    def get_verbose_command(self, image) -> list[str] | None:
        cmd = image.attrs["Config"]["Cmd"]
        if cmd is None:
            return None

        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if any(
            x in cmd_str
            for x in ["-v2", "-v 2", "--verbosity=2", "--verbosity 2"]
        ):
            return None  # already verbose

        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["sh", "-c"]:
            return ["sh", "-c", cmd[2] + " --verbosity=2"]
        if isinstance(cmd, list):
            return cmd + ["--verbosity=2"]
        return None

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

        per_test: dict[str, TestOutcome] = {}
        for m in _DJANGO_VERBOSE_RE.finditer(run_logs):
            name, status = m.group(1), m.group(2)
            per_test[name] = _STATUS_MAP[status]

        counts = _parse_django_counts(run_logs, logs_parser)

        if not counts and not per_test:
            abort = AbortReason.CRASH
        else:
            abort = AbortReason.NORMAL

        return SessionResult(abort_reason=abort, per_test=per_test, counts=counts)

    def match_test(
        self, test_id: str, file_path: str, test_name: str
    ) -> bool:
        return test_id == test_name


def _parse_django_counts(
    run_logs: str, logs_parser: dict[str, str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status, pattern in logs_parser.items():
        if pattern:
            m = None
            for m in re.finditer(pattern, run_logs, re.MULTILINE):
                pass
            if m:
                counts[status] = int(m.group(1))
            else:
                counts[status] = 0
    return counts
