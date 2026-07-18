"""Shared GitHub helpers for the sources: a commit's unified patch, and the URL/identity parsing
every source uses to pull owner/repo/commit out of advisory references.

`fetch_github_commit_patch` — Morefixes (rebuilding its patch dataset) and OSV both need a commit's
patch from GitHub; tries the REST API's patch media type, then the public HTML `.patch` URL, and
returns the patch text (git format-patch, so the full 40-char commit is on its `From` line) or None.

The regexes + non-owner/repo sets identify a project's source repo in a reference URL: a
`github.com/advisories/GHSA-…` or `pypa/advisory-database` link is the advisory/data, never the
source, so it is filtered out.
"""

import re
import time
import requests

from susvibes.curate.mine.constants import GITHUB_HEADERS

COMMIT_URL = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s]+)/commit/([0-9a-f]{7,40})')
REPO_URL = re.compile(r'github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
REF_REPO = re.compile(r'github\.com/([^/\s]+)/([^/\s?#]+)')
FROM_COMMIT = re.compile(r'^From ([0-9a-f]{40}) ', re.MULTILINE)

# GitHub first-path segments that are site sections, not repo owners — a `github.com/advisories/
# GHSA-…` reference URL is the advisory itself, never the project source.
GITHUB_NON_OWNER = {"advisories", "security", "sponsors", "marketplace", "orgs", "collections",
                    "topics", "about", "pricing", "settings", "notifications"}
# Advisory-database repos (`pypa/advisory-database`, `github/advisory-database`, …) are the CVE
# data source, not the vulnerable project — a reference to one is not a source repo.
GITHUB_NON_REPO = {"advisory-database", "advisory-db", "security-advisories"}


def fetch_github_commit_patch(owner: str, repo: str, commit: str,
    timeout: int = 10, max_retries: int = 3) -> str:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "morefixes-tools/patch-fetch",
        "Accept": "application/vnd.github.patch",  # ask API to return patch
        "X-GitHub-Api-Version": "2022-11-28",
    })
    session.headers.update(GITHUB_HEADERS)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}"
    backoff = 1.5
    last_err = None

    for retry in range(max_retries):
        try:
            r = session.get(api_url, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r.text
            if r.status_code in (403, 429):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    time.sleep(int(retry_after))
                else:
                    reset = r.headers.get("X-RateLimit-Reset")
                    if reset:
                        wait = max(0, int(reset) - int(time.time())) + 1
                        time.sleep(wait)
            last_err = f"API status {r.status_code}"
        except requests.RequestException as e:
            last_err = f"API error: {e}"
        time.sleep(backoff ** retry)

    # Fallback
    html_patch_url = f"https://github.com/{owner}/{repo}/commit/{commit}.patch"
    fallback_headers = {"User-Agent": "morefixes-tools/patch-fetch"}
    fallback_headers.update(GITHUB_HEADERS)
    try:
        r2 = requests.get(html_patch_url, timeout=timeout, headers=fallback_headers)
        if r2.status_code == 200 and r2.text.strip():
            return r2.text
        last_err = f"HTML .patch status {r2.status_code}"
    except requests.RequestException as e:
        last_err = f"HTML .patch error: {e}"

    print(f"Failed to fetch patch for {owner}/{repo}@{commit}: {last_err}")
    return None
