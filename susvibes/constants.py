import os
from enum import Enum, StrEnum
from pathlib import Path

root_dir = Path(__file__).parent.parent
current_dir = Path(__file__).parent

ENV_SPECS_DIR = current_dir / "env_specs"

def get_env_spec_path(name: str, run_id: str = "default") -> Path:
    base = ENV_SPECS_DIR / run_id
    paths = {
        'dev_tools': base / 'dev_tools.json',
        'components': base / 'components.json',
    }
    return paths[name]

EVALUATION_LOG_DIR = root_dir / "logs/run_evaluation"
LOG_SUMMARY = "summary.json"

class ContainerLimits:
    """Resource limits and run timeout for analysis / test containers."""
    RUN_TIMEOUT = 1800  # seconds
    MEM_LIMIT = "{}g".format(
        min(int(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024 ** 3) * 0.75), 16))
    CPU_LIMIT = min(16, max(1, int(os.cpu_count() * 0.75)))

DOCKERHUB_USERNAME = "songwen6968"
HF_DATASET_REPO = "songwen6968/SusVibes"
HF_DATASET_FILENAME = "susvibes_dataset.jsonl"
ARCH = os.uname().machine

class Strategies(StrEnum):
    GENERIC = "generic"
    SELF_SELECTION = "self-selection"
    ORACLE = "oracle"
    FEEDBACK_DRIVEN = "feedback-driven"
    SEC_TEST = "sec-test"

class PredictionKeys(StrEnum):
    INSTANCE_ID = "instance_id"
    PREDICTION = "model_patch"
    MODEL = "model_name_or_path"

class EvalStatus(StrEnum):
    NO_PATCH = "no_patch"
    MODEL_PATCH_ERROR = "model_patch_error"
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"

class TestItemStatus(StrEnum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"

class TestStatus(StrEnum):
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"

FAILURE_STATUSES = {TestItemStatus.FAILED, TestItemStatus.ERROR}
