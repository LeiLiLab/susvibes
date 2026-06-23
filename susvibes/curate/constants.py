from enum import StrEnum
from pathlib import Path

from susvibes.core.constants import DATASETS_DIR, get_dataset_path

root_dir = Path(__file__).parent.parent.parent
LOCAL_REPOS_DIR = "/mnt/data2/songwenzhao/projects" #root_dir / 'projects'
CURATE_LOG_DIR = root_dir / "logs/curate"
AGENT_SETTINGS_DIR = Path(__file__).parent / "utils/agents/settings"


def get_log_dir(run_id: str, *module: str) -> Path:
    """Log dir for a run, grouped by run_id first then module:
    ``logs/curate/<run_id>/<module...>/`` — e.g.
    get_log_dir("v2", "mine", "process") -> logs/curate/v2/mine/process.
    Keeping every module's logs for one run together (instead of one dir per
    module) makes a single run's artifacts easy to find and clean up."""
    return CURATE_LOG_DIR.joinpath(run_id, *module)

def get_agent_setting_path(name: str) -> Path:
    return AGENT_SETTINGS_DIR / f"{name}.yaml"

LOGS_PARSER_MODEL = "o3"

class TaskArtifact(StrEnum):
    """Names of the files/dirs written into a task directory."""
    FEATURE_GOLDEN = "feature_golden.md"
    FEATURE_MASK = "feature_mask.md"
    SECURITY_FIX = "security_fix.md"
    FEATURE_VULN = "feature_vuln.md"
    PROBLEM_STATEMENT = "problem_statement.md"
    README = "README.md"
    TEST_PATCH = "test_patch.md"
    TEST_PATCH_BACKUPS = "test_patch_backups"


PATCH_TEMPLATE = """```diff\n\n{patch}\n```"""
