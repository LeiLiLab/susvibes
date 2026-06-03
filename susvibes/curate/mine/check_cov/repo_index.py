"""Per-instance repo index shared by the static scorers.

Parses every snapshot file once (ast -> tree-sitter -> regex) and derives the
maps the file-level and heuristic scorers need: per-file imports and module
identities, a package re-export map, a symbol definition-count (for
distinctiveness), and a forward import-graph BFS from the tests. The jedi symbol
tracer does not use this — it queries jedi directly.
"""
from collections import Counter, deque
from typing import NamedTuple

from susvibes.curate.mine.utils import (
    extract_module_facts,
    resolve_relative_import,
)
from susvibes.curate.mine.check_cov.modules import module_names, package_dirs
from susvibes.curate.mine.check_cov.constants import Index


class RepoIndex(NamedTuple):
    sources: dict            # rel -> source text
    facts: dict              # rel -> extract_module_facts(source)
    file_imports: dict       # rel -> set of dotted modules the file imports
    import_pairs: dict       # rel -> set of (module, name) from `from module import name`
    file_modules: dict       # rel -> candidate dotted module names the file IS
    module_files: dict       # dotted module -> set of files that ARE that module
    reexports: dict          # (package_module, symbol) -> set of origin files
    defcount: Counter        # symbol name -> number of files that define it
    test_set: set            # rel paths that are test files
    bfs_depth: dict          # rel -> hops from the nearest test (forward import graph)


def _relative_anchor(rel: str, file_modules: dict) -> str:
    """The dotted module a file's relative imports resolve against. A package
    __init__.py anchors at the package itself (a synthetic trailing component
    keeps a leading-dot import at the package)."""
    primary = (file_modules.get(rel) or [""])[0]
    if rel.endswith("__init__.py"):
        return (primary + ".__init__") if primary else "__init__"
    return primary


def _resolved_imports(facts: dict, anchor: str):
    """(modules, pairs): absolute dotted modules the file imports, and the
    (module, name) pairs of its `from module import name` (relatives resolved)."""
    modules = set(facts["import_modules"])
    pairs = set()
    for level, mod, names in facts["from_imports"]:
        base = resolve_relative_import(anchor, level, mod) if level else (mod or "")
        if not base:
            continue
        modules.add(base)
        for nm in names:
            if nm == "*":
                continue
            modules.add(f"{base}.{nm}")
            pairs.add((base, nm))
    return modules, pairs


def _bfs_depths(test_set, file_imports, module_files, max_depth) -> dict:
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


def build(sources: dict, test_set: set, max_depth=Index.IMPORT_GRAPH_MAX_DEPTH) -> RepoIndex:
    facts = {rel: extract_module_facts(src) for rel, src in sources.items()}

    pkg_dirs = package_dirs(sources)
    file_modules, module_files = {}, {}
    for rel in sources:
        mods = module_names(rel, pkg_dirs)
        file_modules[rel] = mods
        for m in mods:
            module_files.setdefault(m, set()).add(rel)

    file_imports, import_pairs = {}, {}
    for rel, f in facts.items():
        mods, pairs = _resolved_imports(f, _relative_anchor(rel, file_modules))
        file_imports[rel] = mods
        import_pairs[rel] = pairs

    reexports: dict = {}
    for rel in sources:
        if not rel.endswith("__init__.py"):
            continue
        pkg_module = (file_modules.get(rel) or [None])[0]
        if not pkg_module:
            continue
        for base_module, nm in import_pairs[rel]:
            for origin in module_files.get(base_module, ()):
                reexports.setdefault((pkg_module, nm), set()).add(origin)

    defcount: Counter = Counter()
    for f in facts.values():
        for s in f["defined_symbols"]:
            defcount[s] += 1

    bfs_depth = _bfs_depths(test_set, file_imports, module_files, max_depth)
    return RepoIndex(sources, facts, file_imports, import_pairs, file_modules,
                     module_files, reexports, defcount, test_set, bfs_depth)


def distinctive_symbols(target, index: RepoIndex) -> set:
    """Target-defined symbols unique/long enough to be coverage evidence — a test
    referencing one implies it exercises the target (filters ubiquitous names)."""
    return {
        s for s in index.facts[target]["defined_symbols"]
        if len(s) >= Index.SYMBOL_MIN_LEN and index.defcount[s] <= Index.SYMBOL_MAX_DEFCOUNT
    }
