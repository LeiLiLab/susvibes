import argparse
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from constants import LOCAL_REPOS_DIR
from curate.prompts import MASK_GEN_PROMPT_TEMPLATE
from curate.agents import SWEAgentPort
from curate.utils import (
    load_file, 
    save_file, 
    get_repo_dir,
    clone_github_repo,
    apply_patch,
    rollback,
    len_patch
)

def prologue(
    processed_dataset_path: Path,
    length_ratio: int = 2,
    max_length: int = None,
    instance_ids: list = None,
    model: dict = None
):
    SWEAgentPort.init(run_name=__spec__.name, model=model)
    processed_dataset = load_file(processed_dataset_path)
    if instance_ids != None:
        processed_dataset = [data_record for data_record in processed_dataset 
            if data_record["instance_id"] in instance_ids]
    for data_record in tqdm(processed_dataset, desc="Preparing agent run"):
        instance_id = data_record["instance_id"]
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        try:
            rollback_commit = rollback(repo_dir, data_record["base_commit"], 
                data_record["security_patch"], data_record["test_patch"])
        except Exception as e:
            print(f'Error rolling back repository for {instance_id}: {e}')
            continue 
        if max_length:
              _, num_lines = len_patch(data_record["security_patch"])
              length_ratio = min(length_ratio, max_length / num_lines)
        length_ratio = round(length_ratio, 1)
        SWEAgentPort.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=rollback_commit,
            problem_statement=Template(MASK_GEN_PROMPT_TEMPLATE).render(
                ratio=length_ratio,
                diff_patch=data_record["security_patch"]),
            instance_id=instance_id,
        )
    SWEAgentPort.before_start()

def epilogue(
    agent_output_dir: Path,
    processed_dataset_path: Path,
    task_dataset_path: Path
):
    predictions = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
    processed_dataset_by_id = {data_record["instance_id"]: data_record 
        for data_record in load_file(processed_dataset_path)}
    task_dataset = load_file(task_dataset_path) if task_dataset_path.exists() else []
    task_dataset_by_id = {data_record["instance_id"]: data_record 
        for data_record in task_dataset}
    
    successful_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        instance_id = pred["instance_id"]
        data_record = processed_dataset_by_id[instance_id]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        rollback_commit = rollback(repo_dir, data_record["base_commit"], 
            data_record["security_patch"], data_record["test_patch"])
        try:
            apply_patch(repo_dir, pred["model_patch"])
            apply_patch(repo_dir, pred["model_patch"], reverse=True)
        except Exception as e:
            print(f'Error applying model patch for {instance_id}: {e}')
            continue
        if "--- /dev/null" in pred["model_patch"]:
            print(f'Forbidden file creation for {instance_id}, skipping.')
            continue
        if instance_id in task_dataset_by_id:
            task_dataset_by_id[instance_id]["mask_patch"] = pred["model_patch"]
        else:
            data_record["mask_patch"] = pred["model_patch"]
            task_dataset_by_id[instance_id] = data_record
        successful_instance_ids.append(instance_id)
            
    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids
    
def pipeline(
    processed_dataset_path: Path,
    task_dataset_path: Path,
    length_ratio: int = 2,
    max_length: int = None,
    instance_ids: list = None,
    model: dict = None
):
    print(f"Mask generation pipeline started with ratio {length_ratio:.1f}x.")
    prologue(processed_dataset_path, length_ratio, max_length, instance_ids, model)
    agent_output_dir = SWEAgentPort.run_batch()
    successful_instance_ids = epilogue(
        agent_output_dir=agent_output_dir,
        processed_dataset_path=processed_dataset_path,
        task_dataset_path=task_dataset_path
    )
    return successful_instance_ids

def remove_results(
    instance_ids: list,
):
    SWEAgentPort.init(run_name=__spec__.name)
    SWEAgentPort.remove_results(instance_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for mask generation.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of mask generation.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of mask generation.",
    )
    parser.add_argument(
        "--processed_dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file containing cve records.",
    )
    parser.add_argument(
        "--task_dataset_path",
        type=Path,
        help="Path to the dataset file of created tasks, required in epilogue.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored, required in epilogue.",
    )
    args = parser.parse_args()
    if args.prologue:
        prologue(args.processed_dataset_path)
    elif args.epilogue:
        epilogue(args.agent_output_dir, args.processed_dataset_path,
            task_dataset_path=args.task_dataset_path)
    else:
        pipeline(processed_dataset_path=args.processed_dataset_path,
            task_dataset_path=args.task_dataset_path)