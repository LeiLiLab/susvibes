#!/usr/bin/env python3
"""
Parallel Docker Batch Runner

This script divides the dataset into N parallel processes and runs
docker_batch_run.py for each process concurrently.
"""

import argparse
import json
import logging
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

# Import get_options from batch_run_docker to inherit its arguments
from .batch_run_docker import get_options as get_batch_options

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(message)s',
    handlers=[
        logging.FileHandler('logs/parallel_batch_run.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_options():
    # Get the base parser from batch_run_docker.py
    parser = get_batch_options()
    
    # Update the description for parallel execution
    parser.description = 'Run docker_batch_run.py in parallel across N processes'
    
    # Override num_instances default to None (to allow processing all instances)
    # Use set_defaults to safely override the default value
    parser.set_defaults(num_instances=None)
    # Update the help text for num_instances
    for action in parser._actions:
        if action.dest == 'num_instances':
            action.help = 'Number of instances to process (if None, process all from start_idx to end)'
            break
    
    # Add new options specific to parallel_batch_run.py
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
        help='Python executable to use for running docker_batch_run.py'
    )
    
    return parser


def load_instances(jsonl_file: str) -> List[Dict[str, Any]]:
    """
    Load instances from JSONL file.
    
    Args:
        jsonl_file: Path to the JSONL file
        
    Returns:
        List of instance dictionaries
    """
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
    temp_jsonl: str = None
) -> Dict[str, Any]:
    """
    Run batch_run_docker.py for a subset of instances.
    
    Args:
        process_id: Process ID for logging
        start_idx: Start index in the dataset
        num_instances: Number of instances to process
        jsonl_file: Path to the JSONL file
        results_dir: Path to the results directory
        workspace_root: Path to the workspace root
        model: Model name
        setup_script: Setup script path
        python_executable: Python executable to use
        keep_workspace: Whether to keep the workspace after cleanup
        temp_jsonl: Optional temporary JSONL file path
        
    Returns:
        Dictionary with process results
    """
    logger.info(
        f"Process {process_id}: Starting to process {num_instances} instances "
        f"from index {start_idx}"
    )
    
    # Create process-specific workspace with absolute path
    process_workspace = Path(f"{workspace_root}_process_{process_id}").resolve()
    if not process_workspace.exists():
        logger.info(f"Process {process_id}: Creating workspace directory: {process_workspace}")
        process_workspace.mkdir(parents=True, exist_ok=True)
    
    # Convert to string for passing to subprocess
    process_workspace_str = str(process_workspace)
    
    # Use temp_jsonl if provided, otherwise use the original jsonl_file
    target_jsonl = temp_jsonl if temp_jsonl else jsonl_file
    
    # Build the command with unique timestamp suffix to avoid directory collisions
    cmd = [
        python_executable,
        'batch_run_docker.py',
        '--jsonl_file', target_jsonl,
        '--results_dir', results_dir,
        '--workspace_root', process_workspace_str,
        '--num_instances', str(num_instances),
        '--model', model,
        '--setup_script', setup_script,
        '--timestamp_suffix', f'process{process_id}',
        '--keep_workspace', str(keep_workspace)
    ]
    
    start_time = time.time()
    
    try:
        # Run the subprocess
        logger.info(f"Process {process_id}: Running command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd='.'
        )
        
        elapsed_time = time.time() - start_time
        
        if result.returncode == 0:
            logger.info(
                f"Process {process_id}: Completed successfully in "
                f"{elapsed_time:.2f} seconds"
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
                f"Process {process_id}: Failed with return code "
                f"{result.returncode}\nStderr: {result.stderr}"
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
    """
    Main function to orchestrate parallel batch runs.
    """
    args = get_options().parse_args()
    
    # Create logs directory if it doesn't exist
    Path('logs').mkdir(exist_ok=True)
    
    logger.info("=" * 80)
    logger.info("Parallel Docker Batch Runner")
    logger.info("=" * 80)
    
    # Load instances from JSONL file
    instances = load_instances(args.jsonl_file)
    if not instances:
        logger.error("No instances found in dataset")
        sys.exit(1)
    
    logger.info(f"Total instances in dataset: {len(instances)}")
    
    # Apply load_from_file logic (same as docker_batch_run.py)
    if args.load_from_file:
        logger.info(f"Loading completed instances from: {args.load_from_file}")
        try:
            with open(args.load_from_file, 'r') as f:
                results = json.load(f)
            completed_instances = [ins["instance_id"] for ins in results]
            logger.info(f"Found {len(completed_instances)} completed instances")
            
            # Store original indices before filtering
            original_indices = {}
            for idx, ins in enumerate(instances):
                original_indices[ins.get('instance_id', f'unknown_{idx}')] = idx
            
            # Filter out completed instances
            waiting_instances = [ins for ins in instances if ins.get('instance_id', f'unknown_{instances.index(ins)}') not in completed_instances]
            logger.info(f"Remaining instances to process: {len(waiting_instances)}")
            instances = waiting_instances
        except Exception as e:
            logger.error(f"Error loading from file {args.load_from_file}: {e}")
            sys.exit(1)
    
    # Apply start_idx and num_instances slicing (same as docker_batch_run.py)
    if args.num_instances is None:
        instances = instances[args.start_idx:]
    else:
        instances = instances[args.start_idx:args.start_idx + args.num_instances]
    
    if not instances:
        logger.error(
            f"No instances to process after filtering (start_idx={args.start_idx}, "
            f"num_instances={args.num_instances})"
        )
        sys.exit(1)
    
    total_to_process = len(instances)
    logger.info(
        f"Processing {total_to_process} instances (after filtering and slicing)"
    )
    logger.info(f"Using {args.num_processes} parallel processes")
    
    # Divide instances among processes
    instances_per_process = total_to_process // args.num_processes
    remainder = total_to_process % args.num_processes
    
    # Create temporary JSONL files for each process
    temp_dir = Path('logs') / 'temp_parallel'
    temp_dir.mkdir(exist_ok=True)
    timestamp = int(time.time())
    
    tasks = []
    current_idx = 0
    
    for i in range(args.num_processes):
        # Distribute remainder among first few processes
        num_for_this_process = instances_per_process + (1 if i < remainder else 0)
        
        if num_for_this_process > 0:
            # Get the subset of instances for this process
            process_instances = instances[current_idx:current_idx + num_for_this_process]
            
            # Write to temporary JSONL file
            temp_jsonl = temp_dir / f'process_{i}_{timestamp}.jsonl'
            with open(temp_jsonl, 'w', encoding='utf-8') as f:
                for instance in process_instances:
                    f.write(json.dumps(instance, ensure_ascii=False) + '\n')
            
            tasks.append({
                'process_id': i,
                'start_idx': 0,  # Always start from 0 in the temp file
                'num_instances': num_for_this_process,
                'temp_jsonl': str(temp_jsonl)
            })
            current_idx += num_for_this_process
    
    # Log the distribution
    logger.info("\nTask distribution:")
    for task in tasks:
        logger.info(
            f"  Process {task['process_id']}: "
            f"{task['num_instances']} instances in {task['temp_jsonl']}"
        )
    logger.info("")
    
    # Pre-create workspace directories to avoid race conditions
    logger.info("\nPre-creating workspace directories...")
    for i in range(args.num_processes):
        workspace_dir = Path(f"{args.workspace_root}_process_{i}").resolve()
        if not workspace_dir.exists():
            workspace_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"  Created: {workspace_dir}")
        else:
            logger.info(f"  Already exists: {workspace_dir}")
    logger.info("")
    
    # Run processes in parallel
    start_time = time.time()
    
    with multiprocessing.Pool(processes=args.num_processes) as pool:
        # Create async tasks with 1-second delay between each to avoid timestamp overlap
        async_results = []
        for i, task in enumerate(tasks):
            if i > 0:  # Add delay for all processes except the first one
                logger.info(f"Waiting 1 second before starting process {task['process_id']}...")
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
                    task.get('temp_jsonl')  # Pass temp_jsonl if it exists
                )
            )
            async_results.append(result)
        
        # Wait for all processes to complete
        results = []
        for async_result in async_results:
            try:
                result = async_result.get()
                results.append(result)
            except Exception as e:
                logger.error(f"Error getting result from process: {e}")
                results.append({
                    'success': False,
                    'error': str(e)
                })
    
    total_elapsed = time.time() - start_time
    
    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total elapsed time: {total_elapsed:.2f} seconds")
    
    successful = sum(1 for r in results if r.get('success', False))
    failed = len(results) - successful
    
    logger.info(f"Successful processes: {successful}/{len(results)}")
    logger.info(f"Failed processes: {failed}/{len(results)}")
    
    for result in results:
        status = "✓" if result.get('success', False) else "✗"
        logger.info(
            f"  {status} Process {result.get('process_id', '?')}: "
            f"{result.get('elapsed_time', 0):.2f}s"
        )
    
    # Save summary
    summary_file = Path('logs') / f'parallel_run_summary_{int(time.time())}.json'
    with open(summary_file, 'w') as f:
        json.dump({
            'total_elapsed': total_elapsed,
            'num_processes': args.num_processes,
            'total_instances': total_to_process,
            'results': results
        }, f, indent=2)
    
    logger.info(f"\nSummary saved to: {summary_file}")
    
    # Clean up temporary JSONL files
    logger.info("\nCleaning up temporary files...")
    for task in tasks:
        temp_file = task.get('temp_jsonl')
        if temp_file and Path(temp_file).exists():
            try:
                Path(temp_file).unlink()
                logger.info(f"  Removed: {temp_file}")
            except Exception as e:
                logger.warning(f"  Failed to remove {temp_file}: {e}")
    
    # Try to remove temp directory if empty
    try:
        if temp_dir.exists() and not any(temp_dir.iterdir()):
            temp_dir.rmdir()
            logger.info(f"  Removed empty directory: {temp_dir}")
    except Exception as e:
        logger.debug(f"  Could not remove temp directory: {e}")
    
    logger.info("=" * 80)
    
    # Exit with error code if any process failed
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

