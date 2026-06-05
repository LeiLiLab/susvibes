"""
Purpose: Produce the final SusVibes dataset by filtering validated instances
and generating golden patches.

python -m susvibes.curate.validate.wrapup \
    --run_id playground
"""

import argparse
from tqdm import tqdm
from typing import TypedDict

from susvibes.constants import HF_DATASET_REPO, HF_DATASET_FILE_NAME
from susvibes.curate.constants import LOCAL_REPOS_DIR, get_path
from susvibes.utils import load_file
from susvibes.curate.utils import (
    get_repo_dir,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
    push_dataset_to_hub,
)

class SusVibesRecord(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    image_name: str
    problem_statement: str
    task_patch: str
    golden_patch: str
    security_patch: str
    test_patch: str
    expected_failures: dict
    cwe_ids: str
    cve_id: str
    created_at: str
    language: str
    info_page: str

def make_susvibes_record(data_record: dict) -> SusVibesRecord:
    repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, data_record["base_commit"])
    apply_patch(repo_dir, data_record["security_patch"], reverse=True)
    apply_patch(repo_dir, data_record["mask_patch"])
    code_mask_commit = commit_changes(repo_dir, f'Code mask at {data_record["base_commit"]}')
    golden_patch = get_diff_patch(repo_dir, code_mask_commit, data_record["base_commit"])
    data_record["golden_patch"] = golden_patch
    return {key: data_record[key] for key in SusVibesRecord.__annotations__}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build final SusVibes dataset from validated instances.")
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    args = parser.parse_args()

    dataset_path = get_path('dataset', args.run_id)
    dataset = load_file(dataset_path)

    dataset = [r for r in dataset if "expected_failures" in r]
    dataset = [make_susvibes_record(data_record)
        for data_record in tqdm(dataset, desc="Wrapping up")]

    push_dataset_to_hub(dataset, HF_DATASET_REPO, HF_DATASET_FILE_NAME,
        commit_message=f"wrapup: {len(dataset)} instances (run_id={args.run_id})")
    print(f"Pushed {len(dataset)} records to https://huggingface.co/datasets/{HF_DATASET_REPO}")
