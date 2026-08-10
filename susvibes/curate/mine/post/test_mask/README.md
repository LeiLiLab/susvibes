# test_mask — turning a fix commit's test diff into a test mask

`test_mask` is **mandatory**, and runs **after `dev_tools`** — before the optional filters, so that
their rejections never withhold records from it. A mined record's `raw_test_patch` is the diff the
fix commit made to the test files; those tests are what a task is graded against, so the tree a task
ships must not contain them. For each instance in `datasets/<run_id>/dataset.jsonl` this stage
rolls the test files back to `base_commit`, empties every test function body the fix touched, and
puts the resulting diff in `test_patch` — the one name every later stage reads. Mining's own diff
stays put under `raw_test_patch`, so re-deriving a mask never masks a mask.

Deriving the mask means parsing the test file, and a Python 2-era one is unparseable by the host:
its `ast` rejects `print "x"` / `except E, e` / `123L`, and parso >= 0.8 dropped the 2.7 grammar.
So the parse runs **inside the instance's own version-matched `static_py` container** (from
`dev_tools.json`), exactly as `check_cov` does. Only text crosses the boundary — every git
operation stays on the host, so the container never sees the repo.

## Usage

```bash
python -m susvibes.curate.mine.post.test_mask \
    --run_id playground \
    --max_workers 5 \
    [--instance_ids '["id1", ...]'] \
    [--force | --resume]
```

This needs the per-version `static_py` images, the same ones `check_cov` uses; see
[`check_cov/README.md`](../check_cov/README.md) for how `build_base` provides them.

## Output

The result lands on the record itself: `test_mask` annotates each record in `dataset.jsonl`
with `test_patch` — the mask — and a `keep.test_mask` boolean. It is the only writer of that field,
so a record carrying it is one whose tests are already hidden.

Each instance also gets a `report.json` under `logs/curate/<run_id>/mine/test_mask/<instance_id>/`,
concluding in one of three ways — `masked`, `no_test_patch` (the fix shipped no test, so there is
nothing to hide; kept), or `unparseable` (the test file is genuinely broken; dropped). A container
failure is not a conclusion: it sets the report's `error`, which `--resume` re-runs and a plain
re-run reuses. A run summary over all of them is saved to `summary.json` and printed to stdout.
