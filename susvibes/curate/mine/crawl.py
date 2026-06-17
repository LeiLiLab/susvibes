import time
import argparse
import requests

from tqdm import tqdm

from susvibes.curate.constants import get_dataset_path
from susvibes.curate.mine.constants import GITHUB_HEADERS
from susvibes.core.utils import load_file, save_file

RECENT_YR_CUTOFF = 2014

RAW_MOREFIXES_DIR = get_dataset_path('cve_records') / 'Morefixes'
URL_DATASET_FILE_NAME = "dataset_url.jsonl"
DATASET_FILE_NAME = "dataset.jsonl"

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch GitHub commit patches for a Morefixes URL dataset."
    )
    parser.add_argument(
        "--input_file",
        type=str,
        default=URL_DATASET_FILE_NAME,
        help="Input URL-dataset filename under the Morefixes directory.",
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default=DATASET_FILE_NAME,
        help="Output dataset filename under the Morefixes directory.",
    )
    args = parser.parse_args()

    url_dataset = load_file(RAW_MOREFIXES_DIR / args.input_file)
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
    save_file(dataset, RAW_MOREFIXES_DIR / args.output_file)