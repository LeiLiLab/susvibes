import argparse
import json
from pathlib import Path

from susvibes.constants import *
from susvibes.tasks import TasksHandler, get_summary, print_summary
from susvibes.strategies.tools import get_guardrail
from susvibes.utils import load_file, save_file
from susvibes.env_specs import WORKSPACE_DIR_NAME
from susvibes.curate.utils.agents.ports import SWEAgentPort
from susvibes.curate.constants import get_dataset_path

def prepare_dataset(dataset_path: Path, strategy: str, feedback_tool: str = None):
    dataset = load_file(dataset_path)
    for data_record in dataset:
        problem_statement = get_guardrail(data_record["problem_statement"],
            strategy, data_record["cwe_ids"], dataset, feedback_tool,
            data_record.get("test_patch"))
        data_record["problem_statement"] = problem_statement
    eval_dataset_path = dataset_path.parent / \
        (dataset_path.stem + f"_{strategy}" + dataset_path.suffix)
    save_file(dataset, eval_dataset_path)

def prologue(dataset_path: Path, strategy: str, feedback_tool: str = None):
    run_name = f"{__spec__.name}_{strategy}"
    port = SWEAgentPort(run_name=run_name)

    dataset = load_file(dataset_path)
    for data_record in dataset:
        problem_statement = get_guardrail(data_record["problem_statement"],
            strategy, data_record["cwe_ids"], dataset, feedback_tool,
            data_record.get("test_patch"))
        port.add_task(
            image=data_record["image_name"],
            repo_type="preexisting",
            repo_name=WORKSPACE_DIR_NAME,
            problem_statement=problem_statement,
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    
def run_evaluation(
    run_id: str,
    dataset_path: Path,
    predictions: list,
    strategy: str,
    max_workers: int,
    force: bool = False,
    instance_ids: list = None
):
    dataset = load_file(dataset_path)
    handler = TasksHandler(dataset, strategy, run_id)
    handler.run_evaluation_threadpool(predictions, max_workers, force, instance_ids=instance_ids)
    for model_name_or_path, model_reports in handler.reports.items():
        eval_summary = get_summary(dataset, model_reports, strategy, instance_ids=instance_ids)
        summary_path = EVAL_LOG_DIR / run_id / strategy / model_name_or_path / LOG_SUMMARY
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(eval_summary, summary_path)
        print(f"\n=== {model_name_or_path} ===")
        print_summary(eval_summary)
        print(f"Summary saved to {summary_path}.")

def main():
    """Entry point for the susvibes-eval command."""
    parser = argparse.ArgumentParser(description="Run evaluation for agent predictions.")
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Unique ID that identifies the run.",
    )
    parser.add_argument(
        "--predictions_path",
        type=Path,
        help="Path to the predictions file.",
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
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )

    # Advanced usage
    parser.add_argument(
        "--prepare_dataset",
        action="store_true",
        help="[Advanced Usage] Prepare the evaluation dataset with guardrails.",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="generic",
        choices=["generic", "self-selection", "oracle", "feedback-driven", "sec-test"],
        help="Advanced strategy used in the evaluation."
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
    # Always read the dataset from the "default" run; --run_id only sets the
    # eval-log output directory (logs/run_evaluation/<run_id>/...).
    dataset_path = get_dataset_path('dataset')
    if args.prologue:
        prologue(dataset_path, args.strategy)
    elif args.epilogue:
        predictions, _ = SWEAgentPort.after_completion(args.agent_output_dir)
        run_evaluation(args.run_id, dataset_path, predictions, args.strategy,
            args.max_workers, args.force, instance_ids=args.instance_ids)
    elif args.prepare_dataset:
        prepare_dataset(dataset_path, args.strategy, args.feedback_tool)
    else:
        predictions = load_file(args.predictions_path)
        run_evaluation(args.run_id, dataset_path, predictions, args.strategy,
            args.max_workers, args.force, instance_ids=args.instance_ids)

if __name__ == "__main__":
    main()