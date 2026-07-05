# Advanced Security Strategies

SusVibes supports advanced security-enhancing strategies, applying prompting-based guidance from different aspects. This feature lets you prepare datasets that incorporate specific guidance and evaluate how agents perform under these enhanced circumstances.

## Available Strategies

| Strategy | Description |
|----------|-------------|
| **`generic`** | Provides general security guidelines to the agent without specific vulnerability information |
| **`self-selection`** | Allows the agent to select relevant security concerns from a provided list of all possible CWE (Common Weakness Enumeration) types in the dataset |
| **`oracle`** | Provides the agent with the exact CWE vulnerabilities that are relevant to the specific task |
| **`feedback-driven`** | Iteratively improve the implementation based on feedback from executing security tests. This mode requires `--feedback_tool` and agent-level integration. (Coming soon) |
| **`sec-test`** | Exposes the agent to the actual security test patch used for evaluation, allowing it to inspect the tests and implement a solution that is explicitly secure against them |

## 1. Prepare Enhanced Dataset

First, enhance the dataset with your chosen strategy. This will generate a new dataset file `susvibes_dataset_<run_id>_<strategy>.jsonl` in the `datasets/` directory (`<run_id>` defaults to `default`).

```bash
python -m susvibes.eval.core \
  --prepare_dataset \
  --strategy <strategy_name>
```

## 2. Run Agent with Enhanced Dataset

After preparing the enhanced dataset, use it to harness your agent instead. Evaluate with the same command as in the main [README](../../../README.md#step-2-evaluate-the-agents-solutions), plus the strategy option:

```bash
python -m susvibes.eval.core \
  --strategy <strategy_name> \
  # ... other parameters
```

For `feedback-driven`, also pass `--feedback_tool <tool_name>`.

This lets you assess additional security measures and insights in place. The evaluation logs and summary for each run are written to `logs/eval/<run_id>/<strategy>/<model_name_or_path>/`, so different strategies under the same `run_id` are kept separate.

## Module Contents

| File | Purpose |
|------|---------|
| `prompts.py` | Prompt templates for each strategy (`GENERIC_PROMPT`, `ORACLE_PROMPT`, `SELF_SELECTION_PROMPT`, ...) |
| `cwes.yaml` | The full CWE catalog (id → name) used to render the CWE lists injected into prompts |
| `tools.py` | `apply_safety_strategy(...)` builds the per-task problem statement for a strategy; also `eval_selected_cwes` / `get_cwe_selection_stats` for scoring `self-selection` runs |
