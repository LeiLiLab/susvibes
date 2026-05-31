"""File-level static coverage approximation (no jedi) — the Python 2 fallback.

Scores a target with cheap, language-agnostic layers over the shared RepoIndex:

    L1  a test imports the target module / a symbol from it
    L1b a test imports a symbol the package __init__ re-exports from the target
    L2  the target is reachable from a test in the file import graph
    L4  a test references a distinctive symbol the target defines

When jedi can parse the repo, `symbol_trace` replaces L1/L1b/L2/L4 with precise
symbol references; this engine is used only when jedi cannot (heavy Python 2).
"""
from susvibes.curate.collect.check_cov.repo_index import RepoIndex, distinctive_symbols

# Approximate evidence (lower than precise symbol-trace hits); strongest wins.
SCORE_DIRECT_IMPORT = 0.92   # test imports the target module or a symbol from it
SCORE_REEXPORT = 0.85        # test imports a symbol the package __init__ re-exports
SCORE_GRAPH_NEAR = 0.55      # target reachable from a test within 2 import hops
SCORE_GRAPH_FAR = 0.45       # target reachable from a test within max_depth hops
SCORE_SYMBOL_USED = 0.35     # test references a distinctive symbol the target defines
SCORE_WEAK_STRING = 0.30     # distinctive symbol appears only as a string in a test


def score(target, index: RepoIndex):
    """L1/L1b/L2/L4 evidence for a target; returns a list of (score, message)."""
    if target not in index.sources:
        return []
    target_modules = set(index.file_modules.get(target, []))
    unique_syms = distinctive_symbols(target, index)
    evidence = []

    for tf in index.test_set:
        tfacts = index.facts[tf]
        # L1: test directly imports the target module / a symbol from it.
        if target_modules & index.file_imports[tf]:
            evidence.append((SCORE_DIRECT_IMPORT, f"test imports target module ({tf})"))
        # L1b: test imports a symbol the package __init__ re-exports from the target.
        for pkg, nm in index.import_pairs[tf]:
            if target in index.reexports.get((pkg, nm), ()):
                evidence.append((SCORE_REEXPORT, f"test imports re-exported '{nm}' from {pkg} ({tf})"))
        # L4: test references a distinctive symbol the target defines.
        if unique_syms & tfacts["used_names"]:
            evidence.append((SCORE_SYMBOL_USED, f"test references target symbol(s) ({tf})"))
        elif unique_syms & tfacts["strings"]:
            evidence.append((SCORE_WEAK_STRING, f"test mentions target symbol as string ({tf})"))

    # L2: indirect import-graph reachability — how far the nearest test reaches it.
    depth = index.bfs_depth.get(target)
    if depth is not None and depth >= 1:
        value = SCORE_GRAPH_NEAR if depth <= 2 else SCORE_GRAPH_FAR
        evidence.append((value, f"reachable from tests via import graph (depth {depth})"))

    return evidence
