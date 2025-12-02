from enum import Enum
from pathlib import Path

LOCAL_REPOS_DIR = Path("/mnt/data2/songwenzhao/projects")

DEV_TOOLS_PATH = Path("env_specs/dev_tools.json")
ENV_SPECS_PATH = Path("env_specs/components.json")

ENV_SETUP_LOG_DIR = Path("../logs/curate/env_setup")
EVALUATION_LOG_DIR = Path("../logs/run_evaluation")

class SafetyStrategies(Enum):
    GENERIC = "generic"
    SELF_SELECTION = "self-selection"
    ORACLE = "oracle"
    FEEDBACK_DRIVEN = "feedback-driven"

class PredictionKeys(Enum):
    INSTANCE_ID = "instance_id"
    PREDICTION = "model_patch"
    MODEL = "model_name_or_path"

class EvalStatus(Enum):
    MODEL_PATCH_ERROR = "model_patch_error"
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"
