# -*- coding: utf-8 -*-
"""Self-contained, py2/py3 dual-compatible test-mask engine.

COPYied into a version-matched ``static_py`` container and run under that interpreter, so it has ZERO
susvibes dependencies and is written in the py2/py3 common subset (no f-string, no py3-only
annotations). Why a container at all: the host cannot parse a Python 2 test file — its ``ast``
rejects ``print "x"`` / ``except E, e`` / ``123L`` outright, and parso >= 0.8 dropped the 2.7
grammar. Inside the image, parso 0.7.1 parses either dialect.

Internal imports are RELATIVE so the package works as a top-level
``_susvibes_test_mask_engine`` (container: ``python -m _susvibes_test_mask_engine.worker``).
"""
from __future__ import print_function, division, absolute_import, unicode_literals
