---
name: pre-pr-e2e
description: >-
  Run end-to-end smoke tests before opening a PR. Analyzes the diff to decide
  which harnesses are impacted, then executes a two-pass protocol (1 instance
  smoke, then remaining 2) with the cheapest model per harness.
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
| `evaluation_harness/base.py`, `evaluation_harness/common.py`, `susvibes/core/utils.py`, `susvibes/eval/` | **Shared code** — impacts ALL harnesses. Run at least one (pick the cheapest). If the change touches Protocol shape, `execute_in_container`, `setup_persistent_workspace`, `cleanup`, or `_extract_code_from_image`, run ALL discovered harnesses. |
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
- [ ] The test images are pullable (they will be pulled with `--pull always` during the run)

If any prereq fails, report what is missing and stop.

---

## Step 3: Execute Pass 1 (smoke — 1 instance)

For each harness determined in Step 1, run:

```bash
cd evaluation_harness/<HARNESS>
python3 batch_run_docker.py \
  --jsonl_file ../../tests/e2e/test_instances.jsonl \
  --num_instances 1 \
  --results_dir ../../results/pre-pr-e2e/<HARNESS> \
  --model <MODEL>
```

### Model assignments (cheapest per harness)

Pick the cheapest available model for each harness. Use the table below as a
starting point; if a new harness is added, check its `batch_run_docker.py`
defaults and the `.env` for available API keys.

| Harness | Model flag | Required env var |
|---|---|---|
| `claude_code` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` |
| `gemini_cli` | `gemini-2.5-flash` | `GOOGLE_API_KEY` (or in `.env`) |
| *(new harness)* | Check its `--model` default or use the cheapest model its API supports | Check its setup-env.sh / .env |

### Pass 1 verification

After the command completes, check:

1. Exit code is 0.
2. `results/pre-pr-e2e/<HARNESS>/<model>/<timestamp>/final_results.json` exists
   and contains exactly 1 entry.
3. That entry has a non-empty `model_patch` (not whitespace-only).
4. No Python tracebacks in stdout, stderr, or any `*.log` file in the results dir.

**Fail-fast**: if any check fails, STOP. Report the failure, do NOT proceed to
Pass 2 or to PR creation. Diagnose the issue and fix it first.

---

## Step 4: Execute Pass 2 (full — remaining 2 instances)

Run the same command with `--num_instances 3`. The harness auto-skips the
instance already completed in Pass 1.

```bash
cd evaluation_harness/<HARNESS>
python3 batch_run_docker.py \
  --jsonl_file ../../tests/e2e/test_instances.jsonl \
  --num_instances 3 \
  --results_dir ../../results/pre-pr-e2e/<HARNESS> \
  --model <MODEL>
```

### Pass 2 verification

1. Exit code is 0.
2. `final_results.json` now contains exactly 3 entries.
3. All 3 have non-empty `model_patch`.
4. No Python tracebacks anywhere in the results dir.

---

## Step 5: Execute Pass 3 (parallel — validates multi-process coordination)

After Pass 2 succeeds, run the parallel batch runner on a fresh output dir to
verify that the subprocess-sharding logic still works with the refactored code.

```bash
cd evaluation_harness/<HARNESS>
python3 parallel_batch_run.py \
  --jsonl_file ../../tests/e2e/test_instances.jsonl \
  --num_instances 3 \
  --num_processes 2 \
  --results_dir ../../results/pre-pr-e2e/<HARNESS>-parallel \
  --model <MODEL>
```

### Pass 3 verification

1. Exit code is 0.
2. The merged `final_results.json` (or per-process files) together contain 3
   entries with non-empty `model_patch`.
3. No Python tracebacks in any log or output.

If a harness does not have `parallel_batch_run.py`, skip Pass 3 for that
harness (it is not mandatory for harnesses without a parallel runner).

---

## Step 6: Report

If all passes succeed for all affected harnesses:

> **E2E PASS** — all affected harnesses produced valid patches on 3/3 test
> instances (sequential + parallel). Safe to proceed with PR creation.

If any pass fails:

> **E2E FAIL** — `<HARNESS>` failed at Pass `<N>`, instance
> `<instance_id>`. Error: `<summary>`. Do NOT open PR until fixed.

---

## Step 7: Cleanup

After a successful e2e run, the `results/pre-pr-e2e/` directory can be deleted.
It is gitignored and should never be committed.

---

## Rollback on failure

If the e2e test fails due to a code change in this branch:

1. Do NOT proceed to PR.
2. Identify the failing step (batch_run crash? empty patch? traceback?).
3. Check logs in `results/pre-pr-e2e/<HARNESS>/` for details.
4. Fix the issue, re-run the full protocol from Pass 1.

If the failure is transient (API timeout, Docker pull flake), retry once. If it
fails again, investigate infrastructure before retrying.

---

## Test dataset

The 3-instance test set lives at `tests/e2e/test_instances.jsonl`:

| # | instance_id | Ecosystem |
|---|---|---|
| 1 | `django__django_c5544d289233f501917e25970c03ed444abbd4f0` | Django (Python web framework) |
| 2 | `bottlepy__bottle_6d7e13da0f998820800ecb3fe9ccee4189aefb54` | Bottle (lightweight WSGI) |
| 3 | `ghantoos__lshell_e72dfcd1f258193f9aaea3591ecbdaed207661a0` | lshell (restricted shell) |

These have been validated: all major agent/model combos produce non-empty
patches on all 3 instances.
