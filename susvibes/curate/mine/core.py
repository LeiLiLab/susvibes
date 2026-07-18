import argparse
import random
import re
import json
from tqdm import tqdm
from pathlib import Path
from typing import TypedDict, NotRequired

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import load_file, save_file, setup_logger, get_instance_id
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    apply_patch,
    commit_changes,
    get_diff_patch,
    get_commit_date,
    len_patch,
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
    TARGET_LANG,
    LANG_EXTENSIONS,
    REPO_MAX_SIZE_KB,
    INSTALL_TEST_KEYWORDS,
    PATCH_MAX_LENGTH,
    PATCH_MAX_FILE_COUNT,
)
from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources import SOURCES, SOURCE_BY_NAME

logger = None
detail_logger = None

def init_loggers(core_log_dir):
    global logger, detail_logger
    logger = setup_logger(core_log_dir, "core.log", f"{__name__}.summary", add_stdout=True, handle_tqdm=True, mode="w")
    detail_logger = setup_logger(core_log_dir, "core_details.log", f"{__name__}.detail", add_stdout=False, mode="w")


class CVEFixRecord(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    security_patch: str
    cwe_ids: list[str]
    cve_id: str
    cve_fix_date: str
    language: str
    info_page: str
    test_patch: NotRequired[str]        # present iff the fix ships a test
    test_files: NotRequired[list[str]]


def code_test_split(data_record, target_lang, test_lang, require_test=None) -> CVEFixRecord:
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
    if require_test is True and not with_test:
        raise ValueError("Patch doesn't contain test files.")
    if require_test is False and with_test:
        raise ValueError("Patch contains test files.")

    cve_fix_date = data_record.get('commit_date')
    project = data_record.get('project',
        f"{data_record.get('owner', '')}/{data_record.get('repo', '')}").lower()
    base_commit = data_record['commit_id']
    if not re.fullmatch(r'[0-9a-f]{40}', base_commit):
        raise ValueError("base_commit is not a full 40-char commit.")
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
        cve_fix_date=cve_fix_date,
        language=target_lang,
        info_page=info_page
    )
    if with_test:
        result_data_record['test_patch'] = test_patch
        result_data_record['test_files'] = test_files
    return result_data_record


def build_fix_dataset(sources, target_lang, test_lang, require_test=None, shuffle=False, max_records = None) -> list[CVEFixRecord]:
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
        fix_dataset = list(map_filter(raw_cve_dataset,
            lambda r: code_test_split(r, target_lang, test_lang, require_test)))
        net_new_start = len(accepted)
        for data_record in fix_dataset:
            if known.has_commit(data_record["base_commit"]):
                collapsed += 1
                continue
            known.add(data_record)
            accepted.append(data_record)
        logger.info("[%s] +%d net-new instances (running total %d).", source.name, len(accepted) - net_new_start, len(accepted))
    fix_dataset = accepted
    logger.info("%d records processed successfully from datasets (%d duplicate commits collapsed).", len(fix_dataset), collapsed)
    if shuffle:
        random.shuffle(fix_dataset)
    if max_records is not None:
        fix_dataset = fix_dataset[:max_records]
    return fix_dataset

def clone_repos_and_verify_fixes(fix_dataset, root_dir):
    projects = set(data_record['project'] for data_record in fix_dataset)
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
    for data_record in tqdm(fix_dataset, desc="Verifying patches"):
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
        if 'test_patch' in data_record:
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

def expand_test_mask(fix_dataset, test_lang):
    expanded = []
    for data_record in tqdm(fix_dataset, desc="Making test masks"):
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
        "--debug",
        action="store_true",
        help="Use debug dataset path"
    )
    parser.add_argument(
        "--max_records",
        type=int,
        default=None,
        help="Maximum number of records to process"
    )
    parser.add_argument(
        "--use_handlers",
        type=json.loads,
        default=[],
        help="List of handlers to use (JSON format)"
    )
    parser.add_argument(
        "--require_test",
        type=json.loads,
        default=None,
        help="true keeps only records with a repo test, false only those without; omit to keep both (a record carries test_patch iff the fix ships a test)."
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomly shuffle records before applying max_records"
    )
    parser.add_argument(
        "--skip_verify",
        action="store_true",
        help="Stop after the text-level funnel: save the unverified records and skip repo clone, patch apply-verify, and test-mask expansion."
    )
    parser.add_argument(
        "--force",
        type=json.loads,
        default=[],
        help='JSON list of source names whose cache to rebuild (fetch) before reading, e.g. \'["OSVSource"]\'; others read their cache, building only if missing.'
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)"
    )
    args = parser.parse_args()

    core_log_dir = get_log_dir(args.run_id, "mine", "core")
    init_loggers(core_log_dir)

    fix_dataset_path = get_dataset_path('fix_dataset', args.run_id)

    if args.use_handlers:
        dataset_sources = [SOURCE_BY_NAME[name] for name in args.use_handlers if name in SOURCE_BY_NAME]
    else:
        dataset_sources = SOURCES

    for name in args.force:
        SOURCE_BY_NAME[name].force = True

    require_test = args.require_test
    fix_dataset = build_fix_dataset(
        sources=dataset_sources,
        target_lang=TARGET_LANG,
        test_lang=TARGET_LANG,
        require_test=require_test,
        shuffle=args.shuffle,
        max_records=args.max_records
    )
    if args.skip_verify:
        logger.info("--skip_verify: saving %d text-level (unverified) records; skipping clone, apply-verify, and test-mask expansion.", len(fix_dataset))
    else:
        fix_dataset = clone_repos_and_verify_fixes(fix_dataset, LOCAL_REPOS_DIR)
        if require_test:
            fix_dataset = expand_test_mask(fix_dataset, TARGET_LANG)
    fix_dataset_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(fix_dataset, fix_dataset_path)
    print(f"Fix dataset saved to {fix_dataset_path}.")
    print(f"Logs saved to {core_log_dir}.")