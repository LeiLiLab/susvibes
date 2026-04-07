from enum import Enum
from pathlib import Path

root_dir = Path(__file__).parent.parent
current_dir = Path(__file__).parent

DEV_TOOLS_PATH = current_dir / "env_specs/dev_tools.json"
ENV_SPECS_PATH = current_dir / "env_specs/components.json"

ENV_SETUP_LOG_DIR = root_dir / "logs/curate/env_setup"
EVALUATION_LOG_DIR = root_dir / "logs/run_evaluation"

CONTAINER_RUN_TIMEOUT = 1800

class SafetyStrategies(Enum):
    GENERIC = "generic"
    SELF_SELECTION = "self-selection"
    ORACLE = "oracle"
    HIDDEN_ORACLE = "hidden-oracle"
    FEEDBACK_DRIVEN = "feedback-driven"
    SEC_TEST = "sec-test"
    HIDDEN_SEC_TEST = "hidden-sec-test"

class PredictionKeys(Enum):
    INSTANCE_ID = "instance_id"
    PREDICTION = "model_patch"
    MODEL = "model_name_or_path"

class EvalStatus(Enum):
    MODEL_PATCH_ERROR = "model_patch_error"
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"
