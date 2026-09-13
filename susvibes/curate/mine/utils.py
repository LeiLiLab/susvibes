import re
import time
import requests

# is_test_file / path_has_keyword are defined in the self-contained check_cov engine
# (vendored py2/py3 into the static_py containers); re-export so mine.core keeps importing
# them from here. Importing engine.extract_facts stays light — engine/__init__ pulls in no jedi.
from susvibes.curate.mine.post.check_cov.engine.extract_facts import (  # noqa: F401
    path_has_keyword,
    is_test_file,
)
from susvibes.curate.mine.constants import GITHUB_HEADERS


GITHUB_RETRIES = 3              # attempts per URL before giving up
GITHUB_TIMEOUT = 10             # seconds per request
GITHUB_BACKOFF_SEC = 5          # first retry waits this long, doubling after (matches AGENT_BACKOFF_SEC)
GITHUB_THROTTLE_WAIT_SEC = 60   # a throttled response carrying no reset hint is a secondary rate
                                # limit, for which GitHub asks to wait at least a minute

REPO_URL = "https://api.github.com/repos/{project}"


def rate_limit_sleep(response) -> bool:
    """Wait out a GitHub rate limit if that is what `response` reports: the server's own
    `Retry-After`, else the `X-RateLimit-Reset` epoch, else the flat wait a secondary rate limit
    asks for — it carries neither header, so falling back to a short backoff would burn the
    retries in seconds. Returns whether it waited."""
    if response.status_code not in (403, 429):
        return False
    retry_after = response.headers.get("Retry-After")
    reset = response.headers.get("X-RateLimit-Reset")
    if retry_after:
        time.sleep(int(retry_after))
    elif reset:
        time.sleep(max(0, int(reset) - int(time.time())) + 1)
    else:
        time.sleep(GITHUB_THROTTLE_WAIT_SEC)
    return True


def github_get(url, headers=None, timeout=GITHUB_TIMEOUT, **kwargs):
    """GET a GitHub URL with the mining stage's auth, honouring rate limits: a throttled call waits
    the limit out and retries, so it blocks rather than silently reporting the resource missing —
    which every caller here would read as "this repo/commit is gone". Returns the 200 response, or
    None once the retries are spent or the resource is definitively gone."""
    headers = {**GITHUB_HEADERS, **(headers or {})}
    for attempt in range(GITHUB_RETRIES):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, **kwargs)
            if response.status_code == 200:
                return response
            if response.status_code in (404, 410):
                return None                     # gone for good — retrying cannot help
            if rate_limit_sleep(response):
                continue
        except requests.RequestException:
            pass
        if attempt < GITHUB_RETRIES - 1:
            time.sleep(GITHUB_BACKOFF_SEC * (2 ** attempt))
    return None


def get_repo_size(project) -> int | None:
    """Return repo size in KB via GitHub API, or None if the repo could not be read."""
    response = github_get(REPO_URL.format(project=project))
    return response.json().get("size", 0) if response is not None else None


def get_repo_language(project) -> str | None:
    """Return the repo's primary language via GitHub API: the language name, "" if GitHub detected
    none, or None if the repo could not be read. Callers retry None, cache "" and names."""
    response = github_get(REPO_URL.format(project=project))
    return (response.json().get("language") or "") if response is not None else None


def merge_file_patches(file_patches):
    """
    Merge multiple file patches into a single patch string.
    Each file patch is a dictionary with (relative_path, hunk_str) pairs.
    """
    merged_patch = []
    for path, hunk in file_patches.items():
        merged_patch.append(f"diff --git a/{path} b/{path}")
        merged_patch.append(f"--- a/{path}")
        merged_patch.append(f"+++ b/{path}")
        merged_patch.append(hunk)
    return "\n".join(merged_patch) + "\n"

def split_to_file_patches(patch: str) -> dict[str, str]:
    """
    Split a multi-file git unified diff string into {relative_path: hunk_str}.
    Raises ValueError if a path changes (rename/copy/create/delete), or headers mismatch.
    """
    lines = patch.splitlines()
    diff_re = re.compile(r'^diff --git a/(.*?) b/(.*?)\s*$')
    n, i, file_patches = len(lines), 0, {}
    while i < n and not lines[i].startswith("diff --git "):
        i += 1
    while i < n:
        m = diff_re.match(lines[i])
        if not m:
            i += 1
            continue
        a_path, b_path = m.groups()
        if a_path != b_path:
            raise ValueError(f"Path changed {a_path} -> {b_path} not allowed.")
        path = a_path
        i += 1
        while i < n and not lines[i].startswith("--- "):
            if (lines[i].startswith("rename from ") or
                lines[i].startswith("rename to ") or
                lines[i].startswith("copy from ") or
                lines[i].startswith("copy to ")):
                raise ValueError(f"Path changed via rename or copy not allowed.")
            i += 1
        if i >= n or not lines[i].startswith("--- "):
            raise ValueError(f"Missing '---' header for {path}")
        old_line = lines[i]
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise ValueError(f"Missing '+++' header for {path}")
        new_line = lines[i]
        i += 1
        old_token, new_token = old_line[4:].split("\t")[0].strip(), \
            new_line[4:].split("\t")[0].strip()
        if old_token == "/dev/null" or new_token == "/dev/null":
            raise ValueError(f"File creation or deletion not allowed.")
        if old_token != f"a/{path}" or new_token != f"b/{path}":
            msg = f"Header paths do not match diff header for {path}: {old_token} , {new_token}"
            raise ValueError(msg)
        start = i
        while i < n and not lines[i].startswith("diff --git "):
            i += 1
        hunk_str = "\n".join(lines[start:i]).rstrip("\n")
        file_patches[path] = hunk_str

    return file_patches
