"""Precise backward symbol trace via jedi.

Subsumes the file-level approximations (direct import, re-export, import-graph
reachability, distinctive-symbol matching) with a single precise question: is
there a real symbol-reference chain from some test into a symbol DEFINED in the
target file?

The trace is a backward BFS over the symbol use-graph. Starting from the symbols
defined in the target file (functions, classes, and module-level globals),
jedi's project-wide ``get_references`` finds every place each symbol is used. For
each use we look at the SCOPE that contains it:

  - a use inside a test file means the target is reached (covered);
  - a use inside another function/class body taints that enclosing definition,
    which is expanded on the next hop — so a test that calls a wrapper that calls
    ... that calls the target symbol is found, however many hops away;
  - a use at MODULE TOP LEVEL bound into a global (``urlpatterns = [MyView]``,
    ``HANDLERS = {...: target}``) taints that global, which is expanded next — so
    a test that imports the global reaches the target. This keeps the trace
    symbol-precise instead of degrading to "the file is importable" the moment a
    symbol is used outside a function;
  - a use at module top level with no name to continue (a bare ``register(X)``)
    has no symbol to follow: the file simply executes it on import, so the only
    signal left is whether a test imports that file (import-graph reachability).
    This narrow backstop applies ONLY to a file where we actually found such a
    top-level reference, so it does not reintroduce blanket file-level matching.

The frontier is FIFO, so hop distance grows monotonically and the FIRST test hit
is the shortest chain — the search returns immediately on that hit. ``seen``
dedup keeps the walk linear; ``max_depth`` bounds how far a chain is followed.

Scope discovery uses parso (the syntax tree), NOT jedi name inference:
``Name.type`` / ``infer`` would spawn jedi's compiled subprocess to resolve
imports, which is not thread-safe and corrupts its pipe under the thread pool.
Only ``get_references`` (a pure reference search) uses jedi.

jedi (current releases) parses Python 3 only. For Python 2 repos parsing fails;
``usable()`` samples the repo so the caller can fall back to the file-level engine.
"""
from collections import deque
from pathlib import Path

import jedi
import parso

from susvibes.curate.mine.check_cov.constants import (
    JEDI_PARSE_FAIL_RATIO, JEDI_PARSE_SAMPLE,
    GET_REFERENCES_RETRIES, SYMBOL_TRACE_MAX_FAILURES,
)
from susvibes.curate.mine.check_cov.constants import is_test_file

# Evidence scores for symbol-trace hits (precise → high); the shortest chain wins.
SCORE_DIRECT = 0.95         # a test references a target-defined symbol directly (1 hop)
SCORE_CHAIN_NEAR = 0.80     # reached through 2-3 wrapping/global hops
SCORE_CHAIN_FAR = 0.65      # reached through more hops (still a real reference chain)

# Decorators that make a method implicitly invoked (via attribute access, not an
# explicit ``obj.method()`` call) — like dunder methods, these have no by-name call
# site for get_references to find, so the trace must continue through the class.
_PROPERTY_DECORATORS = {"property", "cached_property", "setter", "getter", "deleter"}
# A target symbol is used by a bare top-level statement in a non-test file that a
# test imports: the file runs the statement on import, but no method body need run
# (often only construction/registration) — weaker, like a file-level reach.
SCORE_TOPLEVEL_REACH = 0.55


def usable(repo_dir, sources) -> bool:
    """Sample-parse repo files with jedi; return False (→ file-level fallback) when
    too many fail, which in practice means a Python 2 repo jedi cannot parse."""
    rels = list(sources)[:JEDI_PARSE_SAMPLE]
    if not rels:
        return False
    fails = 0
    for rel in rels:
        try:
            jedi.Script(code=sources[rel], path=str(Path(repo_dir) / rel)).get_names()
        except Exception:
            fails += 1
    return fails / len(rels) <= JEDI_PARSE_FAIL_RATIO


class JediContext:
    """Per-instance jedi/parso state. Not reusable across instances: each instance
    is a different commit, so the working tree (and parse results) differ."""

    def __init__(self, repo_dir, test_set):
        self.repo_dir = Path(repo_dir)
        self.project = jedi.Project(str(repo_dir))
        self.test_set = test_set
        self._scripts = {}
        self._trees = {}

    def script(self, abs_path) -> jedi.Script:
        key = str(abs_path)
        sc = self._scripts.get(key)
        if sc is None:
            sc = jedi.Script(path=key, project=self.project)
            self._scripts[key] = sc
        return sc

    def invalidate(self, abs_path):
        """Drop a cached Script so the next access rebuilds it — used to recover
        from a transient get_references failure (its inference state may be
        corrupted, e.g. a poisoned compiled-subprocess pipe under load)."""
        self._scripts.pop(str(abs_path), None)

    def parso_tree(self, abs_path):
        """Cached, error-tolerant parso parse of a file (no jedi inference)."""
        key = str(abs_path)
        if key not in self._trees:
            try:
                self._trees[key] = parso.parse(
                    Path(abs_path).read_text(encoding="utf-8", errors="replace"))
            except Exception:
                self._trees[key] = None
        return self._trees[key]

    def rel(self, module_path):
        """Repo-relative posix path for a module path, or None if outside repo."""
        if not module_path:
            return None
        try:
            return Path(module_path).relative_to(self.repo_dir).as_posix()
        except ValueError:
            return None


def build(repo_dir, sources, test_set) -> JediContext:
    return JediContext(repo_dir, test_set)


def _assignment_lhs_names(expr_stmt):
    """(line, col) of each name assigned to on the LHS of an ``expr_stmt``.

    A name is an assignment target when at least one ``=`` operator follows it
    among the statement's direct children, so ``a = b = rhs`` yields a and b but
    not rhs, and a bare expression (no ``=``) yields nothing."""
    children = expr_stmt.children
    eq = [i for i, c in enumerate(children)
          if c.type == "operator" and c.value == "="]
    if not eq:
        return []
    return [c.start_pos for c in children[:eq[-1]] if c.type == "name"]


def _defined_name_positions(tree):
    """(line, col) of every traceable symbol DEFINED in a file: function/class
    names, and the target names of module-level assignments (globals)."""
    if tree is None:
        return []
    out = []
    # Function/class definitions anywhere in the file.
    stack = list(getattr(tree, "children", []))
    while stack:
        node = stack.pop()
        if node.type in ("funcdef", "classdef"):
            out.append(node.name.start_pos)
        if hasattr(node, "children"):
            stack.extend(node.children)
    # Module-level globals: direct module children that are assignments.
    for stmt in getattr(tree, "children", []):
        if stmt.type != "simple_stmt":
            continue
        for child in stmt.children:
            if child.type == "expr_stmt":
                out.extend(_assignment_lhs_names(child))
    return out


def _seed_positions(ctx, target):
    """(abs_path, [(line, col), ...]) of symbols defined in the target file."""
    abs_path = ctx.repo_dir / target
    return abs_path, _defined_name_positions(ctx.parso_tree(abs_path))


def _decorator_tails(funcnode):
    """Last dotted segment of each decorator on a funcdef (``@x.setter`` -> setter,
    ``@property`` -> property); empty if the funcdef carries no decorators."""
    decorated = funcnode.parent
    if decorated is None or decorated.type != "decorated":
        return []
    tails = []
    for dec in decorated.children:
        if dec.type != "decorator":
            continue
        names = [c.value for c in dec.children if c.type == "name"]
        for c in dec.children:
            if c.type == "trailer":
                names += [cc.value for cc in c.children if cc.type == "name"]
        if names:
            tails.append(names[-1])
    return tails


def _is_implicit_method(funcnode) -> bool:
    """Whether a method is invoked implicitly (no ``obj.method()`` call site for
    get_references to find): a dunder, or a property/descriptor accessor."""
    name = funcnode.name.value
    if name.startswith("__") and name.endswith("__"):
        return True
    return any(t in _PROPERTY_DECORATORS for t in _decorator_tails(funcnode))


def _enclosing_class(funcnode):
    """The classdef a funcnode is a direct method of, or None if it is a top-level
    or nested-in-function definition."""
    node = funcnode.parent
    while node is not None and node.type != "module":
        if node.type == "classdef":
            return node
        if node.type == "funcdef":
            return None  # nested inside a function → not a direct method
        node = node.parent
    return None


def _successors(ctx, ref):
    """What to trace next from a non-test reference site, as
    ``(successor_positions, module_level)``:

      - inside a function/class body → [(abs_path, line, col)] of the innermost
        enclosing def's name (taint the wrapper), module_level=False. When that
        innermost def is an implicitly-invoked method (a dunder or property — it
        has no by-name call site), the trace continues through its CLASS instead,
        since the method runs when the class is instantiated / its attribute read;
      - at module top level inside an assignment → the LHS global name
        position(s) to trace who uses that global, module_level=True;
      - at module top level with nothing to continue → [], module_level=True
        (the caller may apply the import-reachability backstop)."""
    abs_path = Path(ref.module_path)
    tree = ctx.parso_tree(abs_path)
    if tree is None:
        return [], False
    try:
        leaf = tree.get_leaf_for_position((ref.line, ref.column))
    except Exception:
        leaf = None
    if leaf is None:
        return [], False

    # Inside a function/class body? Innermost enclosing def is hit first going up.
    node = leaf.parent
    while node is not None and node.type != "module":
        if node.type == "classdef":
            return [(abs_path, *node.name.start_pos)], False
        if node.type == "funcdef":
            if _is_implicit_method(node):
                cls = _enclosing_class(node)
                if cls is not None:
                    return [(abs_path, *cls.name.start_pos)], False
            return [(abs_path, *node.name.start_pos)], False
        node = node.parent

    # Module top level: trace the assignment global(s) this reference feeds, if any.
    node = leaf.parent
    while node is not None and node.type != "module":
        if node.type == "expr_stmt":
            names = _assignment_lhs_names(node)
            if names:
                return [(abs_path, *p) for p in names], True
            break
        node = node.parent
    return [], True


def _score_for_hop(hop):
    if hop <= 1:
        return SCORE_DIRECT
    if hop <= 3:
        return SCORE_CHAIN_NEAR
    return SCORE_CHAIN_FAR


def _get_references(ctx, path, line, col):
    """Project-wide references for a symbol, retrying a transient jedi failure
    (rebuilding the Script in case its inference state was corrupted, e.g. a
    poisoned compiled-subprocess pipe under load). Returns the reference list, or
    None if every attempt failed."""
    for _ in range(GET_REFERENCES_RETRIES + 1):
        try:
            return ctx.script(path).get_references(line, col, scope="project")
        except Exception:
            ctx.invalidate(path)
    return None


def score(target, ctx, index, max_depth):
    """Backward BFS from the target's symbols. Returns ``(evidence, reliable)``.

    ``evidence`` is a list with the single strongest (score, message), or [] if no
    test reaches the target. FIFO frontier ⇒ hop distance is monotonic ⇒ the first
    test reference found is the shortest chain, so the search returns on the first
    hit. Reaching a target symbol only through a bare top-level statement in a
    test-importable file is a weaker backstop, returned only if no chain is found.

    ``reliable`` is False when the trace could not be completed — the target file
    did not parse, or too many get_references calls failed — AND no hit was found,
    so the empty result must be read as ``unknown`` (could not tell), not
    ``unlikely_covered`` (confidently uncovered). A found hit is always reliable: a
    real chain is definitive regardless of failures elsewhere in the walk."""
    if ctx.parso_tree(ctx.repo_dir / target) is None:
        return [], False  # target unparseable → cannot seed the trace → unknown

    abs_path, seeds = _seed_positions(ctx, target)
    frontier = deque((abs_path, line, col, 0) for line, col in seeds)
    seen = set()
    backstop = None  # (score, message) — weakest-evidence fallback
    failures = 0     # get_references calls that failed after retries

    while frontier:
        path, line, col, depth = frontier.popleft()
        key = (str(path), line, col)
        if key in seen or depth >= max_depth:
            continue
        seen.add(key)

        refs = _get_references(ctx, path, line, col)
        if refs is None:
            failures += 1   # lost this hop; keep walking other branches
            continue

        for ref in refs:
            if ref.is_definition():
                continue
            rel = ctx.rel(ref.module_path)
            if rel is None:
                continue
            if is_test_file(rel):
                hop = depth + 1
                return [(_score_for_hop(hop),
                         f"test {rel}:{ref.line} reaches target symbol (symbol hop {hop})")], True
            successors, module_level = _successors(ctx, ref)
            for s in successors:
                frontier.append((*s, depth + 1))
            # Bare top-level use in a non-test file the tests can import: the file
            # runs it on import, so the target is reached (weakly) at file load.
            if module_level and not successors and backstop is None \
                    and rel in index.bfs_depth:
                backstop = (SCORE_TOPLEVEL_REACH,
                            f"target symbol used at top level of test-reachable {rel}")

    if backstop:
        return [backstop], True
    # No hit: trust the negative only if the walk was mostly complete. Too many
    # failed reference searches mean we cannot rule coverage out → unknown.
    return [], failures <= SYMBOL_TRACE_MAX_FAILURES
