"""Sources — anything that yields raw CVE records for the mining funnel.

Uniform interface so `process.py` treats every source identically (records → code_test_split
→ dedup → funnel). Deterministic sources (Morefixes, ReposVul) read the fix commit straight
from their data; discovery sources (OSV residual, NVD — added by M2/M3) run the finder to
locate it, same output shape either way.
"""

from typing import Iterator, Protocol

from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources.morefixes import MorefixesHandler
from susvibes.curate.mine.sources.reposvul import ReposVulHandler


class Source(Protocol):
    name: str

    def records(self, known: KnownSet) -> Iterator[dict]:
        """Yield raw CVE records (`{cve_id, cwe_ids, commit_id, patch:{path:hunk}, project
        or owner+repo, ...}`) for `code_test_split`. Deterministic sources ignore `known`
        (dedup runs at assembly on the resulting base_commit sha); discovery sources use it
        to skip already-covered work before the finder."""
        ...


# Order is load-bearing: on a same-sha collision the earlier source's record is kept.
SOURCES = [ReposVulHandler, MorefixesHandler]
SOURCE_BY_NAME = {source.__name__: source for source in SOURCES}
