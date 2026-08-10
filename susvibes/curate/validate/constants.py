from susvibes.curate.constants import KeepStage
from enum import StrEnum


class ValidationRejected(RuntimeError):
    """A validation check rejected this instance — a verdict about the instance, not the harness
    breaking. Raised so the caller sorts the two by exception type instead of by re-reading the
    message; every "Failed to validate ..." check raises this."""


class ValidateStatus(StrEnum):
    """How validation of one instance concluded. Every member is a NORMAL outcome — the harness
    breaking is not a status but the report's `error` (see core/report.py), which is also the only
    thing `--resume` re-runs."""
    VALIDATED = "validated"                 # meets every requirement — expected_pf recorded
    INVALID = "invalid"                     # a validation check rejected it — `reason` says which
    TEST_PATCH_ERROR = "test_patch_error"   # the test_patch does not apply
    EMPTY_TEST_PATCH = "empty_test_patch"   # nothing to validate

KEEP_STAGE = KeepStage.VALIDATE   # this stage's verdict under record["keep"]

LOG_INSTANCE = "validate.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"     # each run's raw logs, evidence beside the report
LOG_REPORT = "report.json"
LOG_SUMMARY = "summary.json"
