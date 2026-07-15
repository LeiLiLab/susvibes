import re
import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_agent_setting_path
from susvibes.curate.adaptive_gen.prompts import PROBLEM_GEN_PROMPT_TEMPLATE
from susvibes.core.agents.ports import SWEAgentPort
from susvibes.curate.adaptive_gen.utils import module_setup_logger
from susvibes.core.utils import load_file, save_file
from susvibes.curate.utils import (
    get_repo_dir,
    apply_patch,
    rollback
)

logger = None

# Genuine test-suite references (NOT benign words like a `test` parameter, an example
# URL like /test.html, "membership testing", or a CLI command named `test`).
TEST_REF_PATTERN = re.compile(
    r"(?i)\b(?:"
    r"test\s+(?:suite|file|files|case|cases|fixture|fixtures|harness|coverage)"
    r"|(?:unit|integration|functional|regression)\s+tests?"
    r"|(?:repo(?:sitory)?(?:'s)?)\s+tests?"
    r"|pytest|unittest"
    r"|test_\w+\.py"
    r"|tests?\s+(?:pass|passes|passing|fail|fails|expect|expects|verify|cover)"
    r"|(?:pass|passing|satisfy)\s+(?:the\s+)?tests?"
    r")"
)

def init_logger():
    global logger
    logger = module_setup_logger("problem_gen.log", __name__, add_stdout=False)

def prologue(task_dataset_path: Path, instance_ids: list = None, require_test: bool = True):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")), run_name=__spec__.name)
    task_dataset = load_file(task_dataset_path)
    if instance_ids is not None:
        task_dataset = [data_record for data_record in task_dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in tqdm(task_dataset, desc="Preparing agent run"):
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        test_patch = data_record["test_patch"] if require_test else None
        rollback_commit = rollback(repo_dir, data_record["base_commit"],
            data_record["security_patch"], test_patch)

        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            base_commit=rollback_commit,
            problem_statement=Template(PROBLEM_GEN_PROMPT_TEMPLATE).render(
                mask_patch=data_record["mask_patch"]),
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(agent_output_dir: Path, task_dataset_path: Path, require_test: bool = True):
    predictions, total_cost = SWEAgentPort.after_completion(agent_output_dir, submitted_only=True)
    task_dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in load_file(task_dataset_path)}

    successful_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = task_dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        test_patch = data_record["test_patch"] if require_test else None
        rollback_commit = rollback(repo_dir, data_record["base_commit"],
            data_record["security_patch"], test_patch)
        if not pred.get("model_patch", "").strip():
            logger.warning("Empty model_patch for %s, skipping.", pred["instance_id"])
            continue
        try:
            apply_patch(repo_dir, pred["model_patch"])
        except Exception as e:
            logger.warning("Error applying model_patch for %s: %s", pred["instance_id"], e)
            continue
        problem_statement_path = repo_dir / "problem_statement.md"
        if problem_statement_path.exists():
            problem_statement = load_file(problem_statement_path)
        else:
            logger.warning("Problem statement for %s not found.", pred["instance_id"])
            continue
        if TEST_REF_PATTERN.search(problem_statement):
            logger.warning("Problem statement for %s references tests, skipping.", pred["instance_id"])
            continue

        data_record["problem_statement"] = problem_statement
        successful_instance_ids.append(data_record["instance_id"])

    save_file(task_dataset_by_id.values(), task_dataset_path)
    return successful_instance_ids, total_cost

def pipeline(task_dataset_path: Path, instance_ids: list = None, require_test: bool = True, iter_id: int = None):
    logger.info("=== Problem generation pipeline started (iter=%s) ===", iter_id)
    port = prologue(task_dataset_path, instance_ids, require_test=require_test)
    agent_output_dir = port.run_batch()
    successful_instance_ids, total_cost = epilogue(
        agent_output_dir=agent_output_dir,
        task_dataset_path=task_dataset_path,
        require_test=require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, total_cost

def remove_results(instance_ids: list):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")), run_name=__spec__.name)
    port.remove_results(instance_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for problem generation.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of problem generation.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of problem generation.",
    )
    parser.add_argument(
        "--task_dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file of created tasks.",
    )
    parser.add_argument(
        "--agent_output_dir",
        type=Path,
        help="Directory where the agent output is stored, required in epilogue.",
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Require repo-provided tests (default True); false uses the synthesized-test path.",
    )
    args = parser.parse_args()
    if args.prologue:
        prologue(args.task_dataset_path, require_test=args.require_test)
    elif args.epilogue:
        epilogue(args.agent_output_dir, args.task_dataset_path, require_test=args.require_test)
    else:
        pipeline(args.task_dataset_path, require_test=args.require_test)