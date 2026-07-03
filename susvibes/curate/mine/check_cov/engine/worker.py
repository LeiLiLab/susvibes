# -*- coding: utf-8 -*-
"""In-container entry point for the check_cov engine.

The host COPYies the FULL repo tree, this ``engine`` package, and an ``input.json``
into a version-matched cov_py image, then runs
``python -m _susvibes_cov_engine.worker <src_root> <input_path>``. The engine is
installed into the image's site-packages as ``_susvibes_cov_engine`` (leading
underscore + susvibes prefix so a repo's own top-level package can't shadow it).

  src_root    the repo tree (FULL working tree — selecting .py / skipping .git is the
              analysis layer's job here, not the copy layer's)
  input_path  {"instance_id","project","base_commit","targets",[ "max_depth" ]}

We run ``analyze`` with the container's NATIVE ast/jedi/parso and print the
CoverageResult as JSON to stdout after a marker line. The host can't see the
container filesystem (COPY, not a mount), so the marker + stdout (captured as
container logs) is the only result channel.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import io
import os
import sys
import json

from .analyze import analyze
from .containment import seed_names_for_targets
from .constants import SymbolTrace
from .extract_facts import TARGET_EXTENSIONS

RESULT_MARKER = "<<<COV_RESULT>>>"


def read_sources(src_root):
    """{rel_posix: source text} for target-language files under src_root. The host
    copies (rsync -aHAX) the FULL working tree here at max fidelity, nothing filtered;
    pruning .git and selecting .py happen HERE at the analysis layer, where a present-but-unselected
    file is recoverable — unlike a copy-layer miss, which is silent and irreversible
    (the git-archive export-ignore lesson)."""
    sources = {}
    for dirpath, dirnames, filenames in os.walk(src_root):
        if ".git" in dirnames:
            dirnames.remove(".git")   # prune: never descend into a .git tree
        for fn in filenames:
            try:
                if not fn.endswith(TARGET_EXTENSIONS):
                    continue
            except UnicodeDecodeError:
                continue   # py2: a non-ascii (undecodable) filename is never a .py we analyze
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, src_root).replace(os.sep, "/")
            try:
                with io.open(abs_path, encoding="utf-8", errors="replace") as fh:
                    sources[rel] = fh.read()
            except Exception:
                sources[rel] = ""
    return sources


def main(src_root, input_path):
    with io.open(input_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    sources = read_sources(src_root)
    max_depth = meta.get("max_depth", SymbolTrace.MAX_DEPTH)
    # Narrow each trace to the security_patch's containing symbols. Deleted lines map into
    # these rolled-back sources; the post version (for added lines) is derived in-engine by
    # forward-applying the patch, so the host ships only the patch text.
    seed_names_map = seed_names_for_targets(meta["security_patch"], sources)
    result = analyze(meta, src_root, sources, meta["targets"], seed_names_map, max_depth)
    print(RESULT_MARKER)
    # engine builds the result with OrderedDicts, so json.dumps keeps a stable key
    # order (instance_id first) across py2.7/3.5/3.6+ without sort_keys.
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
