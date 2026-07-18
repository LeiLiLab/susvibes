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
import zipfile

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.curate.mine.sources.utils import (
    fetch_github_commit_patch, COMMIT_URL, REPO_URL, REF_REPO, FROM_COMMIT,
    GITHUB_NON_OWNER, GITHUB_NON_REPO)
from susvibes.curate.mine.sources.morefixes import MorefixesHandler
from susvibes.curate.mine.sources.reposvul import ReposVulHandler
from susvibes.curate.mine.utils import split_to_file_patches
from susvibes.curate.mine.dedup import KnownSet, normalize_commit
from susvibes.curate.mine.find_commit import finder_threadpool

OSV_ZIP_PATH = get_dataset_path('raw_cve') / 'OSV' / 'pypi.zip'
OSV_CACHE_PATH = get_dataset_path('raw_cve') / 'OSV' / 'osv_fixes.jsonl'
OSV_RESIDUAL_CACHE_PATH = get_dataset_path('raw_cve') / 'OSV' / 'osv_residual_fixes.jsonl'


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


def read_osv_residual(zip_path) -> dict:
    """Parse OSV PyPI `all.zip` → `{cve_id: {owner, repo, ghsa, cwe_ids, fixed_versions,
    summary}}` for the CVEs that name a GitHub repository but carry NO fix commit — the M2
    finder's input. A commit anywhere (GIT-range `fixed` sha or a reference `/commit/` URL, in
    ANY of the CVE's advisories) removes it from the residual; the repo comes from a GIT-range
    `repo` or, failing that, a non-advisory github.com reference. Aggregated per CVE so a
    commit in one advisory suppresses a repo-only sibling advisory."""
    cve_commits: dict = {}
    cve_meta: dict = {}
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
            commits, repo, fixed_versions = set(), None, []
            for affected in advisory.get("affected", []):
                for rng in affected.get("ranges", []):
                    if rng.get("type") == "GIT":
                        m = REPO_URL.search(rng.get("repo", "") or "")
                        if m:
                            repo = (m.group(1), m.group(2))
                        for event in rng.get("events", []):
                            if event.get("fixed"):
                                commits.add(event["fixed"])
                    else:
                        for event in rng.get("events", []):
                            if event.get("fixed"):
                                fixed_versions.append(event["fixed"])
            for ref in advisory.get("references", []):
                if COMMIT_URL.search(ref.get("url", "")):
                    commits.add(ref["url"])
                elif repo is None and "github.com/" in ref.get("url", ""):
                    m = REF_REPO.search(ref["url"])
                    if m and m.group(1) not in GITHUB_NON_OWNER \
                            and m.group(2).removesuffix(".git").lower() not in GITHUB_NON_REPO:
                        repo = (m.group(1), m.group(2).removesuffix(".git"))
            ghsa = next((a for a in advisory.get("aliases", []) if a.startswith("GHSA-")), "")
            cwe_ids = advisory.get("database_specific", {}).get("cwe_ids") or []
            for cve in cves:
                cve_commits.setdefault(cve, set()).update(commits)
                if repo is not None:
                    cve_meta[cve] = {
                        "owner": repo[0],
                        "repo": repo[1],
                        "ghsa": ghsa,
                        "cwe_ids": cwe_ids,
                        "fixed_versions": sorted(set(fixed_versions)),
                        "summary": advisory.get("summary", ""),
                    }
    return {cve: meta for cve, meta in cve_meta.items() if not cve_commits.get(cve)}


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


def known_source_cves() -> set:
    """CVE ids Morefixes + ReposVul already cover (their raw datasets), so the M2 finder never
    runs on a CVE an existing source may already pin — the same static base as M1's commit skip."""
    cves = set()
    if MorefixesHandler.url_path.exists():
        for line in MorefixesHandler.url_path.read_text().split("\n"):
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("cve_id"):
                cves.add(record["cve_id"])
    if ReposVulHandler.raw_path.exists():
        for record in load_file(ReposVulHandler.raw_path):
            if record.get("cve_id"):
                cves.add(record["cve_id"])
    return cves


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


class OSVResidualSource:
    """M2 — OSV PyPI CVEs with a repo but no commit in any source. The S2 finder pins each fix
    commit by fact; every finder outcome (a pinned commit + evidence, a real miss, or an errored
    run) is cached so the agent never runs twice, and `--resume` re-runs only the errored ones.
    `run_id` (set by `core` alongside `force`/`resume`) routes the per-CVE trajectories under
    `logs/curate/<run_id>/mine/find_commit/`."""
    name = "OSV-residual"
    zip_path = OSV_ZIP_PATH
    cache_path = OSV_RESIDUAL_CACHE_PATH
    force = False
    resume = False
    run_id = None

    @classmethod
    def _fetch(cls, prior=None):
        """Build the residual pool (skipping CVEs Morefixes/ReposVul already cover, before paying
        the finder), run the finder over the CVEs `prior` hasn't already concluded, fetch the
        `.patch` for each new single-commit pin, and return every outcome to cache. `prior` (the
        cached results on a `resume`) carries concluded pins/misses through untouched; only its
        `error` entries fall back into the finder."""
        residual = read_osv_residual(cls.zip_path)
        skip = known_source_cves()
        pool = [{"cve_id": cve, **meta} for cve, meta in residual.items() if cve not in skip]
        done = {r["cve_id"]: r for r in (prior or []) if not r.get("error")}
        todo = [record for record in pool if record["cve_id"] not in done]
        fresh = finder_threadpool(todo, cls.run_id)
        for result in fresh:
            if result["commit"] and not result["multi_commit"]:
                result["patch"] = fetch_github_commit_patch(
                    result["owner"], result["repo"], result["commit"])
        return list(done.values()) + fresh

    @classmethod
    def records(cls, known: KnownSet):
        # force = re-run the finder on every CVE (discard the cache); resume = re-run only the
        # errored entries (keep concluded pins/misses); neither = read the cache as-is. force wins.
        if cls.force or not cls.cache_path.exists():
            results = cls._fetch()
            save_file(results, cls.cache_path)
        elif cls.resume:
            results = cls._fetch(load_file(cls.cache_path))
            save_file(results, cls.cache_path)
        else:
            results = load_file(cls.cache_path)
        for result in results:
            if not result["commit"] or result["multi_commit"]:
                continue
            if known.has_commit(result["commit"]):
                continue
            if not result.get("patch"):
                continue
            try:
                patch = split_to_file_patches(result["patch"])
            except ValueError:
                continue
            yield {
                "patch": patch,
                "commit_id": result["commit"],
                "cve_id": result["cve_id"],
                "cwe_ids": result["cwe_ids"],
                "owner": result["owner"],
                "repo": result["repo"],
                "repo_url": f"https://github.com/{result['owner']}/{result['repo']}",
            }
