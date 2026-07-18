"""The shared record shape every source funnels into.

A source yields a raw CVE record (`{cve_id, cwe_ids, commit_id, patch:{path:hunk}, project
or owner+repo, ...}`); `code_test_split` splits its patch into a `security_patch` (target-
language source hunks) and a `test_patch`, applies the language / length / test-presence
gates, and emits a `CVERecord` — or raises `ValueError` to reject the record. Lifted out of
`process.py` so both `process.py` and the `sources` package import it without a cycle.
"""

from pathlib import Path
from typing import TypedDict

from susvibes.core.utils import get_instance_id
from susvibes.curate.utils import len_patch
from susvibes.curate.mine.utils import merge_file_patches, is_test_file, path_has_keyword
from susvibes.curate.mine.constants import (
    LANG_EXTENSIONS,
    INSTALL_TEST_KEYWORDS,
    PATCH_MAX_LENGTH,
    PATCH_MAX_FILE_COUNT,
)


class CVERecord(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    security_patch: str
    test_patch: str
    test_files: list[str]
    cwe_id: str
    cve_id: str
    cve_fix_date: str
    language: str
    info_page: str


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

    cve_fix_date = data_record.get('commit_date')
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
        cve_fix_date=cve_fix_date,
        language=target_lang,
        info_page=info_page
    )
    if require_test:
        result_data_record['test_patch'] = test_patch
        result_data_record['test_files'] = test_files
    return result_data_record
