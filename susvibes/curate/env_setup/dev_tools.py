"""
Purpose: Identify the Python version required by each task's repository
using an agent, and map it to an available base image version.

python -m susvibes.curate.env_setup.dev_tools \
    --run_id playground
"""

import argparse
import re
from pathlib import Path

from susvibes.constants import *
from susvibes.curate.constants import LOCAL_REPOS_DIR, ENV_SETUP_LOG_DIR, get_path
from susvibes.env_specs import AVAILABLE_DEV_TOOL_VERSIONS
from susvibes.curate.env_setup.prompts import DEV_TOOLS_PROMPT_TEMPLATE
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.utils import load_file, save_file, setup_logger, parse_instance_id
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
)

root_dir = Path(__file__).parent.parent.parent.parent

logger = None
detail_logger = None

def init_loggers(log_dir):
    global logger, detail_logger
    logger = setup_logger(log_dir, "dev_tools.log", f"{__name__}.summary", add_stdout=True)
    detail_logger = setup_logger(log_dir, "dev_tools_details.log", f"{__name__}.detail", add_stdout=False)

def prologue(task_dataset_path: Path):
    port = SWEAgentPort(run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    for data_record in task_dataset:
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        reset_to_commit(repo_dir, data_record["base_commit"])
        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=data_record["base_commit"],
            problem_statement=DEV_TOOLS_PROMPT_TEMPLATE,
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(agent_output_dir: Path, run_id: str = "default"):
    predictions, total_cost = SWEAgentPort.after_completion(agent_output_dir)
    dev_tools_path = get_env_spec_path('dev_tools', run_id)
    dev_tools = load_file(dev_tools_path) if dev_tools_path.exists() else {}

    for pred in predictions:
        project, base_commit = parse_instance_id(pred["instance_id"])
        repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)

        reset_to_commit(repo_dir, base_commit, new_branch=False)
        if not pred.get("model_patch", "").strip():
            detail_logger.warning("Empty model patch for %s, skipping.", pred["instance_id"])
            continue
        try:
            apply_patch(repo_dir, pred["model_patch"])
        except Exception as e:
            detail_logger.warning("Error applying model patch for %s: %s", pred["instance_id"], e)
            continue
        try:
            dev_tool = load_file(repo_dir / "dev_tools.json")
            assert "name" in dev_tool and "version" in dev_tool
            cleaned_version = re.sub(r"[^0-9.]", "", dev_tool["version"])
            dev_tool["version"] = ".".join(cleaned_version.split(".")[:2])
            if dev_tool["name"] not in AVAILABLE_DEV_TOOL_VERSIONS:
                detail_logger.warning("Unsupported dev tool for %s: %s", pred["instance_id"], dev_tool["name"])
                continue
            available_versions = AVAILABLE_DEV_TOOL_VERSIONS[dev_tool["name"]]
            to_num = lambda v: sum(int(part) * 10 ** (2 * i)
                for i, part in enumerate(v.split(".")[::-1]))
            if dev_tool["version"] not in available_versions:
                if to_num(dev_tool["version"]) < to_num("3.6"):
                    detail_logger.warning("Skipping %s: version %s < 3.6", pred["instance_id"], dev_tool["version"])
                    continue
                nearest_version = min(available_versions, key=lambda v: abs(to_num(v) - to_num(dev_tool["version"])))
                detail_logger.info("Rounding version %s to %s for %s", dev_tool["version"], nearest_version, pred["instance_id"])
                dev_tool["version"] = nearest_version
        except FileNotFoundError:
            detail_logger.warning("Dev tools not found or invalid for %s.", pred["instance_id"])
            continue

        dev_tools[pred["instance_id"]] = dev_tool

    logger.info("%d / %d dev tools identified successfully.", len(dev_tools), len(predictions))
    if total_cost is not None:
        logger.info("Agent cost: $%.2f", total_cost)
    dev_tools_path = get_env_spec_path('dev_tools', run_id)
    save_file(dev_tools, dev_tools_path)
    print(f"Dev tools saved to {dev_tools_path}.")

def pipeline(task_dataset_path: Path, run_id: str = "default"):
    logger.info("Dev tools pipeline started.")
    port = prologue(task_dataset_path)
    agent_output_dir = port.run_batch()
    epilogue(agent_output_dir, run_id=run_id)
    logger.info("Dev tools pipeline finished.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for determining environment dev tools.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of dev tools setup.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of dev tools setup.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
    )
    args = parser.parse_args()

    env_setup_log_dir = ENV_SETUP_LOG_DIR / args.run_id
    init_loggers(env_setup_log_dir)

    task_dataset_path = get_path('task_dataset', args.run_id)
    if not task_dataset_path.exists():
        fallback_path = get_path('processed_dataset', args.run_id)
        if fallback_path.exists():
            answer = input(f"task_dataset not found. Use {fallback_path} instead? [Y/n] ").strip().lower()
            if answer in ('', 'y'):
                task_dataset_path = fallback_path
            else:
                print("Aborted.")
                exit(1)
        else:
            print(f"Neither task_dataset nor processed_dataset found under {task_dataset_path.parent}.")
            exit(1)

    if args.prologue:
        prologue(task_dataset_path)
    elif args.epilogue:
        epilogue(args.agent_output_dir, run_id=args.run_id)
    else:
        pipeline(task_dataset_path, run_id=args.run_id)