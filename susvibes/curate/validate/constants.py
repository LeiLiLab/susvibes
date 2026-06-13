from enum import StrEnum


class TestItemStatus(StrEnum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


FAILURE_STATUSES = {TestItemStatus.FAILED, TestItemStatus.ERROR}

LOG_INSTANCE = "validate.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_TIMEOUT = "timeout.json"
LOG_SUMMARY = "summary.json"
