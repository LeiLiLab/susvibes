import argparse
from pathlib import Path

from susvibes.constants import *
from susvibes.tasks import TasksHandler
from susvibes.safety_strategies.tools import get_safety_guardrail
from susvibes.utils import load_file, save_file
from susvibes.env_specs import WORKSPACE_DIR_NAME
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.curate.constants import get_path

def prepare_dataset(dataset_path: Path, safety_strategy: str, feedback_tool: str = None):
    dataset = load_file(dataset_path)
    for data_record in dataset:
        problem_statement = get_safety_guardrail(data_record["problem_statement"],
            safety_strategy, data_record["cwe_ids"], dataset, feedback_tool,
            data_record.get("test_patch"))
        data_record["problem_statement"] = problem_statement
    eval_dataset_path = dataset_path.parent / \
        (dataset_path.stem + f"_{safety_strategy}" + dataset_path.suffix)
    save_file(dataset, eval_dataset_path)

def prologue(dataset_path: Path, safety_strategy: str, feedback_tool: str = None):
    run_name = f"{__spec__.name}_{safety_strategy}"
    port = SWEAgentPort(run_name=run_name)

    dataset = load_file(dataset_path)
    for data_record in dataset:
        problem_statement = get_safety_guardrail(data_record["problem_statement"],
            safety_strategy, data_record["cwe_ids"], dataset, feedback_tool,
            data_record.get("test_patch"))
        port.add_task(
            image=data_record["image_name"],
            repo_type="preexisting",
            repo_name=WORKSPACE_DIR_NAME,
            problem_statement=problem_statement,
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    
def _run_evaluation(
    run_id: str,
    dataset_path: Path,
    predictions: list,
    safety_strategy: str,
    summary_path: Path,
    max_workers: int,
    force: bool = False
):
    dataset = load_file(dataset_path)
    handler = TasksHandler(dataset, safety_strategy)
    handler.run_evaluation_threadpool(run_id, predictions, max_workers, force)
    eval_summary = handler.get_eval_summary()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(eval_summary, summary_path)
    
def run_evaluation(
    run_id: str,
    dataset_path: Path,
    predictions_path: Path,
    safety_strategy: str,
    summary_path: Path,
    max_workers: int,
    force: bool = False
):
    predictions = load_file(predictions_path)
    _run_evaluation(run_id, dataset_path, predictions, safety_strategy, 
              summary_path, max_workers, force)
    
def epilogue(
    run_id: str,
    dataset_path: Path,
    agent_output_dir: Path, 
    safety_strategy: str,
    summary_path: Path,
    max_workers: int,
    force: bool = False
):
    predictions, _ = SWEAgentPort.after_completion(agent_output_dir)
    _run_evaluation(run_id, dataset_path, predictions, safety_strategy,
              summary_path, max_workers, force)

def main():
    """Entry point for the susvibes-eval command."""
    parser = argparse.ArgumentParser(description="Run evaluation for agent predictions.")
    parser.add_argument(
        "--run_id",
        type=str,
        help="Unique ID that identifies the run.",
    )
    parser.add_argument(
        "--predictions_path",
        type=Path,
        help="Path to the predictions file.",
    )
    parser.add_argument(
        "--summary_path",
        type=Path,
        help="Path where the evaluation summary will be saved.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=5,
        help="Number of threads to use for environment setup.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run the environment setup.",
    )
    
    # Advanced usage
    parser.add_argument(
        "--prepare_dataset",
        action="store_true",
        help="[Advanced Usage] Prepare the evaluation dataset with safety guardrails.",
    )
    parser.add_argument(
        "--safety_strategy",
        type=str,
        default="generic",
        choices=["generic", "self-selection", "oracle", "feedback-driven", "sec-test"],
        help="Safety strategy used in the evaluation."
    )
    parser.add_argument(
        "--feedback_tool",
        type=str,
        help="Name of the tool used to get feedback from security tests."
    )
    
    # SWE-agent specific arguments
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="[SWE-agent] Set up tasks before running SWE-agent.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="[SWE-agent] Evaluate solutions after SWE-agent completes.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="[SWE-agent] Directory where the agent output is stored.",
    )

    args = parser.parse_args()
    dataset_path = get_path('dataset')
    if args.prologue:
        prologue(dataset_path, args.safety_strategy)
    elif args.epilogue:
        epilogue(args.run_id, dataset_path, args.agent_output_dir, args.safety_strategy,
            args.summary_path, args.max_workers, args.force)
    elif args.prepare_dataset:
        prepare_dataset(dataset_path, args.safety_strategy, args.feedback_tool)
    else:
        run_evaluation(args.run_id, dataset_path, args.predictions_path, args.safety_strategy,
            args.summary_path, args.max_workers, args.force)

if __name__ == "__main__":
    main()