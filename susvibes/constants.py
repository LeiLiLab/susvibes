import os
from enum import Enum
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

CONTAINER_RUN_TIMEOUT = 1800
CONTAINER_MEM_LIMIT = "{}g".format(
    min(int(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024 ** 3) * 0.75), 16))
CONTAINER_CPU_LIMIT = min(int(os.cpu_count() * 0.75), 16)

DOCKERHUB_USERNAME = "songwen6968"
ARCH = os.uname().machine

class SafetyStrategies(Enum):
    GENERIC = "generic"
    SELF_SELECTION = "self-selection"
    ORACLE = "oracle"
    FEEDBACK_DRIVEN = "feedback-driven"
    SEC_TEST = "sec-test"

class PredictionKeys(Enum):
    INSTANCE_ID = "instance_id"
    PREDICTION = "model_patch"
    MODEL = "model_name_or_path"

class EvalStatus(Enum):
    NO_PATCH = "no_patch"
    MODEL_PATCH_ERROR = "model_patch_error"
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"
