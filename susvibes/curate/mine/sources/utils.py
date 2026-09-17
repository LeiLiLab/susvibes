"""Shared GitHub patch fetch for the sources.

Both Morefixes (when rebuilding its patch dataset) and OSV need a commit's unified patch
from GitHub; this is that one helper. Tries the REST API's patch media type, then the public
HTML `.patch` URL. Returns the patch text (git format-patch, so the full 40-char commit is on
its `From` line) or None on failure. Rate limiting is `mine.utils.github_get`'s job.

`fetch_pinned_patches` is the discovery sources' assembly step on top of it: the patch of every
commit the finder pinned, reusing the one an earlier run already assembled for that same commit.
"""

from pathlib import Path

from susvibes.core.utils import load_file
from susvibes.curate.mine.utils import github_get

PATCH_HEADERS = {
    "User-Agent": "morefixes-tools/patch-fetch",
    "Accept": "application/vnd.github.patch",       # ask the API to return the patch, not JSON
    "X-GitHub-Api-Version": "2022-11-28",
}


def fetch_patch_from_github(owner: str, repo: str, commit: str) -> str | None:
    """A commit's unified patch, from the REST API's patch media type or, if that serves nothing,
    the public HTML `.patch` URL. None when neither does."""
    sources = (
        (f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}", PATCH_HEADERS),
        (f"https://github.com/{owner}/{repo}/commit/{commit}.patch", None),
    )
    for url, headers in sources:
        response = github_get(url, headers=headers)
        if response is not None and response.text.strip():
            return response.text
    print(f"Failed to fetch patch for {owner}/{repo}@{commit}")
    return None


def fetch_pinned_patches(results, cache_path):
    """Fill in the `.patch` of every single fix commit the finder pinned, reusing the patch
    `cache_path` already holds for that same commit. A resume therefore re-fetches nothing — which
    also keeps a failed fetch (rate limit, outage) from blanking a patch an earlier run had
    assembled fine."""
    cache_path = Path(cache_path)
    assembled = {record["cve_id"]: record for record in load_file(cache_path)} if cache_path.exists() else {}
    for result in results:
        if not result["commit"] or result["multi_commit"]:
            continue
        prior = assembled.get(result["cve_id"], {})
        result["patch"] = prior["patch"] if prior.get("commit") == result["commit"] \
            and prior.get("patch") else fetch_patch_from_github(
                result["owner"], result["repo"], result["commit"])
    return results
