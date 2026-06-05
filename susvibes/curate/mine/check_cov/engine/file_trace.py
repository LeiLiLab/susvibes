# -*- coding: utf-8 -*-
"""File-level static coverage approximation (no jedi) — the Python 2 fallback.

Scores a target with cheap, language-agnostic layers over the shared RepoIndex:

    F1  a test imports the target module / a symbol from it
    F2  a test imports a symbol the package __init__ re-exports from the target
    F3  the target is reachable from a test in the file import graph (<=2 hops)
    F4  the target is reachable from a test in the file import graph (deeper)
    F5  a test references a distinctive symbol the target defines
    F6  a distinctive symbol the target defines appears only as a string in a test

When jedi can parse the repo, `symbol_trace` (S1-S4) replaces these precise-er;
this engine is used only when jedi cannot (heavy Python 2).
"""
from __future__ import print_function, division, absolute_import, unicode_literals

from .repo_index import distinctive_symbols

# Approximate evidence (lower than precise symbol-trace hits); strongest wins.
SCORE_DIRECT_IMPORT = 0.92   # F1: test imports the target module or a symbol from it
SCORE_REEXPORT = 0.85        # F2: test imports a symbol the package __init__ re-exports
SCORE_GRAPH_NEAR = 0.55      # F3: target reachable from a test within 2 import hops
SCORE_GRAPH_FAR = 0.45       # F4: target reachable from a test within max_depth hops
SCORE_SYMBOL_USED = 0.35     # F5: test references a distinctive symbol the target defines
SCORE_WEAK_STRING = 0.30     # F6: distinctive symbol appears only as a string in a test


def score(target, index):
    """File-level (F1-F6) evidence for a target; returns a list of (score, message)."""
    if target not in index.sources:
        return []
    target_modules = set(index.file_modules.get(target, []))
    unique_syms = distinctive_symbols(target, index)
    evidence = []

    for tf in index.test_set:
        tfacts = index.facts[tf]
        # F1: test directly imports the target module / a symbol from it.
        if target_modules & index.file_imports[tf]:
            evidence.append((SCORE_DIRECT_IMPORT, "[F1] test imports target module ({0})".format(tf)))
        # F2: test imports a symbol the package __init__ re-exports from the target.
        for pkg, nm in index.import_pairs[tf]:
            if target in index.reexports.get((pkg, nm), ()):
                evidence.append((SCORE_REEXPORT,
                                 "[F2] test imports re-exported '{0}' from {1} ({2})".format(nm, pkg, tf)))
        # F5/F6: test references a distinctive symbol the target defines.
        if unique_syms & tfacts["used_names"]:
            evidence.append((SCORE_SYMBOL_USED, "[F5] test references target symbol(s) ({0})".format(tf)))
        elif unique_syms & tfacts["strings"]:
            evidence.append((SCORE_WEAK_STRING, "[F6] test mentions target symbol as string ({0})".format(tf)))

    # F3/F4: indirect import-graph reachability — how far the nearest test reaches it.
    depth = index.bfs_depth.get(target)
    if depth is not None and depth >= 1:
        if depth <= 2:
            evidence.append((SCORE_GRAPH_NEAR,
                             "[F3] reachable from tests via import graph (depth {0})".format(depth)))
        else:
            evidence.append((SCORE_GRAPH_FAR,
                             "[F4] reachable from tests via import graph (depth {0})".format(depth)))

    return evidence
