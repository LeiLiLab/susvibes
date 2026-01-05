
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent
DATASETS_DIR = ROOT_DIR / 'datasets'

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
