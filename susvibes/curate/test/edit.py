"""
Purpose: Display test_patch entries from susvibes_dataset.jsonl as readable
markdown files for manual inspection and small corrections, then sync edits
back into the dataset.

python -m susvibes.curate.test.edit --mode dump --run_id default

python -m susvibes.curate.test.edit --mode sync --run_id default
"""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import docker
import docker.errors
from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import (
    get_log_dir,
    TaskArtifact,
    PATCH_TEMPLATE,
)
from susvibes.curate.utils import extract_repo_test_cmd, reverse_patch
from susvibes.core.env import Env, Deployment
from susvibes.core.utils import (
    get_image_name, load_file, parse_instance_id, save_file, setup_instance_logger, get_env_specs,
)

docker_client = docker.from_env()

LOG_BUILD = "build_base_no_test_image.log"

BACKUP_HEADER_TEMPLATE = "> Snapshot of `test_patch` from dataset before edit #{n} ({timestamp})\n\n"

README_TEMPLATE = dedent("""\
# Meta Information

Project: {project}

Security issue identifier: {cve_id}

Vulnerability type: {cwes}

Verification image (secure feature implementation, no security tests applied): `{base_no_test_image_name}`

Command to run the repo test suite: `{repo_test_cmd}`

# Folder Contents

- `{test_patch_file}` — security tests testing the security of the feature
- `{feature_vuln_file}` — diff revealing the vulnerable feature implementation
- `{security_fix_file}` — security fix patch fixing the security of the feature
- `{problem_statement_file}` — task description shown to the agent
- `{test_patch_backups_dir}/` — backups of previous versions of the test patch
""")


def build_base_no_test_deployment(
    data_record, env_spec, target_image_name: str, log_dir: Path) -> Deployment | None:
    """Build the per-instance "base_no_test" image: env image with the original
    test_patch reversed, so /project sits at the secure baseline without tests,
    tagged target_image_name. Returns the Deployment (or None on failure)."""
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
            patches=[(data_record["test_patch"], {"reverse": True})],
            logger=logger,
            remove_image=False,
        )
    except docker.errors.BuildError as e:
        logger.error(f"Failed to build base_no_test deployment for {instance_id}: {e}")
        return None

    assert deployment.image.tag(target_image_name)
    logger.info(f"base_no_test deployment built: {target_image_name}")
    return deployment


def dump_test(data_record, env_spec, edits_dir: Path):
    """Write a record's test_patch (and read-only context) to edits_dir/<instance_id>/."""
    task_dir = edits_dir / data_record["instance_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    save_file(PATCH_TEMPLATE.format(patch=data_record["test_patch"]),
        task_dir / TaskArtifact.TEST_PATCH)
    if "problem_statement" in data_record:
        save_file(data_record["problem_statement"], task_dir / TaskArtifact.PROBLEM_STATEMENT)
    if "security_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=data_record["security_patch"]),
            task_dir / TaskArtifact.SECURITY_FIX)
    if "mask_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=reverse_patch(data_record["mask_patch"])),
            task_dir / TaskArtifact.FEATURE_VULN)

    readme = README_TEMPLATE.format(
        project=data_record["project"],
        cve_id=data_record["cve_id"],
        cwes=", ".join(data_record["cwe_ids"]),
        base_no_test_image_name=data_record["base_no_test_image_name"],
        repo_test_cmd=extract_repo_test_cmd(env_spec["dockerfile"]),
        test_patch_file=TaskArtifact.TEST_PATCH,
        feature_vuln_file=TaskArtifact.FEATURE_VULN,
        security_fix_file=TaskArtifact.SECURITY_FIX,
        problem_statement_file=TaskArtifact.PROBLEM_STATEMENT,
        test_patch_backups_dir=TaskArtifact.TEST_PATCH_BACKUPS,
    )
    save_file(readme, task_dir / TaskArtifact.README)


def dump_single(record, env_spec, edits_dir: Path, log_dir: Path, require_test: bool = True):
    if not require_test:
        dump_test(record, env_spec, edits_dir)
        return True
    base_no_test_image_name = get_image_name(f"base_no_test_{record['instance_id']}")
    deployment = build_base_no_test_deployment(
        record, env_spec, base_no_test_image_name, log_dir)
    if not deployment:
        return None
    record["base_no_test_image_name"] = base_no_test_image_name
    dump_test(record, env_spec, edits_dir)
    return base_no_test_image_name


def dump_threadpool(
    dataset: list,
    env_specs: dict,
    edits_dir: Path,
    log_dir: Path,
    max_workers: int,
    instance_ids: list = None,
    require_test: bool = True,
) -> tuple[int, list]:
    """Build base_no_test images and dump editable folders for all candidate
    instances in parallel. Returns (dumped_count, failed_instance_ids).
    When require_test is False, the base_no_test image build is skipped."""
    candidates = [r for r in dataset
        if r.get("test_patch") and r["instance_id"] in env_specs]
    if instance_ids is not None:
        candidates = [r for r in candidates if r["instance_id"] in set(instance_ids)]

    succeeded, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                dump_single, r, env_specs[r["instance_id"]], edits_dir, log_dir,
                require_test=require_test,
            ): r["instance_id"] for r in candidates
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Dumping [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                if result:
                    succeeded.append(instance_id)
                else:
                    failed.append(instance_id)
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} dumped, {len(failed)} failed"
                )
    if succeeded:
        print(f"Succeeded ({len(succeeded)}):")
        for instance_id in sorted(succeeded):
            print(f"  {instance_id}")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for instance_id in sorted(failed):
            print(f"  {instance_id}")
    return len(succeeded), failed


def parse_patch_md(path: Path) -> str | None:
    """Extract the unified diff from a fenced ```diff block. Returns None on failure."""
    m = re.search(r"```diff\s*\n(.*?)\n```", path.read_text(), re.DOTALL)
    if not m:
        return None
    patch = m.group(1).strip("\n")
    if not re.search(r"^(diff --git|--- |\+\+\+ |@@)", patch, re.MULTILINE):
        return None
    return patch


def sync_test(data_record, edits_dir: Path) -> bool:
    """Read edited test_patch.md back into data_record. On update, snapshot the old
    test_patch to edits/<instance_id>/test_patch_backups/<n>.md before overwriting.
    Returns True if updated.
    Raises RuntimeError if the edit file exists but contains invalid diff content."""
    instance_dir = edits_dir / data_record["instance_id"]
    patch_path = instance_dir / TaskArtifact.TEST_PATCH
    if not patch_path.exists():
        return False
    new_patch = parse_patch_md(patch_path)
    if new_patch is None:
        raise RuntimeError(f"Invalid diff content in {patch_path}")
    new_patch += "\n"
    old_patch = data_record.get("test_patch", "")
    if old_patch == new_patch:
        return False

    backups_dir = instance_dir / TaskArtifact.TEST_PATCH_BACKUPS
    backups_dir.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem) for p in backups_dir.glob("*.md") if p.stem.isdigit()]
    n = (max(existing) if existing else 0) + 1
    header = BACKUP_HEADER_TEMPLATE.format(
        n=n,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    save_file(header + PATCH_TEMPLATE.format(patch=old_patch), backups_dir / f"{n}.md")

    data_record["test_patch"] = new_patch
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dump/sync test_patch entries for manual inspection and small corrections.")
    parser.add_argument(
        "--mode",
        choices=["dump", "sync"],
        required=True,
        help="dump: write test_patch markdown files; sync: read edits back into dataset.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID (datasets/<run_id>/...)",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for dump.",
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Build the base_no_test image during dump (default True); false skips it.",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path("env_dataset", args.run_id)
    edits_dir = get_dataset_path("edits", args.run_id)
    dataset = load_file(dataset_path)

    if args.mode == "dump":
        env_specs = get_env_specs(args.run_id)
        log_dir = get_log_dir(args.run_id, "test")
        edits_dir.mkdir(parents=True, exist_ok=True)
        dumped, failed = dump_threadpool(
            dataset, env_specs, edits_dir, log_dir, args.max_workers,
            instance_ids=args.instance_ids,
            require_test=args.require_test,
        )
        save_file(dataset, dataset_path)
        print(f"Built and dumped {dumped} instances to {edits_dir}.")
        print(f"Dataset saved to {dataset_path}.")
    else:
        candidates = dataset
        if args.instance_ids is not None:
            candidates = [r for r in dataset if r["instance_id"] in set(args.instance_ids)]
        updated, unchanged, invalid = 0, 0, []
        for record in candidates:
            try:
                if sync_test(record, edits_dir):
                    updated += 1
                else:
                    unchanged += 1
            except RuntimeError:
                invalid.append(record["instance_id"])
        print(f"Updated {updated} test_patch entries, {unchanged} unchanged.")
        if invalid:
            print(f"Skipped {len(invalid)} with invalid diff content: {invalid}")
        if updated > 0:
            save_file(dataset, dataset_path)
            print(f"Dataset saved to {dataset_path}.")
