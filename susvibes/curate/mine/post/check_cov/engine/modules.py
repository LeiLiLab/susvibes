# -*- coding: utf-8 -*-
"""Map a repo file path to the dotted module name it is importable as.

This mirrors how jedi resolves a path to a module (jedi/inference/sys_path.py
``transform_path_to_dotted`` + jedi/api/project.py ``_get_sys_path``): the source
roots are computed from the ``__init__.py`` structure, NOT a hard-coded prefix
list. jedi puts the project root and every NON-package parent directory (one
without an ``__init__.py``) on ``sys.path``, then takes the SHORTEST dotted path
that strips a root prefix.

That shortest path is exactly the trailing run of package directories (dirs with
an ``__init__.py``) above the file: walk up from the file's directory while each
dir is a package, stop at the first non-package dir — that dir is the source root,
and the names below it form the module path. So:

    src/pkg/user.py  (src/pkg is a package, src is not)  -> pkg.user
    a/b/c.py         (no __init__.py anywhere)           -> c
    pkg/sub/mod.py   (both pkg and pkg/sub are packages)  -> pkg.sub.mod

No ``src``/``lib``/``python`` name list is needed or used — a directory is a
source root simply by not being a package, which adapts to any layout.

Only check_cov's file-level engine needs this; the symbol engine asks jedi.
"""
from __future__ import print_function, division, absolute_import, unicode_literals


def package_dirs(sources):
    """Repo-relative posix dirs that contain an ``__init__.py`` (``""`` = repo
    root). These are the package boundaries the module-name walk stops at."""
    out = set()
    for rel in sources:
        if rel.endswith("__init__.py"):
            out.add(rel[:-len("/__init__.py")] if "/" in rel else "")
    return out


def source_roots(pkg_dirs):
    """Repo-relative dirs to put on sys.path so the repo's OWN packages resolve ahead
    of an installed same-named package: the parent of each top-level package (a package
    dir whose parent is not itself a package). A flat layout yields ``{""}`` (the repo
    root, which jedi already roots at); a ``src`` layout yields ``{"src"}`` — the dir
    jedi does NOT auto-prioritize, so a repo ``import pkg`` would otherwise resolve to a
    site-packages ``pkg`` and the trace would miss the repo's references."""
    roots = set()
    for d in pkg_dirs:
        parent = d.rsplit("/", 1)[0] if "/" in d else ""
        if parent not in pkg_dirs:
            roots.add(parent)
    return roots


def module_name(rel, pkg_dirs):
    """Dotted module name for ``rel`` (jedi's shortest-path resolution), or None
    if ``rel`` is not a .py file or has no importable name (a top-level
    ``__init__.py``).

    The source root is the first ancestor dir that is NOT a package; the module
    path is the package dirs from there down, plus the file stem."""
    if not rel.endswith(".py"):
        return None
    parts = rel.split("/")
    stem = parts[-1][:-len(".py")]
    dirs = parts[:-1]
    # Trailing run of package dirs above the file (those with an __init__.py),
    # walking up until the first non-package dir (the source root).
    chain = []
    for i in range(len(dirs), 0, -1):
        if "/".join(dirs[:i]) in pkg_dirs:
            chain.append(dirs[i - 1])
        else:
            break
    chain.reverse()
    comps = chain if stem == "__init__" else chain + [stem]
    return ".".join(comps) if comps else None


def module_names(rel, pkg_dirs):
    """Candidate dotted module names for ``rel`` — a list for the caller's
    convenience (jedi resolves to one name, so this is [] or a single name)."""
    name = module_name(rel, pkg_dirs)
    return [name] if name else []
