import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template
from typing import TypedDict

from susvibes.constants import *
from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.env_specs import dockerfiles
from susvibes.curate.constants import get_path
from susvibes.curate.prompts import INSTALL_TEST_PROMPT_TEMPLATE
from susvibes.curate.agents.ports import EnvAgentPort
from susvibes.curate.env_setup.create_env import create_env_threadpool
from susvibes.utils import load_file, save_file
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    apply_patch,
    commit_changes,
    reset_to_commit,
    get_diff_patch
)

class SusVibesRecord(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    image_name: str
    problem_statement: str
    task_patch: str
    golden_patch: str
    security_patch: str
    test_patch: str
    expected_failures: dict
    cwe_ids: str
    cve_id: str
    created_at: str
    language: str
    info_page: str 

def prologue(
    task_dataset_path: Path, 
    instance_ids: list = None,
    exclude_projects: list = []
):
    class SafeDict(dict):
        def __missing__(self, key):
            return '{' + key + '}'  
          
    EnvAgentPort.init(run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    dev_tools = load_file(DEV_TOOLS_PATH)
    if instance_ids != None:
        task_dataset = [data_record for data_record in task_dataset 
            if data_record["instance_id"] in instance_ids]
    task_dataset = [data_record for data_record in task_dataset 
        if data_record["project"] not in exclude_projects 
        and data_record["instance_id"] in dev_tools]
    
    for data_record in task_dataset:
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        reset_to_commit(repo_dir, data_record["base_commit"])
        dev_tool = dev_tools[data_record["instance_id"]]
        image_name = f'dind_py:{dev_tool["version"]}'
        dockerfile_template = dockerfiles.DOCKERFILE_ENV_PY_TEMPLATE.format_map(
            SafeDict(base_image=f'base_py:{dev_tool["version"]}'))
        EnvAgentPort.add_task(
            image=image_name,
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=data_record["base_commit"],
            problem_statement=Template(INSTALL_TEST_PROMPT_TEMPLATE).render(
                test_files=data_record["test_files"],
                dockerfile_template=dockerfile_template
            ),
            instance_id=data_record["instance_id"],
        )
    EnvAgentPort.before_start()
    
def make_susvibes_record(data_record: dict) -> SusVibesRecord:
    repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, data_record["base_commit"])
    apply_patch(repo_dir, data_record["security_patch"], reverse=True)
    apply_patch(repo_dir, data_record["mask_patch"])
    code_mask_commit = commit_changes(repo_dir, f'Code mask at {data_record["base_commit"]}')
    golden_patch = get_diff_patch(repo_dir, code_mask_commit, data_record["base_commit"])
    data_record["golden_patch"] = golden_patch
    data_record.pop("mask_patch", None)
    data_record.pop("test_files", None)
    return data_record

def epilogue(
    task_dataset_path: Path,
    dataset_path: Path,
    stats_path: Path,
    max_workers: int,
    agent_output_dir: Path = None,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    predictions = EnvAgentPort.after_completion(agent_output_dir) if agent_output_dir else None
    task_dataset = load_file(task_dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}
    dataset_env = create_env_threadpool(task_dataset, stats, max_workers, predictions, force,
                                        save_specs=save_specs, instance_ids=instance_ids)
    dataset = [make_susvibes_record(data_record)
        for data_record in tqdm(dataset_env, desc="Wrapping up")]
    
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")

        
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for building environment components.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of dataset building.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of dataset building.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored.",
    )
    parser.add_argument(
        "--from_existing_specs",
        action="store_true",
        help="Use existing env specs instead of agent output.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for creating environment.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run the environment creation.",
    )
    parser.add_argument(
        "--skip_specs",
        action="store_true",
        help="Skip saving environment specs to file.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads, 
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--subset",
        type=str,
        default=None,
        help="Subset name for output subdirectory (datasets/<subset>/...)",
    )
    args = parser.parse_args()

    task_dataset_path = get_path('task_dataset', args.subset)
    dataset_path = get_path('dataset', args.subset)
    stats_path = get_path('stats', args.subset)

    if args.prologue:
        prologue(task_dataset_path)
    elif args.epilogue:
        agent_output_dir = None if args.from_existing_specs else args.agent_output_dir
        epilogue(task_dataset_path, dataset_path, stats_path,
                 args.max_workers, agent_output_dir, args.force,
                 save_specs=not args.skip_specs, instance_ids=args.instance_ids)
    else:
        print("Please specify either --prologue or --epilogue.")