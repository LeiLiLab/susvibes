from enum import Enum

class TestItemStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"
    
class TestStatus(Enum):
    STARTUP_ERROR = "startup_error"
    TIMEOUT = "timeout"
    COMPLETION = "completion"
    
FAILURE_STATUSES = {TestItemStatus.FAILED, TestItemStatus.ERROR}

TEST_STARUP_ERROR_PATTERNS = [
    r"errors? during collection",
    r"ImportError while loading",
    r"Test-module import failures?",  
    r"^Creating test database for alias 'default'.*\.\.\.\s*\r?\nTraceback",
    r"^Destroying test database for alias 'default'.*\.\.\.\s*\r?\nTraceback",
    r"^Destroying test database for alias '.*'.*\.\.\.\s*\r?\nmultiprocessing.pool.RemoteTraceback",
    r"IndentationError: unexpected indent(?:\r?\n)+\Z",
    r"Admin Command Error",
    r"\A\s*Traceback",
    r"\A\s*Testing against Django .*\r?\nTraceback",
    r"compilemessages\r?\nTraceback",
    r"INTERNALERROR>",
    r"UsageError: while parsing", 
    r"error: File not found : None",
    r"^Testing against Django .*?\r?\nTraceback",
    r"EDestroying test database for alias",
    r"Applying .*? OK\r?\nTraceback",
    r"\AE\r?\n",
]
TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS = [
    r"ImportError: cannot import",
    r"AttributeError:.*?attribute", 
    r"NameError: name",
    r"UnboundLocalError:",
    r"TypeError:",
    r"pydantic\..*?ValidationError:",
    r"Unknown keyword argument"
]

AVAILABLE_DEV_TOOL_VERSIONS = {
    "python": ["3.7", "3.8", "3.9", "3.10", "3.11", "3.12"],
}
DOCKERFILE_PATTERN = (
    r'^(FROM[^\r\n]*\r?\n)'
    r'(.*?)'
    r'^(COPY[^\r\n]*\r?\n)'
    r'(.*?)' 
    r'^(CMD[^\r\n]*(?:\r?\n|$))'
)

WORKSPACE_DIR_NAME = "project"
BUILD_DATA_DIR_NAME = "build_data"
PATCHES_DIR_NAME = "patches"
REVERSE_PATCH_FLAG = ("-R", "--reverse")
GIT_AUTHOR_CONFIGS = [
    "git config --global user.email setup@susvibes",
    "git config --global user.name SusVibes"
]
GIT_UNIGNORE_PATTERNS = [
    "!/.git", 
    "!/.git/**", 
    "!.gitignore", 
    "!.gitattributes", 
    "!.gitmodules",
    f"!/{PATCHES_DIR_NAME}",
    f"!/{PATCHES_DIR_NAME}/**",
]

BANNED_REINSTALL_FOR_INSTANCE = {
    "ckan/ckan": [
        "4c22c13"
    ],
    "vyperlang/vyper": [
        "3de1415", "019a37a", "a2df088"
    ],
    "gitpython-developers/gitpython": [
        "ca965ec"
    ]
}