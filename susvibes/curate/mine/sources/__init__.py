"""Sources — anything that yields raw CVE records for the mining funnel.

Uniform interface so `core.py` treats every source identically (records → code_test_split
→ dedup → funnel). Each source materialises its expensive network work (patch fetch /
liveness check) into a cached dataset (`cache_path`) that `records` reads; the cache is
(re)built on first use or `--force`, so a normal run does no network. Discovery sources
(NVD — added by M3) run the finder; same output shape either way.
"""

from pathlib import Path
from typing import Iterator, Protocol

from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources.morefixes import MorefixesHandler
from susvibes.curate.mine.sources.reposvul import ReposVulHandler
from susvibes.curate.mine.sources.osv import OSVSource


class Source(Protocol):
    name: str
    cache_path: Path              # the materialised dataset this source reads
    force: bool                   # set by --force to rebuild the cache before reading

    def records(self, known: KnownSet) -> Iterator[dict]:
        """Yield raw CVE records (`{cve_id, cwe_ids, commit_id, patch:{path:hunk}, owner+repo
        or project, ...}`) for `code_test_split`, read from `cache_path` — (re)built via the
        source's network work when the cache is missing or `force` is set. The sha dedup runs
        at assembly on the resulting base_commit; a source may also use `known` to skip
        already-covered work (OSV re-applies it before yielding)."""
        ...


# Order is load-bearing: on a same-sha collision the earlier source's record is kept, and
# the deterministic OSV source runs last so it dedups against Morefixes + ReposVul.
SOURCES = [ReposVulHandler, MorefixesHandler, OSVSource]
SOURCE_BY_NAME = {source.__name__: source for source in SOURCES}
