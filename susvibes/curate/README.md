# SusVibes Tasks Curation Pipeline

This directory contains code for the curation pipeline of SusVibes, including adaptive generation of task candidates, building environments, and conducting execution-based task validation. Before proceeding, ensure the repository is correctly installed according to the guidelines in the main [README](../README.md), and that you are in the `src/` directory.

## Task Candidates Generation

First, you need to retrieve data on historically observed software vulnerabilities from existing datasets. Download the vulnerability dataset from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it under `datasets/cve_records/ReposVul` with the exact name. Then run the following command:

```bash
python -m curate.collect.process \
    --debug \
    --max_records=3 \
    --use_handlers='["ReposVulHandler"]'
```

This will produce an organized dataset of vulnerability-fixing commits under `datasets/`, named `processed_dataset_debug.jsonl`. You should be able to see it after running the above command.

From these processed vulnerability fixing commits, you will next go through an adaptive pipeline for creating tasks in SusVibes, which includes the following stages:

1. **An agent to generate an initial mask.** This mask is generated on the vulnerable commit before the security fix, i.e., masking out a feature from its vulnerable implementation. 
2. **A task description is generated** to describe the functionality of this masked implementation.
3. **A verifier agent** is used to check whether the task description covers all lines in feature implementation with *security fixes*. If the verification fails go back to step 1 for regeneratation; otherwise, go to step 4.
4. Return the task description and the mask.

These stages are conveniently implemented for you in `pipeline.py`. 

The process leverages SWE-agent, which will need to be installed and configured as detailed below. First, find the installation [guidelines](https://swe-agent.com/latest/installation/source/) and install SWE-agent from source. It is recommended that SWE-agent is installed within a `conda` environment. SWE-agent requires a configuration file; you may use [config](agents/config.yaml) and put it under the `config/` directory of SWE-agent. After you've set up the agent, you may specify everything in [settings](agents/settings.yaml), for example, where SWE-agent is installed, what the configuration file is named, etc. A default version is provided for you.

With that, you're ready to run:

```bash
python -m curate.pipeline \
  --debug \
  --max_iters <num_adaptive_iterations> \ 
  --display_tasks \
  --config=<name-of-config-file> --model='{"name": <name-of-llm-to-use>}' --conda_env=<name-of-conda-environment>
```

You should then be able to find several tasks created at `task_dataset_debug.jsonl` in `datasets/`, and displayed at `datasets/task_examples_debug`. These tasks can help you better understand the curation method described in detail in the manuscript.

Running this curation pipeline will typically cost you less than $1. However, if you need support, feel free to contact us for an API key. It is recommended that you do not use a model that is too weak.
