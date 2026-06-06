# check_cov engine — in-container scoring

The `engine/` package is the scoring core of `check_cov`. It is **vendored**: the host
COPYies it into every per-instance `cov_py` container's site-packages as
`_susvibes_cov_engine` and runs it there, so it parses each repo with that
interpreter's **native `ast`/`jedi`/`parso`**. The host orchestration is in
[`../README.md`](../README.md); this file is the **scoring reference** — what evidence
produces which label.

Being vendored into **py2.7–3.12** containers, the package is **py2/py3
dual-compatible** (no `StrEnum`/`TypedDict`/f-strings/py3 annotations, relative imports,
`OrderedDict` results) — which is why its style differs from the host code.

`worker.py` is the container entry: it reads `input.json` (instance meta + `targets`)
and the repo at `/project`, calls **`analyze`** (pure — no I/O, no docker), and prints
the `CoverageResult` JSON after a `<<<COV_RESULT>>>` marker (stdout is the only channel
back to the host).

## Labels

| label (`CoverageLabel`) | meaning |
|---|---|
| `likely_covered`   | strong evidence (a test reaches a target symbol directly, or imports the target module) |
| `maybe_covered`    | indirect evidence (multi-hop symbol chain, import graph, conftest, framework route/CLI, string ref) |
| `unlikely_covered` | repo analyzable but no evidence links any test to the file (incl. no test suite) |
| `unknown`          | the analysis ran but could not decide: jedi unreliable, or the target file would not parse |

The instance label is the **best** over its per-file results (`LABEL_RANK`, then score)
— any covered security-patch file is positive evidence. `classify` maps a score to a
label: `≥ Classifier.LIKELY_THRESHOLD` → likely, `> 0` → maybe, `0` → unlikely.

## How a target is scored

`analyze` picks **one** trace engine per repo — `symbol_trace` (precise, jedi) if
`symbol_trace.usable()` finds jedi can parse the repo, else `file_trace` (the no-jedi
fallback, rarely needed now the cov containers ship a py2-capable jedi) — and
**always** layers `heuristics` (runtime wiring) on top. The per-file
score is the max over every layer that fired. (No test suite at all → every target is
`unlikely_covered` immediately.)

### symbol_trace (jedi) — S1–S4
A backward BFS over the symbol use-graph. **One predicate
(`traceable_symbol_positions`) defines what counts as a traceable node** —
**module-level functions / classes / globals, and class methods (properties
included)** (it qualifies only if referenceable from **outside** the file and resolved
precisely by `get_references`). **Seeds** are *all* such symbols in the target file —
so a class is **always** seeded. **Successors** taint only the *innermost* such node
enclosing a use, so a class is tainted **only** when the use sits directly in a class
body (base class / class-variable value) or in a dunder; an ordinary method-body use
taints the **method**, never its class. Deliberately excluded: **class variables and instance attributes**
(`self.x` is scattered, possibly dynamic/inherited, shared mutable state — running
`get_references` on it over-links every method that merely touches it into bogus
chains; the **class** is seeded instead, which covers "a test constructs/subclasses
it"); **imports** (resolve to a definition in another file → would match that external
symbol's uses repo-wide); **nested functions** (unreachable from outside their scope).

`get_references` finds every use of a seed; **`successors`** routes each use to the
innermost qualifying symbol that **carries** it, re-expanded on the next hop:

| the use sits… | tainted successor |
|---|---|
| in a **test file** | — hit (FIFO frontier ⇒ the first hit is the shortest chain) |
| in a **function / method body** | that function (a wrapper chain of any length is followed); a **dunder**, lacking a by-name call site, continues through its **class** |
| **directly in a class body** (base class, class-variable value) | the class |
| bound into a **module-level global** (`urlpatterns = [View]`) | that global |
| a bare module-top-level statement, no name to follow | — only the S4 import-reachability backstop applies |

Type-annotation uses (`x: T`, `-> T`) are skipped (looked up as a type, never
executed). Scope discovery uses parso, not jedi inference (only `get_references`
touches jedi — its name inference is thread-unsafe).

| id | evidence | score |
|---|---|---|
| S1 | a test references a target symbol directly (≤1 hop) | `0.95` |
| S2 | reached through 2–3 wrapping/global hops | `0.80` |
| S3 | reached through a deeper real reference chain | `0.65` |
| S4 | target symbol used at module top level of a test-imported file (backstop) | `0.55` |

### file_trace — no-jedi fallback: F1–F6
When jedi is unusable, reachability is approximated over imports/symbols.

**F1–F2 — a test imports the target**

| id | evidence | score |
|---|---|---|
| F1 | a test directly imports the target module / a symbol from it | `0.92` |
| F2 | a test imports a symbol the package `__init__` re-exports from target | `0.85` |

**F3–F4 — import-graph reachability**

| id | evidence | score |
|---|---|---|
| F3 | target reachable from a test within ≤2 import hops | `0.55` |
| F4 | target reachable within a deeper import-graph hop bound | `0.45` |

**F5–F6 — distinctive-symbol reference**

| id | evidence | score |
|---|---|---|
| F5 | a test references a distinctive symbol the target defines | `0.35` |
| F6 | a distinctive symbol the target defines appears only as a string in a test | `0.30` |

### heuristics — always run: H1–H9
Runtime wiring neither trace engine sees, layered on top of whichever one ran.

**H1–H4 — fixture / CLI / dynamic-string wiring**

| id | evidence | score |
|---|---|---|
| H1 | conftest imports target — stronger if a fixture it defines is used by a test | `0.70` / `0.60` |
| H2 | target declares a CLI command a test invokes (`runner.invoke`) | `0.55` |
| H3 | target module/path referenced as a **string** in a test (dynamic import) | `0.45` |
| H4 | target module referenced as a string in a **test-reachable production file** (registry wiring) | `0.45` |

**H5–H9 — web-framework route tables.** A URL declared somewhere is dispatched to the
target at runtime; the rules differ only in *where* the URL lives.

| id | evidence | score |
|---|---|---|
| H5 | target **self-declares a route** by decorator (Flask/FastAPI) and a test client hits the same path | `0.55` |
| H6 | a **Django URL table** (`path`/`re_path`/`url`) routes a test URL to a view the target defines | `0.55` |
| H7 | a **DRF router** (`router.register`) routes a test URL to a viewset the target defines | `0.55` |
| H8 | the target's **Blueprint/APIRouter prefix** + its own route match a test client's full URL | `0.55` |
| H9 | **web2py convention**: target is a controller, a test string carries its `<app>/<ctrl>` URL + a target function name | `0.55` |

## Low-false-positive design
- **Distinctive symbols** (F5/F6, H2-CLI, the H6/H7 view anchor): a target-defined name
  counts only if it is long enough and defined few enough times repo-wide —
  uniqueness, not a hand-maintained stop-list, filters ubiquitous names (`get`/`run`).
- **Routes are double-anchored**: a route must tie to a symbol the target defines (a
  *distinctive* one for H6/H7) **and** a test must actually request the matching URL;
  too-short route stems are rejected.
- **String rules (H3/H4)** require a *dotted* module path, so a bare package name
  appearing incidentally as a string is not matched.
- **Decorator detection** matches the last dotted segment exactly (`app.route` →
  `route`), so `@mock.patch` etc. don't masquerade as routes.

## Reliability and `unknown`
`symbol_trace.score` returns `(evidence, reliable)`. `reliable` is **False** only when
the trace could not complete — the target file did not parse, or too many
`get_references` calls failed — **and** no hit was found; that empty result is then
reported as **`unknown`**, never a false `unlikely_covered`. A found hit is always
reliable. The file engine has the analogous "mostly unparseable → unknown" override.

## Where the rest lives
Tunable thresholds (score cutoff, graph depths, distinctiveness, jedi caps, retry /
failure limits) are in [`constants.py`](constants.py), grouped by owning engine. The
shared `RepoIndex` (imports, module map, re-exports, route table, symbol def-counts,
test-to-file BFS depths) is built in [`repo_index.py`](repo_index.py); path→module
mapping is in [`modules.py`](modules.py); the tolerant `ast → tree-sitter → regex` fact
extractor is in [`extract_facts.py`](extract_facts.py).

