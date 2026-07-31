"""
Purpose: Validate task instances where security tests come from a separate synthesis
agent rather than from the repo's own test commits. The repo's test suite is still
used for functional regression checks.

python -m susvibes.curate.validate.no_test \
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

from susvibes.core.constants import *
from susvibes.curate.constants import get_log_dir, LOGS_PARSER_MODEL
from susvibes.core.env import Deployment, Env
from susvibes.core.logs import LogsHandler, get_llm_cost, reset_llm_cost
from susvibes.curate.validate.constants import LOG_INSTANCE, LOG_TEST_OUTPUT, LOG_TIMEOUT, LOG_SUMMARY
from susvibes.curate.validate.utils import build_clean_eval_deployment
from susvibes.curate.utils import get_summary, print_summary
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.utils import load_file, save_file, get_image_name, setup_instance_logger, parse_instance_id, filter_binary_files, get_env_specs, save_env_specs, Route

REPO_TEST_RUNS = ["base", "rollback", "task"]
GEN_SEC_TEST_RUNS = ["rollback_with_gen_test", "base_with_gen_test"]


def run_repo_test_suite_multi(
    env: Env,
    data_record: dict,
    flags: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
) -> tuple[list, list]:
    """Run 3 repo test variants: base, rollback (vulnerable), task (masked).
    Collect and cache each run's logs and timeout flag; returns (test_logs_list, timed_out_list).
    Classification (status / critical-abort) is done by the caller."""
    logger.info(f"Running repo tests in environment deployment {env.deployment.image.tags[0]}...")
    runs_list = [
        [],                                                       # base: original (secure)
        [(data_record["security_patch"], {"reverse": True})],     # rollback: reverse security fix (vulnerable)
        [(data_record["task_patch"], {})],                        # task: apply mask
    ]
    timeout_path = log_dir / LOG_TIMEOUT
    timed_out_dict = load_file(timeout_path) if timeout_path.exists() else {}

    test_logs_list, timed_out_list = [], []
    for run_patches, run_name in zip(runs_list, REPO_TEST_RUNS):
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

    timed_out_dict.update(zip(REPO_TEST_RUNS, timed_out_list))
    save_file(timed_out_dict, timeout_path)
    return test_logs_list, timed_out_list


def validate_repo_test_breaks(
    env: Env,
    test_logs_list: list,
    timed_out_list: list,
    flags: dict,
    logger: logging.Logger
) -> tuple:
    """
    Verify the task on functional test breaks.
    Raises RuntimeError on failure; returns (expected_pf, stats) on success.
    """
    test_pf_list = [env.handle_test_logs(test_logs, timed_out, logger,
        kind=Route.route_logs_kind(flags, run_name))
        for run_name, test_logs, timed_out in zip(REPO_TEST_RUNS, test_logs_list, timed_out_list)]

    base_pf, rollback_pf, task_pf = test_pf_list

    if base_pf.breaks_more_than(rollback_pf):
        msg = "Failed to verify task on functional test baseline: base ({}) breaks more than rollback ({})".format(
            base_pf, rollback_pf)
        logger.error(msg)
        raise RuntimeError(msg)

    if not task_pf.breaks_more_than(rollback_pf):
        msg = "Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_pf, task_pf)
        logger.error(msg)
        raise RuntimeError(msg)

    expected_pf = {
        "func": rollback_pf.get_raw(),
    }
    stats = {}
    stats["num_func_tests"] = task_pf.count_excess_breaks_over(rollback_pf)
    logger.info("Repo tests verified: base={}, rollback={}, task={}".format(
        base_pf, rollback_pf, task_pf))
    return (expected_pf, stats)


def run_gen_sec_test(
    env: Env,
    data_record: dict,
    flags: dict,
    run_name: str,
    patches: list[tuple[str, dict]],
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
) -> tuple[str, bool]:
    """Run one generated-sec-test configuration; cache and return (test_logs, timed_out).
    Classification is done by the caller."""
    test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
    timeout_path = log_dir / LOG_TIMEOUT
    timed_out_dict = load_file(timeout_path) if timeout_path.exists() else {}
    if not force and test_output_path.exists():
        logger.info("Gen sec test container logs found; reusing.")
        return load_file(test_output_path), timed_out_dict.get(run_name, False)

    try:
        deployment: Deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches=patches,
            logger=logger,
        )
    except docker.errors.BuildError as e:
        msg = f"Failed to build gen sec test instance deployment: {e}"
        logger.error(msg)
        raise RuntimeError(msg)

    try:
        deployment.create_container(
            command=Route.route_test_cmd(flags, run_name),
            mem_limit=ContainerLimits.MEM_LIMIT,
            cpu_limit=ContainerLimits.CPU_LIMIT,
        )
    except docker.errors.APIError as e:
        msg = f"Failed to create gen sec test container: {e}"
        logger.error(msg)
        raise RuntimeError(msg)
    try:
        test_logs, timed_out = deployment.run_with_timeout()
    except docker.errors.APIError as e:
        msg = f"Failed to start gen sec test container: {e}"
        logger.error(msg)
        raise RuntimeError(msg)

    test_output_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(test_logs, test_output_path)
    timed_out_dict[run_name] = timed_out
    save_file(timed_out_dict, timeout_path)
    return test_logs, timed_out


def validate_gen_sec_test_breaks(
    env: Env,
    data_record: dict,
    test_patch: str,
    flags: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
) -> tuple:
    """
    Run the generated sec tests (rollback_with_gen_test, base_with_gen_test), then verify the
    vulnerable run breaks at least one case the secure run passes.
    Raises RuntimeError on failure; returns (expected_pf, stats) on success — expected_pf carries
    the "sec" key (the per-case pass map of the secure run, what an eval submission must pass).
    """
    runs_patches = [
        [(data_record["security_patch"], {"reverse": True}), (test_patch, {})],  # rollback_with_gen_test: vulnerable + agent test
        [(test_patch, {})],                                                       # base_with_gen_test: secure + agent test
    ]
    test_pf_list = []
    for run_name, patches in zip(GEN_SEC_TEST_RUNS, runs_patches):
        test_logs, timed_out = run_gen_sec_test(env, data_record, flags, run_name, patches,
            log_dir, logger, force=force)
        test_pf_list.append(env.handle_test_logs(test_logs, timed_out, logger,
            kind=Route.route_logs_kind(flags, run_name)))
    vuln_pf, gold_pf = test_pf_list

    if not vuln_pf.breaks_more_than(gold_pf):
        msg = "Failed to verify task on gen sec test breaks: no distinguishing tests (vuln fail + gold pass). " \
            "vuln={}, gold={}".format(vuln_pf, gold_pf)
        logger.error(msg)
        raise RuntimeError(msg)

    expected_pf = {"sec": vuln_pf.excess_breaks_over(gold_pf, to_raw=True)}
    stats = {}
    stats["num_sec_tests"] = vuln_pf.count_excess_breaks_over(gold_pf)
    logger.info(f"Sec tests verified: {stats['num_sec_tests']} distinguishing.")
    return (expected_pf, stats)


def validate_single(
    data_record: dict,
    instance_stats: dict,
    env_spec: dict,
    test_patch: str,
    validate_log_dir: Path,
    force: bool = False,
) -> tuple[dict | None, str | None]:
    """Validate a single instance in no_require_test mode.
    Returns (env_spec, None) on success, (None, failure_reason) on failure.
    On success, data_record is updated in-place."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_dir = validate_log_dir / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Validating environment (no_require_test) for {instance_id}...")

    # Drop any prior-run verdict so a failed re-validation leaves nothing behind
    # (wrap_up keeps an instance solely on expected_pf being present).
    for key in ("expected_pf", "flags", "image_name"):
        data_record.pop(key, None)

    if not test_patch.strip():
        msg = "Empty model_patch."
        logger.error(msg)
        return None, msg

    image_name = data_record.get("env_image_name")
    if not image_name:
        msg = "env_image_name missing from dataset."
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

    flags = {"gen_test": True}
    try:
        test_logs_list, timed_out_list = run_repo_test_suite_multi(
            env, data_record, flags, log_dir, logger, force)
        env.logs_handler = LogsHandler.get_by_kind("count", env.logs_handler,
            test_logs_list=test_logs_list, timed_out_list=timed_out_list,
            model=LOGS_PARSER_MODEL, log_dir=log_dir, logger=logger, ordering_checks=[(2, 1)],
            allow_timeout=lambda id: id == 2, allow_startup_error=lambda id: id == 2,
            require_failures=False, force=force)
        env.logs_handler = LogsHandler.get_by_kind("gen_sec", env.logs_handler, log_dir=log_dir)
        expected_pf, test_stats = validate_repo_test_breaks(
            env, test_logs_list, timed_out_list, flags, logger)
        gen_sec_expected_pf, gen_sec_stats = validate_gen_sec_test_breaks(
            env, data_record, test_patch, flags, log_dir, logger, force)
    except RuntimeError as e:
        return None, str(e)

    expected_pf.update(gen_sec_expected_pf)
    test_stats.update(gen_sec_stats)
    logger.info("Task verified, expected_pf-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_pf, test_stats["num_sec_tests"], test_stats["num_func_tests"]))

    logger.info(f"Building task deployment for {instance_id}...")
    try:
        task_deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches=[(data_record["task_patch"], {})],
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
    data_record["test_patch"] = test_patch
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
    test_patches: dict,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    candidate_ids = set(dataset_by_id.keys()) & set(env_specs.keys()) & set(test_patches.keys())
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)

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
                test_patches[instance_id],
                validate_log_dir,
                force=force,
            ): instance_id
            for instance_id in candidate_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Validating (no_test) [{max_workers} threads]") as pbar:
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
    parser = argparse.ArgumentParser(
        description="Validate task instances (no test mode) using synthesized security tests via test execution.")
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for validation.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run, ignoring cached test logs (re-run containers) and re-creating the logs handler.",
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
    args = parser.parse_args()

    dataset_path = get_dataset_path('env_dataset', args.run_id)
    stats_path = get_dataset_path('stats', args.run_id)
    validate_log_dir = get_log_dir(args.run_id, "validate")

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    # Load the test-synthesis agent's predictions from its output dir (test.gen's batch).
    predictions, _ = SWEAgentPort.after_completion(get_log_dir(args.run_id, "test", "gen"))
    test_patches = {pred["instance_id"]: filter_binary_files(pred.get("model_patch", "")) for pred in predictions}

    validate_threadpool(
        args.run_id, dataset, stats, args.max_workers, validate_log_dir,
        test_patches,
        force=args.force,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
