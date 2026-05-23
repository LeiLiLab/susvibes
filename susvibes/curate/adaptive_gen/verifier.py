import argparse
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.curate.adaptive_gen.prompts import VERIFIER_PROMPT_TEMPLATE
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.curate.adaptive_gen.utils import setup_logger
from susvibes.utils import load_file, save_file
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    apply_patch,
    commit_changes,
    reset_to_commit,
    rollback,
    get_diff_patch
)

logger = None

def init_logger():
    global logger
    logger = setup_logger("verifier.log", __name__, add_stdout=False)

def prologue(task_dataset_path: Path, instance_ids: list = None, no_require_test: bool = False):
    port = SWEAgentPort(run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    if instance_ids != None:
        task_dataset = [data_record for data_record in task_dataset
            if data_record["instance_id"] in instance_ids]
    for data_record in tqdm(task_dataset, desc="Preparing agent run"):
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        test_patch = None if no_require_test else data_record["test_patch"]
        rollback_commit = rollback(repo_dir, data_record["base_commit"],
            data_record["security_patch"], test_patch)
        apply_patch(repo_dir, data_record["mask_patch"])
        task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')
        code_patch = get_diff_patch(repo_dir, task_commit, data_record["base_commit"])

        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=task_commit,
            problem_statement=Template(VERIFIER_PROMPT_TEMPLATE).render(
                task_desc=data_record["problem_statement"],
                code_patch=code_patch),
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(agent_output_dir: Path, task_dataset_path: Path, no_require_test: bool = False):
    predictions, total_cost = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
    task_dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in load_file(task_dataset_path)}

    successful_instance_ids = []
    verified_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = task_dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        test_patch = None if no_require_test else data_record["test_patch"]
        rollback_commit = rollback(repo_dir, data_record["base_commit"],
            data_record["security_patch"], test_patch)
        apply_patch(repo_dir, data_record["mask_patch"])
        task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')

        if not pred.get("model_patch", "").strip():
            logger.warning("Empty model patch for %s, skipping.", pred["instance_id"])
            continue
        try:
            apply_patch(repo_dir, pred["model_patch"])
        except Exception as e:
            logger.warning("Error applying model patch for %s: %s", pred["instance_id"], e)
            continue
        result_path = repo_dir / "verifier.json"
        if result_path.exists():
            result = load_file(result_path)
            assert "excessive_or_contradictory" in result and "explanation" in result
        else:
            logger.warning("Verifier result for %s not found or invalid.", pred["instance_id"])
            continue
        try:
            task_patch = get_diff_patch(repo_dir, data_record["base_commit"], task_commit)
            reset_to_commit(repo_dir, data_record["base_commit"])
            apply_patch(repo_dir, task_patch)
            if not no_require_test:
                apply_patch(repo_dir, data_record["test_patch"])
        except Exception as e:
            logger.error("Error re-applying test patch for %s.", pred["instance_id"])
            continue
        
        successful_instance_ids.append(pred["instance_id"])
        if result["excessive_or_contradictory"]:
            logger.warning("Excessive implementation found for %s.", pred["instance_id"])
            continue
        verified_instance_ids.append(data_record["instance_id"])

        data_record["task_patch"] = task_patch
    
    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids, verified_instance_ids, total_cost

def pipeline(task_dataset_path: Path, instance_ids: list = None, no_require_test: bool = False, iter_id: int = None):
    logger.info("=== Verifier pipeline started (iter=%s) ===", iter_id)
    port = prologue(task_dataset_path, instance_ids, no_require_test=no_require_test)
    agent_output_dir = port.run_batch()
    successful_instance_ids, verified_instance_ids, total_cost = epilogue(
        agent_output_dir=agent_output_dir,
        task_dataset_path=task_dataset_path,
        no_require_test=no_require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, verified_instance_ids, total_cost

def remove_results(instance_ids: list):
    port = SWEAgentPort(run_name=__spec__.name)
    port.remove_results(instance_ids)


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
    parser.add_argument(
        "--no_require_test",
        action="store_true",
        help="Do not require test patches; skip test_patch in rollback.",
    )
    args = parser.parse_args()
    if args.prologue:
        prologue(args.task_dataset_path, no_require_test=args.no_require_test)
    elif args.epilogue:
        epilogue(args.agent_output_dir, args.task_dataset_path, no_require_test=args.no_require_test)
    else:
        pipeline(args.task_dataset_path, no_require_test=args.no_require_test)