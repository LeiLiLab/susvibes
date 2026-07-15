"""Tests for the AgentHarness Protocol and mock harness lifecycle."""

from __future__ import annotations

from evaluation_harness.base import AgentHarness, PredictionRecord
from tests.mocks import IncompleteHarness, MockHarness


def test_mock_harness_satisfies_protocol() -> None:
    harness = MockHarness()
    assert isinstance(harness, AgentHarness)


def test_incomplete_harness_rejected() -> None:
    harness = IncompleteHarness()
    assert not isinstance(harness, AgentHarness)


def test_lifecycle_call_order() -> None:
    """The RFC's typical caller code should invoke methods in the right
    sequence and produce a well-formed PredictionRecord."""
    with MockHarness(patch="diff --git a/x.py b/x.py\n+fix\n") as harness:
        workspace = harness.setup_workspace("test-001", "img:latest")
        harness.configure_tools(None)
        prompt = harness.build_prompt("Fix the bug", str(workspace))
        result = harness.run_agent(prompt)
        patch = harness.extract_patch()

        record = PredictionRecord(
            instance_id="test-001",
            model_name_or_path="mock-model",
            model_patch=patch,
            agent_stdout=harness.get_agent_stdout(),
            agent_stderr=harness.get_agent_stderr(),
            agent_success=result.get("success", False),
            artifacts_dir=str(harness.artifacts_dir),
        )

    expected_order = [
        "setup_workspace",
        "configure_tools",
        "build_prompt",
        "run_agent",
        "extract_patch",
        "get_agent_stdout",
        "get_agent_stderr",
        "cleanup",
    ]
    assert harness.call_log == expected_order

    assert record["instance_id"] == "test-001"
    assert record["model_patch"].startswith("diff --git")
    assert record["agent_stdout"] == "mock stdout"
    assert record["agent_success"] is True


def test_mock_harness_failure_mode() -> None:
    harness = MockHarness(success=False, stderr="error: segfault")
    result = harness.run_agent("prompt")
    assert result["success"] is False
    assert result["return_code"] != 0
    assert harness.get_agent_stderr() == "error: segfault"
