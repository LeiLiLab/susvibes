import os
import re
import sys
import json
import yaml
import logging
import tempfile
from tqdm import tqdm
from pathlib import Path

from susvibes.core.constants import ARCH, DOCKERHUB_USERNAME, ENV_SPECS_DIR, ENV_SPEC_FILE_NAMES
from susvibes.env_specs import GEN_SEC_TEST_CMD


def get_image_name(local_name: str, username: str = DOCKERHUB_USERNAME) -> str:
    return f"{username}/susvibes.{ARCH}.{local_name.replace('__', '_').lower()}"


def load_file(file_path: Path | str):
    """Load files based on their extension."""
    file_path = Path(file_path)
    if file_path.suffix == ".json":
        return json.loads(file_path.read_text())
    elif file_path.suffix == ".jsonl":
        return [json.loads(line) for line in file_path.read_text().split("\n") if line.strip()]
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

def push_dataset_to_hub(records, repo_id, filename, private=False, commit_message=None):
    """Upload records as a JSONL file to a HuggingFace dataset repo without writing
    into the local datasets/ tree."""
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is not set in the environment.")
    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / filename
        save_file(records, tmp)
        api.upload_file(
            path_or_fileobj=str(tmp),
            path_in_repo=filename,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_message or f"Update {filename}",
        )
    return f"{repo_id}/{filename}"

def get_env_specs(run_id: str = "default", required_fields: tuple = tuple(ENV_SPEC_FILE_NAMES)) -> dict:
    """Per-instance env specs {instance_id: {dev_tools, dockerfile, logs_handler}}, each carrying
    whichever of the three files list it. required_fields gates the result to instances that have
    every listed field (a presence filter, not a field selector); the default requires all three."""
    specs = {}
    for field in ENV_SPEC_FILE_NAMES:
        path = ENV_SPECS_DIR / run_id / ENV_SPEC_FILE_NAMES[field]
        file_map = load_file(path) if path.exists() else {}
        for instance_id, value in file_map.items():
            specs.setdefault(instance_id, {})[field] = value
    return {iid: spec for iid, spec in specs.items()
        if all(field in spec for field in required_fields)}

def save_env_specs(field: str, env_specs: dict, run_id: str = "default") -> Path:
    """Persist one env-spec file from per-instance specs, extracting that field."""
    data = {iid: spec[field] for iid, spec in env_specs.items() if field in spec}
    path = ENV_SPECS_DIR / run_id / ENV_SPEC_FILE_NAMES[field]
    path.parent.mkdir(parents=True, exist_ok=True)
    save_file(data, path)
    return path

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
    handle_tqdm: bool = False,
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
            stream_handler = logging.StreamHandler() if not handle_tqdm else TqdmStreamHandler()
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

def touched_files(patch, side="post"):
    """Files touched by a patch, as a set of paths.

    side="post" -> new-side names (the ``+++ b/`` lines, the post-patch tree);
    side="pre"  -> old-side names (the ``--- a/`` lines, the pre-patch tree).
    A ``/dev/null`` side (a file added or deleted by the patch) contributes nothing,
    so "post" yields exactly the files present after the patch and "pre" exactly
    those present before it."""
    marker, prefix = ("+++ ", "b/") if side == "post" else ("--- ", "a/")
    file_paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(marker):
            path = line[4:].split('\t', 1)[0]
            if path == "/dev/null":
                continue
            if path.startswith(prefix):
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

class Route:
    """Map an instance's flags and a run name to how that run executes — its container command and
    its logs-handler kind. A synthesized-sec instance (flags["gen_test"]) runs sectests.sh and reads
    the gen_sec results on its generated-test run — eval's "sec" run or validate's "*_gen_test" runs;
    every other run uses the image's default command and count parsing."""

    @staticmethod
    def _gen_test(flags: dict, run_name: str) -> bool:
        return flags.get("gen_test", False) and (run_name == "sec" or run_name.endswith("_gen_test"))

    @staticmethod
    def route_test_cmd(flags: dict, run_name: str) -> list | None:
        return GEN_SEC_TEST_CMD if Route._gen_test(flags, run_name) else None

    @staticmethod
    def route_logs_kind(flags: dict, run_name: str) -> str:
        return "gen_sec" if Route._gen_test(flags, run_name) else "count"