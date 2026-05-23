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
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

import docker
import docker.errors
from tqdm import tqdm
from jinja2 import Template

from susvibes.constants import get_env_spec_path
from susvibes.curate.constants import ENV_SETUP_LOG_DIR, get_path
from susvibes.curate.test.prompts import (
    SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE,
    SEC_TEST_GEN_SECFIX_PROMPT_TEMPLATE,
)
from susvibes.curate.utils import extract_repo_test_cmd, reverse_patch
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.env import Env
from susvibes.env_specs import BUILD_DATA_DIR_NAME, PATCHES_DIR_NAME, WORKSPACE_DIR_NAME
from susvibes.utils import load_file, get_image_name, parse_instance_id, setup_instance_logger

SECURITY_PATCH_FILE = ".susvibes.security_patch.diff"
LOG_INSTANCE = "gen_prologue.log"

docker_client = docker.from_env()


class HintStrategy(Enum):
    PATCH_SECFIX = "patch_secfix"
    SECFIX = "secfix"


HINT_STRATEGY_TEMPLATES = {
    HintStrategy.PATCH_SECFIX: SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE,
    HintStrategy.SECFIX: SEC_TEST_GEN_SECFIX_PROMPT_TEMPLATE,
}


def build_rollback_image(data_record, env_spec, log_dir):
    """Build a rollback variant of the env image with security_patch reversed
    (so /project sits in the vulnerable state) and the patch persisted at
    .susvibes.security_patch.diff. Returns the image name (or None on failure)."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)
    rollback_image_name = get_image_name(f"rollback_{instance_id}")

    log_file = log_dir / instance_id / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)

    try:
        env = Env(
            logger=logger,
            project=project,
            image_name=data_record["env_image_name"],
            dockerfile=env_spec["dockerfile"],
        )
        deployment = env.build_instance_deployment(
            base_commit=data_record["base_commit"],
            patches={"post_install": (data_record["security_patch"], "-R")},
            logger=logger,
            remove_image=False,
            persist_files=[(
                f"/{BUILD_DATA_DIR_NAME}/{PATCHES_DIR_NAME}/post_install/0.patch",
                SECURITY_PATCH_FILE,
            )],
        )
    except (docker.errors.BuildError, docker.errors.ImageNotFound) as e:
        logger.error(f"Failed to build rollback image for {instance_id}: {e}")
        return None

    assert deployment.image.tag(rollback_image_name)
    logger.info(f"Rollback image built: {rollback_image_name}")
    return rollback_image_name


def build_rollback_threadpool(records, env_specs, log_dir, max_workers, force=False):
    """Build rollback images for the given records in parallel.
    Returns (image_by_id, failed) — a dict of successful builds and a list of failures."""
    image_by_id, failed = {}, []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                build_rollback_image,
                record,
                env_specs[record["instance_id"]],
                log_dir,
            ): record["instance_id"]
            for record in records
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Building rollback images [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if result:
                    image_by_id[instance_id] = result
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(image_by_id)} built, {len(failed)} failed"
                )
    return image_by_id, failed


def prologue(run_id, strategy, max_workers, instance_ids=None, force=False):
    strategy = HintStrategy(strategy)
    port = SWEAgentPort(run_name=f"{__spec__.name}_{strategy.value}")

    prompt_template = HINT_STRATEGY_TEMPLATES[strategy]

    dataset_path = get_path('dataset', run_id)
    env_specs_path = get_env_spec_path('components', run_id)
    log_dir = ENV_SETUP_LOG_DIR / run_id

    dataset = load_file(dataset_path)
    env_specs = load_file(env_specs_path)

    candidates = [r for r in dataset if r["instance_id"] in env_specs]
    if instance_ids is not None:
        candidates = [r for r in candidates if r["instance_id"] in set(instance_ids)]

    image_by_id, failed = build_rollback_threadpool(
        candidates, env_specs, log_dir, max_workers, force=force)

    added = 0
    for record in candidates:
        instance_id = record["instance_id"]
        if instance_id not in image_by_id:
            continue
        repo_test_cmd = extract_repo_test_cmd(env_specs[instance_id]["dockerfile"])
        render_kwargs = {
            "SECURITY_PATCH": record.get("security_patch", ""),
            "REPO_TEST_CMD": repo_test_cmd,
        }
        if strategy is HintStrategy.PATCH_SECFIX:
            render_kwargs["PATCH"] = reverse_patch(record["mask_patch"])
        problem_statement = Template(prompt_template).render(**render_kwargs)

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
        "--strategy",
        type=str,
        default=HintStrategy.PATCH_SECFIX.value,
        choices=[s.value for s in HintStrategy],
        help="Hint strategy for the problem statement.",
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
        "--force",
        action="store_true",
        help="Force re-build the rollback images even if they already exist locally.",
    )
    args = parser.parse_args()
    prologue(
        run_id=args.run_id,
        strategy=args.strategy,
        max_workers=args.max_workers,
        instance_ids=args.instance_ids,
        force=args.force,
    )
