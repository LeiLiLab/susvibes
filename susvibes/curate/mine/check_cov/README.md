# check_cov — static file-level test-coverage analysis

`check_cov` runs **after `process`**. For each instance in
`datasets/<run_id>/fix_dataset.jsonl` it decides — at `base_commit`, by static
analysis only (no execution) — whether the repo's own test suite covers the files
touched by the `security_patch`, at the **file level**: does any test reach any one of
them. The design goal is to **minimize false negatives** (a file that really is tested
must not be marked uncovered).

Each instance is analyzed **inside a container whose Python version matches the
instance** (from `dev_tools.json`), so the scoring engine runs on that interpreter's
native `ast`/`jedi`/`parso` — no Python-2/3 parser conflicts. The host resets the repo
to `base_commit`, copies the working tree + the `engine/` package + an `input.json`
into a per-instance `cov_py` image, runs it once, and reads the result JSON from its
logs; it never imports jedi itself. How the engine actually scores each file is
documented in [`engine/README.md`](engine/README.md).

## Usage

```bash
python -m susvibes.curate.mine.check_cov \
    --run_id playground \
    --max_workers 5 \
    [--instance_ids '["id1", ...]'] \
    [--max_depth 12]
```

This needs the per-version `cov_py` images. `build_base` pre-builds and pushes one per
Python version (a `cov_py:<version>` is `base_py:<version>` + the matched jedi/parso
stack); the run machine pulls them automatically. Build any that are missing with:

```bash
python -m susvibes.curate.env_setup.build_base \
    --mode build --image_names '["cov_py"]' --versions '["3.10", ...]'
```

## Output

- **`datasets/<run_id>/coverage_report.jsonl`** — one `CoverageResult` per instance
  that ran (label, score, and the per-file evidence breakdown). Written incrementally.
- **`logs/curate/<run_id>/mine/check_cov/summary.json`** — a run summary of which
  instances succeeded and why the rest failed; also printed to stdout.

Each instance's label is `likely_covered` / `maybe_covered` / `unlikely_covered` /
`unknown` (see [`engine/README.md`](engine/README.md)). An instance that could not run
at all — no `dev_tools` version, no `.py` target in the patch, or a container failure
— is reported as a failure in the summary, not in the report. `check_cov` does not
modify `fix_dataset.jsonl`.
