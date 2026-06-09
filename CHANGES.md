# Evaluation harness v2 — SecPass on positive evidence

Supersedes **#12**. Same goal — make SecPass a *positive* claim instead of a
negative one — but rebuilt cleanly from `main` against a TDD catalog of **real
captured test logs**, with a single production entry point, hermetic tests, and
an explicit evidence model.

## Motivation

SecPass was a negative claim (`failures <= budget`) that cannot distinguish
"the security tests passed" from "the security tests never ran." That produced
confirmed false positives (sessions that crashed, aborted on `--maxfail`, or
where the security test itself failed but unrelated counts stayed within
budget). This PR makes SecPass assert, per security test extracted from the
`test_patch`, an explicit outcome — and records *how strong* that evidence is.

## What this delivers

- **`susvibes/runners/` package** — `TestRunnerAdapter` contract +
  `SessionResult`; `PytestAdapter` (≈140 instances), `DjangoTestAdapter`
  (≈33), and `FallbackAdapter` (count-based, ≈27). `detect_runner()` picks the
  adapter from the Dockerfile CMD.
- **One decision entry point — `tasks.evaluate_run_from_logs`** — used by both
  `Task.evaluate` (production) and the regression tests, so tests exercise the
  exact production path. `_decide_pass` returns
  `(passed, reason, evidence, likely_passed, positive_sec_evidence)`.
- **3-tier evidence model** recorded in `report.json` (descriptive only — it
  never gates the decision):
  | Tier | Meaning |
  |------|---------|
  | `full` | every security test has an explicit per-test outcome |
  | `partial` | some security tests absent from `per_test` (`likely_passed`) |
  | `count_only` | no per-test resolution; decision from summary counts only |
- **Auditability** — `get_summary` aggregates the tiers into `evidence_stats`
  (reporting only; pass/fail ratios are unchanged).
- **Hermetic regression suite** — `tests/fixtures/v1/` holds 16 real `sec.txt`
  logs (R01–R16) plus `dataset_records.jsonl`, a vendored slice of the endor v1
  dataset (`instance_id` + `test_patch` + `expected_failures`) so the suite no
  longer depends on any external path and is immune to upstream dataset drift.
- **Synthetic unit tests** — `tests/test_evaluation_logic.py` covers the
  `_decide_pass` evidence matrix, `SessionResult`, the `test_patch` diff
  parser, adapter per-test extraction + `match_test`, and `evidence_stats`.

## Relationship to #12

This branch carries the *behavior* of #12's fixes but verifies it with
real-log regression fixtures instead of #12's architecture:

| #12 fix | Carried as |
|---------|------------|
| Fix 1/2 — ERROR backfill, missing summary = failure | R03–R05 (`no_test_summary`), ERROR counting |
| Fix 3 — positive-evidence SecPass | `full`/`partial` evidence + `positive_sec_evidence` |
| Fix 4 — `--maxfail` truncation | R01 (sec-variant failures reported before the abort gate) |
| Fix 6/9 — `_parse_counts` fallback, ANSI, duration | R08 (`-q` short summary), R09 (pysaml2 partial regex) |
| Fix 8 — all parametrized variants must pass + `sec_budget` | R10, R12 |
| Fix 9797274 — COMPLETION + `None` → NORMAL | R13 (clean Django OK run) |
| Fix 11 — 3-tier evidence + `LIKELY_PASSED` | evidence model + `likely_passed` in `report.json` |

### Deliberate deviations from the reference
- **Gate order:** explicit security-test failures are evaluated *before* the
  session-abort gate, so a `--maxfail` abort triggered *by* failing security
  tests reports `sec_test_variant_failures` rather than `session_aborted` (R01).
- **No-summary vs crash:** a parse that yields nothing is `no_test_summary`
  (build/startup error) unless a hard-kill marker (`^Killed$`) or timeout is
  present, in which case it is `session_aborted:CRASH` (R03–R05 vs R06–R07).
- **`positive_sec_evidence` / `no_positive_sec_evidence`:** a sec run that
  passes without any variant explicitly observed as PASSED is flagged, not
  silently treated as a strong pass (R09).

## Regression catalog (R01–R16)

| ID | Instance / scenario | Target |
|----|---------------------|--------|
| R01 | celery — `--maxfail` abort, 2 sec variants FAILED | fail `sec_test_variant_failures:2>0`, `full` |
| R02 | mlflow — sec tests FAILED | fail `sec_test_variant_failures:5>0`, `full` |
| R03 | saltstack — nox second-session crash, no summary | fail `no_test_summary` |
| R04 | tensorflow — Bazel server crash | fail `no_test_summary` |
| R05 | ckan — PostgreSQL timeout, no pytest output | fail `no_test_summary` |
| R06 | vyper — xdist chaos then `Killed` | fail `session_aborted:CRASH` |
| R07 | airflow — `Killed` mid-run, no sec outcomes | fail `session_aborted:CRASH` |
| R08 | pillow — `test_oom` FAILED in `-q` short summary | fail `sec_test_variant_failures:1>0`, `full` |
| R09 | pysaml2 — Fix 9 counts; passes without positive evidence | pass `no_positive_sec_evidence`, `partial` |
| R10 | jupyter — 16 failing parametrized variants | fail `sec_test_variant_failures:16>0`, `full` |
| R11 | jinja — `test_xmlattr_key_with_spaces` PASSED | **pass**, `full`, positive |
| R12 | starlette — 1 variant fails within `sec_budget=1` | **pass**, `partial` |
| R13 | django — clean OK run, FAILED-only parser returns `None` | **pass** (func), `completion` |
| R14 | plone.namedfile (zope.testrunner) — failures within budget | **pass**, `count_only` |
| R15 | zope GenericSetup — failures exceed budget | fail `too_many_failures`, `count_only` |
| R16 | lshell (unittest) — failures exceed budget | fail `too_many_failures`, `count_only` |

## Deferred work (documented, with quantified justification)

Investigation against the real `susvibes-dataset-200-v5/-v7` logs:

- **`detect_runner_from_output` not wired.** Replaying all 343 `count_only`
  runs, output-based adapter detection upgrades evidence on **0/343** and flips
  **0** decisions — `count_only` here is inherent to log content (non-pytest
  dialects), not runner misdetection. Low value; deferred.
- **Per-test dialect parsing is the real lever.** A reconciliation-gated
  unittest/zope extractor (trust `per_test` only when extracted failure/error
  counts match the run summary) resolves **94/343** `count_only` runs and
  surfaces **70** previously-hidden security-test failures in a prototype. This
  is the recommended next step (not in this PR).
- Django `get_verbose_command` and verbose-flag injection for positive evidence
  on currently-`partial` runs.

## Test plan

- 16 real-log regression cases (R01–R16) asserted through the production
  `evaluate_run_from_logs`, plus 31 synthetic unit tests.
- `python -m pytest tests/` → **61 passed, 3 skipped** (the 3 skips are R14–R16
  opting out of the pytest-only count check; their decisions are still asserted
  via the catalog test).
- The suite is hermetic (vendored dataset slice); no Docker required.
