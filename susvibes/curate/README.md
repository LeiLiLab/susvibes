# SusVibes Task Curation

This directory contains code for the curation pipeline of SusVibes, including vulnerability collection, adaptive generation of task candidates, building environments, and conducting execution-based task validation. Before proceeding, ensure the repository is correctly installed according to the guidelines in the main [README](../../README.md).

The pipeline runs in four sequential stages:

1. [`collect/`](collect/) — collect vulnerability records from existing datasets
2. [`adaptive_gen/`](adaptive_gen/) — adaptively generate task candidates (masks + problem statements)
3. [`env_setup/`](env_setup/) — build per-instance Docker environments
4. [`validate/`](validate/) — validate tasks via execution and finalize the dataset

## Collecting Vulnerability Records

First, retrieve data on historically observed software vulnerabilities from existing datasets. We provide the vulnerability dataset from ReposVul as an example; download it from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it at `datasets/cve_records/ReposVul/`. Then run:

```bash
python -m susvibes.curate.collect.process \
    --max_records 3 \
    --use_handlers '["ReposVulHandler"]' \
    --run_id playground
```

This produces an assembled dataset of vulnerability fixing commits, `processed_dataset.jsonl`, under `datasets/<run_id>/`.

## Generating Task Candidates

From these vulnerability fixing commits, an adaptive pipeline creates a SusVibes task for each. The pipeline includes:

1. **An agent to generate an initial mask.** This mask is generated on the vulnerable commit before the security fix, i.e., masking out a software feature from its vulnerable implementation.
2. **A task description is generated** to describe the functionality of this masked implementation.
3. **A verifier agent** checks whether the task description covers all lines in feature implementation with *security fixes*. If verification fails, go back to step 1; otherwise, proceed to step 4.
4. Return the task description and the mask.

These stages are implemented in [`adaptive_gen/pipeline.py`](adaptive_gen/pipeline.py) and leverage [SWE-agent (sv)](https://github.com/songwen6968/SWE-agent/tree/sv).

Follow the installation [guidelines](https://swe-agent.com/latest/installation/source/) to install SWE-agent from source (a `conda` environment is recommended). Set up SWE-agent with the config file at [`utils/agents/configs/adaptive_gen.yaml`](utils/agents/configs/adaptive_gen.yaml) by placing it under the `config/` directory of SWE-agent. You may configure the SWE-agent setup itself in [`utils/agents/settings.yaml`](utils/agents/settings.yaml); a pre-filled example is provided.

With that, run:

```bash
python -m susvibes.curate.adaptive_gen.pipeline \
  --max_iters <num_adaptive_iterations> \
  --preview 2 \
  --run_id playground
```

The resulting tasks are saved at `datasets/<run_id>/task_dataset.jsonl` and previewed at `datasets/<run_id>/examples/`.

## Building Execution Environment

Next, build a Docker image for each task capable of executing the test suite of the associated repository. We leverage an [environment building agent](https://github.com/songwen6968/SWE-agent/tree/sv-env-setup) for automation.

The image building process has two phases: (i) identifying basic developer tools required (e.g., Python versions) and creating a base image equipped with these tools; and (ii) installing the repository and running the test suite within the base image.

### Step 1: Preparing Base Image with Developer Tools

An agent identifies the Python version each project requires, using the SWE-agent set up in the previous section. The following command prepares the agent run, runs the agent, and post-processes the output into `env_specs/<run_id>/dev_tools.json` (see [`env_specs/default/dev_tools.json`](../env_specs/default/dev_tools.json) for an example):

```bash
python -m susvibes.curate.env_setup.dev_tools \
  --run_id playground
```

The `build_repo --prologue` command in Step 2 will automatically pull the canonical `base_py` and `dind_py` images from [Docker Hub](https://hub.docker.com/r/songwen6968) for every Python version needed and tag `base_py:<version>` locally; no manual pull is required.

### Step 2: Installing Repo and Executing Test Suite

This step uses a specialized environment building agent, [SWE-agent (sv-env-setup)](https://github.com/songwen6968/SWE-agent/tree/sv-env-setup), whose config file is located at [`utils/agents/configs/env_setup.yaml`](utils/agents/configs/env_setup.yaml). Configure it similarly via [`utils/agents/settings.yaml`](utils/agents/settings.yaml).

In short, SWE-agent (sv-env-setup) starts inside the corresponding base image and consults (in order) the pre-existing container configurations, CI/CD pipeline, and other documentation for reproducing the testing workflow. It then invokes Docker commands to create a new image with successful installation and testing steps baked in.

First, prepare the agent run:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --prologue \
  --run_id playground
```

Then run the environment building agent as specified in [`utils/agents/runs.sh`](utils/agents/runs.sh).

> **Note:** *This step can be resource-consuming in both time and space, as the agent repeatedly installs dependencies, tests the environment, and builds Docker images. We recommend at least 2GB of free storage per instance and adjusting parallelism based on available CPU cores.*

After the agent finishes, build the environment Docker images from its output:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --epilogue \
  --agent_output_dir <path_to_agent_output> \
  --max_workers 5 \
  --run_id playground \
  [--force]                # Optional: force re-build
  [--from_existing_specs]  # Optional: reuse the cached dockerfile in env_specs instead of re-extracting it from agent output
```

## Validating Test Cases via Execution

Finally, run validation: this synthesizes test suite output parsers and verifies each collected task instance against its execution environment.

```bash
python -m susvibes.curate.validate.with_test \
  --max_workers 5 \
  --run_id playground \
  [--force]                # Optional: force re-validation
  [--from_existing_specs]  # Optional: reuse the cached logs_parser in env_specs instead of re-synthesizing it via LLM
```

This step requires an LLM API setup for generating test suite output parsers. Configure which LLM to use in [`constants.py`](constants.py), and set the API key in your `.env` file. Set your Docker Hub namespace under which produced images are tagged in [`susvibes/constants.py`](../constants.py).

Then finalize the dataset and publish it to Hugging Face:

```bash
python -m susvibes.curate.validate.wrapup \
  --run_id playground
```

This filters to validated instances, computes each golden patch, strips records to the released
schema, and uploads `susvibes_dataset.jsonl` to the Hugging Face dataset repo configured by
`HF_DATASET_REPO` in [`susvibes/constants.py`](../constants.py) — it does **not** write the local
dataset. Set `HF_TOKEN` (with write access to that repo) in your `.env` first.
