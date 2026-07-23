from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from base import DockerHarnessBase  # noqa: E402

from prompts import USER_PROMPT_TEMPLATE, load_example_instance

load_dotenv()


ALLOWED_TOOLS = [
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "NotebookEdit",
    "NotebookRead",
    "TodoRead",
    "TodoWrite",
    "Agent",
]


class DockerIntegration(DockerHarnessBase):
    """Claude Code Docker integration.

    Shares the full container lifecycle with ``DockerHarnessBase`` and only
    customizes the agent name (workspace/container prefix) and the env files
    sourced before each in-container command.
    """

    name = "claude"
    env_source_files = [
        "/root/.claude_env",
        "/root/.nvm/nvm.sh",
        "/root/.bashrc",
    ]


def main():

    instance = load_example_instance()
    TASK = instance["problem_statement"]
    DOCKER_IMAGE = instance["image_name"]

    env = {}
    env["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "")
    env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL", "")
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", 50000)
    print(f"🔧 Environment: {env}")

    prompt = USER_PROMPT_TEMPLATE.format(local_work_dir="/project", problem_statement=TASK)
    escaped_instruction = shlex.quote(prompt)

    with DockerIntegration(DOCKER_IMAGE, container_work_dir="/project", workspace_root=".", keep_workspace=True) as integration:
        # Set up the workspace
        workspace = integration.setup_persistent_workspace()

        integration.setup_cli_env()

        print(f"🔧 Setting up environment...")

        print(f"🔧 Running Claude...")
        print(f"🔧 Allowed Tools: {' '.join(ALLOWED_TOOLS)}")
        print(f"🔧 Task: {TASK}")
        claude_command = (
            "claude --verbose --output-format stream-json "
            f"-p {escaped_instruction} --allowedTools {' '.join(ALLOWED_TOOLS)}"
        )

        result = integration.execute_in_container(claude_command, env=env)
        print(f"🔧 Result: {result}")

        print(f"\n🎉 Session complete!")
        print(f"📁 Your improved code is at: {workspace}")

        return workspace


if __name__ == "__main__":
    main()
