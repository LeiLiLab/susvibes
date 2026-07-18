"""OSV source (M1) — deterministic CVE → fix commit from the OSV PyPI feed.

For each PyPI advisory in OSV's `all.zip` the fix commit is read straight from the data:
GIT-range `fixed` commits and reference `/commit/` URLs. Building the cache (`osv_fixes.jsonl`,
missing or `--force`) keeps the commits Morefixes + ReposVul don't already have (their raw
commits) — the commit-new residual — fetches each `.patch` from GitHub (no clone) resolving the
full 40-char commit from the patch's `From` header, and saves. `records` reads the cache and
never re-fetches, skipping any commit already covered by an earlier source. PyPI-scoped, so the
~80% cross-language waste of the Morefixes crawl doesn't apply, and a rebuild stays fresh
(GHSA is filled at disclosure). See docs/mine-filters M1.
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

OSV_ZIP_PATH = get_dataset_path('raw_cve_records') / 'OSV' / 'pypi.zip'
OSV_CACHE_PATH = get_dataset_path('raw_cve_records') / 'OSV' / 'osv_fixes.jsonl'

COMMIT_URL = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s]+)/commit/([0-9a-f]{7,40})')
REPO_URL = re.compile(r'github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
FROM_COMMIT = re.compile(r'^From ([0-9a-f]{40}) ', re.MULTILINE)


def read_osv_fix_commits(zip_path) -> dict[str, set]:
    """Parse OSV PyPI `all.zip` → `{cve_id: {(owner, repo, fixed_sha), ...}}`. A CVE may map
    to several fix commits (backport branches); the source picks one per the single-commit
    model. Advisories with no CVE alias (MAL/PYSEC-only) are skipped — the pipeline is
    CVE-keyed."""
    commits: dict[str, set] = {}
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
            fixes = set()
            for affected in advisory.get("affected", []):
                for rng in affected.get("ranges", []):
                    if rng.get("type") != "GIT":
                        continue
                    m = REPO_URL.search(rng.get("repo", "") or "")
                    if not m:
                        continue
                    for event in rng.get("events", []):
                        if event.get("fixed"):
                            fixes.add((m.group(1), m.group(2), event["fixed"]))
            for ref in advisory.get("references", []):
                m = COMMIT_URL.search(ref.get("url", ""))
                if m:
                    fixes.add((m.group(1), m.group(2).removesuffix(".git"), m.group(3)))
            for cve in cves:
                commits.setdefault(cve, set()).update(fixes)
    return commits


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
    def _build_cache(cls):
        """Extract each CVE's fix commit, drop the ones Morefixes/ReposVul already have,
        fetch the rest's `.patch` (resolving the full commit), and save."""
        skip = known_source_commits()
        fix_commits = read_osv_fix_commits(cls.zip_path)
        candidates = []
        for cve, fixes in fix_commits.items():
            if not fixes:
                continue
            owner, repo, commit = sorted(fixes)[0]
            if normalize_commit(commit) in skip:
                continue
            candidates.append((cve, owner, repo, commit))
        records = []
        for cve, owner, repo, commit in tqdm(candidates, desc="OSV: building cache (fetch)", dynamic_ncols=True):
            patch_text = fetch_github_commit_patch(owner, repo, commit)
            if not patch_text:
                continue
            m = FROM_COMMIT.search(patch_text)
            if not m:
                continue        # couldn't resolve the full commit (short/unreliable) — drop
            full_commit = m.group(1)
            records.append({
                "cve_id": cve,
                "commit_id": full_commit,
                "owner": owner,
                "repo": repo,
                "repo_url": f"https://github.com/{owner}/{repo}",
                "patch": patch_text,
            })
        cls.cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(records, cls.cache_path)

    @classmethod
    def records(cls, known: KnownSet):
        if cls.force or not cls.cache_path.exists():
            cls._build_cache()
        for record in load_file(cls.cache_path):
            if len(record["commit_id"]) != 40:
                continue        # guard a stale cache entry whose commit didn't fully resolve
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
                "cwe_ids": [],
                "owner": record["owner"],
                "repo": record["repo"],
                "repo_url": record["repo_url"],
            }
