"""Helpers for regression tests over real evaluation logs.

Loads catalog + dataset + env specs. Asserts target expectations from
``fixtures/v1/regression_catalog.json``.

Do **not** mirror production evaluation logic here. Parser count targets use
the real ``_parse_counts`` helper; decision targets require a production API
(``susvibes.eval.task.evaluate_run_from_logs``) that does not exist yet.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from susvibes.core.env import Env
from susvibes.core.utils import load_file as _load_file


def _parse_counts(log_text: str, logs_parser: dict):
    """Production Fix 9 parser — lazy import so tests collect before Phase 2."""
    try:
        from susvibes.runners.pytest import _parse_counts as parse_fn
    except ImportError:
        pytest.fail(
            "susvibes.runners.pytest._parse_counts not implemented yet (Phase 2)"
        )
    return parse_fn(log_text, logs_parser)


def _detect_runner(dockerfile: str):
    try:
        from susvibes.runners import detect_runner
    except ImportError:
        pytest.fail("susvibes.runners not implemented yet (Phase 2)")
    return detect_runner(dockerfile)

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Catalog + sec logs are namespaced per authoritative dataset version (v1).
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "v1"
_CATALOG_PATH = _FIXTURES_DIR / "regression_catalog.json"
# Vendored, in-repo slice of the endor v1 dataset (instance_id + test_patch +
# expected_failures) for exactly the catalog instances. This is the source of
# truth so the suite is hermetic and immune to upstream dataset drift: the
# catalog's expected values were derived from this snapshot, so the inputs are
# pinned to it too. Regenerate with scripts when adding catalog cases.
_VENDORED_RECORDS = _FIXTURES_DIR / "dataset_records.jsonl"
# Optional gap-fillers (only used for not-yet-vendored cases). The in-repo
# `datasets/default` copy can be stale (e.g. celery's test_patch has 2 added
# tests there vs 3 in v1), so it is listed last and never overrides vendored.
_AUTHORITATIVE_DATASET = Path(
    "/data/agent-sec-leagues/endor-susvibes/datasets/v1/susvibes_dataset.jsonl"
)
_DATASET_PATHS = (
    _VENDORED_RECORDS,
    _AUTHORITATIVE_DATASET,
    _REPO_ROOT / "datasets" / "default" / "susvibes_dataset.jsonl",
)
# Vendored env specs (dockerfile + logs_parser/checker) for exactly the catalog
# instances, so the suite is hermetic and independent of the live env_specs layout.
_COMPONENTS_PATH = _FIXTURES_DIR / "components.json"


@lru_cache(maxsize=1)
def load_regression_catalog() -> list[dict]:
    return _load_file(_CATALOG_PATH)


@lru_cache(maxsize=1)
def _load_components() -> dict:
    return _load_file(_COMPONENTS_PATH)


@lru_cache(maxsize=1)
def _load_dataset_index() -> dict[str, dict]:
    # First path wins on conflict (v1 authoritative); later paths only fill gaps.
    index: dict[str, dict] = {}
    for path in _DATASET_PATHS:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                record = json.loads(line)
                index.setdefault(record["instance_id"], record)
    return index


def load_instance_record(instance_id: str) -> dict:
    record = _load_dataset_index().get(instance_id)
    if record is None:
        pytest.fail(
            f"instance_id {instance_id} not found in vendored records "
            f"({_VENDORED_RECORDS}) or fallback datasets. Add it to the vendored "
            "slice (instance_id, test_patch, expected_failures from endor v1)."
        )
    return record


def make_env_for_instance(instance_id: str) -> Env:
    spec = _load_components()[instance_id]
    env = object.__new__(Env)
    # logs_parser / logs_checker are read-only properties over logs_handler.
    env.logs_handler = {"count": {
        "logs_parser": spec["logs_parser"],
        "logs_checker": spec.get("logs_checker"),
    }}
    env.dockerfile = spec["dockerfile"]
    return env


def failure_budget(record: dict, run_name: str) -> int:
    """Accumulated budget per run (must match production ``Task.evaluate``)."""
    ef = record["expected_failures"]
    if run_name == "sec":
        return ef["func"] + ef["sec"]
    return ef[run_name]


def load_fixture_text(case: dict) -> str:
    return (_FIXTURES_DIR / "sec_logs" / case["fixture"]).read_text()


def parse_target_counts(log_text: str, env: Env) -> dict[str, int] | None:
    """Target parser output (Fix 9 ``_parse_counts``). None when unparseable."""
    counts = _parse_counts(log_text, env.logs_parser)
    return counts or None


def production_eval_report(case: dict) -> dict:
    """Obtain evaluation report from production — no test-side reimplementation."""
    from susvibes.eval import task as tasks

    if not hasattr(tasks, "evaluate_run_from_logs"):
        pytest.fail(
            f"{case['id']}: decision spec is defined but "
            "susvibes.eval.task.evaluate_run_from_logs is not implemented yet"
        )

    record = load_instance_record(case["instance_id"])
    env = make_env_for_instance(case["instance_id"])
    adapter = _detect_runner(env.dockerfile)
    return tasks.evaluate_run_from_logs(
        load_fixture_text(case),
        run_name=case["run_name"],
        env=env,
        adapter=adapter,
        test_patch=record["test_patch"],
        expected_failures=failure_budget(record, case["run_name"]),
        sec_budget=record["expected_failures"].get("sec", 0),
    )


def assert_case_expectations(case: dict) -> None:
    """Assert catalog target spec. Fails until production catches up."""
    expect = case["expect"]
    record = load_instance_record(case["instance_id"])
    env = make_env_for_instance(case["instance_id"])
    log_text = load_fixture_text(case)

    # (1) Parser counts
    if "counts" in expect:
        target_counts = expect["counts"]
        if target_counts is None:
            assert parse_target_counts(log_text, env) is None, (
                f"{case['id']}: expected unparseable summary"
            )
        else:
            actual_counts = parse_target_counts(log_text, env)
            assert actual_counts is not None, (
                f"{case['id']}: expected parseable summary"
            )
            for key, value in target_counts.items():
                assert actual_counts.get(key) == value, (
                    f"{case['id']}: counts[{key}] expected {value}, "
                    f"got {actual_counts.get(key)} (full: {actual_counts})"
                )

    decision = expect["decision"]
    if "failure_budget" in decision:
        assert failure_budget(record, case["run_name"]) == decision["failure_budget"]

    # (2) Pass / fail decision — production API only
    result = production_eval_report(case)

    assert result["pass"] is decision["pass"], (
        f"{case['id']}: pass expected {decision['pass']}, got {result['pass']}; "
        f"reason={result.get('reason')!r}, evidence={result.get('evidence')!r}"
    )

    if "reason" in decision:
        assert result.get("reason") == decision["reason"], (
            f"{case['id']}: reason expected {decision['reason']!r}, "
            f"got {result.get('reason')!r}"
        )

    if case["run_name"] == "sec":
        if "evidence" in decision:
            assert result.get("evidence") == decision["evidence"], (
                f"{case['id']}: evidence expected {decision['evidence']!r}, "
                f"got {result.get('evidence')!r}"
            )
        if "positive_sec_evidence" in decision:
            assert result.get("positive_sec_evidence") is (
                decision["positive_sec_evidence"]
            ), (
                f"{case['id']}: positive_sec_evidence expected "
                f"{decision['positive_sec_evidence']}, "
                f"got {result.get('positive_sec_evidence')}"
            )
        if "likely_passed" in decision:
            assert sorted(result.get("likely_passed") or []) == sorted(
                decision["likely_passed"]
            ), (
                f"{case['id']}: likely_passed mismatch\n"
                f"  expected: {decision['likely_passed']}\n"
                f"  actual:   {result.get('likely_passed')}"
            )

    if "status" in decision:
        assert result["status"] == decision["status"], (
            f"{case['id']}: status expected {decision['status']!r}, "
            f"got {result['status']!r}"
        )
