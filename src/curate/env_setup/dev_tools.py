import argparse
import re
from pathlib import Path

from constants import *
from env_specs import AVAILABLE_DEV_TOOL_VERSIONS
from curate.prompts import DEV_TOOLS_PROMPT_TEMPLATE
from curate.agents import EnvAgentPort
from curate.utils import (
    load_file, 
    save_file, 
    parse_instance_id,
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
)

TASK_DATASET_PATH = Path("../datasets/task_dataset.jsonl")

def prologue(task_dataset_path: Path):
    EnvAgentPort.init(run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    for data_record in task_dataset:
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        reset_to_commit(repo_dir, data_record["base_commit"])
        EnvAgentPort.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=data_record["base_commit"],
            problem_statement=DEV_TOOLS_PROMPT_TEMPLATE,
            instance_id=data_record["instance_id"],
        )
    EnvAgentPort.before_start()

def epilogue(agent_output_dir: Path):
    predictions = EnvAgentPort.after_completion(agent_output_dir)
    dev_tools = {}

    for pred in predictions:
        project, base_commit = parse_instance_id(pred["instance_id"])
        repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)

        reset_to_commit(repo_dir, base_commit, new_branch=False)
        apply_patch(repo_dir, pred["model_patch"])
        try:
            dev_tool = load_file(repo_dir / "dev_tools.json")
            assert "name" in dev_tool and "version" in dev_tool
            cleaned_version = re.sub(r"[^0-9.]", "", dev_tool["version"])
            dev_tool["version"] = ".".join(cleaned_version.split(".")[:2])
            if dev_tool["name"] not in AVAILABLE_DEV_TOOL_VERSIONS:
                print(f"Unsupported dev tool for {pred['instance_id']}: {dev_tool['name']}")
                continue
            available_versions = AVAILABLE_DEV_TOOL_VERSIONS[dev_tool["name"]]
            to_num = lambda v: sum(int(part) * 10 ** (2 * i) 
                for i, part in enumerate(v.split(".")[::-1]))
            if dev_tool["version"] not in available_versions:
                nearest_version = min(available_versions, key=lambda v: abs(to_num(v) - to_num(dev_tool["version"])))
                print(f"Rounding version {dev_tool['version']} to {nearest_version} for {pred['instance_id']}")
                dev_tool["version"] = nearest_version
        except FileNotFoundError:
            print(f"Dev tools not found or invalid.")
            continue

        dev_tools[pred["instance_id"]] = dev_tool

    save_file(dev_tools, DEV_TOOLS_PATH)

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
    args = parser.parse_args()
    if args.prologue:
        prologue(TASK_DATASET_PATH)
    elif args.epilogue:
        epilogue(args.agent_output_dir)
    else:
        print("Please specify either --prologue or --epilogue.")