# SWE-agent Evaluation Harness

This directory contains instructions for running SWE-agent evaluations on code repositories using Docker containers.

## Overview

The evaluation harness leverages SWE-agent's interface, powered by SWE-rex for convenience. Single-instance runs can be handled directly by SWE-agent, while batch processing utilizes SWE-agent's `Expert instances` feature and is seamlessly integrated with SusVibes.

## Usage

### Single Instance

```bash
# In the SWE-agent repo
sweagent run \
    --agent.model.name <model_name> \
    --agent.model.max_output_tokens <max_output_tokens> \
    --agent.model.per_instance_cost_limit <cost_limit> \
    --agent.model.per_instance_call_limit <call_limit> \
    --problem_statement.text <problem_statement> \
    --env.repo.type preexisting \
    --env.repo.repo_name project \
    --env.deployment.image <image_name> \
    --env.deployment.python_standalone_dir /root
```

### Parallel Batch Processing

In SusVibes, prepare batch instances for SWE-agent with the following command:

```bash
python susvibes.run_evaluation --prologue
```

You will find the generated `run_evaluation_generic_instances.yaml` file under `logs/agent_runs/`.

> The above process automatically constructs the instances file required by SWE-agent for batch processing. Alternatively, you may construct it manually for greater flexibility. An example of how to populate the instances file is provided in `expert_instances.yaml`.

Feed these task instances to SWE-agent with the following command:

```bash
# In the SWE-agent repo
sweagent run-batch \
    --agent.model.name <model_name> \
    --agent.model.max_output_tokens <max_output_tokens> \
    --agent.model.per_instance_cost_limit <cost_limit> \
    --agent.model.per_instance_call_limit <call_limit> \
    --instances.type expert_file \
    --instances.path <path_to_instances_file> \
    --num_workers 4
```

To fully utilize SWE-agent's capacity for completing SusVibes tasks, it is recommended to set `<max_output_tokens> ≤ 32000` and `<call_limit> ≈ 200`.

## Requirements

- SusVibes installed
- SWE-agent installed with credentials configured
