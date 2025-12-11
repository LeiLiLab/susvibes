import re
import logging
import docker.errors
from tqdm import tqdm
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.constants import *
from susvibes.env import Deployment, Env
from susvibes.env_specs import (
    GIT_UNIGNORE_PATTERNS,
    TestStatus,
)
from susvibes.curate.env_setup.logs_parser import get_logs_parser
from susvibes.curate.utils import (
    RepoLocks,
    load_file, 
    save_file, 
    reset_to_commit, 
    apply_patch, 
    get_repo_dir, 
    parse_instance_id,
    filter_patch,
    setup_logger,
    get_on_hub_image_name
)

LOG_INSTANCE = "run_instance.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_TEST_STATUSES = "test_statuses.json"

ENV_SETUP_RUNS = ["base", "rollback", "sec_patch", "sec_test", "task"]

def extract_dockerfile(prediction, logger):
    """Extract the Dockerfile from the model prediction patch."""
    project, base_commit = parse_instance_id(prediction["instance_id"])
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, base_commit, new_branch=False)
    try:
        targets = {"Dockerfile", ".dockerignore"}
        apply_patch(repo_dir, filter_patch(prediction["model_patch"], targets))
    except Exception as e:
        msg = f"Error applying model patch: {e}"
        logger.error(msg)
        raise RuntimeError(msg)
        
    try:
        dockerfile = load_file(repo_dir / "Dockerfile")
        if (repo_dir / ".dockerignore").exists():
            dockerignore = load_file(repo_dir / ".dockerignore") + "\n" \
                + "\n".join(GIT_UNIGNORE_PATTERNS)
        else:
            dockerignore = ""
    except FileNotFoundError:
        msg = "Dockerfile corresponding to the environment not found."
        logger.error(msg)
        return RuntimeError(msg)
    return dockerfile, dockerignore

def handle_env_image(prediction, dockerfile, dockerignore, logger):
    """Handle the environment image."""
    project, base_commit = parse_instance_id(prediction["instance_id"])
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)
    env_image_name = f"env_{prediction['instance_id'].lower()}"
    try:
        reset_to_commit(repo_dir, base_commit)
        env_deployment = Deployment.from_build(
            logger=logger,
            context_path=repo_dir,
            dockerfile=dockerfile,
            dockerignore=dockerignore, 
            image_name=env_image_name,
        )
    except docker.errors.BuildError as e:
        msg = "Failed to get environment deployment."
        logger.error(msg)
        raise RuntimeError(msg)
    return env_image_name         

def run_test_suite_multi(
    env: Env, 
    data_record: dict,
    log_dir: Path, 
    logger: logging.Logger,
    force: bool = False
) -> list:
    """Run tests in the environment and return test logs for multiple patches."""
    logger.info(f"Running tests in environment deployment {env.deployment.image.tags[0]}...")
    runs_list = [
        (), (data_record["security_patch"], data_record["test_patch"], "-R"),
        (data_record["test_patch"], "-R"), (data_record["security_patch"], "-R"), 
        (data_record["task_patch"],)
    ]
    allow_timeout = lambda run_id: run_id >= 3
    allow_startup_error = lambda run_id: run_id == 4
    # force_rerun = lambda run_id: run_id in [4,]
    
    test_logs_list, test_status_dict = [], {}
    test_statuses_path = log_dir / LOG_TEST_STATUSES
    for run_id, (run_patches, run_name) in enumerate(zip(runs_list, ENV_SETUP_RUNS)):
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
                with RepoLocks.locked(data_record["project"]):
                    deployment: Deployment = env.build_instance_deployment(
                        base_commit=data_record["base_commit"],
                        patches={"pre_install": run_patches},
                        logger=logger,
                    )
            except docker.errors.BuildError as e:
                msg = "Failed to build instance deployment."
                logger.error(msg)
                raise RuntimeError(msg)
            deployment.create_container()
            test_logs, timed_out = deployment.run_with_timeout()
            test_status = env.get_test_status(test_logs, timed_out)
            test_logs_list.append(test_logs)
            test_status_dict[run_name] = test_status            

            test_output_path.parent.mkdir(parents=True, exist_ok=True)
            save_file(test_logs, test_output_path)
            save_file(test_status_dict, test_statuses_path)
            
        if test_status_dict[run_name] == TestStatus.TIMEOUT.value \
            and not allow_timeout(run_id):
            msg = "Failed to run tests because of critical timeout."
            logger.error(msg)
            raise RuntimeError(msg)
        if test_status_dict[run_name] == TestStatus.STARTUP_ERROR.value \
            and not allow_startup_error(run_id):
            msg = "Failed to run tests because of critical startup error."
            logger.error(msg)
            raise RuntimeError(msg)

    test_statuses = [test_status_dict[run_name] for run_name in ENV_SETUP_RUNS]
    return test_logs_list, test_statuses

def verify_test_breaks(
    env: Env, 
    test_logs_list: list,
    test_statuses: list,
    logger: logging.Logger
) -> tuple[bool, tuple]:
    """
    Verify the task on security and functional test breaks.
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
        
    base_tf, rollback_tf, sec_patch_tf, sec_test_tf, task_tf = test_failures_list
    test_completed_list = [ts == TestStatus.COMPLETION.value for ts in test_statuses]
    _, _, _, sec_test_completed, task_completed = test_completed_list
    
    test_symbres_errs_list = []
    for logs in test_logs_list:
        test_symbres_errs_list.append(env.get_symbol_resolution_errors(logs))
    _, rollback_te, _, sec_test_te, _ = test_symbres_errs_list
    if sec_test_completed and sec_test_te > rollback_te:
        logger.error("Failed to verify task on symbol resolution errors: rollback-{}, sec_test-{}".format(
            rollback_te, sec_test_te))
        return False, ()
    stats = {}
    extra_pass = rollback_tf - sec_patch_tf
    is_broken = not sec_test_completed or sec_test_tf > rollback_tf
    is_repaired = not sec_test_completed or base_tf < sec_test_tf - extra_pass
    if not (is_broken and is_repaired) or extra_pass < 0:
        logger.error("Failed to verify task on security test breaks: rollback-{}, sec_patch-{}, sec_test-{}, base-{}".format(
            rollback_tf, sec_patch_tf, sec_test_tf if sec_test_completed else "N/A", base_tf))
        return False, ()
    stats["num_sec_tests"] = sec_test_tf - extra_pass - base_tf \
        if sec_test_completed else -1

    is_broken = not task_completed or task_tf > rollback_tf
    if not is_broken:
        logger.error("Failed to verify task on functional test breaks: rollback-{}, task-{}".format(
            rollback_tf, task_tf if task_completed else "N/A"))
        return False, ()
    
    expected_failures = {
        "func": rollback_tf,
        "sec": base_tf - sec_patch_tf
    }
    stats["num_func_tests"] = task_tf - rollback_tf \
        if task_completed else -1
    logger.info("Task verified successfully, expected_failures-{}, num_sec_tests-{}, num_func_tests-{}".format(
        expected_failures, stats["num_sec_tests"], stats["num_func_tests"]))
    return True, (expected_failures, stats)

def create_env(
    prediction: dict, 
    data_record: dict, 
    instance_stats: dict, 
    force: bool = False
) -> dict | None:
    """Create environment components and conduct tests verification."""
    instance_id = prediction["instance_id"]
    project, _ = parse_instance_id(instance_id)
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)

    log_dir = ENV_SETUP_LOG_DIR / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Creating environment for {instance_id}...")
    
    try:
        with RepoLocks.locked(project):
            dockerfile, dockerignore = extract_dockerfile(prediction, logger)
            env_image_name = handle_env_image(prediction, dockerfile, dockerignore, logger)
    except RuntimeError as e:
        return None
    
    env_spec = {}
    env_spec["dockerfile"] = dockerfile
    env_spec["dockerignore"] = dockerignore
    env = Env(
        logger=logger,
        project=project,
        repo_dir=repo_dir,
        image_name=env_image_name,
        **env_spec
    )
    try:
        run_result = run_test_suite_multi(env, data_record, log_dir, logger, force)
    except RuntimeError as e:
        return None
    test_logs_list, test_statuses = run_result
    parse_success = get_logs_parser(env, test_logs_list, test_statuses, 
        log_dir=log_dir, logger=logger, model="o3", force=force)
    if not parse_success:
        return None
    is_valid, test_info = verify_test_breaks(env, test_logs_list, test_statuses, logger=logger)
    if not is_valid:
        return None
    expected_failures, test_stats = test_info

    with RepoLocks.locked(project):
        logger.info(f"Building evaluation image for {instance_id}...")
        task_deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches={"pre_install": (data_record["task_patch"],)},
            logger=logger
        )
    eval_image_name = f"eval_{instance_id.lower()}"
    assert task_deployment.image.tag(eval_image_name)
    dockerhub_image_name = get_on_hub_image_name(
        instance_id=instance_id
    )
    assert task_deployment.image.tag(dockerhub_image_name)

    env_spec["logs_parser"] = env.logs_parser
    data_record["expected_failures"] = expected_failures
    data_record["image_name"] = dockerhub_image_name
    instance_stats.update(test_stats)
    return env_spec

def create_env_threadpool(
    predictions: list,
    task_dataset: list,
    stats: dict,
    max_workers: int,
    force: bool = False,
):
    pred_by_id = {pred["instance_id"]: pred for pred in predictions}
    task_dataset_by_id = {data_record["instance_id"]: data_record 
        for data_record in task_dataset}
    env_specs = load_file(ENV_SPECS_PATH) if ENV_SPECS_PATH.exists() else {}
    
    dataset = []
    succeeded, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(create_env, pred_by_id[instance_id], 
                task_dataset_by_id[instance_id], stats[instance_id], force): instance_id
            for instance_id in pred_by_id if instance_id in task_dataset_by_id
        }
        with tqdm(total=len(futures), dynamic_ncols=True, 
            desc=f"Building components [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    env_spec = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if env_spec:
                    env_specs[instance_id] = env_spec
                    dataset.append(task_dataset_by_id[instance_id])
                    succeeded.append(instance_id)
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} ran successfully, {len(failed)} failed"
                )
                save_file(env_specs, ENV_SPECS_PATH)
    if failed:              
        print("failed: \n" + "\n".join(failed))           
    print(f"Environments saved to {ENV_SPECS_PATH}.")   
    return dataset
