# -*- coding: utf-8 -*-
"""Self-contained, py2/py3 dual-compatible static coverage engine.

This whole package is COPYied into per-version ``static_py`` containers and run under
that interpreter's native ast/jedi/parso, so it has ZERO susvibes dependencies and
is written in the py2/py3 common subset (no StrEnum/TypedDict/f-string/py3-only
annotations). Internal imports are RELATIVE so the package works both as
``susvibes.curate.mine.post.check_cov.engine`` (host) and as a top-level
``_susvibes_cov_engine`` package (container: ``python -m _susvibes_cov_engine.worker``).
"""
from __future__ import print_function, division, absolute_import, unicode_literals

# Intentionally NO submodule imports here: importing ``engine`` (e.g. via
# ``engine.extract_facts`` from the host's mine/utils) must stay light and must NOT pull in
# jedi/pathlib (which ``analyze``/``symbol_trace`` need). Import the pieces you
# want explicitly: ``from ...engine.analyze import analyze``,
# ``from ...engine.constants import CoverageLabel``, etc.
