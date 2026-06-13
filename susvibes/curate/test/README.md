# Security `test_patch` Editing and Synthesis

This module manages the security test patches (`test_patch` field of `susvibes_dataset.jsonl`). Two workflows are supported:

- **Editing** ([`edit.py`](edit.py)) — dump existing `test_patch` entries to disk for manual refinement, then sync the edits back into the dataset.
- **Synthesis** ([`gen_prologue.py`](gen_prologue.py)) — for instances where no usable test patch exist, drive a SWE-agent (sv) to author `test_patch` from the security fix.

Both workflows assume the per-instance environment images from [`env_setup/build_repo.py`](../env_setup/build_repo.py) already exist and the dataset is otherwise complete (`task_patch`, `security_patch`, `mask_patch`, `env_image_name`, etc.).

## Editing test_patches

This is for refining existing `test_patch` entries — e.g., loosening assertions tied to the golden implementation so any equally secure alternative would also pass.

### 1. Dump

Write per-instance editable folders to `datasets/<run_id>/edits/`:

```bash
python -m susvibes.curate.test.edit --mode dump --run_id <run_id> \
  --max_workers <N> \
  [--instance_ids '["<id_1>", ...]']  # Optional: only dump these
  [--require_test false]                 # Optional: skip building the base_no_test image
```

Each `datasets/<run_id>/edits/<instance_id>/` contains:

- `test_patch.md` — the patch to edit
- `feature_vuln.md` — vulnerable feature diff
- `security_fix.md` — security fix patch
- `problem_statement.md` — task description
- `README.md` — meta (project, CVE, CWE, verification image, test command)
- `test_patch_backups/` — backups of prior versions (populated on sync)

By default, dump also builds a per-instance `base_no_test` image (env image with the original `test_patch` reversed) and records its name in the dataset under `base_no_test_image_name`. The next validation step then uses this image so the *edited* `test_patch` is the only one applied. Pass `--require_test false` to skip this image build.

### 2. Edit `test_patch.md`

Open each `test_patch.md` and refine the diff as needed. The patch must remain a valid unified diff applying cleanly to the repo at `base_commit`. Regenerate it with `git diff` inside the container rather than hand-editing `@@` headers.

### 3. Sync

Pull edits back into the dataset:

```bash
python -m susvibes.curate.test.edit --mode sync --run_id <run_id> \
  [--instance_ids '["<id_1>", ...]']
```

Each updated record gets its prior `test_patch` snapshotted to `edits/<instance_id>/test_patch_backups/<n>.md`. Re-running sync is idempotent.

### 4. Validate

Run validation against the per-instance `base_no_test` image so the dataset's *edited* `test_patch` is what's exercised:

```bash
python -m susvibes.curate.validate.with_test \
  --run_id <run_id> \
  --from_base_no_test_image \
  --max_workers <N> \
  [--from_existing_specs] \
  [--force]
```

Inspect the resulting `summary.json` for per-instance pass/fail counts and failure reasons.

## Synthesizing test_patches

This is for instances where the original commit had no usable test_patch — a SWE-agent authors a security test from scratch given the security fix.

This path expects the upstream pipeline to have been run in `--require_test false` mode, i.e.:

- `python -m susvibes.curate.mine.process --require_test false ...` — keeps vulnerability records that have no associated test files
- `python -m susvibes.curate.adaptive_gen.core --require_test false ...` — produces tasks without requiring a `test_patch` field
- `python -m susvibes.curate.env_setup.build_repo --prologue --require_test false ...` — instructs SWE-agent (sv-env-setup) to run the full repo test suite instead of a designated set of test files

The synthesis agent itself uses the config at [`../utils/agents/configs/test_gen.yaml`](../utils/agents/configs/test_gen.yaml). Set it up the same way as other SWE-agent configs in this curation pipeline (place under SWE-agent's `config/` directory; see [`../utils/agents/settings.yaml`](../utils/agents/settings.yaml)).

### 1. Build rollback images + prepare agent batch

```bash
python -m susvibes.curate.test.gen_prologue \
  --run_id <run_id> \
  --max_workers <N> \
  [--strategy patch_secfix|secfix]  # Optional: prompt hint variant, default patch_secfix
  [--instance_ids '["<id_1>", ...]']
  [--force]                         # Optional: rebuild rollback images even if present
```

For each candidate this builds a *rollback* variant of the env image with the `security_patch` reversed (so the repo sits in the vulnerable state) and the patch persisted at `.susvibes.security_patch.diff` so the agent can toggle states. It then assembles the SWE-agent batch instances yaml.

### 2. Run the synthesis agent

Run SWE-agent with `test_gen.yaml` as specified in [`../utils/agents/runs.sh`](../utils/agents/runs.sh), pointing at the batch yaml produced in step 1.

For each instance, the agent must produce:

- `sectests.sh` — the entrypoint that runs the synthesized tests
- `secresults.json` — per-test pass/fail JSON written by the test runner
- supporting test files (typically a small Python runner script)

### 3. Validate

```bash
python -m susvibes.curate.validate.no_test \
  --test_agent_output_dir <path_to_agent_output> \
  --run_id <run_id> \
  --max_workers <N> \
  [--from_existing_specs] \
  [--force]
```

This pulls each agent's `model_patch` as the candidate `test_patch`, runs the repo's functional tests across three variants (base / rollback / task), then runs the synthesized sectests under both `rollback_with_test` and `base_with_test`, verifying that at least one test distinguishes the vulnerable from the secure state. Successful instances get an evaluation image tagged and the synthesized `test_patch` written back into the dataset.
