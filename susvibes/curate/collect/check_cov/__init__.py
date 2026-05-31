"""Static file-level test-coverage analysis (see core.py / check_cov.md)."""
from susvibes.curate.collect.check_cov.constants import (
    CoverageLabel, CoverageResult, LABEL_RANK,
)
from susvibes.curate.collect.check_cov.core import (
    analyze,
    check_cov_single,
    check_cov_threadpool,
    get_cov_summary,
    print_cov_summary,
)

__all__ = [
    "CoverageLabel", "CoverageResult", "LABEL_RANK",
    "analyze", "check_cov_single", "check_cov_threadpool",
    "get_cov_summary", "print_cov_summary",
]
