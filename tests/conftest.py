"""Shared pytest configuration and fixtures for the evaluation_harness tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def agent_results() -> dict[str, list[dict]]:
    with open(FIXTURES_DIR / "agent_results.json") as f:
        return json.load(f)


@pytest.fixture()
def predictions() -> list[dict]:
    with open(FIXTURES_DIR / "predictions.json") as f:
        return json.load(f)


@pytest.fixture()
def sweagent_pred() -> dict:
    with open(FIXTURES_DIR / "sweagent_pred.json") as f:
        return json.load(f)


@pytest.fixture()
def problem_statements() -> dict[str, str]:
    with open(FIXTURES_DIR / "problem_statements.json") as f:
        return json.load(f)
