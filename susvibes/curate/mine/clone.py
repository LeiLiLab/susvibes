"""The mining stage's clone tier — every git clone the pipeline makes.

One primitive, `clone_into`, does the cloning; the `*_clone` functions each hand back the path to
a ready clone of one kind, making it only if it isn't there yet:

- `full_clone` — the full working-tree clone the funnel needs to reverse/forward-apply a patch
  (`validate_patches`) and that every later stage builds on.
- `blobless_clone` — the finder's cheap tier (S4): bare and `--filter=blob:none`, so it carries
  the full commit history without the file blobs or a work tree, which is all the fact-finder
  reads (git log/show/grep over history, never a checkout). These live under `LOCAL_REPOS_DIR`
  too, in `.sv.blobless/` — one configured root holds every clone, and the dot keeps them apart
  from the `owner__repo` full clones beside them.
- `blobless_clone` — also what a read-only investigation agent gets: the only clone no other
  stage writes to.

This layer owns every git write: clones are serialized per-repo by RepoLocks so concurrent finder
threads never clone one repo twice; the agents themselves only read (no fetch/checkout/reset).
"""

import shutil
import subprocess
from pathlib import Path

from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.curate.utils import run, get_repo_dir, is_git_repo, RepoLocks

BLOBLESS_DIR = Path(LOCAL_REPOS_DIR) / ".sv.blobless"    # a dotted sibling of the owner__repo clones
CLONE_RETRIES = 3


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


def clone_into(project, dest, *flags):
    """Clone project ("owner/repo") into dest, retrying from a clean slate — a clone that died
    partway leaves a half-written tree, so each attempt starts by removing whatever is there.
    Returns dest, or raises the last failure once the retries are spent."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{project}.git"
    for attempt in range(CLONE_RETRIES):
        try:
            if dest.exists():
                shutil.rmtree(dest)
            run(["git", "clone", *flags, repo_url, str(dest)])
            return dest
        except subprocess.SubprocessError:
            if attempt == CLONE_RETRIES - 1:
                raise


def full_clone(project, root_dir, force=False):
    """The full working-tree clone of project ("owner/repo") under root_dir, as the funnel and
    every later stage need it. Reuses an existing clone unless `force`; returns the clone path."""
    dest = get_repo_dir(project, root_dir)
    if is_git_repo(dest) and not force:
        return dest
    return clone_into(project, dest)


def blobless_clone(project):
    """The bare `--filter=blob:none` clone of project ("owner/repo") under BLOBLESS_DIR — full
    commit graph and trees, no file blobs (fetched lazily on access), no work tree. This is what a
    read-only investigation agent reads: no work tree to write into, and the one clone no other
    stage resets under it while the agent spends minutes there. Reuses an existing clone, under the
    lock so concurrent threads clone it once. Returns the clone path, or None if the clone failed.
    """
    with RepoLocks.locked(project):
        dest = get_repo_dir(project, BLOBLESS_DIR)
        if is_bare_repo(dest):
            return dest
        try:
            return clone_into(project, dest, "--bare", "--filter=blob:none")
        except subprocess.SubprocessError:
            return None


def fetch_commit(repo_dir, commit):
    """Fetch one commit by sha into an existing clone. A clone copies only what the repo's refs
    reach, so a fix commit whose branch was deleted or whose PR was squash-merged is missing even
    from a current clone (`git clone` never fetches `refs/pull/*`) — GitHub still serves it on
    demand. Raises if the commit isn't there either."""
    run(["git", "fetch", "--no-tags", "origin", commit], cwd=repo_dir)


