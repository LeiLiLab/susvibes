import re
import argparse
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.constants import LOCAL_REPOS_DIR
from susvibes.curate.prompts import ISSUE_GEN_PROMPT_TEMPLATE
from susvibes.curate.agents import SWEAgentPort
from susvibes.curate.utils import (
    load_file, 
    save_file, 
    get_repo_dir,
    clone_github_repo,
    apply_patch,
    rollback
)

def prologue(task_dataset_path: Path, instance_ids: list = None, model: dict = None):
    SWEAgentPort.init(run_name=__spec__.name, model=model)
    task_dataset = load_file(task_dataset_path)
    if instance_ids != None:
        task_dataset = [data_record for data_record in task_dataset 
            if data_record["instance_id"] in instance_ids]
    for data_record in tqdm(task_dataset, desc="Preparing agent run"):
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        rollback_commit = rollback(repo_dir, data_record["base_commit"], 
            data_record["security_patch"], data_record["test_patch"])

        SWEAgentPort.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=rollback_commit,
            problem_statement=Template(ISSUE_GEN_PROMPT_TEMPLATE).render(
                mask_patch=data_record["mask_patch"]),
            instance_id=data_record["instance_id"],
        )
    SWEAgentPort.before_start()

def epilogue(agent_output_dir: Path, task_dataset_path: Path):
    predictions = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
    task_dataset_by_id = {data_record["instance_id"]: data_record 
        for data_record in load_file(task_dataset_path)}
    
    successful_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = task_dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        rollback_commit = rollback(repo_dir, data_record["base_commit"], 
            data_record["security_patch"], data_record["test_patch"])
        try:
            apply_patch(repo_dir, pred["model_patch"])
        except Exception as e:
            print(f'Error applying model patch for {pred["instance_id"]}: {e}')
            continue
        problem_statement_path = repo_dir / "problem_statement.md"
        if problem_statement_path.exists():
            problem_statement = load_file(problem_statement_path)
        else:
            print(f'Problem statement for {pred["instance_id"]} not found.')
            continue
        if re.search(r"(?<![A-Za-z])test", problem_statement):
            print(f'Problem statement for {pred["instance_id"]} references tests, skipping.')
            continue

        data_record["problem_statement"] = problem_statement
        successful_instance_ids.append(data_record["instance_id"])
    
    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids

def pipeline(
    task_dataset_path: Path,
    instance_ids: list = None,
    model: dict = None
):
    print(f"Issue generation pipeline started.")
    prologue(task_dataset_path, instance_ids, model=model)
    agent_output_dir = SWEAgentPort.run_batch()
    successful_instance_ids = epilogue(
        agent_output_dir=agent_output_dir,
        task_dataset_path=task_dataset_path
    )
    return successful_instance_ids

def remove_results(
    instance_ids: list,
):
    SWEAgentPort.init(run_name=__spec__.name)
    SWEAgentPort.remove_results(instance_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for problem generation.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of problem generation.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of problem generation.",
    )
    parser.add_argument(
        "--task_dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file of created tasks.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored, required in epilogue.",
    )
    args = parser.parse_args()
    if args.prologue:
        prologue(args.task_dataset_path)
    elif args.epilogue:
        epilogue(args.agent_output_dir, args.task_dataset_path)
    else:
        pipeline(args.task_dataset_path)