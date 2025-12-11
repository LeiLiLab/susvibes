# Claude Code Evaluation Harness

This directory contains tools for running Claude Code evaluations on code repositories using Docker containers.

## Overview

The evaluation harness executes Claude Code on code repositories packaged as Docker images, allowing for isolated and reproducible testing environments.

## Files

- **`prompts.py`** - Contains prompt templates and example task definitions for Claude Code interactions
- **`run_docker.py`** - Core Docker integration class (`DockerIntegration`) for managing containerized Claude Code execution
- **`batch_run_docker.py`** - Processes multiple evaluation instances from a JSONL file sequentially
- **`parallel_batch_run.py`** - Runs batch evaluations in parallel across multiple processes for faster processing
- **`setup-env.sh`** - Setup script that installs Claude CLI and dependencies in Docker containers

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

## Requirements

- Docker installed and running
- Python 3.x
- Claude API credentials (set via environment variables: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, etc.)

## Environment Variables

Please copy a example environment file to `.env` and edit it:
```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys and model configurations:
- `ANTHROPIC_API_KEY` - Your Anthropic API key
- `ANTHROPIC_MODEL` - Model to use (default: "claude-sonnet-4-20250514")
- `ANTHROPIC_BASE_URL` - Optional custom base URL
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS` - Maximum output tokens



