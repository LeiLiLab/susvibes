"""
Purpose: Build environment Docker images from Env-agent output for each task instance.
The prologue pulls the canonical `dind_py` and `base_py` images, and tags the latter
as `base_py:<version>` locally.

python -m susvibes.curate.env_setup.build_repo \
    --prologue \
    --run_id playground

python -m susvibes.curate.env_setup.build_repo \
    --epilogue \
    --max_workers 5 \
    --run_id playground
"""

import argparse
import json
from tqdm import tqdm
from enum import StrEnum
from pathlib import Path
from jinja2 import Template
from concurrent.futures import ThreadPoolExecutor, as_completed

import re
import docker.errors
from docker.models.images import Image

from susvibes.core.constants import *
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR, get_log_dir, get_agent_setting_path
from susvibes.core.env import Deployment
from susvibes.env_specs import dockerfiles, DOCKERFILE_PATTERN, GIT_AUTHOR_CONFIGS, WORKSPACE_DIR_NAME
from susvibes.curate.env_setup.prompts import INSTALL_TEST_PROMPT_TEMPLATE
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.report import reuse_report, save_report, get_report_summary, print_summary
from susvibes.core.utils import load_file, save_file, filter_target_files, get_image_name, setup_instance_logger, parse_instance_id, touched_files, get_env_specs, save_env_specs, is_patch_error, PatchError
from susvibes.curate.utils import (
    RepoLocks,
    reset_to_commit,
    apply_patch,
    get_repo_dir,
    should_keep,
)

LOG_INSTANCE = "build_repo.log"
LOG_REPORT = "report.json"
LOG_SUMMARY = "summary.json"


KEEP_STAGE = KeepStage.BUILD_REPO   # this stage's verdict under record["keep"]
IMAGE_FIELD = "env_image_name"      # the image it built

# The daemon running the Dockerfile and failing is a verdict ON THE DOCKERFILE; an unreachable
# registry, a full disk or a dead daemon is the harness breaking. Naming the verdicts and leaving
# everything else an error is the safe default — a wasted rebuild costs minutes, while a harness
# failure mistaken for a verdict drops the instance and no `--resume` ever revisits it. Mirrors
# PATCH_ERROR_PATTERNS.
UNBUILDABLE_PATTERNS = ["returned a non-zero code:", "dockerfile parse error",
    "unknown instruction", "COPY failed", "ADD failed"]


class BuildRepoStatus(StrEnum):
    """How one instance's environment build concluded. Every member is a NORMAL outcome; the daemon
    or the registry failing is the report's `error`, which is what `--resume` re-runs."""
    BUILT = "built"                 # the image exists — see the report's env_image_name
    EMPTY_PATCH = "empty_patch"     # the agent submitted nothing
    PATCH_ERROR = "patch_error"     # the agent's patch does not apply to the base tree
    INVALID = "invalid"             # the Dockerfile fails this stage's checks — `reason` says which
    UNBUILDABLE = "unbuildable"     # the daemon ran the Dockerfile and it failed

PASS_STATUSES = {BuildRepoStatus.BUILT}   # only a built image clears the gate


class UnbuildableError(RuntimeError):
    """The daemon ran the Dockerfile and it failed. Raised where the build log is, so the caller
    sorts a verdict about the Dockerfile from the harness breaking by exception type. Mirrors
    PatchError."""


class InvalidDockerfileError(RuntimeError):
    """The agent's Dockerfile does not meet this stage's structural requirements — a verdict on
    what the agent wrote, reached without the daemon ever running it."""


def is_unbuildable(message: str) -> bool:
    """Whether a build failure is the daemon executing the Dockerfile and failing — a conclusion
    ABOUT THE DOCKERFILE, not the harness breaking. Anything unrecognised stays an error so a
    `--resume` re-runs it. Mirrors `is_patch_error`."""
    return any(pattern in message for pattern in UNBUILDABLE_PATTERNS)


def build_repo_report(status: BuildRepoStatus, **payload) -> dict:
    """A report for an instance that concluded: the status plus what names the outcome — the image
    and the Dockerfile it was built from, or the reason the Dockerfile was rejected."""
    return {"build_repo_status": status, **payload, "error": None}


def build_repo_miss(error: str) -> dict:
    """A report for a run that concluded nothing — the daemon, the registry or the clone failed."""
    return {"build_repo_status": None, "error": error}


def extract_dockerfile(prediction, logger):
    """Extract the Dockerfile from the model prediction patch. Raises PatchError /
    InvalidDockerfileError on a verdict about what the agent submitted, RuntimeError when the
    harness broke."""
    project, base_commit = parse_instance_id(prediction["instance_id"])
    repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, base_commit, new_branch=False)
    try:
        targets = {"Dockerfile"}
        apply_patch(repo_dir, filter_target_files(prediction["model_patch"], targets))
    except Exception as e:
        msg = f"Error applying model_patch: {e}"
        logger.error(msg)
        # `git apply` refusing the patch is a verdict on the submission; anything else here is git
        # or the clone breaking.
        if is_patch_error(str(e)):
            raise PatchError(msg)
        raise RuntimeError(msg)

    try:
        dockerfile = load_file(repo_dir / "Dockerfile")
    except FileNotFoundError:
        msg = "Dockerfile corresponding to the environment not found."
        logger.error(msg)
        raise InvalidDockerfileError(msg)
    return dockerfile


def validate_and_compose_env_dockerfile(dockerfile, logger):
    """Validate dockerfile structure and append a commit step before CMD."""
    dockerfile_re = re.compile(DOCKERFILE_PATTERN, re.MULTILINE | re.DOTALL)
    m = dockerfile_re.search(dockerfile)
    if not m:
        msg = "Dockerfile does not match expected pattern (FROM/COPY/CMD)."
        logger.error(msg)
        raise InvalidDockerfileError(msg)
    from_stm, _, cpy_stm, _, cmd_stm = m.groups()

    # Validate FROM uses base_py
    if not re.search(r'base_py:', from_stm):
        msg = f"Dockerfile FROM does not use base_py image: {from_stm.strip()}"
        logger.error(msg)
        raise InvalidDockerfileError(msg)

    # Validate WORKDIR /<WORKSPACE_DIR_NAME> is set somewhere in the dockerfile
    workdir_stm = f"WORKDIR /{WORKSPACE_DIR_NAME}"
    if workdir_stm not in dockerfile:
        msg = f"Dockerfile must contain '{workdir_stm}'."
        logger.error(msg)
        raise InvalidDockerfileError(msg)

    # Validate COPY and CMD exist
    if not cpy_stm.strip():
        msg = "Dockerfile missing COPY statement."
        logger.error(msg)
        raise InvalidDockerfileError(msg)
    if not cmd_stm.strip():
        msg = "Dockerfile missing CMD statement."
        logger.error(msg)
        raise InvalidDockerfileError(msg)

    # Insert git config + commit step before CMD
    run_stm = "RUN {}\n"
    git_config = run_stm.format(" && ".join(GIT_AUTHOR_CONFIGS))
    commit_cmd = run_stm.format('git add . && git commit --allow-empty -m "Env created." --no-verify')
    dockerfile = dockerfile[:m.start(5)] + git_config + commit_cmd + dockerfile[m.start(5):]
    return dockerfile


def strip_tag(image: Image, image_name: str) -> None:
    """Tag `image` with the hub-prefix-stripped local tag. `image_name` is the canonical
    `{username}/susvibes.{arch}.{local}:{version}`; keep `{local}:{version}` by taking the
    repo segment after the last `.` (the `:version` suffix is split off first)."""
    repo, _, version = image_name.rpartition(":")
    local_name = repo.rsplit(".", 1)[-1]
    image.tag(local_name, tag=version)


def build_env_deployment(instance_id, dockerfile, logger, nocache: bool = False) -> Deployment:
    """Build an environment Docker image. Raises InvalidDockerfileError / UnbuildableError on a
    verdict about the Dockerfile and RuntimeError when the harness broke; `nocache` bypasses
    docker's layer cache, which is otherwise what makes re-deriving a built image cheap."""
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
            nocache=nocache,
        )
    except docker.errors.BuildError as e:
        msg = f"Failed to build environment deployment: {e}"
        logger.error(msg)
        # Decide here, where the build log is: the daemon running the Dockerfile and failing is a
        # verdict on the Dockerfile, anything else is the harness breaking.
        if is_unbuildable(f"{e}\n{e.build_log}"):
            raise UnbuildableError(msg)
        raise RuntimeError(f"{msg}\n{e.build_log}")
    return env_deployment


def prologue(
    dataset_path: Path,
    instance_ids: list = None,
    exclude_projects: list = [],
    require_test: bool = True,
    run_id: str = "default",
):
    class SafeDict(dict):
        def __missing__(self, key):
            return '{' + key + '}'

    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("env_build")),
        run_name=__spec__.name, output_dir=get_log_dir(run_id, "env_setup", "build_repo"))
    dataset = load_file(dataset_path)
    env_specs = get_env_specs(run_id, ("dev_tools",))
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
            if data_record["instance_id"] in set(instance_ids)]
    # `keep` is the gate — env_specs is a lookup, and its keys being the gate is what used to make
    # an instance without a usable interpreter vanish with nothing recorded anywhere.
    dataset = [data_record for data_record in dataset
        if data_record["project"] not in exclude_projects
        and should_keep(data_record, exclude=KEEP_STAGE, required=(KeepStage.DEV_TOOLS,))]

    versions = {env_specs[d["instance_id"]]["dev_tools"]["version"] for d in dataset}
    for version in versions:
        base_py_image_name = f'{get_image_name("base_py")}:{version}'
        dind_py_image_name = f'{get_image_name("dind_py")}:{version}'
        try:
            base_py_image = Deployment.collect_image(image_name=base_py_image_name)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            raise RuntimeError(f"Base image not found: {base_py_image_name}")
        strip_tag(base_py_image, base_py_image_name)
        try:
            Deployment.collect_image(image_name=dind_py_image_name)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            raise RuntimeError(f"Dind image not found: {dind_py_image_name}")

    for data_record in dataset:
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The clone is shared: hold it across every touch of the tree.
        with RepoLocks.locked(data_record["project"]):
            reset_to_commit(repo_dir, data_record["base_commit"])
        dev_tool = env_specs[data_record["instance_id"]]["dev_tools"]
        dind_py_image_name = f'{get_image_name("dind_py")}:{dev_tool["version"]}'
        dockerfile_template = dockerfiles.DOCKERFILE_ENV_PY_TEMPLATE.format_map(
            SafeDict(base_image=f'base_py:{dev_tool["version"]}'))
        test_files = data_record["test_files"] if require_test else []
        # Fall back to security_patch files when task_patch is absent (dataset mode).
        coverage_files = sorted(touched_files(
            data_record.get("task_patch") or data_record.get("security_patch", "")))
        port.add_task(
            image=dind_py_image_name,
            repo_type="local",
            repo_dir=repo_dir,
            lock_path=RepoLocks.get_lock_path(repo_dir),
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
    run_id: str,
    dataset_path: Path,
    env_setup_log_dir: Path,
    max_workers: int,
    output_dir: Path = None,
    save_specs: bool = True,
    instance_ids: list = None,
    force: bool = False,
    resume: bool = False,
):
    predictions, _ = SWEAgentPort.after_completion(output_dir) if output_dir else (None, None)
    dataset = load_file(dataset_path)
    build_repo_threadpool(
        run_id, dataset, max_workers, env_setup_log_dir,
        predictions=predictions,
        save_specs=save_specs,
        instance_ids=instance_ids,
        force=force,
        resume=resume,
    )
    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")


def build_repo_single(
    data_record: dict,
    env_setup_log_dir: Path,
    prediction: dict = None,
    env_spec: dict = None,
    force: bool = False,
    resume: bool = False,
) -> dict:
    """Build one instance's environment image, and return a report either way: BUILT carrying the
    image and the Dockerfile it came from, a verdict on what the agent submitted, or an
    `error`-marked miss when the daemon or the registry failed.

    Unlike the other staged caches this one does not reuse a concluded report on a plain run: the
    conclusion depends on the Dockerfile, which the report is not keyed on, so a fresh agent round
    would otherwise keep shipping the old image. Docker's own layer cache already makes re-deriving
    cheap and, unlike a report, it invalidates itself when the Dockerfile changes. `--resume` keeps
    what concluded and re-runs only what errored; `--force` also bypasses that layer cache."""
    instance_id = data_record["instance_id"]
    project, _ = parse_instance_id(instance_id)
    env_image_name = get_image_name(f"env_{instance_id}")

    log_dir = env_setup_log_dir / instance_id
    # Only under `resume` — a plain run re-derives. The report is not keyed on the Dockerfile it
    # concluded about, so reusing BUILT would keep the old image after a fresh agent round; docker's
    # layer cache re-derives cheaply and, unlike a report, invalidates itself when it changes.
    if resume:
        report = reuse_report(log_dir / LOG_REPORT, resume=True)
        if report is not None:
            return report

    logger = setup_instance_logger(log_dir / LOG_INSTANCE, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Building environment for {instance_id}...")

    if prediction and not prediction.get("model_patch", "").strip():
        logger.error("Empty model_patch.")
        report = build_repo_report(BuildRepoStatus.EMPTY_PATCH)
    else:
        try:
            if prediction:
                with RepoLocks.locked(project):
                    dockerfile = extract_dockerfile(prediction, logger)
            else:
                dockerfile = env_spec["dockerfile"]
            with RepoLocks.locked(project):
                build_env_deployment(instance_id, dockerfile, logger, nocache=force)
        # PatchError and the two Dockerfile verdicts all subclass RuntimeError, so they are caught
        # ahead of it — the bare RuntimeError below is what is left over, the harness breaking.
        except PatchError:
            report = build_repo_report(BuildRepoStatus.PATCH_ERROR)
        except InvalidDockerfileError as e:
            report = build_repo_report(BuildRepoStatus.INVALID, reason=str(e))
        except UnbuildableError:
            report = build_repo_report(BuildRepoStatus.UNBUILDABLE)
        except Exception as e:
            report = build_repo_miss(str(e))
        else:
            logger.info(f"Environment built successfully: {env_image_name}")
            report = build_repo_report(BuildRepoStatus.BUILT,
                                       **{IMAGE_FIELD: env_image_name}, dockerfile=dockerfile)
    save_report(report, log_dir / LOG_REPORT)
    return report


def build_repo_threadpool(
    run_id: str,
    dataset: list,
    max_workers: int,
    env_setup_log_dir: Path,
    predictions: list = None,
    save_specs: bool = True,
    instance_ids: list = None,
    force: bool = False,
    resume: bool = False,
):
    pred_by_id = {pred["instance_id"]: pred for pred in predictions} if predictions else {}
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in dataset}
    env_specs = get_env_specs(run_id, ("dev_tools", "dockerfile"))
    # An instance is this stage's business if this run predicted for it OR an earlier run already
    # left a dockerfile. Taking one source or the other instead of both is what would leave a mixed
    # run's historical instances built by nobody and judged by nobody.
    gated_ids = (pred_by_id.keys() | env_specs.keys()) & {
        instance_id for instance_id, data_record in dataset_by_id.items()
        if should_keep(data_record, exclude=KEEP_STAGE, required=(KeepStage.DEV_TOOLS,))}
    if instance_ids is not None:
        gated_ids = gated_ids & set(instance_ids)

    reports = {}
    env_specs_path = None
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                build_repo_single,
                dataset_by_id[instance_id],
                env_setup_log_dir,
                prediction=pred_by_id.get(instance_id),
                env_spec=env_specs.get(instance_id),
                force=force,
                resume=resume,
            ): instance_id
            for instance_id in gated_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Building repos [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                reports[instance_id] = report
                data_record = dataset_by_id[instance_id]
                if report["build_repo_status"] == BuildRepoStatus.BUILT:
                    env_specs.setdefault(instance_id, {})["dockerfile"] = report["dockerfile"]
                    data_record[IMAGE_FIELD] = report[IMAGE_FIELD]
                # An errored run is judged too: leaving it unjudged is fail-open, and the record
                # would reach wrap_up on this stage's silence. `exclude` is what lets a re-run
                # revisit it.
                data_record.setdefault("keep", {})[KEEP_STAGE] = \
                    report["build_repo_status"] in PASS_STATUSES
                pbar.update(1)
                built = sum(1 for r in reports.values()
                            if r["build_repo_status"] == BuildRepoStatus.BUILT)
                pbar.set_description(f"{built} built, {len(reports) - built} not")
                if save_specs:
                    env_specs_path = save_env_specs("dockerfile", env_specs, run_id)
    summary = get_report_summary(reports, "build_repo_status")
    print_summary(summary)
    summary_path = Path(env_setup_log_dir) / LOG_SUMMARY
    save_report(summary, summary_path)
    print(f"Summary saved to {summary_path}.")
    if env_specs_path:
        print(f"Environments saved to {env_specs_path}.")


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
        "--from_existing_dockerfiles",
        action="store_true",
        help="Reuse the dockerfile stored in env_specs instead of extracting it from agent output.",
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
        "--force",
        action="store_true",
        help="Rebuild every instance with docker's layer cache bypassed.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Rebuild only the instances whose cached report is an errored run, keeping every "
             "concluded one. A plain run rebuilds all of them — docker's layer cache makes that "
             "cheap, and unlike a report it notices when the Dockerfile changed.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Require designated test files (default True); false runs the repo's whole test suite.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path('dataset', args.run_id)
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}")
        exit(1)

    if args.prologue:
        prologue(dataset_path, require_test=args.require_test, run_id=args.run_id,
            instance_ids=args.instance_ids)
    elif args.epilogue:
        env_setup_log_dir = get_log_dir(args.run_id, "env_setup")
        output_dir = None if args.from_existing_dockerfiles else get_log_dir(args.run_id, "env_setup", "build_repo")
        epilogue(
            args.run_id, dataset_path, env_setup_log_dir,
            args.max_workers, output_dir,
            save_specs=not args.skip_specs,
            instance_ids=args.instance_ids,
            force=args.force,
            resume=args.resume,
        )
    else:
        print("Please specify either --prologue or --epilogue.")
