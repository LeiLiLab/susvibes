---
name: pre-pr-e2e
description: >-
  Run end-to-end smoke tests before opening a PR. Analyzes the diff to decide
  which harnesses are impacted, then runs 1 instance per affected harness.
  Fail on any harness = stop immediately.
---
# Pre-PR End-to-End Testing

Before opening a PR on this repository, you MUST run this protocol to verify
that the evaluation harness still works end-to-end. This avoids merging code
that passes unit tests but breaks real agent execution.

---

## Step 1: Analyze the diff

Run:

```bash
git diff origin/main...HEAD --stat
```

Classify the changed files using the table below to decide which harnesses
need an e2e run.

### Discovering harnesses

A "harness" is any subdirectory of `evaluation_harness/` that contains a
`batch_run_docker.py`. List them dynamically:

```bash
ls evaluation_harness/*/batch_run_docker.py | sed 's|evaluation_harness/||;s|/batch_run_docker.py||'
```

Do NOT hardcode harness names in your reasoning — always discover them.

### Scope inference table

| Files changed | Decision |
|---|---|
| `evaluation_harness/base.py`, `evaluation_harness/common.py`, `susvibes/core/utils.py`, `susvibes/eval/` | **Shared code** — impacts ALL harnesses. Run ALL discovered harnesses. |
| Only files under `evaluation_harness/<H>/` for a single harness `<H>` | **Harness-specific** — run only `<H>`. |
| Files under multiple harness dirs | Run each affected harness. |
| Only `tests/**`, `CHANGELOG.md`, `pyproject.toml`, `*.md`, `.cursor/` | **No harness code changed** — skip e2e. Report "no harness code affected, safe to PR" and stop. |

If you are uncertain whether a change impacts runtime behavior, err on the side
of running all discovered harnesses.

---

## Step 2: Check prerequisites

Before running, verify:

- [ ] Docker daemon is running (`docker info` succeeds)
- [ ] `.env` file exists at repo root with the required API keys (see model table below)
- [ ] Required env vars are exported for each harness (e.g. `ANTHROPIC_MODEL`, `GEMINI_MODEL`)

If any prereq fails, report what is missing and stop.

---

## Step 3: Run 1 instance per affected harness

For each harness determined in Step 1, run sequentially (one at a time):

```bash
cd evaluation_harness/<HARNESS>
source ../../.env
<ENV_OVERRIDES> python3 batch_run_docker.py \
  --jsonl_file ../../tests/e2e/test_instances.jsonl \
  --num_instances 1 \
  --results_dir ../../results/pre-pr-e2e/<HARNESS> \
  --model <MODEL>
```

### Model & env assignments (cheapest per harness)

| Harness | Model | Env override | Required env var |
|---|---|---|---|
| `claude_code` | `claude-sonnet-4-5-20250929` | `ANTHROPIC_MODEL=claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` |
| `gemini_cli` | `gemini-2.5-flash` | `GEMINI_MODEL=gemini-2.5-flash` | `GEMINI_API_KEY` |
| *(new harness)* | Check its `--model` default or cheapest model its API supports | Check setup-env.sh for the env var that controls model selection | Check its .env requirements |

### Verification (per harness)

After the command completes, check:

1. Exit code is 0.
2. `results/pre-pr-e2e/<HARNESS>/<model>/<timestamp>/final_results.json` exists
   and contains exactly 1 entry.
3. That entry has a non-empty `model_patch` (not whitespace-only).
4. No Python tracebacks in stdout/stderr.

**Fail-fast**: if any harness fails, STOP. Report the failure, do NOT proceed
to the next harness or to PR creation. Diagnose and fix first.

---

## Step 4 (optional): Parallel run

Only run this when explicitly requested, or when changes touch
`parallel_batch_run.py` itself. Validates that subprocess-sharding still works.

```bash
cd evaluation_harness/<HARNESS>
source ../../.env
<ENV_OVERRIDES> python3 parallel_batch_run.py \
  --jsonl_file ../../tests/e2e/test_instances.jsonl \
  --num_instances 3 \
  --num_processes 2 \
  --results_dir ../../results/pre-pr-e2e/<HARNESS>-parallel \
  --model <MODEL>
```

Verify: exit code 0, 3 entries total with non-empty patches, no tracebacks.

---

## Step 5: Report

If all harnesses pass:

> **E2E PASS** — all affected harnesses produced valid patches (1 instance
> each). Safe to proceed with PR creation.

If any harness fails:

> **E2E FAIL** — `<HARNESS>` failed. Error: `<summary>`.
> Do NOT open PR until fixed.

---

## Cleanup

After a successful e2e run, the `results/pre-pr-e2e/` directory can be deleted.
It is gitignored and should never be committed.

---

## Rollback on failure

If the e2e test fails due to a code change in this branch:

1. Do NOT proceed to PR.
2. Identify the failing step (batch_run crash? empty patch? traceback?).
3. Check logs in `results/pre-pr-e2e/<HARNESS>/` for details.
4. Fix the issue, re-run from Step 3.

If the failure is transient (API timeout, Docker pull flake), retry once. If it
fails again, investigate infrastructure before retrying.

---

## Test dataset

The test set lives at `tests/e2e/test_instances.jsonl`:

| # | instance_id | Ecosystem |
|---|---|---|
| 1 | `django__django_c5544d289233f501917e25970c03ed444abbd4f0` | Django (Python web framework) |
| 2 | `bottlepy__bottle_6d7e13da0f998820800ecb3fe9ccee4189aefb54` | Bottle (lightweight WSGI) |
| 3 | `ghantoos__lshell_e72dfcd1f258193f9aaea3591ecbdaed207661a0` | lshell (restricted shell) |

Only 1 instance is used per run (the first in the file). The full set exists
for the optional parallel pass or future expansion.
