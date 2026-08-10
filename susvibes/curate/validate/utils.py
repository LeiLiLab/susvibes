import logging
import tempfile
from pathlib import Path

from susvibes.core.env import Deployment
from susvibes.curate.validate.constants import KEEP_STAGE, ValidateStatus
from susvibes.env_specs import WORKSPACE_DIR_NAME


def compose_clean_dockerfile_for_eval(
    source_image_name: str,
    workspace_dir: str = WORKSPACE_DIR_NAME,
) -> str:
    """Compose a Dockerfile that derives an evaluation image from source_image_name
    with all git history under /workspace_dir wiped and re-initialized to a single
    commit, so an evaluation agent cannot recover masked code from history. The
    source image's CMD and other config are inherited (not overridden)."""
    clean_cmds = " && ".join([
        f"cd /{workspace_dir}",
        "find . -name .git -exec rm -rf {} + 2>/dev/null; rm -f .gitmodules",
        "git init -q",
        "git add -A",
        "git -c user.email=clean@susvibes -c user.name=SusVibes commit -q -m 'Initial commit.'",
    ])
    return f"FROM {source_image_name}\nRUN {clean_cmds}\n"


def build_clean_eval_deployment(
    source_image_name: str,
    target_image_name: str,
    logger: logging.Logger,
) -> Deployment:
    """Build the evaluation image from source_image_name with git history wiped."""
    clean_dockerfile = compose_clean_dockerfile_for_eval(source_image_name)
    with tempfile.TemporaryDirectory() as tmpdir:
        return Deployment.from_build(
            logger=logger,
            context_path=Path(tmpdir),
            dockerfile=clean_dockerfile,
            image_name=target_image_name,
        )


def validate_report(status, **payload) -> dict:
    """A report for an instance that concluded: the status plus whatever that status carries — a
    `reason` when a check rejected it, the expected_pf / flags / image_name / logs_handler / stats
    the caller projects onto the record when it validated."""
    return {"validate_status": status, **payload, "error": None}


def validate_miss(error: str) -> dict:
    """A report for a run that concluded nothing — the harness failed to build or run a container.
    `error` set is what `--resume` re-runs. Mirrors `sec_prop_miss` / `check_cov_miss`."""
    return {"validate_status": None, "error": error}


def apply_validate_report(report: dict, data_record: dict, env_spec: dict, stats: dict) -> None:
    """Project one concluded report onto the artifacts that carry it: the record (what wrap_up
    publishes, plus this stage's `keep` verdict), the env_spec, and the run's stats (keyed by instance). The report is
    their cache — the dataset stays their home."""
    validated = report["validate_status"] == ValidateStatus.VALIDATED
    data_record.setdefault("keep", {})[KEEP_STAGE] = validated
    for key in ("expected_pf", "flags", "image_name"):
        if validated:
            data_record[key] = report[key]
        else:
            data_record.pop(key, None)      # a re-validation that now fails leaves nothing behind
    if validated:
        env_spec["logs_handler"] = report["logs_handler"]
        stats.setdefault(data_record["instance_id"], {}).update(report["stats"])
