"""ReposVul source — CVE → repo, patches already present per file in `details[]`.

The raw dataset already carries the patches; the expensive step is confirming each fix
PR/commit is still reachable on GitHub (`remotely_active`). That network pass runs once when
building the cache (`ReposVul_<lang>_active.jsonl`, the still-reachable records with their
patch assembled); `records` then reads it, dropping any sha an earlier source already
covered. No per-file rename/create/delete check, so an "adds a file" commit survives here
but dies later in `apply_patch` — see B7 in docs/mine-filters. `_python` in the filename is
the CVE's tag, not the repo's, so language filtering is entirely `code_test_split`'s job.
"""

import requests

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.constants import GITHUB_HEADERS, TARGET_LANG
from susvibes.curate.mine.dedup import KnownSet

REPOSVUL_RAW_PATH = get_dataset_path('raw_cve_records') / f'ReposVul/ReposVul_{TARGET_LANG}.jsonl'
REPOSVUL_CACHE_PATH = get_dataset_path('raw_cve_records') / f'ReposVul/ReposVul_{TARGET_LANG}_active.jsonl'


class ReposVulHandler:
    name = "ReposVul"
    raw_path = REPOSVUL_RAW_PATH
    cache_path = REPOSVUL_CACHE_PATH
    force = False

    @classmethod
    def remotely_active(cls, data_record, max_retries=3) -> bool:
        diff_url = data_record['html_url'] + '.patch'
        while max_retries > 0:
            max_retries -= 1
            try:
                r = requests.get(diff_url, headers=GITHUB_HEADERS, allow_redirects=True, timeout=10)
                if r.status_code == 200:
                    return True
            except requests.RequestException as e:
                continue
        return False

    @classmethod
    def _build_cache(cls):
        """Keep still-reachable records, assemble each patch from details[], and save."""
        dataset = list(filter(cls.remotely_active, load_file(cls.raw_path)))
        for data_record in dataset:
            data_record["patch"] = {}
            for file_change in data_record["details"]:
                data_record["patch"][file_change["file_name"]] = file_change["patch"]
            data_record["cwe_ids"] = data_record.pop("cwe_id")
        cls.cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(dataset, cls.cache_path)

    @classmethod
    def records(cls, known: KnownSet):
        if cls.force or not cls.cache_path.exists():
            cls._build_cache()
        return [record for record in load_file(cls.cache_path)
            if not known.has_sha(record["commit_id"])]
