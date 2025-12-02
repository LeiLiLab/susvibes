import argparse
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from constants import LOCAL_REPOS_DIR
from curate.prompts import VERIFIER_PROMPT_TEMPLATE
from curate.agents import SWEAgentPort
from curate.utils import (
    load_file, 
    save_file, 
    get_repo_dir,
    clone_github_repo,
    apply_patch,
    commit_changes,
    reset_to_commit,
    rollback,
    get_diff_patch
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
        apply_patch(repo_dir, data_record["mask_patch"])
        task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')
        code_patch = get_diff_patch(repo_dir, task_commit, data_record["base_commit"])

        SWEAgentPort.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=task_commit,
            problem_statement=Template(VERIFIER_PROMPT_TEMPLATE).render(
                task_desc=data_record["problem_statement"],
                code_patch=code_patch),
            instance_id=data_record["instance_id"],
        )
    SWEAgentPort.before_start()

def epilogue(agent_output_dir: Path, task_dataset_path: Path):
    predictions = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
    task_dataset_by_id = {data_record["instance_id"]: data_record 
        for data_record in load_file(task_dataset_path)}

    successful_instance_ids = []
    verified_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = task_dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        rollback_commit = rollback(repo_dir, data_record["base_commit"], 
            data_record["security_patch"], data_record["test_patch"])
        apply_patch(repo_dir, data_record["mask_patch"])
        task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')

        try:
            apply_patch(repo_dir, pred["model_patch"])
        except Exception as e:
            print(f'Error applying model patch for {pred["instance_id"]}: {e}')
            continue
        result_path = repo_dir / "verifier.json"
        if result_path.exists():
            result = load_file(result_path)
            assert "excessive_implementations" in result and "explanation" in result
        else:
            print(f'Verifier result for {pred["instance_id"]} not found or invalid.')
            continue
        try:
            task_patch = get_diff_patch(repo_dir, data_record["base_commit"], task_commit)
            reset_to_commit(repo_dir, data_record["base_commit"])
            apply_patch(repo_dir, task_patch)
            apply_patch(repo_dir, data_record["test_patch"])
        except Exception as e:
            print(f'Error re-applying test patch for {pred["instance_id"]}.')
            continue
        
        successful_instance_ids.append(pred["instance_id"])
        if result["excessive_implementations"]:
            print(f'Excessive implementation found for {pred["instance_id"]}.')
            continue
        verified_instance_ids.append(data_record["instance_id"])

        data_record["task_patch"] = task_patch
    
    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids, verified_instance_ids

def pipeline(
    task_dataset_path: Path,
    instance_ids: list = None,
    model: dict = None
):
    print(f"Verifier pipeline started.")
    prologue(task_dataset_path, instance_ids, model)
    agent_output_dir = SWEAgentPort.run_batch()
    successful_instance_ids, verified_instance_ids = epilogue(
        agent_output_dir=agent_output_dir,
        task_dataset_path=task_dataset_path
    )
    return successful_instance_ids, verified_instance_ids

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
        help="Run the prologue of task verifier.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of task verifier.",
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