import argparse
import json
from tqdm import tqdm
from pathlib import Path
from jinja2 import Template

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_agent_setting_path, get_log_dir
from susvibes.curate.adaptive_gen.prompts import MASK_GEN_PROMPT_TEMPLATE
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.utils import (
    load_file,
    save_file,
    filter_binary_files,
    filter_text_files,
    setup_logger,
)
from susvibes.curate.utils import (
    RepoLocks,
    get_repo_dir,
    apply_patch,
    rollback,
    reset_to_commit,
    len_patch,
)

logger = None

def count_patch_additions_deletions(patch):
    """Count added and deleted lines in a patch string."""
    additions, deletions = 0, 0
    for line in patch.splitlines():
        if line.startswith('+++ ') or line.startswith('--- '):
            continue
        if line.startswith('+'):
            additions += 1
        elif line.startswith('-'):
            deletions += 1
    return additions, deletions

def is_placeholder(line: str) -> bool:
    """Whether an added line is a trivial deletion-mask placeholder: a blank line or a
    whole-line stub (`pass`, `...`, `return [None]`, `raise NotImplementedError`)."""
    stripped = line.strip()
    return not stripped or stripped in ("pass", "...", "return", "return None") \
        or stripped.startswith("raise NotImplementedError")

def is_subsequence(short: str, full: str) -> bool:
    """Whether every character of `short` appears in `full`, in order (not necessarily
    contiguous). An added mask line whose content is a subsequence of some deleted line
    introduced no new content — it is that line with a part removed (an import narrowed,
    a call argument or base class dropped), the unavoidable by-product of a deletion —
    not a fabricated stub, whose new literals (`None`, `[]`, `...`) never subsequence in."""
    it = iter(full)
    return all(char in it for char in short)

def init_logger(adaptive_gen_log_dir):
    global logger
    logger = setup_logger(adaptive_gen_log_dir, "mask.log", __name__, add_stdout=False)

def get_effective_ratio(data_record: dict, length_ratio: int, max_length: int | None) -> int:
    """The deletion ratio this instance is held to. The prologue asks the agent for it and the
    epilogue judges against it, so it is derived once — two copies drift, and a mask judged by a
    ratio nobody asked for is the over-rejection this guards. `max_length` caps it against the same
    `diff_base` the gate multiplies by, so `diff_base * ratio` (the deletions demanded) stays within
    `max_length` — dividing by anything else lets the two disagree on how big the mask may be."""
    if not max_length:
        return int(length_ratio)
    diff_added, diff_deleted = count_patch_additions_deletions(data_record["security_patch"])
    diff_base = diff_deleted or diff_added
    return int(min(length_ratio, max(max_length / diff_base - 1, 1)))


def prologue(
    dataset_path: Path,
    output_dir: Path,
    length_ratio: int = 1,
    max_length: int = None,
    instance_ids: list = None,
    require_test: bool = True,
):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")),
        run_name=__spec__.name, output_dir=output_dir)
    dataset = load_file(dataset_path)
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in tqdm(dataset, desc="Preparing agent run"):
        instance_id = data_record["instance_id"]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The clone is shared: hold it from the rollback through every read of the tree it leaves.
        with RepoLocks.locked(data_record["project"]):
            try:
                test_patch = data_record["test_patch"] if require_test else None
                rollback_commit = rollback(repo_dir, data_record["base_commit"],
                    data_record["security_patch"], test_patch)
            except Exception as e:
                logger.error("Error rolling back repository for %s: %s", instance_id, e)
                continue
        effective_ratio = get_effective_ratio(data_record, length_ratio, max_length)
        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            lock_path=RepoLocks.get_lock_path(repo_dir),
            base_commit=rollback_commit,
            problem_statement=Template(MASK_GEN_PROMPT_TEMPLATE).render(
                ratio=effective_ratio,
                diff_patch=data_record["security_patch"]),
            instance_id=instance_id,
        )
    port.before_start()
    return port

def epilogue(
    output_dir: Path,
    dataset_path: Path,
    length_ratio: int = 1,
    max_length: int = None,
    ratio_tolerance: float = 0,
    require_test: bool = True,
):
    predictions, total_cost = SWEAgentPort.after_completion(output_dir, submitted_only=True)
    dataset_by_id = {data_record["instance_id"]: data_record
        for data_record in load_file(dataset_path)}

    successful_instance_ids = []
    for pred in tqdm(predictions, desc="Processing agent submissions"):
        instance_id = pred["instance_id"]
        data_record = dataset_by_id[instance_id]
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        test_patch = data_record["test_patch"] if require_test else None
        filtered_patch = filter_text_files(filter_binary_files(pred.get("model_patch", "")))
        if not filtered_patch.strip():
            logger.warning("Empty model_patch for %s, skipping.", instance_id)
            continue
        # Only the git half takes the lock — the filtering above is text. The mask is applied and
        # undone purely to prove it applies, so the tree is restored either way: a patch that fails
        # halfway would otherwise be left behind for whoever holds the clone next.
        with RepoLocks.locked(data_record["project"]):
            rollback_commit = rollback(repo_dir, data_record["base_commit"],
                data_record["security_patch"], test_patch)
            try:
                apply_patch(repo_dir, filtered_patch)
            except Exception as e:
                logger.warning("Error applying model_patch for %s: %s", instance_id, e)
                continue
            finally:
                reset_to_commit(repo_dir, rollback_commit, new_branch=False)
        if "--- /dev/null" in filtered_patch:
            logger.warning("Forbidden file creation for %s, skipping.", instance_id)
            continue
        # FORMAT 1: the mask deletes at least `ratio`x as many lines as the security diff
        # (a pure-addition diff gates on its added lines), and stays within max_length.
        diff_added, diff_deleted = count_patch_additions_deletions(data_record["security_patch"])
        _, mask_deleted = count_patch_additions_deletions(filtered_patch)
        _, diff_lines = len_patch(data_record["security_patch"])
        _, mask_lines = len_patch(filtered_patch)
        if max_length and mask_lines > max_length:
            logger.warning("Mask too long for %s: %d lines > max_length %d, skipping.",
                           instance_id, mask_lines, max_length)
            continue
        diff_base = diff_deleted or diff_added
        effective_ratio = get_effective_ratio(data_record, length_ratio, max_length)
        if mask_deleted < diff_base * max(effective_ratio - ratio_tolerance, 1):
            logger.warning("Mask too short for %s: %d mask deleted < %d diff base * %dx, skipping.",
                           instance_id, mask_deleted, diff_base, effective_ratio)
            continue
        # FORMAT 2: every `+` line is a trivial placeholder, or the leftover of a deletion
        # — its content a subsequence of some `-` line (an import narrowed, an argument or
        # base class dropped), which introduces nothing the mask did not remove.
        deleted = [line[1:].strip() for line in filtered_patch.splitlines()
            if line.startswith("-") and not line.startswith("--- ")]
        non_trivial = [line[1:].strip() for line in filtered_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
            and not is_placeholder(line[1:])
            and not any(is_subsequence(line[1:].strip(), d) for d in deleted)]
        if non_trivial:
            logger.warning("Mask has fabricated additions for %s: %d lines (e.g. %r), skipping.",
                           instance_id, len(non_trivial), non_trivial[0][:60])
            continue
        data_record["mask_patch"] = filtered_patch
        successful_instance_ids.append(instance_id)
            
    save_file(dataset_by_id.values(), dataset_path)
    return successful_instance_ids, total_cost

def pipeline(
    dataset_path: Path,
    output_dir: Path,
    length_ratio: int = 1,
    max_length: int = None,
    ratio_tolerance: float = 0,
    instance_ids: list = None,
    require_test: bool = True,
    iter_id: int = None,
):
    logger.info("=== Mask generation pipeline started (iter=%s, ratio=%.1fx) ===",
                iter_id, length_ratio)
    port = prologue(
        dataset_path=dataset_path,
        output_dir=output_dir,
        length_ratio=length_ratio,
        max_length=max_length,
        instance_ids=instance_ids,
        require_test=require_test,
    )
    output_dir = port.run_batch()
    successful_instance_ids, total_cost = epilogue(
        output_dir=output_dir,
        dataset_path=dataset_path,
        length_ratio=length_ratio,
        max_length=max_length,
        ratio_tolerance=ratio_tolerance,
        require_test=require_test,
    )
    logger.info("  Agent cost: $%.2f", total_cost or 0)
    return successful_instance_ids, total_cost

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for mask generation.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of mask generation.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of mask generation.",
    )
    parser.add_argument(
        "--dataset_path",
        type=Path,
        required=True,
        help="Path to the dataset file; mask generation annotates it in place.",
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
        help="Run ID; locates the agent output dir logs/curate/<run_id>/adaptive_gen/mask/.",
    )
    args = parser.parse_args()
    output_dir = get_log_dir(args.run_id, "adaptive_gen", "mask")
    if args.prologue:
        prologue(args.dataset_path, output_dir=output_dir, require_test=args.require_test)
    elif args.epilogue:
        epilogue(output_dir, args.dataset_path, require_test=args.require_test)
    else:
        pipeline(dataset_path=args.dataset_path, output_dir=output_dir,
            require_test=args.require_test)