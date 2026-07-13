# Changelog

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

Initial release — 200-task security benchmark.
