"""ReposVul source — CVE → repo, patches already present per file in `details[]`.

Unlike Morefixes this reader keeps its own year filter (`is_recent`) and confirms the fix
PR/commit is still reachable on GitHub (`remotely_active`, cached). It does no per-file
rename/create/delete check, so an "adds a file" commit survives here but dies later in
`apply_patch` — see B7 in docs/mine-filters. `_python` in the filename is the CVE's tag,
not the repo's, so language filtering is entirely `code_test_split`'s job.
"""

import json
import requests

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file
from susvibes.curate.mine.constants import GITHUB_HEADERS, TARGET_LANG, RECENT_YR_CUTOFF
from susvibes.curate.mine.dedup import KnownSet

RAW_REPOSVUL_DATASET_PATH = get_dataset_path('cve_records') / f'ReposVul/ReposVul_{TARGET_LANG}.jsonl'
# Cache of remote PR-status lookups; lives with the ReposVul dataset it caches.
LOG_REMOTE_STATUS_CACHE_PATH = get_dataset_path('cve_records') / "ReposVul" / "remote_status_cache.json"


def is_recent(data_record):
    return int(data_record['cve_id'].split('-')[1]) >= RECENT_YR_CUTOFF


class ReposVulHandler:
    name = "ReposVul"
    dataset_path = RAW_REPOSVUL_DATASET_PATH
    cached_remote_status = None

    @classmethod
    def _load_cache(cls):
        if cls.cached_remote_status is not None:
            return
        cache_file = LOG_REMOTE_STATUS_CACHE_PATH
        if cache_file.exists():
            cls.cached_remote_status = json.loads(cache_file.read_text())
        else:
            cls.cached_remote_status = {}

    @classmethod
    def _save_cache(cls):
        LOG_REMOTE_STATUS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_REMOTE_STATUS_CACHE_PATH.write_text(json.dumps(cls.cached_remote_status))

    @classmethod
    def remotely_active(cls, data_record, max_retries=3) -> bool:
        cls._load_cache()
        diff_url = data_record['html_url'] + '.patch'
        if diff_url in cls.cached_remote_status:
            return cls.cached_remote_status[diff_url]
        while max_retries > 0:
            max_retries -= 1
            try:
                r = requests.get(diff_url, headers=GITHUB_HEADERS, allow_redirects=True, timeout=10)
                if r.status_code == 200:
                    cls.cached_remote_status[diff_url] = True
                    cls._save_cache()
                    return True
            except requests.RequestException as e:
                continue
        cls.cached_remote_status[diff_url] = False
        cls._save_cache()
        return False

    @classmethod
    def records(cls, known: KnownSet):
        dataset = load_file(cls.dataset_path)
        dataset_filtered = list(filter(cls.remotely_active, filter(is_recent, dataset)))
        for data_record in dataset_filtered:
            data_record["patch"] = {}
            for file_change in data_record["details"]:
                data_record["patch"][file_change["file_name"]] = file_change["patch"]
            data_record["cwe_ids"] = data_record.pop("cwe_id")
        return dataset_filtered
