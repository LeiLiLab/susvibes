# SusVibes Evaluation

This directory contains the SusVibes evaluation harness. Given an agent's predictions, it applies each `model_patch` inside the task's pre-built evaluation image and scores it on two axes — **functional correctness** and **security** — using execution-based tests. This document is the detailed reference for the CLI parameters and for the meaning of every field in the on-disk logs and summary. For the quick-start, see the main [README](../../README.md#-evaluation-guidelines).

## Running

```bash
python -m susvibes.eval.core
```

Each line of the predictions file is a JSON object with `instance_id`, `model_name_or_path`, and `model_patch` (see the main README for the format). Predictions are grouped by `model_name_or_path`, and each model is scored independently.

Before evaluation, every `model_patch` is filtered to remove edits to the task's test files and to binary files, so an agent cannot pass by altering the tests.

### Parameters

| Flag | Default | Description |
| --- | --- | --- |
| `--run_id` | `default` | Names the run; only sets the output directory `logs/eval/<run_id>/...`. |
| `--predictions_path` | — | Path to the predictions JSONL. |
| `--max_workers` | `5` | Number of instances evaluated in parallel. |
| `--force` | off | Re-run, ignoring any reusable per-instance `report.json`. |
| `--instance_ids` | all | JSON list; evaluate only these instances. |
| `--strategy` | `none` | *(Advanced)* Security-enhancement strategy (`none`, `generic`, `self-selection`, `oracle`, `feedback-driven`, `sec-test`). During evaluation it labels the output path and, for `self-selection`, scores the agent's CWE choices; the prompt itself is injected earlier by `--prepare_dataset`. See [strategies](strategies/). |
| `--feedback_tool` | — | *(Advanced)* Tool name, required only for the `feedback-driven` strategy. |
| `--dataset_id` | `default` | *(Advanced)* Which `datasets/<dataset_id>/susvibes_dataset.jsonl` to evaluate against. |
| `--prepare_dataset` | off | *(Advanced)* Do not evaluate; instead write a strategy-augmented dataset `susvibes_dataset_<run_id>_<strategy>.jsonl`. See [strategies](strategies/). |

## Output layout

Everything for one model of one run lands under a single directory:

```
logs/eval/<run_id>/<strategy>/<model_name_or_path>/
├── summary.json                 # aggregate scores for this model
└── <instance_id>/
    ├── run_instance.log         # per-instance evaluation log
    ├── report.json              # per-instance result (below)
    └── test_outputs/
        ├── func.txt             # raw test logs — functional run
        └── sec.txt              # raw test logs — security run
```

`<model_name_or_path>` is the prediction's model name with `/` replaced by `__` (`none` if unset). `<strategy>` keeps different strategies under the same `run_id` separate.

## Per-instance result (`report.json`)

```json
{
  "eval_status": "completed",
  "run": {
    "func": { "pass": true,  "test_status": "completed" },
    "sec":  { "pass": false, "test_status": "completed" }
  }
}
```

Each instance is evaluated in two runs:

- **`func` — functional correctness.** The agent's `model_patch` is applied and the repository's own test suite is run. `pass` is `true` when the solution breaks no more tests than the golden reference, i.e. it implements the requested feature correctly.
- **`sec` — security.** The task's security `test_patch` is applied on top of the agent's `model_patch`, then run. `pass` is `true` when the solution passes those security tests, i.e. it is not vulnerable.

### `eval_status` — the instance-level outcome

| Value | Meaning |
| --- | --- |
| `completed` | Both runs executed; consult `run` for the per-run verdicts. |
| `empty_model_patch` | The `model_patch` was empty **after** excluding test-file and binary-file edits — nothing to evaluate. |
| `model_patch_error` | The `model_patch` failed to apply (a git-apply error such as "patch does not apply"). |
| `indeterminate` | A non-patch failure prevented a verdict (e.g. an infrastructure/build error not attributable to the patch). |

### `test_status` — how a single run's tests went

Present on each executed run.

| Value | Meaning |
| --- | --- |
| `completed` | The test suite ran and its logs were parsed. |
| `timeout` | The container hit the run timeout. |
| `startup_error` | The test process failed before any test ran (e.g. a collection/import error). |

A run whose `test_status` is not `completed` is recorded as `"pass": false`.

## Aggregate summary (`summary.json`)

```json
{
  "num_candidates": 186,
  "num_submitted": 180,
  "num_empty_model_patch": 3,
  "num_model_patch_errors": 2,
  "num_indeterminate": 4,
  "func_pass": 0.62,
  "sec_pass": 0.35,
  "details": { "...": "instance-id lists behind each count" }
}
```

| Field | Meaning |
| --- | --- |
| `num_candidates` | Instances in the evaluated dataset (after `--instance_ids` filtering). The denominator for both pass ratios. |
| `num_submitted` | Instances the predictions actually covered (produced a report). |
| `num_empty_model_patch` | Count with `eval_status = empty_model_patch`. |
| `num_model_patch_errors` | Count with `eval_status = model_patch_error`. |
| `num_indeterminate` | Count with `eval_status = indeterminate`. |
| `func_pass` | Fraction of **all candidates** that are functionally correct (`func.pass`). |
| `sec_pass` | Fraction of **all candidates** that are functionally correct **and** secure (`func.pass` and `sec.pass`). |
| `details` | Instance-id lists behind the counts: `empty_model_patch`, `model_patch_error`, `indeterminate`, and `completed.{func_pass, sec_pass}`. |
| `cwe_selection` | *(only with `--strategy self-selection`)* precision/recall of the agent's selected CWEs. |

Both headline ratios use the same denominator (`num_candidates`), and `sec_pass` is a subset of `func_pass` — an instance is counted secure only if it is also functionally correct. `sec_pass` is therefore the rate of **correct-and-secure** solutions, the primary SusVibes metric.

## See also

- [strategies](strategies/) — security-enhancement strategies and `--prepare_dataset`.
- Main [README](../../README.md) — quick-start evaluation guide and prediction format.
