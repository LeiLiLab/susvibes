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
from tqdm import tqdm
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker
import docker.errors

from susvibes.core.constants import *
from susvibes.curate.constants import get_log_dir, LOGS_PARSER_MODEL, get_dataset_path
from susvibes.core.env import Deployment, Env
from susvibes.core.logs import LogsHandler, get_llm_cost, reset_llm_cost
from susvibes.curate.validate.constants import LOG_INSTANCE, LOG_TEST_OUTPUT, LOG_TIMEOUT, LOG_SUMMARY
from susvibes.curate.validate.utils import build_clean_eval_deployment
from susvibes.curate.utils import get_summary, print_summary
from susvibes.core.utils import load_file, save_file, get_image_name, setup_instance_logger, parse_instance_id, get_env_specs, save_env_specs, Route

docker_client = docker.from_env()

TEST_RUNS = ["base", "rollback", "base_no_test", "rollback_with_test", "task"]


def run_test_suite_multi(
    env: Env,
    data_record: dict,
    flags: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
    from_base_no_test_image: bool = False,
) -> tuple[list, list]:
    """Run tests in the environment; collect and cache each run's logs and timeout flag.
    Returns (test_logs_list, timed_out_list); classification is done by the caller.

    When `from_base_no_test_image` is True, the env's image is expected to start
    at the base_no_test commit (secure code without the dataset's test_patch),
    so the patch lists are computed relative to that baseline."""
    logger.info(f"Running tests in environment deployment {env.deployment.image.tags[0]}...")
    sec = data_record["security_patch"]
    test = data_record["test_patch"]
    if from_base_no_test_image:
        runs_list = [
            [(test, {})],                                                 # base
            [(sec, {"reverse": True})],                                   # rollback
            [],                                                           # base_no_test
            [(sec, {"reverse": True}), (test, {})],                       # rollback_with_test
            [(sec, {"reverse": True}), (data_record["mask_patch"], {})],  # task: rollback to vulnerable, then mask feature
        ]
    else:
        runs_list = [
            [], [(sec, {"reverse": True}), (test, {"reverse": True})],
            [(test, {"reverse": True})], [(sec, {"reverse": True})],
            [(data_record["task_patch"], {})]
        ]
    timeout_path = log_dir / LOG_TIMEOUT
    timed_out_dict = load_file(timeout_path) if timeout_path.exists() else {}

    test_logs_list, timed_out_list = [], []
    for run_patches, run_name in zip(runs_list, TEST_RUNS):
        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        if not force and test_output_path.exists():
            logger.info("Container logs found; reusing.")
            test_logs = load_file(test_output_path)
            timed_out = timed_out_dict.get(run_name, False)
        else:
            try:
                deployment: Deployment = env.build_instance_deployment(
                    base_commit=data_record["base_commit"],
                    patches=run_patches,
                    logger=logger,
                )
            except docker.errors.BuildError as e:
                msg = f"Failed to build instance deployment: {e}"
                logger.error(msg)
                raise RuntimeError(msg)
            try:
                deployment.create_container(command=Route.route_test_cmd(flags, run_name),
                    mem_limit=ContainerLimits.MEM_LIMIT, cpu_limit=ContainerLimits.CPU_LIMIT)
            except docker.errors.APIError as e:
                msg = f"Failed to create container: {e}"
                logger.error(msg)
                raise RuntimeError(msg)
            try:
                test_logs, timed_out = deployment.run_with_timeout()
            except docker.errors.APIError as e:
                msg = f"Failed to start container: {e}"
                logger.error(msg)
                raise RuntimeError(msg)
            test_output_path.parent.mkdir(parents=True, exist_ok=True)
            save_file(test_logs, test_output_path)
        test_logs_list.append(test_logs)
        timed_out_list.append(timed_out)

    save_file(dict(zip(TEST_RUNS, timed_out_list)), timeout_path)
    return test_logs_list, timed_out_list

def validate_test_breaks(
    env: Env,
    test_logs_list: list,
    timed_out_list: list,
    flags: dict,
    logger: logging.Logger
) -> tuple:
    """
    Verify the task on security and functional test breaks.
    Raises RuntimeError on failure; returns (expected_pf, stats) on success.
    """
    test_pf_list = [env.handle_test_logs(test_logs, timed_out, logger,
        kind=Route.route_logs_kind(flags, run_name))
        for run_name, test_logs, timed_out in zip(TEST_RUNS, test_logs_list, timed_out_list)]

    base_pf, rollback_pf, base_no_test_pf, rollback_with_test_pf, task_pf = test_pf_list

    test_symb_res_errs_list = [LogsHandler.count_symb_res_errors(test_logs) for test_logs in test_logs_list]
    _, rollback_sre, _, rollback_with_test_sre, _ = test_symb_res_errs_list
    if rollback_with_test_pf.completed() and rollback_with_test_sre > rollback_sre:
        msg = "Failed to verify task on symbol resolution errors: rollback-{}, rollback_with_test-{}".format(
            rollback_sre, rollback_with_test_sre)
        logger.error(msg)
        raise RuntimeError(msg)
    stats = {}
    is_broken = rollback_with_test_pf.breaks_more_than(rollback_pf)
    is_repaired = base_pf.excess_breaks_over(base_no_test_pf) \
        < rollback_with_test_pf.excess_breaks_over(rollback_pf)
    if not (is_broken and is_repaired) or base_no_test_pf.breaks_more_than(rollback_pf):
        msg = "Failed to verify task on sec test breaks: rollback-{}, base_no_test-{}, rollback_with_test-{}, base-{}".format(
            rollback_pf, base_no_test_pf, rollback_with_test_pf, base_pf)
        logger.error(msg)
        raise RuntimeError(msg)
    stats["num_sec_tests"] = rollback_with_test_pf.count_excess_breaks_over(rollback_pf) \
        - base_pf.count_excess_breaks_over(base_no_test_pf)

    if not task_pf.breaks_more_than(rollback_pf):
        msg = "Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_pf, task_pf)
        logger.error(msg)
        raise RuntimeError(msg)

    expected_pf = {
        "func": rollback_pf.get_raw(),
        "sec": base_pf.excess_breaks_over(base_no_test_pf, to_raw=True)
    }
    stats["num_func_tests"] = task_pf.count_excess_breaks_over(rollback_pf)
    return (expected_pf, stats)


def validate_single(
    data_record: dict,
    instance_stats: dict,
    env_spec: dict,
    validate_log_dir: Path,
    force: bool = False,
    from_base_no_test_image: bool = False,
) -> tuple[dict | None, str | None]:
    """Validate a single instance via test execution.
    Returns (env_spec, None) on success, (None, failure_reason) on failure.
    On success, data_record is updated in-place with expected_pf and image_name."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_dir = validate_log_dir / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Validating environment for {instance_id}...")

    image_kind = "base_no_test_image" if from_base_no_test_image else "env_image"
    image_name = data_record.get(f"{image_kind}_name")
    if not image_name:
        msg = f"{image_kind}_name missing from dataset."
        logger.error(msg)
        raise RuntimeError(msg)
    try:
        env = Env(
            logger=logger,
            project=project,
            image_name=image_name,
            **env_spec
        )
    except (docker.errors.ImageNotFound, docker.errors.NotFound):
        msg = f"Env image not found: {image_name}"
        logger.error(msg)
        raise RuntimeError(msg)
    flags = {}
    try:
        test_logs_list, timed_out_list = run_test_suite_multi(
            env, data_record, flags, log_dir, logger, force,
            from_base_no_test_image=from_base_no_test_image,
        )
        env.logs_handler = LogsHandler.get_by_kind("count", env.logs_handler,
            test_logs_list=test_logs_list, timed_out_list=timed_out_list,
            model=LOGS_PARSER_MODEL, log_dir=log_dir, logger=logger, ordering_checks=[(3, 0), (4, 1)],
            allow_timeout=lambda id: id >= 3, allow_startup_error=lambda id: id == 4,
            force=force)
        expected_pf, test_stats = validate_test_breaks(env, test_logs_list, timed_out_list, flags, logger=logger)
    except RuntimeError as e:
        return None, str(e)
    logger.info("Task verified, expected_pf-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_pf, test_stats["num_sec_tests"], test_stats["num_func_tests"]))

    logger.info(f"Building task deployment for {instance_id}...")
    if from_base_no_test_image:
        eval_patches = [(data_record["security_patch"], {"reverse": True}), (data_record["mask_patch"], {})]
    else:
        eval_patches = [(data_record["task_patch"], {})]
    try:
        task_deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches=eval_patches,
            logger=logger
        )
    except docker.errors.BuildError as e:
        msg = f"Failed to build task instance deployment: {e}"
        logger.error(msg)
        return None, msg
    task_image_name = get_image_name(f"task_{instance_id}")
    assert task_deployment.image.tag(task_image_name)

    logger.info(f"Building evaluation deployment for {instance_id}...")
    eval_image_name = get_image_name(f"eval_{instance_id}")
    try:
        build_clean_eval_deployment(task_image_name, eval_image_name, logger)
    except docker.errors.BuildError as e:
        msg = f"Failed to build eval deployment: {e}"
        logger.error(msg)
        return None, msg

    env_spec["logs_handler"] = env.logs_handler
    data_record["expected_pf"] = expected_pf
    data_record["flags"] = flags
    data_record["image_name"] = eval_image_name
    instance_stats.update(test_stats)
    return env_spec, None


def validate_threadpool(
    run_id: str,
    dataset: list,
    stats: dict,
    max_workers: int,
    validate_log_dir: Path,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
    from_base_no_test_image: bool = False,
):
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    candidate_ids = set(dataset_by_id.keys()) & set(env_specs.keys())
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)

    if from_base_no_test_image:
        use_bnt = set(candidate_ids)
    else:
        use_bnt = set()
        for iid in candidate_ids:
            bnt_name = dataset_by_id[iid].get("base_no_test_image_name")
            if not bnt_name:
                continue
            try:
                Deployment.collect_image(image_name=bnt_name)
                use_bnt.add(iid)
            except docker.errors.ImageNotFound:
                pass
        if use_bnt:
            print(f"\n{len(use_bnt)}/{len(candidate_ids)} candidate instances have base_no_test_image available locally.")
            missing = len(candidate_ids) - len(use_bnt)
            if missing:
                print(f"  (the other {missing} will continue to use env_image.)")
            answer = input("Switch these to --from_base_no_test_image? [Y/n] ").strip().lower()
            if answer not in ('', 'y'):
                use_bnt = set()

    reset_llm_cost()
    succeeded, failed = [], {}
    env_specs_path = None
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                validate_single,
                dataset_by_id[instance_id],
                stats.setdefault(instance_id, {}),
                env_specs[instance_id],
                validate_log_dir,
                force=force,
                from_base_no_test_image=(instance_id in use_bnt),
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
                    f"{len(succeeded)} succeeded, {len(failed)} failed"
                )
                if save_specs:
                    env_specs_path = save_env_specs("logs_handler", env_specs, run_id)

    summary = get_summary(succeeded, failed)
    summary_path = validate_log_dir / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    print(f"LogsHandler LLM cost: ${get_llm_cost():.4f}")
    if env_specs_path:
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
        "--from_base_no_test_image",
        action="store_true",
        help="Use the per-instance base_no_test image as the starting point.",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path('dataset', args.run_id)
    stats_path = get_dataset_path('stats', args.run_id)
    validate_log_dir = get_log_dir(args.run_id, "validate")

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    validate_threadpool(
        args.run_id, dataset, stats, args.max_workers, validate_log_dir,
        force=args.force,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
        from_base_no_test_image=args.from_base_no_test_image,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
