"""
Purpose: Validate task instances where security tests come from a separate synthesis
agent rather than from the repo's own test commits. The repo's test suite is still
used for functional regression checks.

python -m susvibes.curate.validate.gen_test \
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
from susvibes.curate.constants import KeepStage, get_log_dir, LOGS_PARSER_MODEL
from susvibes.core.env import Deployment, Env
from susvibes.core.logs import LogsHandler, get_llm_cost, reset_llm_cost
from susvibes.curate.validate.constants import (
    LOG_INSTANCE, LOG_TEST_OUTPUT, LOG_REPORT, LOG_SUMMARY, ValidateStatus, ValidationRejected,
    KEEP_STAGE)
from susvibes.curate.validate.utils import (
    build_clean_eval_deployment, validate_report, validate_miss, apply_validate_report)
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.curate.utils import should_keep
from susvibes.core.report import reuse_report, save_report, get_report_summary, print_summary
from susvibes.core.utils import load_file, save_file, get_image_name, setup_instance_logger, parse_instance_id, filter_binary_files, get_env_specs, save_env_specs, Route, PatchError, is_patch_error

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
    test_logs_list, timed_out_list = [], []
    for run_patches, run_name in zip(runs_list, REPO_TEST_RUNS):
        try:
            deployment: Deployment = env.build_instance_deployment(
                base_commit=data_record["base_commit"],
                patches=run_patches,
                logger=logger,
            )
        except docker.errors.BuildError as e:
            msg = f"Failed to build instance deployment: {e}"
            logger.error(msg)
            # Decide here, where the build log is: `git apply` refusing the test_patch is a verdict
            # on the instance, anything else is the harness breaking.
            raise PatchError(msg) if is_patch_error(str(e)) else RuntimeError(msg)
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
        # The logs are evidence beside the report, not a cache: an instance with a cached report
        # never reaches here, and one without needs a fresh verdict anyway.
        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        test_output_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(test_logs, test_output_path)
        test_logs_list.append(test_logs)
        timed_out_list.append(timed_out)
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
    Raises RuntimeError on failure; returns (expected_pf, test_stats) on success.
    """
    test_pf_list = [env.handle_test_logs(test_logs, timed_out, logger,
        kind=Route.route_logs_kind(flags, run_name))
        for run_name, test_logs, timed_out in zip(REPO_TEST_RUNS, test_logs_list, timed_out_list)]

    base_pf, rollback_pf, task_pf = test_pf_list

    if base_pf.breaks_more_than(rollback_pf):
        msg = "Failed to validate functional test baseline: base ({}) breaks more than rollback ({})".format(
            base_pf, rollback_pf)
        logger.error(msg)
        raise ValidationRejected(msg)

    if not task_pf.breaks_more_than(rollback_pf):
        msg = "Failed to validate functional test breaks: rollback-{}, task-{}".format(
            rollback_pf, task_pf)
        logger.error(msg)
        raise ValidationRejected(msg)

    expected_pf = {
        "func": rollback_pf.get_raw(),
    }
    test_stats = {}
    test_stats["num_func_tests"] = task_pf.count_excess_breaks_over(rollback_pf)
    logger.info("Repo tests verified: base={}, rollback={}, task={}".format(
        base_pf, rollback_pf, task_pf))
    return (expected_pf, test_stats)


def run_gen_sec_test_suite_multi(
    env: Env,
    data_record: dict,
    test_patch: str,
    flags: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False,
) -> tuple[list, list]:
    """Run 2 generated-sec-test variants: rollback_with_gen_test (vulnerable + agent test) and
    base_with_gen_test (secure + agent test). Collect and cache each run's logs and timeout flag;
    returns (test_logs_list, timed_out_list). Classification (status / discrimination) is done by the
    caller. Mirrors run_repo_test_suite_multi."""
    logger.info(f"Running generated sec tests in environment deployment {env.deployment.image.tags[0]}...")
    runs_list = [
        [(data_record["security_patch"], {"reverse": True}), (test_patch, {})],  # rollback_with_gen_test: vulnerable + agent test
        [(test_patch, {})],                                                       # base_with_gen_test: secure + agent test
    ]
    test_logs_list, timed_out_list = [], []
    for run_patches, run_name in zip(runs_list, GEN_SEC_TEST_RUNS):
        try:
            deployment: Deployment = env.build_instance_deployment(
                base_commit=data_record["base_commit"],
                patches=run_patches,
                logger=logger,
            )
        except docker.errors.BuildError as e:
            # `run_name` goes AFTER the first colon: the prefix is what groups errors, and each of
            # this loop's builds applies a different set of patches, so the verdict must name which.
            msg = f"Failed to build gen sec test instance deployment: {run_name}: {e}"
            logger.error(msg)
            raise PatchError(msg) if is_patch_error(str(e)) else RuntimeError(msg)
        try:
            deployment.create_container(command=Route.route_test_cmd(flags, run_name),
                mem_limit=ContainerLimits.MEM_LIMIT, cpu_limit=ContainerLimits.CPU_LIMIT)
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
        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)   # evidence, not a cache
        test_output_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(test_logs, test_output_path)
        test_logs_list.append(test_logs)
        timed_out_list.append(timed_out)
    return test_logs_list, timed_out_list


def validate_gen_sec_test_breaks(
    env: Env,
    test_logs_list: list,
    timed_out_list: list,
    flags: dict,
    logger: logging.Logger,
) -> tuple:
    """
    Verify the generated sec tests discriminate: the vulnerable run must break at least one case the
    secure run passes, and each state must conclude pass or fail (never error). Raises ValidationRejected
    on failure; returns (expected_pf, test_stats) on success — expected_pf carries the "sec" key (the
    distinguishing amount an eval submission must not break). Mirrors validate_repo_test_breaks.
    """
    test_pf_list = [env.handle_test_logs(test_logs, timed_out, logger,
        kind=Route.route_logs_kind(flags, run_name))
        for run_name, test_logs, timed_out in zip(GEN_SEC_TEST_RUNS, test_logs_list, timed_out_list)]

    vuln_pf, gold_pf = test_pf_list

    # Error contract: a security test must conclude pass or fail in BOTH states. A run that does not
    # complete - a collection/import error, a missing driver, a setup failure, or a timeout - means the
    # tests are broken, not a security signal; reject rather than reading a non-completed run (which
    # compares as infinitely broken) as detection.
    for run_name, test_pf in zip(GEN_SEC_TEST_RUNS, test_pf_list):
        if not test_pf.completed():
            msg = f"Failed to validate gen sec tests: {run_name} did not conclude ({test_pf.status}) - a " \
                "security test must pass or fail, never error."
            logger.error(msg)
            raise ValidationRejected(msg)

    if not vuln_pf.breaks_more_than(gold_pf):
        msg = "Failed to validate gen sec test breaks: no distinguishing tests (vuln fail + gold pass). " \
            "vuln={}, gold={}".format(vuln_pf, gold_pf)
        logger.error(msg)
        raise ValidationRejected(msg)

    expected_pf = {"sec": vuln_pf.excess_breaks_over(gold_pf, to_raw=True)}
    test_stats = {}
    test_stats["num_sec_tests"] = vuln_pf.count_excess_breaks_over(gold_pf)
    logger.info(f"Gen sec tests verified: {test_stats['num_sec_tests']} distinguishing.")
    return (expected_pf, test_stats)


def validate_single(
    data_record: dict,
    env_spec: dict,
    validate_log_dir: Path,
    force: bool = False,
    resume: bool = False,
) -> dict:
    """Validate a single instance in no_require_test mode, reusing this instance's cached report
    unless `force`/`resume` asks to re-run it. Always returns a report: `validate_status` says how
    it concluded, `error` is set only when the harness itself failed. The caller projects a
    validated report onto the record, env_specs and stats — the report is their cache, not their
    home."""
    instance_id = data_record["instance_id"]
    test_patch = data_record["test_patch"]
    project, _ = parse_instance_id(instance_id)

    log_dir = validate_log_dir / instance_id
    report = reuse_report(log_dir / LOG_REPORT, force=force, resume=resume)
    if report is not None:
        return report

    logger = setup_instance_logger(log_dir / LOG_INSTANCE, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Validating environment (no_require_test) for {instance_id}...")

    if not test_patch.strip():
        logger.error("Empty test_patch.")
        report = validate_report(ValidateStatus.EMPTY_PATCH)
        save_report(report, log_dir / LOG_REPORT)
        return report

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
        # Functional: the repo's own tests across base/rollback/task, with a count parser synthesized from them.
        test_logs_list, timed_out_list = run_repo_test_suite_multi(
            env, data_record, flags, log_dir, logger, force)
        env.logs_handler = LogsHandler.get_by_kind("count", env.logs_handler,
            test_logs_list=test_logs_list, timed_out_list=timed_out_list,
            model=LOGS_PARSER_MODEL, log_dir=log_dir, logger=logger, ordering_checks=[(2, 1)],
            allow_timeout=lambda id: id == 2, allow_aborted=lambda id: id == 2,
            require_failures=False, force=force)
        expected_pf, test_stats = validate_repo_test_breaks(
            env, test_logs_list, timed_out_list, flags, logger)

        # Generated security tests across the two states, with their OWN count parser synthesized from their
        # own output (its format can differ from the functional run's). require_failures=True: if neither
        # state yields a countable failure the suite discriminates nothing — an INVALID verdict, not a miss;
        # allow_timeout/aborted are permissive so a non-completed run is judged (INVALID) by the breaks step.
        gen_sec_test_logs_list, gen_sec_timed_out_list = run_gen_sec_test_suite_multi(
            env, data_record, test_patch, flags, log_dir, logger, force)
        try:
            env.logs_handler = LogsHandler.get_by_kind("count_gen_sec", env.logs_handler,
                test_logs_list=gen_sec_test_logs_list, timed_out_list=gen_sec_timed_out_list,
                model=LOGS_PARSER_MODEL, log_dir=log_dir, logger=logger, ordering_checks=[(0, 1)],
                allow_timeout=lambda id: True, allow_aborted=lambda id: True,
                require_failures=True, force=force)
        except RuntimeError as e:
            # Pass the cause through rather than naming one: this catches a checker that would not
            # synthesize, a run the abort rules rejected, and a parser that found nothing to count —
            # a fixed label here reads as a diagnosis, and sends whoever trusts it the wrong way.
            raise ValidationRejected(f"Gen sec tests rejected: {e}")
        gen_sec_expected_pf, gen_sec_test_stats = validate_gen_sec_test_breaks(
            env, gen_sec_test_logs_list, gen_sec_timed_out_list, flags, logger)

        expected_pf.update(gen_sec_expected_pf)
        test_stats.update(gen_sec_test_stats)
        logger.info("Task validated, expected_pf-{}, num_sec_tests-{}, num_func_tests-{}".format(
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
            raise PatchError(msg) if is_patch_error(str(e)) else RuntimeError(msg)
        task_image_name = get_image_name(f"task_{instance_id}")
        assert task_deployment.image.tag(task_image_name)

        logger.info(f"Building evaluation deployment for {instance_id}...")
        eval_image_name = get_image_name(f"eval_{instance_id}")
        try:
            build_clean_eval_deployment(task_image_name, eval_image_name, logger)
        except docker.errors.BuildError as e:
            msg = f"Failed to build eval deployment: {e}"
            logger.error(msg)
            raise RuntimeError(msg)
    except ValidationRejected as e:
        report = validate_report(ValidateStatus.INVALID, reason=str(e))
    except PatchError as e:
        report = validate_report(ValidateStatus.PATCH_ERROR, reason=str(e))
    except RuntimeError as e:
        report = validate_miss(str(e))
    else:
        report = validate_report(
            ValidateStatus.VALIDATED,
            run=dict(zip(REPO_TEST_RUNS, ({"timed_out": t} for t in timed_out_list))),
            expected_pf=expected_pf,
            flags=flags,
            image_name=eval_image_name,
            logs_handler=env.logs_handler,
            stats=test_stats,
        )

    save_report(report, log_dir / LOG_REPORT)
    return report


def validate_threadpool(
    run_id: str,
    dataset: list,
    stats: dict,
    max_workers: int,
    validate_log_dir: Path,
    force: bool = False,
    resume: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    gated_ids = {instance_id for instance_id, r in dataset_by_id.items() if should_keep(r, exclude=KEEP_STAGE, required=(KeepStage.BUILD_REPO, KeepStage.GEN_TEST))} \
        & set(env_specs.keys())
    if instance_ids is not None:
        gated_ids = gated_ids & set(instance_ids)

    reset_llm_cost()
    reports = {}
    env_specs_path = None
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                validate_single,
                dataset_by_id[instance_id],
                env_specs[instance_id],
                validate_log_dir,
                force=force,
                resume=resume,
            ): instance_id
            for instance_id in gated_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Validating (no_test) [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                reports[instance_id] = report
                apply_validate_report(report, dataset_by_id[instance_id],
                                      env_specs[instance_id], stats)
                pbar.update(1)
                validated = sum(1 for r in reports.values()
                                if r["validate_status"] == ValidateStatus.VALIDATED)
                pbar.set_description(f"{validated} validated, {len(reports) - validated} not")
                if save_specs:
                    env_specs_path = save_env_specs("logs_handler", env_specs, run_id)
    summary = get_report_summary(reports, "validate_status")
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
        help="Re-validate every instance instead of reusing cached reports, and re-create the logs handler.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-validate only the instances whose cached report is an errored run, keeping every "
             "verdict. If both --force and --resume are given, --force wins.",
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

    dataset_path = get_dataset_path('dataset', args.run_id)
    stats_path = get_dataset_path('stats', args.run_id)
    validate_log_dir = get_log_dir(args.run_id, "validate")

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    validate_threadpool(
        args.run_id, dataset, stats, args.max_workers, validate_log_dir,
        force=args.force,
        resume=args.resume,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
