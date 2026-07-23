"""Real-Docker integration tests for the harness container lifecycle.

These exercise the *actual* Docker plumbing — image extraction, container
start, in-container command execution, CLI setup, and cleanup — without any
LLM or tokens.  They are marked ``docker`` and are deselected by default
(see pyproject.toml ``addopts``); run them explicitly with:

    pytest -m docker

They are parametrized over every Docker CLI agent so each harness subclass
is exercised, even though the lifecycle itself is shared by
``DockerHarnessBase``.  Only the per-agent differences (workspace/container
naming, env-file sourcing) vary between them.

The test owns a default real susvibes eval image (carries ``/project`` and
bash), overridable via ``SUSVIBES_TEST_IMAGE``; the harness pulls it with
``--pull always`` each run.  Set ``ACR_REGISTRY_URL`` to pull from an ACR.
"""

from __future__ import annotations

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

AGENTS = ["claude_code", "gemini_cli"]

if not docker_available():
    pytest.skip("Docker not available", allow_module_level=True)

if not image_available(TEST_IMAGE):
    pytest.skip(f"Test image not available: {TEST_IMAGE}", allow_module_level=True)


def _make_harness(agent_dir: str, tmp_path):
    mod = import_agent_module(agent_dir)
    return mod, mod.DockerIntegration(
        TEST_IMAGE,
        container_work_dir=TEST_WORK_DIR,
        workspace_root=str(tmp_path),
        keep_workspace=False,
    )


@pytest.mark.parametrize("agent_dir", AGENTS)
def test_full_lifecycle(agent_dir, tmp_path):
    mod, harness = _make_harness(agent_dir, tmp_path)
    container_name = None
    workspace = None
    try:
        workspace = harness.setup_persistent_workspace()

        # Extraction produced a real, non-empty local workspace.
        assert workspace.exists()
        assert any(workspace.iterdir())
        # Container is named per-agent and actually running.
        container_name = harness.workspace_container
        assert container_name.startswith(f"{mod.DockerIntegration.name}_work_")
        assert container_exists(container_name)

        # A command really runs inside the container.
        result = harness.execute_in_container("echo harness-ok", env={"FOO": "bar"})
        assert result["success"] is True
        assert result["return_code"] == 0
        assert "harness-ok" in result["stdout"]

        # Env vars injected by the harness are visible within the command.
        env_result = harness.execute_in_container("echo value=$FOO", env={"FOO": "bar"})
        assert "value=bar" in env_result["stdout"]
    finally:
        harness.cleanup()
        # The upstream cleanup can leak the container (see
        # tests/test_docker_cleanup_leak.py); force-remove to avoid orphans.
        force_remove(container_name)


@pytest.mark.parametrize("agent_dir", AGENTS)
def test_setup_cli_env_runs_script(agent_dir, tmp_path, monkeypatch):
    mod, harness = _make_harness(agent_dir, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "setup-env.sh").write_text("#!/bin/bash\necho setup-complete\n")

    container_name = None
    try:
        harness.setup_persistent_workspace()
        container_name = harness.workspace_container
        result = harness.setup_cli_env("setup-env.sh")
        assert result["success"] is True
        assert "setup-complete" in result["stdout"]
    finally:
        harness.cleanup()
        force_remove(container_name)


def test_execute_reports_failure_on_bad_command(tmp_path):
    _mod, harness = _make_harness("claude_code", tmp_path)
    container_name = None
    try:
        harness.setup_persistent_workspace()
        container_name = harness.workspace_container
        result = harness.execute_in_container("exit 7")
        assert result["success"] is False
        assert result["return_code"] == 7
    finally:
        harness.cleanup()
        force_remove(container_name)
