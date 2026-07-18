"""Static file-level test-coverage analysis for collected CVE instances.

Runs after `process`. For each instance in ``fix_dataset.jsonl`` it decides, by
static analysis only (no execution), whether the repo's own test suite covers the
``security_patch`` files — at the FILE level. Analysis runs on the rollback tree
(``base_commit`` + reverse(security_patch), the vulnerable state), so targets are the
patch's PRE-side files.

Per-version Docker isolation. Each instance is analyzed INSIDE a container whose
Python version matches the instance (from ``dev_tools.json``), so the engine runs on
that interpreter's NATIVE ast/jedi/parso — no py2/py3 parser conflicts. The self
contained ``engine/`` package (S*/F*/H* scoring; see check_cov.md) is COPYied into
a thin per-instance image built ``FROM`` the version-matched cov_py image (prebuilt by build_base),
run once, and torn down (image + container removed). The host only orchestrates:
roll the repo back to the vulnerable tree, snapshot sources, build/run the container,
read the JSON result from its logs.

Because jedi runs in the container (a fresh process each time, naturally isolated),
the host needs no per-instance process isolation: a ThreadPoolExecutor with the
per-repo RepoLocks (reset of the shared clone is serialized per repo name) drives
it, one container per worker.

The goal is to MINIMIZE false negatives: a file that really is tested must not be
marked uncovered.

Usage:
    python -m susvibes.curate.mine.check_cov --run_id <id> [--max_workers N]
"""
import argparse
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker.errors
from tqdm import tqdm

from susvibes.core.constants import ContainerLimits, get_dataset_path
from susvibes.env_specs.constants import (
    WORKSPACE_DIR_NAME,
    SUSVIBES_RUNTIME_DATA_DIR,
)
from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import (
    load_file, save_file, touched_files, setup_instance_logger, get_image_name, get_env_specs,
)
from susvibes.curate.utils import (
    get_repo_dir,
    rollback,
    run,
    RepoLocks,
    get_summary,
    print_summary,
)
from susvibes.core.env import Deployment
from susvibes.curate.mine.check_cov.engine.constants import SymbolTrace
from susvibes.curate.mine.check_cov.engine.extract_facts import TARGET_EXTENSIONS

LOG_INSTANCE = "check_cov.log"
LOG_COV_OUTPUT = "cov_output.txt"
LOG_SUMMARY = "summary.json"

# Files laid into each per-instance build context (context_path) and where each lands in
# the image (by prepare_engine_context + compose_cov_dockerfile):
#   context_path/<WORKSPACE_DIR_NAME>/  (repo tree)  -> image  /<WORKSPACE_DIR_NAME>
#   context_path/<ENGINE_PKG_NAME>/     (engine pkg) -> image  <SITE_PACKAGES_DIR>/<ENGINE_PKG_NAME>
#   context_path/<SUSVIBES_RUNTIME_DATA_DIR>/<INPUT_FILE_NAME>  (input json) -> image  <SUSVIBES_RUNTIME_DATA_DIR>/<INPUT_FILE_NAME>
ENGINE_DIR = Path(__file__).parent / "engine"       # host: engine source to copy in
ENGINE_PKG_NAME = "_susvibes_cov_engine"             # engine package name (context dir + image pkg)
INPUT_FILE_NAME = "input.json"                       # worker input: instance meta + targets
SITE_PACKAGES_DIR = "/usr/local/lib/python{version}/site-packages"  # purelib (all 9 cov bases)

RESULT_MARKER = "<<<COV_RESULT>>>"

class CovContainerLimits:
    """Per-instance cov container limits. The analysis is light (one single-threaded
    jedi process — get_references does not parallelize), so CPU is kept small to let
    many instances run concurrently, capped at the host's share so a small box isn't
    oversubscribed. The run is hard-capped so a hung container can't stall the pool."""
    RUN_TIMEOUT = 600   # seconds — hard cap per instance container
    MEM_LIMIT = ContainerLimits.MEM_LIMIT
    CPU_LIMIT = min(2, max(1, int(os.cpu_count() * 0.75)))


# --- per-instance build context --------------------------------------------

def prepare_engine_context(repo_dir: Path, data_record: dict, targets: list[str],
                           max_depth: int, context_path: Path) -> None:
    """Lay the repo tree, engine package, and worker input into the build context (see the
    build-context constants above for the exact names and their image targets). The repo is
    rsync'd -aHAX (max fidelity); .git pruning and .py selection happen in the worker."""
    workspace_dir = context_path / WORKSPACE_DIR_NAME
    workspace_dir.mkdir(parents=True, exist_ok=True)
    run(["rsync", "-aHAX",
         str(repo_dir).rstrip("/") + "/", str(workspace_dir).rstrip("/") + "/"])
    engine_dir = context_path / ENGINE_PKG_NAME
    engine_dir.mkdir(parents=True, exist_ok=True)
    run(["rsync", "-aHAX", "--exclude=__pycache__", "--exclude=*.pyc",
         str(ENGINE_DIR).rstrip("/") + "/", str(engine_dir).rstrip("/") + "/"])
    # The rsync'd tree is the rollback (pre) state; the worker derives each touched file's
    # post (base) version in-container by forward-applying the security_patch, so the host
    # ships only the patch text (no git-show) and containment parses with version-matched parso.
    inp = {
        "instance_id": data_record["instance_id"],
        "project": data_record["project"],
        "base_commit": data_record["base_commit"],
        "targets": targets,
        "max_depth": max_depth,
        "security_patch": data_record["security_patch"],
    }
    runtime_dir = context_path / SUSVIBES_RUNTIME_DATA_DIR.lstrip("/")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / INPUT_FILE_NAME).write_text(json.dumps(inp), encoding="utf-8")


# --- container run / result --------------------------------------------------

def parse_cov_result(logs: str) -> dict | None:
    """Extract the CoverageResult JSON the worker printed after RESULT_MARKER."""
    if not logs or RESULT_MARKER not in logs:
        return None
    tail = logs.split(RESULT_MARKER)[-1].strip()
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            return None
    return None


def compose_cov_dockerfile(version: str) -> str:
    """Per-instance Dockerfile over the version-matched cov_py base (see the build-context
    constants for what lands where). The engine is installed as a site-packages library, not on
    PYTHONPATH=/, so jedi treats it as a library and won't scan the container root and time out
    on large repos."""
    cov_py_image = f'{get_image_name("cov_py")}:{version}'
    site_packages = SITE_PACKAGES_DIR.format(version=version)
    return (
        "FROM {base_image}\n"
        "COPY {ws} /{ws}\n"
        "COPY {engine} {site_packages}/{engine}\n"
        "COPY {runtime_rel}/{input} {runtime}/{input}\n"
        'CMD ["python", "-m", "{engine}.worker", "/{ws}", "{runtime}/{input}"]\n'
    ).format(base_image=cov_py_image, ws=WORKSPACE_DIR_NAME, engine=ENGINE_PKG_NAME,
             site_packages=site_packages, input=INPUT_FILE_NAME,
             runtime=SUSVIBES_RUNTIME_DATA_DIR,
             runtime_rel=SUSVIBES_RUNTIME_DATA_DIR.lstrip("/"))


def build_cov_deployment(data_record: dict, version: str, context_path: Path,
                         logger: logging.Logger) -> Deployment | None:
    """Build the per-instance cov image over context_path; return the Deployment
    (image + container auto-removed after the run) or None on build failure."""
    image_name = get_image_name("cov_{0}".format(data_record["instance_id"]))
    try:
        return Deployment.from_build(
            logger=logger,
            context_path=context_path,
            dockerfile=compose_cov_dockerfile(version),
            image_name=image_name,
            remove_image=True,
            remove_container=True,
        )
    except docker.errors.BuildError as e:
        logger.error(f"Failed to build cov deployment: {e}")
        return None


# --- orchestration ---------------------------------------------------------

def check_cov_single(data_record: dict, check_cov_log_dir: Path, dev_tools: dict,
                     max_depth: int = SymbolTrace.MAX_DEPTH,
                     force: bool = False) -> tuple[dict | None, str | None]:
    """Analyze one instance's file-level test coverage on the rollback tree
    (base_commit + reverse(security_patch)), inside a version-matched cov container.
    Returns (CoverageResult, None) on success, (None, reason) on failure."""
    instance_id = data_record["instance_id"]
    project = data_record["project"]
    base_commit = data_record["base_commit"]

    log_file = Path(check_cov_log_dir) / instance_id / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking test coverage for {instance_id}...")

    # Reuse a prior run's saved container logs if present — re-parsing them reproduces the
    # result with no rebuild/rerun; --force re-runs from scratch.
    cov_output_path = Path(check_cov_log_dir) / instance_id / LOG_COV_OUTPUT
    if not force and cov_output_path.exists():
        logger.info("Container logs found; reusing.")
        result = parse_cov_result(load_file(cov_output_path))
        reason = None if result is not None else "Failed to parse coverage logs."
    else:
        version = dev_tools[instance_id]["version"]
        # Targets are the security_patch's PRE-side files (the names present in the
        # rollback tree), filtered to target-language (.py). A fix that only ADDS a
        # .py file yields none — the vulnerable code has no such file to cover.
        targets = sorted(t for t in touched_files(data_record["security_patch"], side="pre")
                         if t.endswith(TARGET_EXTENSIONS))
        if not targets:
            msg = "No target-language file in the rollback tree (security_patch only adds files)."
            logger.info(msg)
            return None, msg
        repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)
        result, reason = None, "Failed to build cov deployment."
        with tempfile.TemporaryDirectory(prefix="cov_") as tmpdir:
            context_path = Path(tmpdir)
            # rollback + snapshot under the per-repo lock (the shared clone is mutated);
            # the build context is an independent copy, so build/run happen lock-free.
            with RepoLocks.locked(project):
                rollback(repo_dir, base_commit, data_record["security_patch"])
                prepare_engine_context(repo_dir, data_record, targets, max_depth, context_path)
            cov_deployment = build_cov_deployment(data_record, version, context_path, logger)
            # Run the worker in the container, read its CoverageResult from the logs.
            # create_container / run_with_timeout self-clean on failure; errors leave result=None.
            if cov_deployment:
                try:
                    cov_deployment.create_container(mem_limit=CovContainerLimits.MEM_LIMIT,
                                                    cpu_limit=CovContainerLimits.CPU_LIMIT)
                except docker.errors.APIError as e:
                    reason = f"Failed to create container: {e}"
                    logger.error(reason)
                else:
                    try:
                        logs, timed_out = cov_deployment.run_with_timeout(timeout=CovContainerLimits.RUN_TIMEOUT)
                    except docker.errors.APIError as e:
                        reason = f"Failed to start container: {e}"
                        logger.error(reason)
                    else:
                        save_file(logs, cov_output_path)
                        if timed_out:
                            reason = "Failed to run cov container because of timeout."  # run_with_timeout already logged it
                        else:
                            result = parse_cov_result(logs)
                            if result is None:
                                reason = "Failed to parse coverage logs."
                                logger.error(reason)

    if result is None:
        return None, reason
    logger.info(f"Coverage for {instance_id}: {result['label']} "
                f"(score {result.get('score')}, engine {result.get('engine')}): {result.get('reason')}")
    return result, None


def check_cov_threadpool(fix_dataset: list, max_workers: int, coverage_report_path: Path,
                         check_cov_log_dir: Path, dev_tools: dict,
                         instance_ids: list = None,
                         max_depth: int = SymbolTrace.MAX_DEPTH,
                         force: bool = False) -> dict:
    """Analyze each instance in its own version-matched container, write every
    instance that ran to the coverage report, and save a run summary (succeeded
    coverage labels + failed reasons).

    One container per worker thread; the host imports no jedi, so threads (not
    processes) suffice. Per-repo resets are serialized by RepoLocks; distinct repos
    run concurrently."""
    records = fix_dataset
    if instance_ids is not None:
        wanted = set(instance_ids)
        records = [r for r in records if r["instance_id"] in wanted]

    # Cov images are a hard dependency for instances that will run — an instance reused
    # from its existing cov_output.txt needs none. Fail fast if a needed version is missing.
    versions = {v for r in records
                if (force or not (Path(check_cov_log_dir) / r["instance_id"] / LOG_COV_OUTPUT).exists())
                and (v := (dev_tools.get(r["instance_id"]) or {}).get("version"))}
    for version in versions:
        cov_py_image = f'{get_image_name("cov_py")}:{version}'
        try:
            Deployment.collect_image(image_name=cov_py_image)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            raise RuntimeError(f"Cov image not found: {cov_py_image}")

    results, succeeded, failed = [], [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_cov_single, r, check_cov_log_dir, dev_tools, max_depth, force):
                   r["instance_id"] for r in records}
        with tqdm(total=len(futures), dynamic_ncols=True,
                  desc=f"Checking coverage [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result, reason = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if result is not None:
                    results.append(result)
                    succeeded.append(instance_id)
                else:
                    failed[instance_id] = reason
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} succeeded, {len(failed)} failed"
                )
                save_file(results, coverage_report_path)

    summary = get_summary(succeeded, failed)
    summary_path = Path(check_cov_log_dir) / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    print(f"Coverage report saved to {coverage_report_path}.")
    print(f"Summary saved to {summary_path}.")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID locating datasets/<run_id>/fix_dataset.jsonl",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of concurrent instance containers",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run, ignoring any reusable coverage check output.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--max_depth",
        type=int,
        default=SymbolTrace.MAX_DEPTH,
        help="Max symbol-trace hops for indirect coverage evidence",
    )
    args = parser.parse_args()

    check_cov_log_dir = get_log_dir(args.run_id, "mine", "check_cov")
    fix_dataset_path = get_dataset_path('fix_dataset', args.run_id)
    coverage_report_path = get_dataset_path('coverage_report', args.run_id)
    coverage_report_path.parent.mkdir(parents=True, exist_ok=True)

    fix_dataset = load_file(fix_dataset_path)
    dev_tools = {iid: spec["dev_tools"]
        for iid, spec in get_env_specs(args.run_id, ("dev_tools",)).items()}

    check_cov_threadpool(
        fix_dataset,
        max_workers=args.max_workers,
        coverage_report_path=coverage_report_path,
        check_cov_log_dir=check_cov_log_dir,
        dev_tools=dev_tools,
        instance_ids=args.instance_ids,
        max_depth=args.max_depth,
        force=args.force,
    )


if __name__ == "__main__":
    main()
