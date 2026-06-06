import os
import re
import uuid
import shutil
import tempfile
import subprocess
import threading
from pathlib import Path
from contextlib import contextmanager
from textwrap import dedent
from huggingface_hub import HfApi
from susvibes.utils import save_file, touched_files
from susvibes.curate.constants import TaskArtifact, PATCH_TEMPLATE
from susvibes.env_specs import DOCKERFILE_PATTERN


def extract_repo_test_cmd(dockerfile: str) -> str:
    """Extract the repo test command from the CMD line of an env_spec dockerfile."""
    m = re.search(DOCKERFILE_PATTERN, dockerfile, re.MULTILINE | re.DOTALL)
    _, _, _, _, cmd_stm = m.groups()
    return cmd_stm.strip()[len("CMD"):].strip()

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



def push_dataset_to_hub(records, repo_id, filename, private=False, commit_message=None):
    """Upload records as a JSONL file to a HuggingFace dataset repo without writing
    into the local datasets/ tree."""
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


class RepoLocks:
    _locks = {}
    _guard = threading.Lock()

    @classmethod
    def get_lock(cls, project: str) -> threading.Lock:
        # The shared resource is the local clone dir, which get_repo_dir keys by
        # repo name only (not owner). Lock on the same key so that "a/foo" and
        # "b/foo" — which map to the same dir — serialize against each other.
        repo_name = project.split("/", 1)[1] if "/" in project else project
        with cls._guard:
            lock = cls._locks.get(repo_name)
            if lock is None:
                lock = threading.Lock()
                cls._locks[repo_name] = lock
            return lock

    @classmethod
    @contextmanager
    def locked(cls, project: str):
        lock = cls.get_lock(project)
        with lock:
            yield
            
def dump_task(data_record, examples_path: Path):
    README_TEMPLATE = dedent("""\
    # Meta Information

    Project: {project}

    GitHub vuln. fix commit page: {info_page}

    Security issue identifier: {cve_id}

    Vulnerability type: {cwes}

    # Folder Contents

    - `{feature_golden_file}` — diff revealing the secure feature implementation
    - `{feature_mask_file}` — diff redacting feature lines from the vulnerable code
    - `{security_fix_file}` — security fix patch fixing the security of the feature
    - `{problem_statement_file}` — task description shown to the agent
    """)

    task_dir = examples_path / data_record["instance_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    if "golden_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=data_record["golden_patch"]),
            task_dir / TaskArtifact.FEATURE_GOLDEN)
    if "mask_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=data_record["mask_patch"]),
            task_dir / TaskArtifact.FEATURE_MASK)
    if "security_patch" in data_record:
        save_file(PATCH_TEMPLATE.format(patch=data_record["security_patch"]),
            task_dir / TaskArtifact.SECURITY_FIX)

    save_file(data_record["problem_statement"], task_dir / TaskArtifact.PROBLEM_STATEMENT)

    readme = README_TEMPLATE.format(
        project=data_record["project"],
        info_page=data_record["info_page"],
        cve_id=data_record["cve_id"],
        cwes=", ".join(data_record["cwe_ids"]),
        feature_golden_file=TaskArtifact.FEATURE_GOLDEN,
        feature_mask_file=TaskArtifact.FEATURE_MASK,
        security_fix_file=TaskArtifact.SECURITY_FIX,
        problem_statement_file=TaskArtifact.PROBLEM_STATEMENT,
    )
    save_file(readme, task_dir / TaskArtifact.README)

def reverse_patch(patch: str) -> str:
    """Reverse a unified diff patch (swap roles of old/new sides).

    Handles:
    - +/- content lines
    - @@ hunk headers (swap old/new ranges)
    - --- / +++ file headers (swap header types and a/<->b/ prefixes)
    - diff --git a/X b/Y header (swap a/X and b/Y)
    - deleted file mode <-> new file mode
    - old mode <-> new mode (chmod)
    - rename from <-> rename to
    - copy from <-> copy to
    - index <sha1>..<sha2> (swap sha1 and sha2)
    """
    if not patch:
        return patch

    def swap_ab(path: str) -> str:
        if path.startswith("a/"):
            return "b/" + path[2:]
        if path.startswith("b/"):
            return "a/" + path[2:]
        return path

    lines = patch.split("\n")
    out = []
    i = 0
    in_hunk = False
    while i < len(lines):
        line = lines[i]

        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        if m:
            in_hunk = False
            out.append(f"diff --git a/{m.group(2)} b/{m.group(1)}")
            i += 1
            continue

        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            in_hunk = False
            old = line[4:]
            new = lines[i + 1][4:]
            out.append(f"--- {swap_ab(new)}")
            out.append(f"+++ {swap_ab(old)}")
            i += 2
            continue

        if line.startswith("@@"):
            in_hunk = True
            m = re.match(r"^@@ -(\S+) \+(\S+) @@(.*)$", line)
            if m:
                out.append(f"@@ -{m.group(2)} +{m.group(1)} @@{m.group(3)}")
            else:
                out.append(line)
            i += 1
            continue

        if not in_hunk:
            if line.startswith("deleted file mode"):
                out.append(line.replace("deleted file mode", "new file mode", 1))
                i += 1
                continue
            if line.startswith("new file mode"):
                out.append(line.replace("new file mode", "deleted file mode", 1))
                i += 1
                continue

            if line.startswith("old mode ") and i + 1 < len(lines) and lines[i + 1].startswith("new mode "):
                old_mode = line[len("old mode "):]
                new_mode = lines[i + 1][len("new mode "):]
                out.append(f"old mode {new_mode}")
                out.append(f"new mode {old_mode}")
                i += 2
                continue

            if line.startswith("rename from ") and i + 1 < len(lines) and lines[i + 1].startswith("rename to "):
                from_path = line[len("rename from "):]
                to_path = lines[i + 1][len("rename to "):]
                out.append(f"rename from {to_path}")
                out.append(f"rename to {from_path}")
                i += 2
                continue
            if line.startswith("copy from ") and i + 1 < len(lines) and lines[i + 1].startswith("copy to "):
                from_path = line[len("copy from "):]
                to_path = lines[i + 1][len("copy to "):]
                out.append(f"copy from {to_path}")
                out.append(f"copy to {from_path}")
                i += 2
                continue

            m = re.match(r"^index (\S+)\.\.(\S+)(.*)$", line)
            if m:
                out.append(f"index {m.group(2)}..{m.group(1)}{m.group(3)}")
                i += 1
                continue

        if in_hunk:
            if line.startswith("-") and not line.startswith("--- "):
                out.append("+" + line[1:])
            elif line.startswith("+") and not line.startswith("+++ "):
                out.append("-" + line[1:])
            else:
                out.append(line)
        else:
            out.append(line)

        i += 1

    return "\n".join(out)

