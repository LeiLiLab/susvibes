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
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

from susvibes.constants import get_env_spec_path
from susvibes.curate.constants import (
    FEATURE_VULN_FILE,
    PATCH_TEMPLATE,
    PROBLEM_STATEMENT_FILE,
    README_FILE,
    SECURITY_FIX_FILE,
    TEST_PATCH_BACKUPS_DIR_NAME,
    TEST_PATCH_FILE,
    get_path,
)
from susvibes.curate.utils import extract_repo_test_cmd, reverse_patch
from susvibes.utils import load_file, save_file

BACKUP_HEADER_TEMPLATE = "> Snapshot of `test_patch` from dataset before edit #{n} ({timestamp})\n\n"

README_TEMPLATE = dedent("""\
# Meta Information

Project: {project}

Security issue identifier: {cve_id}

Vulnerability type: {cwes}

Environment image with repo code (security_fix patch + test_patch applied): `{env_image_name}`

Command to run the repo test suite: `{repo_test_cmd}`

# Folder Contents

- `{test_patch_file}` — security tests testing the security of the feature
- `{feature_vuln_file}` — diff revealing the vulnerable feature implementation
- `{security_fix_file}` — security fix patch fixing the security of the feature
- `{problem_statement_file}` — task description shown to the agent
- `{test_patch_backups_dir}/` — backups of previous versions of the test patch
""")


def dump_test(data_record, env_spec, edits_dir: Path):
    """Write a record's test_patch (and read-only context) to edits_dir/<instance_id>/."""
    task_dir = edits_dir / data_record["instance_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    save_file(PATCH_TEMPLATE.format(patch=data_record["test_patch"]),
        task_dir / TEST_PATCH_FILE)
    if "problem_statement" in data_record:
        save_file(data_record["problem_statement"], task_dir / PROBLEM_STATEMENT_FILE)
    if "security_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=data_record["security_patch"]),
            task_dir / SECURITY_FIX_FILE)
    if "mask_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=reverse_patch(data_record["mask_patch"])),
            task_dir / FEATURE_VULN_FILE)

    readme = README_TEMPLATE.format(
        project=data_record["project"],
        cve_id=data_record["cve_id"],
        cwes=", ".join(data_record["cwe_ids"]),
        env_image_name=data_record["env_image_name"],
        repo_test_cmd=extract_repo_test_cmd(env_spec["dockerfile"]),
        test_patch_file=TEST_PATCH_FILE,
        feature_vuln_file=FEATURE_VULN_FILE,
        security_fix_file=SECURITY_FIX_FILE,
        problem_statement_file=PROBLEM_STATEMENT_FILE,
        test_patch_backups_dir=TEST_PATCH_BACKUPS_DIR_NAME,
    )
    save_file(readme, task_dir / README_FILE)


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
    patch_path = instance_dir / TEST_PATCH_FILE
    if not patch_path.exists():
        return False
    new_patch = parse_patch_md(patch_path)
    if new_patch is None:
        raise RuntimeError(f"Invalid diff content in {patch_path}")
    new_patch += "\n"
    old_patch = data_record.get("test_patch", "")
    if old_patch == new_patch:
        return False

    backups_dir = instance_dir / TEST_PATCH_BACKUPS_DIR_NAME
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
        help="Only dump the given instance IDs (sync mode reads everything in edits/).",
    )
    args = parser.parse_args()

    dataset_path = get_path("dataset", args.run_id)
    edits_dir = get_path("edits", args.run_id)
    dataset = load_file(dataset_path)

    if args.mode == "dump":
        env_specs = load_file(get_env_spec_path("components", args.run_id))
        candidates = [r for r in dataset
            if r.get("test_patch") and r["instance_id"] in env_specs]
        if args.instance_ids is not None:
            candidates = [r for r in candidates if r["instance_id"] in set(args.instance_ids)]
        edits_dir.mkdir(parents=True, exist_ok=True)
        for record in candidates:
            dump_test(record, env_specs[record["instance_id"]], edits_dir)
        print(f"Dumped {len(candidates)} test_patches to {edits_dir}.")
    else:
        updated, invalid = 0, []
        for record in dataset:
            try:
                if sync_test(record, edits_dir):
                    updated += 1
            except RuntimeError:
                invalid.append(record["instance_id"])
        print(f"Updated {updated} test_patches.")
        if invalid:
            print(f"Skipped {len(invalid)} with invalid diff content: {invalid}")
        if updated > 0:
            save_file(dataset, dataset_path)
            print(f"Dataset saved to {dataset_path}.")
