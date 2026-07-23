# Prompt Architecture

This document describes how task prompts are assembled for agent evaluation
in SusVibes, covering the canonical template, the optional tools prefix,
and the relationship between harness-level and dataset-level safety strategies.

## Design goal: prompt uniformity

All agents receive **semantically identical** task instructions for a given
benchmark instance.  Format adaptation is allowed (e.g. OpenHands uses Jinja2,
SWE-agent injects via YAML config), but the core content --- workspace path,
problem statement, anti-cheating block, 6-step workflow, and safety hint ---
must be preserved.

## Two-layer prompt model

The prompt is assembled from two layers:

### 1. Base task template (`evaluation_harness/common.py`)

`_INSTANCE_TEMPLATE_BODY` is the single source of truth.  It contains:

- `<uploaded_files>` wrapper with workspace path
- `<pr_description>` wrapper with the problem statement
- Task framing ("implement the necessary changes...")
- **IMPORTANT** constraints (pre-installed deps, no git commits, no cheating)
- **Anti-cheating block** listing prohibited strategies (git history inspection,
  web patch lookup, remote clone/fetch) with a post-processing detection warning
- **6-step workflow** guidance (find code, reproduce, edit, verify, edge cases,
  repeat)

The neutral markers `__WORK_DIR__` and `__PROBLEM_STATEMENT__` are replaced at
import time by `get_instance_template()` with each agent's placeholder syntax
(e.g. `{local_work_dir}` for Docker CLI agents).

### 2. Safety hint (`apply_safety_hint()`)

A short security best-practices reminder appended to the problem statement
before it is inserted into the template.  The text is imported from
`susvibes.eval.strategies.prompts.GENERIC_PROMPT` so the harness-level and
dataset-level hints are always byte-identical.

## Optional tools prefix

When external tools (MCP servers, CLI utilities) are configured for a run,
a tools prefix is prepended to the base template:

```
+---------------------------------------------+
| TOOLS PREFIX (optional)                     |
|  - System preamble                          |
|  - Tool blocks (per-tool instructions)      |
|  - Workflow hints (maps tools to steps)     |
|  - Security validation block                |
|  - Tool preference guidance                 |
+---------------------------------------------+
| BASE TASK TEMPLATE (always present)         |
|  - <uploaded_files> + workspace path        |
|  - Problem statement (+ safety hint)        |
|  - Anti-cheating block                      |
|  - 6-step workflow guidance                 |
+---------------------------------------------+
```

The prefix is assembled by `get_instance_template(..., tools=["codenav", ...])`,
which delegates to `evaluation_harness.tools.loader.compose_all_prompts()`.  Each
tool contributes its blocks via a pluggable `prompt.py` module.

## Relationship to the strategy pipeline (`--prepare_dataset`)

Safety text can enter the pipeline at two stages:

### Dataset-level (default for benchmark runs)

```
susvibes.eval --prepare_dataset --strategy generic
```

This calls `apply_safety_strategy()` in `susvibes/eval/strategies/tools.py`,
which bakes the strategy prompt directly into a new dataset file
(`susvibes_dataset_<run_id>_<strategy>.jsonl`).  The evaluation harness then
consumes that altered dataset, guaranteeing every harness sees identical text.

Supported strategies: `generic`, `self-selection`, `oracle`, `feedback-driven`,
`sec-test`.  See `susvibes/eval/strategies/README.md` for details.

### Harness-level (for ad-hoc experiments)

`apply_safety_hint()` in `evaluation_harness/common.py` applies the same
`generic` strategy text at runtime inside `build_prompt()`.  This gives
fine-grained per-run control without regenerating the dataset.

**Coherence safeguards:**

- `apply_safety_hint()` imports `GENERIC_PROMPT` from the strategies module
  rather than maintaining its own copy, so the text can never drift.
- `apply_safety_hint()` is **idempotent**: if the hint text is already present
  in the problem statement (because the dataset was prepared with a strategy),
  it returns the statement unchanged.  This prevents double-application.
- For official benchmark runs, the dataset-level path is the default; the
  harness-level hook exists for experimental flexibility.

## Key files

| File | Role |
|------|------|
| `evaluation_harness/common.py` | Canonical template, `get_instance_template()`, `apply_safety_hint()` |
| `evaluation_harness/base.py` | `AgentHarness` Protocol, `AgentResult`, `PredictionRecord` |
| `susvibes/eval/strategies/prompts.py` | Strategy prompt text (source of truth for `GENERIC_PROMPT`) |
| `susvibes/eval/strategies/tools.py` | `apply_safety_strategy()` for dataset preparation |
| `evaluation_harness/tools/loader.py` | `compose_all_prompts()` for tool prefix assembly (optional package) |
