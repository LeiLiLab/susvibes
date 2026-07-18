"""Running cross-source dedup for the mining stage.

A single `KnownSet` is threaded through the sources (Morefixes, ReposVul, and — once
added — OSV and the discovery sources M2/M3). It holds the CVE ids and full-length
commit shas already accepted, so the assembler collapses mirror commits (the same fix
reached under a different project/CVE) to one record. This catches the mirror
duplicates that the old `(project, base_commit)` `instance_id` key misses, and lets a
source skip already-covered work before its expensive step.

Dedup is on the **full 40-char sha** only — sources must resolve short shas to full
before adding; a 7-char prefix collides across unrelated repos.
"""


def normalize_sha(sha: str) -> str:
    """Lowercase + strip a commit sha for comparison."""
    return sha.strip().lower()


class KnownSet:
    """CVE ids and full-length commit shas already accepted into the dataset.

    Not thread-safe: mutate it from the single assembler loop, not from finder workers.
    """

    def __init__(self) -> None:
        self.cve_ids: set[str] = set()
        self.shas: set[str] = set()

    def has_cve(self, cve_id: str) -> bool:
        return cve_id in self.cve_ids

    def has_sha(self, sha: str) -> bool:
        return normalize_sha(sha) in self.shas

    def add(self, record: dict) -> None:
        """Record a CVE id + its base_commit as covered. `base_commit` should be a
        full sha (resolve upstream); a short sha here silently weakens the sha check."""
        self.cve_ids.add(record["cve_id"])
        self.shas.add(normalize_sha(record["base_commit"]))

    def seed(self, records) -> None:
        for record in records:
            self.add(record)
