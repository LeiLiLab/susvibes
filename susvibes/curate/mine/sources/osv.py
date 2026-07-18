"""OSV source (M1) — deterministic CVE → fix commit from the OSV PyPI feed.

For each PyPI advisory in OSV's `all.zip` the fix commit is read straight from the data:
GIT-range `fixed` commits and reference `/commit/` URLs, with the advisory's CWE ids. `_fetch`
keeps the commits Morefixes + ReposVul don't already have (their raw commits) — the commit-new
residual — and fetches each `.patch` from GitHub (no clone), resolving the full 40-char commit
from the patch's `From` header. `records` reads the cache (or `_fetch` + save on first use /
`--force`), skipping any commit an earlier source already covered. PyPI-scoped, so the ~80%
cross-language waste of the Morefixes crawl doesn't apply. See docs/mine-filters M1.
"""

import json
import re
import zipfile

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.sources.utils import fetch_github_commit_patch
from susvibes.curate.mine.sources.morefixes import MorefixesHandler
from susvibes.curate.mine.sources.reposvul import ReposVulHandler
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet, normalize_commit

OSV_ZIP_PATH = get_dataset_path('raw_cve') / 'OSV' / 'pypi.zip'
OSV_CACHE_PATH = get_dataset_path('raw_cve') / 'OSV' / 'osv_fixes.jsonl'

COMMIT_URL = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s]+)/commit/([0-9a-f]{7,40})')
REPO_URL = re.compile(r'github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
FROM_COMMIT = re.compile(r'^From ([0-9a-f]{40}) ', re.MULTILINE)


def read_osv_fix_commits(zip_path) -> dict:
    """Parse OSV PyPI `all.zip` → `{cve_id: {"commits": {(owner, repo, commit), ...},
    "cwe_ids": [...]}}`. A CVE may map to several fix commits (backport branches); the source
    picks one per the single-commit model. CWE ids come from `database_specific.cwe_ids`.
    Advisories with no CVE alias (MAL/PYSEC-only) are skipped — the pipeline is CVE-keyed."""
    fix_data: dict = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            advisory = json.loads(z.read(name))
            cves = [alias for alias in advisory.get("aliases", []) if alias.startswith("CVE-")]
            if advisory["id"].startswith("CVE-"):
                cves.append(advisory["id"])
            if not cves:
                continue
            commits = set()
            for affected in advisory.get("affected", []):
                for rng in affected.get("ranges", []):
                    if rng.get("type") != "GIT":
                        continue
                    m = REPO_URL.search(rng.get("repo", "") or "")
                    if not m:
                        continue
                    for event in rng.get("events", []):
                        if event.get("fixed"):
                            commits.add((m.group(1), m.group(2), event["fixed"]))
            for ref in advisory.get("references", []):
                m = COMMIT_URL.search(ref.get("url", ""))
                if m:
                    commits.add((m.group(1), m.group(2).removesuffix(".git"), m.group(3)))
            cwe_ids = advisory.get("database_specific", {}).get("cwe_ids") or []
            for cve in cves:
                entry = fix_data.setdefault(cve, {"commits": set(), "cwe_ids": []})
                entry["commits"].update(commits)
                for cwe in cwe_ids:
                    if cwe not in entry["cwe_ids"]:
                        entry["cwe_ids"].append(cwe)
    return fix_data


def known_source_commits() -> set:
    """Full commits Morefixes + ReposVul already cover (their raw datasets), so OSV only
    fetches the commit-new residual. Read from Morefixes' small URL dataset and ReposVul's raw
    dataset — the same base the offline M1 model used."""
    commits = set()
    if MorefixesHandler.url_path.exists():
        for line in MorefixesHandler.url_path.read_text().split("\n"):
            try:
                record = json.loads(line)
            except Exception:
                continue
            for commit in record.get("commits", []):
                if commit.get("commit_sha"):
                    commits.add(normalize_commit(commit["commit_sha"]))
    if ReposVulHandler.raw_path.exists():
        for record in load_file(ReposVulHandler.raw_path):
            if record.get("commit_id"):
                commits.add(normalize_commit(record["commit_id"]))
    return commits


class OSVSource:
    name = "OSV"
    zip_path = OSV_ZIP_PATH
    cache_path = OSV_CACHE_PATH
    force = False

    @classmethod
    def _fetch(cls):
        """Drop the commits Morefixes/ReposVul already have, fetch the rest's `.patch`
        (keeping only those whose full commit resolves), and return the records to cache."""
        skip = known_source_commits()
        fix_data = read_osv_fix_commits(cls.zip_path)
        candidates = []
        for cve, entry in fix_data.items():
            if not entry["commits"]:
                continue
            owner, repo, commit = sorted(entry["commits"])[0]
            if normalize_commit(commit) in skip:
                continue
            candidates.append((cve, owner, repo, commit, entry["cwe_ids"]))
        records = []
        for cve, owner, repo, commit, cwe_ids in tqdm(candidates, desc="OSV: fetching patches", dynamic_ncols=True):
            patch_text = fetch_github_commit_patch(owner, repo, commit)
            if not patch_text:
                continue
            m = FROM_COMMIT.search(patch_text)
            if not m:
                continue        # couldn't resolve the full commit (short/unreliable) — drop
            records.append({
                "cve_id": cve,
                "commit_id": m.group(1),
                "cwe_ids": cwe_ids,
                "owner": owner,
                "repo": repo,
                "repo_url": f"https://github.com/{owner}/{repo}",
                "patch": patch_text,
            })
        return records

    @classmethod
    def records(cls, known: KnownSet):
        if not cls.force and cls.cache_path.exists():
            records = load_file(cls.cache_path)
        else:
            records = cls._fetch()
            save_file(records, cls.cache_path)
        for record in records:
            if known.has_commit(record["commit_id"]):
                continue
            try:
                patch = split_to_file_patches(record["patch"])
            except ValueError:
                continue
            yield {
                "patch": patch,
                "commit_id": record["commit_id"],
                "cve_id": record["cve_id"],
                "cwe_ids": record["cwe_ids"],
                "owner": record["owner"],
                "repo": record["repo"],
                "repo_url": record["repo_url"],
            }
