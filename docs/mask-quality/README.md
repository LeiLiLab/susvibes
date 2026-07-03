# Mask quality

**Status**: active
**Last touched**: 2026-06-19

Auditing mask-generation outputs (the `mask_patch` column in `task_dataset.jsonl`) for structural and semantic quality. Used during prompt iteration to catch agents that "魔改" (rewrite existing lines) or invent new code rather than cleanly deleting.

## Through-line

- `quality-buckets.md` — coarse three-bucket classification (pure deletion / placeholder-only / flagged). First-pass health check on a cohort.
- `modification-detection.md` — per-hunk analysis on flagged patches: separate true modifications (魔改 pairs) from suspect-new additions and from harmless str_replace artifacts.

## Skills / agents in use

None yet. The analysis is currently inline Python. If we re-run this enough to justify it, promote `quality-buckets.md`'s `classify` and `modification-detection.md`'s `analyze` out of these docs into a skill or a real susvibes module.

## Workflow notes

- **Validation harness candidate**: the `classify` + `analyze_patch` + `compare(OLD, NEW, ids)` pipeline is a clear graduation candidate. Next time we iterate the mask prompt, extract it into `reusable_validate.py` here — single import / single CLI invocation. If it gets used a second time, promote out of `docs/` into a real susvibes module.
- **Parallel-run for prompt iteration**: do NOT iterate variants serially (A → measure → B → measure → C → measure). Run 2–3 variants in parallel sessions (git worktrees, or just clearly-labeled `task_dataset.<variant>.jsonl` files), compare reports side-by-side. Avoids anchoring bias from prior results and halves wall time.

## Open questions / TODO

- The LHS-only regex misses non-assignment modifications (function-call arg drops, control-flow rewrites). All such cases currently land in "suspect new add" without an explicit "mod" label. Either tighten detection or accept the conflation.
- Decide whether `return X` (with a return value) counts as a placeholder. Today it's treated as one; this may be too permissive.
- Some `+` lines pair with `-` lines in a *different* hunk, not the same one — currently those are mislabeled as suspect new adds. Rare in practice; revisit if it starts noisy.

## Findings log (newest first)

### 2026-06-19 — v3 50-instance stress test with new HARD NOTE

New HARD NOTE added: *"Your modification must contain only two kinds of changes per file: (1) `-` lines that delete code, and (2) `+` lines that are pure placeholders. Any other `+` content is forbidden."*

Result on 50 instances (paired vs OLD baseline):

|              | OLD | NEW |
|---            |---:|---:|
| mods total    |   6 |   4 |
| inst w/ mods  |   4 |   3 |
| sus total     |  34 |   7 |
| inst w/ sus   |   4 |   5 |

Net positive: octoprint's big rewrite (30 sus + 2 mods) almost fully fixed; requests / guarddog / zulip fixes. Remaining: nltk still does `A = expr → A = None` (HARD NOTE didn't stop it); a few new minor sus introductions (qlib signature simplification).
