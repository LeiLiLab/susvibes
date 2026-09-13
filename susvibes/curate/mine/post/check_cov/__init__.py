"""Static file-level test-coverage analysis (see core.py / README.md)."""
from susvibes.curate.mine.post.check_cov.engine.constants import (
    CoverageLabel, CoverageResult, LABEL_RANK,
)
from susvibes.curate.mine.post.check_cov.engine.analyze import analyze
from susvibes.curate.mine.post.check_cov.core import (
    check_cov_single,
    check_cov_threadpool,
)

__all__ = [
    "CoverageLabel", "CoverageResult", "LABEL_RANK",
    "analyze", "check_cov_single", "check_cov_threadpool",
]
