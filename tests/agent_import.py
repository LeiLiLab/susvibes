"""Helper for importing per-agent modules using their sys.path convention.

The agent scripts (e.g. ``claude_code/run_docker.py``) import their siblings
as top-level modules (``from base import ...``, ``from prompts import ...``)
after inserting the ``evaluation_harness`` directory on ``sys.path``.  Tests
replicate that convention here so they exercise the modules exactly as the
batch runners do.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "evaluation_harness"

# Modules that each agent directory defines as top-level names.
_AGENT_LOCAL_MODULES = ("run_docker", "prompts")


def import_agent_module(agent_dir: str, module_name: str = "run_docker"):
    """Import ``module_name`` from an agent directory as a top-level module.

    Cleans up ``sys.path`` and ``sys.modules`` afterward so repeated calls
    for different agents don't collide on the shared module names.
    """
    agent_path = str(HARNESS_ROOT / agent_dir)
    parent_path = str(HARNESS_ROOT)
    added = []
    for p in (agent_path, parent_path):
        if p not in sys.path:
            sys.path.insert(0, p)
            added.append(p)
    for name in _AGENT_LOCAL_MODULES:
        sys.modules.pop(name, None)
    try:
        return importlib.import_module(module_name)
    finally:
        for name in _AGENT_LOCAL_MODULES:
            sys.modules.pop(name, None)
        for p in added:
            if p in sys.path:
                sys.path.remove(p)
