"""Morefixes source — CVE → repo → single fix commit, patch fetched from GitHub.

The URL dataset (`dataset_url_new.jsonl`) ships CVE → repo → `commit_sha`; the cache
(`dataset_new.jsonl`) is that with each commit's `.patch` fetched and stored, which `records`
splits into per-file hunks. `records` reads the cache (or `_fetch` + save on first use /
`--force`), dropping any commit an earlier source already covered.
"""

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.constants import TARGET_LANG
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources.utils import fetch_patch_from_github

MOREFIXES_URL_PATH = get_dataset_path('raw_cve') / 'Morefixes/dataset_url_new.jsonl'
MOREFIXES_CACHE_PATH = get_dataset_path('raw_cve') / 'Morefixes/dataset_new.jsonl'


class MorefixesHandler:
    name = "MoreFixes"
    url_path = MOREFIXES_URL_PATH
    cache_path = MOREFIXES_CACHE_PATH
    target_lang = TARGET_LANG
    test_lang = TARGET_LANG
    force = False

    @classmethod
    def _fetch(cls):
        """Keep recent single-commit CVEs from the URL dataset and fetch each commit's
        `.patch`; return the records to cache."""
        recent_year_cutoff = 2014  # should be removed after re-fetching morefixes dataset, should have instance-level reuse
        url_dataset = load_file(cls.url_path)
        dataset = [data_record for data_record in url_dataset
            if int(data_record['cve_id'].split('-')[1]) >= recent_year_cutoff
            and len(data_record['commits']) == 1]
        for data_record in tqdm(dataset, desc="MoreFixes: fetching patches", dynamic_ncols=True):
            if "patch" not in data_record:
                data_record["patch"] = fetch_patch_from_github(
                    owner=data_record["owner"],
                    repo=data_record["repo"],
                    commit=data_record["commits"][0]["commit_sha"],
                )
        return dataset

    @classmethod
    def records(cls, known: KnownSet):
        if not cls.force and cls.cache_path.exists():
            dataset = load_file(cls.cache_path)
        else:
            dataset = cls._fetch()
            save_file(dataset, cls.cache_path)
        dataset_filtered = []
        for data_record in dataset:
            if not data_record["patch"]:
                continue
            commit_sha = data_record["commits"][0]['commit_sha']
            if known.has(data_record["cve_id"], commit_sha):
                continue
            try:
                file_patches = split_to_file_patches(data_record["patch"])
            except ValueError as e:
                continue
            data_record["patch"] = file_patches
            data_record["commit_id"] = commit_sha
            dataset_filtered.append(data_record)
        return dataset_filtered
