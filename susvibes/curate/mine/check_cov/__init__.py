"""Static file-level test-coverage analysis (see core.py / check_cov.md)."""
from susvibes.curate.mine.check_cov.engine.constants import (
    CoverageLabel, CoverageResult, LABEL_RANK,
)
from susvibes.curate.mine.check_cov.engine.analyze import analyze
from susvibes.curate.mine.check_cov.core import (
    check_cov_single,
    check_cov_threadpool,
)

__all__ = [
    "CoverageLabel", "CoverageResult", "LABEL_RANK",
    "analyze", "check_cov_single", "check_cov_threadpool",
]
