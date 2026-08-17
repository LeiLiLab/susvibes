# -*- coding: utf-8 -*-
"""Per-instance coverage analysis: pure scoring over an in-memory snapshot.

Given the repo snapshot (rel -> source), the security-patch target files, and the
repo dir, run the engines and classify each target, then aggregate to one
instance-level CoverageResult. No I/O, no docker, no susvibes deps — the host
orchestrator and the in-container worker both call ``analyze``.

``targets`` is passed in (not computed here): the host derives it from the
security_patch diff and hands the same list to the worker via input.json, so the
engine never needs the diff parser.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

from collections import OrderedDict

from .constants import (
    CoverageLabel, LABEL_RANK, file_result, Classifier, SymbolTrace,
)
from .extract_facts import is_test_file, has_test_def, TARGET_EXTENSIONS
from .modules import package_dirs, source_roots
from . import repo_index, heuristics, symbol_trace


def classify(score):
    if score >= Classifier.LIKELY_THRESHOLD:
        return CoverageLabel.LIKELY
    if score > 0:
        return CoverageLabel.MAYBE
    return CoverageLabel.UNLIKELY


def result_from_evidence(evidence):
    """evidence: list of (score, message). Strongest score wins."""
    if not evidence:
        return file_result(CoverageLabel.UNLIKELY, "no evidence found")
    evidence = sorted(evidence, key=lambda e: -e[0])
    score = evidence[0][0]
    label = classify(score)
    msgs = [m for _, m in evidence][:6]
    return file_result(label, msgs[0], score=round(score, 3), evidence=msgs)


def aggregate(data_record, per_file, engine, n_target_files, reason=None):
    files = list(per_file.values())
    if files:
        best = max(files, key=lambda v: (LABEL_RANK[v["label"]], v["score"]))
        label, score = best["label"], best["score"]
    else:
        best, label, score = None, CoverageLabel.INDETERMINATE, 0.0
    return OrderedDict([
        ("instance_id", data_record["instance_id"]),
        ("project", data_record["project"]),
        ("base_commit", data_record["base_commit"]),
        ("label", label),
        ("score", score),
        ("per_file", per_file),
        ("reason", reason or (best["reason"] if best else "no target-language files in patch")),
        ("engine", engine),
        ("n_target_files", n_target_files),
    ])


def analyze(data_record, repo_dir, sources, targets, seed_names_map=None,
            max_depth=SymbolTrace.MAX_DEPTH):
    """Classify coverage of an instance's security-patch files against the snapshot.

    ``targets`` is the sorted list of security-patch files (all target-language);
    the caller (host or worker) supplies it. ``seed_names_map`` (target -> set of
    symbol names) narrows each trace to the security_patch's containing symbols; when
    given, a target absent from it (or mapping to an empty set) seeds nothing — there
    is no whole-file fallback. ``data_record`` provides the instance meta echoed into
    the result."""
    # mine.core guarantees the security_patch is non-empty and all target-language
    # (.py): a patch with no .py target here means that upstream invariant was violated.
    if not targets or any(not tgt.endswith(TARGET_EXTENSIONS) for tgt in targets):
        raise ValueError(
            "{0}: security_patch must be all .py files, got {1}".format(
                data_record["instance_id"], targets))

    per_file = OrderedDict()
    # A real test file is on a test-ish path AND actually defines a test — excludes
    # production / utility code under a test-ish name (which would fabricate coverage).
    test_set = set(rel for rel in sources if is_test_file(rel) and has_test_def(sources[rel]))
    if not test_set:
        for tgt in targets:
            per_file[tgt] = file_result(CoverageLabel.UNLIKELY, "no test suite detected")
        return aggregate(data_record, per_file, "symbol", len(targets), reason="no test suite detected")

    # The index (parse + maps) feeds the heuristics and the symbol-trace top-level
    # backstop; jedi does the precise reference tracing.
    index = repo_index.build(sources, test_set)
    jctx = symbol_trace.build(repo_dir, source_roots(package_dirs(sources)))

    for tgt in targets:
        names = seed_names_map.get(tgt, set()) if seed_names_map is not None else None
        sym_evidence, reliable = symbol_trace.score(tgt, jctx, index, max_depth,
                                                    seed_names=names)            # S1-S4 precise
        evidence = list(sym_evidence)
        evidence += heuristics.score(tgt, index, names)                           # H1-H10 always
        if not evidence and not reliable:
            # Symbol trace could not complete (jedi unreliable or target unparseable)
            # and nothing else scored: we cannot tell — indeterminate, not (false) unlikely.
            per_file[tgt] = file_result(CoverageLabel.INDETERMINATE,
                                        "symbol trace incomplete (jedi unreliable or target unparseable)")
        else:
            per_file[tgt] = result_from_evidence(evidence)

    return aggregate(data_record, per_file, "symbol", len(targets))
