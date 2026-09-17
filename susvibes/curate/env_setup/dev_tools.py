"""
Purpose: Identify the Python version required by each task's repository
using an agent, and map it to an available base image version.

python -m susvibes.curate.env_setup.dev_tools \
    --run_id playground
"""

import argparse
import json
import re
from pathlib import Path
from jinja2 import Template

from susvibes.core.constants import *
from susvibes.curate.constants import KeepStage, LOCAL_REPOS_DIR, get_log_dir, get_agent_setting_path
from susvibes.env_specs import DEV_TOOL_VERSIONS
from susvibes.curate.env_setup.prompts import DEV_TOOLS_PROMPT_TEMPLATE
from susvibes.core.agents.sweagent import SWEAgentPort
from susvibes.core.report import get_two_state_summary, print_summary
from susvibes.core.utils import load_file, save_file, parse_instance_id, touched_files, get_env_specs, save_env_specs, filter_binary_files
from susvibes.curate.utils import (
    get_repo_dir,
    RepoLocks,
    reset_to_commit,
    apply_patch,
    should_keep,
)

KEEP_STAGE = KeepStage.DEV_TOOLS   # this stage's verdict under record["keep"]
LOG_SUMMARY = "summary.json"

root_dir = Path(__file__).parent.parent.parent.parent

def prologue(dataset_path: Path, run_id: str = "default", instance_ids: list = None):
    port = SWEAgentPort.from_settings(load_file(get_agent_setting_path("curate")),
        run_name=__spec__.name, output_dir=get_log_dir(run_id, "env_setup", "dev_tools"))
    dataset = load_file(dataset_path)
    gated = [data_record for data_record in dataset if should_keep(data_record, exclude=KEEP_STAGE)]
    print(f"{len(gated)} / {len(dataset)} records gated by every stage that has judged them "
          "(or by none yet).")
    dataset = gated
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
            if data_record["instance_id"] in set(instance_ids)]
    for data_record in dataset:
        repo_dir = get_repo_dir(data_record["project"], root_dir=LOCAL_REPOS_DIR)
        # The clone is shared: hold it across every touch of the tree.
        with RepoLocks.locked(data_record["project"]):
            reset_to_commit(repo_dir, data_record["base_commit"], new_branch=True)
        security_files = sorted(touched_files(data_record["security_patch"]))
        port.add_task(
            repo_type="local",
            repo_dir=repo_dir,
            lock_path=RepoLocks.get_lock_path(repo_dir),
            base_commit=data_record["base_commit"],
            problem_statement=Template(DEV_TOOLS_PROMPT_TEMPLATE).render(
                security_files=security_files,
            ),
            instance_id=data_record["instance_id"],
        )
    port.before_start()
    return port

def epilogue(dev_tools_log_dir: Path, run_id: str = "default", instance_ids: list = None):
    predictions, total_cost = SWEAgentPort.after_completion(dev_tools_log_dir)
    if instance_ids is not None:
        predictions = [pred for pred in predictions
            if pred["instance_id"] in set(instance_ids)]
    env_specs = get_env_specs(run_id, ("dev_tools",))
    predicted = {pred["instance_id"] for pred in predictions}   # what this run has to say
    succeeded, errored, num_rounded = [], {}, 0

    for pred in predictions:
        instance_id = pred["instance_id"]
        project, base_commit = parse_instance_id(instance_id)
        repo_dir = get_repo_dir(project, root_dir=LOCAL_REPOS_DIR)

        # The clone is shared: hold it across every touch of the tree.
        with RepoLocks.locked(project):
            reset_to_commit(repo_dir, base_commit, new_branch=False)
            if not pred.get("model_patch", "").strip():
                errored[instance_id] = "Empty model_patch"
                continue
            try:
                # The agent's container can sweep a compiled .pyc into the diff; `git apply` fails the
                # WHOLE patch on a binary block, taking dev_tools.json down with it (mirrors build_repo).
                apply_patch(repo_dir, filter_binary_files(pred["model_patch"]))
            except Exception as e:
                errored[instance_id] = f"Failed to apply model_patch: {e}"
                continue
            try:
                dev_tool = load_file(repo_dir / "dev_tools.json")
            except FileNotFoundError:
                errored[instance_id] = "Dev tools not found"
                continue
            if not ("name" in dev_tool and "version" in dev_tool):
                errored[instance_id] = "Dev tools malformed"
                continue
            cleaned_version = re.sub(r"[^0-9.]", "", dev_tool["version"])
        # Drop empty components: a wildcard like "3.x" cleans to "3." and would otherwise
        # carry an unparseable "" into to_num below, aborting the whole batch over one record.
        parts = [part for part in cleaned_version.split(".") if part][:2]
        if not parts:
            errored[instance_id] = f"No version number: {dev_tool['version']}"
            continue
        if len(parts) == 1:
            parts.append("0")
        dev_tool["version"] = ".".join(parts)
        if dev_tool["name"] not in DEV_TOOL_VERSIONS:
            errored[instance_id] = f"Unsupported dev tool: {dev_tool['name']}"
            continue
        tool_config = DEV_TOOL_VERSIONS[dev_tool["name"]]
        available_versions = list(tool_config["versions"])
        to_num = lambda v: sum(int(part) * 10 ** (2 * i)
            for i, part in enumerate(v.split(".")[::-1]))
        if dev_tool["version"] not in available_versions:
            min_compat = tool_config["minimal_compatible_version"]
            if to_num(dev_tool["version"]) < to_num(min_compat):
                errored[instance_id] = f"Version below the minimum: {dev_tool['version']} < {min_compat}"
                continue
            dev_tool["version"] = min(available_versions,
                key=lambda v: abs(to_num(v) - to_num(dev_tool["version"])))
            num_rounded += 1

        env_specs[instance_id] = {"dev_tools": dev_tool}
        succeeded.append(instance_id)

    summary = get_two_state_summary(succeeded, errored)
    print_summary(summary)
    print(f"{num_rounded} version(s) rounded to the nearest available.")
    if total_cost is not None:
        print(f"Agent cost: ${total_cost:.2f}")
    save_file(summary, Path(dev_tools_log_dir) / LOG_SUMMARY)
    print(f"Summary saved to {Path(dev_tools_log_dir) / LOG_SUMMARY}.")

    dev_tools_path = save_env_specs("dev_tools", env_specs, run_id)
    print(f"Dev tools saved to {dev_tools_path}.")

    # Record the verdict on the dataset, not just in env_specs: an instance with no usable
    # interpreter cannot go on, and every later stage should read that from `keep` rather than
    # re-deriving it from whether env_specs happens to hold a key.
    dataset_path = get_dataset_path("dataset", run_id)
    dataset = load_file(dataset_path)
    for data_record in dataset:
        instance_id = data_record["instance_id"]
        # A verdict exists either because this run produced a prediction, or because env_specs
        # already carries a version (seeded from an earlier run — no prediction, but judged all the
        # same). Anything else this stage has simply not seen, and must stay unjudged.
        if instance_id in predicted or instance_id in env_specs:
            data_record.setdefault("keep", {})[KEEP_STAGE] = instance_id in env_specs
    save_file(dataset, dataset_path)
    print(f"dataset annotated in place with keep.{KEEP_STAGE}: {dataset_path}.")

def pipeline(dataset_path: Path, run_id: str = "default", instance_ids: list = None):
    port = prologue(dataset_path, run_id=run_id, instance_ids=instance_ids)
    epilogue(port.run_batch(), run_id=run_id, instance_ids=instance_ids)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run prologue or epilogue for determining environment dev tools.")
    parser.add_argument(
        "--prologue",
        action="store_true",
        help="Run the prologue of dev tools setup.",
    )
    parser.add_argument(
        "--epilogue",
        action="store_true",
        help="Run the epilogue of dev tools setup.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs.",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
    )
    args = parser.parse_args()

    dev_tools_log_dir = get_log_dir(args.run_id, "env_setup", "dev_tools")

    dataset_path = get_dataset_path('dataset', args.run_id)
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}")
        exit(1)

    if args.prologue:
        prologue(dataset_path, run_id=args.run_id, instance_ids=args.instance_ids)
    elif args.epilogue:
        epilogue(dev_tools_log_dir, run_id=args.run_id, instance_ids=args.instance_ids)
    else:
        pipeline(dataset_path, run_id=args.run_id, instance_ids=args.instance_ids)
    print(f"Logs saved to {dev_tools_log_dir}.")