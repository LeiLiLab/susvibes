import os
import uuid
import shutil
import subprocess
import threading
import requests
import docker
import docker.errors
from pathlib import Path
from contextlib import contextmanager
from textwrap import dedent
from susvibes.utils import save_file, touched_files
from susvibes.constants import DOCKERHUB_USERNAME

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

def get_repo_dir(project, root_dir):
    """Get the local directory of a GitHub repository ("owner/repo")."""
    root_dir = Path(root_dir)
    repo_name = project.split("/", 1)[1]
    return root_dir / repo_name

def get_repo_size(project) -> int | None:
    """Return repo size in KB via GitHub API, or None on failure."""
    try:
        r = requests.get(f"https://api.github.com/repos/{project}", timeout=10)
        if r.status_code == 200:
            return r.json().get("size", 0)
    except requests.RequestException:
        pass
    return None

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

def rollback(repo_dir, base_commit, security_patch, test_patch=None):
    reset_to_commit(repo_dir, base_commit)
    apply_patch(repo_dir, security_patch, reverse=True)
    if test_patch:
        apply_patch(repo_dir, test_patch, reverse=True)
    rollback_commit = commit_changes(repo_dir, f"Rollback at {base_commit}")
    return rollback_commit

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

def count_patch_additions_deletions(patch):
    """Count added and deleted lines in a patch string."""
    additions, deletions = 0, 0
    for line in patch.splitlines():
        if line.startswith('+++ ') or line.startswith('--- '):
            continue
        if line.startswith('+'):
            additions += 1
        elif line.startswith('-'):
            deletions += 1
    return additions, deletions

def get_on_hub_image_name(
    instance_id: str,
    username: str = DOCKERHUB_USERNAME
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
            
def display_task(data_record, examples_path: Path):
    META_INFO_TEMPLATE = dedent("""
    # Meta Information\n
    Project: {project}\n
    Vulnerability fix commit: [Github Page]({info_page})\n
    Security issue identifier: {cve_id}\n
    Vulnerability type: {cwes}\n
    """)
    PATCH_TEMPLATE = """```diff\n\n{mask_patch}\n```"""
    
    task_dir = examples_path / data_record["instance_id"]
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