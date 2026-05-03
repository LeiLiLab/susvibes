"""
Purpose: Validate task instances by running the test suite, synthesizing a logs parser,
and verifying the expected test break patterns (security + functional).

python -m susvibes.curate.validate.with_test \
    --max_workers 5 \
    --run_id playground
"""

import argparse
import json
import logging
import docker.errors
from tqdm import tqdm
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.constants import *
from susvibes.curate.constants import ENV_SETUP_LOG_DIR, LOGS_PARSER_MODEL, get_path
from susvibes.env import Deployment, Env
from susvibes.env_specs import TestStatus
from susvibes.curate.validate.logs_parser import get_logs_parser
from susvibes.curate.validate.utils import get_validate_summary, print_summary
from susvibes.utils import load_file, save_file, get_image_name, setup_instance_logger, parse_instance_id
from susvibes.curate.utils import (
    reverse_patch,
)

LOG_INSTANCE = "validate.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_TEST_STATUSES = "test_statuses.json"
LOG_SUMMARY = "summary.json"

ENV_SETUP_RUNS = ["base", "rollback", "sec_patch", "sec_test", "task"]


def run_test_suite_multi(
    env: Env,
    data_record: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False
) -> list:
    """Run tests in the environment and return test logs for multiple patches."""
    logger.info(f"Running tests in environment deployment {env.deployment.image.tags[0]}...")
    rev_security = reverse_patch(data_record["security_patch"])
    rev_test = reverse_patch(data_record["test_patch"])
    runs_list = [
        (), (rev_security, rev_test),
        (rev_test,), (rev_security,),
        (data_record["task_patch"],)
    ]
    allow_timeout = lambda id: id >= 3
    allow_startup_error = lambda id: id == 4

    test_logs_list, test_status_dict = [], {}
    test_statuses_path = log_dir / LOG_TEST_STATUSES
    for id, (run_patches, run_name) in enumerate(zip(runs_list, ENV_SETUP_RUNS)):
        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        test_status_dict_from_log = {}
        if test_statuses_path.exists():
            test_status_dict_from_log = load_file(test_statuses_path)

        with_log = test_output_path.exists() and run_name in test_status_dict_from_log
        if not force and with_log:
            logger.info("Container logs found; reusing.")
            test_logs = load_file(test_output_path)
            test_logs_list.append(test_logs)
            test_status_dict[run_name] = test_status_dict_from_log[run_name]

            for k, v in test_status_dict_from_log.items():
                if k not in test_status_dict:
                    test_status_dict[k] = v
        else:
            try:
                deployment: Deployment = env.build_instance_deployment(
                    base_commit=data_record["base_commit"],
                    patches={"post_install": run_patches},
                    logger=logger,
                )
            except docker.errors.BuildError as e:
                msg = f"Failed to build instance deployment: {e}"
                logger.error(msg)
                raise RuntimeError(msg)
            deployment.create_container(mem_limit=CONTAINER_MEM_LIMIT, cpu_limit=CONTAINER_CPU_LIMIT)
            test_logs, timed_out = deployment.run_with_timeout()
            test_status = env.get_test_status(test_logs, timed_out)
            test_logs_list.append(test_logs)
            test_status_dict[run_name] = test_status

            test_output_path.parent.mkdir(parents=True, exist_ok=True)
            save_file(test_logs, test_output_path)
            save_file(test_status_dict, test_statuses_path)

        if test_status_dict[run_name] == TestStatus.TIMEOUT.value \
            and not allow_timeout(id):
            msg = "Failed to run tests because of critical timeout."
            logger.error(msg)
            raise RuntimeError(msg)
        if test_status_dict[run_name] == TestStatus.STARTUP_ERROR.value \
            and not allow_startup_error(id):
            msg = "Failed to run tests because of critical startup error."
            logger.error(msg)
            raise RuntimeError(msg)

    test_statuses = [test_status_dict[run_name] for run_name in ENV_SETUP_RUNS]
    return test_logs_list, test_statuses

def validate_test_breaks(
    env: Env,
    test_logs_list: list,
    test_statuses: list,
    logger: logging.Logger
) -> tuple:
    """
    Verify the task on security and functional test breaks.
    Raises RuntimeError on failure; returns (expected_failures, stats) on success.
    """
    test_result_list, test_failures_list = [], []
    for logs, status in zip(test_logs_list, test_statuses):
        if not status:
            test_result_list.append({})
            continue
        try:
            test_result = env.parse_test_logs(logs, logger)
            test_result_list.append(test_result)
        except Exception as e:
            msg = "Failed to parse test logs."
            logger.error(msg)
            raise RuntimeError(msg)
        test_failures_list.append(env.get_test_failures(test_result))

    base_tf, rollback_tf, sec_patch_tf, sec_test_tf, task_tf = test_failures_list
    test_completed_list = [ts == TestStatus.COMPLETION.value for ts in test_statuses]
    _, _, _, sec_test_completed, task_completed = test_completed_list

    test_symbres_errs_list = []
    for logs in test_logs_list:
        test_symbres_errs_list.append(env.get_symbol_resolution_errors(logs))
    _, rollback_te, _, sec_test_te, _ = test_symbres_errs_list
    if sec_test_completed and sec_test_te > rollback_te:
        msg = "Failed to verify task on symbol resolution errors: rollback-{}, sec_test-{}".format(
            rollback_te, sec_test_te)
        logger.error(msg)
        raise RuntimeError(msg)
    stats = {}
    extra_pass = rollback_tf - sec_patch_tf
    is_broken = not sec_test_completed or sec_test_tf > rollback_tf
    is_repaired = not sec_test_completed or base_tf < sec_test_tf - extra_pass
    if not (is_broken and is_repaired) or extra_pass < 0:
        msg = "Failed to verify task on sec test breaks: rollback-{}, sec_patch-{}, sec_test-{}, base-{}".format(
            rollback_tf, sec_patch_tf, sec_test_tf if sec_test_completed else "N/A", base_tf)
        logger.error(msg)
        raise RuntimeError(msg)
    stats["num_sec_tests"] = sec_test_tf - extra_pass - base_tf \
        if sec_test_completed else -1

    is_broken = not task_completed or task_tf > rollback_tf
    if not is_broken:
        msg = "Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_tf, task_tf if task_completed else "N/A")
        logger.error(msg)
        raise RuntimeError(msg)

    expected_failures = {
        "func": rollback_tf,
        "sec": base_tf - sec_patch_tf
    }
    stats["num_func_tests"] = task_tf - rollback_tf \
        if task_completed else -1
    return (expected_failures, stats)


def validate_single(
    data_record: dict,
    instance_stats: dict,
    env_spec: dict,
    env_setup_log_dir: Path,
    force: bool = False,
    from_existing_specs: bool = False,
) -> tuple[dict | None, str | None]:
    """Validate a single instance via test execution.
    Returns (env_spec, None) on success, (None, failure_reason) on failure.
    On success, data_record is updated in-place with expected_failures and image_name."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_dir = env_setup_log_dir / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Validating environment for {instance_id}...")

    env = Env(
        logger=logger,
        project=project,
        image_name=data_record["env_image_name"],
        **env_spec
    )
    try:
        test_logs_list, test_statuses = run_test_suite_multi(env, data_record, log_dir, logger, force)
        if from_existing_specs and env_spec.get("logs_parser"):
            logger.info("Reusing logs parser from env_spec.")
        else:
            get_logs_parser(env, test_logs_list, test_statuses,
                log_dir=log_dir, logger=logger, model=LOGS_PARSER_MODEL,
                ordering_checks=[(3, 0), (4, 1)], force=force)
        expected_failures, test_stats = validate_test_breaks(env, test_logs_list, test_statuses, logger=logger)
    except RuntimeError as e:
        return None, str(e)
    logger.info("Task verified successfully, expected_failures-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_failures, test_stats["num_sec_tests"], test_stats["num_func_tests"]))

    logger.info(f"Building evaluation image for {instance_id}...")
    task_deployment = env.build_instance_deployment(
        base_commit=data_record["base_commit"],
        patches={"post_install": (data_record["task_patch"],)},
        logger=logger
    )
    eval_image_name = get_image_name(f"eval_{instance_id.lower()}")
    assert task_deployment.image.tag(eval_image_name)

    env_spec["logs_parser"] = env.logs_parser
    data_record["expected_failures"] = expected_failures
    data_record["image_name"] = eval_image_name
    instance_stats.update(test_stats)
    return env_spec, None


def validate_threadpool(
    dataset: list,
    stats: dict,
    max_workers: int,
    env_specs_path: Path,
    env_setup_log_dir: Path,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
    from_existing_specs: bool = False,
):
    env_specs = load_file(env_specs_path) if env_specs_path.exists() else {}
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    candidate_ids = set(dataset_by_id.keys()) & set(env_specs.keys())
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)
    if from_existing_specs:
        candidate_ids = {iid for iid in candidate_ids if env_specs[iid].get("logs_parser")}

    succeeded, failed = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                validate_single,
                dataset_by_id[instance_id],
                stats.get(instance_id, {}),
                env_specs[instance_id],
                env_setup_log_dir,
                force=force,
                from_existing_specs=from_existing_specs,
            ): instance_id
            for instance_id in candidate_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Validating [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    updated_spec, reason = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if updated_spec:
                    env_specs[instance_id] = updated_spec
                    succeeded.append(instance_id)
                else:
                    failed[instance_id] = reason
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} ran successfully, {len(failed)} failed"
                )
                if save_specs:
                    save_file(env_specs, env_specs_path)

    summary = get_validate_summary(succeeded, failed)
    summary_path = env_setup_log_dir / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    if save_specs:
        print(f"Environments saved to {env_specs_path}.")
    print(f"Summary saved to {summary_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate task instances via test execution.")
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for validation.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run the validation.",
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
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    parser.add_argument(
        "--from_existing_specs",
        action="store_true",
        help="Reuse the logs_parser stored in env_specs instead of re-synthesizing via LLM.",
    )
    args = parser.parse_args()

    dataset_path = get_path('dataset', args.run_id)
    stats_path = get_path('stats', args.run_id)
    env_specs_path = get_env_spec_path('components', args.run_id)
    env_setup_log_dir = ENV_SETUP_LOG_DIR / args.run_id

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    validate_threadpool(
        dataset, stats, args.max_workers, env_specs_path, env_setup_log_dir,
        force=args.force,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
        from_existing_specs=args.from_existing_specs,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
