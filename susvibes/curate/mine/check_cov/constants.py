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
    score: float             # best per-file score
    per_file: dict           # target_path -> {label, score, evidence, reason}
    reason: str
    engine: str              # "symbol" | "file" — which trace produced the scores
    n_target_files: int      # target-language files touched by the security patch


def file_result(label, reason, score=0.0, evidence=None) -> dict:
    """Build a per-file coverage entry (the value type of CoverageResult.per_file)."""
    return {
        "label": label,
        "score": score,
        "evidence": evidence or [],
        "reason": reason,
    }


def basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


# === tuning ==================================================================

# Grouped by owning engine so each knob's owner is obvious. Plain namespace classes,
# NOT Enums: these are heterogeneous dials read individually (Class.KNOB), not a set
# of interchangeable choices.


class Classifier:
    """core._classify: per-file score -> label."""
    # At/above this is `likely`; any positive score below is `maybe`; zero `unlikely`.
    LIKELY_THRESHOLD = 0.80


class Index:
    """repo_index: shared import graph + distinctive-symbol facts (used by every engine)."""
    # Max depth of the file import-graph BFS from tests (edge = "file A imports file
    # B"). Consumed by file_trace L2, heuristics L7b, and the symbol-trace top-level
    # backstop. Deeper = higher recall, more false positives.
    IMPORT_GRAPH_MAX_DEPTH = 8
    # A repo-defined symbol is "distinctive" evidence (a test referencing it implies
    # coverage) only when defined at most this many times repo-wide and at least this
    # long — uniqueness, not a stop-list, filters ubiquitous names like get/run/main.
    SYMBOL_MAX_DEFCOUNT = 1
    SYMBOL_MIN_LEN = 4


class SymbolTrace:
    """symbol_trace: precise backward symbol trace via jedi."""
    # Max hops in the backward symbol use-graph (target symbol -> who uses it -> ...).
    # Larger than the import-graph depth: a symbol chain takes more steps (each
    # wrapping function is a hop); `seen` dedup bounds the walk.
    MAX_DEPTH = 12
    # If more than this fraction of a sample of repo files fail to parse under jedi,
    # treat jedi as unusable (likely Python 2) and fall back to file_trace.
    JEDI_PARSE_FAIL_RATIO = 0.5
    JEDI_PARSE_SAMPLE = 40
    # A get_references call (project-wide search) can fail transiently — the jedi
    # compiled-subprocess it may spawn is fragile under load. Retry this many times
    # (rebuilding the Script) before giving up.
    GET_REFERENCES_RETRIES = 2
    # If more than this many get_references calls permanently fail during one target's
    # backward trace AND no test was reached, the trace is too incomplete to trust:
    # report `unknown` rather than a possibly-false `unlikely_covered`.
    MAX_FAILURES = 3
    # The top-level-reach backstop (a target symbol referenced at a test-importable
    # file's module top level, nothing further to trace) only counts when a test
    # DIRECTLY imports that file — import-graph depth 1. A deeper transitive import is
    # too incidental (the test didn't choose to load it) to be a coverage signal.
    TOPLEVEL_REACH_MAX_DEPTH = 1
    # jedi's project-wide get_references caps how many files it opens/parses, via two
    # HARDCODED module globals (jedi.inference.references._OPENED_FILE_LIMIT=2000 /
    # _PARSED_FILE_LIMIT=30) with no public setting. On a large repo the directory walk
    # exhausts that budget on the source tree before reaching tests/, so real test
    # references are SILENTLY dropped (dragging true-covered files to a weak score). We
    # monkeypatch these globals up at startup (symbol_trace).
    #   OPENED: opening a file is a cheap, sequential read+regex (read then close, no
    #     fd pile-up); it just needs to exceed the repo's total .py count.
    #   PARSED: parsing+inferring a candidate is the EXPENSIVE step (~10-100ms), so kept
    #     far lower — margin for a distinctive symbol across a big package without a
    #     ubiquitous-name seed parsing thousands of files.
    # On a machine with a low `ulimit -n` or limited RAM, tune both down.
    JEDI_OPENED_FILE_LIMIT = 20_000
    JEDI_PARSED_FILE_LIMIT = 200


class FileTrace:
    """file_trace: file-level approximation (L1-L4, the Python 2 fallback)."""
    # Fraction of target files that must be unparseable (with no other evidence)
    # before the instance is downgraded to `unknown` (don't go unknown too eagerly).
    MAX_PARSE_FAIL_RATIO = 0.5


class Heuristics:
    """heuristics: dynamic-wiring edges (H1-H5); this knob bounds H5's reach."""
    # Max hops from a test at which a production file's STRING reference to the target
    # module (dynamic / registry wiring, e.g. importlib.import_module) still counts as
    # evidence (L7b). Shallower than the import-graph depth: a string match allowed
    # arbitrarily deep raises false positives.
    DYNAMIC_WIRING_MAX_DEPTH = 4
