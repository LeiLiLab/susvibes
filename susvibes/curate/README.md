# SusVibes Tasks Curation Pipeline

This directory contains code for the curation pipeline of SusVibes, including adaptive generation of task candidates, building environments, and conducting execution-based task validation. Before proceeding, ensure the repository is correctly installed according to the guidelines in the main [README](../../README.md).

## Task Candidates Generation

First, you need to retrieve data on historically observed software vulnerabilities from existing datasets. Download the vulnerability dataset from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it under `datasets/cve_records/ReposVul` with the exact name. Then run the following command:

```bash
python -m susvibes.curate.collect.process \
    --max_records 3 \
    --use_handlers '["ReposVulHandler"]' \
    --subset playground # outputs to subfolder to avoid possible mixing
```

This will produce an assembled dataset of vulnerability fixing commits `processed_dataset.jsonl` under `datasets/`. 

From these vulnerability fixing commits, you will next go through an adaptive pipeline that create a SusVibes task from each processed fixing commit. The pipeline includes the following stages:

1. **An agent to generate an initial mask.** This mask is generated on the vulnerable commit before the security fix, i.e., masking out a software feature from its vulnerable implementation. 
2. **A task description is generated** to describe the functionality of this masked implementation.
3. **A verifier agent** is used to check whether the task description covers all lines in feature implementation with *security fixes*. If the verification fails go back to step 1 for regeneratation; otherwise, go to step 4.
4. Return the task description and the mask.

These stages are conveniently implemented for you in `pipeline.py`. The process leverages [SWE-agent](https://github.com/SWE-agent/SWE-agent), which will need to be installed and configured as detailed below.

First, find the installation [guidelines](https://swe-agent.com/latest/installation/source/) and install SWE-agent from source. It is recommended that SWE-agent is installed within a `conda` environment. SWE-agent requires a configuration file; you may use [config](agents/config.yaml) and put it under the `config/` directory of SWE-agent. After you've set up the agent, you may specify everything in [settings](agents/settings.yaml), for example, where SWE-agent is installed, what the configuration file is named, etc. A pre-filled example is provided for you.

With that, you're ready to run:

```bash
python -m susvibes.curate.adaptive_gen.pipeline \
  --max_iters <num_adaptive_iterations> \
  --preview 2 \
  --subset playground 
```

You can then find several tasks created at `task_dataset.jsonl` in `datasets/`, and displayed at `task_examples/`. You may use these tasks to examine quality, or better interpret the curation method.

> **Note:** *Running this curation pipeline will typically cost you less than $1 per task. We used Claude 4 Sonnet, however you may employ any agentic LM with decent performance.*

## Execution Environment Setup

🚧 *Under construction*
