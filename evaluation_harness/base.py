"""Agent harness interface — Protocol definitions and shared data types.

Defines the ``AgentHarness`` Protocol that every evaluation harness must
satisfy, plus the ``DockerHarness`` extension for container-based agents.
No existing code needs to inherit from these; Python's structural
(duck-typing) Protocol matching is sufficient.

See also: ``evaluation_harness/PROMPTS.md`` for the prompt architecture, and
``evaluation_harness/common.py`` for the canonical prompt template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, TypedDict, runtime_checkable


# ── Data types ────────────────────────────────────────────────────────


class AgentResult(TypedDict, total=False):
    """Result of a single agent invocation."""

    stdout: str
    stderr: str
    return_code: int
    execution_time: float
    success: bool


class PredictionRecord(TypedDict, total=False):
    """Standardized prediction output for cross-agent tooling.

    Keys use normalized names (``agent_stdout``, not ``claude_stdout``).
    """

    instance_id: str
    model_name_or_path: str
    model_patch: str
    agent_stdout: str
    agent_stderr: str
    agent_success: bool
    workspace: str
    artifacts_dir: str
    execution_time: float
    error: str


# ── Protocols ─────────────────────────────────────────────────────────


@runtime_checkable
class AgentHarness(Protocol):
    """Contract every agent evaluation harness must satisfy.

    Covers the full lifecycle: workspace setup, tool configuration,
    prompt construction, agent execution, patch extraction, and cleanup.
    """

    name: str
    local_workspace_dir: Path | None
    artifacts_dir: Path | None

    def setup_workspace(self, instance_id: str, image_name: str) -> Path:
        """Prepare a ready-to-run workspace from a Docker image.

        Implementations MUST:
        - Extract code from the image to a local directory
        - Sanitize workspace (orphan branch + git guard) to prevent
          agents from inspecting upstream git history
        - Prepare the agent runtime environment (CLI tools, container, etc.)

        Should delegate to existing sanitization APIs when available.
        After this method returns, the workspace must be ready for
        ``run_agent()`` to execute immediately.
        """
        ...

    def configure_tools(
        self, tool_config: Mapping[str, Any] | None = None
    ) -> None:
        """Set up external tool access (MCP servers, CLI flags, etc.).

        Called after ``setup_workspace()`` and before
        ``build_prompt()`` / ``run_agent()``.  Default is a no-op.
        Agents with tool integrations override this to install MCP
        server configs, set CLI flags, or start sidecar processes.
        ``tool_config`` is agent-specific; callers pass the relevant
        section from the pipeline config.
        """
        ...

    def build_prompt(
        self, problem_statement: str, workspace_dir: str = "/project"
    ) -> str:
        """Assemble the full prompt for a task instance.

        The default implementation should use the canonical template from
        ``common.py`` (``get_instance_template``), insert
        *problem_statement* (applying ``apply_safety_hint`` only if the
        dataset was not already prepared with a strategy), and prepend
        tool instructions if tools are configured.

        Agents SHOULD use the canonical template.  Agents that need
        different formatting (e.g. Jinja2, YAML config injection) MAY
        override but MUST preserve the same semantic content: workspace
        path, anti-cheating block, workflow guidance, safety hint.
        """
        ...

    def run_agent(
        self, prompt: str, env: Mapping[str, str] | None = None
    ) -> AgentResult:
        """Execute the agent on a task.  Returns execution result.

        How the agent is invoked (container CLI, host subprocess,
        framework API) is an implementation detail.
        """
        ...

    def extract_patch(self) -> str:
        """Return the model's diff after agent execution.

        Typically a ``git diff`` from the workspace, but implementations
        may obtain the patch differently (e.g. from agent output or
        framework runtime).
        """
        ...

    def get_agent_stdout(self) -> str:
        """Normalized agent output, extracted from ``artifacts_dir``.

        CLI harnesses read the raw stdout dump they saved during
        ``run_agent()``.  Framework harnesses parse their on-disk
        artifacts (e.g. render a SWE-agent ``.traj`` into a normalized
        transcript).  The internal layout of ``artifacts_dir`` is an
        implementation detail; this method is the contract for reading
        output back.
        """
        ...

    def get_agent_stderr(self) -> str:
        """Normalized agent error output, extracted from ``artifacts_dir``.

        Same contract as ``get_agent_stdout()``, for the error channel
        (raw stderr dump, or error events from framework artifacts).
        """
        ...

    def cleanup(self) -> None:
        """Release resources.  Optionally preserve workspace for debugging."""
        ...

    def __enter__(self) -> AgentHarness:
        ...

    def __exit__(self, *args: object) -> None:
        ...


@runtime_checkable
class DockerHarness(AgentHarness, Protocol):
    """Extension of ``AgentHarness`` for Docker-container-based agents.

    Adds container-specific attributes and methods that don't belong in
    the top-level protocol (e.g. Codex runs on the host, SWE-agent uses
    ``sweagent run-batch``, OpenHands uses ``run_controller()``).
    """

    workspace_container: str | None

    def execute_in_container(
        self, command: str, env: Mapping[str, str] | None = None
    ) -> AgentResult:
        """Run a shell command inside the agent's Docker container."""
        ...

    def setup_cli_env(self, setup_script_path: str | Path | None = None) -> None:
        """Install the agent CLI inside the container."""
        ...


# ── Helpers ───────────────────────────────────────────────────────────

# Keys produced by current per-agent batch runners that should map to
# the normalized PredictionRecord fields.
_AGENT_PREFIXED_KEYS: dict[str, str] = {
    "stdout": "agent_stdout",
    "stderr": "agent_stderr",
    "success": "agent_success",
}

# Keys that pass through unchanged.
_PASSTHROUGH_KEYS = frozenset(
    PredictionRecord.__annotations__.keys()
) - {"agent_stdout", "agent_stderr", "agent_success"}


def normalize_prediction(
    record: Mapping[str, Any],
    agent_prefix: str,
) -> PredictionRecord:
    """Convert a legacy agent-prefixed result dict to a ``PredictionRecord``.

    Today's batch runners produce keys like ``claude_stdout``,
    ``cursor_stderr``, ``codex_success``.  This helper maps them to
    normalized names so cross-agent tooling can consume a uniform schema.

    >>> normalize_prediction({"claude_stdout": "hi", "instance_id": "x"}, "claude")
    {'agent_stdout': 'hi', 'instance_id': 'x'}
    """
    out: dict[str, Any] = {}
    for old_suffix, new_key in _AGENT_PREFIXED_KEYS.items():
        prefixed = f"{agent_prefix}_{old_suffix}"
        if prefixed in record:
            out[new_key] = record[prefixed]
    for key in _PASSTHROUGH_KEYS:
        if key in record:
            out[key] = record[key]
    return PredictionRecord(**out)  # type: ignore[typeddict-item]
