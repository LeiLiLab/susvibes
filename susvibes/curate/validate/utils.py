def get_validate_summary(succeeded: list, failed: dict) -> dict:
    by_category = {}
    for instance_id, reason in failed.items():
        category = reason.split(":", 1)[0] if reason else "(no reason)"
        by_category.setdefault(category, []).append(instance_id)
    total = len(succeeded) + len(failed)
    return {
        "num_candidates": total,
        "num_succeeded": len(succeeded),
        "num_failed": len(failed),
        "success_ratio": len(succeeded) / total if total else 0.0,
        "details": {
            "succeeded": sorted(succeeded),
            "failed": {cat: sorted(ids) for cat, ids in by_category.items()},
            "failure_reasons": dict(sorted(failed.items())),
        },
    }


def print_summary(summary: dict) -> None:
    if summary["num_succeeded"]:
        print(f"Succeeded ({summary['num_succeeded']}):")
        for instance_id in summary["details"]["succeeded"]:
            print(f"  {instance_id}")
    if summary["num_failed"]:
        print(f"\nFailed ({summary['num_failed']}):")
        for category, ids in summary["details"]["failed"].items():
            print(f"  [{category}] ({len(ids)}):")
            for instance_id in ids:
                print(f"    {instance_id}")
