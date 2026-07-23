"""Tests for the canonical prompt template in evaluation_harness/common.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from evaluation_harness.common import (
    _INSTANCE_TEMPLATE_BODY,
    get_instance_template,
)

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "evaluation_harness"

# ── Template content tests ────────────────────────────────────────────


def test_template_has_anti_cheating_block() -> None:
    assert "Anti-cheating requirements:" in _INSTANCE_TEMPLATE_BODY
    assert "git log" in _INSTANCE_TEMPLATE_BODY
    assert "strictly prohibited" in _INSTANCE_TEMPLATE_BODY


def test_template_has_six_step_workflow() -> None:
    for step in range(1, 7):
        assert f"{step}." in _INSTANCE_TEMPLATE_BODY


def test_template_has_neutral_markers() -> None:
    assert "__WORK_DIR__" in _INSTANCE_TEMPLATE_BODY
    assert "__PROBLEM_STATEMENT__" in _INSTANCE_TEMPLATE_BODY


# ── get_instance_template tests ───────────────────────────────────────


def test_placeholder_substitution() -> None:
    tpl = get_instance_template("{my_dir}", "{my_problem}")
    assert "{my_dir}" in tpl
    assert "{my_problem}" in tpl
    assert "__WORK_DIR__" not in tpl
    assert "__PROBLEM_STATEMENT__" not in tpl


def test_format_with_real_problem(problem_statements: dict) -> None:
    tpl = get_instance_template("{local_work_dir}", "{problem_statement}")
    result = tpl.format(
        local_work_dir="/project",
        problem_statement=problem_statements["plain"],
    )
    assert "/project" in result
    assert "Anti-cheating" in result


def test_claude_and_gemini_templates_identical() -> None:
    """Both Docker CLI agents should get byte-identical templates."""
    added: list[str] = []
    templates: dict[str, str] = {}

    for agent_dir in ("claude_code", "gemini_cli"):
        agent_path = str(HARNESS_ROOT / agent_dir)
        parent_path = str(HARNESS_ROOT)
        for p in (agent_path, parent_path):
            if p not in sys.path:
                sys.path.insert(0, p)
                added.append(p)
        if "prompts" in sys.modules:
            del sys.modules["prompts"]

        import prompts  # noqa: E402

        templates[agent_dir] = prompts.USER_PROMPT_TEMPLATE
        del sys.modules["prompts"]

    for p in added:
        if p in sys.path:
            sys.path.remove(p)

    assert templates["claude_code"] == templates["gemini_cli"]


def test_tools_raises_import_error() -> None:
    with pytest.raises(ImportError, match="evaluation_harness.tools"):
        get_instance_template("{d}", "{p}", tools=["nonexistent_tool"])
