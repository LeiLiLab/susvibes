import logging
import tempfile
from pathlib import Path

from susvibes.core.env import Deployment
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
