
from pathlib import Path

root_dir = Path(__file__).parent.parent.parent
DATASETS_DIR = root_dir / 'datasets'
LOCAL_REPOS_DIR = root_dir / 'projects'

LOGS_PARSER_MODEL = "o3"

def get_path(name: str, subset: str = None) -> Path:
    base = DATASETS_DIR / subset if subset else DATASETS_DIR
    paths = {
        'cve_records': base / 'cve_records',
        'processed_dataset': base / 'processed_dataset.jsonl',
        'task_dataset': base / 'task_dataset.jsonl',
        'stats': base / 'stats.json',
        'dataset': base / 'susvibes_dataset.jsonl',
        'examples': base / 'task_examples',
    }
    return paths[name]
