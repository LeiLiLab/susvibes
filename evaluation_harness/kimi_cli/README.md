# Kimi CLI Code Evaluation Harness

This directory contains tools for running Kimi CLI evaluations on SusVibes benchmark tasks using Docker containers.

## Overview

The evaluation harness executes Kimi CLI on benchmark code repositories packaged as Docker images, providing an isolated, reproducible testing environment.

## Files

- **`prompts.py`** - Prompt templates and example task definitions for Kimi CLI interactions
- **`run_docker.py`** - Core Docker integration class (`DockerIntegration`) for managing containerized Kimi CLI execution
- **`batch_run_docker.py`** - Processes multiple evaluation instances sequentially from a JSONL dataset
- **`parallel_batch_run.py`** - Runs batch evaluations in parallel across multiple processes
- **`setup-env.sh`** - Container setup script installing Kimi CLI in an isolated virtual environment via `uv`
- **`convert.py`** - Converts harness `final_results.json` into standard OpenAI message trajectory format
- **`.env.sample`** - Sample environment variables for Kimi CLI / Moonshot API

## Usage

### Single Instance

Run a single evaluation using `run_docker.py`:

```bash
python run_docker.py
```

### Batch Processing

Process multiple instances sequentially from a JSONL file:

```bash
python batch_run_docker.py --jsonl_file ../../datasets/default/susvibes_dataset.jsonl --num_instances 10
```

### Parallel Batch Processing

Run batch evaluations across N parallel processes:

```bash
python parallel_batch_run.py --jsonl_file ../../datasets/default/susvibes_dataset.jsonl --num_processes 4
```

### Trajectory Conversion

A batch run writes `final_results.json` under `results/<model>/<timestamp>/`. Convert it into the standard trajectory format with:

```bash
python convert.py --input_dir results/kimi-k2-turbo-preview/<timestamp>/
```

See [`../TRAJECTORY_FORMAT.md`](../TRAJECTORY_FORMAT.md) for the record specification. The converter writes the split layout: an index `<stem>.trials.json` referencing individual instance messages in `messages/<instance_id>.json`.

## Environment Variables

Copy `.env.sample` to `.env` or export credentials in your shell:

```bash
export MOONSHOT_API_KEY="your-api-key"
export KIMI_MODEL="kimi-k2-turbo-preview"
```
