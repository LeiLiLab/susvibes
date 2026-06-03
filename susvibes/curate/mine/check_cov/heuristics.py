"""Domain heuristics for coverage edges that symbol/import references can't see.

These run regardless of which trace engine is used, because the link between a
test and the target is not a Python symbol reference here:

    H1   conftest imports the target (a fixture wiring), stronger if a fixture it
         defines is consumed by a test
    H2   the target declares a framework ROUTE a test exercises (Flask/FastAPI/
         Django client paths) — routed at runtime, no static edge to the target
    H3   the target declares a CLI command a test invokes (Click runner.invoke)
    H4   the target module/path is referenced as a STRING in a test (dynamic import
         / registry wiring, e.g. importlib.import_module("pkg.x")) — no static edge
    H5   the target module is referenced as a STRING in a test-reachable production
         file (dynamic / registry wiring written in app code, e.g. ROOT_URLCONF)
"""
from susvibes.curate.mine.check_cov.constants import Heuristics
from susvibes.curate.mine.check_cov.constants import basename
from susvibes.curate.mine.check_cov.repo_index import RepoIndex, distinctive_symbols

SCORE_CONFTEST_USED = 0.70   # H1: conftest imports target and a fixture it defines is used by a test
SCORE_CONFTEST = 0.60        # H1: conftest imports target
SCORE_FRAMEWORK = 0.55       # H2/H3: test exercises a route / invokes a CLI command the target defines
SCORE_STRING_REF = 0.45      # H4/H5: target module/path referenced as a string (in a test, or test-reachable prod)

_HTTP_DECORATORS = ("route", "get", "post", "put", "delete", "patch")
_CLI_INVOKERS = {"invoke", "CliRunner", "main"}


def _decorator_tail(dotted: str) -> str:
    """Last dotted segment of a decorator name (``app.route`` -> ``route``)."""
    return dotted.rsplit(".", 1)[-1]


def score(target, index: RepoIndex):
    """Heuristic (H1-H5) evidence for a target; returns a list of (score, message)."""
    if target not in index.sources:
        return []
    facts = index.facts[target]
    target_modules = set(index.file_modules.get(target, []))
    # For STRING-literal matches (H4/H5 dynamic wiring) require a dotted module path:
    # a bare top-level package name (e.g. a package __init__'s "certifi"/"journalpump")
    # appears as a string incidentally — logging tags, resource lookups like
    # files("certifi") — so matching it yields false positives, whereas "pkg.sub.mod"
    # as a string (e.g. Django ROOT_URLCONF="ownphotos.urls") is a real dynamic ref.
    # (H1 below keeps target_modules: it matches IMPORTS, which are never incidental.)
    dotted_modules = {m for m in target_modules if "." in m}
    unique_syms = distinctive_symbols(target, index)
    target_routes = facts["route_paths"]
    is_route_file = any(_decorator_tail(d) in _HTTP_DECORATORS for d in facts["decorators"])
    is_cli_file = any(_decorator_tail(d) in ("command", "group") for d in facts["decorators"])
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
                             f"[H1] conftest imports target ({tf})"))

        # H2 / H3: framework route / CLI correspondence.
        if is_route_file and target_routes and (target_routes & tfacts["route_paths"]):
            evidence.append((SCORE_FRAMEWORK, f"[H2] test exercises matching route path ({tf})"))
        if is_cli_file and (unique_syms & tfacts["used_names"]) and (_CLI_INVOKERS & tfacts["used_names"]):
            evidence.append((SCORE_FRAMEWORK, f"[H3] test invokes target CLI command ({tf})"))

        # H4: target module / path referenced as a string in the test (dynamic import).
        if dotted_modules & tfacts["strings"]:
            evidence.append((SCORE_STRING_REF, f"[H4] test references target module as string ({tf})"))
        elif target in tfacts["strings"]:
            evidence.append((SCORE_STRING_REF, f"[H4] test references target path as string ({tf})"))

    # H5: target module referenced as a string in a production file the tests can
    # reach within a few hops — dynamic / registry wiring with no static edge.
    for rel, fd in index.bfs_depth.items():
        if 1 <= fd <= Heuristics.DYNAMIC_WIRING_MAX_DEPTH and rel not in index.test_set \
                and dotted_modules & index.facts[rel]["strings"]:
            evidence.append((SCORE_STRING_REF,
                             f"[H5] target module referenced as string in test-reachable {rel}"))
            break

    return evidence
