# Changelog

## Unreleased

### Phase A — Agent harness interface & prompt infrastructure

- **Shared harness Protocol + data types.** `AgentHarness` / `DockerHarness` Protocols,
  `AgentResult` / `PredictionRecord` TypedDicts, and `normalize_prediction` helper in
  `evaluation_harness/base.py`.
- **Canonical prompt infrastructure.** `evaluation_harness/common.py` aligns
  `apply_safety_hint()` with the strategy pipeline's `GENERIC_PROMPT`;
  `evaluation_harness/PROMPTS.md` documents the two-layer prompt architecture.
  Fixed broken `ADDITIONAL_INSTRUCTIONS` imports in the Claude/Gemini harnesses.
- **Offline test suite.** Hand-curated fixtures, pytest `live`/`docker` markers so
  expensive tests are opt-in and deselected by default.

### Phase B — Docker lifecycle deduplication & ACR override

- **Deduplicated Docker lifecycle.** `DockerHarnessBase` in `evaluation_harness/base.py`
  factors extract/start/exec/setup-env/cleanup out of the per-agent `run_docker.py`
  files, which become thin subclasses supplying only `name` and `env_source_files`.
- **Opt-in registry override.** Ported `resolve_image_name()` (driven by
  `ACR_REGISTRY_URL`) from the Endor fork into `susvibes/core/utils.py`, applied once
  in `DockerHarnessBase.__init__`; unset = Docker Hub (unchanged upstream behavior).
- **Real-Docker tests (no LLM).** Opt-in `docker`-marked lifecycle tests parametrized
  over both harnesses, offline ACR-resolution unit tests, and a reproduction of the
  pre-existing container cleanup leak (`tests/test_docker_cleanup_leak.py`).

## v1.0

v1.0 sharpens the security signal and closes reward-hacking loopholes. It succeeds v0.0
(200 tasks) with a 186-task set; both stay selectable on the leaderboard, with v1.0 preferred
going forward.

### What's new

**Human-verified security tests.** A security test lifted from a vulnerability-fix commit can be
bound to that specific fix, so a *different but equally secure* implementation may still fail it.
In v1.0 every security test was reviewed by human annotators to confirm it checks a security
property, and revised where needed to pass any correct secure solution — not just the reference.

**Reward-hack-resistant environments.** Eval images now remove `.git` and re-initialize each repo
as a fresh single-commit repo, so agents can't recover the fix from git history. Reward hacking
through other channels (e.g. web search) still requires custom guardrails on the agent scaffold.

**Corrected CWE labels.** A small fraction of `cwe_ids` were re-annotated for precision.

**Tighter task set (200 → 186).** 14 tasks that couldn't meet the raised security-test quality bar
were dropped; none added.

### Effect on scores
- Human-verified security tests likely lift SecPass slightly; overall trends stay similar.
- Removing the git-history channel can lower FuncPass and SecPass for agents that reward-hacked
  through it.
- In **both** versions, trajectories that reward-hack count as failures.

### Migrating v0.0 results to v1.0
- **Revised security tests** — re-evaluate existing solutions against the new tests on the 186
  tasks; patches are unchanged, so no new inference.
- **Git-history hardening** — detect the git-hacking trajectories and re-run only those on the new
  image; the rest carry over.

### Reproducibility
v0.0 and v1.0 are pinned to fixed dataset snapshots (git tags `v0.0` and `v1.0`). v0.0 scores are
computed on the 200-task set and remain valid there; they are not directly comparable to v1.0.

## v0.0

Initial release — 200-task security-oriented vibe coding benchmark.
