"""Constants for the collect stage.

Single source of truth for language / file-classification settings shared by
collect.process (patch classification), collect.utils (test detection), and
collect.check_cov (repo test-suite discovery & module mapping), so all three
agree on the target language, its file extensions, and what counts as a "test".
"""

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
# test/config rather than the security patch. A strict superset of TEST_KEYWORDS,
# so every test file is also routed out of the security patch.
INSTALL_TEST_KEYWORDS = ["install", "version", "meta", "setup"] + TEST_KEYWORDS

# Source roots check_cov maps repo files into dotted module names from.
SOURCE_ROOTS = ["", "src", "lib", "python"]

# Patch-size limits for an acceptable security patch (process.py).
RECENT_YR_CUTOFF = 2014
PATCH_MAX_LENGTH = 500
PATCH_MAX_FILE_COUNT = 10
REPO_MAX_SIZE_KB = 2 * 1024 * 1024  # 2 GB

# --- check_cov tuning (static test-coverage analysis) -------------------------
# Score thresholds for the per-file coverage label.
COV_LIKELY_THRESHOLD = 0.80
COV_MAYBE_THRESHOLD = 0.55
# Max depth of the file import-graph BFS for indirect coverage evidence. The edge
# is "file A imports file B", so a test that reaches the target within this many
# hops counts as covered. Deeper = higher recall, more false positives.
IMPORT_GRAPH_MAX_DEPTH = 8
# Max hops from a test at which a production file's string reference to the target
# module (dynamic / registry-style wiring, e.g. importlib.import_module) still
# counts as evidence (L7b). Kept shallower than the import-graph depth: this is a
# string-match heuristic, so allowing it arbitrarily deep raises false positives.
DYNAMIC_WIRING_MAX_DEPTH = 4
# Fraction of target files that must be unparseable (with no other evidence)
# before the instance is downgraded to `unknown` (don't go unknown too eagerly).
MAX_TARGET_PARSE_FAIL_RATIO = 0.5
# A repo-defined symbol counts as "distinctive" evidence (a test referencing it
# implies coverage) only when it is defined at most this many times across the
# whole repo and is at least this long — uniqueness, not a hand-maintained
# stop-list, filters out ubiquitous names like get/run/main.
SYMBOL_MAX_DEFCOUNT = 1
SYMBOL_MIN_LEN = 4
