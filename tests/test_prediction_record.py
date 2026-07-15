"""Tests for normalize_prediction against real fixture records."""

from __future__ import annotations

import pytest

from evaluation_harness.base import normalize_prediction


@pytest.mark.parametrize(
    "agent,prefix",
    [
        ("claude_code", "claude"),
        ("cursor", "cursor"),
        ("codex", "codex"),
    ],
)
def test_normalize_maps_prefixed_keys(
    agent_results: dict, agent: str, prefix: str
) -> None:
    records = agent_results[agent]
    assert len(records) >= 1

    rec = records[0]
    normalized = normalize_prediction(rec, prefix)

    assert "agent_stdout" in normalized
    assert normalized["agent_stdout"] == rec[f"{prefix}_stdout"]
    assert "agent_stderr" in normalized
    assert normalized["agent_stderr"] == rec[f"{prefix}_stderr"]
    assert "agent_success" in normalized
    assert normalized["agent_success"] == rec[f"{prefix}_success"]


@pytest.mark.parametrize(
    "agent,prefix",
    [
        ("claude_code", "claude"),
        ("cursor", "cursor"),
        ("codex", "codex"),
    ],
)
def test_normalize_preserves_passthrough_keys(
    agent_results: dict, agent: str, prefix: str
) -> None:
    rec = agent_results[agent][0]
    normalized = normalize_prediction(rec, prefix)

    assert normalized["instance_id"] == rec["instance_id"]
    assert normalized["model_name_or_path"] == rec["model_name_or_path"]
    assert "model_patch" in normalized


def test_normalize_codex_extra_keys(agent_results: dict) -> None:
    """Codex records have extra keys (duration_ms, status, setup_ok) that
    are not in PredictionRecord.  They should be silently dropped."""
    rec = agent_results["codex"][0]
    assert "duration_ms" in rec
    normalized = normalize_prediction(rec, "codex")
    assert "duration_ms" not in normalized
    assert "status" not in normalized


def test_normalize_slim_prediction(predictions: list) -> None:
    """merged_predictions.json records only have the 3 core keys —
    no agent-prefixed stdout.  Normalization should still work."""
    rec = predictions[0]
    normalized = normalize_prediction(rec, "claude")
    assert normalized["instance_id"] == rec["instance_id"]
    assert "agent_stdout" not in normalized


def test_normalize_sweagent_pred(sweagent_pred: dict) -> None:
    """SWE-agent .pred records have the 3 core keys only."""
    normalized = normalize_prediction(sweagent_pred, "sweagent")
    assert normalized["instance_id"] == sweagent_pred["instance_id"]
    assert normalized["model_patch"] == sweagent_pred["model_patch"]
