"""Mock harness implementations for testing the AgentHarness Protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from evaluation_harness.base import AgentHarness, AgentResult


class MockHarness:
    """Minimal ``AgentHarness``-satisfying class that replays canned data.

    Records the order of method calls so tests can assert the correct
    lifecycle sequence.  All heavy operations (Docker, agent invocation)
    are replaced with in-memory stubs.

    Reusable as the template for Phase 2 harness tests.
    """

    name: str = "mock_agent"

    def __init__(
        self,
        *,
        patch: str = "diff --git a/f.py b/f.py\n",
        stdout: str = "mock stdout",
        stderr: str = "",
        success: bool = True,
    ) -> None:
        self.local_workspace_dir: Path | None = None
        self.artifacts_dir: Path | None = None
        self._patch = patch
        self._stdout = stdout
        self._stderr = stderr
        self._success = success
        self.call_log: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def setup_workspace(self, instance_id: str, image_name: str) -> Path:
        self.call_log.append("setup_workspace")
        self.local_workspace_dir = Path(f"/tmp/mock_workspace_{instance_id}")
        self.artifacts_dir = self.local_workspace_dir / "artifacts"
        return self.local_workspace_dir

    def configure_tools(
        self, tool_config: Mapping[str, Any] | None = None
    ) -> None:
        self.call_log.append("configure_tools")

    def build_prompt(
        self, problem_statement: str, workspace_dir: str = "/project"
    ) -> str:
        self.call_log.append("build_prompt")
        return f"[mock prompt] {problem_statement[:80]}"

    def run_agent(
        self, prompt: str, env: Mapping[str, str] | None = None
    ) -> AgentResult:
        self.call_log.append("run_agent")
        return AgentResult(
            stdout=self._stdout,
            stderr=self._stderr,
            return_code=0 if self._success else 1,
            execution_time=1.23,
            success=self._success,
        )

    def extract_patch(self) -> str:
        self.call_log.append("extract_patch")
        return self._patch

    def get_agent_stdout(self) -> str:
        self.call_log.append("get_agent_stdout")
        return self._stdout

    def get_agent_stderr(self) -> str:
        self.call_log.append("get_agent_stderr")
        return self._stderr

    def cleanup(self) -> None:
        self.call_log.append("cleanup")

    def __enter__(self) -> MockHarness:
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()


class IncompleteHarness:
    """Deliberately missing several Protocol methods — used to test that
    ``isinstance(..., AgentHarness)`` correctly rejects it.
    """

    name: str = "incomplete"

    def setup_workspace(self, instance_id: str, image_name: str) -> Path:
        return Path("/tmp/incomplete")
