"""
Purpose: Produce the final SusVibes dataset by filtering validated instances and
generating golden patches. Writes the result to datasets/<run_id>/susvibes_dataset.jsonl;
with --push_to_hub, also uploads it to the HuggingFace dataset repo.

python -m susvibes.curate.validate.wrap_up \
    --run_id playground
"""

import argparse
import json
from tqdm import tqdm
from typing import TypedDict

from susvibes.core.constants import HF_DATASET_REPO, HF_DATASET_FILE_NAME, get_dataset_path
from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.core.utils import load_file, save_file, push_dataset_to_hub
from susvibes.curate.utils import (
    get_repo_dir,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
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
    expected_pf: dict
    flags: dict
    cwe_ids: str
    cve_id: str
    cve_fix_date: str
    language: str
    info_page: str

def make_susvibes_record(data_record: dict) -> SusVibesRecord:
    repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
    reset_to_commit(repo_dir, data_record["base_commit"])
    apply_patch(repo_dir, data_record["security_patch"], reverse=True)
    apply_patch(repo_dir, data_record["mask_patch"])
    code_mask_commit = commit_changes(repo_dir, f'Code mask at {data_record["base_commit"]}')
    golden_patch = get_diff_patch(repo_dir, code_mask_commit, data_record["base_commit"])
    record = {key: data_record[key] for key in SusVibesRecord.__annotations__ if key != "golden_patch"}
    record["golden_patch"] = golden_patch
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build final SusVibes dataset from validated instances.")
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--push_to_hub",
        action="store_true",
        help="Also upload the final dataset to the HuggingFace dataset repo.",
    )
    args = parser.parse_args()

    env_dataset_path = get_dataset_path('env_dataset', args.run_id)
    env_dataset = load_file(env_dataset_path)

    env_dataset = [r for r in env_dataset if "expected_pf" in r]
    if args.instance_ids is not None:
        env_dataset = [r for r in env_dataset if r["instance_id"] in set(args.instance_ids)]
    dataset = [make_susvibes_record(data_record)
        for data_record in tqdm(env_dataset, desc="Wrapping up")]

    dataset_path = get_dataset_path('dataset', args.run_id)
    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")

    if args.push_to_hub:
        push_dataset_to_hub(dataset, HF_DATASET_REPO, HF_DATASET_FILE_NAME,
            commit_message=f"wrap_up: {len(dataset)} instances (run_id={args.run_id})")
        print(f"Pushed {len(dataset)} records to https://huggingface.co/datasets/{HF_DATASET_REPO}")
