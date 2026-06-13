import argparse
import random
import requests
import json
from tqdm import tqdm
from pathlib import Path
from typing import TypedDict

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir
from susvibes.curate.constants import get_dataset_path
from susvibes.utils import load_file, save_file, get_instance_id, setup_logger
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
    len_patch
)

from susvibes.curate.mine.utils import (
    get_repo_size,
    mask_test_funcs,
    merge_file_patches,
    split_to_file_patches,
    is_test_file,
    path_has_keyword,
)
from susvibes.curate.mine.constants import (
    GITHUB_HEADERS,
    TARGET_LANG,
    LANG_EXTENSIONS,
    INSTALL_TEST_KEYWORDS,
    RECENT_YR_CUTOFF,
    PATCH_MAX_LENGTH,
    PATCH_MAX_FILE_COUNT,
    REPO_MAX_SIZE_KB,
)

logger = None
detail_logger = None

def init_loggers(log_dir):
    global logger, detail_logger
    logger = setup_logger(log_dir, "process.log", f"{__name__}.summary", add_stdout=True, handle_tqdm=True, mode="w")
    detail_logger = setup_logger(log_dir, "process_details.log", f"{__name__}.detail", add_stdout=False, mode="w")

RAW_CVE_RECORDS_DIR = get_dataset_path('cve_records')
RAW_REPOSVUL_DATASET_PATH = RAW_CVE_RECORDS_DIR / f'ReposVul/ReposVul_{TARGET_LANG}.jsonl'
RAW_MOREFIXES_DATASET_PATH = RAW_CVE_RECORDS_DIR / 'Morefixes/dataset_new.jsonl'

class CVERecord(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    security_patch: str
    test_patch: str
    test_files: list[str]
    cwe_id: str
    cve_id: str
    created_at: str
    language: str
    info_page: str 

def is_recent(data_record):
    return int(data_record['cve_id'].split('-')[1]) >= RECENT_YR_CUTOFF

# Cache of remote PR-status lookups; lives with the ReposVul dataset it caches.
LOG_REMOTE_STATUS_CACHE_PATH = RAW_CVE_RECORDS_DIR / "ReposVul" / "remote_status_cache.json"

class ReposVulHandler():
    dataset_path = RAW_REPOSVUL_DATASET_PATH
    cached_remote_status = None

    @classmethod
    def _load_cache(cls):
        if cls.cached_remote_status is not None:
            return
        cache_file = LOG_REMOTE_STATUS_CACHE_PATH
        if cache_file.exists():
            cls.cached_remote_status = json.loads(cache_file.read_text())
        else:
            cls.cached_remote_status = {}

    @classmethod
    def _save_cache(cls):
        LOG_REMOTE_STATUS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_REMOTE_STATUS_CACHE_PATH.write_text(json.dumps(cls.cached_remote_status))

    @classmethod
    def remotely_active(cls, data_record, max_retries=3) -> bool:
        cls._load_cache()
        diff_url = data_record['html_url'] + '.patch'
        if diff_url in cls.cached_remote_status:
            return cls.cached_remote_status[diff_url]
        while max_retries > 0:
            max_retries -= 1
            try:
                r = requests.get(diff_url, headers=GITHUB_HEADERS, allow_redirects=True, timeout=10)
                if r.status_code == 200:
                    cls.cached_remote_status[diff_url] = True
                    cls._save_cache()
                    return True
            except requests.RequestException as e:
                continue
        cls.cached_remote_status[diff_url] = False
        cls._save_cache()
        return False

    @classmethod
    def get_dataset(cls):
        dataset = load_file(cls.dataset_path)
        dataset_filtered = list(filter(cls.remotely_active, filter(is_recent, dataset)))
        for data_record in dataset_filtered:
            data_record["patch"] = {}
            for file_change in data_record["details"]:
                data_record["patch"][file_change["file_name"]] = file_change["patch"]
            data_record["cwe_ids"] = data_record.pop("cwe_id")
        logger.info("[ReposVul] %d records collected successfully.", len(dataset_filtered))
        return dataset_filtered
 
class MorefixesHandler():
    dataset_path = RAW_MOREFIXES_DATASET_PATH
    target_lang = TARGET_LANG
    test_lang = TARGET_LANG
    
    @classmethod
    def get_dataset(cls):
        dataset_text = cls.dataset_path.read_text()
        dataset_crawled = []
        for line in dataset_text.splitlines():
            try:
                data_record = json.loads(line.strip())
            except Exception as e:
                continue
            if data_record["patch"]:
                dataset_crawled.append(data_record)
                
        dataset_filtered = []
        for data_record in dataset_crawled:
            try:
                file_patches = split_to_file_patches(data_record["patch"])
            except ValueError as e:
                continue
            data_record["patch"] = file_patches
            commit = data_record["commits"][0]
            data_record["commit_id"] = commit['commit_sha']
            dataset_filtered.append(data_record)
        logger.info("[MoreFixes] %d records collected successfully.", len(dataset_filtered))
        return dataset_filtered
    
def code_test_split(data_record, target_lang, test_lang, require_test=True) -> CVERecord | bool:
    is_target_lang, with_test = True, False
    code_patch, test_patch, test_files = {}, {}, []
    for file_path, file_patch in data_record['patch'].items():
        file_path = Path(file_path)
        if file_path.suffix in sum(LANG_EXTENSIONS.values(), []):
            if path_has_keyword(file_path, INSTALL_TEST_KEYWORDS):
                test_patch[file_path] = file_patch
                if is_test_file(file_path, LANG_EXTENSIONS.get(test_lang, [])):
                    test_files.append(str(file_path))
                    with_test = True
                continue
            code_patch[file_path] = file_patch
            if file_path.suffix not in LANG_EXTENSIONS.get(target_lang, []):
                is_target_lang = False
        else:
            test_patch[file_path] = file_patch

    if not code_patch:
        raise ValueError("No code patch (all files classified as test/config).")
    code_patch = merge_file_patches(code_patch)
    test_patch = merge_file_patches(test_patch)
    num_files, num_lines = len_patch(code_patch)
    if num_lines > PATCH_MAX_LENGTH or num_files > PATCH_MAX_FILE_COUNT:
        raise ValueError(f"Patch exceeds length limits.")

    if not is_target_lang:
        raise ValueError("Patch doesn't contain target language.")
    if require_test and not with_test:
        raise ValueError("Patch doesn't contain test files.")
    if not require_test and with_test:
        raise ValueError("Patch contains test files.")

    created_at = data_record.get('created_at', data_record.get('commit_date', None))
    project = data_record.get('project',
        f"{data_record.get('owner', '')}/{data_record.get('repo', '')}").lower()
    base_commit = data_record['commit_id']
    instance_id = get_instance_id(project, base_commit)
    info_page = data_record.get('html_url',
        data_record.get('repo_url', '') + f"/commit/{base_commit}")
    result_data_record = dict(
        instance_id=instance_id,
        project=project,
        base_commit=data_record['commit_id'],
        security_patch=code_patch,
        cwe_ids=data_record['cwe_ids'],
        cve_id=data_record['cve_id'],
        created_at=created_at,
        language=target_lang,
        info_page=info_page
    )
    if require_test:
        result_data_record['test_patch'] = test_patch
        result_data_record['test_files'] = test_files
    return result_data_record

def process_datasets(dataset_handlers, target_lang, test_lang, require_test=True, shuffle=False, max_records = None) -> list[CVERecord]:
    def map_filter(iterable, func):
        for item in iterable:
            try:
                result = func(item)
            except ValueError as e:
                continue
            yield result  
    assembled_by_id = {}
    for handler in dataset_handlers:
        raw_cve_dataset = handler.get_dataset()                
        processed_dataset = list(map_filter(raw_cve_dataset, 
            lambda r: code_test_split(r, target_lang, test_lang, require_test)))
        for data_record in processed_dataset:
            if data_record["instance_id"] not in assembled_by_id:
                assembled_by_id[data_record["instance_id"]] = data_record
    processed_dataset = list(assembled_by_id.values())
    logger.info("%d records processed successfully from datasets.", len(processed_dataset))
    if shuffle:
        random.shuffle(processed_dataset)
    if max_records is not None:
        processed_dataset = processed_dataset[:max_records]
    return processed_dataset 

def download_repos_and_verify_patches(processed_dataset, root_dir, require_test=True):
    projects = set(data_record['project'] for data_record in processed_dataset)
    skipped_projects = set()
    with tqdm(total=len(projects), dynamic_ncols=True) as pbar:
        for project in projects:
            pbar.set_description(f"Cloning {project}")
            repo_size = get_repo_size(project)
            if repo_size is not None and repo_size > REPO_MAX_SIZE_KB:
                logger.warning("Skipping %s: repo size %.1f GB exceeds limit", project, repo_size / 1024 / 1024)
                skipped_projects.add(project)
                pbar.update(1)
                continue
            try:
                clone_github_repo(project, root_dir, force=False)
            except Exception as e:
                logger.error("Error cloning repository %s: %s", project, e)
                skipped_projects.add(project)
            pbar.update(1)
    patch_successfully_applied = []
    for data_record in tqdm(processed_dataset, desc="Verifying patches"):
        instance_id = data_record['instance_id']
        if data_record['project'] in skipped_projects:
            detail_logger.warning("%s skipped: project not cloned or too large", instance_id)
            continue
        repo_dir = get_repo_dir(data_record['project'], root_dir)
        try:
            reset_to_commit(repo_dir, data_record['base_commit'], new_branch=False)
        except Exception as e:
            detail_logger.error("%s reset_to_commit failed: %s", instance_id, e)
            continue
        is_valid = True
        patches_to_verify = [("security_patch", data_record['security_patch'])]
        if require_test:
            patches_to_verify.append(("test_patch", data_record['test_patch']))
        for patch_name, patch in patches_to_verify:
            assert patch
            try:
                apply_patch(repo_dir, patch, reverse=True)
                apply_patch(repo_dir, patch)
            except Exception as e:
                detail_logger.error("%s apply_patch (%s) failed: %s", instance_id, patch_name, e)
                is_valid = False
                break
        if is_valid:
            patch_successfully_applied.append(data_record)

    logger.info("%d patches verified successfully.", len(patch_successfully_applied))
    return patch_successfully_applied

def expand_test_mask(processed_dataset, test_lang):
    expanded = []
    for data_record in tqdm(processed_dataset, desc="Making test masks"):
        is_syntax_error = False
        base_commit = data_record["base_commit"]
        repo_dir = get_repo_dir(data_record['project'], LOCAL_REPOS_DIR)
        reset_to_commit(repo_dir, base_commit, new_branch=False)
        test_patch = split_to_file_patches(data_record["test_patch"])
        for file_path, file_patch in test_patch.items():
            file_path = Path(file_path)
            if is_test_file(file_path, LANG_EXTENSIONS.get(test_lang, [])):
                code_after = load_file(repo_dir / file_path)
                apply_patch(repo_dir, merge_file_patches({file_path: file_patch}), reverse=True)
                code_before = load_file(repo_dir / file_path)
                try:
                    mask_patch = mask_test_funcs(file_patch, code_before, code_after)
                except ValueError as e:
                    is_syntax_error = True
                    break
                if mask_patch.strip():
                    apply_patch(repo_dir, merge_file_patches({file_path: mask_patch}))
            else:
                apply_patch(repo_dir, merge_file_patches({file_path: file_patch}), reverse=True)
                    
        if not is_syntax_error:
            test_mask_commit = commit_changes(repo_dir, f'Test mask at {base_commit}')
            data_record["test_patch"] = get_diff_patch(repo_dir, test_mask_commit, base_commit)
            expanded.append(data_record)
                  
    logger.info("%d test masks expanded successfully.", len(expanded))
    return expanded

    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--debug', 
        action='store_true', 
        help='Use debug dataset path'
    )
    parser.add_argument(
        '--max_records', 
        type=int, 
        default=None, 
        help='Maximum number of records to process'
    )
    parser.add_argument(
        '--use_handlers', 
        type=json.loads, 
        default=[], 
        help='List of handlers to use (JSON format)'
    )
    parser.add_argument(
        '--require_test',
        type=json.loads,
        default=True,
        help='Require repo-provided test files (default True); false keeps only records without tests.'
    )
    parser.add_argument(
        '--shuffle',
        action='store_true',
        help='Randomly shuffle records before applying max_records'
    )
    parser.add_argument(
        '--run_id',
        type=str,
        default='default',
        help='Run ID for output subdirectory (datasets/<run_id>/...)'
    )
    args = parser.parse_args()

    mine_log_dir = get_log_dir(args.run_id, "mine", "process")
    init_loggers(mine_log_dir)

    processed_dataset_path = get_dataset_path('processed_dataset', args.run_id)

    if args.use_handlers:
        handler_map = {
            'ReposVulHandler': ReposVulHandler,
            'MorefixesHandler': MorefixesHandler
        }
        dataset_handlers = [handler_map[name] for name in args.use_handlers if name in handler_map]
    else:
        dataset_handlers = (ReposVulHandler, MorefixesHandler)

    require_test = args.require_test
    processed_dataset = process_datasets(
        dataset_handlers=dataset_handlers,
        target_lang=TARGET_LANG,
        test_lang=TARGET_LANG,
        require_test=require_test,
        shuffle=args.shuffle,
        max_records=args.max_records
    )
    processed_dataset = download_repos_and_verify_patches(processed_dataset, LOCAL_REPOS_DIR, require_test)
    if require_test:
        processed_dataset = expand_test_mask(processed_dataset, TARGET_LANG)
    processed_dataset_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(processed_dataset, processed_dataset_path)
    logger.info("Logs saved to %s", mine_log_dir)
    print(f"Processed dataset saved to {processed_dataset_path}.")