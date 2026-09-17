"""Running cross-source dedup for the mining stage.

A single `KnownSet` is threaded through the sources (Morefixes, ReposVul, OSV, and the
discovery sources M2/M3). It holds the CVE ids and full-length fix commits already accepted,
so the assembler keeps the first record of a fix and collapses the rest. This catches the
duplicates that the old `(project, base_commit)` `instance_id` key misses, and lets a source
skip already-covered work before its expensive step.

Both keys are needed, and neither subsumes the other:

- **commit** catches a mirror — one fix reached under a different project or renumbered CVE.
  Matched on the **full 40-char** commit; sources must resolve short commits before adding,
  since a 7-char prefix collides across unrelated repos.
- **CVE** catches one vulnerability spread over several commits — a fix backported across
  maintenance branches, where every sha is genuinely different so the commit key sees nothing.
  Morefixes drops these upstream (`len(commits) == 1` in its crawl); ReposVul emits one record
  per commit, so without this key its backports enter as near-duplicate instances.
"""


def normalize_commit(commit: str) -> str:
    """Lowercase + strip a commit for comparison."""
    return commit.strip().lower()


class KnownSet:
    """CVE ids and full-length fix commits already accepted into the dataset.

    Not thread-safe: mutate it from the single assembler loop, not from finder workers.
    """

    def __init__(self) -> None:
        self.cve_ids: set[str] = set()
        self.commits: set[str] = set()

    def has(self, cve_id: str, commit: str) -> bool:
        """Whether this fix is already covered under either key — the same commit, or the same
        CVE reached by a different commit."""
        return cve_id in self.cve_ids or normalize_commit(commit) in self.commits

    def add(self, record: dict) -> None:
        """Record a CVE id + its base_commit as covered. `base_commit` should be a full
        commit (resolve upstream); a short one here silently weakens the commit check."""
        self.cve_ids.add(record["cve_id"])
        self.commits.add(normalize_commit(record["base_commit"]))

    def seed(self, records) -> None:
        for record in records:
            self.add(record)
