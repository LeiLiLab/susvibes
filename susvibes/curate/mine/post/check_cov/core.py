"""Static file-level test-coverage analysis for collected CVE instances.

Optional, and runs after `test_mask` so its rejections never withhold records from that mandatory
stage. For each instance in ``dataset.jsonl`` it decides, by
static analysis only (no execution), whether the repo's own test suite covers the
``security_patch`` files — at the FILE level. Analysis runs on the rollback tree
(``base_commit`` + reverse(security_patch), the vulnerable state), so targets are the
patch's PRE-side files.

Per-version Docker isolation. Each instance is analyzed INSIDE a container whose
Python version matches the instance (from ``dev_tools.json``), so the engine runs on
that interpreter's NATIVE ast/jedi/parso — no py2/py3 parser conflicts. The self
contained ``engine/`` package (S*/F*/H* scoring; see engine/README.md) is COPYied into
a thin per-instance image built ``FROM`` the version-matched static_py image (prebuilt by build_base),
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
    python -m susvibes.curate.mine.post.check_cov --run_id <id> [--max_workers N]
"""
import argparse
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import StrEnum
from pathlib import Path

import docker.errors
from tqdm import tqdm

from susvibes.core.constants import ContainerLimits, get_dataset_path
from susvibes.env_specs.constants import (
    WORKSPACE_DIR_NAME,
    SUSVIBES_RUNTIME_DATA_DIR,
)
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import (
    load_file, save_file, touched_files, setup_instance_logger, get_image_name, get_env_specs,
)
from susvibes.curate.utils import (
    get_repo_dir,
    rollback,
    run,
    RepoLocks,
    should_keep,
)
from susvibes.core.env import Deployment
from susvibes.core.report import (
    reuse_report, save_report, strip_bookkeeping, get_report_summary, print_summary)
from susvibes.curate.mine.post.check_cov.engine.constants import SymbolTrace, CoverageLabel
from susvibes.curate.mine.post.check_cov.engine.extract_facts import TARGET_EXTENSIONS

LOG_INSTANCE = "check_cov.log"
LOG_COV_OUTPUT = "cov_output.txt"     # the container's own logs, kept as evidence beside the report
LOG_REPORT = "report.json"
LOG_SUMMARY = "summary.json"

KEEP_STAGE = KeepStage.CHECK_COV   # this stage's verdict under record["keep"]


class CheckCovStatus(StrEnum):
    """How coverage analysis of one instance concluded. Every member is a NORMAL outcome — the
    container failing is the report's `error`, which is what `--resume` re-runs."""
    ANALYZED = "analyzed"                   # the engine produced a label — see the report's `label`
    NO_TARGET_FILE = "no_target_file"       # security_patch only adds files — nothing to cover

PASS_LABELS = {CoverageLabel.LIKELY, CoverageLabel.MAYBE}   # labels that clear the coverage gate
PASS_STATUSES = {CheckCovStatus.NO_TARGET_FILE}   # nothing to cover clears it without a label

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
    # Measured over 2292 instances: p50 31s, p99 387s, but the three slowest real analyses
    # (airflow, home-assistant, pretix) needed 613-741s, which a 600s cap cut by a hair.
    RUN_TIMEOUT = ContainerLimits.RUN_TIMEOUT   # seconds — hard cap per instance container
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
    """Per-instance Dockerfile over the version-matched static_py base (see the build-context
    constants for what lands where). The engine is installed as a site-packages library, not on
    PYTHONPATH=/, so jedi treats it as a library and won't scan the container root and time out
    on large repos."""
    static_py_image = f'{get_image_name("static_py")}:{version}'
    site_packages = SITE_PACKAGES_DIR.format(version=version)
    return (
        "FROM {base_image}\n"
        "COPY {ws} /{ws}\n"
        "COPY {engine} {site_packages}/{engine}\n"
        "COPY {runtime_rel}/{input} {runtime}/{input}\n"
        'CMD ["python", "-m", "{engine}.worker", "/{ws}", "{runtime}/{input}"]\n'
    ).format(base_image=static_py_image, ws=WORKSPACE_DIR_NAME, engine=ENGINE_PKG_NAME,
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

def run_cov_engine(data_record: dict, targets: list, version: str, max_depth: int,
                   log_dir: Path, logger: logging.Logger) -> tuple[dict | None, str | None]:
    """Analyze one instance in its version-matched container, returning (CoverageResult, None) or
    (None, reason) when the container never produced one. The container's own logs are saved beside
    the report as evidence — never as the cache, since an empty one cannot be told from a good run."""
    project, base_commit = data_record["project"], data_record["base_commit"]
    repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)
    with tempfile.TemporaryDirectory(prefix="cov_") as tmpdir:
        context_path = Path(tmpdir)
        # rollback + snapshot under the per-repo lock (the shared clone is mutated);
        # the build context is an independent copy, so build/run happen lock-free.
        with RepoLocks.locked(project):
            rollback(repo_dir, base_commit, data_record["security_patch"])
            prepare_engine_context(repo_dir, data_record, targets, max_depth, context_path)
        deployment = build_cov_deployment(data_record, version, context_path, logger)
        if deployment is None:
            return None, "Failed to build cov deployment."
        # create_container / run_with_timeout self-clean on failure.
        try:
            deployment.create_container(mem_limit=CovContainerLimits.MEM_LIMIT,
                                        cpu_limit=CovContainerLimits.CPU_LIMIT)
        except docker.errors.APIError as e:
            return None, f"Failed to create container: {e}"
        try:
            logs, timed_out = deployment.run_with_timeout(timeout=CovContainerLimits.RUN_TIMEOUT)
        except docker.errors.APIError as e:
            return None, f"Failed to start container: {e}"
    save_file(logs, log_dir / LOG_COV_OUTPUT)
    if timed_out:
        return None, "Failed to run cov container because of timeout."   # already logged
    result = parse_cov_result(logs)
    return (result, None) if result is not None else (None, "Failed to parse coverage logs.")


def check_cov_miss(error: str) -> dict:
    """A report for a run that concluded nothing — the container failed to build, run, or print a
    parseable result. `error` set is what `--resume` re-runs. Mirrors `sec_prop_miss`."""
    return {"check_cov_status": None, "label": None, "error": error}


def check_cov_report(status: CheckCovStatus, result: dict = None) -> dict:
    """A report for a run that concluded: the coverage result when the engine produced one, or the
    bare status when the instance was never analysable (no version, nothing to cover)."""
    payload = {k: v for k, v in (result or {}).items() if k != "instance_id"}
    # `label` is declared even when the engine never ran, so every report has one shape to read.
    return {"check_cov_status": status, "label": None, **payload, "error": None}


def check_cov_single(data_record: dict, check_cov_log_dir: Path, dev_tools: dict,
                     max_depth: int = SymbolTrace.MAX_DEPTH,
                     force: bool = False, resume: bool = False) -> dict:
    """Analyze one instance's file-level test coverage on the rollback tree
    (base_commit + reverse(security_patch)), inside a version-matched cov container, reusing this
    instance's cached report unless `force`/`resume` asks to re-run it. Always returns a report:
    the coverage result on success, a bare status when the instance was never analysable, or an
    `error`-marked miss when the container failed. The container's own logs are saved beside the
    report as evidence — never as the cache, since an empty one cannot be told from a good run."""
    instance_id = data_record["instance_id"]

    log_dir = Path(check_cov_log_dir) / instance_id
    report = reuse_report(log_dir / LOG_REPORT, force=force, resume=resume)
    if report is not None:
        return report

    logger = setup_instance_logger(log_dir / LOG_INSTANCE, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking test coverage for {instance_id}...")

    version = dev_tools[instance_id]["version"]
    # Targets are the security_patch's PRE-side files (the names present in the
    # rollback tree), filtered to target-language (.py). A fix that only ADDS a
    # .py file yields none — the vulnerable code has no such file to cover.
    targets = sorted(t for t in touched_files(data_record["security_patch"], side="pre")
                     if t.endswith(TARGET_EXTENSIONS))
    if not targets:
        logger.info("No target-language file in the rollback tree (security_patch only adds files).")
        report = check_cov_report(CheckCovStatus.NO_TARGET_FILE)
    else:
        result, reason = run_cov_engine(data_record, targets, version, max_depth, log_dir, logger)
        if result is None:
            logger.error(reason)
            report = check_cov_miss(reason)
        else:
            report = check_cov_report(CheckCovStatus.ANALYZED, result)
            logger.info(f"Coverage for {instance_id}: {result['label']} "
                        f"(score {result.get('score')}, engine {result.get('engine')}): "
                        f"{result.get('reason')}")
    save_report(report, log_dir / LOG_REPORT)
    return report


def check_cov_threadpool(dataset: list, max_workers: int,
                         check_cov_log_dir: Path, dev_tools: dict,
                         instance_ids: list = None,
                         max_depth: int = SymbolTrace.MAX_DEPTH,
                         force: bool = False, resume: bool = False) -> dict:
    """Analyze each instance in its own version-matched container, annotating every record the
    engine labelled in place (`func_coverage` + its `keep` verdict), and save a run summary. The
    caller saves the dataset; nothing else is written.

    One container per worker thread; the host imports no jedi, so threads (not
    processes) suffice. Per-repo resets are serialized by RepoLocks; distinct repos
    run concurrently."""
    gated = dataset
    if instance_ids is not None:
        wanted = set(instance_ids)
        gated = [data_record for data_record in gated if data_record["instance_id"] in wanted]
    # Only records every earlier stage kept: one an earlier stage dropped was skipped by dev_tools
    # too, so it has no version and must not reach the lookup. This stage's own verdict is excluded,
    # or a re-run would only ever revisit the instances it already found covered.
    # `required`: this stage indexes dev_tools directly, so an instance dev_tools never
    # judged must not reach it — should_keep alone is fail-open and would let it through.
    gated = [data_record for data_record in gated
               if should_keep(data_record, exclude=KEEP_STAGE, required=(KeepStage.DEV_TOOLS,))]

    # Cov images are a hard dependency for instances that will actually run — one served from its
    # cached report needs none. Fail fast if a needed version is missing.
    versions = {v for data_record in gated
                if reuse_report(Path(check_cov_log_dir) / data_record["instance_id"] / LOG_REPORT,
                                force=force, resume=resume) is None
                and (v := (dev_tools.get(data_record["instance_id"]) or {}).get("version"))}
    for version in versions:
        static_py_image = f'{get_image_name("static_py")}:{version}'
        try:
            Deployment.collect_image(image_name=static_py_image)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            raise RuntimeError(f"Static image not found: {static_py_image}")

    reports = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_cov_single, data_record, check_cov_log_dir, dev_tools,
                                   max_depth, force, resume): data_record for data_record in gated}
        with tqdm(total=len(futures), dynamic_ncols=True,
                  desc=f"Checking coverage [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                record = futures[future]
                instance_id = record["instance_id"]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                reports[instance_id] = report
                # The records are the caller's own dataset entries, so annotating here is what puts
                # the result in the dataset — the report is only its cache. An errored run is
                # annotated too: its `error` on the record is what tells it from never-analyzed,
                # and its null label fails the gate rather than passing on this stage's silence.
                record[KEEP_STAGE] = strip_bookkeeping(report, drop=("check_cov_status",))
                record.setdefault("keep", {})[KEEP_STAGE] = (
                    report["check_cov_status"] in PASS_STATUSES
                    or report["label"] in PASS_LABELS)
                pbar.update(1)
                labelled = sum(1 for r in reports.values()
                               if r["check_cov_status"] == CheckCovStatus.ANALYZED)
                pbar.set_description(f"{labelled} labelled, {len(reports) - labelled} not")

    summary = get_report_summary(reports, "check_cov_status")
    summary_path = Path(check_cov_log_dir) / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    print(f"Summary saved to {summary_path}.")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID locating datasets/<run_id>/dataset.jsonl",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=16,
        help="Number of concurrent instance containers",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze every instance instead of reusing cached reports.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-analyze only the instances whose cached report is an errored run, keeping every "
             "concluded one. If both --force and --resume are given, --force wins.",
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
    dataset_path = get_dataset_path('dataset', args.run_id)

    dataset = load_file(dataset_path)
    dev_tools = {iid: spec["dev_tools"]
        for iid, spec in get_env_specs(args.run_id, ("dev_tools",)).items()}

    check_cov_threadpool(
        dataset,
        max_workers=args.max_workers,
        check_cov_log_dir=check_cov_log_dir,
        dev_tools=dev_tools,
        instance_ids=args.instance_ids,
        max_depth=args.max_depth,
        force=args.force,
        resume=args.resume,
    )
    save_file(dataset, dataset_path)
    print(f"dataset annotated in place with {KEEP_STAGE}: {dataset_path}.")


if __name__ == "__main__":
    main()
