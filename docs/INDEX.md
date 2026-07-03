# Docs index

Topic-based working areas. Each `docs/<topic>/` has a `README.md` as the through-line for that topic — open it first when revisiting.

## Conventions

- **Status header**: every topic's `README.md` opens with `**Status**: active | paused | done | superseded by <link>` and `**Last touched**: YYYY-MM-DD`. Skim this index before drilling in.
- **Code in topic dirs**:
  - `scratch_*.py` — one-off / experimental scripts. Throwaway by default.
  - `reusable_*.py` — candidates for promotion to a real module under `susvibes/`. If it stays useful, graduate it out.
- **Skill / agent references go one-way**: a topic `README.md` may link to skills it uses; skills should NOT link back into topic docs (skills must be self-contained).
- **Promotion path**: scratch → reusable → proper module. A topic shouldn't host long-term production code.

## Topics

| Topic | Status | Last touched | Summary |
|---|---|---|---|
| [mask-quality](mask-quality/) | active | 2026-06-19 | Auditing mask-generation outputs: quality buckets + modification/invention detection. |
