"""Rewrite each with-test instance's test diff into a test MASK.

Runs after `dev_tools`. A mined record's `raw_test_patch` is the diff the fix commit made to the
test files; the task must ship a tree with those tests REMOVED, so this stage derives the diff that
deletes every test function the fix touched and puts it in `test_patch` — the one name every later
stage reads. Mining leaves its own diff alone, so a re-derivation never masks a mask.

Why a container. Deriving the mask means parsing the test file, and a Python 2-era test file is
unparseable by the host: its `ast` rejects `print "x"` / `except E, e` / `123L`, and parso >= 0.8
dropped the 2.7 grammar. The parse therefore runs inside the instance's own version-matched static_py
image, exactly as check_cov does. Only text crosses the boundary — every git operation stays on the
host, so the container needs no repo.

Usage:
    python -m susvibes.curate.mine.post.test_mask --run_id <id> [--max_workers N] [--force] [--resume]
"""
import argparse
import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import StrEnum
from pathlib import Path

import docker.errors
from tqdm import tqdm

from susvibes.core.constants import ContainerLimits, get_dataset_path
from susvibes.env_specs.constants import SUSVIBES_RUNTIME_DATA_DIR
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import (
    load_file, save_file, setup_instance_logger, get_image_name, get_env_specs,
)
from susvibes.core.env import Deployment
from susvibes.core.report import (
    reuse_report, save_report, get_report_summary, print_summary)
from susvibes.curate.utils import (
    get_repo_dir, reset_to_commit, apply_patch, commit_changes, get_diff_patch, RepoLocks,
    should_keep,
)
from susvibes.curate.mine.constants import TARGET_LANG, LANG_EXTENSIONS
from susvibes.curate.mine.utils import split_to_file_patches, merge_file_patches, is_test_file

LOG_INSTANCE = "test_mask.log"
LOG_REPORT = "report.json"
LOG_SUMMARY = "summary.json"

KEEP_STAGE = KeepStage.TEST_MASK   # this stage's verdict under record["keep"]
# `test_patch` is the tests a task is graded against, and mining's own diff keeps a name of its own
# so nothing confuses the two. Every consumer reads `test_patch`, so the field existing at all means
# the tests are already hidden — one reading it too early crashes instead of shipping them in the open.
RAW_TEST_FIELD = "raw_test_patch"   # the fix commit's own test diff, from mine.core
TEST_FIELD = "test_patch"           # the mask derived from it

ENGINE_DIR = Path(__file__).parent / "engine"
ENGINE_PKG_NAME = "_susvibes_test_mask_engine"
INPUT_FILE_NAME = "input.json"
RESULT_MARKER = "<<<TEST_MASK_RESULT>>>"


class TestMaskStatus(StrEnum):
    """How the mask derivation concluded. Every member is a NORMAL outcome — the container failing
    is the report's `error`, which is what `--resume` re-runs."""
    MASKED = "masked"                   # mask derived — it replaced the record's test_patch
    NO_TEST_PATCH = "no_test_patch"     # the fix ships no test, so there is nothing to hide
    UNPARSEABLE = "unparseable"         # even the matched interpreter cannot parse the test file


PASS_STATUSES = {TestMaskStatus.MASKED, TestMaskStatus.NO_TEST_PATCH}


class TestMaskContainerLimits:
    """The engine parses a handful of text files, so it needs neither the repo nor much of anything
    else; the cap only stops a pathological parse from stalling the pool."""
    RUN_TIMEOUT = 300
    MEM_LIMIT = ContainerLimits.MEM_LIMIT
    CPU_LIMIT = 1


def test_mask_miss(error: str) -> dict:
    """A report for a run that concluded nothing — the container failed to build or run."""
    return {"test_mask_status": None, "error": error}


def test_mask_report(status: TestMaskStatus, **payload) -> dict:
    """A report for an instance that concluded: the status plus the mask when one was derived."""
    return {"test_mask_status": status, **payload, "error": None}


def apply_test_patch(repo_dir: Path, test_patch: str, reverse: bool = False) -> None:
    """Apply the fix commit's test diff to a repo at base_commit, file by file. Reversed it is the
    rollback, leaving the pre-fix tree the derived masks apply onto — mask against any other tree
    and the diff comes out wrong."""
    for file_path, file_patch in split_to_file_patches(test_patch).items():
        apply_patch(repo_dir, merge_file_patches({Path(file_path): file_patch}), reverse=reverse)


def get_test_files(repo_dir: Path, test_patch: str) -> list:
    """The engine's input — each test file's hunk plus its post and PRE text.

    The two texts sit on either side of the rollback, which is why this performs one; it re-applies
    the diff before returning, handing the tree back exactly as it was lent."""
    test_patches = {Path(file_path): file_patch
                    for file_path, file_patch in split_to_file_patches(test_patch).items()
                    if is_test_file(Path(file_path), LANG_EXTENSIONS.get(TARGET_LANG, []))}
    after = {file_path: load_file(repo_dir / file_path) for file_path in test_patches}
    apply_test_patch(repo_dir, test_patch, reverse=True)
    try:
        return [{"path": str(file_path), "patch": file_patch,
                 "before": load_file(repo_dir / file_path), "after": after[file_path]}
                for file_path, file_patch in test_patches.items()]
    finally:
        apply_test_patch(repo_dir, test_patch)


def prepare_engine_context(files: list, version: str, context_path: Path) -> None:
    """Lay the engine package and the worker's input into the build context. Unlike check_cov's,
    this context carries no repo — the engine only ever sees text."""
    engine_dir = context_path / ENGINE_PKG_NAME
    engine_dir.mkdir(parents=True, exist_ok=True)
    for source in ENGINE_DIR.glob("*.py"):
        (engine_dir / source.name).write_text(source.read_text(), encoding="utf-8")
    runtime_dir = context_path / SUSVIBES_RUNTIME_DATA_DIR.lstrip("/")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / INPUT_FILE_NAME).write_text(
        json.dumps({"version": version, "files": files}), encoding="utf-8")


def compose_test_mask_dockerfile(version: str) -> str:
    """Per-instance Dockerfile over the version-matched static_py base — the same images check_cov
    uses, for the same reason: they carry that interpreter and a parso that knows its grammar."""
    return (
        "FROM {base_image}\n"
        "COPY {engine} /{engine}\n"
        "COPY {runtime_rel}/{input} {runtime}/{input}\n"
        'CMD ["python", "-m", "{engine}.worker", "{runtime}/{input}"]\n'
    ).format(base_image='{0}:{1}'.format(get_image_name("static_py"), version),
             engine=ENGINE_PKG_NAME, input=INPUT_FILE_NAME,
             runtime=SUSVIBES_RUNTIME_DATA_DIR, runtime_rel=SUSVIBES_RUNTIME_DATA_DIR.lstrip("/"))


def parse_test_mask_result(logs: str) -> dict | None:
    """Extract the masks the worker printed after RESULT_MARKER."""
    if not logs or RESULT_MARKER not in logs:
        return None
    for line in logs.split(RESULT_MARKER)[-1].strip().splitlines():
        if line.strip():
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def run_test_mask_engine(data_record: dict, files: list, version: str,
                    logger: logging.Logger) -> tuple[dict | None, str | None]:
    """Derive the masks for one instance's test files inside its version-matched container.
    Returns (result, None), or (None, reason) when the container never produced one."""
    image_name = get_image_name("test_mask_{0}".format(data_record["instance_id"]))
    with tempfile.TemporaryDirectory(prefix="test_mask_") as tmpdir:
        context_path = Path(tmpdir)
        prepare_engine_context(files, version, context_path)
        try:
            deployment = Deployment.from_build(
                logger=logger, context_path=context_path,
                dockerfile=compose_test_mask_dockerfile(version), image_name=image_name,
                remove_image=True, remove_container=True)
        except docker.errors.BuildError as e:
            return None, "Failed to build mask deployment: {0}".format(e)
        try:
            deployment.create_container(mem_limit=TestMaskContainerLimits.MEM_LIMIT,
                                        cpu_limit=TestMaskContainerLimits.CPU_LIMIT)
        except docker.errors.APIError as e:
            return None, "Failed to create container: {0}".format(e)
        try:
            logs, timed_out = deployment.run_with_timeout(timeout=TestMaskContainerLimits.RUN_TIMEOUT)
        except docker.errors.APIError as e:
            return None, "Failed to start container: {0}".format(e)
    if timed_out:
        return None, "Failed to run mask container because of timeout."   # already logged
    result = parse_test_mask_result(logs)
    return (result, None) if result is not None else (None, "Failed to parse mask logs.")


def test_mask_single(data_record: dict, test_mask_log_dir: Path, dev_tools: dict,
                  force: bool = False, resume: bool = False) -> dict:
    """Derive one instance's test mask, reusing its cached report unless `force`/`resume` asks to
    re-run it. Always returns a report: the mask on success, a bare status when there was nothing
    to mask or the file is genuinely broken, or an `error`-marked miss when the container failed."""
    instance_id = data_record["instance_id"]
    project = data_record["project"]
    base_commit = data_record["base_commit"]

    log_dir = Path(test_mask_log_dir) / instance_id
    report = reuse_report(log_dir / LOG_REPORT, force=force, resume=resume)
    if report is not None:
        return report

    if RAW_TEST_FIELD not in data_record:
        report = test_mask_report(TestMaskStatus.NO_TEST_PATCH)
        save_report(report, log_dir / LOG_REPORT)
        return report

    logger = setup_instance_logger(log_dir / LOG_INSTANCE, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Deriving test mask for {instance_id}...")
    version = dev_tools[instance_id]["version"]
    repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)

    # Each git half takes the per-repo lock; the engine between them only ever sees strings, and
    # holding the lock across its image build would stall every other holder of this repo.
    with RepoLocks.locked(project):
        reset_to_commit(repo_dir, base_commit, new_branch=False)
        files = get_test_files(repo_dir, data_record[RAW_TEST_FIELD])
    result, reason = run_test_mask_engine(data_record, files, version, logger)

    if result is None:
        logger.error(reason)
        report = test_mask_miss(reason)
    elif result["unparseable"]:
        for path, detail in result["unparseable"]:
            logger.error(f"Unparseable test file {path}: {detail}")
        report = test_mask_report(TestMaskStatus.UNPARSEABLE,
                               reason=result["unparseable"][0][1])
    else:
        with RepoLocks.locked(project):
            # Rebuild the tree the masks were derived against: the lock was released in between,
            # so another holder may have left this clone anywhere.
            reset_to_commit(repo_dir, base_commit, new_branch=False)
            apply_test_patch(repo_dir, data_record[RAW_TEST_FIELD], reverse=True)
            for path, mask in result["masks"].items():
                if mask.strip():
                    apply_patch(repo_dir, merge_file_patches({Path(path): mask}))
            test_mask_commit = commit_changes(repo_dir, f"Test mask at {base_commit}")
            report = test_mask_report(
                TestMaskStatus.MASKED,
                **{TEST_FIELD: get_diff_patch(repo_dir, test_mask_commit, base_commit)})
        logger.info(f"Test mask derived for {instance_id} ({len(files)} test file(s)).")
    save_report(report, log_dir / LOG_REPORT)
    return report


def test_mask_threadpool(dataset: list, max_workers: int, test_mask_log_dir: Path, dev_tools: dict,
                      instance_ids: list = None,
                      force: bool = False, resume: bool = False) -> dict:
    """Derive every kept instance's mask, annotating each record in place with `test_patch` and this
    stage's `keep` verdict. The caller saves the dataset; nothing else is written."""
    gated = dataset
    if instance_ids is not None:
        gated = [data_record for data_record in gated if data_record["instance_id"] in set(instance_ids)]
    # `required`: this stage indexes dev_tools directly, so an instance dev_tools never
    # judged must not reach it — should_keep alone is fail-open and would let it through.
    gated = [data_record for data_record in gated
               if should_keep(data_record, exclude=KEEP_STAGE, required=(KeepStage.DEV_TOOLS,))]

    # The cov images are a hard dependency for instances that will actually run.
    versions = {v for data_record in gated
                if RAW_TEST_FIELD in r
                and reuse_report(Path(test_mask_log_dir) / data_record["instance_id"] / LOG_REPORT,
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
        futures = {executor.submit(test_mask_single, data_record, test_mask_log_dir, dev_tools, force, resume): r
                   for data_record in gated}
        with tqdm(total=len(futures), dynamic_ncols=True,
                  desc=f"Deriving test masks [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                record = futures[future]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {record['instance_id']}: {e}")
                reports[record["instance_id"]] = report
                if report["test_mask_status"] == TestMaskStatus.MASKED:
                    record[TEST_FIELD] = report[TEST_FIELD]
                if report["test_mask_status"] is not None:
                    record.setdefault("keep", {})[KEEP_STAGE] = \
                        report["test_mask_status"] in PASS_STATUSES
                pbar.update(1)
                masked = sum(1 for r in reports.values()
                               if data_record["test_mask_status"] == TestMaskStatus.MASKED)
                pbar.set_description(f"{masked} masked, {len(reports) - masked} not")

    summary = get_report_summary(reports, "test_mask_status")
    save_report(summary, Path(test_mask_log_dir) / LOG_SUMMARY)
    print_summary(summary)
    print(f"Summary saved to {Path(test_mask_log_dir) / LOG_SUMMARY}.")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive each with-test instance's test mask, annotating dataset in place.")
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
        help="Re-derive every instance's mask instead of reusing cached reports.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-derive only the instances whose cached report is an errored run, keeping every "
             "concluded one. If both --force and --resume are given, --force wins.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    args = parser.parse_args()

    test_mask_log_dir = get_log_dir(args.run_id, "mine", "test_mask")
    dataset_path = get_dataset_path('dataset', args.run_id)
    dataset = load_file(dataset_path)
    dev_tools = {iid: spec["dev_tools"]
        for iid, spec in get_env_specs(args.run_id, ("dev_tools",)).items()}

    test_mask_threadpool(
        dataset,
        max_workers=args.max_workers,
        test_mask_log_dir=test_mask_log_dir,
        dev_tools=dev_tools,
        instance_ids=args.instance_ids,
        force=args.force,
        resume=args.resume,
    )
    save_file(dataset, dataset_path)
    print(f"dataset annotated in place with {TEST_FIELD}: {dataset_path}.")


if __name__ == "__main__":
    main()
