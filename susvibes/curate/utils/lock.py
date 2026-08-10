"""Serialization of the shared local clones, across threads and across processes.

Every stage that needs a repo at some commit works in the one clone under ``LOCAL_REPOS_DIR``, and
so do the agents dispatched to copy it. Whoever mutates a clone holds its lock, so nobody — not
another thread, not a separate program — reads a tree that is halfway through a rollback.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path

from susvibes.curate.constants import LOCAL_REPOS_DIR
from susvibes.curate.utils.common import get_repo_dir

LOCKS_DIR = Path(LOCAL_REPOS_DIR) / ".sv.locks"     # a dotted sibling of the owner__repo clones


class RepoLocks:
    """One advisory lock per project, held while a caller mutates that project's clone.

    `flock` rather than a lock file's existence: the kernel drops it when the holder's descriptor
    closes, so a killed run — or a rebooted machine — leaves nothing to clean up. It also covers
    this process's own threads, since separate descriptors on one file still exclude each other.
    Acquiring blocks indefinitely; a deadline would be a policy this has never had.
    """

    @staticmethod
    def get_lock_path(repo_dir) -> Path:
        """The lock file for a clone, named after its directory — which get_repo_dir keeps unique
        per "owner/repo", so a project's full clone and its blobless one share one lock, as the one
        resource they are."""
        return LOCKS_DIR / f"{Path(repo_dir).name}.lock"

    @classmethod
    @contextmanager
    def locked(cls, project: str):
        lock_path = cls.get_lock_path(get_repo_dir(project, LOCAL_REPOS_DIR))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            yield
