"""
Base contract for test-runner adapters.

Defines SessionResult (the structured object tasks.py consumes) and
TestRunnerAdapter (the interface each runner implements).

See docs/MULTI-LANGUAGE-RUNNER-PROPOSAL.md for the full design rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class TestOutcome(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    XFAIL = "XFAIL"


class AbortReason(str, Enum):
    NORMAL = "NORMAL"
    PREMATURE_ABORT = "PREMATURE_ABORT"
    BUILD_ERROR = "BUILD_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    CRASH = "CRASH"


FAILURE_OUTCOMES = frozenset({TestOutcome.FAILED, TestOutcome.ERROR})


@dataclass
class SessionResult:
    """Structured result of a single test-runner invocation.

    Adapters populate this; the pass rule in tasks.py consumes it.
    """
    abort_reason: AbortReason
    per_test: dict[str, TestOutcome] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def terminated_normally(self) -> bool:
        return self.abort_reason is AbortReason.NORMAL

    def visible_failures(self) -> int:
        return sum(self.counts.get(s.value, 0) for s in FAILURE_OUTCOMES)


class TestRunnerAdapter:
    """Base class for test-runner adapters.

    Subclass and override parse_session() for each runner family.
    extract_added_tests() is shared by all Python-based adapters;
    a future JUnit adapter would override it to parse @Test annotations.
    """
    runner_id: str = "base"

    def get_verbose_env(self) -> dict[str, str] | None:
        """Env vars to set on the container for verbose per-test output."""
        return None

    def get_verbose_command(self, image) -> list[str] | None:
        """Command override to inject verbose flags.

        Returns None to keep the image's original CMD.
        ``image`` is a docker.models.images.Image.
        """
        return None

    def parse_session(
        self,
        run_logs: str,
        logs_parser: dict[str, str],
        timed_out: bool = False,
        logs_checker: str | None = None,
    ) -> SessionResult:
        """Parse runner stdout/stderr into a SessionResult."""
        raise NotImplementedError

    def match_test(
        self, test_id: str, file_path: str, test_name: str
    ) -> bool:
        """True if *test_id* (from per_test) corresponds to the given
        (file_path, test_name) pair extracted from a test_patch diff."""
        return False

    @staticmethod
    def extract_added_tests(test_patch: str) -> list[tuple[str, str]]:
        """Extract (file_path, test_name) pairs added by a unified diff.

        Handles ``def test_snake``, ``def testCamel``, and ``async def``.
        Uses ``[ \\t]*`` (not ``\\s*``) so the regex cannot cross a
        newline from an added blank line into a context line.
        """
        result: list[tuple[str, str]] = []
        current_file: str | None = None
        for line in test_patch.split("\n"):
            m = re.match(r"^diff --git a/\S+ b/(\S+)", line)
            if m:
                current_file = m.group(1)
                continue
            m = re.match(r"^\+[ \t]*(?:async )?def (test\w+)", line)
            if m and current_file:
                result.append((current_file, m.group(1)))
        return result
