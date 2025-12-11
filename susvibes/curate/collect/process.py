import argparse
import requests
import json
from tqdm import tqdm
from pathlib import Path
from typing import TypedDict

from susvibes.constants import LOCAL_REPOS_DIR
from susvibes.curate.utils import (
    load_file, 
    save_file, 
    get_instance_id, 
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch, 
    commit_changes,
    get_diff_patch,
    len_patch
)
from susvibes.curate.collect.utils import (
    mask_test_funcs,
    merge_file_patches,
    split_to_file_patches
)

TARGET_LANG = "python"
TEST_LANG = "python"
LANG_EXTENSIONS = {
    'python': ['.py'],
    'java': ['.java'],
    'javascript': ['.js'],
    'c': ['.c', '.h'],
    'cpp': ['.cpp', '.hpp', '.cc', '.h'],
    'ruby': ['.rb'],
    'go': ['.go'],
    'rust': ['.rs'],
    'php': ['.php'],
    'typescript': ['.ts', '.tsx'],
    'swift': ['.swift'],
    'html': ['.html', '.htm']
}
TEST_KEYWORD = "test"
INSTALL_TEST_KEYWORDS = ["install", "test", "version", "meta", "setup."]

RECENT_YR_CUTOFF = 2014
PATCH_MAX_LENGTH = 500
PATCH_MAX_FILE_COUNT = 10

root_dir = Path(__file__).parent.parent.parent.parent
PROCESSED_DATASET_PATH = root_dir / 'datasets/processed_dataset.jsonl'
RAW_REPOSVUL_DATASET_PATH = root_dir / f'datasets/cve_records/ReposVul/ReposVul_{TARGET_LANG}.jsonl'
RAW_MOREFIXES_DATASET_PATH = root_dir / 'datasets/cve_records/Morefixes/dataset.jsonl'

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

class ReposVulHandler():
    dataset_path = RAW_REPOSVUL_DATASET_PATH
    cached_remote_status = {}
    
    @classmethod
    def remotely_active(cls, data_record, max_retries=3) -> bool:
        diff_url = data_record['html_url'] + '.patch'
        if diff_url in cls.cached_remote_status:
            return cls.cached_remote_status[diff_url]
        while max_retries > 0:
            max_retries -= 1
            try:
                r = requests.get(diff_url, allow_redirects=True, timeout=10)
                if r.status_code == 200:
                    cls.cached_remote_status[diff_url] = True
                    return True
            except requests.RequestException as e:
                continue
        cls.cached_remote_status[diff_url] = False
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
        print(f"[ReposVul] {len(dataset_filtered)} records collected successfully.")
        return dataset_filtered
 
class MorefixesHandler():
    dataset_path = RAW_MOREFIXES_DATASET_PATH
    target_lang = TARGET_LANG
    test_lang = TEST_LANG
    
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
            is_target_lang, with_test = True, False
            try:
                file_patches = split_to_file_patches(data_record["patch"])
            except ValueError as e:
                continue
            for file_path in file_patches.keys():
                file_path = Path(file_path)
                if file_path.suffix in sum(LANG_EXTENSIONS.values(), []):
                    if TEST_KEYWORD in str(file_path).lower() and \
                        file_path.suffix in LANG_EXTENSIONS.get(cls.test_lang, []):
                        with_test = True
                        continue
                    if file_path.suffix not in LANG_EXTENSIONS.get(cls.target_lang, []):
                        is_target_lang = False
            if with_test and is_target_lang:
                data_record["patch"] = file_patches
                commit = data_record["commits"][0]
                data_record["commit_id"] = commit['commit_sha']
                dataset_filtered.append(data_record)
        print(f"[MoreFixes] {len(dataset_filtered)} records collected successfully.")
        return dataset_filtered
    
def code_test_split(data_record, target_lang, test_lang) -> CVERecord | bool:
    contains_target_lang, with_test = False, False
    code_patch, test_patch, test_files = {}, {}, []
    for file_path, file_patch in data_record['patch'].items():
        file_path = Path(file_path)
        if file_path.suffix in sum(LANG_EXTENSIONS.values(), []):
            if any(keyword in str(file_path).lower() for keyword in INSTALL_TEST_KEYWORDS): #
                test_patch[file_path] = file_patch
                if TEST_KEYWORD in str(file_path).lower() and \
                    file_path.suffix in LANG_EXTENSIONS.get(test_lang, []): #
                    test_files.append(str(file_path))
                    with_test = True
                continue
            code_patch[file_path] = file_patch
            if file_path.suffix in LANG_EXTENSIONS.get(target_lang, []):
                contains_target_lang = True
        else:
            test_patch[file_path] = file_patch

    code_patch = merge_file_patches(code_patch)
    test_patch = merge_file_patches(test_patch)
    num_files, num_lines = len_patch(code_patch)
    if num_lines > PATCH_MAX_LENGTH or num_files > PATCH_MAX_FILE_COUNT:
        raise ValueError(f"Patch exceeds length limits.")
    
    if contains_target_lang and with_test:
        created_at = data_record.get('created_at', data_record.get('commit_date', None))
        project = data_record.get('project', 
            f"{data_record.get('owner', '')}/{data_record.get('repo', '')}")
        base_commit = data_record['commit_id']
        instance_id = get_instance_id(project, base_commit)
        info_page = data_record.get('html_url', 
            data_record.get('repo_url', '') + f"/commit/{base_commit}")
        result_data_record = CVERecord(
            instance_id=instance_id,
            project=project,
            base_commit=data_record['commit_id'],
            security_patch=code_patch,
            test_files=test_files,
            test_patch=test_patch,
            cwe_ids=data_record['cwe_ids'],
            cve_id=data_record['cve_id'],
            created_at=created_at,
            language=target_lang,
            info_page=info_page
        )
        return result_data_record
    elif not contains_target_lang:
        raise ValueError("Patch doesn't contain target language.")
    elif not with_test:
        raise ValueError("Patch doesn't contain test files.")

def process_datasets(dataset_handlers, target_lang, test_lang, max_records = None) -> list[CVERecord]:
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
            lambda r: code_test_split(r, target_lang, test_lang)))
        for data_record in processed_dataset:
            if data_record["instance_id"] not in assembled_by_id:
                assembled_by_id[data_record["instance_id"]] = data_record
    processed_dataset = list(assembled_by_id.values())
    print(f"{len(processed_dataset)} records processed successfully from datasets.")
    if max_records is not None:
        processed_dataset = processed_dataset[:max_records]
    return processed_dataset 

def download_repos_and_verify_patches(processed_dataset, root_dir):
    projects = set(data_record['project'] for data_record in processed_dataset)
    with tqdm(total=len(projects), dynamic_ncols=True) as pbar:
        for project in projects:
            pbar.set_description(f"Cloning {project}")
            try:
                clone_github_repo(project, root_dir, force=False)
            except Exception as e:
                print(f'Error cloning repository {project}: {e}')
            pbar.update(1)
    patch_successfully_applied = []
    for data_record in tqdm(processed_dataset, desc="Verifying patches"):
        repo_dir = get_repo_dir(data_record['project'], root_dir)
        try:
            reset_to_commit(repo_dir, data_record['base_commit'], new_branch=False)
        except Exception as e:
            continue
        is_valid = True
        for patch in [data_record['security_patch'], data_record['test_patch']]:
            assert patch
            try:
                apply_patch(repo_dir, patch, reverse=True)
                apply_patch(repo_dir, patch)
            except Exception as e:
                is_valid = False
                break
        if is_valid:
            patch_successfully_applied.append(data_record)
            
    print(f"{len(patch_successfully_applied)} patches verified successfully.")      
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
            if TEST_KEYWORD in str(file_path).lower() and \
                file_path.suffix in LANG_EXTENSIONS.get(test_lang, []):   
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
                  
    print(f"{len(expanded)} test masks expanded successfully.")
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
        default=None, 
        help='List of handlers to use (JSON format)'
    )
    args = parser.parse_args()

    if args.debug:
        PROCESSED_DATASET_PATH = PROCESSED_DATASET_PATH.with_stem(PROCESSED_DATASET_PATH.stem + '_debug')
        
    if args.use_handlers:
        handler_map = {
            'ReposVulHandler': ReposVulHandler,
            'MorefixesHandler': MorefixesHandler
        }
        dataset_handlers = [handler_map[name] for name in args.use_handlers if name in handler_map]
    else:
        dataset_handlers = (ReposVulHandler, MorefixesHandler)

    processed_dataset = process_datasets(
        dataset_handlers=dataset_handlers, 
        target_lang=TARGET_LANG, 
        test_lang=TEST_LANG,
        max_records=args.max_records
    )
    processed_dataset = download_repos_and_verify_patches(processed_dataset, LOCAL_REPOS_DIR)
    processed_dataset = expand_test_mask(processed_dataset, TEST_LANG)
    save_file(processed_dataset, PROCESSED_DATASET_PATH)