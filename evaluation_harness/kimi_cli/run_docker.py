from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from dotenv import load_dotenv

script_dir = Path(__file__).resolve().parent
harness_root = script_dir.parent
repo_root = harness_root.parent

for p in (str(repo_root), str(harness_root), str(script_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from base import DockerHarnessBase  # noqa: E402
from prompts import USER_PROMPT_TEMPLATE, load_example_instance  # noqa: E402

load_dotenv()


ALLOWED_TOOLS = [
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "LS",
]


class DockerIntegration(DockerHarnessBase):
    """Kimi CLI Docker integration.

    Shares the full container lifecycle with ``DockerHarnessBase`` and only
    customizes the agent name (workspace/container prefix) and the env files
    sourced before each in-container command.
    """

    name = "kimi"
    env_source_files = [
        "/root/.kimi/.env",
        "/root/.cargo/env",
        "/root/.bashrc",
    ]


def main():
    instance = load_example_instance()
    TASK = instance["problem_statement"]
    DOCKER_IMAGE = instance["image_name"]

    env = {}
    env["KIMI_MODEL"] = os.environ.get("KIMI_MODEL", "kimi-k2-turbo-preview")
    env["MOONSHOT_API_KEY"] = os.environ.get("MOONSHOT_API_KEY", os.environ.get("KIMI_API_KEY", ""))
    env["KIMI_API_KEY"] = env["MOONSHOT_API_KEY"]

    print(f"🔧 Environment: {env}")

    prompt = USER_PROMPT_TEMPLATE.format(local_work_dir="/project", problem_statement=TASK)
    escaped_instruction = shlex.quote(prompt)

    with DockerIntegration(DOCKER_IMAGE, container_work_dir="/project", workspace_root=".", keep_workspace=True) as integration:
        # Set up the workspace
        workspace = integration.setup_persistent_workspace()

        integration.setup_cli_env()

        print("🔧 Setting up environment...")
        print("🔧 Running Kimi CLI...")
        print(f"🔧 Allowed Tools: {' '.join(ALLOWED_TOOLS)}")
        print(f"🔧 Task: {TASK}")
        kimi_command = (
            "kimi --output-format stream-json "
            f"-p {escaped_instruction} --yolo"
        )
        print(f"🔧 Command: {kimi_command}")

        result = integration.execute_in_container(kimi_command, env=env)
        print(f"🔧 Result: {result}")

        print("\n🎉 Session complete!")
        print(f"📁 Your improved code is at: {workspace}")

        return workspace


if __name__ == "__main__":
    main()
