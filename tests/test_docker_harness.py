"""Offline tests for DockerHarnessBase and the per-agent subclasses.

All Docker interaction is mocked (subprocess.run patched), so these tests
run without Docker, containers, or network access.  They verify that the
shared lifecycle builds the correct docker commands, names workspaces and
containers per-agent, and sources the right env files.
"""

from __future__ import annotations

from unittest import mock

import pytest

from evaluation_harness.base import DockerHarnessBase
from tests.agent_import import import_agent_module


class _FooHarness(DockerHarnessBase):
    name = "foo"
    env_source_files = [
        "/root/.foo_env",
        "/root/.nvm/nvm.sh",
        "/root/.bashrc",
    ]


def _fake_completed(stdout="out", stderr="err", returncode=0):
    return mock.MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


# ── setup_persistent_workspace / naming ───────────────────────────────


def test_setup_workspace_naming(tmp_path):
    harness = _FooHarness("img:latest", workspace_root=str(tmp_path))
    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        result = harness.setup_persistent_workspace()

    assert result == harness.local_workspace_dir
    assert harness.local_workspace_dir.name.startswith("foo_workspace_")
    assert harness.local_workspace_dir.exists()
    assert harness.workspace_container.startswith("foo_work_")


def test_extract_code_docker_commands(tmp_path):
    harness = _FooHarness("img:latest", workspace_root=str(tmp_path))
    harness.local_workspace_dir = tmp_path / "ws"
    harness.local_workspace_dir.mkdir()

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        harness._extract_code_from_image()

    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0][:4] == ["docker", "create", "--pull", "always"]
    assert "img:latest" in commands[0]
    assert commands[1][:2] == ["docker", "cp"]
    assert commands[-1][:2] == ["docker", "rm"]


def test_start_container_command(tmp_path):
    harness = _FooHarness("img:latest", workspace_root=str(tmp_path))
    harness.local_workspace_dir = tmp_path / "ws"

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        harness._start_persistent_container()

    cmd = run.call_args_list[0].args[0]
    assert cmd[:3] == ["docker", "run", "-d"]
    assert "--network" in cmd and "host" in cmd
    assert "-v" in cmd
    assert cmd[-3:] == ["tail", "-f", "/dev/null"]
    assert harness.workspace_container.startswith("foo_work_")


# ── execute_in_container ──────────────────────────────────────────────


def test_execute_requires_container():
    harness = _FooHarness("img:latest")
    with pytest.raises(RuntimeError, match="No persistent container"):
        harness.execute_in_container("echo hi")


def test_execute_builds_command_and_sources_env():
    harness = _FooHarness("img:latest")
    harness.workspace_container = "foo_work_123"

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed(stdout="hello", returncode=0)
        result = harness.execute_in_container("run-agent", env={"KEY": "val"})

    exec_cmd = run.call_args.args[0]
    assert exec_cmd[:2] == ["docker", "exec"]
    assert "foo_work_123" in exec_cmd
    assert exec_cmd[-3] == "bash"
    assert exec_cmd[-2] == "-c"

    full_command = exec_cmd[-1]
    assert "source /root/.foo_env" in full_command
    assert "source /root/.nvm/nvm.sh" in full_command
    assert "export KEY=val" in full_command
    assert full_command.rstrip().endswith("run-agent")

    assert result["success"] is True
    assert result["stdout"] == "hello"
    assert result["return_code"] == 0
    assert result["command"] == "run-agent"


def test_execute_timeout_returns_failure():
    import subprocess as real_subprocess

    harness = _FooHarness("img:latest")
    harness.workspace_container = "foo_work_123"

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.side_effect = real_subprocess.TimeoutExpired(cmd="x", timeout=3000)
        result = harness.execute_in_container("run-agent")

    assert result["success"] is False
    assert result["return_code"] == -1
    assert "timed out" in result["stderr"]


def test_env_source_block_lists_all_files():
    harness = _FooHarness("img:latest")
    block = harness._env_source_block()
    for path in harness.env_source_files:
        assert f"source {path}" in block


# ── cleanup ───────────────────────────────────────────────────────────


def test_cleanup_stops_and_removes_container(tmp_path):
    harness = _FooHarness("img:latest")
    harness.workspace_container = "foo_work_123"
    harness.local_workspace_dir = tmp_path / "ws"
    harness.local_workspace_dir.mkdir()
    harness.keep_workspace = True

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        harness.cleanup()

    verbs = [call.args[0][1] for call in run.call_args_list]
    assert "stop" in verbs
    assert "rm" in verbs
    assert harness.local_workspace_dir.exists()  # preserved


def test_cleanup_deletes_workspace_when_not_kept(tmp_path):
    harness = _FooHarness("img:latest")
    harness.local_workspace_dir = tmp_path / "ws"
    harness.local_workspace_dir.mkdir()
    harness.keep_workspace = False

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        harness.cleanup()

    assert not harness.local_workspace_dir.exists()


def test_context_manager_calls_cleanup():
    harness = _FooHarness("img:latest")
    with mock.patch.object(harness, "cleanup") as cleanup:
        with harness as h:
            assert h is harness
        cleanup.assert_called_once()


# ── per-agent subclasses ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_dir,expected_name,expected_env_file",
    [
        ("claude_code", "claude", "/root/.claude_env"),
        ("gemini_cli", "gemini", "/root/.gemini/.env"),
    ],
)
def test_agent_subclass_config(agent_dir, expected_name, expected_env_file):
    mod = import_agent_module(agent_dir)

    # The agent imports ``from base import DockerHarnessBase`` (top-level
    # module, matching the batch runner's sys.path convention), so compare
    # against the base class it actually imported rather than the
    # package-qualified evaluation_harness.base import.
    assert mod.DockerHarnessBase.__name__ == "DockerHarnessBase"
    assert issubclass(mod.DockerIntegration, mod.DockerHarnessBase)
    assert mod.DockerIntegration.name == expected_name
    assert mod.DockerIntegration.env_source_files[0] == expected_env_file
    # Re-exports the batch runner relies on:
    assert hasattr(mod, "USER_PROMPT_TEMPLATE")
    assert hasattr(mod, "ALLOWED_TOOLS")


@pytest.mark.parametrize("agent_dir", ["claude_code", "gemini_cli"])
def test_agent_workspace_prefix(agent_dir, tmp_path):
    mod = import_agent_module(agent_dir)
    harness = mod.DockerIntegration("img:latest", workspace_root=str(tmp_path))

    with mock.patch("evaluation_harness.base.subprocess.run") as run:
        run.return_value = _fake_completed()
        harness.setup_persistent_workspace()

    assert harness.local_workspace_dir.name.startswith(f"{mod.DockerIntegration.name}_workspace_")
    assert harness.workspace_container.startswith(f"{mod.DockerIntegration.name}_work_")
