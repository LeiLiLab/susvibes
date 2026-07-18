"""S4 clone tier for the finder — a blobless-bare clone carries the full commit history without
the file blobs or a work tree, which is all the fact-finder reads (git log/show/grep over history,
never a checkout). If the repo is already fully cloned under LOCAL_REPOS_DIR (a shared curate
clone), the finder reads that instead. Survivors — the CVEs the finder pinned a commit for — get
the full working-tree clone later, at clone_repos_and_verify_fixes (apply-verify needs the tree).

This layer owns every git write: clones are serialized per-repo by RepoLocks so concurrent finder
threads never clone one repo twice; the agents themselves only read (no fetch/checkout/reset).
"""

import shutil
import subprocess
from pathlib import Path

from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.curate.utils import run, get_repo_dir, is_git_repo, RepoLocks

BLOBLESS_DIR = Path(LOCAL_REPOS_DIR).parent / "blobless"


def is_bare_repo(repo_dir) -> bool:
    """Whether repo_dir is a bare git repository (the blobless clones carry no work tree)."""
    repo_dir = Path(repo_dir)
    if not repo_dir.is_dir():
        return False
    try:
        result = run(["git", "rev-parse", "--is-bare-repository"], cwd=repo_dir)
        return result.stdout.strip() == "true"
    except subprocess.SubprocessError:
        return False


def bare_blobless(project, max_retries=3):
    """Clone project ("owner/repo") bare with --filter=blob:none into BLOBLESS_DIR — full commit
    graph and trees, no file blobs (fetched lazily on access), no work tree. Reuses an existing
    clone; returns the clone path."""
    dest = get_repo_dir(project, BLOBLESS_DIR)
    if is_bare_repo(dest):
        return dest
    BLOBLESS_DIR.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{project}.git"
    while max_retries > 0:
        max_retries -= 1
        try:
            if dest.exists():
                shutil.rmtree(dest)
            run(["git", "clone", "--bare", "--filter=blob:none", repo_url, str(dest)])
            return dest
        except subprocess.SubprocessError as e:
            if not max_retries:
                raise e


def finder_clone(project):
    """A history-carrying clone for the finder: the existing full clone if there is one, otherwise
    a blobless-bare one. Serialized per-repo so concurrent finder threads clone the repo once.
    Returns the clone path, or None if the clone failed."""
    if is_git_repo(get_repo_dir(project, LOCAL_REPOS_DIR)):
        return get_repo_dir(project, LOCAL_REPOS_DIR)
    with RepoLocks.locked(project):
        try:
            return bare_blobless(project)
        except subprocess.SubprocessError:
            return None
