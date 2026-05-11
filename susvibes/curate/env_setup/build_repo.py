"""
Purpose: Build environment Docker images from Env-agent output for each task instance.
The prologue pulls the canonical `dind_py` and `base_py` images, and tags the latter
as `base_py:<version>` locally.

python -m susvibes.curate.env_setup.build_repo \
    --prologue \
    --run_id playground

python -m susvibes.curate.env_setup.build_repo \
    --epilogue \
    --agent_output_dir <path_to_agent_output> \
    --max_workers 5 \
    --run_id playground
"""

import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template
from concurrent.futures import ThreadPoolExecutor, as_completed

import re
import docker
import docker.errors

from susvibes.constants import *
from susvibes.curate.constants import LOCAL_REPOS_DIR, ENV_SETUP_LOG_DIR, get_path
from susvibes.env import Deployment
from susvibes.env_specs import dockerfiles, DOCKERFILE_PATTERN, GIT_AUTHOR_CONFIGS, WORKSPACE_DIR_NAME
from susvibes.curate.env_setup.prompts import INSTALL_TEST_PROMPT_TEMPLATE
from susvibes.curate.utils.agents.ports import EnvAgentPort
from susvibes.utils import load_file, save_file, filter_patch, get_image_name, setup_instance_logger, parse_instance_id, touched_files
from susvibes.curate.utils import (
    RepoLocks,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
    get_repo_dir,
)

docker_client = docker.from_env()

LOG_INSTANCE = "build_repo.log"


def extract_dockerfile(prediction, logger):
    """Extract the Dockerfile from the model prediction patch."""
    project, base_commit = parse_instance_id(prediction["instance_id"])
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, base_commit, new_branch=False)
    try:
        targets = {"Dockerfile"}
        apply_patch(repo_dir, filter_patch(prediction["model_patch"], targets))
    except Exception as e:
        msg = f"Error applying model patch: {e}"
        logger.error(msg)
        raise RuntimeError(msg)

    try:
        dockerfile = load_file(repo_dir / "Dockerfile")
    except FileNotFoundError:
        msg = "Dockerfile corresponding to the environment not found."
        logger.error(msg)
        return RuntimeError(msg)
    return dockerfile

def validate_and_compose_env_dockerfile(dockerfile, logger):
    """Validate dockerfile structure and append a commit step before CMD."""
    dockerfile_re = re.compile(DOCKERFILE_PATTERN, re.MULTILINE | re.DOTALL)
    m = dockerfile_re.search(dockerfile)
    if not m:
        msg = "Dockerfile does not match expected pattern (FROM/COPY/CMD)."
        logger.error(msg)
        raise RuntimeError(msg)
    from_stm, _, cpy_stm, _, cmd_stm = m.groups()

    # Validate FROM uses base_py
    if not re.search(r'base_py:', from_stm):
        msg = f"Dockerfile FROM does not use base_py image: {from_stm.strip()}"
        logger.error(msg)
        raise RuntimeError(msg)

    # Validate WORKDIR /<WORKSPACE_DIR_NAME> is set somewhere in the dockerfile
    workdir_stm = f"WORKDIR /{WORKSPACE_DIR_NAME}"
    if workdir_stm not in dockerfile:
        msg = f"Dockerfile must contain '{workdir_stm}'."
        logger.error(msg)
        raise RuntimeError(msg)

    # Validate COPY and CMD exist
    if not cpy_stm.strip():
        msg = "Dockerfile missing COPY statement."
        logger.error(msg)
        raise RuntimeError(msg)
    if not cmd_stm.strip():
        msg = "Dockerfile missing CMD statement."
        logger.error(msg)
        raise RuntimeError(msg)

    # Insert git config + commit step before CMD
    run_stm = "RUN {}\n"
    git_config = run_stm.format(" && ".join(GIT_AUTHOR_CONFIGS))
    commit_cmd = run_stm.format('git add . && git commit --allow-empty -m "Env created." --no-verify')
    dockerfile = dockerfile[:m.start(5)] + git_config + commit_cmd + dockerfile[m.start(5):]
    return dockerfile


def pull_canonical(short_name: str, version: str) -> str:
    """Ensure the canonical hub-named image for {short_name}:{version} is
    locally available; pull from Docker Hub if missing. Returns the canonical
    name."""
    canonical = f"{get_image_name(short_name)}:{version}"
    try:
        docker_client.images.get(canonical)
    except docker.errors.ImageNotFound:
        docker_client.images.pull(canonical)
    return canonical


def pull_short_tag(short_name: str, version: str) -> str:
    """Ensure {short_name}:{version} exists locally as a tag of the canonical
    hub-named image; pull and re-tag if missing. Returns the short tag."""
    short = f"{short_name}:{version}"
    try:
        docker_client.images.get(short)
        return short
    except docker.errors.ImageNotFound:
        pass
    canonical = pull_canonical(short_name, version)
    docker_client.images.get(canonical).tag(short_name, tag=version)
    return short


def build_env_deployment(instance_id, dockerfile, logger):
    """Build a environment Docker image."""
    project, base_commit = parse_instance_id(instance_id)
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)
    env_image_name = get_image_name(f"env_{instance_id}")
    try:
        dockerfile = validate_and_compose_env_dockerfile(dockerfile, logger)
        reset_to_commit(repo_dir, base_commit)
        # Remove repo's .dockerignore to ensure .git is included in build context
        repo_dockerignore = repo_dir / ".dockerignore"
        if repo_dockerignore.exists():
            repo_dockerignore.unlink()
        env_deployment = Deployment.from_build(
            logger=logger,
            context_path=repo_dir,
            dockerfile=dockerfile,
            image_name=env_image_name,
        )
    except docker.errors.BuildError as e:
        msg = "Failed to get environment deployment."
        logger.error(msg)
        raise RuntimeError(msg)
    return env_deployment


def prologue(
    task_dataset_path: Path,
    instance_ids: list = None,
    exclude_projects: list = [],
    no_require_test: bool = False,
    run_id: str = "default",
):
    class SafeDict(dict):
        def __missing__(self, key):
            return '{' + key + '}'

    port = EnvAgentPort(run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    dev_tools = load_file(get_env_spec_path('dev_tools', run_id))
    if instance_ids != None:
        task_dataset = [data_record for data_record in task_dataset
            if data_record["instance_id"] in instance_ids]
    task_dataset = [data_record for data_record in task_dataset
        if data_record["project"] not in exclude_projects
        and data_record["instance_id"] in dev_tools]

    versions = {dev_tools[d["instance_id"]]["version"] for d in task_dataset}
    for version in versions:
        pull_short_tag("base_py", version)
        pull_canonical("dind_py", version)

    for data_record in task_dataset:
        repo_dir = clone_github_repo(data_record["project"], root_dir=LOCAL_REPOS_DIR, force=False)
        reset_to_commit(repo_dir, data_record["base_commit"])
        dev_tool = dev_tools[data_record["instance_id"]]
        image_name = f'{get_image_name("dind_py")}:{dev_tool["version"]}'
        dockerfile_template = dockerfiles.DOCKERFILE_ENV_PY_TEMPLATE.format_map(
            SafeDict(base_image=f'base_py:{dev_tool["version"]}'))
        test_files = [] if no_require_test else data_record["test_files"]
        coverage_files = sorted(touched_files(data_record.get("task_patch", ""))) \
            if no_require_test else []
        port.add_task(
            image=image_name,
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=data_record["base_commit"],
            problem_statement=Template(INSTALL_TEST_PROMPT_TEMPLATE).render(
                test_files=test_files,
                coverage_files=coverage_files,
                dockerfile_template=dockerfile_template
            ),
            instance_id=data_record["instance_id"],
        )
    port.before_start()


def epilogue(
    task_dataset_path: Path,
    dataset_path: Path,
    env_specs_path: Path,
    env_setup_log_dir: Path,
    max_workers: int,
    agent_output_dir: Path = None,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    predictions, _ = EnvAgentPort.after_completion(agent_output_dir) if agent_output_dir else (None, None)
    task_dataset = load_file(task_dataset_path)
    dataset = build_repo_threadpool(
        task_dataset, max_workers, env_specs_path, env_setup_log_dir,
        predictions=predictions,
        force=force,
        save_specs=save_specs,
        instance_ids=instance_ids,
    )
    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")


def build_repo_single(
    data_record: dict,
    env_setup_log_dir: Path,
    prediction: dict = None,
    env_spec: dict = None,
    force: bool = False,
) -> tuple[dict, str] | None:
    """Build environment Docker image for a single instance.
    Returns (env_spec, image_name) on success, None on failure."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)
    env_image_name = get_image_name(f"env_{instance_id}")

    log_dir = env_setup_log_dir / instance_id
    log_file = log_dir / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Building environment for {instance_id}...")

    try:
        if prediction:
            if not prediction.get("model_patch", "").strip():
                msg = "Empty model patch."
                logger.error(msg)
                raise RuntimeError(msg)
            with RepoLocks.locked(project):
                dockerfile = extract_dockerfile(prediction, logger)
        elif env_spec:
            dockerfile = env_spec["dockerfile"]
        else:
            msg = "No environment prediction or specification provided."
            logger.error(msg)
            raise RuntimeError(msg)
    except RuntimeError as e:
        return None

    if env_spec is None:
        env_spec = {}
    if not force and env_spec.get("dockerfile") == dockerfile:
        try:
            docker_client.images.get(env_image_name)
            logger.info("Environment image matches env_spec and exists locally; skipping build.")
            return env_spec, env_image_name
        except docker.errors.ImageNotFound:
            pass

    try:
        with RepoLocks.locked(project):
            env_deployment = build_env_deployment(instance_id, dockerfile, logger)
    except RuntimeError as e:
        return None

    env_spec["dockerfile"] = dockerfile
    logger.info(f"Environment built successfully: {env_image_name}")
    return env_spec, env_image_name


def build_repo_threadpool(
    task_dataset: list,
    max_workers: int,
    env_specs_path: Path,
    env_setup_log_dir: Path,
    predictions: list = None,
    force: bool = False,
    save_specs: bool = True,
    instance_ids: list = None,
):
    pred_by_id = {pred["instance_id"]: pred for pred in predictions} if predictions else {}
    task_dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in task_dataset}
    env_specs = load_file(env_specs_path) if env_specs_path.exists() else {}
    candidate_ids = pred_by_id if pred_by_id else env_specs
    candidate_ids = candidate_ids.keys() & task_dataset_by_id.keys()
    if instance_ids is not None:
        candidate_ids = candidate_ids & set(instance_ids)

    dataset = []
    succeeded, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                build_repo_single,
                task_dataset_by_id[instance_id],
                env_setup_log_dir,
                prediction=pred_by_id.get(instance_id),
                env_spec=env_specs.get(instance_id),
                force=force,
            ): instance_id
            for instance_id in candidate_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Building repos [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if result:
                    new_env_spec, image_name = result
                    env_specs[instance_id] = new_env_spec
                    data_record = task_dataset_by_id[instance_id].copy()
                    data_record["env_image_name"] = image_name
                    dataset.append(data_record)
                    succeeded.append(instance_id)
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} built, {len(failed)} failed"
                )
                if save_specs:
                    save_file(env_specs, env_specs_path)
    if succeeded:
        print(f"Succeeded ({len(succeeded)}):")
        for instance_id in sorted(succeeded):
            print(f"  {instance_id}")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for instance_id in sorted(failed):
            print(f"  {instance_id}")
    if save_specs:
        print(f"Environments saved to {env_specs_path}.")
    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build environment Docker images for task instances.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue: prepare Env-agent tasks.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue: build env images from agent output.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored.",
    )
    parser.add_argument(
        "--from_existing_specs",
        action="store_true",
        help="Reuse the dockerfile stored in env_specs instead of extracting it from agent output.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-build the env image even if a matching env_spec and local image are already cached.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for building.",
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
        "--no_require_test",
        action="store_true",
        help="Do not require mandatory test files; run the repo's entire test suite instead.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    args = parser.parse_args()

    task_dataset_path = get_path('task_dataset', args.run_id)
    if not task_dataset_path.exists():
        fallback_path = get_path('processed_dataset', args.run_id)
        if fallback_path.exists():
            answer = input(f"task_dataset not found. Use {fallback_path} instead? [Y/n] ").strip().lower()
            if answer in ('', 'y'):
                task_dataset_path = fallback_path
            else:
                print("Aborted.")
                exit(1)
        else:
            print(f"Neither task_dataset nor processed_dataset found under {task_dataset_path.parent}.")
            exit(1)

    if args.prologue:
        prologue(task_dataset_path, no_require_test=args.no_require_test, run_id=args.run_id)
    elif args.epilogue:
        dataset_path = get_path('dataset', args.run_id)
        env_specs_path = get_env_spec_path('components', args.run_id)
        env_setup_log_dir = ENV_SETUP_LOG_DIR / args.run_id
        agent_output_dir = None if args.from_existing_specs else args.agent_output_dir
        epilogue(
            task_dataset_path, dataset_path, env_specs_path, env_setup_log_dir,
            args.max_workers, agent_output_dir,
            force=args.force,
            save_specs=not args.skip_specs,
            instance_ids=args.instance_ids,
        )
    else:
        print("Please specify either --prologue or --epilogue.")
