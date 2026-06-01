"""Coverage labels, result shape, and tuning constants for check_cov.

Following the repo convention (e.g. ``env_specs/constants.py``), a module's
labels/enums, result types, and tunables all live in its ``constants.py``.

Two parts:
  - DATA SHAPE: the coverage label enum, the result TypedDict, and the small
    helpers that build/serialize results.
  - TUNING: scoring thresholds and per-engine knobs. A constant is prefixed by
    the engine it belongs to — ``SYMBOL_``/``JEDI_``/``GET_REFERENCES_`` for the
    jedi symbol trace, ``FILE_TRACE_``/``IMPORT_GRAPH_`` for the file-level
    approximation, ``DYNAMIC_WIRING_`` for the heuristics, ``COV_`` for the
    shared classifier.

Language / file-classification settings shared with ``process``
(TARGET_EXTENSIONS, TEST_KEYWORDS, ...) live in ``collect.constants`` instead,
because ``collect.utils`` — used by both process and check_cov — depends on them.
(File-path-to-module-name mapping is in ``check_cov.modules``, computed from the
repo's ``__init__.py`` structure the way jedi does — no source-root name list.)
"""
from enum import StrEnum
from typing import TypedDict

from susvibes.curate.mine.utils import is_test_file  # re-exported for the package


# === data shape ==============================================================

class CoverageLabel(StrEnum):
    """Per-file / per-instance coverage label (best -> worst). A StrEnum, so a
    member is its own string value — serializes to JSON and compares equal to the
    plain string, and str() / logging shows the value."""
    LIKELY = "likely_covered"
    MAYBE = "maybe_covered"
    UNLIKELY = "unlikely_covered"
    UNKNOWN = "unknown"


LABEL_RANK = {
    CoverageLabel.LIKELY: 3,
    CoverageLabel.MAYBE: 2,
    CoverageLabel.UNLIKELY: 1,
    CoverageLabel.UNKNOWN: 0,
}


class CoverageResult(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    label: str               # instance label (best over per_file)
    confidence: str          # high | medium | low
    score: float             # best per-file score
    per_file: dict           # target_path -> {label, score, confidence, evidence, reason}
    reason: str
    engine: str              # "symbol" | "file" — which trace produced the scores
    n_target_files: int      # target-language files touched by the security patch


def file_result(label, confidence, reason, score=0.0, evidence=None) -> dict:
    """Build a per-file coverage entry (the value type of CoverageResult.per_file)."""
    return {
        "label": label,
        "score": score,
        "confidence": confidence,
        "evidence": evidence or [],
        "reason": reason,
    }


def basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


# === tuning ==================================================================

# --- classifier thresholds (shared by all engines) ---------------------------
# Per-file score cutoffs for the coverage label.
COV_LIKELY_THRESHOLD = 0.80
COV_MAYBE_THRESHOLD = 0.55

# --- distinctive-symbol evidence (shared by file_trace L4 and heuristics) -----
# A repo-defined symbol counts as "distinctive" evidence (a test referencing it
# implies coverage) only when it is defined at most this many times across the
# whole repo and is at least this long — uniqueness, not a hand-maintained
# stop-list, filters out ubiquitous names like get/run/main.
SYMBOL_MAX_DEFCOUNT = 1
SYMBOL_MIN_LEN = 4

# --- file_trace: file-level approximation (L1-L4, the Python 2 fallback) ------
# Max depth of the file import-graph BFS for indirect coverage evidence. The edge
# is "file A imports file B", so a test that reaches the target within this many
# hops counts as covered. Deeper = higher recall, more false positives.
IMPORT_GRAPH_MAX_DEPTH = 8
# Fraction of target files that must be unparseable (with no other evidence)
# before the instance is downgraded to `unknown` (don't go unknown too eagerly).
FILE_TRACE_MAX_PARSE_FAIL_RATIO = 0.5

# --- heuristics: dynamic-wiring edges (L5-L7) --------------------------------
# Max hops from a test at which a production file's string reference to the target
# module (dynamic / registry-style wiring, e.g. importlib.import_module) still
# counts as evidence (L7b). Kept shallower than the import-graph depth: this is a
# string-match heuristic, so allowing it arbitrarily deep raises false positives.
DYNAMIC_WIRING_MAX_DEPTH = 4

# --- symbol_trace: precise backward symbol trace via jedi --------------------
# Max hops in the backward symbol use-graph (target symbol -> who uses it -> ...).
# Larger than the file import-graph depth because a symbol chain takes more steps
# (each wrapping function is one hop); `seen` dedup keeps the walk bounded.
SYMBOL_TRACE_MAX_DEPTH = 12
# If more than this fraction of a sample of repo files fail to parse under jedi,
# treat jedi as unusable for the repo (likely Python 2) and fall back to the
# file-level approximation.
JEDI_PARSE_FAIL_RATIO = 0.5
JEDI_PARSE_SAMPLE = 40
# A single jedi get_references call (a project-wide reference search) can fail
# transiently — the jedi compiled-subprocess it may spawn is fragile under load.
# Retry a failed call this many times (rebuilding the Script) before giving up.
GET_REFERENCES_RETRIES = 2
# If more than this many get_references calls permanently fail during one target's
# backward trace AND no test was reached, the trace is too incomplete to trust:
# report `unknown` rather than a possibly-false `unlikely_covered`.
SYMBOL_TRACE_MAX_FAILURES = 3
