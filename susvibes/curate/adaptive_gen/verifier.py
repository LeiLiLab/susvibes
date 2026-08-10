import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_agent_setting_path, get_log_dir
from susvibes.curate.adaptive_gen.prompts import VERIFIER_PROMPT_TEMPLATE
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.utils import load_file, save_file, setup_logger
from susvibes.curate.utils import (
    RepoLocks,
    get_repo_dir,
    apply_patch,
    commit_changes,
    reset_to_commit,
    rollback,
    get_diff_patch
)

logger = None

def init_logger(adaptive_gen_log_dir):
    global logger
    logger = setup_logger(adaptive_gen_log_dir, "verifier.log", __name__, add_stdout=False)

def prologue(dataset_path: Path, output_dir: Path, instance_ids: list = None, require_test: bool = True):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")),
        run_name=__spec__.name, output_dir=output_dir)
    dataset = load_file(dataset_path)
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in tqdm(dataset, desc="Preparing agent run"):
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The clone is shared: hold it from the rollback through every read of the tree it leaves.
        with RepoLocks.locked(data_record["project"]):
            test_patch = data_record["test_patch"] if require_test else None
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)
            apply_patch(repo_dir, data_record["mask_patch"])
            task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')
            code_patch = get_diff_patch(repo_dir, task_commit, data_record["base_commit"])

        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            lock_path=RepoLocks.get_lock_path(repo_dir),
            base_commit=task_commit,
            problem_statement=Template(VERIFIER_PROMPT_TEMPLATE).render(
                task_desc=data_record["problem_statement"],
                code_patch=code_patch),
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(output_dir: Path, dataset_path: Path, require_test: bool = True):
    predictions, total_cost = SWEAgentPort.after_completion(output_dir, submitted_only=True)
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in load_file(dataset_path)}

    successful_instance_ids = []
    verified_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # One tree state carries the whole read — rollback, mask, the agent's patch, then the diff
        # — so the lock spans it, and the `finally` hands the clone back as it was found however
        # this exits.
        with RepoLocks.locked(data_record["project"]):
            test_patch = data_record["test_patch"] if require_test else None
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)
            apply_patch(repo_dir, data_record["mask_patch"])
            task_commit = commit_changes(repo_dir, f'Task at {data_record["base_commit"]}')
            try:
                if not pred.get("model_patch", "").strip():
                    logger.warning("Empty model_patch for %s, skipping.", pred["instance_id"])
                    continue
                try:
                    apply_patch(repo_dir, pred["model_patch"])
                except Exception as e:
                    logger.warning("Error applying model_patch for %s: %s", pred["instance_id"], e)
                    continue
                result_path = repo_dir / "verifier.json"
                result = load_file(result_path) if result_path.exists() else {}
                if not all(k in result for k in
                           ("excessive", "excessive_explanation", "contradict", "contradict_explanation")):
                    logger.warning("Verifier result for %s not found or invalid.", pred["instance_id"])
                    continue
                try:
                    task_patch = get_diff_patch(repo_dir, data_record["base_commit"], task_commit)
                    reset_to_commit(repo_dir, data_record["base_commit"])
                    apply_patch(repo_dir, task_patch)
                    if require_test:
                        apply_patch(repo_dir, data_record["test_patch"])
                except Exception as e:
                    logger.error("Error re-applying test_patch for %s.", pred["instance_id"])
                    continue
            finally:
                reset_to_commit(repo_dir, rollback_commit, new_branch=False)
        
        successful_instance_ids.append(pred["instance_id"])
        if result["excessive"] or result["contradict"]:
            logger.warning("Verification failed for %s (excessive=%s, contradict=%s).",
                           pred["instance_id"], result["excessive"], result["contradict"])
            continue
        verified_instance_ids.append(data_record["instance_id"])

        data_record["task_patch"] = task_patch
    
    save_file(dataset_by_id.values(), dataset_path)
    return successful_instance_ids, verified_instance_ids, total_cost

def pipeline(dataset_path: Path, output_dir: Path, instance_ids: list = None, require_test: bool = True, iter_id: int = None):
    logger.info("=== Verifier pipeline started (iter=%s) ===", iter_id)
    port = prologue(dataset_path, output_dir, instance_ids, require_test=require_test)
    output_dir = port.run_batch()
    successful_instance_ids, verified_instance_ids, total_cost = epilogue(
        output_dir=output_dir,
        dataset_path=dataset_path,
        require_test=require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, verified_instance_ids, total_cost

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
        "--dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file of created tasks.",
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Require repo-provided tests (default True); false uses the synthesized-test path.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID; locates the agent output dir logs/curate/<run_id>/adaptive_gen/verifier/.",
    )
    args = parser.parse_args()
    output_dir = get_log_dir(args.run_id, "adaptive_gen", "verifier")
    if args.prologue:
        prologue(args.dataset_path, output_dir=output_dir, require_test=args.require_test)
    elif args.epilogue:
        epilogue(output_dir, args.dataset_path, require_test=args.require_test)
    else:
        pipeline(args.dataset_path, output_dir=output_dir, require_test=args.require_test)