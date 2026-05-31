from pathlib import Path

root_dir = Path(__file__).parent.parent.parent
DATASETS_DIR = root_dir / 'datasets'
LOCAL_REPOS_DIR = "/mnt/data2/songwenzhao/projects" #root_dir / 'projects'
curate_log_dir = root_dir / "logs/curate"
COLLECT_LOG_DIR = curate_log_dir / "collect"
ADAPTIVE_GEN_LOG_DIR = curate_log_dir / "adaptive_gen"
ENV_SETUP_LOG_DIR = curate_log_dir / "env_setup"
TEST_LOG_DIR = curate_log_dir / "test"
VALIDATE_LOG_DIR = curate_log_dir / "validate"
AGENT_RUN_LOG_DIR = root_dir / "logs/agent_runs"

LOGS_PARSER_MODEL = "o3"

FEATURE_GOLDEN_FILE = "feature_golden.md"
FEATURE_MASK_FILE = "feature_mask.md"
SECURITY_FIX_FILE = "security_fix.md"
FEATURE_VULN_FILE = "feature_vuln.md"
PROBLEM_STATEMENT_FILE = "problem_statement.md"
README_FILE = "README.md"
TEST_PATCH_FILE = "test_patch.md"
TEST_PATCH_BACKUPS_DIR_NAME = "test_patch_backups"

PATCH_TEMPLATE = """```diff\n\n{patch}\n```"""

def get_path(name: str, run_id: str = "default") -> Path:
    base = DATASETS_DIR / run_id
    paths = {
        'cve_records': DATASETS_DIR / 'cve_records',
        'processed_dataset': base / 'processed_dataset.jsonl',
        'coverage_report': base / 'coverage_report.jsonl',
        'task_dataset': base / 'task_dataset.jsonl',
        'stats': base / 'stats.json',
        'dataset': base / 'susvibes_dataset.jsonl',
        'examples': base / 'examples',
        'edits': base / 'edits',
    }
    return paths[name]
