# SWE-agent Evaluation Harness

This directory contains tools for running SWE-agent evaluations on a SusVibes dataset and
converting the resulting trajectories into the standard format.

## Overview

The harness drives [SWE-agent](https://github.com/SWE-agent/SWE-agent) over a dataset to
produce predictions, then converts its trajectory output into the standard OpenAI-messages
format. The dataset's `problem_statement` is used as-is.

## Files

- **`run_infer.py`** - Runs SWE-agent over a dataset and writes `predictions.json`.
- **`setting.yaml`** - Run configuration (SWE-agent config(s), conda env, workers, model limits).
- **`expert_instances.yaml`** - Example of the SWE-agent instances file `run_infer.py` builds.
- **`convert.py`** - Converts SWE-agent `.traj` output into the standard format (OpenAI messages).

## Usage

### Installation

Install [SWE-agent](https://github.com/SWE-agent/SWE-agent) with credentials configured,
then set the SWE-agent config(s), conda env, and workers in `setting.yaml`.

### Run

```bash
python run_infer.py --dataset_path <dataset.jsonl> --model <litellm-model>
```

This builds the SWE-agent batch, runs it, and writes `predictions.json` under the output
directory.

### Convert

```bash
python convert.py --input_dir <run dir> [--output_dir <out dir>]
```

See [`../TRAJECTORY_FORMAT.md`](../TRAJECTORY_FORMAT.md) for the record format. Because
trajectories are large, the converter writes the split layout: an index `<DIR>.trials.json`
whose `messages` field is a path, plus one `messages/<id>.json` per instance.

## Requirements

- SusVibes installed
- SWE-agent installed with credentials configured
