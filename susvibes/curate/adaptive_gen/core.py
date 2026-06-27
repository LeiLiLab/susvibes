import random
import argparse
import json
from pathlib import Path

from susvibes.curate.constants import get_dataset_path, get_log_dir
from susvibes.curate.adaptive_gen import mask, problem_gen, verifier
from susvibes.curate.adaptive_gen import utils as module_utils
from susvibes.curate.adaptive_gen.utils import set_log_dir, module_setup_logger
from susvibes.core.utils import load_file, save_file, confirm_overwrite_logs
from susvibes.curate.utils import len_patch, dump_task
from susvibes.curate.mine.check_cov.engine.constants import CoverageLabel

LENGTH_RATIO_FUNC = [1, 2, 5, 15, 30, 50]
TASK_MAX_LENGTH = 2000
MASK_RATIO_TOLERANCE = 0.5

logger = None

def init_loggers(log_dir):
    global logger
    set_log_dir(log_dir)
    logger = module_setup_logger("pipeline.log", __name__)
    mask.init_logger()
    problem_gen.init_logger()
    verifier.init_logger()

def adaptive_task_gen(
    processed_dataset_path: Path,
    task_dataset_path: Path,
    coverage_report_path: Path,
    instance_ids: list = None,
    max_iters: int = None,
    start_iter: int = 0,
    require_test: bool = True,
    debug: bool = False,
):
    processed_dataset = load_file(processed_dataset_path)
    # Only keep instances check_cov found (likely/maybe) test-covered.
    covered_ids = {r["instance_id"] for r in load_file(coverage_report_path)
        if r["label"] in (CoverageLabel.LIKELY, CoverageLabel.MAYBE)}
    processed_dataset = [data_record for data_record in processed_dataset
        if data_record["instance_id"] in covered_ids]
    if instance_ids is not None:
        processed_dataset = [data_record for data_record in processed_dataset
            if data_record["instance_id"] in set(instance_ids)]
    instance_ids = [data_record["instance_id"] for data_record in processed_dataset]
    total_instances = len(instance_ids)
    total_verified = 0
    total_cost = 0

    if not confirm_overwrite_logs(module_utils._log_dir):
        logger.info("Aborted by user.")
        return

    if debug:
        if max_iters is not None and max_iters != 1:
            answer = input(f"Debug mode forces max_iters=1, but max_iters={max_iters} was given. Continue? [Y/n] ").strip().lower()
            if answer not in ('', 'y'):
                logger.info("Aborted by user.")
                return
        max_iters = 1

    logger.info("Adaptive task generation started with %d instances.%s",
                total_instances, " (debug mode)" if debug else "")

    processed_dataset_by_id = {r["instance_id"]: r for r in processed_dataset}

    def _avg_mask_stats(ids, task_dataset_by_id):
        ratios, mask_lines_list, diff_lines_list = [], [], []
        for iid in ids:
            if iid not in task_dataset_by_id or "mask_patch" not in task_dataset_by_id[iid]:
                continue
            _, mask_lines = len_patch(task_dataset_by_id[iid]["mask_patch"])
            _, diff_lines = len_patch(processed_dataset_by_id[iid]["security_patch"])
            mask_lines_list.append(mask_lines)
            diff_lines_list.append(diff_lines)
            if diff_lines > 0:
                ratios.append(mask_lines / diff_lines)
        n = len(mask_lines_list)
        avg_ratio = sum(ratios) / len(ratios) if ratios else 0
        avg_mask = sum(mask_lines_list) / n if n else 0
        avg_diff = sum(diff_lines_list) / n if n else 0
        return avg_ratio, avg_mask, avg_diff

    pending_instance_ids = instance_ids.copy()
    end_iter = start_iter + max_iters if max_iters is not None else len(LENGTH_RATIO_FUNC)
    for iter_id in range(start_iter, end_iter):
        iter_input = len(pending_instance_ids)
        iter_cost = 0
        logger.info("=== Iteration %d/%d  |  ratio=%.1fx  |  pending=%d ===",
                     iter_id + 1, end_iter, LENGTH_RATIO_FUNC[iter_id], iter_input)

        # ---- Stage 1: Mask generation ----
        successful_instance_ids, mask_cost = mask.pipeline(
            processed_dataset_path=processed_dataset_path,
            task_dataset_path=task_dataset_path,
            length_ratio=LENGTH_RATIO_FUNC[iter_id],
            max_length=TASK_MAX_LENGTH,
            ratio_tolerance=MASK_RATIO_TOLERANCE,
            instance_ids=pending_instance_ids,
            require_test=require_test,
            iter_id=iter_id + 1,
        )
        iter_cost += mask_cost or 0
        mask_ok = len(successful_instance_ids)
        mask_fail = iter_input - mask_ok
        failed_instances = [id for id in pending_instance_ids if id not in successful_instance_ids]
        logger.info("  [mask]        %d succeeded, %d failed  |  cost=$%.2f",
                     mask_ok, mask_fail, mask_cost or 0)
        pending_instance_ids = successful_instance_ids
        if not pending_instance_ids:
            logger.info("  No instances left after mask stage, stopping.")
            total_cost += iter_cost
            break

        # ---- Stage 2: Problem statement generation ----
        stage2_input = len(pending_instance_ids)
        successful_instance_ids, gen_cost = problem_gen.pipeline(
            task_dataset_path=task_dataset_path,
            instance_ids=pending_instance_ids,
            require_test=require_test,
            iter_id=iter_id + 1,
        )
        iter_cost += gen_cost or 0
        gen_ok = len(successful_instance_ids)
        gen_fail = stage2_input - gen_ok
        failed_instances += [id for id in pending_instance_ids if id not in successful_instance_ids]
        logger.info("  [problem_gen] %d succeeded, %d failed  |  cost=$%.2f",
                     gen_ok, gen_fail, gen_cost or 0)
        pending_instance_ids = successful_instance_ids
        if not pending_instance_ids:
            logger.info("  No instances left after problem_gen stage, stopping.")
            total_cost += iter_cost
            break

        # ---- Stage 3: Verification ----
        stage3_input = len(pending_instance_ids)
        successful_instance_ids, verified_instance_ids, verify_cost = verifier.pipeline(
            task_dataset_path=task_dataset_path,
            instance_ids=pending_instance_ids,
            require_test=require_test,
            iter_id=iter_id + 1,
        )
        iter_cost += verify_cost or 0
        verify_ok = len(successful_instance_ids)
        verify_fail = stage3_input - verify_ok
        newly_verified = len(verified_instance_ids)
        total_verified += newly_verified
        failed_instances += [id for id in pending_instance_ids if id not in successful_instance_ids]
        remaining_instance_ids = [id for id in successful_instance_ids if id not in verified_instance_ids]
        logger.info("  [verifier]    %d succeeded, %d failed  |  %d verified, %d unverified  |  cost=$%.2f",
                     verify_ok, verify_fail, newly_verified, len(remaining_instance_ids), verify_cost or 0)

        # ---- Iteration summary ----
        total_cost += iter_cost
        total_failed = len(failed_instances)

        # Compute avg mask stats for verified vs non-verified
        task_dataset_by_id = {r["instance_id"]: r for r in load_file(task_dataset_path)}
        v_ratio, v_mask, v_diff = _avg_mask_stats(verified_instance_ids, task_dataset_by_id)
        non_verified_ids = remaining_instance_ids + failed_instances
        nv_ratio, nv_mask, nv_diff = _avg_mask_stats(non_verified_ids, task_dataset_by_id)
        logger.info("  Mask stats (verified):     avg ratio=%.1fx, mask_lines=%.1f, diff_lines=%.1f",
                     v_ratio, v_mask, v_diff)
        logger.info("  Mask stats (non-verified): avg ratio=%.1fx, mask_lines=%.1f, diff_lines=%.1f",
                     nv_ratio, nv_mask, nv_diff)

        logger.info("  Iteration %d summary: input=%d, verified=%d, unverified=%d, "
                     "failed=%d, cost=$%.2f  |  cumulative verified=%d/%d, cost=$%.2f",
                     iter_id + 1, iter_input, newly_verified, len(remaining_instance_ids),
                     total_failed, iter_cost, total_verified, total_instances, total_cost)

        if not remaining_instance_ids and not failed_instances:
            logger.info("  All instances resolved, stopping.")
            break

        pending_instance_ids = remaining_instance_ids + failed_instances
        if not debug:
            mask.remove_results(pending_instance_ids)
            problem_gen.remove_results(pending_instance_ids)
            verifier.remove_results(pending_instance_ids)
        logger.info("  Retrying %d instances (%d unverified + %d failed).",
                     len(pending_instance_ids), len(remaining_instance_ids), total_failed)

    task_dataset = load_file(task_dataset_path)
    task_dataset = [data_record for data_record in task_dataset if "task_patch" in data_record]
    save_file(task_dataset, task_dataset_path)
    logger.info("=== Final: %d / %d tasks successfully created  |  total cost=$%.2f ===",
                len(task_dataset), total_instances, total_cost)
    logger.info("Logs saved to %s", module_utils._log_dir)
    print(f"Task dataset saved to {task_dataset_path}.")
    
def get_task_stats(task_dataset_path: Path, stats_path: Path):
    task_dataset = load_file(task_dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}
    for data_record in task_dataset:
        num_files, num_lines = len_patch(data_record["mask_patch"])
        stats[data_record["instance_id"]] = {
            "num_files_edited": num_files,
            "num_lines_edited": num_lines,
        }
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--max_iters',
        type=int,
        default=None,
        help='Maximum number of iterations'
    )
    parser.add_argument(
        '--start_iter',
        type=int,
        default=0,
        help='Iteration index to start from (0-based)'
    )
    parser.add_argument(
        '--preview',
        type=int,
        default=0,
        help='Number of tasks to preview after generation'
    )
    parser.add_argument(
        '--require_test',
        type=json.loads,
        default=True,
        help='Require repo-provided tests (default True); false uses the synthesized-test path.'
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        '--run_id',
        type=str,
        default='default',
        help='Run ID for output subdirectory (datasets/<run_id>/...)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Debug mode: default max_iters=1, keep agent trajectories.'
    )
    args = parser.parse_args()

    adaptive_gen_log_dir = get_log_dir(args.run_id, "adaptive_gen")
    init_loggers(adaptive_gen_log_dir)

    processed_dataset_path = get_dataset_path('processed_dataset', args.run_id)
    task_dataset_path = get_dataset_path('task_dataset', args.run_id)
    coverage_report_path = get_dataset_path('coverage_report', args.run_id)
    stats_path = get_dataset_path('stats', args.run_id)
    examples_path = get_dataset_path('examples', args.run_id)

    adaptive_task_gen(
        processed_dataset_path=processed_dataset_path,
        task_dataset_path=task_dataset_path,
        coverage_report_path=coverage_report_path,
        instance_ids=args.instance_ids,
        max_iters=args.max_iters,
        start_iter=args.start_iter,
        require_test=args.require_test,
        debug=args.debug,
    )
    get_task_stats(
        task_dataset_path=task_dataset_path,
        stats_path=stats_path
    )

    if args.preview > 0:
        task_dataset = load_file(task_dataset_path)
        samples = random.sample(task_dataset, min(args.preview, len(task_dataset)))
        for data_record in samples:
            dump_task(data_record, examples_path)