import argparse
import json
from pathlib import Path

from curate import mask, problem_gen, verifier
from curate.utils import load_file, save_file, len_patch, display_task

LENGTH_RATIO_FUNC = [2, 5, 8, 10, 10, 15, 20, 50, 100]
TASK_MAX_LENGTH = 1500

PROCESSED_DATASET_PATH = Path('../datasets/processed_dataset.jsonl')
TASK_DATASET_PATH = Path('../datasets/task_dataset.jsonl')
STATS_PATH = Path("../datasets/stats.json")
DISPLAY_PATH = Path("../datasets/task_examples")

def adaptive_task_gen(
    processed_dataset_path: Path, 
    task_dataset_path: Path, 
    max_iters: int = None,
    model: dict = None
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
            model=model
        )
        print(STAGE_PROGRESS_MSG.format(len(successful_instance_ids), len(pending_instance_ids)))
        failed_instances = [id for id in pending_instance_ids if id not in successful_instance_ids]
        pending_instance_ids = successful_instance_ids
        if not pending_instance_ids:
            print(NO_PENDING_MSG)
            break
        
        # generate task description
        successful_instance_ids = problem_gen.pipeline(
            task_dataset_path=TASK_DATASET_PATH,
            instance_ids=pending_instance_ids,
            model=model
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
            model=model
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
        '--debug', 
        action='store_true', 
        help='Use debug dataset paths'
    )
    parser.add_argument(
        '--max_iters', 
        type=int, 
        default=None, 
        help='Maximum number of iterations'
    )
    parser.add_argument(
        '--display_tasks', 
        action='store_true', 
        help='Display all tasks created'
    )
    parser.add_argument(
        '--model', 
        type=json.loads, 
        default=None, 
        help='Model configuration as JSON dictionary'
    )
    args = parser.parse_args()

    if args.debug:
        PROCESSED_DATASET_PATH = PROCESSED_DATASET_PATH.with_stem(PROCESSED_DATASET_PATH.stem + '_debug')
        TASK_DATASET_PATH = TASK_DATASET_PATH.with_stem(TASK_DATASET_PATH.stem + '_debug')
        STATS_PATH = STATS_PATH.with_stem(STATS_PATH.stem + '_debug')
        DISPLAY_PATH = Path(str(DISPLAY_PATH) + '_debug')

    adaptive_task_gen(
        processed_dataset_path=PROCESSED_DATASET_PATH,
        task_dataset_path=TASK_DATASET_PATH,
        max_iters=args.max_iters,
        model=args.model
    )
    get_task_stats(
        task_dataset_path=TASK_DATASET_PATH,
        stats_path=STATS_PATH
    )

    if args.display_tasks:
        task_dataset = load_file(TASK_DATASET_PATH)
        for task in task_dataset:
            display_task(task, DISPLAY_PATH)
