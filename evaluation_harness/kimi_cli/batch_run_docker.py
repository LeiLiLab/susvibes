#!/usr/bin/env python3
"""
Docker Batch Runner for Multiple Tasks with Kimi CLI Execution

This script reads instances from a JSONL file, processes each one
with Docker-based Kimi execution, and saves the git diff results to JSON files.
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

script_dir = Path(__file__).resolve().parent
harness_root = script_dir.parent
repo_root = harness_root.parent

for p in (str(repo_root), str(harness_root), str(script_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from run_docker import ALLOWED_TOOLS, USER_PROMPT_TEMPLATE, DockerIntegration  # noqa: E402
from common import apply_safety_hint  # noqa: E402

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
    handlers=[
        logging.FileHandler('docker_batch_run.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_options():
    parser = argparse.ArgumentParser(description='Process multiple tasks with Docker-based Kimi CLI execution')
    parser.add_argument('--jsonl_file', type=str, default='../../datasets/default/susvibes_dataset.jsonl', help='Path to the JSONL file')
    parser.add_argument('--results_dir', type=str, default='results', help='Path to the results directory')
    parser.add_argument('--workspace_root', type=str, default='logs/workspace', help='Path to the workspace root directory')
    parser.add_argument('--start_idx', type=int, default=0, help='Start index of the instances to process')
    parser.add_argument('--num_instances', type=int, default=2, help='Number of instances to process')
    parser.add_argument('--load_from_file', type=str, default=None, help='Path to the file to load from')
    parser.add_argument('--model', type=str, default="kimi-k2-turbo-preview", help='Model to use')
    parser.add_argument('--setup_script', type=str, default="setup-env.sh", help='Setup script to run in container')
    parser.add_argument('--timestamp_suffix', type=str, default=None, help='Suffix to append to timestamp for unique directory names (for parallel runs)')
    parser.add_argument('--keep_workspace', action='store_true', default=False, help='Whether to keep the workspace after cleanup')
    parser.add_argument('--strategy', type=str, default='none', choices=['none', 'generic'],
                        help='Safety strategy applied to the problem statement (none = no hint)')
    return parser


def simple_git_diff(dirpath: str) -> str:
    """Get git diff from the specified directory."""
    original_cwd = os.getcwd()
    try:
        os.chdir(dirpath)
        result = subprocess.run(['git', 'diff'], capture_output=True, text=True)
        return result.stdout
    finally:
        os.chdir(original_cwd)


def load_instances(jsonl_file: str) -> List[Dict[str, Any]]:
    """Load instances from JSONL file."""
    instances = []
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        instance = json.loads(line)
                        instances.append(instance)
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing line {line_num}: {e}")
                        continue
        logger.info(f"Loaded {len(instances)} instances from {jsonl_file}")
        return instances
    except FileNotFoundError:
        logger.error(f"File not found: {jsonl_file}")
        return []
    except Exception as e:
        logger.error(f"Error loading instances: {e}")
        return []


async def process_instance(
    instance: Dict[str, Any],
    index: int,
    total: int,
    model: str,
    workspace_root: str = ".",
    setup_script: str = "setup-env.sh",
    keep_workspace: bool = True,
    strategy: str = "none"
) -> Dict[str, Any]:
    """Process a single instance with Docker-based Kimi execution."""
    instance_id = instance.get('instance_id', f'unknown_{index}')
    image_name = instance.get('image_name', '')
    problem_statement = instance.get('problem_statement', '')
    workspace = None

    logger.info(f"Processing instance {index + 1}/{total}: {instance_id}")

    if not image_name:
        logger.error(f"No image_name found for instance {instance_id}")
        return {
            "instance_id": instance_id,
            "model_name_or_path": model,
            "model_patch": "",
            "error": "No image_name found"
        }

    if not problem_statement:
        logger.error(f"No problem_statement found for instance {instance_id}")
        return {
            "instance_id": instance_id,
            "model_name_or_path": model,
            "model_patch": "",
            "error": "No problem_statement found"
        }

    try:
        logger.info(f"Starting Docker integration for {instance_id}")

        env = {}
        env["KIMI_MODEL"] = os.environ.get("KIMI_MODEL", model)
        env["MOONSHOT_API_KEY"] = os.environ.get("MOONSHOT_API_KEY", os.environ.get("KIMI_API_KEY", ""))
        env["KIMI_API_KEY"] = env["MOONSHOT_API_KEY"]

        if strategy != "none":
            problem_statement = apply_safety_hint(problem_statement)

        prompt = USER_PROMPT_TEMPLATE.format(local_work_dir="/project", problem_statement=problem_statement)
        escaped_instruction = shlex.quote(prompt)

        with DockerIntegration(image_name, container_work_dir="/project", workspace_root=workspace_root, keep_workspace=keep_workspace) as integration:
            print("🔧 Setting up environment...")
            workspace = integration.setup_persistent_workspace()

            setup_result = integration.setup_cli_env(setup_script_path=setup_script)
            if not setup_result["success"]:
                logger.warning(f"Environment setup failed for {instance_id}: {setup_result.get('stderr', '')}")

            logger.info(f"Running Kimi CLI for {instance_id}")
            kimi_command = (
                "kimi --output-format stream-json "
                f"-p {escaped_instruction} --yolo"
            )

            result = integration.execute_in_container(kimi_command, env=env)

            if result["success"]:
                logger.info(f"Kimi execution completed successfully for {instance_id}")
            else:
                logger.error(f"Kimi execution failed for {instance_id}: {result.get('stderr', '')}")

            print("\n🎉 Session complete!")
            print(f"📁 Your improved code is at: {workspace}")

            diff_text = simple_git_diff(str(workspace))

            result_dict = {
                "instance_id": instance_id,
                "model_name_or_path": model,
                "model_patch": diff_text,
                "workspace": str(workspace),
                "kimi_stdout": result.get("stdout", ""),
                "kimi_stderr": result.get("stderr", ""),
                "kimi_success": result.get("success", False)
            }

            integration.cleanup()
            logger.info(f"Successfully processed {instance_id}")
            return result_dict

    except Exception as e:
        logger.error(f"Error processing instance {instance_id}: {e}")
        return {
            "instance_id": instance_id,
            "model_name_or_path": model,
            "model_patch": "",
            "error": str(e),
            "workspace": str(workspace)
        }


async def main(args):
    """Main function to process all instances."""
    jsonl_file = args.jsonl_file
    model = args.model
    setup_script = args.setup_script

    timestamp = int(time.time())
    if args.timestamp_suffix:
        timestamp_str = f"{timestamp}_{args.timestamp_suffix}"
    else:
        timestamp_str = str(timestamp)

    workspace_root = Path(args.results_dir, "workspace")
    workspace_root.mkdir(parents=True, exist_ok=True)
    results_dir = Path(args.results_dir, model, timestamp_str)
    results_dir.mkdir(parents=True, exist_ok=True)

    instances = load_instances(jsonl_file)
    if not instances:
        logger.error("No instances to process")
        return

    if args.load_from_file:
        with open(args.load_from_file, 'r', encoding='utf-8') as f:
            completed_results = json.load(f)
        completed_instances = [ins["instance_id"] for ins in completed_results]
        instances = [ins for ins in instances if ins["instance_id"] not in completed_instances]
        logger.info(f"Processing {len(instances)} instances after {args.load_from_file}")

    if args.num_instances is None:
        instances = instances[args.start_idx:]
    else:
        instances = instances[args.start_idx:args.start_idx + args.num_instances]
    logger.info(f"Starting processing of {len(instances)} instances")

    results = []
    for i, instance in enumerate(instances):
        try:
            result = await process_instance(
                instance, i, len(instances), model,
                workspace_root=str(workspace_root),
                setup_script=setup_script,
                keep_workspace=args.keep_workspace,
                strategy=args.strategy,
            )

            intermediate_file = results_dir / f"intermediate_{i + 1}.json"
            with open(intermediate_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved intermediate results to {intermediate_file}")

            results.append(result)

        except KeyboardInterrupt:
            logger.info("Processing interrupted by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error processing instance {i}: {e}")
            continue

    output_file = results_dir / "final_results.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"Processing complete. Results saved to {output_file}")
    logger.info(f"Processed {len(results)} instances successfully")


if __name__ == "__main__":
    args = get_options().parse_args()
    asyncio.run(main(args))
