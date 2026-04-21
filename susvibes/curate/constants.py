from pathlib import Path

root_dir = Path(__file__).parent.parent.parent
DATASETS_DIR = root_dir / 'datasets'
LOCAL_REPOS_DIR = "/mnt/data2/songwenzhao/projects" #root_dir / 'projects'
curate_log_dir = root_dir / "logs/curate"
COLLECT_LOG_DIR = curate_log_dir / "collect"
ADAPTIVE_GEN_LOG_DIR = curate_log_dir / "adaptive_gen"
ENV_SETUP_LOG_DIR = curate_log_dir / "env_setup"
AGENT_RUN_LOG_DIR = root_dir / "logs/agent_runs"

LOGS_PARSER_MODEL = "o3"

def get_path(name: str, run_id: str = "default") -> Path:
    base = DATASETS_DIR / run_id
    paths = {
        'cve_records': DATASETS_DIR / 'cve_records',
        'processed_dataset': base / 'processed_dataset.jsonl',
        'task_dataset': base / 'task_dataset.jsonl',
        'stats': base / 'stats.json',
        'dataset': base / 'susvibes_dataset.jsonl',
        'examples': base / 'task_examples',
    }
    return paths[name]
