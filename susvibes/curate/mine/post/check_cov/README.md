# check_cov — static file-level test-coverage analysis

`check_cov` is **optional**: an early stop that drops instances whose vulnerable code the repo's
own suite likely never exercises, before the expensive agent stages spend anything on them — a
lower final count in exchange for that saved cost. It runs **after `test_mask`**, so its rejections
never withhold records from the mandatory stage. For each instance in
`datasets/<run_id>/dataset.jsonl` it decides — at `base_commit`, by static
analysis only (no execution) — whether the repo's own test suite covers the files
touched by the `security_patch`, at the **file level**: does any test reach any one of
them. The design goal is to **minimize false negatives** (a file that really is tested
must not be marked uncovered).

Each instance is analyzed **inside a container whose Python version matches the
instance** (from `dev_tools.json`), so the scoring engine runs on that interpreter's
native `ast`/`jedi`/`parso` — no Python-2/3 parser conflicts. The host resets the repo
to `base_commit`, copies the working tree + the `engine/` package + an `input.json`
into a per-instance `static_py` image, runs it once, and reads the result JSON from its
logs; it never imports jedi itself. How the engine actually scores each file is
documented in [`engine/README.md`](engine/README.md).

## Usage

```bash
python -m susvibes.curate.mine.post.check_cov \
    --run_id playground \
    --max_workers 5 \
    [--instance_ids '["id1", ...]'] \
    [--max_depth 12]
```

This needs the per-version `static_py` images. `build_base` pre-builds and pushes one per
Python version (a `static_py:<version>` is `base_py:<version>` + the matched jedi/parso
stack); the run machine pulls them automatically. Build any that are missing with:

```bash
python -m susvibes.curate.env_setup.build_base \
    --mode build --image_names '["static_py"]' --versions '["3.10", ...]'
```

## Output

The result lands on the record itself: `check_cov` annotates each analyzed record in
`dataset.jsonl` in place with a `func_coverage` object (the `CoverageResult` minus
`instance_id`) and a `keep.func_coverage` boolean — `True` iff the label is `likely_covered` /
`maybe_covered` (see [`engine/README.md`](engine/README.md) for how a label is scored). Downstream
stages admit only records passing every recorded `keep.*` verdict, so an `unlikely_covered` /
`indeterminate` instance is dropped here.

Each instance also gets a `report.json` under `logs/curate/<run_id>/mine/check_cov/<instance_id>/`,
beside the container's own logs. The report is the cache a re-run reuses: an instance the engine
could not analyze at all (no `.py` target in the patch) still concludes, with a bare status; only a
container failure sets `error`, which `--resume` re-runs and a plain re-run reuses. A run summary
over all of them is saved to `summary.json` and printed to stdout.
