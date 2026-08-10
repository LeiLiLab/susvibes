"""
Purpose: Security test generation. Build a rollback variant of each instance's env
image (security_patch reversed, original patch persisted at .sv.security_patch.diff
so the agent can toggle states), assemble the SWE-agent batch, and run the
test-synthesis agent (SWE-agent, sv) over it in-process.

python -m susvibes.curate.test.gen \
    --max_workers 5 \
    --run_id playground
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker.errors
from tqdm import tqdm
from jinja2 import Template

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR, get_log_dir, get_agent_setting_path
from susvibes.curate.test.prompts import SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE

KEEP_STAGE = KeepStage.GEN_TEST   # this stage's verdict under record["keep"]
TEST_FIELD = "test_patch"   # the tests a task is graded against — the same field test_mask writes,
                            # for the population that had none to mask
from susvibes.curate.utils import (
    get_repo_test_cmd, reverse_patch, should_keep, get_repo_dir, reset_to_commit,
    apply_patch, RepoLocks,
)
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.env import Env, Deployment
from susvibes.env_specs import WORKSPACE_DIR_NAME
from susvibes.core.report import get_two_state_summary, print_summary
from susvibes.core.utils import load_file, save_file, filter_binary_files, get_image_name, parse_instance_id, setup_instance_logger, get_env_specs

LOG_BUILD = "build_rollback_image.log"
SECURITY_PATCH_FILE_NAME = ".sv.security_patch.diff"  # kept in repo root for state toggling


def build_rollback_deployment(data_record, env_spec, target_image_name, log_dir) -> tuple:
    """Build a rollback variant of the env image with security_patch reversed
    (so /project sits in the vulnerable state) and the patch persisted at
    .sv.security_patch.diff, tagged target_image_name. Returns the
    Deployment (or None on failure)."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_file = log_dir / instance_id / LOG_BUILD
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)

    try:
        env = Env(
            logger=logger,
            project=project,
            image_name=data_record["env_image_name"],
            **env_spec,
        )
    except (docker.errors.ImageNotFound, docker.errors.NotFound):
        msg = f"Env image not found: {data_record['env_image_name']}"
        logger.error(msg)
        raise RuntimeError(msg)
    try:
        deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches=[(data_record["security_patch"], {"reverse": True, "save_to": SECURITY_PATCH_FILE_NAME})],
            logger=logger,
            remove_image=False,
        )
    except docker.errors.BuildError as e:
        msg = f"Failed to build rollback deployment: {instance_id}: {e}"
        logger.error(msg)
        return None, msg

    assert deployment.image.tag(target_image_name)
    logger.info(f"Rollback deployment built: {target_image_name}")
    return deployment, None


def build_rollback_threadpool(dataset, env_specs, log_dir, max_workers):
    """Build rollback images for the given dataset in parallel.
    Returns (image_by_id, failed) — a dict of successful builds and a list of failures."""
    image_by_id, errored = {}, {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for data_record in dataset:
            instance_id = data_record["instance_id"]
            rollback_image_name = get_image_name(f"rollback_{instance_id}")
            futures[executor.submit(
                build_rollback_deployment,
                data_record,
                env_specs[instance_id],
                rollback_image_name,
                log_dir,
            )] = (instance_id, rollback_image_name)
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Building rollback images [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id, rollback_image_name = futures[future]
                try:
                    deployment, reason = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if deployment:
                    image_by_id[instance_id] = rollback_image_name
                else:
                    errored[instance_id] = reason
                pbar.update(1)
                pbar.set_description(
                    f"{len(image_by_id)} built, {len(errored)} failed"
                )
    print_summary(get_two_state_summary(list(image_by_id), errored))
    return image_by_id, errored


def prologue(run_id, max_workers, instance_ids=None) -> SWEAgentPort:
    """Build the rollback images and assemble the SWE-agent batch; return the port."""
    gen_log_dir = get_log_dir(run_id, "test", "gen")
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("test_gen")),
        run_name=__spec__.name, output_dir=gen_log_dir)

    dataset_path = get_dataset_path('dataset', run_id)

    dataset = load_file(dataset_path)
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))

    gated = [data_record for data_record in dataset if should_keep(data_record, required=(KeepStage.BUILD_REPO,)) and data_record["instance_id"] in env_specs]
    if instance_ids is not None:
        gated = [data_record for data_record in gated if data_record["instance_id"] in set(instance_ids)]

    image_by_id, errored = build_rollback_threadpool(
        gated, env_specs, gen_log_dir, max_workers)

    added = 0
    for record in gated:
        instance_id = record["instance_id"]
        if instance_id not in image_by_id:
            continue
        repo_test_cmd = get_repo_test_cmd(env_specs[instance_id]["dockerfile"])
        sec_prop = record["sec_prop"]
        render_kwargs = {
            "VULN_CLASS": sec_prop["vuln_class"],
            "RISK_NARRATIVE": sec_prop["risk_narrative"],
            "INVARIANT": sec_prop["invariant"],
            "VULNERABLE_IF": sec_prop["vulnerable_if"],
            "SECURE_IF": sec_prop["secure_if"],
            "IRRELEVANT": sec_prop["security_irrelevant_differences"],
            "REPO_TEST_CMD": repo_test_cmd,
            "PATCH": reverse_patch(record["mask_patch"]),
        }
        problem_statement = Template(SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE).render(**render_kwargs)

        port.add_task(
            image=image_by_id[instance_id],
            repo_type="preexisting",
            repo_name=WORKSPACE_DIR_NAME,
            problem_statement=problem_statement,
            instance_id=instance_id,
        )
        added += 1

    port.before_start()
    print(f"Added {added} tasks. {len(errored)} builds failed.")
    return port


def epilogue(run_id, instance_ids=None):
    """Take the agent's tests onto the records as `test_patch`, keeping only what actually applies.

    A patch that `git apply` refuses is this stage's failure to conclude, not validate's: validate
    runs the tests and judges, and should never be the first to learn the tests were unusable."""
    gen_log_dir = get_log_dir(run_id, "test", "gen")
    predictions, total_cost = SWEAgentPort.after_completion(gen_log_dir)
    if instance_ids is not None:
        predictions = [pred for pred in predictions
            if pred["instance_id"] in set(instance_ids)]
    dataset_path = get_dataset_path('dataset', run_id)
    dataset = load_file(dataset_path)
    dataset_by_id = {data_record["instance_id"]: data_record for data_record in dataset}
    succeeded, errored = [], {}

    for pred in predictions:
        instance_id = pred["instance_id"]
        data_record = dataset_by_id[instance_id]
        # The agent's container sweeps a compiled .pyc into the diff, and one binary block fails
        # the WHOLE `git apply` (mirrors dev_tools / build_repo).
        test_patch = filter_binary_files(pred.get("model_patch", ""))
        if not test_patch.strip():
            errored[instance_id] = "Empty model_patch"
            data_record.setdefault("keep", {})[KEEP_STAGE] = False
            continue
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # Applied only to prove it applies, so the tree is handed back either way.
        with RepoLocks.locked(data_record["project"]):
            reset_to_commit(repo_dir, data_record["base_commit"], new_branch=False)
            try:
                apply_patch(repo_dir, test_patch)
            except Exception as e:
                errored[instance_id] = f"Failed to apply test patch: {e}"
                data_record.setdefault("keep", {})[KEEP_STAGE] = False
                continue
            finally:
                reset_to_commit(repo_dir, data_record["base_commit"], new_branch=False)
        data_record[TEST_FIELD] = test_patch
        data_record.setdefault("keep", {})[KEEP_STAGE] = True
        succeeded.append(instance_id)

    print_summary(get_two_state_summary(succeeded, errored))
    if total_cost is not None:
        print(f"Agent cost: ${total_cost:.2f}")
    save_file(dataset, dataset_path)
    print(f"dataset annotated in place with {TEST_FIELD}: {dataset_path}.")


def pipeline(run_id, max_workers, instance_ids=None):
    """Build rollback images, run the test-synthesis agent over them, then take its tests on."""
    port = prologue(run_id, max_workers, instance_ids)
    if not port.task_instances:
        return
    port.run_batch()
    epilogue(run_id, instance_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build rollback env images, prepare the SWE-agent batch, and run the security "
                    "test-synthesis agent over it.")
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID (datasets/<run_id>/...)",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for building rollback images.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue: build rollback images and prepare the agent batch.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue: take the agent's tests onto the records as test_patch.",
    )
    args = parser.parse_args()

    if args.prologue:
        prologue(args.run_id, args.max_workers, args.instance_ids)
    elif args.epilogue:
        epilogue(args.run_id, args.instance_ids)
    else:
        pipeline(
            run_id=args.run_id,
            max_workers=args.max_workers,
            instance_ids=args.instance_ids,
        )
