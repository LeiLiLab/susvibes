# SusVibes Tasks Curation Pipeline

This directory contains code for the curation pipeline of SusVibes, including adaptive generation of task candidates, building environments, and conducting execution-based task validation. Before proceeding, ensure the repository is correctly installed according to the guidelines in the main [README](../README.md), and that you are in the `src/` directory.

## Task Candidates Generation

First, you need to retrieve data on historically observed software vulnerabilities from existing datasets. Download the vulnerability dataset from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it under `datasets/cve_records/ReposVul` with the exact name. Then run the following command:

```bash
python -m curate.collect.process --debug --max_records=3 --use_handlers='["ReposVulHandler"]'
```

This will produce an organized dataset of vulnerability-fixing commits under `datasets/`, named `processed_dataset_debug.jsonl`. You should be able to see it after running the above command.

From these processed vulnerability fixing commits, you will next go through the adaptive pipeline for creating a task in SusVibes, which includes three agent-powered stages:

1. Starting from a vulnerability fixing commit, mask a feature implementation surrounding the touched lines
2. Given the masked out feature implementation, write a task description specifying the re-implementation requirements for this feature
3. Verify the compatibility between the feature implementation and the task description line by line, and adaptively adjust the mask

These stages are conveniently implemented for you in `pipeline.py`. It leverages SWE-agent; find its installation guidelines [here](https://swe-agent.com/latest/installation/source/) and place it in parallel with the `susvibes-project/` directory. You may start the curation pipeline with the following command:

```bash
python -m curate.pipeline --debug --max_iters=1 --model='{"name": <name-of-llm-to-use>}' --display_tasks
```

You should then be able to find several tasks created at `task_dataset_debug.jsonl` in `datasets/`, and displayed at `datasets/task_examples_debug`. These tasks can help you better understand the curation method described in detail in the manuscript.

Running this curation pipeline will typically cost you less than $1. However, if you need support, feel free to contact us for an API key. It is recommended that you do not use a model that is too weak.
