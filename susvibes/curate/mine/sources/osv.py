"""OSV source (M1) — deterministic CVE → fix commit from the OSV PyPI feed.

For each PyPI advisory in OSV's `all.zip` the fix commit is read straight from the data:
GIT-range `fixed` shas and reference `/commit/` URLs. Like Morefixes this fetches the
`.patch` from GitHub (no clone) and funnels through `code_test_split`; the full 40-char
sha is read back from the fetched patch's `From` header so `KnownSet` dedups exactly, even
when OSV gave a short `/commit/` sha. PyPI-scoped, so the ~80% cross-language waste of the
Morefixes crawl doesn't apply, and it stays fresh on re-runs (GHSA is filled by maintainers
at disclosure). Runs after Morefixes + ReposVul, contributing the sha-new residual. See
docs/mine-filters M1.
"""

import json
import re
import zipfile

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.curate.mine.crawl import fetch_github_commit_patch
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet, normalize_sha

RAW_OSV_PYPI_PATH = get_dataset_path('cve_records') / 'OSV' / 'pypi.zip'

_COMMIT_URL = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s]+)/commit/([0-9a-f]{7,40})')
_REPO_URL = re.compile(r'github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
_FROM_SHA = re.compile(r'^From ([0-9a-f]{40}) ', re.MULTILINE)


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
                    m = _REPO_URL.search(rng.get("repo", "") or "")
                    if not m:
                        continue
                    for event in rng.get("events", []):
                        if event.get("fixed"):
                            fixes.add((m.group(1), m.group(2), event["fixed"]))
            for ref in advisory.get("references", []):
                m = _COMMIT_URL.search(ref.get("url", ""))
                if m:
                    fixes.add((m.group(1), m.group(2).removesuffix(".git"), m.group(3)))
            for cve in cves:
                commits.setdefault(cve, set()).update(fixes)
    return commits


class OSVSource:
    name = "OSV"
    zip_path = RAW_OSV_PYPI_PATH

    @classmethod
    def records(cls, known: KnownSet):
        fix_commits = read_osv_fix_commits(cls.zip_path)
        candidates = []
        for cve, fixes in fix_commits.items():
            if not fixes:
                continue
            owner, repo, sha = sorted(fixes)[0]
            if known.has_sha(sha):
                continue
            candidates.append((cve, owner, repo, sha))
        for cve, owner, repo, sha in tqdm(candidates, desc="OSV: fetching patches", dynamic_ncols=True):
            patch_text = fetch_github_commit_patch(owner, repo, sha)
            if not patch_text:
                continue
            m = _FROM_SHA.search(patch_text)
            full_sha = m.group(1) if m else normalize_sha(sha)
            if known.has_sha(full_sha):
                continue
            try:
                patch = split_to_file_patches(patch_text)
            except ValueError:
                continue
            yield {
                "patch": patch,
                "commit_id": full_sha,
                "cve_id": cve,
                "cwe_ids": [],
                "owner": owner,
                "repo": repo,
                "repo_url": f"https://github.com/{owner}/{repo}",
            }
