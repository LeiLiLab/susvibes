"""ReposVul source — CVE → repo, patches already present per file in `details[]`.

The raw dataset already carries the patches; the expensive step is confirming each fix
PR/commit is still reachable on GitHub (`remotely_active`). `_fetch` does that network pass
once, keeping the still-reachable records with their patch assembled; `records` reads the
cache (or `_fetch` + save on first use / `--force`), dropping any commit an earlier source
already covered. No per-file rename/create/delete check, so an "adds a file" commit survives
here but dies later in `apply_patch`. `_python` in the filename is the CVE's tag, not the
repo's, so language filtering is entirely `code_test_split`'s job.
"""

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.constants import TARGET_LANG
from susvibes.curate.mine.utils import github_get
from susvibes.curate.mine.dedup import KnownSet

REPOSVUL_RAW_PATH = get_dataset_path('raw_cve') / f'ReposVul/ReposVul_{TARGET_LANG}.jsonl'
REPOSVUL_CACHE_PATH = get_dataset_path('raw_cve') / f'ReposVul/ReposVul_{TARGET_LANG}_active.jsonl'


class ReposVulHandler:
    name = "ReposVul"
    raw_path = REPOSVUL_RAW_PATH
    cache_path = REPOSVUL_CACHE_PATH
    force = False

    @classmethod
    def remotely_active(cls, data_record) -> bool:
        """Whether the record's fix commit is still reachable on GitHub — the raw dataset carries
        links to repos and commits that have since gone."""
        return github_get(data_record['html_url'] + '.patch', allow_redirects=True) is not None

    @classmethod
    def _fetch(cls):
        """Keep the still-reachable records, assemble each patch from details[], and return
        the records to cache."""
        dataset = list(filter(cls.remotely_active, load_file(cls.raw_path)))
        for data_record in dataset:
            data_record["patch"] = {}
            for file_change in data_record["details"]:
                data_record["patch"][file_change["file_name"]] = file_change["patch"]
            data_record["cwe_ids"] = data_record.pop("cwe_id")
        return dataset

    @classmethod
    def records(cls, known: KnownSet):
        if not cls.force and cls.cache_path.exists():
            dataset = load_file(cls.cache_path)
        else:
            dataset = cls._fetch()
            save_file(dataset, cls.cache_path)
        return [record for record in dataset
                if not known.has(record["cve_id"], record["commit_id"])]
