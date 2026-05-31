"""Static file-level test-coverage analysis for collected CVE instances.

Runs after `process`. For each instance in ``processed_dataset.jsonl`` it decides,
at ``base_commit`` and by static analysis only (no execution), whether the repo's
own test suite covers the files touched by the ``security_patch`` — at the FILE
level: does any test in the repo import / reference / reach any one of them.

The goal is to MINIMIZE false negatives: a file that really is tested must not be
marked uncovered. Evidence is therefore layered and additive — any plausible
signal yields at least ``maybe_covered``; only a genuine absence of evidence (or
of a test suite) yields ``unlikely_covered``.

See ``check_cov.md`` for the full design, and ``check_cov_ref.md`` for the
original rationale behind the evidence layers and scores.

Usage:
    python -m susvibes.curate.collect.check_cov --run_id <id> [--max_workers N]
"""
import argparse
import json
from enum import Enum
from collections import deque, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from pathlib import Path
from typing import NamedTuple, TypedDict

from susvibes.curate.constants import LOCAL_REPOS_DIR, COLLECT_LOG_DIR, get_path
from susvibes.utils import (
    load_file,
    save_file,
    touched_files,
    setup_instance_logger,
)
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    RepoLocks,
)
from susvibes.curate.collect.utils import (
    extract_module_facts,
    candidate_modules,
    resolve_relative_import,
    is_test_file,
)
from susvibes.curate.collect.constants import (
    SOURCE_ROOTS,
    TARGET_EXTENSIONS,
    COV_LIKELY_THRESHOLD,
    COV_MAYBE_THRESHOLD,
    IMPORT_GRAPH_MAX_DEPTH,
    DYNAMIC_WIRING_MAX_DEPTH,
    MAX_TARGET_PARSE_FAIL_RATIO,
    SYMBOL_MAX_DEFCOUNT,
    SYMBOL_MIN_LEN,
)

LOG_INSTANCE = "check_cov.log"
LOG_SUMMARY = "summary.json"


# --- labels & result types -------------------------------------------------

class CoverageLabel(str, Enum):
    """Per-file / per-instance coverage label (best -> worst).

    Subclasses ``str`` so members serialize to their value in JSON and compare
    equal to the plain string; ``__str__`` keeps logs showing the value.
    """
    LIKELY = "likely_covered"
    MAYBE = "maybe_covered"
    UNLIKELY = "unlikely_covered"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


LABEL_RANK = {
    CoverageLabel.LIKELY: 3,
    CoverageLabel.MAYBE: 2,
    CoverageLabel.UNLIKELY: 1,
    CoverageLabel.UNKNOWN: 0,
}


class CoverageResult(TypedDict):
    instance_id: str
    project: str
    base_commit: str
    label: str               # instance label (best over per_file)
    confidence: str          # high | medium | low
    score: float             # best per-file score
    per_file: dict           # target_path -> {label, score, confidence, evidence, reason}
    reason: str
    parse_failures: int      # files in the repo that fell back to regex / failed to parse
    n_target_files: int      # target-language files touched by the security patch


def _file_result(label, confidence, reason, score=0.0, evidence=None) -> dict:
    """Build a per-file coverage entry (the value type of CoverageResult.per_file)."""
    return {
        "label": label,
        "score": score,
        "confidence": confidence,
        "evidence": evidence or [],
        "reason": reason,
    }


# --- repo snapshot ---------------------------------------------------------

def _read_file_raw(path: Path) -> str:
    """Read a repo file as raw text, tolerating odd encodings.

    The whole-repo snapshot must not choke on any single file. The repo's
    ``load_file`` is suffix-dispatched and decodes strictly, so it is unsuited to
    an indiscriminate scan: ``errors="replace"`` lets a Py2-era / non-UTF-8 file
    still yield extractable imports instead of aborting the instance.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _snapshot_sources(repo_dir: Path) -> dict:
    """Return {rel_path: source} for every target-language file under repo_dir.

    Called while holding the repo lock right after reset_to_commit, so the snapshot
    reflects base_commit regardless of later resets by other workers.
    """
    repo_dir = Path(repo_dir)
    sources: dict[str, str] = {}
    for path in repo_dir.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if path.suffix in TARGET_EXTENSIONS:
            sources[path.relative_to(repo_dir).as_posix()] = _read_file_raw(path)
    return sources


def _basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


# --- import-graph index ----------------------------------------------------

class RepoIndex(NamedTuple):
    """Everything score_target needs about one repo, built once per instance."""
    sources: dict            # rel -> source text
    facts: dict              # rel -> extract_module_facts(source)
    file_imports: dict       # rel -> set of dotted modules the file imports
    import_pairs: dict       # rel -> set of (module, name) from `from module import name`
    file_modules: dict       # rel -> candidate dotted module names the file IS
    module_files: dict       # dotted module -> set of files that ARE that module
    reexports: dict          # (package_module, symbol) -> set of origin files
    defcount: Counter        # symbol name -> number of files that define it
    test_set: set            # rel paths that are test files
    bfs_depth: dict          # rel -> hops from the nearest test (forward import graph)


def _relative_anchor(rel: str, file_modules: dict) -> str:
    """The dotted module a file's relative imports resolve against. A package
    __init__.py anchors at the package itself (a synthetic trailing component
    keeps a leading-dot import at the package)."""
    primary = (file_modules.get(rel) or [""])[0]
    if rel.endswith("__init__.py"):
        return (primary + ".__init__") if primary else "__init__"
    return primary


def _resolved_imports(facts: dict, anchor: str):
    """Return (modules, pairs): absolute dotted modules the file imports, and the
    (module, name) pairs of its `from module import name` (relatives resolved)."""
    modules = set(facts["import_modules"])
    pairs = set()
    for level, mod, names in facts["from_imports"]:
        base = resolve_relative_import(anchor, level, mod) if level else (mod or "")
        if not base:
            continue
        modules.add(base)
        for nm in names:
            if nm == "*":
                continue
            modules.add(f"{base}.{nm}")
            pairs.add((base, nm))
    return modules, pairs


def _bfs_depths(test_set, file_imports, module_files, max_depth) -> dict:
    """Forward BFS over the file import graph from all tests; returns
    {rel: hops-from-nearest-test} (edge: a file imports module m, m is a file).
    Computed once and shared by all targets: L2 reads the target's own depth, L7b
    reads files within a few hops of a test."""
    depth = {tf: 0 for tf in test_set}
    queue = deque(test_set)
    while queue:
        cur = queue.popleft()
        d = depth[cur]
        if d >= max_depth:
            continue
        for m in file_imports.get(cur, ()):
            for nxt in module_files.get(m, ()):
                if nxt not in depth:
                    depth[nxt] = d + 1
                    queue.append(nxt)
    return depth


def _build_index(sources: dict, test_set: set, max_depth: int) -> RepoIndex:
    facts = {rel: extract_module_facts(src) for rel, src in sources.items()}

    file_modules: dict = {}
    module_files: dict = {}
    for rel in sources:
        mods = candidate_modules(rel, SOURCE_ROOTS)
        file_modules[rel] = mods
        for m in mods:
            module_files.setdefault(m, set()).add(rel)

    file_imports: dict = {}
    import_pairs: dict = {}
    for rel, f in facts.items():
        mods, pairs = _resolved_imports(f, _relative_anchor(rel, file_modules))
        file_imports[rel] = mods
        import_pairs[rel] = pairs

    # A package __init__.py re-exports a symbol it imports; map (package, symbol)
    # back to the file that actually defines it, so a test importing the symbol
    # from the package still counts toward that file.
    reexports: dict = {}
    for rel in sources:
        if not rel.endswith("__init__.py"):
            continue
        pkg_module = (file_modules.get(rel) or [None])[0]
        if not pkg_module:
            continue
        for base_module, nm in import_pairs[rel]:
            for origin in module_files.get(base_module, ()):
                reexports.setdefault((pkg_module, nm), set()).add(origin)

    defcount: Counter = Counter()
    for f in facts.values():
        for s in f["defined_symbols"]:
            defcount[s] += 1

    bfs_depth = _bfs_depths(test_set, file_imports, module_files, max_depth)

    return RepoIndex(sources, facts, file_imports, import_pairs, file_modules,
                     module_files, reexports, defcount, test_set, bfs_depth)


# --- scoring ---------------------------------------------------------------

# Evidence scores; for each target the strongest matching signal wins. Higher
# means a test more certainly exercises the file. See check_cov.md for rationale.
SCORE_DIRECT_IMPORT = 0.92   # test imports the target module or a symbol from it
SCORE_REEXPORT = 0.85        # test imports a symbol the package __init__ re-exports from target
SCORE_CONFTEST_USED = 0.70   # conftest imports target and a fixture it defines is used by a test
SCORE_CONFTEST = 0.60        # conftest imports target
SCORE_GRAPH_NEAR = 0.55      # target reachable from a test within 2 import hops
SCORE_FRAMEWORK = 0.55       # test exercises a route / CLI command the target defines
SCORE_GRAPH_FAR = 0.45       # target reachable from a test within max_depth hops
SCORE_STRING_REF = 0.45      # target module / path referenced as a string (dynamic import)
SCORE_NAME_MATCH = 0.40      # test filename corresponds to the target basename
SCORE_SYMBOL_USED = 0.35     # test references a distinctive symbol the target defines
SCORE_WEAK_STRING = 0.30     # distinctive symbol / target basename appears only as a string

_HTTP_DECORATORS = ("route", "get", "post", "put", "delete", "patch")
_CLI_INVOKERS = {"invoke", "CliRunner", "main"}


def score_target(target: str, index: RepoIndex) -> dict:
    """Score one security-patch file against the repo's tests; return a per-file
    coverage entry. The strongest evidence layer determines the score."""
    if target not in index.sources or not index.sources[target].strip():
        return _file_result(CoverageLabel.UNKNOWN, "low", "target not found at base_commit")

    target_modules = set(index.file_modules.get(target, []))
    facts = index.facts[target]
    base = _basename(target)[:-len(TARGET_EXTENSIONS[0])]
    # Distinctive symbols: long enough and defined at most SYMBOL_MAX_DEFCOUNT
    # times repo-wide. Uniqueness (not a stop-list) filters out common names.
    unique_syms = {
        s for s in facts["defined_symbols"]
        if len(s) >= SYMBOL_MIN_LEN and index.defcount[s] <= SYMBOL_MAX_DEFCOUNT
    }
    target_routes = facts["route_paths"]
    is_route_file = any(any(k in dec for k in _HTTP_DECORATORS) for dec in facts["decorators"])
    is_cli_file = any("command" in dec or "group" in dec for dec in facts["decorators"])

    score = 0.0
    evidence: list[str] = []

    def add(value, message):
        nonlocal score
        score = max(score, value)
        if message not in evidence:
            evidence.append(message)

    # Per-test-file evidence.
    for tf in index.test_set:
        tfacts = index.facts[tf]
        imports = index.file_imports[tf]

        # L1: test directly imports the target module / a symbol from it.
        if target_modules & imports:
            add(SCORE_DIRECT_IMPORT, f"test imports target module ({tf})")

        # L1b: test imports a symbol the package __init__ re-exports from target.
        for pkg, nm in index.import_pairs[tf]:
            if target in index.reexports.get((pkg, nm), ()):
                add(SCORE_REEXPORT, f"test imports re-exported '{nm}' from {pkg} ({tf})")

        # L3: test/source filename correspondence (incl. dir-combined names, e.g.
        # tests/test_auth_utils.py for auth/utils.py).
        tb = _basename(tf)[:-len(TARGET_EXTENSIONS[0])]
        if (tb in (f"test_{base}", f"{base}_test")
                or tb.replace("test_", "").replace("_test", "") == base
                or (len(base) >= 4 and base in tb.split("_"))):
            add(SCORE_NAME_MATCH, f"test name matches target basename ({tf})")

        # L4: test references a distinctive symbol the target defines.
        if unique_syms & tfacts["used_names"]:
            add(SCORE_SYMBOL_USED, f"test references target symbol(s) ({tf})")
        elif unique_syms & tfacts["strings"]:
            add(SCORE_WEAK_STRING, f"test mentions target symbol as string ({tf})")

        # L5: conftest imports target (stronger if a fixture it defines is used).
        if _basename(tf) == "conftest.py" and target_modules & imports:
            used = tfacts["defined_symbols"] and any(
                tfacts["defined_symbols"] & index.facts[other]["used_names"]
                for other in index.test_set if other != tf
            )
            add(SCORE_CONFTEST_USED if used else SCORE_CONFTEST, f"conftest imports target ({tf})")

        # L6: framework route correspondence (Flask/FastAPI/Django client paths).
        if is_route_file and target_routes and (target_routes & tfacts["route_paths"]):
            add(SCORE_FRAMEWORK, f"test exercises matching route path ({tf})")

        # L6b: CLI command invoked in a test (Click runner.invoke / argparse).
        if is_cli_file and (unique_syms & tfacts["used_names"]) and (_CLI_INVOKERS & tfacts["used_names"]):
            add(SCORE_FRAMEWORK, f"test invokes target CLI command ({tf})")

        # L7: target referenced as a string in the test itself (dynamic import).
        if target_modules & tfacts["strings"]:
            add(SCORE_STRING_REF, f"test references target module as string ({tf})")
        elif target in tfacts["strings"]:
            add(SCORE_STRING_REF, f"test references target path as string ({tf})")
        elif base in tfacts["strings"]:
            add(SCORE_WEAK_STRING, f"test mentions target basename as string ({tf})")

    # L2: indirect import-graph reachability — how far the nearest test reaches
    # the target. Depth 1 overlaps L1; depth 2 is solid indirect evidence.
    depth = index.bfs_depth.get(target)
    if depth is not None and depth >= 1:
        value = SCORE_GRAPH_NEAR if depth <= 2 else SCORE_GRAPH_FAR
        add(value, f"reachable from tests via import graph (depth {depth})")

    # L7b: target referenced as a string in a production file the tests can reach
    # within a few hops — catches dynamic / registry wiring with no static edge.
    for rel, fd in index.bfs_depth.items():
        if 1 <= fd <= DYNAMIC_WIRING_MAX_DEPTH and rel not in index.test_set \
                and target_modules & index.facts[rel]["strings"]:
            add(SCORE_STRING_REF, f"target module referenced as string in test-reachable {rel}")
            break

    label, confidence = _classify(score)
    reason = evidence[0] if evidence else "no evidence found"
    return _file_result(label, confidence, reason, score=round(score, 3), evidence=evidence[:6])


def _classify(score: float):
    """Map an evidence score to a (label, confidence) pair."""
    if score >= COV_LIKELY_THRESHOLD:
        return CoverageLabel.LIKELY, "high"
    if score >= COV_MAYBE_THRESHOLD:
        return CoverageLabel.MAYBE, "medium"
    if score > 0:
        return CoverageLabel.MAYBE, "low"
    return CoverageLabel.UNLIKELY, "medium"


# --- per-instance analysis -------------------------------------------------

def _aggregate(data_record, per_file, parse_failures, n_targets, reason=None) -> CoverageResult:
    """Roll per-file results up to an instance result (best file wins)."""
    files = list(per_file.values())
    if files:
        best = max(files, key=lambda v: (LABEL_RANK[v["label"]], v["score"]))
        label, confidence, score = best["label"], best["confidence"], best["score"]
    else:
        best, label, confidence, score = None, CoverageLabel.UNKNOWN, "low", 0.0
    return {
        "instance_id": data_record["instance_id"],
        "project": data_record["project"],
        "base_commit": data_record["base_commit"],
        "label": label,
        "confidence": confidence,
        "score": score,
        "per_file": per_file,
        "reason": reason or (best["reason"] if best else "no target-language files in patch"),
        "parse_failures": parse_failures,
        "n_target_files": n_targets,
    }


def _unknown_result(data_record, targets, reason) -> CoverageResult:
    """An all-unknown result (e.g. the repo could not be read)."""
    per_file = {t: _file_result(CoverageLabel.UNKNOWN, "low", reason) for t in targets}
    n_targets = len([t for t in targets if t.endswith(TARGET_EXTENSIONS)])
    return _aggregate(data_record, per_file, 0, n_targets, reason=reason)


def analyze(data_record, sources, max_depth) -> CoverageResult:
    """Classify coverage of a single instance's security-patch files against the
    in-memory repo snapshot."""
    touched = sorted(touched_files(data_record["security_patch"]))
    target_files = [t for t in touched if t.endswith(TARGET_EXTENSIONS)]
    other_targets = [t for t in touched if not t.endswith(TARGET_EXTENSIONS)]

    per_file = {t: _file_result(CoverageLabel.UNKNOWN, "low", "non-target-language file")
                for t in other_targets}

    if not target_files:
        return _aggregate(data_record, per_file, 0, 0, reason="no target-language files in patch")

    test_set = {rel for rel in sources if is_test_file(rel)}
    if not test_set:
        for t in target_files:
            per_file[t] = _file_result(CoverageLabel.UNLIKELY, "high", "no test suite detected")
        return _aggregate(data_record, per_file, 0, len(target_files), reason="no test suite detected")

    index = _build_index(sources, test_set, max_depth)
    parse_failures = sum(1 for f in index.facts.values() if f["parse_level"] in ("regex", "none"))

    for t in target_files:
        per_file[t] = score_target(t, index)

    # If most target files were unparseable and nothing scored, the absence of
    # evidence is unreliable — report unknown rather than unlikely.
    unparseable = sum(1 for t in target_files
                      if index.facts.get(t, {}).get("parse_level") in ("regex", "none"))
    if unparseable > len(target_files) * MAX_TARGET_PARSE_FAIL_RATIO \
            and all(per_file[t]["score"] == 0.0 for t in target_files):
        for t in target_files:
            per_file[t] = _file_result(CoverageLabel.UNKNOWN, "low", "target files unparseable")
        return _aggregate(data_record, per_file, parse_failures, len(target_files),
                          reason="target files unparseable")

    return _aggregate(data_record, per_file, parse_failures, len(target_files))


# --- orchestration ---------------------------------------------------------

def check_cov_single(data_record, log_dir, max_depth=IMPORT_GRAPH_MAX_DEPTH) -> CoverageResult:
    """Analyze one instance's file-level test coverage at base_commit."""
    instance_id = data_record["instance_id"]
    project = data_record["project"]
    base_commit = data_record["base_commit"]

    log_file = Path(log_dir) / instance_id / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking test coverage for {instance_id}...")

    repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)
    try:
        clone_github_repo(project, LOCAL_REPOS_DIR, force=False)
        with RepoLocks.locked(project):
            reset_to_commit(repo_dir, base_commit, new_branch=False)
            sources = _snapshot_sources(repo_dir)
    except Exception as e:
        logger.error(f"repo unavailable: {e}")
        targets = sorted(touched_files(data_record["security_patch"]))
        return _unknown_result(data_record, targets, f"repo unavailable: {e}")

    result = analyze(data_record, sources, max_depth)
    logger.info(f"{instance_id} -> {result['label']} (score {result['score']:.2f}): {result['reason']}")
    return result


def check_cov_threadpool(
    processed_dataset,
    max_workers,
    coverage_report_path,
    processed_dataset_path,
    log_dir,
    instance_ids=None,
    max_depth=IMPORT_GRAPH_MAX_DEPTH,
):
    """Analyze each instance, write the full coverage report, write a per-instance
    ``coverage`` summary back into processed_dataset.jsonl, and save a run summary."""
    records = processed_dataset
    if instance_ids is not None:
        wanted = set(instance_ids)
        records = [r for r in records if r["instance_id"] in wanted]

    results: list[CoverageResult] = []
    result_by_id: dict = {}
    label_counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_cov_single, record, log_dir, max_depth): record["instance_id"]
            for record in records
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
                  desc=f"Checking coverage [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {instance_id}: {e}")
                results.append(result)
                result_by_id[instance_id] = result
                label_counts[str(result["label"])] += 1
                pbar.update(1)
                pbar.set_description(", ".join(f"{k}={v}" for k, v in sorted(label_counts.items())))
                save_file(results, coverage_report_path)

    for record in processed_dataset:
        res = result_by_id.get(record["instance_id"])
        if res is not None:
            record["coverage"] = {
                "label": res["label"],
                "score": res["score"],
                "confidence": res["confidence"],
            }
    save_file(processed_dataset, processed_dataset_path)

    summary = get_cov_summary(results)
    summary_path = Path(log_dir) / LOG_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(summary, summary_path)
    print_cov_summary(summary)
    print(f"Coverage report saved to {coverage_report_path}.")
    print(f"Summary saved to {summary_path}.")
    return results


# --- run summary -----------------------------------------------------------

def get_cov_summary(results: list) -> dict:
    """Group instance ids by coverage label (mirrors validate.get_validate_summary)."""
    by_label: dict = {}
    for r in results:
        by_label.setdefault(str(r["label"]), []).append(r["instance_id"])
    return {
        "num_instances": len(results),
        "counts": {label: len(ids) for label, ids in by_label.items()},
        "details": {label: sorted(ids) for label, ids in by_label.items()},
    }


def print_cov_summary(summary: dict) -> None:
    print(f"Coverage ({summary['num_instances']} instances):")
    for label in (CoverageLabel.LIKELY, CoverageLabel.MAYBE,
                  CoverageLabel.UNLIKELY, CoverageLabel.UNKNOWN):
        ids = summary["details"].get(str(label))
        if not ids:
            continue
        print(f"  [{label}] ({len(ids)}):")
        for instance_id in ids:
            print(f"    {instance_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--run_id',
        type=str,
        default='default',
        help='Run ID locating datasets/<run_id>/processed_dataset.jsonl'
    )
    parser.add_argument(
        '--max_workers',
        type=int,
        default=5,
        help='Number of worker threads'
    )
    parser.add_argument(
        '--max_records',
        type=int,
        default=None,
        help='Maximum number of instances to analyze'
    )
    parser.add_argument(
        '--instance_ids',
        type=json.loads,
        default=None,
        help='JSON list of instance_ids to analyze (subset)'
    )
    parser.add_argument(
        '--max_depth',
        type=int,
        default=IMPORT_GRAPH_MAX_DEPTH,
        help='Max import-graph depth for indirect coverage evidence'
    )
    args = parser.parse_args()

    collect_log_dir = COLLECT_LOG_DIR / args.run_id
    processed_dataset_path = get_path('processed_dataset', args.run_id)
    coverage_report_path = get_path('coverage_report', args.run_id)
    coverage_report_path.parent.mkdir(parents=True, exist_ok=True)

    # Load the full dataset; it is always written back, so never truncate the list
    # itself — limit the analysis scope via instance_ids instead.
    processed_dataset = load_file(processed_dataset_path)
    analyze_ids = args.instance_ids
    if args.max_records is not None:
        head_ids = [r["instance_id"] for r in processed_dataset[:args.max_records]]
        analyze_ids = head_ids if analyze_ids is None else \
            [i for i in head_ids if i in set(analyze_ids)]

    check_cov_threadpool(
        processed_dataset,
        max_workers=args.max_workers,
        coverage_report_path=coverage_report_path,
        processed_dataset_path=processed_dataset_path,
        log_dir=collect_log_dir,
        instance_ids=analyze_ids,
        max_depth=args.max_depth,
    )
