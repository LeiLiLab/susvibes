# Mask quality buckets

Coarse classification of each `mask_patch` by what its `+` lines look like. Used as a quick first-pass health check before drilling into per-line analysis.

## The three buckets

| Bucket | Definition | Reading |
|---|---|---|
| **Pure deletion** | Patch contains zero `+` lines (excluding the `+++` file header). | Cleanest — mask is a strict subtraction. |
| **Placeholder-only** | All non-header `+` lines are placeholders. | Acceptable — agent collapsed an emptied unit's body. |
| **Flagged** | Any non-header `+` line that is not a placeholder. | Needs the finer analysis in `modification-detection.md`. |

## What counts as a placeholder

Stripped body must match one of:

- empty / whitespace-only
- `pass`
- `...`
- `raise NotImplementedError`
- `return` (with or without a value — debatable; consider tightening if abused)

Anything else flips the patch into the **flagged** bucket and earns finer analysis.

## How to compute

For each instance's `mask_patch`:

```python
import re
PLACEHOLDER = re.compile(r"^\s*$|^\s*(pass|\.\.\.|raise NotImplementedError|return)\s*$")

def classify(patch: str) -> str:
    adds = [l[1:] for l in patch.splitlines()
            if l.startswith("+") and not l.startswith("+++")]
    if not adds:
        return "pure_deletion"
    if all(PLACEHOLDER.match(a) for a in adds):
        return "placeholder_only"
    return "flagged"
```

## Reporting

For a dataset, count instances per bucket and report as percentages. Two cohorts on the same instance IDs are comparable directly:

```
                  pure  placeholder  flagged
OLD baseline      51%   40%          9%
NEW (v3 prompt)   51%   37%          12%
```

A drop in pure-deletion or placeholder-only is a regression even if the flagged share stays small.

## What this bucket does NOT tell you

- Whether the mask is the right *size* — that's a separate length / ratio analysis.
- Whether a flagged patch is genuinely problematic — many `+` lines are str_replace artifacts (signatures, imports) that round-trip identically with a `-` line. See `modification-detection.md` for the next layer.
