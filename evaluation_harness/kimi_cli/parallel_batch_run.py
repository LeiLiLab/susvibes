#!/usr/bin/env python3
"""
Parallel Docker Batch Runner for Kimi CLI

This script divides the dataset into N parallel processes and runs
batch_run_docker.py for each process concurrently.
"""

import argparse
import json
import logging
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

script_dir = Path(__file__).resolve().parent
harness_root = script_dir.parent
repo_root = harness_root.parent

for p in (str(repo_root), str(harness_root), str(script_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from batch_run_docker import get_options as get_batch_options  # noqa: E402

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(message)s',
    handlers=[
        logging.FileHandler('parallel_batch_run.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_options():
    parser = get_batch_options()
    parser.description = 'Run batch_run_docker.py for Kimi CLI in parallel across N processes'
    parser.set_defaults(num_instances=None)

    for action in parser._actions:
        if action.dest == 'num_instances':
            action.help = 'Number of instances to process (if None, process all from start_idx to end)'
            break

    parser.add_argument(
        '--num_processes',
        type=int,
        default=4,
        help='Number of parallel processes to run'
    )
    parser.add_argument(
        '--python_executable',
        type=str,
        default='python3',
        help='Python executable to use for running batch_run_docker.py'
    )
    return parser


def load_instances(jsonl_file: str) -> List[Dict[str, Any]]:
    """Load instances from JSONL file."""
    instances = []
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        instance = json.loads(line)
                        instances.append(instance)
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing line {line_num}: {e}")
                        continue
        logger.info(f"Loaded {len(instances)} instances from {jsonl_file}")
        return instances
    except FileNotFoundError:
        logger.error(f"File not found: {jsonl_file}")
        return []
    except Exception as e:
        logger.error(f"Error loading instances: {e}")
        return []


def run_batch_process(
    process_id: int,
    start_idx: int,
    num_instances: int,
    jsonl_file: str,
    results_dir: str,
    workspace_root: str,
    model: str,
    setup_script: str,
    python_executable: str,
    keep_workspace: bool = True,
    temp_jsonl: Optional[str] = None,
    strategy: str = 'none'
) -> Dict[str, Any]:
    """Run batch_run_docker.py for a subset of instances."""
    logger.info(
        f"Process {process_id}: Starting to process {num_instances} instances "
        f"from index {start_idx}"
    )

    process_workspace = Path(f"{workspace_root}_process_{process_id}").resolve()
    if not process_workspace.exists():
        logger.info(f"Process {process_id}: Creating workspace directory: {process_workspace}")
        process_workspace.mkdir(parents=True, exist_ok=True)

    process_workspace_str = str(process_workspace)
    target_jsonl = temp_jsonl if temp_jsonl else jsonl_file

    script_dir = Path(__file__).resolve().parent
    batch_script = str(script_dir / 'batch_run_docker.py')

    cmd = [
        python_executable,
        batch_script,
        '--jsonl_file', target_jsonl,
        '--results_dir', results_dir,
        '--workspace_root', process_workspace_str,
        '--num_instances', str(num_instances),
        '--model', model,
        '--setup_script', setup_script,
        '--timestamp_suffix', f'process{process_id}',
        '--strategy', strategy,
    ]
    if keep_workspace:
        cmd.append('--keep_workspace')

    start_time = time.time()

    try:
        logger.info(f"Process {process_id}: Running command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(script_dir)
        )

        elapsed_time = time.time() - start_time

        if result.returncode == 0:
            logger.info(
                f"Process {process_id}: Completed successfully in {elapsed_time:.2f} seconds"
            )
            return {
                'process_id': process_id,
                'start_idx': start_idx,
                'num_instances': num_instances,
                'success': True,
                'elapsed_time': elapsed_time,
                'returncode': result.returncode
            }
        else:
            logger.error(
                f"Process {process_id}: Failed with return code {result.returncode}\nStderr: {result.stderr}"
            )
            return {
                'process_id': process_id,
                'start_idx': start_idx,
                'num_instances': num_instances,
                'success': False,
                'elapsed_time': elapsed_time,
                'returncode': result.returncode,
                'stderr': result.stderr
            }

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Process {process_id}: Exception occurred: {e}")
        return {
            'process_id': process_id,
            'start_idx': start_idx,
            'num_instances': num_instances,
            'success': False,
            'elapsed_time': elapsed_time,
            'error': str(e)
        }


def main():
    args = get_options().parse_args()

    Path('logs').mkdir(exist_ok=True)

    logger.info("=" * 80)
    logger.info("Parallel Docker Batch Runner for Kimi CLI")
    logger.info("=" * 80)

    instances = load_instances(args.jsonl_file)
    if not instances:
        logger.error("No instances found in dataset")
        sys.exit(1)

    logger.info(f"Total instances in dataset: {len(instances)}")

    if args.load_from_file:
        logger.info(f"Loading completed instances from: {args.load_from_file}")
        try:
            with open(args.load_from_file, 'r', encoding='utf-8') as f:
                completed = json.load(f)
            completed_ids = {ins["instance_id"] for ins in completed}
            logger.info(f"Found {len(completed_ids)} completed instances")
            instances = [ins for ins in instances if ins.get("instance_id") not in completed_ids]
            logger.info(f"Remaining instances to process: {len(instances)}")
        except Exception as e:
            logger.error(f"Error loading from file {args.load_from_file}: {e}")
            sys.exit(1)

    if args.num_instances is None:
        instances = instances[args.start_idx:]
    else:
        instances = instances[args.start_idx:args.start_idx + args.num_instances]

    if not instances:
        logger.error("No instances to process after filtering and slicing")
        sys.exit(1)

    total_to_process = len(instances)
    logger.info(f"Processing {total_to_process} instances using {args.num_processes} parallel processes")

    instances_per_process = total_to_process // args.num_processes
    remainder = total_to_process % args.num_processes

    temp_dir = Path('logs') / 'temp_parallel'
    temp_dir.mkdir(exist_ok=True)
    timestamp = int(time.time())

    tasks = []
    current_idx = 0

    for i in range(args.num_processes):
        num_for_this_process = instances_per_process + (1 if i < remainder else 0)
        if num_for_this_process > 0:
            process_instances = instances[current_idx:current_idx + num_for_this_process]
            temp_jsonl = temp_dir / f'process_{i}_{timestamp}.jsonl'
            with open(temp_jsonl, 'w', encoding='utf-8') as f:
                for instance in process_instances:
                    f.write(json.dumps(instance, ensure_ascii=False) + '\n')

            tasks.append({
                'process_id': i,
                'start_idx': 0,
                'num_instances': num_for_this_process,
                'temp_jsonl': str(temp_jsonl)
            })
            current_idx += num_for_this_process

    logger.info("\nPre-creating workspace directories...")
    for i in range(args.num_processes):
        workspace_dir = Path(f"{args.workspace_root}_process_{i}").resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()

    with multiprocessing.Pool(processes=args.num_processes) as pool:
        async_results = []
        for i, task in enumerate(tasks):
            if i > 0:
                time.sleep(1)

            result = pool.apply_async(
                run_batch_process,
                args=(
                    task['process_id'],
                    task['start_idx'],
                    task['num_instances'],
                    args.jsonl_file,
                    args.results_dir,
                    args.workspace_root,
                    args.model,
                    args.setup_script,
                    args.python_executable,
                    args.keep_workspace,
                    task.get('temp_jsonl'),
                    args.strategy
                )
            )
            async_results.append(result)

        results = []
        for async_result in async_results:
            try:
                res = async_result.get()
                results.append(res)
            except Exception as e:
                logger.error(f"Error getting result from process: {e}")
                results.append({'success': False, 'error': str(e)})

    total_elapsed = time.time() - start_time

    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total elapsed time: {total_elapsed:.2f} seconds")

    successful = sum(1 for r in results if r.get('success', False))
    failed = len(results) - successful

    logger.info(f"Successful processes: {successful}/{len(results)}")
    logger.info(f"Failed processes: {failed}/{len(results)}")

    summary_file = Path('logs') / f'parallel_run_summary_{int(time.time())}.json'
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            'total_elapsed': total_elapsed,
            'num_processes': args.num_processes,
            'total_instances': total_to_process,
            'results': results
        }, f, indent=2)

    logger.info(f"\nSummary saved to: {summary_file}")

    for task in tasks:
        temp_file = task.get('temp_jsonl')
        if temp_file and Path(temp_file).exists():
            try:
                Path(temp_file).unlink()
            except Exception as e:
                logger.warning(f"Failed to remove {temp_file}: {e}")

    try:
        if temp_dir.exists() and not any(temp_dir.iterdir()):
            temp_dir.rmdir()
    except Exception:
        pass

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
