import re
import sys
import json
import yaml
import logging
from tqdm import tqdm
from pathlib import Path

from susvibes.constants import ARCH, DOCKERHUB_USERNAME


def get_image_name(local_name: str, username: str = DOCKERHUB_USERNAME) -> str:
    return f"{username}/susvibes.{ARCH}.{local_name.replace('__', '_').lower()}"


def load_file(file_path: Path | str):
    """Load files based on their extension."""
    file_path = Path(file_path)
    if file_path.suffix == ".json":
        return json.loads(file_path.read_text())
    elif file_path.suffix == ".jsonl":
        return [json.loads(line) for line in file_path.read_text().splitlines() if line.strip()]
    elif file_path.suffix == ".yaml":
        return yaml.safe_load(file_path.read_text())
    else:
        return file_path.read_text()

def save_file(data, file_path: Path | str):
    """Save files based on their extension."""
    file_path = Path(file_path)
    if file_path.suffix == ".json":
        file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    elif file_path.suffix == ".jsonl":
        lines = [json.dumps(line, ensure_ascii=False) + "\n" for line in data]
        file_path.write_text("".join(lines))
    elif file_path.suffix == ".yaml":
        with file_path.open("w") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    else:
        file_path.write_text(data)
        
class TqdmStreamHandler(logging.StreamHandler):
    def __init__(self, stream=None):
        super().__init__(stream or sys.stderr) 
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg, file=self.stream) 
            self.flush()
        except Exception:
            self.handleError(record)
        
def confirm_overwrite_logs(log_dir: Path):
    log_dir = Path(log_dir)
    if not log_dir.exists():
        return True
    log_files = list(log_dir.glob("*.log"))
    non_empty = [f for f in log_files if f.stat().st_size > 0]
    if not non_empty:
        return True
    print(f"Found {len(non_empty)} non-empty log file(s) in {log_dir}:")
    for f in non_empty:
        print(f"  {f.name}")
    answer = input("Overwrite? [Y/n] ").strip().lower()
    if answer not in ('', 'y'):
        return False
    for f in non_empty:
        f.write_text("")
    return True

def setup_logger(
    log_dir: Path,
    log_file_name: str,
    logger_name: str,
    mode: str = "a",
    add_stdout: bool = True,
):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / log_file_name
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file, mode=mode)
        file_handler.setFormatter(logging.Formatter(
            "[%(levelname)s] - %(asctime)s - %(message)s"))
        logger.addHandler(file_handler)
        if add_stdout:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(logging.Formatter(
                "[%(levelname)s] - %(asctime)s - %(message)s"))
            logger.addHandler(stream_handler)
    return logger

def setup_instance_logger(
    log_file: Path,
    logger_name: str,
    instance_id: str,
    mode: str = "w",
    add_stdout: bool = True,
    handle_tqdm: bool = False
):
    def get_short(instance_id):
        parts = instance_id.split("_")
        return "_".join(parts[:-1] + [parts[-1][:7]])

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{logger_name}.{instance_id}")

    handler = logging.FileHandler(log_file, mode=mode)
    formatter = logging.Formatter("[%(levelname)s] - %(asctime)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    setattr(logger, "log_file", log_file)
    if add_stdout:
        handler = logging.StreamHandler() if not handle_tqdm else TqdmStreamHandler()
        formatter = logging.Formatter(
            f"[%(levelname)s] - %(asctime)s - {get_short(instance_id)} - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

def touched_files(patch):
    """Extract the list of files touched by a patch string."""
    file_paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith('+++ '):
            path = line[4:].split('\t', 1)[0]
            if path.startswith('b/'):
                path = path[2:]
            file_paths.add(path)
    return file_paths

def filter_target_files(patch, targets, exclude=False):
    diff_re = re.compile(r'^diff --git a/(.*?) b/(.*?)$')
    out, keep = [], False
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            m = diff_re.match(line)
            if m:
                in_targets = (m.group(1) in targets) or (m.group(2) in targets)
                keep = in_targets ^ exclude
            else:
                keep = False
            if keep:
                out.append(line)
        elif keep:
            out.append(line)
    return "".join(out)

def filter_binary_files(patch):
    """Drop binary diff blocks (e.g. compiled .pyc artifacts swept in by `git add -A`).
    `git apply` cannot apply a binary patch lacking a full index line, and such artifacts
    are never part of a legitimate source fix; dropping them lets the real source hunks
    still apply. A diff block is binary iff it carries a `Binary files ...`/`GIT binary
    patch` marker — an unprefixed header line that cannot appear in a well-formed text
    hunk (hunk lines always carry a +/-/space prefix), so text changes are never touched.
    """
    out, block, is_binary = [], [], False
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if block and not is_binary:
                out.extend(block)
            block, is_binary = [line], False
        elif block:
            block.append(line)
            if line.startswith(("Binary files ", "GIT binary patch")):
                is_binary = True
    if block and not is_binary:
        out.extend(block)
    return "".join(out)

def get_instance_id(project, base_commit):
    """Generate a unique instance ID based on the project and base commit."""
    project = project.replace("/", "__")
    return f"{project}_{base_commit}"

def parse_instance_id(instance_id):
    """Parse the instance id to extract project and base commit."""
    project_part, _, base_commit = instance_id.rpartition("_")
    project = project_part.replace("__", "/")
    return project, base_commit