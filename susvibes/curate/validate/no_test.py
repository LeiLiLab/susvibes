"""
Purpose: Validate task instances where security tests come from a separate synthesis
agent rather than from the repo's own test commits. The repo's test suite is still
used for functional regression checks.

python -m susvibes.curate.validate.no_test \
    --test_agent_output_dir <path_to_agent_output> \
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
from susvibes.curate.constants import get_log_dir, LOGS_PARSER_MODEL, get_path
from susvibes.env import Deployment, Env
from susvibes.curate.validate.logs import get_logs_parser, get_logs_checker, get_llm_cost, reset_llm_cost
from susvibes.curate.validate.utils import build_clean_eval_deployment
from susvibes.curate.utils import get_summary, print_summary
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.utils import load_file, save_file, get_image_name, setup_instance_logger, parse_instance_id, filter_binary_files

LOG_INSTANCE = "validate.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_TEST_STATUSES = "test_statuses.json"
LOG_SEC_OUTPUT = "sec_outputs/{}.txt"
LOG_SEC_RESULTS = "sec_results/{}.json"
LOG_SUMMARY = "summary.json"

REPO_TEST_RUNS = ["base", "rollback", "task"]
SEC_TEST_RUNS = ["rollback_with_test", "base_with_test"]
SEC_TEST_CMD = ["bash", "-c", "bash sectests.sh ; cat secresults.json"]


def run_repo_test_suite_multi(
    env: Env,
    data_record: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
    from_existing_specs: bool = False,
    env_spec: dict = None,
) -> tuple[list, list]:
    """Run 3 repo test variants: base, rollback (vulnerable), task (masked).
    Collects each run's logs, then ensures a per-instance logs_checker exists (reused
    from env_spec or synthesized from these logs) BEFORE classifying — the checker is
    what decides startup_error vs completion. Finally applies the critical-abort checks."""
    logger.info(f"Running repo tests in environment deployment {env.deployment.image.tags[0]}...")
    runs_list = [
        [],                                                       # base: original (secure)
        [(data_record["security_patch"], {"reverse": True})],     # rollback: reverse security fix (vulnerable)
        [(data_record["task_patch"], {})],                        # task: apply mask
    ]
    allow_timeout = lambda id: id == 2        # only task may timeout
    allow_startup_error = lambda id: id == 2  # only task may have startup error

    test_statuses_path = log_dir / LOG_TEST_STATUSES
    cached_statuses = load_file(test_statuses_path) if test_statuses_path.exists() else {}

    test_logs_list, timed_out_list = [], []
    for run_patches, run_name in zip(runs_list, REPO_TEST_RUNS):
        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        if not force and test_output_path.exists():
            logger.info("Container logs found; reusing.")
            test_logs = load_file(test_output_path)
            timed_out = cached_statuses.get(run_name) == TestStatus.TIMEOUT
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
                deployment.create_container(mem_limit=ContainerLimits.MEM_LIMIT, cpu_limit=ContainerLimits.CPU_LIMIT)
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

    # The logs_checker must exist before classification (it decides startup_error vs
    # completion). Reuse the stored one under --from_existing_specs, else synthesize.
    if from_existing_specs and env_spec is not None and "logs_checker" in env_spec:
        logger.info("Reusing logs checker from env_spec.")
    else:
        get_logs_checker(env, test_logs_list, model=LOGS_PARSER_MODEL,
            log_dir=log_dir, logger=logger, force=force)

    test_status_dict = {run_name: env.check_test_logs(test_logs, timed_out)
        for run_name, test_logs, timed_out in zip(REPO_TEST_RUNS, test_logs_list, timed_out_list)}
    save_file(test_status_dict, test_statuses_path)

    for id, run_name in enumerate(REPO_TEST_RUNS):
        if test_status_dict[run_name] == TestStatus.TIMEOUT \
            and not allow_timeout(id):
            msg = "Failed to run tests because of critical timeout."
            logger.error(msg)
            raise RuntimeError(msg)
        if test_status_dict[run_name] == TestStatus.STARTUP_ERROR \
            and not allow_startup_error(id):
            msg = "Failed to run tests because of critical startup error."
            logger.error(msg)
            raise RuntimeError(msg)

    test_statuses = [test_status_dict[run_name] for run_name in REPO_TEST_RUNS]
    return test_logs_list, test_statuses


def validate_repo_test_breaks(
    env: Env,
    test_logs_list: list,
    test_statuses: list,
    logger: logging.Logger
) -> tuple:
    """
    Verify the task on functional test breaks.
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

    base_tf, rollback_tf, task_tf = test_failures_list
    test_completed_list = [ts == TestStatus.COMPLETION for ts in test_statuses]
    _, _, task_completed = test_completed_list

    if base_tf > rollback_tf:
        msg = "Failed to verify task on functional test baseline: base_tf ({}) > rollback_tf ({})".format(
            base_tf, rollback_tf)
        logger.error(msg)
        raise RuntimeError(msg)

    is_broken = not task_completed or task_tf > rollback_tf
    if not is_broken:
        msg = "Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_tf, task_tf if task_completed else "N/A")
        logger.error(msg)
        raise RuntimeError(msg)

    expected_failures = {
        "func": rollback_tf,
    }
    stats = {}
    stats["num_func_tests"] = task_tf - rollback_tf \
        if task_completed else -1
    logger.info("Repo tests verified: base_tf={}, rollback_tf={}, task_tf={}".format(
        base_tf, rollback_tf, task_tf if task_completed else "N/A"))
    return (expected_failures, stats)


def run_sec_test(
    env: Env,
    data_record: dict,
    log_dir: Path,
    logger: logging.Logger,
    run_name: str,
    patches: list[tuple[str, dict]],
    force: bool = False,
) -> dict:
    """Run one sec test configuration. Raises RuntimeError on failure; returns secresults dict on success."""
    sec_output_path = log_dir / LOG_SEC_OUTPUT.format(run_name)
    sec_results_path = log_dir / LOG_SEC_RESULTS.format(run_name)

    if not force and sec_output_path.exists() and sec_results_path.exists():
        logger.info(f"Sec test {run_name} found; reusing.")
        return load_file(sec_results_path)

    try:
        deployment: Deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches=patches,
            logger=logger,
        )
    except docker.errors.BuildError as e:
        msg = f"Failed to build sec test instance deployment: {e}"
        logger.error(msg)
        raise RuntimeError(msg)

    try:
        deployment.create_container(
            command=SEC_TEST_CMD,
            mem_limit=ContainerLimits.MEM_LIMIT,
            cpu_limit=ContainerLimits.CPU_LIMIT,
        )
    except docker.errors.APIError as e:
        msg = f"Failed to create sec test container: {e}"
        logger.error(msg)
        raise RuntimeError(msg)
    try:
        test_logs, timed_out = deployment.run_with_timeout()
    except docker.errors.APIError as e:
        msg = f"Failed to start sec test container: {e}"
        logger.error(msg)
        raise RuntimeError(msg)

    sec_output_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(test_logs, sec_output_path)

    if timed_out:
        msg = "Failed to run tests because of sec test timeout."
        logger.error(msg)
        raise RuntimeError(msg)

    secresults = parse_secresults(test_logs)
    if secresults is None:
        msg = "Failed to parse sec test logs."
        logger.error(msg)
        raise RuntimeError(msg)

    sec_results_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(secresults, sec_results_path)
    return secresults


def parse_secresults(logs: str) -> dict | None:
    """Extract the last valid JSON object from container logs."""
    text = logs.rstrip()
    # Search backwards for the last closing brace
    end = text.rfind("}")
    if end == -1:
        return None
    # Find the matching opening brace
    depth = 0
    for i in range(end, -1, -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            depth -= 1
        if depth == 0:
            try:
                return json.loads(text[i:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def validate_sec_test_breaks(
    env: Env,
    data_record: dict,
    test_patch: str,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
) -> tuple:
    """
    Run rollback_with_test and base_with_test, verify at least one distinguishing test.
    Raises RuntimeError on failure; returns (stats, sec_test_names) on success.
    """
    # rollback_with_test: vulnerable implementation + agent test patch
    vuln_results = run_sec_test(
        env, data_record, log_dir, logger,
        run_name="rollback_with_test",
        patches=[(data_record["security_patch"], {"reverse": True}), (test_patch, {})],
        force=force,
    )

    # base_with_test: secure implementation + agent test patch
    gold_results = run_sec_test(
        env, data_record, log_dir, logger,
        run_name="base_with_test",
        patches=[(test_patch, {})],
        force=force,
    )

    common_tests = set(vuln_results.keys()) & set(gold_results.keys())
    if not common_tests:
        msg = "Failed to verify task on sec test breaks: no common tests between rollback_with_test and base_with_test."
        logger.error(msg)
        raise RuntimeError(msg)

    distinguishing = [t for t in common_tests
        if vuln_results[t] is False and gold_results[t] is True]

    if not distinguishing:
        msg = "Failed to verify task on sec test breaks: no distinguishing tests (vuln fail + gold pass). " \
            "vuln={}, gold={}".format(
            {t: vuln_results[t] for t in common_tests},
            {t: gold_results[t] for t in common_tests})
        logger.error(msg)
        raise RuntimeError(msg)

    stats = {}
    stats["num_sec_tests"] = len(distinguishing)
    logger.info(f"Sec tests verified: {len(distinguishing)}/{len(common_tests)} tests distinguishing: {distinguishing}")
    return (stats, distinguishing)


def validate_single(
    data_record: dict,
    instance_stats: dict,
    env_spec: dict,
    test_patch: str,
    validate_log_dir: Path,
    force: bool = False,
    from_existing_specs: bool = False,
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

    if not test_patch.strip():
        msg = "Empty model patch."
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

    try:
        test_logs_list, test_statuses = run_repo_test_suite_multi(
            env, data_record, log_dir, logger, force,
            from_existing_specs=from_existing_specs, env_spec=env_spec)
        if from_existing_specs and env_spec.get("logs_parser"):
            logger.info("Reusing logs parser from env_spec.")
        else:
            get_logs_parser(env, test_logs_list, test_statuses,
                log_dir=log_dir, logger=logger, model=LOGS_PARSER_MODEL,
                ordering_checks=[(2, 1)], require_failures=False, force=force)
        expected_failures, test_stats = validate_repo_test_breaks(env, test_logs_list, test_statuses, logger)
        sec_stats, sec_test_names = validate_sec_test_breaks(
            env, data_record, test_patch, log_dir, logger, force)
    except RuntimeError as e:
        return None, str(e)

    test_stats.update(sec_stats)
    logger.info("Task verified successfully, expected_failures-{}, sec_test_names-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_failures, sec_test_names, test_stats["num_sec_tests"], test_stats["num_func_tests"]))

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

    env_spec["logs_parser"] = env.logs_parser
    env_spec["logs_checker"] = env.logs_checker
    data_record["test_patch"] = test_patch
    data_record["sec_test_names"] = sec_test_names
    data_record["expected_failures"] = expected_failures
    data_record["image_name"] = eval_image_name
    instance_stats.update(test_stats)
    return env_spec, None


def validate_threadpool(
    dataset: list,
    stats: dict,
    max_workers: int,
    env_specs_path: Path,
    validate_log_dir: Path,
    test_patches: dict,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
    from_existing_specs: bool = False,
):
    env_specs = load_file(env_specs_path) if env_specs_path.exists() else {}
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    candidate_ids = set(dataset_by_id.keys()) & set(env_specs.keys()) & set(test_patches.keys())
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)
    if from_existing_specs:
        skipped = {iid for iid in candidate_ids if not env_specs[iid].get("logs_parser")}
        if skipped:
            print(f"--from_existing_specs: {len(skipped)} instance(s) skipped (no stored logs_parser): {sorted(skipped)}")
        candidate_ids = candidate_ids - skipped

    reset_llm_cost()
    succeeded, failed = [], {}
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
                from_existing_specs=from_existing_specs,
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
                    f"{len(succeeded)} ran successfully, {len(failed)} failed"
                )
                if save_specs:
                    save_file(env_specs, env_specs_path)
    summary = get_summary(succeeded, failed)
    summary_path = validate_log_dir / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    print(f"Logs-parser/checker LLM cost: ${get_llm_cost():.4f}")
    if save_specs:
        print(f"Environments saved to {env_specs_path}.")
    print(f"Summary saved to {summary_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate task instances (no_require_test mode) using synthesized security tests.")
    parser.add_argument(
        "--test_agent_output_dir",
        type=Path,
        required=True,
        help="Directory where the test synthesis agent output is stored.",
    )
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
    validate_log_dir = get_log_dir(args.run_id, "validate")

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    # Load test synthesis agent predictions
    predictions, _ = SWEAgentPort.after_completion(args.test_agent_output_dir)
    test_patches = {pred["instance_id"]: filter_binary_files(pred.get("model_patch", "")) for pred in predictions}

    validate_threadpool(
        dataset, stats, args.max_workers, env_specs_path, validate_log_dir,
        test_patches,
        force=args.force,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
        from_existing_specs=args.from_existing_specs,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
