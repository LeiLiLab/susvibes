"""
Purpose: Produce the final SusVibes dataset by filtering validated instances and
generating golden patches. Writes the result to datasets/<run_id>/susvibes_dataset.jsonl;
with --push_images, also pushes each instance's eval image to the Docker Hub (so the
dataset's image_name is pullable); with --push_to_hub, also uploads the dataset to the
HuggingFace dataset repo.

python -m susvibes.curate.wrap_up \
    --run_id playground
"""

import argparse
import json
import docker.errors
from tqdm import tqdm
from typing import TypedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.core.constants import HF_DATASET_REPO, HF_DATASET_FILE_NAME, get_dataset_path
from susvibes.core.env import Deployment
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR
from susvibes.core.utils import load_file, save_file, push_dataset_to_hub

from susvibes.core.report import get_two_state_summary, print_summary
from susvibes.curate.utils import (
    get_repo_dir,
    should_keep,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
    RepoLocks,
)

# Every stage a released record cannot be missing. `required` is what makes this fail-closed: one
# dataset now carries the whole run, so a record that simply never reached a stage would otherwise
# sail through on that stage's silence.
REQUIRED_STAGES = (KeepStage.TEST_MASK, KeepStage.BUILD_REPO, KeepStage.ADAPTIVE_GEN, KeepStage.VALIDATE)

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
    # The clone is shared: hold it across every touch of the tree.
    with RepoLocks.locked(data_record["project"]):
        reset_to_commit(repo_dir, data_record["base_commit"])
        apply_patch(repo_dir, data_record["security_patch"], reverse=True)
        apply_patch(repo_dir, data_record["mask_patch"])
        code_mask_commit = commit_changes(repo_dir, f'Code mask at {data_record["base_commit"]}')
        golden_patch = get_diff_patch(repo_dir, code_mask_commit, data_record["base_commit"])
    record = {key: data_record[key] for key in SusVibesRecord.__annotations__
              if key != "golden_patch"}
    record["golden_patch"] = golden_patch
    return record


def push_images_threadpool(image_names: list, max_workers: int) -> None:
    """Push each eval image to the Docker Hub in parallel; missing locals are skipped."""
    def _push(image_name):
        try:
            Deployment.collect_image(image_name=image_name)
        except docker.errors.ImageNotFound:
            return image_name, f"Eval image not found locally: {image_name}"
        try:
            Deployment.push_image(image_name)
            return image_name, None
        except Exception as e:
            return image_name, f"Failed to push image: {e}"

    print(f"\nPushing {len(image_names)} eval images...")
    succeeded, errored = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_push, image_name) for image_name in image_names]
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Pushing [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                image_name, err = f.result()
                if err is None:
                    succeeded.append(image_name)
                else:
                    errored[image_name] = err
                pbar.update(1)
                pbar.set_description(f"{len(succeeded)} pushed, {len(errored)} failed")
    print_summary(get_two_state_summary(succeeded, errored))


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
        "--push_images",
        action="store_true",
        help="Also push each instance's eval image to the Docker Hub.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads for pushing eval images.",
    )
    parser.add_argument(
        "--push_to_hub",
        action="store_true",
        help="Also upload the final dataset to the HuggingFace dataset repo.",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path('dataset', args.run_id)
    dataset = load_file(dataset_path)

    dataset = [data_record for data_record in dataset
                   if should_keep(data_record, required=REQUIRED_STAGES)]
    if args.instance_ids is not None:
        dataset = [data_record for data_record in dataset if data_record["instance_id"] in set(args.instance_ids)]
    susvibes_dataset = [make_susvibes_record(data_record)
        for data_record in tqdm(dataset, desc="Wrapping up")]

    susvibes_dataset_path = get_dataset_path('susvibes_dataset', args.run_id)
    save_file(susvibes_dataset, susvibes_dataset_path)
    print(f"Dataset saved to {susvibes_dataset_path}.")

    if args.push_images:
        push_images_threadpool([record["image_name"] for record in susvibes_dataset], args.max_workers)

    if args.push_to_hub:
        push_dataset_to_hub(susvibes_dataset, HF_DATASET_REPO, HF_DATASET_FILE_NAME,
            commit_message=f"wrap_up: {len(susvibes_dataset)} instances (run_id={args.run_id})")
        print(f"Pushed {len(susvibes_dataset)} records to https://huggingface.co/datasets/{HF_DATASET_REPO}")
