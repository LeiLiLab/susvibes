"""Shared helpers for the opt-in, docker-marked tests.

The test owns a default real susvibes eval image (which carries ``/project``
and bash), overridable via ``SUSVIBES_TEST_IMAGE``.  The harness pulls it with
``--pull always`` each run; set ``ACR_REGISTRY_URL`` to pull from an ACR.
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_TEST_IMAGE = (
    "songwen6968/susvibes.x86_64."
    "eval_zopefoundation_restrictedpython_c8eca66ae49081f0016d2e1f094c3d72095ef531:latest"
)
TEST_IMAGE = os.environ.get("SUSVIBES_TEST_IMAGE", DEFAULT_TEST_IMAGE)
TEST_WORK_DIR = "/project"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except Exception:
        return False


def image_available(image: str) -> bool:
    """True if the image is local or pullable from its registry."""
    local = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True
    )
    if local.returncode == 0:
        return True
    remote = subprocess.run(
        ["docker", "manifest", "inspect", image], capture_output=True, timeout=60
    )
    return remote.returncode == 0


def container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return name in result.stdout.split()


def force_remove(name: str | None) -> None:
    if name:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
