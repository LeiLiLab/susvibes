"""Agent harness interface — Protocol definitions and shared data types.

Defines the ``AgentHarness`` Protocol that every evaluation harness must
satisfy, plus the ``DockerHarness`` extension for container-based agents.
No existing code needs to inherit from these; Python's structural
(duck-typing) Protocol matching is sufficient.

``DockerHarnessBase`` is a concrete base class that factors out the Docker
container lifecycle shared by the Docker CLI agents (Claude Code, Gemini
CLI, ...).  Each agent's ``run_docker.py`` subclasses it and only supplies
the small per-agent differences.

See also: ``evaluation_harness/PROMPTS.md`` for the prompt architecture, and
``evaluation_harness/common.py`` for the canonical prompt template.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Protocol, TypedDict, runtime_checkable

from susvibes.core.utils import resolve_image_name


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


# ── Concrete Docker container lifecycle ───────────────────────────────


class DockerHarnessBase:
    """Shared Docker container lifecycle for Docker CLI agent harnesses.

    Factors out the workspace extraction, container start, command
    execution, CLI setup, and cleanup that were previously copy-pasted
    into each agent's ``DockerIntegration`` class.  Concrete agents
    subclass this and set:

    - ``name``: short agent id used to name the local workspace
      (``{name}_workspace_<ts>``) and the container (``{name}_work_<ts>``).
    - ``env_source_files``: files sourced at the start of every
      ``execute_in_container`` command (agent env, nvm, bashrc, ...).

    Method names and behavior mirror the original ``DockerIntegration``
    so existing batch runners keep working unchanged.
    """

    #: Short agent id; subclasses override.
    name: str = "agent"

    #: Files sourced before each in-container command; subclasses override.
    env_source_files: list[str] = ["/root/.nvm/nvm.sh", "/root/.bashrc"]

    def __init__(
        self,
        docker_image: str,
        container_work_dir: str = "/project",
        workspace_root: str = ".",
        keep_workspace: bool = True,
    ):
        """Initialize the Docker integration.

        Args:
            docker_image: Docker image with the embedded code.
            container_work_dir: Working directory inside the container.
            workspace_root: Root directory for local workspaces.
            keep_workspace: Keep the workspace directory after cleanup.
        """
        self.docker_image = resolve_image_name(docker_image)
        self.container_work_dir = container_work_dir
        self.workspace_container: str | None = None
        self.local_workspace_dir: Path | None = None
        self.workspace_root = workspace_root
        self.keep_workspace = keep_workspace

    def setup_persistent_workspace(self) -> Path:
        """Create a persistent container workspace with live volume sync."""
        print(f"🚀 Setting up persistent workspace from {self.docker_image}")

        self.local_workspace_dir = (
            Path(self.workspace_root).resolve()
            / f"{self.name}_workspace_{int(time.time())}"
        )
        self.local_workspace_dir.mkdir(exist_ok=True)

        try:
            self._extract_code_from_image()
            self._start_persistent_container()

            print("✅ Workspace ready!")
            print(f"📁 Local: {self.local_workspace_dir}")
            print(f"🐳 Container: {self.workspace_container}")

            return self.local_workspace_dir

        except Exception as e:
            print(f"❌ Workspace setup failed: {e}")
            self.cleanup()
            raise

    def _extract_code_from_image(self):
        """Extract initial code from the Docker image to the local workspace."""
        print("📦 Extracting initial code...")

        temp_container_name = f"temp_extract_{int(time.time())}_{id(self)}"

        subprocess.run(
            [
                "docker", "create", "--pull", "always",
                "--name", temp_container_name, self.docker_image,
            ],
            capture_output=True, text=True, check=True,
        )

        try:
            subprocess.run(
                [
                    "docker", "cp",
                    f"{temp_container_name}:{self.container_work_dir}/.",
                    str(self.local_workspace_dir),
                ],
                check=True,
            )
        finally:
            subprocess.run(
                ["docker", "rm", temp_container_name], capture_output=True
            )

    def _start_persistent_container(self):
        """Start a persistent container with a volume mount for live sync."""
        print("🐳 Starting persistent container with live sync...")

        container_name = f"{self.name}_work_{int(time.time())}"

        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                "--network", "host",
                "-v", f"{self.local_workspace_dir}:{self.container_work_dir}",
                "-w", self.container_work_dir,
                self.docker_image,
                "tail", "-f", "/dev/null",
            ],
            capture_output=True, text=True, check=True,
        )

        self.workspace_container = container_name
        print(f"✅ Container {container_name} started with live volume sync")

    def _env_source_block(self) -> str:
        """Build the shell block that sources the agent env files."""
        lines = ["\n"]
        for path in self.env_source_files:
            lines.append(f"        [ -f {path} ] && source {path}\n")
        return "".join(lines)

    def execute_in_container(self, command: str, env: dict = {}) -> dict:
        """Execute a command in the persistent container.

        Returns a dict with stdout, stderr, return_code, execution_time,
        command, and success.
        """
        if not self.workspace_container:
            raise RuntimeError("No persistent container available")

        start_time = time.time()

        full_command = self._env_source_block()
        print(f"🔧 Environment: {env}")
        if env:
            for key, value in env.items():
                full_command += f"export {key}={value}\n"

        print(f"🔧 Full command: {full_command}")

        full_command += f"{command}"

        exec_cmd = [
            "docker", "exec",
            "-w", self.container_work_dir,
            self.workspace_container,
            "bash", "-c", full_command,
        ]

        try:
            result = subprocess.run(
                exec_cmd, capture_output=True, text=True, timeout=3000
            )
            execution_time = time.time() - start_time

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
                "execution_time": execution_time,
                "command": command,
                "success": result.returncode == 0,
            }

        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Command timed out after 5 minutes",
                "return_code": -1,
                "execution_time": 300,
                "command": command,
                "success": False,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
                "execution_time": time.time() - start_time,
                "command": command,
                "success": False,
            }

    def setup_cli_env(self, setup_script_path: str = "setup-env.sh") -> dict:
        """Copy and run the CLI setup script inside the container."""
        if not self.workspace_container:
            raise RuntimeError(
                "No persistent container available. "
                "Call setup_persistent_workspace() first."
            )

        print(f"🔧 Setting up environment using {setup_script_path}...")

        setup_file = Path("./") / setup_script_path
        if not setup_file.exists():
            return {
                "stdout": "",
                "stderr": f"Setup script not found: {setup_file}",
                "return_code": 1,
                "success": False,
                "command": f"setup from {setup_script_path}",
            }

        try:
            container_setup_path = (
                f"{self.container_work_dir}/{setup_script_path}"
            )

            subprocess.run(["chmod", "+x", str(setup_file)], check=True)

            copy_result = subprocess.run(
                [
                    "docker", "cp",
                    str(setup_file),
                    f"{self.workspace_container}:{container_setup_path}",
                ],
                capture_output=True, text=True,
            )

            if copy_result.returncode != 0:
                return {
                    "stdout": "",
                    "stderr": (
                        "Failed to copy setup script to container: "
                        f"{copy_result.stderr}"
                    ),
                    "return_code": copy_result.returncode,
                    "success": False,
                    "command": f"copy {setup_script_path} to container",
                }

            chmod_result = self.execute_in_container(
                f"chmod +x {container_setup_path}"
            )
            if not chmod_result["success"]:
                print(
                    "⚠️  Warning: Could not make script executable: "
                    f"{chmod_result['stderr']}"
                )

            print("🚀 Running setup script...")
            setup_result = self.execute_in_container(
                f"bash {container_setup_path}"
            )

            if setup_result["success"]:
                print("✅ Environment setup completed successfully!")
                print(
                    f"⏱️  Execution time: {setup_result['execution_time']:.2f}s"
                )

                if setup_result["stdout"]:
                    stdout_lines = setup_result["stdout"].strip().split("\n")
                    if len(stdout_lines) > 10:
                        print("📋 Setup output (last 10 lines):")
                        for line in stdout_lines[-10:]:
                            print(f"   {line}")
                    else:
                        print("📋 Setup output:")
                        print(setup_result["stdout"])
            else:
                print("❌ Environment setup failed!")
                print(f"Error: {setup_result['stderr']}")
                if setup_result["stdout"]:
                    print(f"Output: {setup_result['stdout']}")

            return setup_result

        except subprocess.CalledProcessError as e:
            error_msg = (
                "Docker command failed: "
                f"{e.stderr if hasattr(e, 'stderr') else str(e)}"
            )
            print(f"❌ {error_msg}")
            return {
                "stdout": e.stdout if hasattr(e, "stdout") else "",
                "stderr": error_msg,
                "return_code": e.returncode,
                "success": False,
                "command": f"setup from {setup_script_path}",
                "execution_time": 0,
            }
        except Exception as e:
            error_msg = f"Setup failed: {str(e)}"
            print(f"❌ {error_msg}")
            return {
                "stdout": "",
                "stderr": error_msg,
                "return_code": -1,
                "success": False,
                "command": f"setup from {setup_script_path}",
                "execution_time": 0,
            }

    def cleanup(self):
        """Stop and remove the container; optionally preserve the workspace."""
        print("🧹 Cleaning up...")

        if self.workspace_container:
            try:
                subprocess.run(
                    ["docker", "stop", self.workspace_container],
                    capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["docker", "rm", self.workspace_container],
                    capture_output=True,
                )
                print(f"✅ Removed container: {self.workspace_container}")
            except Exception as e:
                print(f"⚠️  Container cleanup issue: {e}")

        if self.local_workspace_dir and self.local_workspace_dir.exists():
            try:
                if self.keep_workspace:
                    print(f"📁 Workspace preserved at: {self.local_workspace_dir}")
                    print("   (Delete manually if no longer needed)")
                else:
                    shutil.rmtree(self.local_workspace_dir)
                    print(f"🗑️  Workspace deleted: {self.local_workspace_dir}")
            except Exception as e:
                print(f"⚠️  Workspace cleanup issue: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
