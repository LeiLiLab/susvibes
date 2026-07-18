import argparse
import random
import json
from tqdm import tqdm
from pathlib import Path

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import load_file, save_file, setup_logger
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
    get_commit_date,
)

from susvibes.curate.mine.utils import (
    get_repo_size,
    mask_test_funcs,
    merge_file_patches,
    split_to_file_patches,
    is_test_file,
)
from susvibes.curate.mine.constants import (
    TARGET_LANG,
    LANG_EXTENSIONS,
    REPO_MAX_SIZE_KB,
)
from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.cve_record import CVERecord, code_test_split
from susvibes.curate.mine.sources import SOURCES, SOURCE_BY_NAME

logger = None
detail_logger = None

def init_loggers(log_dir):
    global logger, detail_logger
    logger = setup_logger(log_dir, "process.log", f"{__name__}.summary", add_stdout=True, handle_tqdm=True, mode="w")
    detail_logger = setup_logger(log_dir, "process_details.log", f"{__name__}.detail", add_stdout=False, mode="w")

def process_datasets(sources, target_lang, test_lang, require_test=True, shuffle=False, max_records = None) -> list[CVERecord]:
    def map_filter(iterable, func):
        for item in iterable:
            try:
                result = func(item)
            except ValueError as e:
                continue
            yield result
    known = KnownSet()
    accepted = []
    collapsed = 0
    for source in sources:
        raw_cve_dataset = list(source.records(known))
        logger.info("[%s] %d records collected successfully.", source.name, len(raw_cve_dataset))
        processed_dataset = list(map_filter(raw_cve_dataset,
            lambda r: code_test_split(r, target_lang, test_lang, require_test)))
        net_new_start = len(accepted)
        for data_record in processed_dataset:
            if known.has_sha(data_record["base_commit"]):
                collapsed += 1
                continue
            known.add(data_record)
            accepted.append(data_record)
        logger.info("[%s] +%d net-new instances (running total %d).", source.name, len(accepted) - net_new_start, len(accepted))
    processed_dataset = accepted
    logger.info("%d records processed successfully from datasets (%d duplicate commits collapsed).", len(processed_dataset), collapsed)
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
        if not data_record.get('cve_fix_date'):
            data_record['cve_fix_date'] = get_commit_date(repo_dir, data_record['base_commit'])
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
        dataset_sources = [SOURCE_BY_NAME[name] for name in args.use_handlers if name in SOURCE_BY_NAME]
    else:
        dataset_sources = SOURCES

    require_test = args.require_test
    processed_dataset = process_datasets(
        sources=dataset_sources,
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