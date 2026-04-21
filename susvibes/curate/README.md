# SusVibes Task Curation Pipeline

This directory contains code for the curation pipeline of SusVibes, including adaptive generation of task candidates, building environments, and conducting execution-based task validation. Before proceeding, ensure the repository is correctly installed according to the guidelines in the main [README](../../README.md).

## Creating Task Candidates

First, you need to retrieve data on historically observed software vulnerabilities from existing datasets. Download the vulnerability dataset from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it under `datasets/cve_records/` with the exact directory name `ReposVul`. Then run the following command:

```bash
python -m susvibes.curate.collect.process \
    --max_records 3 \
    --use_handlers '["ReposVulHandler"]' \
    --run_id playground
```

This will produce an assembled dataset of vulnerability fixing commits, named `processed_dataset.jsonl`, under `datasets/`.

From these vulnerability fixing commits, you will next go through an adaptive pipeline that creates a SusVibes task from each processed fixing commit. The pipeline includes the following stages:

1. **An agent to generate an initial mask.** This mask is generated on the vulnerable commit before the security fix, i.e., masking out a software feature from its vulnerable implementation. 
2. **A task description is generated** to describe the functionality of this masked implementation.
3. **A verifier agent** is used to check whether the task description covers all lines in feature implementation with *security fixes*. If the verification fails, go back to step 1 for regeneration; otherwise, go to step 4.
4. Return the task description and the mask.

These stages are conveniently implemented for you in `pipeline.py`. The process leverages [SWE-agent](https://github.com/SWE-agent/SWE-agent).

First, find the installation [guidelines](https://swe-agent.com/latest/installation/source/) and install SWE-agent from source. It is recommended that SWE-agent is installed within a `conda` environment. 

In this section, it is recommended that you set up SWE-agent with the config file at `agents/configs/susvibes_curate.yaml` by putting it under the `config/` directory of SWE-agent. You may specify everything about the SWE-agent setup in [settings](agents/settings.yaml). A pre-filled example is provided.

With that, run the following command to adaptively construct and verify the tasks:

```bash
python -m susvibes.curate.adaptive_gen.pipeline \
  --max_iters <num_adaptive_iterations> \
  --preview 2 \
  --run_id playground 
```

You can then find several tasks created at `task_dataset.jsonl` in `datasets/`, and displayed at `task_examples/`. You may use these tasks to examine quality, or better interpret the curation method.

> **Note:** *Running this curation pipeline will typically cost you less than $1 per task. We used Claude 4 Sonnet, however you may employ any agentic LM with decent performance.*

## Building Execution Environment

Next, you will build a Docker image for each task that is capable of executing the test suite of the associated repository. We leverage an [environment building agent](https://github.com/songwen6968/Env-agent) for automation.

The image building process has two phases: (i) identifying the basic developer tools required (e.g., Python versions) and creating a base image equipped with these tools; and (ii) installing the repository and running the test suite within the base image.

### Step 1: Preparing Base Image with Developer Tools

In this step, an agent is utilized to automatically identify the Python version each project requires. The agent can be set up in the exact same way as above. The following command prepares the agent run, runs the agent, and post-processes its output to produce the identified dev tools for all task candidates, stored in [dev_tools](../env_specs/dev_tools.json):

```bash
python -m susvibes.curate.env_setup.dev_tools \
  --run_id playground
```

After identifying dev tools, you need to prepare a set of base images with the required dev tools installed. You may pull and tag `base_py` and `dind_py` from [dockerhub](https://hub.docker.com/r/songwen6968).

### Step 2: Installing Repo and Executing Test Suite

In this section, you will be utilizing a specialized environment building agent, [Env-agent](https://github.com/songwen6968/Env-agent). Its config file is located at `agents/configs/susvibes_env_setup.yaml`, and can be configured in a similar way in [settings](agents/settings.yaml).

In short, Env-agent is started inside the corresponding base image, and is instructed to consult, in order: the pre-existing container configurations, CI/CD pipeline, and other documentation for reproducing the testing workflow. It then invokes Docker commands to create a new image with successful installation and testing steps baked in.

First, prepare the agent run:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --prologue \
  --run_id playground
```

Then run the environment building agent as specified in [runs](agents/runs.sh). This step can be resource-consuming in both time and space, as the agent repeatedly installs dependencies, tests the environment, and builds Docker images.

After the agent has finished, build the environment Docker images from the agent output:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --epilogue \
  --agent_output_dir <path_to_agent_output> \
  --max_workers 5 \
  --run_id playground \
  [--force]  # Optional: force re-build
```

## Validating Test Cases via Execution

Next, run validation: this synthesizes test suite output parsers and verifies each collected task instance against its execution environment.

```bash
python -m susvibes.curate.validate.with_test \
  --max_workers 5 \
  --run_id playground \
  [--force]  # Optional: force re-validation
```

This step requires an LLM API setup for generating test suite output parsers. Configure which LLM to use in [constants](constants.py), and set the API key in your `.env` file. Set your desired repository name of produced docker images also in [constants](constants.py).

Finally, produce the SusVibes dataset as `susvibes_dataset.jsonl`:

```bash
python -m susvibes.curate.validate.wrapup \
  --run_id playground
```
