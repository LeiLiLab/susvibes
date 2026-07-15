"""Tests for apply_safety_hint and its coherence with the strategy pipeline."""

from __future__ import annotations

from evaluation_harness.common import GENERIC_SAFETY_HINT, apply_safety_hint
from susvibes.eval.strategies.prompts import GENERIC_PROMPT


def test_hint_is_strategy_generic_prompt() -> None:
    """GENERIC_SAFETY_HINT is a direct re-export of GENERIC_PROMPT."""
    assert GENERIC_SAFETY_HINT is GENERIC_PROMPT


def test_hint_applied_to_plain_statement(problem_statements: dict) -> None:
    plain = problem_statements["plain"]
    result = apply_safety_hint(plain)
    assert GENERIC_SAFETY_HINT in result
    assert result.startswith(plain)


def test_hint_format_matches_strategy_pipeline() -> None:
    """The join format must match apply_safety_strategy's pattern:
    ``f"{problem_statement} \\n\\n---\\n {hint}"``
    """
    ps = "Some bug description."
    result = apply_safety_hint(ps)
    expected = f"{ps} \n\n---\n {GENERIC_SAFETY_HINT}"
    assert result == expected


def test_idempotent_no_double_apply(problem_statements: dict) -> None:
    """When the dataset was prepared with --strategy generic, the hint is
    already in the problem statement.  apply_safety_hint must not add it
    again."""
    prepared = problem_statements["with_generic_strategy"]
    assert GENERIC_SAFETY_HINT in prepared
    result = apply_safety_hint(prepared)
    assert result == prepared
    assert result.count(GENERIC_SAFETY_HINT) == 1


def test_idempotent_after_own_application() -> None:
    ps = "Fix the XSS vulnerability."
    once = apply_safety_hint(ps)
    twice = apply_safety_hint(once)
    assert once == twice
