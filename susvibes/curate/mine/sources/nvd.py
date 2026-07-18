"""NVD source (M3) — beyond-ecosystem CVEs: NVD CVEs that reference a GitHub repo and are
uncovered by OSV/Morefixes/ReposVul, dug out of a ~48k low-signal haystack (other languages +
PoC/writeup repos). Internally: prefilter (deterministic, recall-first) → inspect (Haiku, extract
repo + vulnerable_files + languages) → route → finder (Sonnet). All in this file per the design;
see docs/mine-filters Issue C.

This file so far = the deterministic prefilter: `nvd_universe()` (366k → github-linked ∩ uncovered)
+ `prefilter()` (recall-first drop of the clearly-non-Python). inspect/route/finder land next.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from susvibes.core.constants import get_dataset_path
from susvibes.curate.mine.utils import get_repo_language
from susvibes.curate.mine.sources.osv import (
    REF_REPO, GITHUB_NON_OWNER, GITHUB_NON_REPO, known_source_cves,
    read_osv_fix_commits, OSV_ZIP_PATH)

NVD_PATH = get_dataset_path('raw_cve') / 'NVD' / 'nvd.jsonl'
NVD_REPO_LANG_CACHE_PATH = get_dataset_path('raw_cve') / 'NVD' / 'nvd_repo_language.json'
LANG_WORKERS = 16

# An NVD description hitting one of these keeps the CVE regardless of repo language — a union
# signal (keyword recall is only ~32%), not the gate. Word-boundary, case-insensitive.
PYTHON_KEYWORDS = re.compile(
    r'\b(python|django|flask|pypi|pip|jupyter|numpy|pandas|werkzeug|pytorch|tensorflow|ansible|'
    r'celery|tornado|sqlalchemy|pyramid|fastapi|aiohttp|scrapy|wsgi|conda|matplotlib|scipy)\b', re.I)

# GitHub repo names that are PoC/writeup dumps, not the vulnerable project — kept (passed to
# inspect, which decides) but never trusted as a positive repo-language signal.
POC_REPO = re.compile(r'(cve[-_]?\d|poc|exploit|proof.?of.?concept|writeup|vuln.?report|advisor)', re.I)

# GitHub primary languages that count as Python for the recall-first keep — GitHub labels
# notebook-heavy Python repos "Jupyter Notebook", so a repo tagged that is Python in practice.
PYTHON_LANGS = {"python", "jupyter notebook"}


def github_repos(record) -> list:
    """The (owner, repo) GitHub source repos an NVD record references — advisory/data URLs
    (advisories/, */advisory-database) filtered out, mirroring read_osv_residual."""
    repos = []
    for url in record.get("refs", []):
        if "github.com/" not in (url or ""):
            continue
        m = REF_REPO.search(url)
        if not m or m.group(1) in GITHUB_NON_OWNER \
                or m.group(2).removesuffix(".git").lower() in GITHUB_NON_REPO:
            continue
        pair = (m.group(1), m.group(2).removesuffix(".git"))
        if pair not in repos:
            repos.append(pair)
    return repos


def is_poc_repo(owner, repo) -> bool:
    """Whether owner/repo looks like a PoC/writeup dump rather than the vulnerable project."""
    return bool(POC_REPO.search(f"{owner}/{repo}"))


def covered_cves() -> set:
    """CVE ids another source already handles (Morefixes + ReposVul + every OSV alias), so NVD
    mines only the residual — the static dedup base, mirroring the other discovery sources."""
    return set(known_source_cves()) | set(read_osv_fix_commits(OSV_ZIP_PATH).keys())


def nvd_universe() -> list:
    """The M3 haystack: NVD CVEs that reference a GitHub source repo and are uncovered by
    OSV/Morefixes/ReposVul (~48k). Each entry keeps `{cve_id, desc, repos, cpe_products}`."""
    covered = covered_cves()
    universe = []
    for line in open(NVD_PATH):                 # 305M / 366k lines — stream, not a whole-file load
        record = json.loads(line)
        if record["id"] in covered:
            continue
        repos = github_repos(record)
        if not repos:
            continue
        universe.append({
            "cve_id": record["id"],
            "desc": record.get("desc", ""),
            "repos": repos,
            "cpe_products": record.get("cpe_products", []),
        })
    return universe


def repos_to_language(universe) -> set:
    """The repos whose language could decide a drop: those under a CVE with no Python keyword hit,
    excluding PoC repos (kept regardless). A keyword-hit CVE or a PoC repo never needs the API."""
    needed = set()
    for cve in universe:
        if PYTHON_KEYWORDS.search(cve["desc"]):
            continue
        for owner, repo in cve["repos"]:
            if not is_poc_repo(owner, repo):
                needed.add(f"{owner}/{repo}")
    return needed


def fetch_repo_languages(projects, force=False):
    """Return `{owner/repo: language}` for `projects` via the GitHub API, threaded and cached
    (`NVD_REPO_LANG_CACHE_PATH`). "" means no detected language; a failed lookup is left uncached
    so a later run retries it."""
    cache = {} if force or not NVD_REPO_LANG_CACHE_PATH.exists() \
        else json.loads(NVD_REPO_LANG_CACHE_PATH.read_text())
    todo = [p for p in projects if p not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=LANG_WORKERS) as executor:
            futures = {executor.submit(get_repo_language, p): p for p in todo}
            for future in tqdm(as_completed(futures), total=len(futures),
                desc="repo languages", dynamic_ncols=True):
                lang = future.result()
                if lang is not None:            # cache success ("" = no language); retry failures
                    cache[futures[future]] = lang
        NVD_REPO_LANG_CACHE_PATH.write_text(json.dumps(cache))
    return cache


def should_drop(cve, lang_cache) -> bool:
    """Recall-first: drop ONLY when every referenced repo has a known, non-Python primary language.
    A Python keyword hit, a PoC repo, or any repo whose language is unknown/uncached/none keeps it
    (ambiguity is passed to inspect, never dropped here)."""
    if PYTHON_KEYWORDS.search(cve["desc"]):
        return False
    langs = set()
    for owner, repo in cve["repos"]:
        if is_poc_repo(owner, repo):
            return False
        lang = lang_cache.get(f"{owner}/{repo}")
        if not lang:                            # None (uncached/failed) or "" (no language) → keep
            return False
        langs.add(lang.lower())
    return not (langs & PYTHON_LANGS)           # every repo known and non-Python-ish → drop


def prefilter(force=False):
    """Deterministic recall-first cut: the NVD universe minus the CVEs whose every repo is a known
    non-Python language. Returns `(universe, survivors)`; survivors feed inspect."""
    universe = nvd_universe()
    lang_cache = fetch_repo_languages(repos_to_language(universe), force=force)
    survivors = [cve for cve in universe if not should_drop(cve, lang_cache)]
    return universe, survivors
