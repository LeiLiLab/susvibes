"""Reproduction of a container-cleanup leak in DockerHarnessBase.cleanup().

Root cause
----------
The persistent container's PID 1 is ``tail -f /dev/null`` (see
``_start_persistent_container``).  A process running as PID 1 does not receive
default signal dispositions, so it *ignores SIGTERM*.  ``docker stop`` therefore
waits the full stop grace period (default 10s) before sending SIGKILL.

``cleanup()`` runs ``docker stop`` with ``subprocess.run(..., timeout=10)``,
which races that 10s grace: the subprocess timeout fires, the exception is
caught, and the subsequent ``docker rm`` is skipped -> the container is leaked.

This is pre-existing behavior in both upstream susvibes and the Endor fork.
It is faithfully preserved by ``DockerHarnessBase``; these opt-in tests
(``-m docker``, no LLM) are a concrete reproduction to hand to the susvibes
maintainers.

Suggested fixes (any one)
-------------------------
- ``docker stop -t <n>`` with ``n`` < the subprocess timeout, or
- a larger subprocess ``timeout`` than the stop grace, or
- ``docker rm -f`` (kill + remove in one call).
"""

from __future__ import annotations

import subprocess
import time

import pytest

from tests.agent_import import import_agent_module
from tests.docker_env import (
    TEST_IMAGE,
    TEST_WORK_DIR,
    container_exists,
    docker_available,
    force_remove,
    image_available,
)

pytestmark = pytest.mark.docker

if not docker_available():
    pytest.skip("Docker not available", allow_module_level=True)

if not image_available(TEST_IMAGE):
    pytest.skip(f"Test image not available: {TEST_IMAGE}", allow_module_level=True)


def _make_harness(tmp_path):
    mod = import_agent_module("claude_code")
    return mod.DockerIntegration(
        TEST_IMAGE,
        container_work_dir=TEST_WORK_DIR,
        workspace_root=str(tmp_path),
        keep_workspace=False,
    )


def test_pid1_tail_ignores_sigterm(tmp_path):
    """Root cause: `tail -f /dev/null` as PID 1 only dies on SIGKILL."""
    harness = _make_harness(tmp_path)
    container_name = None
    try:
        harness.setup_persistent_workspace()
        container_name = harness.workspace_container
        assert container_exists(container_name)

        # A responsive PID 1 would exit on SIGTERM in well under the grace.
        # This one ignores SIGTERM, so docker stop must wait the full grace
        # (here 3s) before SIGKILL.
        start = time.monotonic()
        subprocess.run(
            ["docker", "stop", "--time", "3", container_name],
            capture_output=True, timeout=30,
        )
        elapsed = time.monotonic() - start
        assert elapsed >= 2.5, (
            f"docker stop returned in {elapsed:.2f}s; expected it to wait the "
            "full grace, proving PID 1 ignored SIGTERM"
        )
    finally:
        force_remove(container_name)


def test_cleanup_leaks_container(tmp_path):
    """Defect: cleanup()'s `docker stop timeout=10` races the 10s grace, so
    `docker rm` is skipped and the container is leaked."""
    harness = _make_harness(tmp_path)
    container_name = None
    try:
        harness.setup_persistent_workspace()
        container_name = harness.workspace_container
        assert container_exists(container_name)

        # Exercise the real, unchanged cleanup path.
        harness.cleanup()

        # Bug reproduced: the container is still present after cleanup().
        assert container_exists(container_name), (
            "Expected the container to be leaked by cleanup() (docker rm skipped "
            "after docker stop timed out); if this now passes cleanly, the "
            "upstream cleanup bug may have been fixed."
        )
    finally:
        # Test-side teardown so we never leave an orphan behind.
        force_remove(container_name)
