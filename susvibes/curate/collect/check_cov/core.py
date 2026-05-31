"""Static file-level test-coverage analysis for collected CVE instances.

Runs after `process`. For each instance in ``processed_dataset.jsonl`` it decides,
at ``base_commit`` and by static analysis only (no execution), whether the repo's
own test suite covers the files touched by the ``security_patch`` — at the FILE
level: does any test in the repo reach any one of them.

Engines (see check_cov.md):
  - symbol_trace (jedi): precise backward symbol-reference trace; used when jedi
    can parse the repo (Python 3). Its hits carry the highest weight.
  - file_trace: ast->tree-sitter->regex approximation (L1/L1b/L2/L4); fallback
    when jedi cannot parse the repo (Python 2).
  - heuristics: L5-L7 (conftest, framework routes/CLI, dynamic-import strings);
    always run, because these edges are not symbol references.

The goal is to MINIMIZE false negatives: a file that really is tested must not be
marked uncovered.

Usage:
    python -m susvibes.curate.collect.check_cov --run_id <id> [--max_workers N]
"""
import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from pathlib import Path

from susvibes.curate.constants import LOCAL_REPOS_DIR, COLLECT_LOG_DIR, get_path
from susvibes.utils import load_file, save_file, touched_files, setup_instance_logger
from susvibes.curate.utils import (
    get_repo_dir,
    clone_github_repo,
    reset_to_commit,
    RepoLocks,
)
from susvibes.curate.collect.constants import TARGET_EXTENSIONS
from susvibes.curate.collect.check_cov.constants import (
    CoverageLabel, LABEL_RANK, CoverageResult, file_result, is_test_file,
    COV_LIKELY_THRESHOLD,
    COV_MAYBE_THRESHOLD,
    SYMBOL_TRACE_MAX_DEPTH,
    FILE_TRACE_MAX_PARSE_FAIL_RATIO,
)
from susvibes.curate.collect.check_cov import repo_index, file_trace, heuristics, symbol_trace

LOG_INSTANCE = "check_cov.log"
LOG_SUMMARY = "summary.json"


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
    if score >= COV_LIKELY_THRESHOLD:
        return CoverageLabel.LIKELY, "high"
    if score >= COV_MAYBE_THRESHOLD:
        return CoverageLabel.MAYBE, "medium"
    if score > 0:
        return CoverageLabel.MAYBE, "low"
    return CoverageLabel.UNLIKELY, "medium"


def _result_from_evidence(evidence):
    """evidence: list of (score, message). Strongest score wins."""
    if not evidence:
        return file_result(CoverageLabel.UNLIKELY, "medium", "no evidence found")
    evidence = sorted(evidence, key=lambda e: -e[0])
    score = evidence[0][0]
    label, confidence = _classify(score)
    msgs = [m for _, m in evidence][:6]
    return file_result(label, confidence, msgs[0], score=round(score, 3), evidence=msgs)


def _aggregate(data_record, per_file, engine, n_targets, reason=None) -> CoverageResult:
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
        "engine": engine,
        "n_target_files": n_targets,
    }


def analyze(data_record, repo_dir, sources, max_depth=SYMBOL_TRACE_MAX_DEPTH) -> CoverageResult:
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
            per_file[t] = file_result(CoverageLabel.UNLIKELY, "high", "no test suite detected")
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
            sym_evidence, reliable = symbol_trace.score(t, jctx, index, max_depth)  # precise
            evidence += sym_evidence
        else:
            evidence += file_trace.score(t, index)               # L1/L1b/L2/L4 approximation
        evidence += heuristics.score(t, index)                   # L5-L7 always
        if not evidence and not reliable:
            # Symbol trace could not complete (jedi unreliable, or target unparseable)
            # and nothing else scored: we cannot tell — unknown, not (false) unlikely.
            per_file[t] = file_result(CoverageLabel.UNKNOWN, "low",
                                      "symbol trace incomplete (jedi unreliable or target unparseable)")
        else:
            per_file[t] = _result_from_evidence(evidence)

    # File engine only: if most targets were unparseable and nothing scored, the
    # absence of evidence is unreliable — report unknown rather than unlikely. (The
    # symbol engine is precise: no reference chain genuinely means uncovered.)
    if not jedi_ok and _mostly_unparseable(targets, index) \
            and all(per_file[t]["score"] == 0.0 for t in targets):
        for t in targets:
            per_file[t] = file_result(CoverageLabel.UNKNOWN, "low", "target files unparseable")
        return _aggregate(data_record, per_file, engine, len(targets),
                          reason="target files unparseable")

    return _aggregate(data_record, per_file, engine, len(targets))


def _mostly_unparseable(targets, index) -> bool:
    fails = sum(1 for t in targets
                if index.facts.get(t, {}).get("parse_level") in ("regex", "none"))
    return fails > len(targets) * FILE_TRACE_MAX_PARSE_FAIL_RATIO


# --- orchestration ---------------------------------------------------------

def check_cov_single(data_record, log_dir, max_depth=SYMBOL_TRACE_MAX_DEPTH) -> CoverageResult:
    """Analyze one instance's file-level test coverage at base_commit."""
    instance_id = data_record["instance_id"]
    project = data_record["project"]
    base_commit = data_record["base_commit"]

    log_file = Path(log_dir) / instance_id / LOG_INSTANCE
    logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking test coverage for {instance_id}...")

    repo_dir = get_repo_dir(project, LOCAL_REPOS_DIR)
    clone_github_repo(project, LOCAL_REPOS_DIR, force=False)
    with RepoLocks.locked(project):
        reset_to_commit(repo_dir, base_commit, new_branch=False)
        sources = _snapshot_sources(repo_dir)
        result = analyze(data_record, repo_dir, sources, max_depth)

    logger.info(f"{instance_id} -> {result['label']} (score {result['score']:.2f}, "
                f"{result['engine']}): {result['reason']}")
    return result


def check_cov_threadpool(processed_dataset, max_workers, coverage_report_path,
                         processed_dataset_path, log_dir, instance_ids=None,
                         max_depth=SYMBOL_TRACE_MAX_DEPTH):
    """Analyze each instance, write the full coverage report, write a per-instance
    ``coverage`` summary back into processed_dataset.jsonl, and save a run summary."""
    records = processed_dataset
    if instance_ids is not None:
        wanted = set(instance_ids)
        records = [r for r in records if r["instance_id"] in wanted]

    results, result_by_id, label_counts = [], {}, Counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_cov_single, r, log_dir, max_depth): r["instance_id"]
                   for r in records}
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
            record["coverage"] = {"label": res["label"], "score": res["score"],
                                  "confidence": res["confidence"], "engine": res["engine"]}
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
    parser.add_argument('--max_workers', type=int, default=5, help='Number of worker threads')
    parser.add_argument('--max_records', type=int, default=None,
                        help='Maximum number of instances to analyze')
    parser.add_argument('--instance_ids', type=json.loads, default=None,
                        help='JSON list of instance_ids to analyze (subset)')
    parser.add_argument('--max_depth', type=int, default=SYMBOL_TRACE_MAX_DEPTH,
                        help='Max symbol-trace hops for indirect coverage evidence')
    args = parser.parse_args()

    collect_log_dir = COLLECT_LOG_DIR / args.run_id
    processed_dataset_path = get_path('processed_dataset', args.run_id)
    coverage_report_path = get_path('coverage_report', args.run_id)
    coverage_report_path.parent.mkdir(parents=True, exist_ok=True)

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


if __name__ == "__main__":
    main()
