"""
Regression catalog R01–R13: structured target spec (TDD).

Each catalog entry defines:
  (1) ``expect.counts`` — target FAILED/PASSED/SKIPPED from Fix 9 parser
  (2) ``expect.decision`` — target pass/fail, reason, evidence, sec metadata

Tests assert the target spec. No evaluation logic is mirrored in tests —
decision checks call ``susvibes.eval.task.evaluate_run_from_logs`` when it
exists. Until Phase 2, most tests fail (that is intentional).
"""

import pytest

from tests.eval_regression_support import (
    assert_case_expectations,
    load_regression_catalog,
    parse_target_counts,
    load_fixture_text,
    make_env_for_instance,
)

CATALOG = load_regression_catalog()


@pytest.mark.parametrize("case", CATALOG, ids=[c["id"] for c in CATALOG])
def test_regression_catalog_expectations(case):
    """Assert full structured expectation from regression_catalog.json."""
    assert_case_expectations(case)


def test_regression_status_matrix(capsys):
    """Print catalog target spec (always passes; for inspection)."""
    rows = []
    for case in CATALOG:
        decision = case["expect"]["decision"]
        counts = case["expect"].get("counts")
        rows.append(
            f"{case['id']:4}  "
            f"pass={str(decision['pass']):5}  "
            f"reason={decision.get('reason') or '-':28}  "
            f"counts={counts!s:32}  "
            f"{case['note']}"
        )
    print("\n".join(["Regression catalog (target spec):", *rows]))


@pytest.mark.parametrize("case", CATALOG, ids=[c["id"] for c in CATALOG])
def test_regression_parser_counts_only(case):
    """Parser target counts — independent of decision API."""
    if "counts" not in case["expect"]:
        pytest.skip("no counts expectation")
    env = make_env_for_instance(case["instance_id"])
    target = case["expect"]["counts"]
    actual = parse_target_counts(load_fixture_text(case), env)
    if target is None:
        assert actual is None
    else:
        assert actual is not None
        for key, value in target.items():
            assert actual.get(key) == value
