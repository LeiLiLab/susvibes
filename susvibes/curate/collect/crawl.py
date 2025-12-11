import os
import time
import requests

from tqdm import tqdm
from pathlib import Path
from dotenv import load_dotenv

from susvibes.curate.utils import load_file, save_file

load_dotenv()
token = os.getenv("GITHUB_TOKEN")

RECENT_YR_CUTOFF = 2014

root_dir = Path(__file__).parent.parent.parent.parent
RAW_MORE_FIXES_DIR = root_dir / 'datasets/cve_records/Morefixes'
URL_DATASET_NAME = "dataset_url.jsonl"
DATASET_NAME = "dataset.jsonl"

def fetch_github_commit_patch(owner: str, repo: str, sha: str,
    timeout: int = 10, max_retries: int = 3) -> str:
    """
    Fetch a commit's unified patch from GitHub. Tries REST API, 
    then falls back to the public HTML .patch URL.
    Returns the patch text (unified diff format).
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "morefixes-tools/patch-fetch",
        "Accept": "application/vnd.github.patch",  # ask API to return patch
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

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
    try:
        r2 = requests.get(html_patch_url, timeout=timeout, headers={"User-Agent": "morefixes-tools/patch-fetch"})
        if r2.status_code == 200 and r2.text.strip():
            return r2.text
        last_err = f"HTML .patch status {r2.status_code}"
    except requests.RequestException as e:
        last_err = f"HTML .patch error: {e}"

    print(f"Failed to fetch patch for {owner}/{repo}@{sha}: {last_err}")
    return None

if __name__ == "__main__":
    url_dataset = load_file(RAW_MORE_FIXES_DIR / URL_DATASET_NAME)
    dataset = []
    for data_record in url_dataset:
        if int(data_record['cve_id'].split('-')[1]) >= RECENT_YR_CUTOFF and len(data_record['commits']) == 1:
            dataset.append(data_record)
    for data_record in tqdm(dataset, total=len(dataset), desc="Fetching patches"):
        if "patch" not in data_record:
            data_record["patch"] = fetch_github_commit_patch(
                owner=data_record["owner"],
                repo=data_record["repo"],
                sha=data_record["commits"][0]["commit_sha"],
            )
    save_file(dataset, RAW_MORE_FIXES_DIR / DATASET_NAME)