import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.curate.adaptive_gen.prompts import MASK_GEN_PROMPT_TEMPLATE
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.curate.adaptive_gen.utils import module_setup_logger
from susvibes.core.utils import load_file, save_file, touched_files, filter_target_files
from susvibes.curate.utils import (
    get_repo_dir,
    apply_patch,
    rollback,
    len_patch,
    count_patch_additions_deletions
)

logger = None

def init_logger():
    global logger
    logger = module_setup_logger("mask.log", __name__, add_stdout=False)

def prologue(
    processed_dataset_path: Path,
    length_ratio: int = 1,
    max_length: int = None,
    instance_ids: list = None,
    require_test: bool = True,
):
    port = SWEAgentPort(run_name=__spec__.name)
    processed_dataset = load_file(processed_dataset_path)
    if instance_ids is not None:
        processed_dataset = [data_record for data_record in processed_dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in tqdm(processed_dataset, desc="Preparing agent run"):
        instance_id = data_record["instance_id"]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        try:
            test_patch = data_record["test_patch"] if require_test else None
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)
        except Exception as e:
            logger.error("Error rolling back repository for %s: %s", instance_id, e)
            continue
        effective_ratio = length_ratio
        if max_length:
              _, diff_lines = len_patch(data_record["security_patch"])
              effective_ratio = min(effective_ratio, max(max_length / diff_lines - 1, 1))
        effective_ratio = int(effective_ratio)
        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=rollback_commit,
            problem_statement=Template(MASK_GEN_PROMPT_TEMPLATE).render(
                ratio=effective_ratio,
                diff_patch=data_record["security_patch"]),
            instance_id=instance_id,
        )
    port.before_start()
    return port

def epilogue(
    agent_output_dir: Path,
    processed_dataset_path: Path,
    task_dataset_path: Path,
    length_ratio: int = 1,
    max_length: int = None,
    ratio_tolerance: float = 0,
    require_test: bool = True,
):
    predictions, total_cost = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
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
        test_patch = data_record["test_patch"] if require_test else None
        rollback_commit = rollback(repo_dir, data_record["base_commit"],
            data_record["security_patch"], test_patch)
        model_patch = pred.get("model_patch", "")
        txt_exts = (".md", ".txt", ".log")
        txt_files = {f for f in touched_files(model_patch) if f.endswith(txt_exts)}
        if txt_files:
            logger.warning("Filtering out text files from patch for %s: %s", instance_id, txt_files)
        filtered_patch = filter_target_files(model_patch, txt_files, exclude=True) if txt_files else model_patch
        if not filtered_patch.strip():
            logger.warning("Empty model_patch for %s, skipping.", instance_id)
            continue
        try:
            apply_patch(repo_dir, filtered_patch)
            apply_patch(repo_dir, filtered_patch, reverse=True)
        except Exception as e:
            logger.warning("Error applying model_patch for %s: %s", instance_id, e)
            continue
        if "--- /dev/null" in filtered_patch:
            logger.warning("Forbidden file creation for %s, skipping.", instance_id)
            continue
        _, diff_deleted = count_patch_additions_deletions(data_record["security_patch"])
        _, mask_deleted = count_patch_additions_deletions(filtered_patch)
        _, diff_lines = len_patch(data_record["security_patch"])
        _, mask_lines = len_patch(filtered_patch)
        if max_length and mask_lines > max_length:
            logger.warning("Mask too long for %s: %d lines > max_length %d, skipping.",
                           instance_id, mask_lines, max_length)
            continue
        effective_ratio = length_ratio
        if max_length:
            effective_ratio = min(effective_ratio, max(max_length / diff_lines - 1, 1))
        effective_ratio = int(effective_ratio)
        if mask_deleted < diff_deleted * max(effective_ratio - ratio_tolerance, 1):
            logger.warning("Mask too short for %s: %d mask deleted < %d diff deleted * %dx, skipping.",
                           instance_id, mask_deleted, diff_deleted, effective_ratio)
            continue
        additions, deletions = count_patch_additions_deletions(filtered_patch)
        if additions > deletions:
            logger.warning("Mask has more additions than deletions for %s: +%d/-%d, skipping.",
                           instance_id, additions, deletions)
            continue
        if instance_id in task_dataset_by_id:
            task_dataset_by_id[instance_id]["mask_patch"] = filtered_patch
        else:
            data_record["mask_patch"] = filtered_patch
            task_dataset_by_id[instance_id] = data_record
        successful_instance_ids.append(instance_id)
            
    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids, total_cost

def pipeline(
    processed_dataset_path: Path,
    task_dataset_path: Path,
    length_ratio: int = 1,
    max_length: int = None,
    ratio_tolerance: float = 0,
    instance_ids: list = None,
    require_test: bool = True,
    iter_id: int = None,
):
    logger.info("=== Mask generation pipeline started (iter=%s, ratio=%.1fx) ===",
                iter_id, length_ratio)
    port = prologue(
        processed_dataset_path=processed_dataset_path,
        length_ratio=length_ratio,
        max_length=max_length,
        instance_ids=instance_ids,
        require_test=require_test,
    )
    agent_output_dir = port.run_batch()
    successful_instance_ids, total_cost = epilogue(
        agent_output_dir=agent_output_dir,
        processed_dataset_path=processed_dataset_path,
        task_dataset_path=task_dataset_path,
        length_ratio=length_ratio,
        max_length=max_length,
        ratio_tolerance=ratio_tolerance,
        require_test=require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, total_cost

def remove_results(instance_ids: list):
    port = SWEAgentPort(run_name=__spec__.name)
    port.remove_results(instance_ids)


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
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Require repo-provided tests (default True); false uses the synthesized-test path.",
    )
    args = parser.parse_args()
    if args.prologue:
        prologue(args.processed_dataset_path, require_test=args.require_test)
    elif args.epilogue:
        epilogue(args.agent_output_dir, args.processed_dataset_path,
            task_dataset_path=args.task_dataset_path, require_test=args.require_test)
    else:
        pipeline(processed_dataset_path=args.processed_dataset_path,
            task_dataset_path=args.task_dataset_path, require_test=args.require_test)