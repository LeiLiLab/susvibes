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
import tiktoken
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template
from litellm import completion, get_max_tokens
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.constants import *
from susvibes.curate.constants import ENV_SETUP_LOG_DIR, LOGS_PARSER_MODEL, get_path
from susvibes.env import Deployment, Env
from susvibes.env_specs import TestStatus, TestItemStatus
from susvibes.curate.prompts import LOGS_PARSER_PROMPT_TEMPLATE
from susvibes.curate.validate.logs_parser import validate_logs_parser
from susvibes.curate.agents.ports import SWEAgentPort
from susvibes.utils import load_file, save_file, setup_instance_logger, parse_instance_id
from susvibes.curate.utils import (
    RepoLocks,
    get_on_hub_image_name,
)

load_dotenv()

LOG_INSTANCE = "validate.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_TEST_STATUSES = "test_statuses.json"
LOG_TEST_LOGS_PARSER = "logs_parser.json"
LOG_SEC_OUTPUT = "sec_outputs/{}.txt"
LOG_SEC_RESULTS = "sec_results/{}.json"

REPO_TEST_RUNS = ["base", "rollback", "task"]
SEC_TEST_RUNS = ["sec_vuln", "sec_gold"]
SEC_TEST_CMD = ["bash", "-c", "bash sectests.sh ; cat secresults.json"]


def run_repo_test_suite_multi(
    env: Env,
    data_record: dict,
    log_dir: Path,
    logger: logging.Logger,
    force: bool = False
) -> tuple[list, list]:
    """Run 3 repo test variants: base, rollback (vulnerable), task (masked)."""
    logger.info(f"Running repo tests in environment deployment {env.deployment.image.tags[0]}...")
    runs_list = [
        (),                                           # base: original (secure)
        (data_record["security_patch"], "-R"),         # rollback: reverse security fix (vulnerable)
        (data_record["task_patch"],),                  # task: apply mask
    ]
    allow_timeout = lambda id: id == 2        # only task may timeout
    allow_startup_error = lambda id: id == 2  # only task may have startup error

    test_logs_list, test_status_dict = [], {}
    test_statuses_path = log_dir / LOG_TEST_STATUSES
    for id, (run_patches, run_name) in enumerate(zip(runs_list, REPO_TEST_RUNS)):
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
                    patches={"pre_install": run_patches},
                    logger=logger,
                )
            except docker.errors.BuildError as e:
                msg = "Failed to build instance deployment."
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

    test_statuses = [test_status_dict[run_name] for run_name in REPO_TEST_RUNS]
    return test_logs_list, test_statuses


def get_repo_logs_parser(
    env: Env,
    test_logs_list: list,
    test_statuses: list,
    model: str,
    log_dir: Path,
    logger: logging.Logger,
    max_retries: int = 10,
    force: bool = False
) -> bool:
    """Synthesize a logs parser for repo test output. Like get_logs_parser but validates 3 runs."""
    test_logs_parser_path = log_dir / LOG_TEST_LOGS_PARSER
    if test_logs_parser_path.exists() and not force:
        logger.info("Logs parser found; reusing.")
        env.logs_parser = load_file(test_logs_parser_path)
        return True

    def clip_tokens(text: str, model: str, limit: int) -> str:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) > limit:
            tokens = tokens[-limit:]
        return enc.decode(tokens)

    test_logs_list = [clip_tokens(logs, model, limit=(get_max_tokens(model) // 8))
        for logs in test_logs_list]

    messages = []
    for prompt_key, prompt in LOGS_PARSER_PROMPT_TEMPLATE.items():
        if prompt_key == "system":
            messages.append({"role": "system", "content": Template(prompt).render(
                statuses=[status.value for status in TestItemStatus])})
        else:
            messages.append({"role": "user", "content": Template(prompt).render(
                logs=[logs for logs, status in zip(test_logs_list, test_statuses) if status])})

    logger.info("Synthesizing logs parser...")
    is_success = False
    conserv_retry = 1
    conservative_max_retries = 5
    for retry in range(max_retries):
        if retry:
            logger.info(f"Retrying... {retry + 1}/{max_retries}")
        try:
            message = completion(model=model, messages=messages).choices[0].message
        except Exception as e:
            logger.warning(f"Failed to get model response: {e}")
            continue
        try:
            logs_parser = json.loads(message.content.split("```")[1].strip()) \
                if "```" in message.content else json.loads(message.content)
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Failed to parse model response as JSON: {e}")
            continue
        if not validate_logs_parser(logs_parser, logger):
            continue
        env.logs_parser = logs_parser
        test_result_list, test_failures_list = [], []
        for logs, status in zip(test_logs_list, test_statuses):
            if not status:
                test_result_list.append({})
                continue
            try:
                test_result = env.parse_test_logs(logs, logger)
                test_result_list.append(test_result)
            except Exception as e:
                logger.warning(f"Failed to parse test logs: {e}. logs_parser-{logs_parser}")
                break
            test_failures_list.append(env.get_test_failures(test_result))
        if len(test_result_list) < len(test_logs_list):
            continue
        if not sum(test_failures_list) or any(tf < 0 for tf in test_failures_list):
            logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
            continue
        # 3-run validation: task should have more failures than rollback
        base_tf, rollback_tf, task_tf = test_failures_list
        task_completed = test_statuses[2] == TestStatus.COMPLETION.value
        if task_completed and task_tf < rollback_tf:
            if conserv_retry < conservative_max_retries:
                conserv_retry += 1
                logger.warning(f"Failed to verify test failures. logs_parser-{logs_parser}")
                continue
            else:
                logger.warning(f"Conservative retry limit reached. logs_parser-{logs_parser}")
        is_success = True
        break

    if not is_success:
        logger.error("Failed to synthesize logs parser.")
        return False
    logger.info("Logs parser created successfully.")
    save_file(logs_parser, test_logs_parser_path)
    return True


def validate_repo_test_breaks(
    env: Env,
    test_logs_list: list,
    test_statuses: list,
    logger: logging.Logger
) -> tuple[bool, tuple]:
    """
    Verify the task on functional test breaks.
    Returns a boolean success flag and a tuple of the expected failures and stats.
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
            logger.error(f"Failed to parse test logs: {e}")
            return False, ()
        test_failures_list.append(env.get_test_failures(test_result))

    base_tf, rollback_tf, task_tf = test_failures_list
    test_completed_list = [ts == TestStatus.COMPLETION.value for ts in test_statuses]
    base_completed, rollback_completed, task_completed = test_completed_list

    # Base and rollback must both complete
    if not base_completed or not rollback_completed:
        logger.error("Failed to verify: base_completed={}, rollback_completed={}".format(
            base_completed, rollback_completed))
        return False, ()

    # Secure should not have more failures than vulnerable on repo tests
    if base_tf > rollback_tf:
        logger.error("Failed to verify: base_tf ({}) > rollback_tf ({})".format(base_tf, rollback_tf))
        return False, ()

    # Mask should break functional tests
    is_broken = not task_completed or task_tf > rollback_tf
    if not is_broken:
        logger.error("Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_tf, task_tf if task_completed else "N/A"))
        return False, ()

    expected_failures = {
        "func": rollback_tf,
    }
    stats = {}
    stats["num_func_tests"] = task_tf - rollback_tf \
        if task_completed else -1
    logger.info("Repo tests verified: base_tf={}, rollback_tf={}, task_tf={}".format(
        base_tf, rollback_tf, task_tf if task_completed else "N/A"))
    return True, (expected_failures, stats)


def run_sec_test(
    env: Env,
    data_record: dict,
    test_patch: str,
    log_dir: Path,
    logger: logging.Logger,
    run_name: str,
    pre_install_patches: tuple,
    force: bool = False,
) -> dict | None:
    """Run one sec test configuration. Returns secresults dict or None."""
    sec_output_path = log_dir / LOG_SEC_OUTPUT.format(run_name)
    sec_results_path = log_dir / LOG_SEC_RESULTS.format(run_name)

    if not force and sec_output_path.exists() and sec_results_path.exists():
        logger.info(f"Sec test {run_name} found; reusing.")
        return load_file(sec_results_path)

    try:
        deployment: Deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches={
                "pre_install": pre_install_patches,
                "post_install": (test_patch,),
            },
            logger=logger,
        )
    except docker.errors.BuildError as e:
        logger.error(f"Failed to build sec test deployment for {run_name}: {e}")
        return None

    deployment.create_container(
        command=SEC_TEST_CMD,
        mem_limit=CONTAINER_MEM_LIMIT,
        cpu_limit=CONTAINER_CPU_LIMIT,
    )
    test_logs, timed_out = deployment.run_with_timeout()

    sec_output_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(test_logs, sec_output_path)

    if timed_out:
        logger.error(f"Sec test {run_name} timed out.")
        return None

    secresults = parse_secresults(test_logs)
    if secresults is None:
        logger.error(f"Failed to parse secresults for {run_name}.")
        return None

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
) -> tuple[bool, tuple]:
    """
    Run sec_vuln and sec_gold, verify at least one distinguishing test.
    Returns a boolean success flag and a tuple of (stats, sec_test_names).
    """
    # sec_vuln: vulnerable implementation + agent test patch
    vuln_results = run_sec_test(
        env, data_record, test_patch, log_dir, logger,
        run_name="sec_vuln",
        pre_install_patches=(data_record["security_patch"], "-R"),
        force=force,
    )
    if vuln_results is None:
        return False, ()

    # sec_gold: secure implementation + agent test patch
    gold_results = run_sec_test(
        env, data_record, test_patch, log_dir, logger,
        run_name="sec_gold",
        pre_install_patches=(),
        force=force,
    )
    if gold_results is None:
        return False, ()

    # Validate: at least one test distinguishes (vuln fail + gold pass)
    common_tests = set(vuln_results.keys()) & set(gold_results.keys())
    if not common_tests:
        logger.error("No common tests found between sec_vuln and sec_gold.")
        return False, ()

    distinguishing = [t for t in common_tests
        if vuln_results[t] is False and gold_results[t] is True]

    if not distinguishing:
        logger.error("No distinguishing sec tests (vuln fail + gold pass). "
            "vuln={}, gold={}".format(
            {t: vuln_results[t] for t in common_tests},
            {t: gold_results[t] for t in common_tests}))
        return False, ()

    stats = {}
    stats["num_sec_tests"] = len(distinguishing)
    logger.info(f"Sec tests verified: {len(distinguishing)}/{len(common_tests)} tests distinguishing: {distinguishing}")
    return True, (stats, distinguishing)


def validate_single(
    data_record: dict,
    instance_stats: dict,
    env_spec: dict,
    test_patch: str,
    env_setup_log_dir: Path,
    force: bool = False,
) -> dict | None:
    """Validate a single instance in no_require_test mode.
    Returns updated env_spec on success, None on failure.
    On success, data_record is updated in-place."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_dir = env_setup_log_dir / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Validating environment (no_require_test) for {instance_id}...")

    env = Env(
        logger=logger,
        project=project,
        image_name=data_record["image_name"],
        **env_spec
    )

    try:
        test_logs_list, test_statuses = run_repo_test_suite_multi(env, data_record, log_dir, logger, force)
    except RuntimeError as e:
        return None

    parse_success = get_repo_logs_parser(env, test_logs_list, test_statuses,
        log_dir=log_dir, logger=logger, model=LOGS_PARSER_MODEL, force=force)
    if not parse_success:
        return None

    is_valid, test_info = validate_repo_test_breaks(env, test_logs_list, test_statuses, logger)
    if not is_valid:
        return None
    expected_failures, test_stats = test_info

    is_valid, sec_info = validate_sec_test_breaks(
        env, data_record, test_patch, log_dir, logger, force)
    if not is_valid:
        return None
    sec_stats, sec_test_names = sec_info

    test_stats.update(sec_stats)
    logger.info("Task verified successfully, expected_failures-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_failures, test_stats["num_sec_tests"], test_stats["num_func_tests"]))

    with RepoLocks.locked(project):
        logger.info(f"Building evaluation image for {instance_id}...")
        task_deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches={"pre_install": (data_record["task_patch"],)},
            logger=logger
        )
    eval_image_name = f"eval_{instance_id.lower()}"
    assert task_deployment.image.tag(eval_image_name)
    dockerhub_image_name = get_on_hub_image_name(instance_id=instance_id)
    assert task_deployment.image.tag(dockerhub_image_name)

    env_spec["logs_parser"] = env.logs_parser
    data_record["test_patch"] = test_patch
    data_record["sec_test_names"] = sec_test_names
    data_record["expected_failures"] = expected_failures
    data_record["image_name"] = dockerhub_image_name
    instance_stats.update(test_stats)
    return env_spec


def validate_threadpool(
    dataset: list,
    stats: dict,
    max_workers: int,
    env_specs_path: Path,
    env_setup_log_dir: Path,
    test_patches: dict,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    env_specs = load_file(env_specs_path) if env_specs_path.exists() else {}
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    candidate_ids = set(dataset_by_id.keys()) & set(env_specs.keys()) & set(test_patches.keys())
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)

    succeeded, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                validate_single,
                dataset_by_id[instance_id],
                stats.get(instance_id, {}),
                env_specs[instance_id],
                test_patches[instance_id],
                env_setup_log_dir,
                force=force,
            ): instance_id
            for instance_id in candidate_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Validating (no_test) [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    updated_spec = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if updated_spec:
                    env_specs[instance_id] = updated_spec
                    succeeded.append(instance_id)
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} ran successfully, {len(failed)} failed"
                )
                if save_specs:
                    save_file(env_specs, env_specs_path)
    if failed:
        print("failed: \n" + "\n".join(failed))
    if save_specs:
        print(f"Environments saved to {env_specs_path}.")


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
    args = parser.parse_args()

    dataset_path = get_path('dataset', args.run_id)
    stats_path = get_path('stats', args.run_id)
    env_specs_path = get_env_spec_path('components', args.run_id)
    env_setup_log_dir = ENV_SETUP_LOG_DIR / args.run_id

    dataset = load_file(dataset_path)
    stats = load_file(stats_path) if stats_path.exists() else {}

    # Load test synthesis agent predictions
    predictions, _ = SWEAgentPort.after_completion(args.test_agent_output_dir)
    test_patches = {}
    for pred in predictions:
        patch = pred.get("model_patch", "")
        if patch.strip():
            test_patches[pred["instance_id"]] = patch

    validate_threadpool(
        dataset, stats, args.max_workers, env_specs_path, env_setup_log_dir,
        test_patches,
        force=args.force,
        save_specs=not args.skip_specs,
        instance_ids=args.instance_ids,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset updated at {dataset_path}.")
    save_file(stats, stats_path)
    print(f"Stats saved to {stats_path}.")
