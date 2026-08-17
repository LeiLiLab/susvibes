# -*- coding: utf-8 -*-
"""Per-instance repo index shared by the static scorers.

Parses every snapshot file once (ast -> tree-sitter -> regex) and derives the
maps the file-level and heuristic scorers need: per-file imports and module
identities, a package re-export map, a symbol definition-count (for
distinctiveness), and a forward import-graph BFS from the tests. The jedi symbol
tracer does not use this — it queries jedi directly.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

from collections import Counter, deque, namedtuple

from .extract_facts import (
    extract_module_facts,
    resolve_relative_import,
)
from .modules import module_names, package_dirs
from .constants import Index


# Fields:
#   sources       rel -> source text
#   facts         rel -> extract_module_facts(source)
#   file_imports  rel -> set of dotted modules the file imports
#   file_modules  rel -> candidate dotted module names the file IS
#   module_files  dotted module -> set of files that ARE that module
#   defcount      symbol name -> number of files that define it
#   test_set      set of rel paths that are test files
#   bfs_depth     rel -> hops from the nearest test (forward import graph)
RepoIndex = namedtuple("RepoIndex", [
    "sources", "facts", "file_imports", "file_modules",
    "module_files", "defcount", "test_set", "bfs_depth",
])


def relative_anchor(rel, file_modules):
    """The dotted module a file's relative imports resolve against. A package
    __init__.py anchors at the package itself (a synthetic trailing component
    keeps a leading-dot import at the package)."""
    primary = (file_modules.get(rel) or [""])[0]
    if rel.endswith("__init__.py"):
        return (primary + ".__init__") if primary else "__init__"
    return primary


def resolved_imports(facts, anchor):
    """Absolute dotted modules the file imports (relative imports resolved against
    ``anchor``), including each ``from module import name`` as ``module.name``."""
    modules = set(facts["import_modules"])
    for level, mod, names in facts["from_imports"]:
        base = resolve_relative_import(anchor, level, mod) if level else (mod or "")
        if not base:
            continue
        modules.add(base)
        for nm in names:
            if nm == "*":
                continue
            modules.add("{0}.{1}".format(base, nm))
    return modules


def bfs_depths(test_set, file_imports, module_files, max_depth):
    """Forward BFS over the file import graph from all tests; {rel: hops-from-test}."""
    depth = {tf: 0 for tf in test_set}
    queue = deque(test_set)
    while queue:
        cur = queue.popleft()
        d = depth[cur]
        if d >= max_depth:
            continue
        for m in file_imports.get(cur, ()):
            for nxt in module_files.get(m, ()):
                if nxt not in depth:
                    depth[nxt] = d + 1
                    queue.append(nxt)
    return depth


def build(sources, test_set, max_depth=Index.IMPORT_GRAPH_MAX_DEPTH):
    facts = {rel: extract_module_facts(src) for rel, src in sources.items()}

    pkg_dirs = package_dirs(sources)
    file_modules, module_files = {}, {}
    for rel in sources:
        mods = module_names(rel, pkg_dirs)
        file_modules[rel] = mods
        for m in mods:
            module_files.setdefault(m, set()).add(rel)

    file_imports = {}
    for rel, f in facts.items():
        file_imports[rel] = resolved_imports(f, relative_anchor(rel, file_modules))

    defcount = Counter()
    for f in facts.values():
        for s in f["defined_symbols"]:
            defcount[s] += 1

    bfs_depth = bfs_depths(test_set, file_imports, module_files, max_depth)
    return RepoIndex(sources, facts, file_imports, file_modules,
                     module_files, defcount, test_set, bfs_depth)


def distinctive_symbols(target, index):
    """Target-defined symbols unique/long enough to be coverage evidence — a test
    referencing one implies it exercises the target (filters ubiquitous names)."""
    return set(
        s for s in index.facts[target]["defined_symbols"]
        if len(s) >= Index.SYMBOL_MIN_LEN and index.defcount[s] <= Index.SYMBOL_MAX_DEFCOUNT
    )
