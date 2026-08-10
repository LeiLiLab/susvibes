"""The per-item report contract shared by every stage that runs something expensive per instance.

A stage whose per-item work can fail in a way worth recording writes one **report** per item — a
dict that is always produced, even when the work failed. Its raw evidence (container logs, an agent
trajectory) is written beside the report, never used as the cache itself: raw evidence cannot
express "ran and produced nothing" or "timed out", which is what makes an evidence-keyed cache
silently reuse a failure forever.

The contract is one invariant:

    `error` set  <=>  the harness failed to conclude, and nothing else in the report is meaningful.

Everything else a report carries is a *conclusion* — including conclusions like "the patch does not
apply", which cost real work to reach and must not be thrown away. That is what makes the reuse
ladder well-defined: a plain run reuses whatever is cached, `--resume` re-runs only the errored
ones, `--force` re-runs everything.

`get_report_summary` / `print_summary` read the same invariant, so every stage's run summary splits
the same way: what concluded (grouped by that stage's own status field) versus what errored and can
be re-run. Stages needing more (eval's pass ratios) build on the returned dict. `get_two_state_summary`
is the same summary for a batch that keeps no reports — a build, a push — where the only two states
are "it worked" and the message that stopped it.
"""

from pathlib import Path

from susvibes.core.utils import load_file, save_file


# Facts about the RUN, not about the item: what it cost and whether this call re-read a cached
# report. A stage projecting a report onto its record always strips them — a dataset has no
# business carrying either. `error` is NOT one of them: it is a conclusion, and a stage may well
# want the record to say "this errored" rather than leaving it indistinguishable from "never ran".
BOOKKEEPING_FIELDS = ("meta", "reused")


def strip_bookkeeping(report: dict, *, drop: tuple = ()) -> dict:
    """The report minus `BOOKKEEPING_FIELDS`, and minus whatever else the stage names in `drop` —
    what is left is fit to project onto a record."""
    return {key: value for key, value in report.items()
            if key not in BOOKKEEPING_FIELDS and key not in drop}


def is_errored(report: dict) -> bool:
    """Whether the harness failed to conclude for this item — the contract's one invariant, and the
    only thing `--resume` re-runs. Everything else a report holds is a conclusion."""
    return bool(report.get("error"))


def reuse_report(report_path, *, force=False, resume=False) -> dict | None:
    """The cached report to reuse for one item, or None when the item must be (re-)run: a plain run
    reuses whatever is cached (an errored report included, so a permanently failing item costs
    nothing to re-visit), `resume` re-runs only the errored ones, `force` re-runs everything.

    A reused report is marked `reused` — its `meta` describes the run that produced it, not this
    one, so a caller totalling what this run spent must skip it. The mark is added on the way out
    and never written back, so it exists only for the caller that asked."""
    report_path = Path(report_path)
    if force or not report_path.exists():
        return None
    report = load_file(report_path)
    report.setdefault("error", None)         # reports written before `error` joined the contract
    if resume and is_errored(report):
        return None
    report["reused"] = True
    return report


def save_report(report: dict, report_path) -> None:
    """Cache one item's report the moment it concludes — an errored one included, so the outcome
    survives a crash mid-batch either way."""
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(report, report_path)


def get_report_summary(reports: dict, status_field: str) -> dict:
    """Run-level summary of per-item reports, split on the contract: what concluded, grouped by
    `status_field` (the stage's own status, e.g. "eval_status"), versus what errored, grouped by
    the leading clause of the error message — free-form text, so a prefix is all there is to group
    on, and only the errored need grouping since they are what a re-run targets.

    Counts what this run actually re-ran (`num_reused`) but deliberately NOT what it spent: a stage
    can hold caches the report knows nothing about — validate reuses its logs_handler independently
    — so "not reused" does not mean "paid for". Each stage totals its own cost."""
    concluded, errored = {}, {}
    for instance_id, report in reports.items():
        if is_errored(report):
            message = report["error"]
            # A message with no colon is its own category, so drop the sentence-ending period
            # ("Empty model_patch." next to "Failed to build image") — the groups are labels.
            errored.setdefault(message.split(":", 1)[0].rstrip(" ."), {})[instance_id] = message
        else:
            concluded.setdefault(report.get(status_field), []).append(instance_id)
    return {
        "num_items": len(reports),
        "num_concluded": sum(len(ids) for ids in concluded.values()),
        "num_errored": sum(len(ids) for ids in errored.values()),
        "num_reused": sum(1 for report in reports.values() if report.get("reused")),
        "concluded": {status: sorted(ids) for status, ids in sorted(concluded.items(),
                                                                   key=lambda kv: str(kv[0]))},
        "errored": {category: dict(sorted(items.items())) for category, items in sorted(errored.items())},
    }


def get_two_state_summary(succeeded: list, errored: dict) -> dict:
    """A `get_report_summary` for a batch that keeps no reports, and so has only two states — a
    build, a push, a dump, where an item either succeeded or carries the message that stopped it.
    `succeeded` is the item ids, `errored` maps an item id to its message.

    Each message's leading clause becomes its group, so the text before the first `:` must be a
    FIXED description with nothing interpolated into it — `f"Failed to build image: {tag}: {e}"`,
    never `f"Failed to build {tag}: {e}"`, or a tag carrying a colon gives every item its own
    group."""
    reports = {item: {"error": None, "status": "succeeded"} for item in succeeded}
    reports.update({item: {"error": message} for item, message in errored.items()})
    return get_report_summary(reports, "status")


def print_summary(summary: dict) -> None:
    """Print a `get_report_summary` / `get_two_state_summary` result. Concluded items are counted per
    status — a run is thousands of ids nobody reads — while errored ones are listed, since those
    are what a re-run targets."""
    if summary["num_concluded"]:
        print(f"Concluded ({summary['num_concluded']}):")
        width = max(len(str(status)) for status in summary["concluded"])
        for status, ids in summary["concluded"].items():
            print(f"  [{status}]".ljust(width + 5) + f"{len(ids):6d}")
    if summary["num_errored"]:
        print(f"\nErrored ({summary['num_errored']}):")
        for category, items in summary["errored"].items():
            print(f"  [{category}] ({len(items)}):")
            for instance_id in items:
                print(f"    {instance_id}")
    reused = summary.get("num_reused", 0)
    if reused:
        # Trails both groups: a reused report may have concluded or errored.
        print(f"\n({reused} reused, {summary['num_items'] - reused} fresh)")
