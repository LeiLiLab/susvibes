# Leak check

An eval image can carry a readable copy of the instance's golden code outside the agent-visible
work tree: a wheel in pip's cache, `build/lib` output, an installed copy under `site-packages`,
bytecode of the fixed tree. The suite still runs the agent's code, but the answer key is on disk.
This stage runs after `validate` and before `wrap_up`: it sweeps every validated instance's eval
image for such copies, removes what a rule covers, and proves the instance still validates the
same way.

## Outcome

One verdict per instance (`leak_status` in `logs/curate/<run_id>/leak/<instance_id>/report.json`,
projected onto the record as `leak` and as its `keep.leak` verdict):

- **clean** — no copy our build left behind. Copies that are not ours are set aside and counted
  (`aside`): tracked repo files, pip's vendored copy, the CPython stdlib, a dependency that bundles
  the code, a lockfile-pinned npm package.
- **fixed** — the copies were removed and the result verified: the instance re-validates with the
  same `expected_pf`, the eval image's `Cmd` / `WorkingDir` are unchanged, and a re-sweep finds
  nothing. The rebuilt `env_<id>` / `eval_<id>` images take the standard names; the leaky originals
  stay as `env_leak_<id>` / `eval_leak_<id>`. A removal that only touches package download caches
  is applied to the eval image directly (`cache_only`), with no re-validation.
- **residue** — a copy stays, and the instance is excluded (`keep.leak = false`): either no removal
  rule covers it (`refused`), or removing it broke one of the checks above, in which case the
  original images are restored under their standard names. `reason` says which. A residue reviewed
  and accepted by hand is listed in `datasets/<run_id>/known_residue.json` and keeps its verdict.

Every image the fix touched is recorded under `images`; `sweep.json` / `resweep.json` beside the
report hold the raw hits.

## Usage

```bash
python -m susvibes.curate.leak.core \
  --run_id playground \
  --max_workers 2 \
  [--pull_images]   # pull each eval (and env) image from the Docker Hub instead of using the local one
  [--push_images]   # push each fixed instance's rebuilt images
  [--resume | --force]
```

The sweep runs a self-contained python inside each image (the image may have none). It is fetched
once into `build/sweep_python/`; point `SV_SWEEP_PYTHON_DIR` at an unpacked
[python-build-standalone](https://github.com/astral-sh/python-build-standalone) `python/` directory
to reuse an existing one.
