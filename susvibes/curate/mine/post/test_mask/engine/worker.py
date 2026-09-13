# -*- coding: utf-8 -*-
"""In-container entry point for the test-mask engine.

The host COPYies this package and an ``input.json`` into a version-matched static_py image, then runs
``python -m _susvibes_test_mask_engine.worker <input_path>``. Only text crosses the boundary — the host
keeps every git operation, so the container needs no repo and no work tree.

  input_path  {"version", "files": [{"path", "patch", "before", "after"}, ...]}

The masks are printed as JSON after a marker line: the host cannot see the container filesystem
(COPY, not a mount), so stdout captured as container logs is the only result channel.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import io
import sys
import json

from .mask import mask_test_funcs

RESULT_MARKER = "<<<TEST_MASK_RESULT>>>"


def main(input_path):
    with io.open(input_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    version = meta["version"]
    masks, unparseable = {}, []
    for entry in meta["files"]:
        try:
            masks[entry["path"]] = mask_test_funcs(
                entry["patch"], entry["before"], entry["after"], version)
        except ValueError as e:
            # The instance's own interpreter cannot parse this file either — a genuinely broken
            # source (a merge-conflict marker left in the tree), not a dialect mismatch.
            unparseable.append([entry["path"], str(e)])
    print(RESULT_MARKER)
    print(json.dumps({"masks": masks, "unparseable": unparseable}))


if __name__ == "__main__":
    main(sys.argv[1])
