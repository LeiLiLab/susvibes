import os
import re
import sys
import json
import yaml
import uuid
import shutil
import subprocess
import threading
import docker
import docker.errors
import logging
from tqdm import tqdm
from pathlib import Path
from contextlib import contextmanager
from textwrap import dedent

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
        file_path.write_text(json.dumps(data, ensure_ascii=False))
    elif file_path.suffix == ".jsonl":
        lines = [json.dumps(line, ensure_ascii=False) + "\n" for line in data]
        file_path.write_text("".join(lines))
    elif file_path.suffix == ".yaml":
        with file_path.open("w") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    else:
        file_path.write_text(data)

def run(cmd, cwd=None, capture_output=True, text=True, check=True, **kwargs):
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=capture_output,
                                text=text, check=check, **kwargs)
    except subprocess.CalledProcessError as e:
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        raise subprocess.SubprocessError(
            f"Command '{cmd_str}' failed with return code {e.returncode}.\n"
            f"Output: {e.stdout}\n"
            f"Error: {e.stderr}\n"
        )
    return proc

def is_git_repo(repo_dir):
    """Check if the given directory is a Git repository."""
    repo_dir = Path(repo_dir)
    if not repo_dir.is_dir():
        return False
    if (repo_dir / ".git").is_dir():
        return True
    try:
        result = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        return result.stdout.strip() == "true"
    except subprocess.SubprocessError:
        return False

def is_clean_git_repo(repo_dir):
    """Determine if a Git repository has no uncommitted changes (including untracked files)."""
    repo_dir = Path(repo_dir)
    if not is_git_repo(repo_dir):
        raise FileNotFoundError(f"Project directory {repo_dir} is not a Git repository.")
    if run(["git", "status", "--porcelain"], cwd=repo_dir).stdout:
        return False
    return True 

def get_instance_id(project, base_commit):
    """Generate a unique instance ID based on the project and base commit."""
    project = project.replace("/", "__")
    return f"{project}_{base_commit}"

def parse_instance_id(instance_id):
    """Parse the instance id to extract project and base commit."""
    project_part, _, base_commit = instance_id.rpartition("_")
    project = project_part.replace("__", "/")
    return project, base_commit

def get_repo_dir(project, root_dir):
    """Get the local directory of a GitHub repository ("owner/repo")."""
    root_dir = Path(root_dir)
    repo_name = project.split("/", 1)[1]
    return root_dir / repo_name

def clone_github_repo(project, root_dir, force=False, max_retries=3, timeout=None):
    """Clone a GitHub repository ("owner/repo") into the root directory."""
    root_dir = Path(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{project}.git"
    dest = get_repo_dir(project, root_dir)
    if is_git_repo(dest) and not force:
        return dest
    while max_retries > 0:
        max_retries -= 1
        try:
            if dest.exists():
                shutil.rmtree(dest)
            run(["git", "clone", repo_url, str(dest)], timeout=timeout)
        except subprocess.SubprocessError as e:
            if not max_retries:
                raise e
    return dest

def apply_patch(repo_dir, patch, patch_file_name=None, reverse=False):
    """Apply a single patch string to the Git repository by writing it to a patch file."""
    repo_dir = Path(repo_dir)
    if not is_git_repo(repo_dir):
        raise FileNotFoundError(f"Project directory {repo_dir} is not a Git repository.")
    if not patch_file_name:
        patch_file_name = "tmp.patch"
        save_patch_file = False
    else:
        save_patch_file = True
    patch_path = repo_dir / patch_file_name
    patch_path.write_text(patch)
    extra_args = ["-c", "core.fileMode=false"]
    cmd = ["git", *extra_args, "apply", "--ignore-space-change"] # prevent CRLF inconsistency
    if reverse:
        cmd.append("--reverse")
    cmd.append(patch_file_name)
    run(cmd, cwd=repo_dir)
    if not save_patch_file:
        patch_path.unlink()

def get_diff_patch(repo_dir: str, base_commit: str, target_commit: str) -> str:
    """Get the diff patch between two commits in the Git repository."""
    repo_dir = Path(repo_dir)
    if not is_git_repo(repo_dir):
        raise FileNotFoundError(f"Project directory {repo_dir} is not a Git repository.")
    cmd = ["git", "diff", base_commit, target_commit, "--patch"]
    proc = run(cmd, cwd=repo_dir)
    return proc.stdout

def reset_to_commit(repo_dir, commit, new_branch=True):
    """Hard-reset the repository to a specific commit and clean untracked files."""
    repo_dir = Path(repo_dir)
    if not is_git_repo(repo_dir):
        raise FileNotFoundError(f"Project directory {repo_dir} is not a Git repository.")
    extra_args = ["-c", "core.precomposeunicode=false"]
    run(["git", "reset", "--hard", commit], cwd=repo_dir) 
    run(["git", *extra_args, "clean", "-fdx"], cwd=repo_dir)
    if new_branch:
        run(["git", "checkout", "-b", f"susvibes-{uuid.uuid4()}"], cwd=repo_dir)

def commit_changes(repo_dir, message):
    """
    Stage all changes and commit with the provided message.
    Returns the new commit's SHA.
    """
    repo_dir = Path(repo_dir)
    if not is_git_repo(repo_dir):
        raise FileNotFoundError(f"Project directory {repo_dir} is not a Git repository.")
    extra_args = ["-c", "core.precomposeunicode=false"]
    run(["git", *extra_args,  "add", "--all"], cwd=repo_dir)
    run(["git", *extra_args, "commit", "-m", f"[susvibes] {message}"], cwd=repo_dir)
    commit_sha = run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    return commit_sha

def rollback(repo_dir, base_commit, security_patch, test_patch):
    reset_to_commit(repo_dir, base_commit)
    apply_patch(repo_dir, security_patch, reverse=True)
    apply_patch(repo_dir, test_patch, reverse=True)
    rollback_commit = commit_changes(repo_dir, f"Rollback at {base_commit}")
    return rollback_commit

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

def len_patch(patch):
    """Count the number of changed files and lines in a patch string."""
    num_lines = 0
    num_files = len(touched_files(patch))

    for line in patch.splitlines():
        if line.startswith('+++ ') or line.startswith('--- '):
            continue
        if (line.startswith('+') or line.startswith('-')):
            num_lines += 1
    return num_files, num_lines

def filter_patch(patch, targets, exclude=False):
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

def get_on_hub_image_name(
    instance_id: str,
    username: str = "songwen6968"
):
    arch = os.uname().machine
    escaped = instance_id.replace("__", "_")
    return f"{username}/susvibes.{arch}.eval_{escaped.lower()}"

def push_image_to_hub(image_name, max_retries=3):
    """Push image to Docker Hub with a specified name."""
    docker_client = docker.from_env()
    for retry in range(max_retries):
        try:
            response = docker_client.images.push(image_name, stream=True, decode=True)
            for chunk in response:
                if any(key in chunk for key in ["error", "denied"]):
                    raise docker.errors.APIError(chunk["error"])
            break
        except docker.errors.APIError as e:
            if retry == max_retries - 1:
                raise

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

def setup_logger(
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
    
class RepoLocks:
    _locks = {}
    _guard = threading.Lock() 

    @classmethod
    def get_lock(cls, project: str) -> threading.Lock:
        with cls._guard:
            lock = cls._locks.get(project)
            if lock is None:
                lock = threading.Lock()
                cls._locks[project] = lock
            return lock

    @classmethod
    @contextmanager
    def locked(cls, project: str):
        lock = cls.get_lock(project)
        with lock:
            yield
            
def display_task(data_record, display_path: Path):
    META_INFO_TEMPLATE = dedent("""
    # Meta Information\n
    Project: {project}\n
    Vulnerability fix commit: [Github Page]({info_page})\n
    Security issue identifier: {cve_id}\n
    Vulnerability type: {cwes}\n
    """)
    PATCH_TEMPLATE = """```diff\n\n{mask_patch}\n```"""
    
    task_dir = display_path / data_record["instance_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    if "golden_patch" in data_record:
        golden_path = task_dir / "golden.md"
        golden = PATCH_TEMPLATE.format(mask_patch=data_record["golden_patch"])
        save_file(golden, golden_path)
    if "mask_patch" in data_record:
        mask_path = task_dir / "mask.md"
        mask = PATCH_TEMPLATE.format(mask_patch=data_record["mask_patch"])
        save_file(mask, mask_path)
    if "security_patch" in data_record:
        security_path = task_dir / "security_fix.md"
        security_fix = PATCH_TEMPLATE.format(mask_patch=data_record["security_patch"])
        save_file(security_fix, security_path)
    
    problem_statement_path = task_dir / "problem_statement.md"
    save_file(data_record["problem_statement"], problem_statement_path)
    
    meta_path = task_dir / "meta_info.md"
    meta_info = META_INFO_TEMPLATE.format(
        project=data_record["project"],
        info_page=data_record["info_page"],
        cve_id=data_record["cve_id"],
        cwes=", ".join(data_record["cwe_ids"])
    )
    save_file(meta_info, meta_path)