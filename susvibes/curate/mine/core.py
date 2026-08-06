import argparse
import os
import random
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from pathlib import Path
from typing import TypedDict, NotRequired

from susvibes.core.constants import get_dataset_path
from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir
from susvibes.core.utils import load_file, save_file, setup_logger, get_instance_id
from susvibes.curate.utils import (
    get_repo_dir,
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
from susvibes.curate.mine.clone import full_clone, fetch_commit
from susvibes.curate.mine.dedup import KnownSet
from susvibes.curate.mine.sources import SOURCES, SOURCE_BY_NAME

CLONE_WORKERS = 8

logger = None

def init_loggers(core_log_dir):
    global logger
    logger = setup_logger(core_log_dir, "core.log", __name__, add_stdout=True, handle_tqdm=True, mode="w")


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
        logger.info("[%s] %d raw cves collected successfully.", source.name, len(raw_cve_dataset))
        fix_dataset = list(map_filter(raw_cve_dataset,
            lambda r: code_test_split(r, target_lang, test_lang, require_test)))
        net_new_start = len(accepted)
        for data_record in fix_dataset:
            if known.has(data_record["cve_id"], data_record["base_commit"]):
                collapsed += 1
                continue
            known.add(data_record)
            accepted.append(data_record)
        logger.info("[%s] +%d net-new instances (running total %d).", source.name, len(accepted) - net_new_start, len(accepted))
    fix_dataset = accepted
    logger.info("%d records processed successfully from datasets (%d duplicates collapsed — same commit or same CVE).", len(fix_dataset), collapsed)
    if shuffle:
        random.shuffle(fix_dataset)
    if max_records is not None:
        fix_dataset = fix_dataset[:max_records]
    return fix_dataset

def clone_repo_single(project, root_dir) -> str | None:
    """Clone one project's repo, unless it is too large to be worth curating. Returns None once the
    repo is on disk, or the reason it is not — the driver only needs to know which projects to drop,
    the detail is already logged here."""
    repo_size = get_repo_size(project)
    if repo_size is not None and repo_size > REPO_MAX_SIZE_KB:
        logger.warning("Skipping %s: repo size %.1f GB exceeds limit", project, repo_size / 1024 / 1024)
        return "repo too large"
    try:
        full_clone(project, root_dir)
    except Exception as e:
        logger.error("Error cloning repository %s: %s", project, e)
        return "clone failed"
    return None


def clone_repo_threadpool(projects, root_dir, max_workers=CLONE_WORKERS) -> set:
    """Clone every project concurrently and return the ones that are not on disk afterwards.

    Each project owns a distinct clone dir (`owner__repo`), so the writes never overlap and no
    per-repo lock is needed. Cloning is dominated by per-repo latency rather than bandwidth — the
    repos are small (median ~29 MB) and most time goes to GitHub's pack build — which is what makes
    the concurrency pay off."""
    skipped_projects, cloned = set(), 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(clone_repo_single, project, root_dir): project
                   for project in projects}
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Cloning repos [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                project = futures[future]
                try:
                    reason = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {project}: {e}")
                if reason is None:
                    cloned += 1
                else:
                    skipped_projects.add(project)
                pbar.update(1)
                pbar.set_description(f"{cloned} cloned, {len(skipped_projects)} skipped")
    logger.info("%d repos cloned successfully (%d skipped).", cloned, len(skipped_projects))
    return skipped_projects


def validate_patches(fix_dataset, root_dir, skipped_projects):
    """Keep the records whose patches really apply against their `base_commit`: roll the clone to
    that commit and reverse- then forward-apply each patch. Records under `skipped_projects` (what
    `clone_repo_threadpool` could not put on disk) have no tree to validate against and are dropped."""
    patch_validated = []
    for data_record in tqdm(fix_dataset, desc="Validating patches"):
        instance_id = data_record['instance_id']
        base_commit = data_record['base_commit']
        if data_record['project'] in skipped_projects:
            logger.warning("%s skipped: project not cloned or too large", instance_id)
            continue
        repo_dir = get_repo_dir(data_record['project'], root_dir)
        try:
            reset_to_commit(repo_dir, base_commit, new_branch=False)
        except Exception:
            # A fix commit no ref reaches is missing from the clone however current it is;
            # fetching it by sha recovers it (see fetch_commit).
            try:
                fetch_commit(repo_dir, base_commit)
                reset_to_commit(repo_dir, base_commit, new_branch=False)
            except Exception as e:
                logger.error("%s reset_to_commit failed: %s", instance_id, e)
                continue
        if not data_record.get('cve_fix_date'):
            data_record['cve_fix_date'] = get_commit_date(repo_dir, base_commit)
        is_valid = True
        patches_to_verify = [("security_patch", data_record['security_patch'])]
        if 'test_patch' in data_record:
            patches_to_verify.append(("test_patch", data_record['test_patch']))
        for patch_name, patch in patches_to_verify:
            try:
                apply_patch(repo_dir, patch, reverse=True)
                apply_patch(repo_dir, patch)
            except Exception as e:
                logger.error("%s apply_patch (%s) failed: %s", instance_id, patch_name, e)
                is_valid = False
                break
        if is_valid:
            patch_validated.append(data_record)

    logger.info("%d patches validated successfully.", len(patch_validated))
    return patch_validated

def expand_tests(fix_dataset, test_lang):
    """Rewrite every with-test record's `test_patch` into a test *mask*: the diff that puts back the
    test functions the fix commit touched, so the task can ship a tree with them removed. A record
    carrying no `test_patch` has no security test to hide and passes through untouched — which is
    what makes this safe to run over a combined (`--require_test` omitted) dataset."""
    kept, expanded = [], 0
    for data_record in tqdm(fix_dataset, desc="Making test masks"):
        if "test_patch" not in data_record:
            kept.append(data_record)
            continue
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
                    test_mask_patch = mask_test_funcs(file_patch, code_before, code_after)
                except ValueError as e:
                    is_syntax_error = True
                    break
                if test_mask_patch.strip():
                    apply_patch(repo_dir, merge_file_patches({file_path: test_mask_patch}))
            else:
                apply_patch(repo_dir, merge_file_patches({file_path: file_patch}), reverse=True)

        if not is_syntax_error:
            test_mask_commit = commit_changes(repo_dir, f'Test mask at {base_commit}')
            data_record["test_patch"] = get_diff_patch(repo_dir, test_mask_commit, base_commit)
            kept.append(data_record)
            expanded += 1

    logger.info("%d test masks expanded successfully.", expanded)
    return kept


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
        help='JSON list of source names whose cache to rebuild from scratch before reading, e.g. \'["OSVSource"]\' (every source supports it); others read their cache, building only if missing.'
    )
    parser.add_argument(
        "--resume",
        type=json.loads,
        default=[],
        help='JSON list of discovery-source names to resume, e.g. \'["OSVResidualSource"]\' (only discovery sources support it): re-run just the finder outcomes that errored, keeping concluded pins and misses. If a source is in both --force and --resume, --force wins (rebuild-all supersedes rerun-errors).'
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="default",
        help="Run ID for output subdirectory (datasets/<run_id>/...)"
    )
    args = parser.parse_args()

    # The funnel only ever reads source text, so a clone or checkout must not stop to pull a repo's
    # LFS assets: git-lfs fails the whole `git reset --hard` when a download does (jumpserver's
    # 74 MB GeoIP database), and the pointer files it leaves instead cost the funnel nothing.
    os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"

    core_log_dir = get_log_dir(args.run_id, "mine", "core")
    init_loggers(core_log_dir)

    fix_dataset_path = get_dataset_path('fix_dataset', args.run_id)

    if args.use_handlers:
        dataset_sources = [SOURCE_BY_NAME[name] for name in args.use_handlers if name in SOURCE_BY_NAME]
    else:
        dataset_sources = SOURCES

    for name in args.force:
        SOURCE_BY_NAME[name].force = True

    for name in args.resume:
        source = SOURCE_BY_NAME.get(name)
        if source is None or not hasattr(source, "resume"):
            resumable = [n for n, s in SOURCE_BY_NAME.items() if hasattr(s, "resume")]
            parser.error(f"--resume: {name} is not a resume-capable source; supported: {resumable}")
        source.resume = True

    for source in dataset_sources:              # discovery sources route their finder trajectories
        if hasattr(source, "run_id"):
            source.run_id = args.run_id

    fix_dataset = build_fix_dataset(
        sources=dataset_sources,
        target_lang=TARGET_LANG,
        test_lang=TARGET_LANG,
        require_test=args.require_test,
        shuffle=args.shuffle,
        max_records=args.max_records
    )
    if args.skip_verify:
        logger.info("--skip_verify: saving %d text-level (unverified) records; skipping clone, apply-verify, and test-mask expansion.", len(fix_dataset))
    else:
        projects = set(data_record["project"] for data_record in fix_dataset)
        skipped_projects = clone_repo_threadpool(projects, LOCAL_REPOS_DIR)
        fix_dataset = validate_patches(fix_dataset, LOCAL_REPOS_DIR, skipped_projects)
        fix_dataset = expand_tests(fix_dataset, TARGET_LANG)
    fix_dataset_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(fix_dataset, fix_dataset_path)
    print(f"Fix dataset saved to {fix_dataset_path}.")
    print(f"Logs saved to {core_log_dir}.")