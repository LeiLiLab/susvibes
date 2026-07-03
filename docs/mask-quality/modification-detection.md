# Modification & invention detection

Per-hunk analysis of a flagged `mask_patch` to separate genuine quality issues from harmless str_replace artifacts.

## The three categories per `+` line

Every non-header `+` line lands in exactly one of:

| Category | Definition | Verdict |
|---|---|---|
| **Placeholder** | Body matches the placeholder pattern (see `quality-buckets.md`). | Skip. |
| **Preserved structure** | The line's body appears verbatim as a `-` line in the same hunk. | Skip — round-trip artifact of how str_replace generates diffs (signatures, imports, brackets re-emitted on both sides). |
| **Modification pair (魔改)** | A `-` line and a `+` line in the same hunk share an LHS identifier (`name = …`) but their bodies differ. | **Violation** — agent rewrote a line's value (e.g., `-host = ri.netloc.split(splitstr)[0]` / `+host = None`). |
| **Suspect new add** | None of the above. | **Probable violation** — agent injected content with no corresponding source. |

## LHS identifier extraction

```python
import re
LHS = re.compile(r"([A-Za-z_][\w\.]*)\s*(?::\s*[^=]+)?\s*=")
def key_of(line: str):
    m = LHS.match(line.strip())
    return ("assign", m.group(1)) if m else None
```

Catches `NAME = ...`, `NAME: type = ...`, `self.x = ...`. Misses non-assignment modifications (function call arg changes, return changes, control-flow tweaks) — those fall through to **suspect new add**.

## Per-hunk algorithm

```python
def analyze(patch):
    mods, sus_adds = [], []
    hunks, cur = [], []
    for ln in patch.splitlines():
        if ln.startswith("@@"):
            if cur: hunks.append(cur)
            cur = []
        else:
            cur.append(ln)
    if cur: hunks.append(cur)

    for h in hunks:
        minus = [l for l in h if l.startswith("-") and not l.startswith("---")]
        plus  = [l for l in h if l.startswith("+") and not l.startswith("+++")]
        minus_bodies = {m[1:] for m in minus}
        minus_keys   = {key_of(m[1:]): m for m in minus if key_of(m[1:])}
        for p in plus:
            body = p[1:]
            if PLACEHOLDER.match(body):       continue
            if body in minus_bodies:          continue
            k = key_of(body)
            if k and k in minus_keys and minus_keys[k][1:].strip() != body.strip():
                mods.append((minus_keys[k], p))
            else:
                sus_adds.append(p)
    return mods, sus_adds
```

## Reporting

Aggregate over a cohort:

- `mods`: total `(-, +)` modification pairs
- `inst_with_mods`: count of distinct instances with at least one
- `sus_adds`: total suspect-new lines
- `inst_with_sus`: distinct instances

Paired OLD-vs-NEW comparison is the strongest signal: same `instance_id`s on both sides, label each instance FIXED / NEW issue / UNCHANGED / REGRESSION.

## Human pass after the auto detector

The detector flags candidates; quality is decided by reading the lines. Common verdicts seen on v3:

- **Trivial / false positive**: `-kwargs = dict()` / `+kwargs = {}` — semantically identical rewrite the detector still pairs as a mod.
- **Genuine 魔改**: `-host = ri.netloc.split(splitstr)[0]` / `+host = None` — the canonical "A = expr → A = None" pattern.
- **Genuine 魔改 the detector misses**: function call with the function name unchanged but args dropped, e.g. `-load(..., freq)` / `+load(...)` — no `=` so LHS regex doesn't match.
- **Real suspect new add**: `+def get_exp(self):` when the original signature was `def get_exp(self, experiment_id=None, ...):` — agent simplified a signature.
- **Benign suspect**: a multi-line signature's `):` re-emitted on the `+` side after str_replace removed the middle args.

## Known limitations

- Only LHS-form assignments are paired as modifications. Function-call rewrites, control-flow rewrites, and return-statement rewrites all land in **suspect new add** instead of being labeled as mods explicitly.
- Whitespace-only differences in a `-` / `+` pair are reported as a mod. Treat as trivial in human review.
- `+` lines whose body matches a `-` line from a **different** hunk are reported as suspect new adds. Rare in practice but possible.
