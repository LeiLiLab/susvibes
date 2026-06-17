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
    CoverageLabel, LABEL_RANK, file_result, Classifier, SymbolTrace, FileTrace,
)
from .extract_facts import is_test_file, TARGET_EXTENSIONS
from . import repo_index, file_trace, heuristics, symbol_trace


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


def mostly_unparseable(targets, index):
    fails = sum(1 for t in targets
                if index.facts.get(t, {}).get("parse_level") in ("regex", "none"))
    return fails > len(targets) * FileTrace.MAX_PARSE_FAIL_RATIO


def aggregate(data_record, per_file, engine, n_targets, reason=None):
    files = list(per_file.values())
    if files:
        best = max(files, key=lambda v: (LABEL_RANK[v["label"]], v["score"]))
        label, score = best["label"], best["score"]
    else:
        best, label, score = None, CoverageLabel.UNKNOWN, 0.0
    return OrderedDict([
        ("instance_id", data_record["instance_id"]),
        ("project", data_record["project"]),
        ("base_commit", data_record["base_commit"]),
        ("label", label),
        ("score", score),
        ("per_file", per_file),
        ("reason", reason or (best["reason"] if best else "no target-language files in patch")),
        ("engine", engine),
        ("n_target_files", n_targets),
    ])


def analyze(data_record, repo_dir, sources, targets, max_depth=SymbolTrace.MAX_DEPTH):
    """Classify coverage of an instance's security-patch files against the snapshot.

    ``targets`` is the sorted list of security-patch files (all target-language);
    the caller (host or worker) supplies it. ``data_record`` provides the instance
    meta (instance_id / project / base_commit) echoed into the result.
    """
    # process.py guarantees the security_patch is non-empty and all target-language
    # (.py): a patch with no .py target here means that upstream invariant was violated.
    if not targets or any(not t.endswith(TARGET_EXTENSIONS) for t in targets):
        raise ValueError(
            "{0}: security_patch must be all .py files, got {1}".format(
                data_record["instance_id"], targets))

    per_file = OrderedDict()
    test_set = set(rel for rel in sources if is_test_file(rel))
    if not test_set:
        for t in targets:
            per_file[t] = file_result(CoverageLabel.UNLIKELY, "no test suite detected")
        return aggregate(data_record, per_file, "none", len(targets), reason="no test suite detected")

    # The index (parse + maps) is always needed: heuristics consume it, and it is
    # the file-level fallback when jedi is unusable.
    index = repo_index.build(sources, test_set)
    jedi_ok = symbol_trace.usable(repo_dir, sources)
    jctx = symbol_trace.build(repo_dir) if jedi_ok else None
    engine = "symbol" if jedi_ok else "file"

    for t in targets:
        evidence, reliable = [], True
        if jedi_ok:
            sym_evidence, reliable = symbol_trace.score(t, jctx, index, max_depth)  # S1-S4 precise
            evidence += sym_evidence
        else:
            evidence += file_trace.score(t, index)               # F1-F6 approximation
        evidence += heuristics.score(t, index)                   # H1-H9 always
        if not evidence and not reliable:
            # Symbol trace could not complete (jedi unreliable, or target unparseable)
            # and nothing else scored: we cannot tell — unknown, not (false) unlikely.
            per_file[t] = file_result(CoverageLabel.UNKNOWN,
                                      "symbol trace incomplete (jedi unreliable or target unparseable)")
        else:
            per_file[t] = result_from_evidence(evidence)

    # File engine only: if most targets were unparseable and nothing scored, the
    # absence of evidence is unreliable — report unknown rather than unlikely. (The
    # symbol engine is precise: no reference chain genuinely means uncovered.)
    if not jedi_ok and mostly_unparseable(targets, index) \
            and all(per_file[t]["score"] == 0.0 for t in targets):
        for t in targets:
            per_file[t] = file_result(CoverageLabel.UNKNOWN, "target files unparseable")
        return aggregate(data_record, per_file, engine, len(targets),
                          reason="target files unparseable")

    return aggregate(data_record, per_file, engine, len(targets))
