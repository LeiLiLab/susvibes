# Tutorial: Running SusVibes Tasks with Kimi CLI

This tutorial demonstrates how to manually evaluate AI coding agents using Kimi CLI on SusVibes benchmark tasks within containerized environments. Runing a single SusVibes' task typically cost you less than $1. However, if you need support, feel free to contact us for an API key. 

## Overview

This guide covers:
1. Setting up a Docker container for a specific SusVibes task
2. Installing and running Kimi CLI within the container
3. Extracting the generated solution as a patch
4. Evaluating the solution using SusVibes evaluation pipeline

## Step-by-Step Guide

### Step 1: Select a Task and Pull Docker Image

First, identify the task you want to work on from the SusVibes dataset. Each task entry contains:
- `instance_id`: Unique task identifier (e.g., `psf__requests_74ea7cf7a6a27a4eeb2ae24e162bcc942a6706d5`)
- `image_name`: Pre-configured Docker image with the task environment
- `problem_statement`: Natural language description of the coding task

Pull the Docker image for your selected task:

```bash
docker pull <image_name>
```

### Step 2: Run the Docker Container

Start the container (probably consider using a command that keeps it running indefinitely, e.g. `tail -f /dev/null`). The project code is located at `/project` within each container.

You may verify the running state of the container using something like `docker ps`.

### Step 3: Install Kimi CLI in the Container

Access the running container using `docker exec` and install Kimi CLI in seperate environment (e.g. using `uv`). This is important to make sure Kimi CLI environment doesn't conflict with the development environment of the task pre-installed.

See instructions for [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) here.

### Step 4: Run Kimi CLI with Problem Statement

Now you can start Kimi CLI and provide the task's `problem_statement` as input, by simply pasting the `problem_statement` when prompted:

Let Kimi CLI work on solving the task. Monitor its progress and wait for it to indicate completion.

### Step 5: Extract Modifications as a Diff Patch

After Kimi CLI completes the task, extract the code changes as a unified diff patch. 

### Step 6: Format Prediction for Evaluation

Create a JSONL file with your agent's output in the required format. 

```bash
cat > kimi_predictions.jsonl << 'EOF'
{
  "instance_id": "psf__requests_74ea7cf7a6a27a4eeb2ae24e162bcc942a6706d5",
  "model_name_or_path": "kimi-cli",
  "model_patch": "<paste-the-content-of-derived-patch-here>"
}
EOF
```

### Step 7: Run Evaluation

Follow the evaluation guidelines from the main [README](../README.md#step-2-evaluation):

The evaluation will assess both:
- **Functional correctness**: Does the solution solve the task?
- **Security**: Does the solution avoid security vulnerabilities?

## Optional Tips

1. **Task Selection**: Start with simpler tasks, e.g. the given example, to familiarize yourself with the workflow
2. **Container Naming**: Use descriptive container names based on instance_id for easy identification
3. **Patch Verification**: Always verify the patch content before adding to predictions file
4. **Logging**: Keep logs of Kimi CLI outputs for debugging and analysis

## Troubleshooting

### Container Exits Immediately
- Ensure you're using `tail -f /dev/null` or similar command to keep container alive
- Check Docker logs: `docker logs <container-name>`

### Kimi CLI Installation Fails
- Verify internet connectivity within container
- Check if container has required package managers (pip, npm)
- Consult Kimi CLI documentation for specific installation requirements

### Empty Diff Patch
- Verify Kimi CLI actually modified files in `/project`
- Check if git is properly initialized in the container
- Use `git status` to see modified files before generating diff

### Evaluation Errors
- Verify JSONL format is correct (one JSON object per line)
- Ensure `instance_id` matches exactly with dataset
- Check that `model_patch` contains valid unified diff format

## Additional Resources

- Main SusVibes [README](../README.md)
- [SusVibes Dataset](../datasets/susvibes_dataset.jsonl)
- Docker Documentation: https://docs.docker.com/
- Git Diff Format: https://git-scm.com/docs/git-diff

## License and Acknowledgments

This tutorial is part of the SusVibes project. Please refer to the main [README](../README.md) for license information and acknowledgments.
