import random
import argparse
from pathlib import Path

from susvibes.curate.constants import get_path
from susvibes.curate.adaptive_gen import mask, problem_gen, verifier
from susvibes.utils import load_file, save_file
from susvibes.curate.utils import len_patch, display_task

LENGTH_RATIO_FUNC = [2, 5, 8, 10, 10, 15, 20, 50, 100]
TASK_MAX_LENGTH = 1500

def adaptive_task_gen(
    processed_dataset_path: Path, 
    task_dataset_path: Path, 
    max_iters: int = None,
):
    processed_dataset = load_file(processed_dataset_path)
    instance_ids = [data_record["instance_id"] for data_record in processed_dataset]
    
    STAGE_PROGRESS_MSG = "{} / {} instances processed."
    NO_PENDING_MSG = "No instances left to process, exiting..."
    TASKS_CREATED_SUMMARY_MSG = "{} / {} tasks successfully created."
    
    pending_instance_ids = instance_ids.copy()
    num_iters = max_iters if max_iters is not None else len(LENGTH_RATIO_FUNC)
    for iter_id in range(num_iters):
        print(f"Iteration {iter_id + 1}/{num_iters}")
        failed_instances = []
        
        # mask surrounding code
        successful_instance_ids = mask.pipeline(
            processed_dataset_path=processed_dataset_path,
            task_dataset_path=task_dataset_path,
            length_ratio=LENGTH_RATIO_FUNC[iter_id],
            max_length=TASK_MAX_LENGTH,
            instance_ids=pending_instance_ids,
        )
        print(STAGE_PROGRESS_MSG.format(len(successful_instance_ids), len(pending_instance_ids)))
        failed_instances = [id for id in pending_instance_ids if id not in successful_instance_ids]
        pending_instance_ids = successful_instance_ids
        if not pending_instance_ids:
            print(NO_PENDING_MSG)
            break
        
        # generate task description
        successful_instance_ids = problem_gen.pipeline(
            task_dataset_path=task_dataset_path,
            instance_ids=pending_instance_ids,
        )
        print(STAGE_PROGRESS_MSG.format(len(successful_instance_ids), len(pending_instance_ids)))
        failed_instances += [id for id in pending_instance_ids if id not in successful_instance_ids]
        pending_instance_ids = successful_instance_ids
        if not pending_instance_ids:
            print(NO_PENDING_MSG)
            break
        
        # verify generated issue
        successful_instance_ids, verified_instance_ids = verifier.pipeline(
            task_dataset_path=task_dataset_path,
            instance_ids=pending_instance_ids,
        )
        print(STAGE_PROGRESS_MSG.format(len(successful_instance_ids), len(pending_instance_ids)))
        failed_instances += [id for id in pending_instance_ids if id not in successful_instance_ids]
        print("{} instances verified, {} instances remaining.".format(
            len(verified_instance_ids), len(successful_instance_ids) - len(verified_instance_ids)
        ))
        remaining_instance_ids = [id for id in successful_instance_ids if id not in verified_instance_ids]
        if not remaining_instance_ids:
            print(NO_PENDING_MSG)
            break
                
        print("Failed to process {} instance, retrying...".format(len(failed_instances)))
        pending_instance_ids = remaining_instance_ids + failed_instances
        mask.remove_results(pending_instance_ids)
        problem_gen.remove_results(pending_instance_ids)
        verifier.remove_results(pending_instance_ids)
    
    task_dataset = load_file(task_dataset_path)
    task_dataset = [data_record for data_record in task_dataset if "task_patch" in data_record]
    print(TASKS_CREATED_SUMMARY_MSG.format(len(task_dataset), len(instance_ids)))
    save_file(task_dataset, task_dataset_path)
    
def get_task_stats(task_dataset_path: Path, stats_path: Path):
    task_dataset = load_file(task_dataset_path)
    stats = {}
    for data_record in task_dataset:
        num_files, num_lines = len_patch(data_record["mask_patch"])
        stats[data_record["instance_id"]] = {
            "num_files_edited": num_files,
            "num_lines_edited": num_lines,
        }
    save_file(stats, stats_path)
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--max_iters', 
        type=int, 
        default=None, 
        help='Maximum number of iterations'
    )
    parser.add_argument(
        '--preview',
        type=int,
        default=0,
        help='Number of tasks to preview after generation'
    )
    parser.add_argument(
        '--subset', 
        type=str, 
        default=None, 
        help='Subset name for output subdirectory (datasets/<subset>/...)'
    )
    args = parser.parse_args()
    
    processed_dataset_path = get_path('processed_dataset', args.subset)
    task_dataset_path = get_path('task_dataset', args.subset)
    stats_path = get_path('stats', args.subset)
    examples_path = get_path('examples', args.subset)

    adaptive_task_gen(
        processed_dataset_path=processed_dataset_path,
        task_dataset_path=task_dataset_path,
        max_iters=args.max_iters,
    )
    get_task_stats(
        task_dataset_path=task_dataset_path,
        stats_path=stats_path
    )

    if args.preview > 0:
        task_dataset = load_file(task_dataset_path)
        samples = random.sample(task_dataset, min(args.preview, len(task_dataset)))
        for data_record in samples:
            display_task(data_record, examples_path)