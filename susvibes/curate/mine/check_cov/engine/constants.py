# -*- coding: utf-8 -*-
"""Coverage labels, result shape, and tuning constants for the check_cov engine.

Two parts:
  - DATA SHAPE: the coverage label class, the result dict shape, and the small
    helpers that build results.
  - TUNING: scoring thresholds and per-engine knobs, grouped by owning engine.

py2/py3 dual-compatible (this whole package is vendored into py2.7-3.12 cov_py
containers): the label set is a plain string-constant class, NOT ``enum.StrEnum``
(3.11+, unavailable in older containers). A member still IS its string value
(compare/serialize/dict-key it directly), so the StrEnum contract the rest of the
codebase relies on is preserved — only the declaration differs.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

from collections import OrderedDict

from .extract_facts import is_test_file  # re-exported for the package


# === data shape ==============================================================

class CoverageLabel(object):
    """Per-file / per-instance coverage label (best -> worst). Plain string-constant
    class: a member IS its own string value — serializes to JSON and compares equal
    to the plain string, and str()/logging shows the value (same contract as a
    StrEnum, which we cannot use in the py2.7/3.5-3.10 containers)."""
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

# A per-instance coverage result is a plain dict (no typing.TypedDict — 3.8+ only).
# Keys: instance_id, project, base_commit, label (best over per_file), score (best
# per-file score), per_file (target_path -> {label, score, evidence, reason}),
# reason, engine ("symbol" | "file"), n_target_files.
CoverageResult = dict


def file_result(label, reason, score=0.0, evidence=None):
    """Build a per-file coverage entry (the value type of CoverageResult.per_file).
    OrderedDict so JSON key order is stable across py2.7/3.5 (unordered dict) and
    py3.6+ (insertion-ordered) — a tidy, version-independent coverage report."""
    return OrderedDict([
        ("label", label),
        ("score", score),
        ("evidence", evidence or []),
        ("reason", reason),
    ])


def basename(rel):
    return rel.rsplit("/", 1)[-1]


# === tuning ==================================================================

# Grouped by owning engine so each knob's owner is obvious. Plain namespace classes,
# NOT Enums: these are heterogeneous dials read individually (Class.KNOB), not a set
# of interchangeable choices.


class Classifier(object):
    """analyze.classify: per-file score -> label."""
    # At/above this is `likely`; any positive score below is `maybe`; zero `unlikely`.
    LIKELY_THRESHOLD = 0.80


class Index(object):
    """repo_index: shared import graph + distinctive-symbol facts (used by every engine)."""
    # Max depth of the file import-graph BFS from tests (edge = "file A imports file
    # B"). Consumed by file_trace F4, heuristics H5, and the symbol-trace top-level
    # backstop. Deeper = higher recall, more false positives.
    IMPORT_GRAPH_MAX_DEPTH = 8
    # A repo-defined symbol is "distinctive" evidence (a test referencing it implies
    # coverage) only when defined at most this many times repo-wide and at least this
    # long — uniqueness, not a stop-list, filters ubiquitous names like get/run/main.
    SYMBOL_MAX_DEFCOUNT = 1
    SYMBOL_MIN_LEN = 4


class SymbolTrace(object):
    """symbol_trace: precise backward symbol trace via jedi."""
    # Max hops in the backward symbol use-graph (target symbol -> who uses it -> ...).
    MAX_DEPTH = 12
    # A symbol with more references than this is a framework/util API used repo-wide,
    # not a coverage chain: still check its refs for a direct test hit, but don't expand
    # its successors, or the backward BFS blows up combinatorially — each get_references
    # is a project-wide scan, so expanding a widely-referenced symbol re-searches the
    # whole repo for every one of its hundreds of references, at each hop.
    REFERENCE_EXPAND_LIMIT = 100
    # If more than this fraction of a sample of repo files fail to parse under jedi,
    # treat jedi as unusable and fall back to file_trace (rare — the version-matched
    # jedi parses py2 and py3 alike; this catches genuinely broken/exotic sources).
    JEDI_PARSE_FAIL_RATIO = 0.5
    JEDI_PARSE_SAMPLE = 40
    # A get_references call (project-wide search) can fail transiently — retry this
    # many times (rebuilding the Script) before giving up.
    GET_REFERENCES_RETRIES = 2
    # If more than this many get_references calls permanently fail during one target's
    # backward trace AND no test was reached, report `unknown` rather than a
    # possibly-false `unlikely_covered`.
    MAX_FAILURES = 3
    # The top-level-reach backstop only counts when a test DIRECTLY imports the file
    # (import-graph depth 1); a deeper transitive import is too incidental.
    TOPLEVEL_REACH_MAX_DEPTH = 1
    # jedi's project-wide get_references caps how many files it opens/parses via two
    # HARDCODED module globals (jedi.inference.references._OPENED_FILE_LIMIT=2000 /
    # _PARSED_FILE_LIMIT=30) with no public setting. On a large repo the directory
    # walk exhausts that budget before reaching tests/, so real test references are
    # SILENTLY dropped. symbol_trace monkeypatches these globals up at import.
    #   OPENED: opening a file is a cheap sequential read; just exceed the repo's .py count.
    #   PARSED: parsing+inferring is the EXPENSIVE step, so kept far lower.
    JEDI_OPENED_FILE_LIMIT = 20000
    JEDI_PARSED_FILE_LIMIT = 200


class FileTrace(object):
    """file_trace: file-level approximation (F1-F6, the no-jedi fallback)."""
    # Fraction of target files that must be unparseable (with no other evidence)
    # before the instance is downgraded to `unknown` (don't go unknown too eagerly).
    MAX_PARSE_FAIL_RATIO = 0.5


class Heuristics(object):
    """heuristics: dynamic-wiring edges (H1-H9); this knob bounds H4's reach."""
    # Max hops from a test at which a production file's STRING reference to the target
    # module (dynamic / registry wiring) still counts as evidence (H4). Shallower
    # than the import-graph depth: a string match allowed arbitrarily deep raises FPs.
    DYNAMIC_WIRING_MAX_DEPTH = 4
