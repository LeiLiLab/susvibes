"""Verify that all evaluation_harness modules import cleanly.

Catches issues like the ADDITIONAL_INSTRUCTIONS breakage where a thin-wrapper
prompts.py stopped exporting a symbol that run_docker.py still imported.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "evaluation_harness"

# Modules that can be imported directly (no agent-specific sys.path setup).
TOP_LEVEL_MODULES = [
    "evaluation_harness.base",
    "evaluation_harness.common",
]

# Per-agent modules use relative imports from their own directory, so we
# replicate the sys.path convention the batch runners use.
AGENT_DIRS = ["claude_code", "gemini_cli"]
AGENT_MODULES = ["prompts"]


@pytest.mark.parametrize("module_name", TOP_LEVEL_MODULES)
def test_top_level_import(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert mod is not None


@pytest.mark.parametrize("agent_dir", AGENT_DIRS)
@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_agent_module_import(agent_dir: str, module_name: str) -> None:
    """Import agent modules using the same sys.path hack the scripts use."""
    agent_path = str(HARNESS_ROOT / agent_dir)
    parent_path = str(HARNESS_ROOT)
    added = []
    for p in (agent_path, parent_path):
        if p not in sys.path:
            sys.path.insert(0, p)
            added.append(p)
    try:
        if module_name in sys.modules:
            del sys.modules[module_name]
        mod = importlib.import_module(module_name)
        assert hasattr(mod, "USER_PROMPT_TEMPLATE")
    finally:
        for p in added:
            sys.path.remove(p)
        if module_name in sys.modules:
            del sys.modules[module_name]
