"""Morefixes source — CVE → repo → single fix commit, patch already fetched by crawl.

Upstream ships CVE → repo → `commit_sha`; `crawl.py` fetched the `.patch` text and applied
its year / single-commit filters, so this reader only splits the patch into per-file hunks
and drops records whose patch can't be represented. No year filter here (crawl did it — see
B4 in docs/mine-filters).
"""

import json

from susvibes.core.constants import get_dataset_path
from susvibes.curate.mine.constants import TARGET_LANG
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet

RAW_MOREFIXES_DATASET_PATH = get_dataset_path('cve_records') / 'Morefixes/dataset_new.jsonl'


class MorefixesHandler:
    name = "MoreFixes"
    dataset_path = RAW_MOREFIXES_DATASET_PATH
    target_lang = TARGET_LANG
    test_lang = TARGET_LANG

    @classmethod
    def records(cls, known: KnownSet):
        dataset_text = cls.dataset_path.read_text()
        dataset_crawled = []
        for line in dataset_text.splitlines():
            try:
                data_record = json.loads(line.strip())
            except Exception as e:
                continue
            if data_record["patch"]:
                dataset_crawled.append(data_record)

        dataset_filtered = []
        for data_record in dataset_crawled:
            try:
                file_patches = split_to_file_patches(data_record["patch"])
            except ValueError as e:
                continue
            data_record["patch"] = file_patches
            commit = data_record["commits"][0]
            data_record["commit_id"] = commit['commit_sha']
            dataset_filtered.append(data_record)
        return dataset_filtered
