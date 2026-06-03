"""Static file-level test-coverage analysis for collected CVE instances.

Runs after `process`. For each instance in ``processed_dataset.jsonl`` it decides,
at ``base_commit`` and by static analysis only (no execution), whether the repo's
own test suite covers the files touched by the ``security_patch`` — at the FILE
level: does any test in the repo reach any one of them.

Engines (see check_cov.md); each scoring rule has an ID — S* (symbol_trace),
F* (file_trace), H* (heuristics) — emitted in the evidence message:
  - symbol_trace (jedi): precise backward symbol-reference trace (S1-S4); used when
    jedi can parse the repo (Python 3). Its hits carry the highest weight.
  - file_trace: ast->tree-sitter->regex approximation (F1-F6); fallback when jedi
    cannot parse the repo (Python 2).
  - heuristics: H1-H5 (conftest, framework routes/CLI, dynamic-import strings);
    always run, because these edges are not symbol references.

The goal is to MINIMIZE false negatives: a file that really is tested must not be
marked uncovered.

Usage:
    python -m susvibes.curate.mine.check_cov --run_id <id> [--max_workers N]
"""
import argparse
import json
from collections import Counter
import fcntl
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from tqdm import tqdm
from pathlib import Path

from susvibes.curate.constants import LOCAL_REPOS_DIR, get_log_dir, get_path
from susvibes.utils import load_file, save_file, touched_files, setup_instance_logger
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
)
from susvibes.curate.mine.constants import TARGET_EXTENSIONS
from susvibes.curate.mine.check_cov.constants import (
    CoverageLabel, LABEL_RANK, CoverageResult, file_result, is_test_file,
    Classifier, SymbolTrace, FileTrace,
)
from susvibes.curate.mine.check_cov import repo_index, file_trace, heuristics, symbol_trace

LOG_INSTANCE = "check_cov.log"
LOG_SUMMARY = "summary.json"


# --- cross-process per-repo lock -------------------------------------------
# Each instance runs in its own fresh process (see check_cov_processpool) so jedi
# state never degrades across instances. That makes the in-process RepoLocks
# useless for serializing concurrent resets of a shared clone, so two instances of
# the SAME repo (e.g. web2py x2) could `reset_to_commit` it at once. A file lock
# keyed by repo name (matching get_repo_dir) serializes them; distinct repos never
# contend, so cross-repo parallelism is unaffected.
_REPO_LOCK_DIR = Path(tempfile.gettempdir()) / "check_cov_repo_locks"


@contextmanager
def _repo_lock(project):
    name = project.split("/", 1)[1] if "/" in project else project
    _REPO_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = _REPO_LOCK_DIR / f"{name.replace('/', '_')}.lock"
    with open(lockfile, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- repo snapshot ---------------------------------------------------------

def _read_file_raw(path: Path) -> str:
    """Read a repo file as raw text, tolerating odd encodings (one bad file must
    not abort the scan)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _snapshot_sources(repo_dir: Path) -> dict:
    """Return {rel_path: source} for every target-language file under repo_dir,
    read while holding the repo lock right after reset_to_commit."""
    repo_dir = Path(repo_dir)
    sources = {}
    for path in repo_dir.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if path.suffix in TARGET_EXTENSIONS:
            sources[path.relative_to(repo_dir).as_posix()] = _read_file_raw(path)
    return sources


# --- scoring / classification ----------------------------------------------

def _classify(score: float):
    if score >= Classifier.LIKELY_THRESHOLD:
        return CoverageLabel.LIKELY
    if score > 0:
        return CoverageLabel.MAYBE
    return CoverageLabel.UNLIKELY


def _result_from_evidence(evidence):
    """evidence: list of (score, message). Strongest score wins."""
    if not evidence:
        return file_result(CoverageLabel.UNLIKELY, "no evidence found")
    evidence = sorted(evidence, key=lambda e: -e[0])
    score = evidence[0][0]
    label = _classify(score)
    msgs = [m for _, m in evidence][:6]
    return file_result(label, msgs[0], score=round(score, 3), evidence=msgs)


def _aggregate(data_record, per_file, engine, n_targets, reason=None) -> CoverageResult:
    files = list(per_file.values())
    if files:
        best = max(files, key=lambda v: (LABEL_RANK[v["label"]], v["score"]))
        label, score = best["label"], best["score"]
    else:
        best, label, score = None, CoverageLabel.UNKNOWN, 0.0
    return {
        "instance_id": data_record["instance_id"],
        "project": data_record["project"],
        "base_commit": data_record["base_commit"],
        "label": label,
        "score": score,
        "per_file": per_file,
        "reason": reason or (best["reason"] if best else "no target-language files in patch"),
        "engine": engine,
        "n_target_files": n_targets,
    }


def analyze(data_record, repo_dir, sources, max_depth=SymbolTrace.MAX_DEPTH) -> CoverageResult:
    """Classify coverage of an instance's security-patch files against the snapshot."""
    # process.py guarantees the security_patch is non-empty and all target-language
    # (.py): non-code and non-target-language files are split into the test_patch,
    # and an instance with an empty or non-target code patch is dropped. A patch
    # with no .py target here means that upstream invariant was violated.
    targets = sorted(touched_files(data_record["security_patch"]))
    if not targets or any(not t.endswith(TARGET_EXTENSIONS) for t in targets):
        raise ValueError(
            f"{data_record['instance_id']}: security_patch must be all .py files, got {targets}")

    per_file = {}
    test_set = {rel for rel in sources if is_test_file(rel)}
    if not test_set:
        for t in targets:
            per_file[t] = file_result(CoverageLabel.UNLIKELY, "no test suite detected")
        return _aggregate(data_record, per_file, "none", len(targets), reason="no test suite detected")

    # The index (parse + maps) is always needed: heuristics consume it, and it is
    # the file-level fallback when jedi is unusable.
    index = repo_index.build(sources, test_set)
    jedi_ok = symbol_trace.usable(repo_dir, sources)
    jctx = symbol_trace.build(repo_dir, sources, test_set) if jedi_ok else None
    engine = "symbol" if jedi_ok else "file"

    for t in targets:
        evidence, reliable = [], True
        if jedi_ok:
            sym_evidence, reliable = symbol_trace.score(t, jctx, index, max_depth)  # S1-S4 precise
            evidence += sym_evidence
        else:
            evidence += file_trace.score(t, index)               # F1-F6 approximation
        evidence += heuristics.score(t, index)                   # H1-H5 always
        if not evidence and not reliable:
            # Symbol trace could not complete (jedi unreliable, or target unparseable)
            # and nothing else scored: we cannot tell — unknown, not (false) unlikely.
            per_file[t] = file_result(CoverageLabel.UNKNOWN,
                                      "symbol trace incomplete (jedi unreliable or target unparseable)")
        else:
            per_file[t] = _result_from_evidence(evidence)

    # File engine only: if most targets were unparseable and nothing scored, the
    # absence of evidence is unreliable — report unknown rather than unlikely. (The
    # symbol engine is precise: no reference chain genuinely means uncovered.)
    if not jedi_ok and _mostly_unparseable(targets, index) \
            and all(per_file[t]["score"] == 0.0 for t in targets):
        for t in targets:
            per_file[t] = file_result(CoverageLabel.UNKNOWN, "target files unparseable")
        return _aggregate(data_record, per_file, engine, len(targets),
                          reason="target files unparseable")

    return _aggregate(data_record, per_file, engine, len(targets))


def _mostly_unparseable(targets, index) -> bool:
    fails = sum(1 for t in targets
                if index.facts.get(t, {}).get("parse_level") in ("regex", "none"))
    return fails > len(targets) * FileTrace.MAX_PARSE_FAIL_RATIO


# --- orchestration ---------------------------------------------------------

def check_cov_single(data_record, log_dir, max_depth=SymbolTrace.MAX_DEPTH) -> CoverageResult:
    """Analyze one instance's file-level test coverage at base_commit."""
    instance_id = data_record["instance_id"]
    project = data_record["project"]
    base_commit = data_record["base_commit"]

    log_file = Path(log_dir) / instance_id / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking test coverage for {instance_id}...")

    repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)
    with _repo_lock(project):
        clone_github_repo(project, LOCAL_REPOS_DIR, force=False)
        reset_to_commit(repo_dir, base_commit, new_branch=False)
        sources = _snapshot_sources(repo_dir)
        result = analyze(data_record, repo_dir, sources, max_depth)

    logger.info(f"{instance_id} -> {result['label']} (score {result['score']:.2f}, "
                f"{result['engine']}): {result['reason']}")
    return result


def check_cov_processpool(processed_dataset, max_workers, coverage_report_path,
                          processed_dataset_path, log_dir, instance_ids=None,
                          max_depth=SymbolTrace.MAX_DEPTH):
    """Analyze each instance, write the full coverage report, write a per-instance
    ``coverage`` summary back into processed_dataset.jsonl, and save a run summary.

    Each instance runs in its OWN fresh worker process (``max_tasks_per_child=1``):
    jedi's inference state degrades across instances within a long-lived process
    (its compiled subprocess gets poisoned), silently making later instances'
    ``get_references`` fail or return empty — a reused worker would then mislabel
    them unknown/uncovered. A fresh process per instance keeps jedi pristine;
    processes also don't share jedi's non-thread-safe state, so ``max_workers`` > 1
    is safe (and parallel)."""
    records = processed_dataset
    if instance_ids is not None:
        wanted = set(instance_ids)
        records = [r for r in records if r["instance_id"] in wanted]

    results, result_by_id, label_counts = [], {}, Counter()
    with ProcessPoolExecutor(max_workers=max_workers, max_tasks_per_child=1) as executor:
        futures = {executor.submit(check_cov_single, r, log_dir, max_depth): r["instance_id"]
                   for r in records}
        with tqdm(total=len(futures), dynamic_ncols=True,
                  desc=f"Checking coverage [{max_workers} procs]") as pbar:
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
            record["coverage"] = {"label": res["label"], "score": res["score"],
                                  "engine": res["engine"]}
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
    by_label: dict = {}
    for r in results:
        by_label.setdefault(str(r["label"]), []).append(r["instance_id"])
    return {
        "num_instances": len(results),
        "counts": {label: len(ids) for label, ids in by_label.items()},
        "engines": dict(Counter(r.get("engine", "?") for r in results)),
        "details": {label: sorted(ids) for label, ids in by_label.items()},
    }


def print_cov_summary(summary: dict) -> None:
    print(f"Coverage ({summary['num_instances']} instances, engines={summary['engines']}):")
    for label in (CoverageLabel.LIKELY, CoverageLabel.MAYBE,
                  CoverageLabel.UNLIKELY, CoverageLabel.UNKNOWN):
        ids = summary["details"].get(str(label))
        if not ids:
            continue
        print(f"  [{label}] ({len(ids)}):")
        for instance_id in ids:
            print(f"    {instance_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run_id', type=str, default='default',
                        help='Run ID locating datasets/<run_id>/processed_dataset.jsonl')
    parser.add_argument('--max_workers', type=int, default=5,
                        help='Number of worker processes (each instance runs in a fresh process)')
    parser.add_argument('--max_records', type=int, default=None,
                        help='Maximum number of instances to analyze')
    parser.add_argument('--instance_ids', type=json.loads, default=None,
                        help='JSON list of instance_ids to analyze (subset)')
    parser.add_argument('--max_depth', type=int, default=SymbolTrace.MAX_DEPTH,
                        help='Max symbol-trace hops for indirect coverage evidence')
    args = parser.parse_args()

    mine_log_dir = get_log_dir(args.run_id, "mine", "check_cov")
    processed_dataset_path = get_path('processed_dataset', args.run_id)
    coverage_report_path = get_path('coverage_report', args.run_id)
    coverage_report_path.parent.mkdir(parents=True, exist_ok=True)

    processed_dataset = load_file(processed_dataset_path)
    analyze_ids = args.instance_ids
    if args.max_records is not None:
        head_ids = [r["instance_id"] for r in processed_dataset[:args.max_records]]
        analyze_ids = head_ids if analyze_ids is None else \
            [i for i in head_ids if i in set(analyze_ids)]

    check_cov_processpool(
        processed_dataset,
        max_workers=args.max_workers,
        coverage_report_path=coverage_report_path,
        processed_dataset_path=processed_dataset_path,
        log_dir=mine_log_dir,
        instance_ids=analyze_ids,
        max_depth=args.max_depth,
    )


if __name__ == "__main__":
    main()
