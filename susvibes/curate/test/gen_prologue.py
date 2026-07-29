"""
Purpose: Prologue for security test generation. Build a rollback variant
of each instance's env image (security_patch reversed, original patch persisted
at .susvibes.security_patch.diff so the agent can toggle states), then assemble
the SWE-agent batch instances yaml.

python -m susvibes.curate.test.gen_prologue \
    --max_workers 5 \
    --run_id playground
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker
import docker.errors
from tqdm import tqdm
from jinja2 import Template

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import get_log_dir, get_agent_setting_path
from susvibes.curate.test.prompts import SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE
from susvibes.curate.utils import extract_repo_test_cmd, reverse_patch
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.env import Env, Deployment
from susvibes.env_specs import WORKSPACE_DIR_NAME
from susvibes.core.utils import load_file, get_image_name, parse_instance_id, setup_instance_logger, get_env_specs

LOG_INSTANCE = "gen_prologue.log"
SECURITY_PATCH_FILE_NAME = ".susvibes.security_patch.diff"  # kept in repo root for state toggling

docker_client = docker.from_env()


def build_rollback_deployment(data_record, env_spec, target_image_name, log_dir) -> Deployment | None:
    """Build a rollback variant of the env image with security_patch reversed
    (so /project sits in the vulnerable state) and the patch persisted at
    .susvibes.security_patch.diff, tagged target_image_name. Returns the
    Deployment (or None on failure)."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)

    log_file = log_dir / instance_id / LOG_INSTANCE
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
        logger.error(f"Failed to build rollback deployment for {instance_id}: {e}")
        return None

    assert deployment.image.tag(target_image_name)
    logger.info(f"Rollback deployment built: {target_image_name}")
    return deployment


def build_rollback_threadpool(records, env_specs, log_dir, max_workers):
    """Build rollback images for the given records in parallel.
    Returns (image_by_id, failed) — a dict of successful builds and a list of failures."""
    image_by_id, failed = {}, []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for record in records:
            instance_id = record["instance_id"]
            rollback_image_name = get_image_name(f"rollback_{instance_id}")
            futures[executor.submit(
                build_rollback_deployment,
                record,
                env_specs[instance_id],
                rollback_image_name,
                log_dir,
            )] = (instance_id, rollback_image_name)
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Building rollback images [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id, rollback_image_name = futures[future]
                try:
                    deployment = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if deployment:
                    image_by_id[instance_id] = rollback_image_name
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(image_by_id)} built, {len(failed)} failed"
                )
    if image_by_id:
        print(f"Succeeded ({len(image_by_id)}):")
        for instance_id in sorted(image_by_id):
            print(f"  {instance_id}")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for instance_id in sorted(failed):
            print(f"  {instance_id}")
    return image_by_id, failed


def prologue(run_id, max_workers, instance_ids=None):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")),
        run_name=__spec__.name, output_dir=get_log_dir(run_id, "test", "gen_prologue"))

    dataset_path = get_dataset_path('env_dataset', run_id)
    log_dir = get_log_dir(run_id, "test")

    dataset = load_file(dataset_path)
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))

    candidates = [r for r in dataset if r["instance_id"] in env_specs]
    if instance_ids is not None:
        candidates = [r for r in candidates if r["instance_id"] in set(instance_ids)]

    image_by_id, failed = build_rollback_threadpool(
        candidates, env_specs, log_dir, max_workers)

    added = 0
    for record in candidates:
        instance_id = record["instance_id"]
        if instance_id not in image_by_id:
            continue
        repo_test_cmd = extract_repo_test_cmd(env_specs[instance_id]["dockerfile"])
        secprop = record["secprop"]
        render_kwargs = {
            "VULN_CLASS": secprop["vuln_class"],
            "RISK_NARRATIVE": secprop["risk_narrative"],
            "INVARIANT": secprop["invariant"],
            "VULNERABLE_IF": secprop["vulnerable_if"],
            "SECURE_IF": secprop["secure_if"],
            "IRRELEVANT": secprop["security_irrelevant_differences"],
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
    print(f"Added {added} tasks. {len(failed)} builds failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build rollback env images and prepare SWE-agent batch for security test generation.")
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
    args = parser.parse_args()
    prologue(
        run_id=args.run_id,
        max_workers=args.max_workers,
        instance_ids=args.instance_ids,
    )
