# OpenHands Evaluation Harness

This directory contains tools for running OpenHands evaluations on a SusVibes dataset and
converting the resulting trajectories into the standard format. This harness targets
**OpenHands 0.54**.

## Overview

The run script lives inside the OpenHands repository's evaluation section and produces an
`output.jsonl`; `convert.py` turns that into the standard OpenAI-messages format.

## Files

- **`susvibes/run_infer.py`** - Runs OpenHands over a dataset.
- **`susvibes/configs/default.j2`** - Jinja2 template for the OpenHands instructions.
- **`susvibes/susvibes_dataset.jsonl`** - Dataset file (to be copied).
- **`convert.py`** - Converts OpenHands `output.jsonl` into the standard format (OpenAI messages).

## Usage

### Installation

Follow the OpenHands [development guideline](https://github.com/All-Hands-AI/OpenHands/blob/main/Development.md)
to install. Then copy this `susvibes` scripts folder into the OpenHands repository at
`evaluation/benchmarks/`, and place the SusVibes dataset in `evaluation/benchmarks/susvibes`.

### Run

```bash
# In the OpenHands repo
poetry run python evaluation/benchmarks/susvibes/run_infer.py \
    --dataset_path susvibes_dataset.jsonl \
    --agent-cls CodeActAgent \
    --llm-config <llm-config-name> \
    --max-iterations <max-iterations> \
    --eval-num-workers 4 \
    --eval-note run_evaluation_instances
```

It is recommended to set `<max-iterations> ≈ 200` and `<max_output_tokens> ≤ 32000`. The
run writes `output.jsonl` under the eval output directory.

### Convert

```bash
python convert.py --input_dir <run dir> [--output_dir <out dir>]
```

See [`../TRAJECTORY_FORMAT.md`](../TRAJECTORY_FORMAT.md) for the record format. Because
trajectories are large, the converter writes the split layout: an index `<DIR>.trials.json`
whose `messages` field is a path, plus one `messages/<id>.json` per instance.

## Requirements

- OpenHands 0.54 installed with credentials configured
