"""
Purpose: Check every validated instance's eval image for a readable copy of its golden code
(the answer key an agent could read off disk), remove what a rule covers, and re-validate the
rebuilt image. Runs after validate and before wrap_up: an instance whose copy cannot be removed
is residue and is not published.

python -m susvibes.curate.leak.core \\
    --run_id playground \\
    --pull_images   # Optional: pull each eval/env image from the Docker Hub instead of using the local one
"""

import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.core.report import get_report_summary, print_summary
from susvibes.core.utils import load_file, save_file, get_env_specs
from susvibes.curate.constants import KeepStage, get_log_dir
from susvibes.curate.leak.constants import LeakStatus, KEEP_STAGE, LOG_SUMMARY, KNOWN_RESIDUE_FILE_NAME
from susvibes.curate.leak.fix import fix_leak_single, apply_leak_report
from susvibes.curate.utils import should_keep


def fix_leak_threadpool(
    run_id: str,
    dataset: list,
    max_workers: int,
    leak_log_dir: Path,
    force: bool = False,
    resume: bool = False,
    instance_ids: list = None,
    pull_images: bool = False,
    push_images: bool = False,
    known_residue: set = frozenset(),
) -> None:
    env_specs = get_env_specs(run_id)
    dataset_by_id = {data_record["instance_id"]: data_record for data_record in dataset}
    gated_ids = {instance_id for instance_id, r in dataset_by_id.items()
                 if should_keep(r, exclude=KEEP_STAGE, required=(KeepStage.VALIDATE,))} \
        & set(env_specs.keys())
    if instance_ids is not None:
        gated_ids = gated_ids & set(instance_ids)

    reports = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fix_leak_single,
                dataset_by_id[instance_id],
                env_specs[instance_id],
                leak_log_dir,
                pull_images=pull_images,
                push_images=push_images,
                force=force,
                resume=resume,
            ): instance_id
            for instance_id in gated_ids
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Checking leaks [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                reports[instance_id] = report
                if not report["error"]:
                    apply_leak_report(report, dataset_by_id[instance_id], known_residue)
                pbar.update(1)
                residue = sum(1 for r in reports.values() if r["leak_status"] == LeakStatus.RESIDUE)
                pbar.set_description(f"{len(reports) - residue} clean or fixed, {residue} residue")
    summary = get_report_summary(reports, "leak_status")
    summary_path = leak_log_dir / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_summary(summary)
    print(f"Summary saved to {summary_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check validated instances' eval images for golden-code leaks and fix what a rule covers.")
    parser.add_argument(
        "--max_workers",
        type=int,
        default=2,
        help="Number of threads; each one sweeps a whole image and may re-validate an instance.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-check every instance instead of reusing cached reports.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-check only the instances whose cached report is an errored run, keeping every "
             "verdict. If both --force and --resume are given, --force wins.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--pull_images",
        action="store_true",
        help="Pull each instance's eval image (and env image when a fix needs it) from the Docker Hub "
             "instead of requiring it locally.",
    )
    parser.add_argument(
        "--push_images",
        action="store_true",
        help="Also push each fixed instance's rebuilt eval (and env) image to the Docker Hub.",
    )
    parser.add_argument(
        "--known_residue_path",
        type=Path,
        default=None,
        help="JSON list of instance IDs whose residue was reviewed and accepted; they keep their `keep` "
             f"verdict. Defaults to datasets/<run_id>/{KNOWN_RESIDUE_FILE_NAME} when that file exists.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path('dataset', args.run_id)
    leak_log_dir = get_log_dir(args.run_id, "leak")
    known_residue_path = args.known_residue_path or dataset_path.parent / KNOWN_RESIDUE_FILE_NAME

    dataset = load_file(dataset_path)
    known_residue = set(load_file(known_residue_path)) if known_residue_path.exists() else set()

    fix_leak_threadpool(
        args.run_id, dataset, args.max_workers, leak_log_dir,
        force=args.force,
        resume=args.resume,
        instance_ids=args.instance_ids,
        pull_images=args.pull_images,
        push_images=args.push_images,
        known_residue=known_residue,
    )

    save_file(dataset, dataset_path)
    print(f"Dataset saved to {dataset_path}.")
