# Gemini CLI Code Evaluation Harness

This directory contains tools for running Gemini CLI Code evaluations on code repositories using Docker containers.

## Overview

The evaluation harness executes Gemini CLI Code on code repositories packaged as Docker images, allowing for isolated and reproducible testing environments.

## Files

- **`prompts.py`** - Contains prompt templates and example task definitions for Gemini CLI Code interactions
- **`run_docker.py`** - Core Docker integration class (`DockerIntegration`) for managing containerized Gemini CLI Code execution
- **`batch_run_docker.py`** - Processes multiple evaluation instances from a JSONL file sequentially
- **`parallel_batch_run.py`** - Runs batch evaluations in parallel across multiple processes for faster processing
- **`setup-env.sh`** - Setup script that installs Claude CLI and dependencies in Docker containers
- **`convert.py`** - Converts the harness `final_results.json` into the standard format (OpenAI messages)

## Usage

### Single Instance

Run a single evaluation using `run_docker.py`:

```bash
python run_docker.py
```

### Batch Processing

Process multiple instances from a JSONL file:

```bash
python batch_run_docker.py --jsonl_file dataset.jsonl --num_instances 10
```

### Parallel Batch Processing

Run batch evaluations in parallel:

```bash
python parallel_batch_run.py --jsonl_file dataset.jsonl --num_processes 4
```

### Convert

A batch run writes `final_results.json` under `results/<model>/<timestamp>/`; turn it into the standard format with:

```bash
python convert.py --input_dir <run dir> [--output_dir <out dir>]
```

See [`../TRAJECTORY_FORMAT.md`](../TRAJECTORY_FORMAT.md) for the record format. Because trajectories are large, the converter writes the split layout: an index `<DIR>.trials.json` whose `messages` field is a path, plus one `messages/<id>.json` per instance.

## Requirements

- Docker installed and running
- Python 3.x
- Gemini CLI API credentials (set via environment variables: `GEMINI_API_KEY`, `GEMINI_MODEL`, etc.)

## Environment Variables

Please copy a example environment file to `.env` and edit it:
```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys and model configurations:
- `GEMINI_API_KEY` - Your Gemini CLI API key
- `GEMINI_MODEL` - Model to use (default: "gemini-3-pro-preview")



