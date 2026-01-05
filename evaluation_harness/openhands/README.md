# OpenHands Evaluation Harness

This directory contains instructions for running OpenHands evaluations on code repositories using Docker containers.

## Overview

The evaluation harness leverages OpenHands's interface. The standard approach is to write evaluation scripts within the OpenHands repository's evaluation section. Below, we provide detailed installation guidelines and example evaluation scripts.

## Files

- **`susvibes/run_infer.py`** - Main evaluation script for running SusVibes tasks with OpenHands agents
- **`susvibes/configs/default.j2`** - Jinja2 template for generating instructions for OpenHands
- **`susvibes/susvibes_dataset.jsonl`** - Dataset file (to be copied)

## Usage

### Installation

Follow the OpenHands development [guideline](https://github.com/OpenHands/OpenHands/blob/main/Development.md) for installation.

### Parallel Batch Processing

After installation, copy the `susvibes` scripts folder from this directory into the OpenHands repository at `evaluation/benchmarks/`. Place the SusVibes dataset file in `evaluation/benchmarks/susvibes`.

Then you are ready to run:

```bash
# In the OpenHands repo
poetry run python evaluation/benchmarks/susvibes/run_infer.py \
    --dataset susvibes_dataset.jsonl \
    --agent-cls CodeActAgent \
    --llm-config <llm-config-name> \
    --max-iterations <max-iterations> \
    --eval-num-workers 4 \
    --eval-note run_evaluation_instances
```

It is recommended to set `<max-iterations> ≈ 200` and `<max_output_tokens> ≤ 32000` when configuring your backbone LLM.

## Requirements

- OpenHands installed with credentials configured
