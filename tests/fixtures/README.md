# Test Fixtures

Small sanitized excerpts from real agent evaluation runs, used by the
`tests/` suite to validate the `AgentHarness` Protocol, prompt template,
safety hint, and `PredictionRecord` normalization without running any agent
or Docker container.

## Approach

Fixtures are **manually curated**: when a test needs data from a real run,
we copy the relevant records by hand from
`/data/agent-sec-leagues/results/susvibes-dataset-200` (or `.old` for
SWE-agent/OpenHands), truncate large fields (stdout, stderr, patches) to a
few KB, and commit the result here.  This keeps fixtures minimal, targeted,
and easy to understand.

Add new fixture files as needed when writing new tests -- there is no
auto-generation script.

## Current contents

| File | Source | Description |
|------|--------|-------------|
| `agent_results.json` | `final_results.json` shards | 2 records each for `claude_code`, `cursor`, `codex` with agent-prefixed keys |
| `predictions.json` | `merged_predictions.json` | 2 slim prediction records (instance_id, model_name_or_path, model_patch) |
| `sweagent_pred.json` | `.pred` files | 1 SWE-agent prediction record |
| `problem_statements.json` | dataset log JSONL | 1 plain + 1 strategy-prepared problem statement |

## Provenance

Current fixtures were extracted from:

- CLI agents: `/data/agent-sec-leagues/results/susvibes-dataset-200/{claude_code,cursor,codex}/`
- SWE-agent: `/data/agent-sec-leagues/results/susvibes-dataset-200.old/sweagent/`
- Problem statements: dataset logs under `logs/todo_dataset_*.jsonl`

Stdout/stderr fields are truncated; workspace paths are replaced with
placeholders.
