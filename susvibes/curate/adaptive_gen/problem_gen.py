import re
import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_agent_setting_path, get_log_dir
from susvibes.curate.adaptive_gen.prompts import PROBLEM_GEN_PROMPT_TEMPLATE
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.utils import load_file, save_file, setup_logger
from susvibes.curate.utils import (
    RepoLocks,
    get_repo_dir,
    apply_patch,
    reset_to_commit,
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

def init_logger(adaptive_gen_log_dir):
    global logger
    logger = setup_logger(adaptive_gen_log_dir, "problem_gen.log", __name__, add_stdout=False)

def prologue(dataset_path: Path, output_dir: Path, instance_ids: list = None, require_test: bool = True):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")),
        run_name=__spec__.name, output_dir=output_dir)
    dataset = load_file(dataset_path)
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in tqdm(dataset, desc="Preparing agent run"):
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The clone is shared: hold it from the rollback through every read of the tree it leaves.
        with RepoLocks.locked(data_record["project"]):
            test_patch = data_record["test_patch"] if require_test else None
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)

        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            lock_path=RepoLocks.get_lock_path(repo_dir),
            base_commit=rollback_commit,
            problem_statement=Template(PROBLEM_GEN_PROMPT_TEMPLATE).render(
                mask_patch=data_record["mask_patch"]),
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(output_dir: Path, dataset_path: Path, require_test: bool = True):
    predictions, total_cost = SWEAgentPort.after_completion(output_dir, submitted_only=True)
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in load_file(dataset_path)}

    successful_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        data_record = dataset_by_id[pred["instance_id"]]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The whole read runs on one tree state, so the lock spans it; the `finally` is what makes
        # every exit — including the `continue`s — hand the clone back as it was found.
        with RepoLocks.locked(data_record["project"]):
            test_patch = data_record["test_patch"] if require_test else None
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)
            try:
                if not pred.get("model_patch", "").strip():
                    logger.warning("Empty model_patch for %s, skipping.", pred["instance_id"])
                    continue
                try:
                    apply_patch(repo_dir, pred["model_patch"])
                except Exception as e:
                    logger.warning("Error applying model_patch for %s: %s", pred["instance_id"], e)
                    continue
                problem_statement_path = repo_dir / "problem_statement.md"
                if not problem_statement_path.exists():
                    logger.warning("Problem statement for %s not found.", pred["instance_id"])
                    continue
                problem_statement = load_file(problem_statement_path)
            finally:
                reset_to_commit(repo_dir, rollback_commit, new_branch=False)
        if TEST_REF_PATTERN.search(problem_statement):
            logger.warning("Problem statement for %s references tests, skipping.", pred["instance_id"])
            continue

        data_record["problem_statement"] = problem_statement
        successful_instance_ids.append(data_record["instance_id"])

    save_file(dataset_by_id.values(), dataset_path)
    return successful_instance_ids, total_cost

def pipeline(dataset_path: Path, output_dir: Path, instance_ids: list = None, require_test: bool = True, iter_id: int = None):
    logger.info("=== Problem generation pipeline started (iter=%s) ===", iter_id)
    port = prologue(dataset_path, output_dir, instance_ids, require_test=require_test)
    output_dir = port.run_batch()
    successful_instance_ids, total_cost = epilogue(
        output_dir=output_dir,
        dataset_path=dataset_path,
        require_test=require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, total_cost

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
        "--dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file of created tasks.",
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=True,
        help="Require repo-provided tests (default True); false uses the synthesized-test path.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID; locates the agent output dir logs/curate/<run_id>/adaptive_gen/problem_gen/.",
    )
    args = parser.parse_args()
    output_dir = get_log_dir(args.run_id, "adaptive_gen", "problem_gen")
    if args.prologue:
        prologue(args.dataset_path, output_dir=output_dir, require_test=args.require_test)
    elif args.epilogue:
        epilogue(output_dir, args.dataset_path, require_test=args.require_test)
    else:
        pipeline(args.dataset_path, output_dir=output_dir, require_test=args.require_test)