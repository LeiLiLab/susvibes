"""Running cross-source dedup for the mining stage.

A single `KnownSet` is threaded through the sources (Morefixes, ReposVul, and — once
added — OSV and the discovery sources M2/M3). It holds the CVE ids and full-length fix
commits already accepted, so the assembler collapses mirror commits (the same fix reached
under a different project/CVE) to one record. This catches the mirror duplicates that the
old `(project, base_commit)` `instance_id` key misses, and lets a source skip
already-covered work before its expensive step.

Dedup is on the **full 40-char commit** only — sources must resolve short commits to full
before adding; a 7-char prefix collides across unrelated repos.
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

    def has_cve(self, cve_id: str) -> bool:
        return cve_id in self.cve_ids

    def has_commit(self, commit: str) -> bool:
        return normalize_commit(commit) in self.commits

    def add(self, record: dict) -> None:
        """Record a CVE id + its base_commit as covered. `base_commit` should be a full
        commit (resolve upstream); a short one here silently weakens the commit check."""
        self.cve_ids.add(record["cve_id"])
        self.commits.add(normalize_commit(record["base_commit"]))

    def seed(self, records) -> None:
        for record in records:
            self.add(record)
