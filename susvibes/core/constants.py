import os
from enum import Enum, StrEnum
from pathlib import Path

# This module lives in susvibes/core/: the susvibes package dir is two levels up, repo root three.
root_dir = Path(__file__).parent.parent.parent
current_dir = Path(__file__).parent.parent

ENV_SPECS_DIR = current_dir / "env_specs"

ENV_SPEC_FILE_NAMES = {
    "dev_tools": "dev_tools.json",
    "dockerfile": "dockerfile.json",
    "logs_handler": "logs_handler.json",
}

EVAL_LOG_DIR = root_dir / "logs/eval"
LOG_SUMMARY = "summary.json"
DATASETS_DIR = root_dir / "datasets"


def get_dataset_path(name: str, run_id: str = "default") -> Path:
    base = DATASETS_DIR / run_id
    paths = {
        'raw_cve': DATASETS_DIR / 'raw_cve',
        'fix_dataset': base / 'fix_dataset.jsonl',
        'coverage_report': base / 'coverage_report.jsonl',
        'task_dataset': base / 'task_dataset.jsonl',
        'stats': base / 'stats.json',
        'env_dataset': base / 'env_dataset.jsonl',
        'dataset': base / 'susvibes_dataset.jsonl',
        'examples': base / 'examples',
        'edits': base / 'edits',
    }
    return paths[name]

class ContainerLimits:
    """Resource limits and run timeout for analysis / test containers."""
    RUN_TIMEOUT = 1800  # seconds
    MEM_LIMIT = "{}g".format(
        min(int(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024 ** 3) * 0.75), 16))
    CPU_LIMIT = min(16, max(1, int(os.cpu_count() * 0.75)))

DOCKERHUB_USERNAME = "songwen6968"
HF_DATASET_REPO = "songwen6968/SusVibes"
HF_DATASET_FILE_NAME = "susvibes_dataset.jsonl"
ARCH = os.uname().machine

class Strategies(StrEnum):
    NONE = "none"
    GENERIC = "generic"
    SELF_SELECTION = "self-selection"
    ORACLE = "oracle"
    FEEDBACK_DRIVEN = "feedback-driven"
    SEC_TEST = "sec-test"

class ImageLoc(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"

class PredictionKeys(StrEnum):
    INSTANCE_ID = "instance_id"
    PREDICTION = "model_patch"
    MODEL = "model_name_or_path"

class TestStatus(StrEnum):
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETED = "completed"

class EvalStatus(StrEnum):
    EMPTY_MODEL_PATCH = "empty_model_patch"
    MODEL_PATCH_ERROR = "model_patch_error"
    INDETERMINATE = "indeterminate"
    COMPLETED = "completed"
