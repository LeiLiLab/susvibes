"""Shared GitHub patch fetch for the sources.

Both Morefixes (when rebuilding its patch dataset) and OSV need a commit's unified patch
from GitHub; this is that one helper. Tries the REST API's patch media type, then the public
HTML `.patch` URL. Returns the patch text (git format-patch, so the full 40-char sha is on
its `From` line) or None on failure.
"""

import time
import requests

from susvibes.curate.mine.constants import GITHUB_HEADERS


def fetch_github_commit_patch(owner: str, repo: str, sha: str,
    timeout: int = 10, max_retries: int = 3) -> str:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "morefixes-tools/patch-fetch",
        "Accept": "application/vnd.github.patch",  # ask API to return patch
        "X-GitHub-Api-Version": "2022-11-28",
    })
    session.headers.update(GITHUB_HEADERS)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
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
    html_patch_url = f"https://github.com/{owner}/{repo}/commit/{sha}.patch"
    fallback_headers = {"User-Agent": "morefixes-tools/patch-fetch"}
    fallback_headers.update(GITHUB_HEADERS)
    try:
        r2 = requests.get(html_patch_url, timeout=timeout, headers=fallback_headers)
        if r2.status_code == 200 and r2.text.strip():
            return r2.text
        last_err = f"HTML .patch status {r2.status_code}"
    except requests.RequestException as e:
        last_err = f"HTML .patch error: {e}"

    print(f"Failed to fetch patch for {owner}/{repo}@{sha}: {last_err}")
    return None
