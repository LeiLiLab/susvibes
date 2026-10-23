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

- **clean** — no copy our build left behind. Copies that are not ours are set aside, counted by
  reason under `aside` and marked in `sweep.json`: a tracked repo file, pip's vendored copy, the
  CPython stdlib, another project's installed package or cached archive (a dependency that bundles
  the code), an npm dependency, and — named apart so it can be reviewed — a published release of
  the project itself under `node_modules`. That last one is a lockfile-pinned release, so it
  normally predates the fix (the openclaw audit in `docs/golden-leak` found no self-dependency
  published after its record's fix); it cannot be removed, so an instance whose copy does carry
  the golden code has to be excluded by hand.
- **fixed** — the copies were removed and the result verified: the instance re-validates with the
  same `expected_pf`, the eval image's `Cmd` / `WorkingDir` are unchanged, and a re-sweep finds
  nothing. The rebuilt images take the standard names; the leaky originals stay as
  `eval_leak_<id>`, `env_leak_<id>` and, for a record that validates from a `base_no_test` image
  (an edited `test_patch`), `base_no_test_leak_<id>`. A removal that only touches package download
  caches is applied to the eval image directly (`cache_only`), with no re-validation.
- **residue** — a copy stays, and the instance is excluded (`keep.leak = false`): either no removal
  rule covers it (`refused`), or removing it broke one of the checks above, in which case the
  original images are restored under their standard names. `reason` says which. A run that did
  not conclude (`error` set) also keeps nothing, as in validate, until `--resume` concludes it. A residue reviewed
  and accepted by hand keeps its verdict: list its instance id in `datasets/<run_id>/known_residue.json`
  (a JSON list).

Every image the fix touched is recorded under `images`; `sweep.json` / `resweep.json` beside the
report hold the raw hits. The `*_leak_<id>` snapshots are a record of the originals, not a rollback
point: a `--force` re-run checks the images as they stand (so an instance rebuilt upstream since is
simply re-checked), and only a run that died mid-fix plans from the snapshots again.

## Usage

Runs on a `validate`d run: each gated record's eval image must be local or pullable, and a record
that names a `base_no_test` image needs it local (the pipeline never pushes one).

```bash
python -m susvibes.curate.leak.core \
  --run_id playground \
  --max_workers 2 \
  [--pull_images]   # pull the eval (and env) images from the Docker Hub instead of requiring them locally
  [--push_images]   # push each fixed instance's rebuilt eval and env images
  [--resume | --force]
```

The sweep runs a self-contained python inside each image (the image may have none). It is fetched
once into `build/sweep_python/`; point `SV_SWEEP_PYTHON_DIR` at an unpacked
[python-build-standalone](https://github.com/astral-sh/python-build-standalone) `python/` directory
to reuse an existing one.
