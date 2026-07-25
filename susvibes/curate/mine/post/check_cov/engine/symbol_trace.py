# -*- coding: utf-8 -*-
"""Precise backward symbol trace via jedi.

Subsumes the file-level approximations (direct import, re-export, import-graph
reachability, distinctive-symbol matching) with a single precise question: is
there a real symbol-reference chain from some test into a symbol DEFINED in the
target file?

The trace is a backward BFS over the symbol use-graph. Seeds are the symbols
defined in the target file that can be referenced from outside it and reliably found
by get_references — module-level functions, classes, and globals, plus class methods
(properties included); see ``seed_symbol_positions``. jedi's project-wide
``get_references`` finds every place each symbol is used; ``successors`` maps each
use to the innermost such symbol that carries it, expanded on the next hop:

  - a use inside a test file means the target is reached (covered);
  - a use inside a function/method body taints that function, expanded on the next
    hop — so a wrapper chain of any length is found; a dunder, lacking a by-name
    call site, continues through its CLASS instead;
  - a use directly in a class body (a base class, a class-variable value) taints the
    class — who instantiates or subclasses it is then traced;
  - a use bound into a module-level global (``urlpatterns = [MyView]``) taints that
    global, so a test that uses the global reaches the target. This keeps the trace
    symbol-precise instead of degrading to "the file is importable" the moment a
    symbol is used at module top level;
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

Runs inside a version-matched cov_py container, so the jedi/parso here are the
ones pinned for the target's Python version (jedi 0.19/parso 0.8 on py3.6+, jedi
0.17/parso 0.7 on py2.7/3.5) — so jedi parses the sources and the trace is the sole
coverage engine (no file-level fallback).
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import re
from collections import deque

try:
    from pathlib import Path           # py3 (and py2 if pathlib backport present)
except ImportError:                    # py2.7 stdlib has no pathlib
    from pathlib2 import Path

import jedi
import jedi.inference.references as jedi_references
import parso

from .constants import SymbolTrace
from .heuristics import test_hits_route, test_uses_named_route

# jedi's project-wide get_references silently caps how many files it opens/parses
# via hardcoded module globals, dropping test references on large repos (see the
# constants). Raise them. Module-level so it runs once per worker process on import;
# search_in_file_ios reads these globals at call time, so patching here takes effect.
# Guarded by hasattr so a jedi version without these globals does not break import.
if hasattr(jedi_references, "_OPENED_FILE_LIMIT"):
    jedi_references._OPENED_FILE_LIMIT = SymbolTrace.JEDI_OPENED_FILE_LIMIT
if hasattr(jedi_references, "_PARSED_FILE_LIMIT"):
    jedi_references._PARSED_FILE_LIMIT = SymbolTrace.JEDI_PARSED_FILE_LIMIT

# Evidence scores for symbol-trace hits (precise -> high); the shortest chain wins.
SCORE_DIRECT = 0.95         # S1: a test references a target-defined symbol directly (hop<=1)
SCORE_CHAIN_NEAR = 0.80     # S2: reached through 2-3 wrapping/global hops
SCORE_CHAIN_FAR = 0.65      # S3: reached through >3 hops (still a real reference chain)

# A target symbol is used by a bare top-level statement in a non-test file that a
# test imports: the file runs the statement on import, but no method body need run
# (often only construction/registration) — weaker, like a file-level reach.
SCORE_TOPLEVEL_REACH = 0.55   # S4: target symbol used at module top level of a test-imported file (backstop)

# The backward walk reached a function that is itself an HTTP route handler (a route
# decorator dispatches to it) whose route a test exercises: the change is reached at
# runtime through that route, the dispatch edge get_references cannot cross. Same score
# as the route heuristics (a runtime dispatch, not a direct reference) — a backstop.
SCORE_ROUTE_BRIDGE = 0.55     # S5: a test hits the route of a handler the walk reached from the change


class JediContext(object):
    """Per-instance jedi/parso state. Not reusable across instances: each instance
    is a different commit, so the working tree (and parse results) differ."""

    def __init__(self, repo_dir, source_roots=()):
        self.repo_dir = Path(repo_dir)
        # Prepend each non-root source root (e.g. a src/ layout's "src") so the repo's
        # own packages shadow an installed same-named package — added_sys_path goes to
        # the FRONT of jedi's sys.path, ahead of site-packages.
        added = [str(self.repo_dir / r) for r in sorted(source_roots) if r]
        self.project = jedi.Project(str(repo_dir), added_sys_path=added)
        self._scripts = {}
        self._trees = {}

    def script(self, abs_path):
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


def build(repo_dir, source_roots=()):
    return JediContext(repo_dir, source_roots)


def annassign_value_op(expr_stmt):
    """The ``=`` operator node of an annotated assignment (``x: T = v`` -> children
    ``[name, annassign]``), or None when ``expr_stmt`` is not an annotated assignment
    WITH a value (a bare ``x: T`` declaration binds nothing)."""
    children = expr_stmt.children
    if len(children) == 2 and children[1].type == "annassign":
        for c in children[1].children:
            if c.type == "operator" and c.value == "=":
                return c
    return None


def assignment_lhs_names(expr_stmt):
    """(line, col) of each name assigned to on the LHS of an ``expr_stmt``.

    A name is an assignment target when at least one ``=`` operator follows it
    among the statement's direct children, so ``a = b = rhs`` yields a and b but
    not rhs, and a bare expression (no ``=``) yields nothing. An annotated assignment
    ``a: T = rhs`` (whose ``=`` is nested in an ``annassign`` child) yields a too, but a
    bare ``a: T`` annotation (no value) yields nothing."""
    children = expr_stmt.children
    if annassign_value_op(expr_stmt) is not None:
        return [children[0].start_pos] if children[0].type == "name" else []
    eq = [i for i, c in enumerate(children)
          if c.type == "operator" and c.value == "="]
    if not eq:
        return []
    return [c.start_pos for c in children[:eq[-1]] if c.type == "name"]


def definition_scope(node):
    """The scope a definition lives in: 'module', 'class', or 'function' (the kind of
    the nearest enclosing def/class, or 'module' if none)."""
    parent = node.parent
    while parent is not None:
        if parent.type == "funcdef":
            return "function"
        if parent.type == "classdef":
            return "class"
        if parent.type == "module":
            return "module"
        parent = parent.parent
    return "module"


def seed_symbol_positions(tree, seed_names=None):
    """(line, col) of every symbol DEFINED in a file that can be referenced from
    OUTSIDE it and reliably found by get_references — used to SEED the backward trace
    (``successors`` applies the same symbol-kind rule, but by walking UP from a single
    use rather than scanning a whole file, so it does not call this). When
    ``seed_names`` is given, only symbols whose name is in it are returned (the
    containment-narrowed seed; see containment.py):

      - module-level function / class / global variable;
      - class method (property included).

    Class variables and instance attributes are NOT seeded. An instance attribute
    (``self.X``) has no real definition point (scattered across methods, possibly set
    via setattr or inherited) and is shared mutable state — get_references on it links
    every method that merely touches the attribute, fabricating chains between code
    that never calls each other. Seeding the CLASS instead covers the real case (a
    test that constructs or subclasses it) without that hazard. Imports are not seeded
    either: an imported name resolves to a definition in ANOTHER file, so
    get_references would match that external symbol's uses project-wide. Nested
    functions and function-local names are excluded too — nothing outside their
    function can reference them."""
    if tree is None:
        return []
    out = []
    stack = list(getattr(tree, "children", []))
    while stack:
        node = stack.pop()
        t = node.type
        if t in ("funcdef", "classdef"):
            if definition_scope(node) in ("module", "class"):
                out.append(node.name.start_pos)
        elif t == "expr_stmt":
            if definition_scope(node) == "module":
                out.extend(assignment_lhs_names(node))   # module-level global
        if hasattr(node, "children"):
            stack.extend(node.children)
    if seed_names is not None:
        kept = []
        for pos in out:
            leaf = tree.get_leaf_for_position(pos)
            if leaf is not None and leaf.value in seed_names:
                kept.append(pos)
        out = kept
    return out


def seed_positions(ctx, target, seed_names=None):
    """(abs_path, [(line, col), ...]) of the seed symbols in the target file —
    narrowed to ``seed_names`` (the security_patch's containing symbols) when given."""
    abs_path = ctx.repo_dir / target
    return abs_path, seed_symbol_positions(ctx.parso_tree(abs_path), seed_names)


def is_implicit_method(funcnode):
    """Whether a method is invoked implicitly, with no by-name site for get_references
    to find — a dunder (``__init__`` on construction, ``__getitem__`` on indexing):
    the trace must continue through its class. A property is NOT implicit — ``obj.attr``
    IS its by-name site (verified) — so it traces as an ordinary method."""
    name = funcnode.name.value
    return name.startswith("__") and name.endswith("__")


def enclosing_class(funcnode):
    """The classdef a funcnode is a direct method of, or None if it is a top-level
    or nested-in-function definition."""
    node = funcnode.parent
    while node is not None and node.type != "module":
        if node.type == "classdef":
            return node
        if node.type == "funcdef":
            return None  # nested inside a function -> not a direct method
        node = node.parent
    return None


def child_between(children, node, start_op, end_ops):
    """True if the direct child ``node`` sits after the first ``start_op`` operator
    and before any ``end_ops`` operator among ``children`` — used to locate an
    annotation region (after ``:`` / ``->``, before a ``=`` default or the suite ``:``)."""
    try:
        idx = children.index(node)
    except ValueError:
        return False
    start = next((i for i, c in enumerate(children)
                  if c.type == "operator" and c.value == start_op), None)
    if start is None or idx <= start:
        return False
    return not any(c.type == "operator" and c.value in end_ops
                   for c in children[start + 1:idx])


def is_annotation_ref(ctx, ref):
    """Whether a reference sits in a TYPE-ANNOTATION position: a parameter annotation
    ``def f(x: T)``, a variable annotation ``x: T`` (not its ``= value`` part), or a
    return annotation ``-> T``. Such a name is only looked up as a type object — it
    never executes the referenced symbol — so it is not a coverage edge and must not
    propagate the backward trace (e.g. ``mw: AnkiQt`` does not run AnkiQt)."""
    tree = ctx.parso_tree(Path(ref.module_path))
    if tree is None:
        return False
    try:
        leaf = tree.get_leaf_for_position((ref.line, ref.column))
    except Exception:
        leaf = None
    if leaf is None:
        return False
    node, parent = leaf, leaf.parent
    while parent is not None and parent.type != "module":
        # parameter annotation `x: T` is wrapped in a `tfpdef` node (the `param`
        # only holds the tfpdef + an optional `= default`); variable annotation
        # `x: T` is an `annassign`. In both, the type is after `:` and before `=`.
        if parent.type in ("tfpdef", "param", "annassign") \
                and child_between(parent.children, node, ":", ("=",)):
            return True
        if parent.type == "funcdef" and child_between(parent.children, node, "->", (":",)):
            return True
        node, parent = parent, parent.parent
    return False


def is_monkeypatch_ref(ctx, ref):
    """Whether a reference is the TARGET of an assignment (``pkg.symbol = ...``): the
    symbol is being REBOUND, not used. A test that rebinds a production symbol is
    mocking / monkey-patching it — the real (masked) implementation never runs — so
    this is not a coverage edge (``FederationHttpClient.post_json_get_nothing = Mock()``
    does not exercise the real method)."""
    tree = ctx.parso_tree(Path(ref.module_path))
    if tree is None:
        return False
    try:
        leaf = tree.get_leaf_for_position((ref.line, ref.column))
    except Exception:
        leaf = None
    if leaf is None:
        return False
    node, parent = leaf, leaf.parent
    while parent is not None and parent.type != "module":
        if parent.type == "expr_stmt":
            eqs = [i for i, c in enumerate(parent.children)
                   if c.type == "operator" and c.value == "="]
            try:
                idx = parent.children.index(node)
            except ValueError:
                idx = None
            # a plain `=` and our child sits before the last one -> assignment target
            return bool(eqs) and idx is not None and idx < eqs[-1]
        node, parent = parent, parent.parent
    return False


def carrier_names(expr_stmt, leaf):
    """If ``leaf`` is in the RHS of a MODULE-LEVEL assignment, the
    ``(global_positions, True)`` of the global name(s) it is bound into
    (``urlpatterns = [View]`` binds View into urlpatterns), so the trace follows who
    uses that global; else None (not a module-level assignment, or the leaf is on the
    LHS). Class-body and function-level bindings are not carried — a class-body
    reference taints the class and a function-level one taints the function (see
    ``successors``)."""
    if definition_scope(expr_stmt) != "module":
        return None
    children = expr_stmt.children
    ann_eq = annassign_value_op(expr_stmt)
    if ann_eq is not None:
        eq_pos = ann_eq.start_pos          # `x: T = rhs` -> '=' nested in annassign
    else:
        eq = [i for i, c in enumerate(children)
              if c.type == "operator" and c.value == "="]
        if not eq:
            return None
        eq_pos = children[eq[-1]].start_pos
    if (leaf.line, leaf.column) < eq_pos:
        return None   # leaf is on the LHS / annotation, not a use of the bound value
    names = assignment_lhs_names(expr_stmt)
    return (names, True) if names else None


def enclosing_symbol(leaf):
    """Innermost referenceable symbol enclosing ``leaf``, as ``(positions, module_level)``
    where positions are (line, col) of the symbol's name. The SHARED walk-up shared by
    successor-tainting and containment seeding — same symbol kinds as
    ``seed_symbol_positions``:

      - bound into a module-level global (``urlpatterns = [target]``) -> the global name(s);
      - directly in a class body (a base class, a class-variable value) -> the class;
      - inside a function/method body -> that function (a dunder, lacking a by-name call
        site, continues through its CLASS instead);
      - on a decorator of a def -> the decorated def (the decorator is a sibling of the
        funcdef under ``decorated``, so the plain walk-up would miss it);
      - nothing left at module top level -> ([], True) (caller may apply the
        import-reachability backstop)."""
    node = leaf.parent
    while node is not None and node.type != "module":
        if node.type == "expr_stmt":
            carrier = carrier_names(node, leaf)
            if carrier is not None:
                return carrier
        elif node.type == "classdef":
            if definition_scope(node) != "function":
                return [node.name.start_pos], False
        elif node.type == "funcdef":
            if definition_scope(node) != "function":   # skip nested functions: not
                if is_implicit_method(node):            # externally traceable, keep
                    cls = enclosing_class(node)         # walking up to the outer def
                    if cls is not None:
                        return [cls.name.start_pos], False
                return [node.name.start_pos], False
        elif node.type == "decorated":
            inner = node.children[-1]
            if inner.type in ("funcdef", "classdef") and definition_scope(inner) != "function":
                if inner.type == "funcdef" and is_implicit_method(inner):
                    cls = enclosing_class(inner)
                    if cls is not None:
                        return [cls.name.start_pos], False
                return [inner.name.start_pos], False
        node = node.parent
    return [], True


def successors(ctx, ref):
    """Innermost referenceable symbol that carries a non-test reference, as
    ``(successor_positions, module_level)`` — wraps ``enclosing_symbol`` with the
    reference's file path so the caller can push the successors onto the frontier."""
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
    positions, module_level = enclosing_symbol(leaf)
    return [(abs_path,) + p for p in positions], module_level


def score_for_hop(hop):
    if hop <= 1:
        return SCORE_DIRECT, "S1"
    if hop <= 3:
        return SCORE_CHAIN_NEAR, "S2"
    return SCORE_CHAIN_FAR, "S3"


def get_references(ctx, path, line, col):
    """Project-wide references for a symbol, retrying a transient jedi failure
    (rebuilding the Script in case its inference state was corrupted, e.g. a
    poisoned compiled-subprocess pipe under load). Returns the reference list, or
    None if every attempt failed."""
    for _ in range(SymbolTrace.GET_REFERENCES_RETRIES + 1):
        try:
            return ctx.script(path).get_references(line, col, scope="project")
        except Exception:
            ctx.invalidate(path)
    return None


def goto_defs(ctx, path, line, col):
    """Definitions a name points to (imports followed), with the same transient-failure
    retry as get_references. Returns [] if every attempt failed."""
    for _ in range(SymbolTrace.GET_REFERENCES_RETRIES + 1):
        try:
            return ctx.script(path).goto(line, col, follow_imports=True)
        except Exception:
            ctx.invalidate(path)
    return []


def classdef_at(tree, line):
    """The classdef whose name is defined on `line`, or None."""
    stack = list(getattr(tree, "children", []))
    while stack:
        n = stack.pop()
        if n.type == "classdef" and n.name.start_pos[0] == line:
            return n
        if hasattr(n, "children"):
            stack.extend(n.children)
    return None


def method_named(classdef, name):
    """A funcdef named `name` directly in `classdef`'s body, or None."""
    stack = list(getattr(classdef, "children", []))
    while stack:
        n = stack.pop()
        if n.type == "funcdef" and n.name.value == name:
            return n
        if hasattr(n, "children"):
            stack.extend(n.children)
    return None


def base_class_names(classdef):
    """Name leaves of `classdef`'s base classes (between '(' and ')')."""
    out, inside = [], False
    for c in classdef.children:
        if c.type == "operator" and c.value == "(":
            inside = True
        elif c.type == "operator" and c.value == ")":
            break
        elif inside:
            if c.type == "name":
                out.append(c)
            elif hasattr(c, "children"):
                out.extend(lf for lf in leaves(c) if lf.type == "name")
    return out


def leaves(node):
    stack, out = [node], []
    while stack:
        n = stack.pop()
        if hasattr(n, "children"):
            stack.extend(n.children)
        else:
            out.append(n)
    return out


def overridden_base_methods(ctx, abs_path, line, col):
    """If the symbol at (line, col) is a method overriding a base-class method, the
    (abs_path, (line, col)) of the SAME-named method in each base class — possibly
    cross-file (resolved via jedi goto). A method dispatched polymorphically through
    a base ``self.m()`` has its call sites attributed to the BASE method, so seeding
    the base too is what lets the trace follow that dispatch to a test."""
    tree = ctx.parso_tree(abs_path)
    if tree is None:
        return []
    try:
        leaf = tree.get_leaf_for_position((line, col))
    except Exception:
        return []
    if leaf is None:
        return []
    name = leaf.value
    node = leaf.parent
    funcnode = None
    while node is not None and node.type != "module":
        if node.type == "funcdef":
            funcnode = node
            break
        node = node.parent
    if funcnode is None:
        return []
    cls = enclosing_class(funcnode)
    if cls is None:
        return []
    out = []
    for bl in base_class_names(cls):
        for d in goto_defs(ctx, str(abs_path), bl.start_pos[0], bl.start_pos[1]):
            mp = getattr(d, "module_path", None)
            if not mp:
                continue
            btree = ctx.parso_tree(Path(mp))
            if btree is None:
                continue
            cd = classdef_at(btree, d.line)
            if cd is None:
                continue
            m = method_named(cd, name)
            if m is not None:
                out.append((Path(mp), m.name.start_pos))
    return out


def route_segments(route):
    """Static leading path segments of a route, params/regex/format tails dropped and
    lowercased: ``/job/<int:sid>`` -> ['job'], ``/users/{id}`` -> ['users'],
    ``^api/(?P<x>...)`` -> ['api']."""
    r = route.strip().lstrip("^").rstrip("$")
    r = re.split(r"[<({\\?*]", r)[0]
    return [s for s in r.strip("/").lower().split("/") if s]


def url_hits_route(test_url, route):
    """Whether ``test_url`` contains the route's static segment-run as CONTIGUOUS path
    segments — a segment-aligned match that tolerates an external mount prefix the route
    itself does not carry (a blueprint mounted at ``/restore`` makes ``/job/<sid>`` live
    at ``/restore/job/...``). A too-short stem (<3 chars total) is rejected (FP guard)."""
    rsegs = route_segments(route)
    if not rsegs or sum(len(s) for s in rsegs) < 3:
        return False
    tsegs = [s for s in test_url.strip("/").lower().split("/") if s]
    n = len(rsegs)
    for i in range(len(tsegs) - n + 1):
        if tsegs[i:i + n] == rsegs:
            return True
    return False


# Class-based-view dispatch methods Django routes a request to (View.http_method_names).
CBV_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def cbv_route_hit(index, view_vars):
    """``(route_label, test_file)`` where a urlconf entry routes one of ``view_vars`` (an
    ``as_view()`` module var) and a test addresses that route — by name (reverse()) or by
    literal path; else None."""
    for rel, f in index.facts.items():
        if rel in index.test_set or not f["url_patterns"]:
            continue
        for kind, route, view, route_name in f["url_patterns"]:
            if view not in view_vars:
                continue
            for tf in index.test_set:
                tfacts = index.facts[tf]
                if test_uses_named_route(tfacts["url_names"], kind, route_name) \
                        or test_hits_route(tfacts["route_paths"], route):
                    return (route_name or route, tf)
    return None


def route_handler_bridge(ctx, index, path, line, col, depth):
    """If the symbol at ``(path, line, col)`` is a route handler whose route a test
    exercises, the evidence ``(score, message)``; else None. Lets the backward walk
    CONCLUDE at an HTTP-dispatched handler it reached from the change — the runtime
    route-dispatch edge ``get_references`` cannot cross. Two dispatch shapes:
      (a) a function a route DECORATOR (``@app.route`` / ``@expose``) in its file targets;
      (b) an http method (get/post/...) of a CLASS-based view exposed via ``Cls.as_view()``
          and routed by a urlconf entry."""
    rel = ctx.rel(path)
    if rel is None:
        return None
    facts = index.facts.get(rel)
    if not facts or (not facts["route_handlers"] and not facts["as_view_targets"]):
        return None
    tree = ctx.parso_tree(path)
    if tree is None:
        return None
    try:
        leaf = tree.get_leaf_for_position((line, col))
    except Exception:
        leaf = None
    if leaf is None:
        return None
    name = leaf.value

    # (a) decorator-route handler
    routes = [r for r, handlers in facts["route_handlers"].items() if name in handlers]
    for r in routes:
        for tf in index.test_set:
            for test_url in index.facts[tf]["route_paths"]:
                if url_hits_route(test_url, r):
                    return (SCORE_ROUTE_BRIDGE,
                            "[S5] test {0} hits route {1} dispatched to target handler {2} (symbol hop {3})".format(
                                tf, r, name, depth))

    # (b) class-based-view http method routed via as_view()
    if name in CBV_METHODS and facts["as_view_targets"]:
        funcnode = leaf.parent
        cls = enclosing_class(funcnode) if funcnode is not None and funcnode.type == "funcdef" else None
        if cls is not None:
            view_vars = set(v for v, c in facts["as_view_targets"].items() if c == cls.name.value)
            hit = cbv_route_hit(index, view_vars) if view_vars else None
            if hit:
                route_label, tf = hit
                return (SCORE_ROUTE_BRIDGE,
                        "[S5] test {0} addresses CBV route {1} -> target {2}.{3} (symbol hop {4})".format(
                            tf, route_label, cls.name.value, name, depth))
    return None


def score(target, ctx, index, max_depth, seed_names=None):
    """Backward BFS from the target's symbols. Returns ``(evidence, reliable)``.

    ``seed_names`` (when given) narrows the seed to the security_patch's containing
    symbols (see containment.py) instead of every symbol in the target file.

    ``evidence`` is a list with the single strongest (score, message), or [] if no
    test reaches the target. FIFO frontier => hop distance is monotonic => the first
    test reference found is the shortest chain, so the search returns on the first
    hit. Reaching a target symbol only through a bare top-level statement in a
    test-importable file is a weaker backstop, returned only if no chain is found.

    ``reliable`` is False when the trace could not be completed — the target file
    did not parse, or too many get_references calls failed — AND no hit was found,
    so the empty result must be read as ``indeterminate`` (could not tell), not
    ``unlikely_covered`` (confidently uncovered). A found hit is always reliable: a
    real chain is definitive regardless of failures elsewhere in the walk."""
    if ctx.parso_tree(ctx.repo_dir / target) is None:
        return [], False  # target unparseable -> cannot seed the trace -> indeterminate

    abs_path, seeds = seed_positions(ctx, target, seed_names)
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

        # Route-handler bridge: this reached symbol is itself an HTTP route handler whose
        # route a test exercises — the change is covered through that route's runtime
        # dispatch (the edge get_references cannot cross). A backstop, like S4: a real
        # direct reference chain found elsewhere is stronger and returns first.
        bridge = route_handler_bridge(ctx, index, path, line, col, depth)
        if bridge is not None and (backstop is None or bridge[0] > backstop[0]):
            backstop = bridge

        # If this symbol is a method overriding a base-class method, also trace the
        # base method (same hop): polymorphic ``self.m()`` dispatch attributes call
        # sites to the base. Applied to seeds AND successors uniformly (both flow
        # through here), so the two stay consistent.
        for bpath, (bl, bc) in overridden_base_methods(ctx, path, line, col):
            frontier.append((bpath, bl, bc, depth))

        refs = get_references(ctx, path, line, col)
        if refs is None:
            failures += 1   # lost this hop; keep walking other branches
            continue

        # A widely-referenced symbol is a framework/util API, not a coverage chain: check
        # its refs for a direct test hit but don't expand them (see REFERENCE_EXPAND_LIMIT).
        expand = len(refs) <= SymbolTrace.REFERENCE_EXPAND_LIMIT
        for ref in refs:
            if ref.is_definition():
                continue
            rel = ctx.rel(ref.module_path)
            if rel is None:
                continue
            if is_annotation_ref(ctx, ref):
                continue  # a type-annotation use (x: T, -> T) never executes the symbol
            if is_monkeypatch_ref(ctx, ref):
                continue  # the symbol is rebound (mock/monkey-patch), not executed
            if rel in index.test_set:   # content-checked test set (excludes test-ish non-tests)
                hop = depth + 1
                hop_score, sid = score_for_hop(hop)
                return [(hop_score,
                         "[{0}] test {1}:{2} reaches target symbol (symbol hop {3})".format(
                             sid, rel, ref.line, hop))], True
            if not expand:
                continue
            succs, module_level = successors(ctx, ref)
            for s in succs:
                frontier.append(s + (depth + 1,))
            # Bare top-level use in a non-test file a test DIRECTLY imports (import
            # depth 1): the file runs it on import, so the target is reached (weakly)
            # at file load. A deeper transitive import is too incidental to count
            # (see SymbolTrace.TOPLEVEL_REACH_MAX_DEPTH).
            file_depth = index.bfs_depth.get(rel, 0)
            if module_level and not succs \
                    and 1 <= file_depth <= SymbolTrace.TOPLEVEL_REACH_MAX_DEPTH:
                cand = (SCORE_TOPLEVEL_REACH,
                        "[S4] target symbol used at top level of test-reachable {0}".format(rel))
                if backstop is None or cand[0] > backstop[0]:
                    backstop = cand

    if backstop:
        return [backstop], True
    # No hit: trust the negative only if the walk was mostly complete. Too many
    # failed reference searches mean we cannot rule coverage out -> indeterminate.
    return [], failures <= SymbolTrace.MAX_FAILURES
