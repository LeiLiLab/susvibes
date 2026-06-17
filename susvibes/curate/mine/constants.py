"""Constants for the collect stage.

Single source of truth for language / file-classification settings shared by
collect.process (patch classification), collect.utils (test detection), and
collect.check_cov (repo test-suite discovery & module mapping), so all three
agree on the target language, its file extensions, and what counts as a "test".
"""

import os
from dotenv import load_dotenv

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

TARGET_LANG = "python"
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
    'html': ['.html', '.htm'],
}
# Default extensions for the target language (e.g. ('.py',)).
TARGET_EXTENSIONS = tuple(LANG_EXTENSIONS[TARGET_LANG])

# A test is detected by splitting the path on "/", "_" and "." and matching any
# token against these keywords — so a test directory ("test/a.py") and a test
# filename ("test_x.py", "x_test.py", "conftest.py") are both caught, while
# "latest.py" / "contest.py" are not (no substring matching).
TEST_KEYWORDS = ["tests", "test", "testing", "testsuite", "conftest"]

# Keywords (same token matching) that route a code-extension file into
# test/config rather than the security_patch. A strict superset of TEST_KEYWORDS,
# so every test file is also routed out of the security_patch.
INSTALL_TEST_KEYWORDS = ["install", "version", "meta", "setup"] + TEST_KEYWORDS

# Patch-size limits for an acceptable security_patch (process.py).
RECENT_YR_CUTOFF = 2014
PATCH_MAX_LENGTH = 500
PATCH_MAX_FILE_COUNT = 10
REPO_MAX_SIZE_KB = 2 * 1024 * 1024  # 2 GB

# check_cov's own scoring/engine tuning lives in collect/check_cov/constants.py.
