"""Morefixes source — CVE → repo → single fix commit, patch fetched from GitHub.

The URL dataset (`dataset_url_new.jsonl`) ships CVE → repo → `commit_sha`; the cache
(`dataset_new.jsonl`) is that with each commit's `.patch` fetched and stored, which
`records` splits into per-file hunks. `records` reads the cache; building it (missing, or
`--force`) keeps recent single-commit CVEs, fetches each `.patch`, and saves. No year
filter on the cached read — the build applies it (see B4 in docs/mine-filters).
"""

import json

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.constants import TARGET_LANG, RECENT_YR_CUTOFF
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources.utils import fetch_github_commit_patch

RAW_MOREFIXES_URL_DATASET_PATH = get_dataset_path('raw_cve_records') / 'Morefixes/dataset_url_new.jsonl'
MOREFIXES_CACHE_PATH = get_dataset_path('raw_cve_records') / 'Morefixes/dataset_new.jsonl'


class MorefixesHandler:
    name = "MoreFixes"
    url_path = RAW_MOREFIXES_URL_DATASET_PATH
    cache_path = MOREFIXES_CACHE_PATH
    target_lang = TARGET_LANG
    test_lang = TARGET_LANG
    force = False

    @classmethod
    def _build_cache(cls):
        """Rebuild the patch cache from the URL dataset: keep recent single-commit CVEs,
        fetch each commit's `.patch` from GitHub, and save."""
        url_dataset = load_file(cls.url_path)
        dataset = [data_record for data_record in url_dataset
            if int(data_record['cve_id'].split('-')[1]) >= RECENT_YR_CUTOFF
            and len(data_record['commits']) == 1]
        for data_record in tqdm(dataset, desc="MoreFixes: fetching patches", dynamic_ncols=True):
            if "patch" not in data_record:
                data_record["patch"] = fetch_github_commit_patch(
                    owner=data_record["owner"],
                    repo=data_record["repo"],
                    sha=data_record["commits"][0]["commit_sha"],
                )
        cls.cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(dataset, cls.cache_path)

    @classmethod
    def records(cls, known: KnownSet):
        if cls.force or not cls.cache_path.exists():
            cls._build_cache()
        dataset_text = cls.cache_path.read_text()
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
