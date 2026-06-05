# -*- coding: utf-8 -*-
"""Domain heuristics for coverage edges that symbol/import references can't see.

These run regardless of which trace engine is used, because the link between a
test and the target is a RUNTIME wiring (fixture injection, route dispatch, CLI
dispatch, dynamic string load) with no static Python symbol reference:

    H1   conftest imports the target (fixture wiring), stronger if a fixture it
         defines is consumed by a test
    H2   the target declares a CLI command a test invokes (Click runner.invoke)
    H3   the target module/path is referenced as a STRING in a test (dynamic import
         / registry wiring, e.g. importlib.import_module("pkg.x"))
    H4   the target module is referenced as a STRING in a test-reachable production
         file (dynamic / registry wiring in app code, e.g. ROOT_URLCONF)

    -- route-table heuristics (test hits a URL, framework dispatches to the target) --
    H5   the target SELF-DECLARES a route via decorator (Flask @app.route / FastAPI
         @router.get ...) and a test client exercises the same path
    H6   a Django URL table (urls.py path/re_path/url(route, view)) routes to a view
         the target defines, and a test client hits that route
    H7   a DRF router (router.register(prefix, ViewSet)) routes to a viewset the
         target defines, and a test client hits a URL under that prefix
    H8   the target's Blueprint url_prefix / APIRouter prefix + its own route match a
         test client's full (prefixed) URL
    H9   web2py convention routing: the target is applications/<app>/controllers/
         <ctrl>.py, a test string contains the URL segment <app>/<ctrl>, and a target
         function name appears in the test (the test hits /<app>/<ctrl>/<function>)

H5-H8 share one idea — a URL declared somewhere is dispatched to the target at
runtime — but the URL lives in different places: H5 a decorator in the target,
H6/H7 a central route table in another file, H8 a prefixed blueprint/router. To
keep false positives low, every route rule is DOUBLE-ANCHORED: the route must be
tied to a symbol the target defines (for H6/H7 a DISTINCTIVE one) AND a test must
actually request the matching URL — never a bare path-substring match.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import re

from .constants import Heuristics
from .constants import basename
from .repo_index import distinctive_symbols

SCORE_CONFTEST_USED = 0.70   # H1: conftest imports target and a fixture it defines is used by a test
SCORE_CONFTEST = 0.60        # H1: conftest imports target
SCORE_FRAMEWORK = 0.55       # H2 / H5-H9: CLI invoke or a route dispatched to the target
SCORE_STRING_REF = 0.45      # H3/H4: target module/path referenced as a string

HTTP_DECORATORS = ("route", "get", "post", "put", "delete", "patch")
CLI_INVOKERS = {"invoke", "CliRunner", "main"}
WEB2PY_CTRL = re.compile(r'(?:applications/)?([^/]+)/controllers/([^/]+)\.py$')


def decorator_tail(dotted):
    """Last dotted segment of a decorator name (``app.route`` -> ``route``)."""
    return dotted.rsplit(".", 1)[-1]


def route_stem(route):
    """Static leading segment of a route/regex/prefix, lowercased & slash-trimmed:
    ``users/`` -> 'users', ``^profile/$`` -> 'profile', ``u/<int:pk>/`` -> 'u',
    ``r"^api/(?P<x>...)"`` -> 'api'. Used to match a test URL against a route table
    entry by static prefix (params/regex tails dropped)."""
    r = route.strip().lstrip("^").rstrip("$")
    r = re.split(r'[<(\\?*]', r)[0]
    return r.strip("/").lower()


def test_hits_route(test_routes, route):
    """Whether any test client route (``client.get("/x")`` path) hits ``route`` by
    static prefix. A too-short stem (<3 chars) is ambiguous and rejected (FP guard)."""
    stem = route_stem(route)
    if len(stem) < 3:
        return False
    for tr in test_routes:
        t = tr.strip("/").lower()
        if t == stem or t.startswith(stem + "/"):
            return True
    return False


def score(target, index):
    """Heuristic (H1-H9) evidence for a target; returns a list of (score, message)."""
    if target not in index.sources:
        return []
    facts = index.facts[target]
    target_modules = set(index.file_modules.get(target, []))
    # For STRING-literal matches (H3/H4 dynamic wiring) require a dotted module path:
    # a bare top-level package name (e.g. a package __init__'s "certifi") appears as a
    # string incidentally (logging tags, resource lookups), so matching it yields false
    # positives, whereas "pkg.sub.mod" as a string is a real dynamic ref. (H1 keeps
    # target_modules: it matches IMPORTS, which are never incidental.)
    dotted_modules = set(m for m in target_modules if "." in m)
    unique_syms = distinctive_symbols(target, index)
    target_defs = facts["defined_symbols"]
    target_routes = facts["route_paths"]
    is_route_file = any(decorator_tail(d) in HTTP_DECORATORS for d in facts["decorators"])
    is_cli_file = any(decorator_tail(d) in ("command", "group") for d in facts["decorators"])
    evidence = []

    for tf in index.test_set:
        tfacts = index.facts[tf]

        # H1: conftest imports target (stronger if a fixture it defines is used).
        if basename(tf) == "conftest.py" and target_modules & index.file_imports[tf]:
            used = tfacts["defined_symbols"] and any(
                tfacts["defined_symbols"] & index.facts[other]["used_names"]
                for other in index.test_set if other != tf
            )
            evidence.append((SCORE_CONFTEST_USED if used else SCORE_CONFTEST,
                             "[H1] conftest imports target ({0})".format(tf)))

        # H2: target declares a CLI command a test invokes.
        if is_cli_file and (unique_syms & tfacts["used_names"]) and (CLI_INVOKERS & tfacts["used_names"]):
            evidence.append((SCORE_FRAMEWORK, "[H2] test invokes target CLI command ({0})".format(tf)))

        # H3: target module / path referenced as a string in the test (dynamic import).
        if dotted_modules & tfacts["strings"]:
            evidence.append((SCORE_STRING_REF, "[H3] test references target module as string ({0})".format(tf)))
        elif target in tfacts["strings"]:
            evidence.append((SCORE_STRING_REF, "[H3] test references target path as string ({0})".format(tf)))

        # H5: target self-declares a route (decorator) a test client exercises.
        if is_route_file and target_routes and (target_routes & tfacts["route_paths"]):
            evidence.append((SCORE_FRAMEWORK, "[H5] test exercises matching route path ({0})".format(tf)))

    # H4: target module referenced as a string in a production file the tests can
    # reach within a few hops — dynamic / registry wiring with no static edge.
    for rel, fd in index.bfs_depth.items():
        if 1 <= fd <= Heuristics.DYNAMIC_WIRING_MAX_DEPTH and rel not in index.test_set \
                and dotted_modules & index.facts[rel]["strings"]:
            evidence.append((SCORE_STRING_REF,
                             "[H4] target module referenced as string in test-reachable {0}".format(rel)))
            break

    # H6 / H7: a route table in ANOTHER file (urls.py path/re_path/url, or DRF
    # router.register) routes a URL to a DISTINCTIVE symbol the target defines, and a
    # test client hits that route. Double-anchored: distinctive view symbol + test URL.
    if unique_syms:
        for rel, f in index.facts.items():
            if rel in index.test_set or not f["url_patterns"]:
                continue
            for kind, route, view in f["url_patterns"]:
                if view not in unique_syms:
                    continue
                for tf in index.test_set:
                    if test_hits_route(index.facts[tf]["route_paths"], route):
                        if kind == "register":
                            evidence.append((SCORE_FRAMEWORK,
                                "[H7] test hits DRF route '{0}' -> target viewset {1} ({2})".format(route, view, tf)))
                        else:
                            evidence.append((SCORE_FRAMEWORK,
                                "[H6] test hits Django route '{0}' -> target view {1} ({2})".format(route, view, tf)))
                        break

    # H8: the target's own Blueprint/APIRouter prefix + its route match a test's full URL.
    prefix = facts["route_prefix"]
    if prefix and target_routes:
        for r in target_routes:
            full = "/" + (prefix.strip("/") + "/" + r.strip("/")).strip("/")
            fl = full.lower().rstrip("/")
            hit = False
            for tf in index.test_set:
                for tr in index.facts[tf]["route_paths"]:
                    trn = tr.lower().rstrip("/")
                    if trn == fl or trn.startswith(fl + "/"):
                        evidence.append((SCORE_FRAMEWORK,
                            "[H8] test exercises prefixed route {0} ({1})".format(full, tf)))
                        hit = True
                        break
                if hit:
                    break

    # H9: web2py convention routing — target is a controller, a test string carries
    # the URL segment <app>/<ctrl>, and a target function name appears in the test.
    m = WEB2PY_CTRL.match(target)
    if m:
        url_seg = (m.group(1) + "/" + m.group(2)).lower()
        for tf in index.test_set:
            tstr = index.facts[tf]["strings"]
            if any(url_seg in s.lower() for s in tstr) and (target_defs & tstr):
                evidence.append((SCORE_FRAMEWORK,
                    "[H9] web2py controller {0} reached via URL + function in test ({1})".format(url_seg, tf)))
                break

    return evidence
