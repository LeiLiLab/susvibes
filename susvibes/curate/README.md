# SusVibes Curation

This directory contains the SusVibes task-curation pipeline: mining vulnerability records, identifying each repo's developer tools, checking test coverage, adaptively generating task candidates, building per-instance Docker environments, and execution-based validation. Before proceeding, ensure the repository is installed per the main [README](../../README.md).

Before the first run, point `LOCAL_REPOS_DIR` ([`constants.py`](constants.py)) at a disk with room to spare: every stage from mining onward works in one shared clone per project, kept for the life of the run.

The pipeline runs in this order (all artifacts land under `datasets/<run_id>/` and `env_specs/<run_id>/`):

1. [`mine/core`](mine/) — mine vulnerability-fixing commits → `dataset.jsonl`
2. [`env_setup/dev_tools`](env_setup/) — identify each repo's Python version → `dev_tools.json`
3. [`env_setup/build_base`](env_setup/) `--mode pull` — pull the `base_py` / `dind_py` / `static_py` images for those versions
4. [`mine/post/test_mask`](mine/post/test_mask/) — hide each instance's test bodies → `test_patch`
5. [`mine/post/sec_prop`](mine/post/sec_prop.py) *(optional)* — state each CVE's security property → `sec_prop`
6. [`mine/post/check_cov`](mine/post/check_cov/) *(optional)* — label each instance's test coverage → `func_coverage`
7. [`adaptive_gen`](adaptive_gen/) — generate masks + problem statements → `task_patch`
8. [`env_setup/build_repo`](env_setup/) — build a per-instance environment image → `env_image_name`
9. [`validate`](validate/) — validate via execution, then publish the dataset

Every stage records its verdict under a record's `keep`, and a stage admits only the records that
passed *every* verdict recorded so far. That AND is why **the order within steps 4–6 is a
requirement, not a preference**: `test_mask` is mandatory, so it runs before the optional filters —
were one of them to run first, its rejections would withhold from the mandatory stage records it
still owes a result for. Skipping an optional step records no verdict, and the gate ignores it.

Several stages drive [SWE-agent (sv)](https://github.com/songwen6968/SWE-agent/tree/sv). Install it from source per its [guidelines](https://swe-agent.com/latest/installation/source/) (a `conda` env is recommended); for each agent stage, place its config (named below) under SWE-agent's `config/` directory and configure its setting under [`utils/agents/settings/`](utils/agents/settings/) (pre-filled examples are provided). Run the agent batches as specified in [`utils/agents/runs.sh`](utils/agents/runs.sh).

## 1. Collecting Vulnerability Records

Retrieve data on historically observed software vulnerabilities from existing datasets. We provide the ReposVul dataset as an example; download it from [Google Drive](https://drive.google.com/file/d/1vk_WAPW3DvRsRKT7mfb4lpZWtVEGED0M/view?usp=share_link) and place it at `datasets/raw_cve/ReposVul/`. Then run:

```bash
python -m susvibes.curate.mine.core \
    --max_records 3 \
    --use_handlers '["ReposVulHandler"]' \
    --run_id playground
```

This produces `dataset.jsonl` — an assembled dataset of vulnerability-fixing commits.

## 2. Identifying Developer Tools

An agent identifies the Python version each project requires and maps it to an available base-image version, producing `env_specs/<run_id>/dev_tools.json` (see [`env_specs/default/dev_tools.json`](../env_specs/default/dev_tools.json) for an example). It reads `dataset.jsonl` (SWE-agent config: [`utils/agents/configs/curate.yaml`](utils/agents/configs/curate.yaml)):

```bash
python -m susvibes.curate.env_setup.dev_tools \
  --run_id playground
```

## 3. Pulling Base Images

Pull the per-version `base_py`, `dind_py`, and `static_py` images from [Docker Hub](https://hub.docker.com/r/songwen6968) for the Python versions `dev_tools` identified — `check_cov` and `build_repo` expect them present locally:

```bash
python -m susvibes.curate.env_setup.build_base \
  --mode pull \
  --image_names '["base_py", "dind_py", "static_py"]' \
  --versions '["3.10", "3.11"]'
```

## 4. Deriving Test Masks

The vulnerability-fixing commit's own tests are what a task is graded against, so they must not be
readable from the repo the task ships. For each instance, roll the test files back to `base_commit`
and empty every test function body, inside a version-matched `static_py` container so the parser
matches the instance's own Python syntax. The resulting diff lands in `test_patch`, the one name
every later stage reads; mining's own diff stays put under `raw_test_patch`. See
[`mine/post/test_mask/`](mine/post/test_mask/) for details.

```bash
python -m susvibes.curate.mine.post.test_mask \
  --run_id playground \
  --max_workers 5
```

## 5. Extracting Security Properties *(optional)*

Run this if anything downstream needs to reason about *what* each CVE broke. An agent reads the
`security_patch` and states the security property it restores, annotating the record with
`sec_prop`; an instance whose patch carries no coherent property is dropped.

```bash
python -m susvibes.curate.mine.post.sec_prop \
  --run_id playground \
  --max_workers 5
```

## 6. Checking Test Coverage *(optional)*

An early stop: a rough static check for whether the repo's own suite exercises the vulnerable code,
applied before the expensive agent stages. Running it lowers the final instance count in exchange
for the downstream agent cost those instances would have spent. For each instance, statically decide
(no execution) whether any test reaches the files touched by the security fix, inside a
version-matched `static_py` container. Each record is annotated with a
`func_coverage` object labeled `likely_covered` / `maybe_covered` / `unlikely_covered` /
`indeterminate`, and only the first two are kept. See [`mine/post/check_cov/`](mine/post/check_cov/)
for details.

```bash
python -m susvibes.curate.mine.post.check_cov \
  --run_id playground \
  --max_workers 5
```

## 7. Generating Task Candidates

From the covered vulnerability-fixing commits, an adaptive pipeline creates a SusVibes task for each:

1. **An agent generates an initial mask** on the vulnerable commit (before the security fix), masking out a software feature from its vulnerable implementation.
2. **A task description is generated** for the masked implementation.
3. **A verifier agent** checks whether the description covers all lines of the security-fixed feature implementation; if not, it retries the mask.

These stages are implemented in [`adaptive_gen/core.py`](adaptive_gen/core.py) (SWE-agent config: [`utils/agents/configs/curate.yaml`](utils/agents/configs/curate.yaml)). They run on the records still kept by every preceding stage, annotating each in place (previewed at `datasets/<run_id>/examples/`):

```bash
python -m susvibes.curate.adaptive_gen.core \
  --max_iters <num_adaptive_iterations> \
  --preview 2 \
  --run_id playground
```

## 8. Building Execution Environments

Build a Docker image for each task capable of executing the test suite of its repository, using the environment-building agent [SWE-agent (sv-env-setup)](https://github.com/songwen6968/SWE-agent/tree/sv-env-setup) (config: [`utils/agents/configs/env_setup.yaml`](utils/agents/configs/env_setup.yaml)). The agent starts inside the corresponding base image and consults the pre-existing container configuration, CI/CD pipeline, and other docs to reproduce the testing workflow, then bakes the working install + test steps into a new image.

First, prepare the agent run:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --prologue \
  --run_id playground
```

Then run the environment-building agent as specified in [`utils/agents/runs.sh`](utils/agents/runs.sh).

> **Note:** *This step can be resource-consuming in time and space, as the agent repeatedly installs dependencies, tests the environment, and builds Docker images. We recommend at least 2GB of free storage per instance and adjusting parallelism to available CPU cores.*

After the agent finishes, build the environment images from its output, tagging each record with its `env_image_name`:

```bash
python -m susvibes.curate.env_setup.build_repo \
  --epilogue \
  --max_workers 5 \
  --run_id playground \
  [--from_existing_dockerfiles]  # Optional: reuse the cached dockerfile in env_specs instead of re-extracting it from agent output
```

## 9. Validating and Publishing

Validation synthesizes test-suite output parsers and verifies each task instance against its environment (expected security + functional test breaks), tagging an evaluation image per validated instance:

```bash
python -m susvibes.curate.validate.with_test \
  --max_workers 5 \
  --run_id playground \
  [--force]                # Optional: force re-validation
  [--from_existing_specs]  # Optional: reuse the cached logs_parser in env_specs instead of re-synthesizing it via LLM
```

This requires an LLM API for generating test-suite output parsers: configure the model in [`constants.py`](constants.py), set the API key in your `.env`, and set your Docker Hub namespace in [`susvibes/core/constants.py`](../core/constants.py).

If you want to publish the dataset, finalize it and publish to it Hugging Face:

```bash
python -m susvibes.curate.wrap_up \
  --run_id playground
```

This filters to validated instances, computes each golden patch, strips records to the released schema, and writes the result to `datasets/<run_id>/susvibes_dataset.jsonl` — the final dataset the evaluation harness consumes. Pass `--push_to_hub` to also upload it to the Hugging Face dataset repo set by `HF_DATASET_REPO` in [`susvibes/core/constants.py`](../core/constants.py) (set `HF_TOKEN` with write access in your `.env` first).

## Two Curation Pipelines

The nine steps above are the **main pipeline**: each task's security `test_patch` is mined directly from the vulnerability-fixing commit. A **second pipeline** handles commits that ship *no* test changes — there's no potential tests that come with the security-fix commit, so a SWE-agent synthesizes the security tests from the security fix, while functional regression still uses the repo's own suite.

The second pipeline reuses the same stages, differing only in three points:

- Steps 1, 7, 8 (`mine.core`, `adaptive_gen.core`, `build_repo --prologue`) take `--require_test false` — keeping only the test-less records; steps 2–6 are unchanged (step 4 concludes `no_test_patch` for a record with nothing to hide).
- A test-synthesis stage is inserted before validation: `test.gen` builds rollback images and runs a SWE-agent to author the security tests.
- Step 9 validation uses `validate.gen_test` instead of `validate.with_test`; `wrap_up` is shared.

See [`test`](test/) for the full second-pipeline walkthrough (it also covers manually editing an existing `test_patch`).
