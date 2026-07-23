"""
Test parallel_batch_run.py with mocked agent predictions.

The agent call (claude/gemini CLI) is replaced by a simple shell command
that modifies a file in the container, so we get a real git diff without
calling any LLM. This validates:
  - Docker image pull + workspace extraction
  - Parallel subprocess spawning + temp JSONL sharding
  - Container lifecycle (start, exec, cleanup) across multiple processes
  - Diff extraction + result file creation per process
"""

import json
import os
import re
import subprocess
import shutil
from pathlib import Path

import pytest

TEST_INSTANCES = Path(__file__).resolve().parent / "e2e" / "test_instances.jsonl"
HARNESS_DIR = Path(__file__).resolve().parent.parent / "evaluation_harness" / "claude_code"

# The mock agent command: modify an existing tracked file to produce a git diff.
# We use setup.py / setup.cfg which exist in all test images (Django, Bottle, lshell).
MOCK_AGENT_CMD = "echo MOCK_PATCH >> /project/setup.py || echo MOCK_PATCH >> /project/setup.cfg"


@pytest.fixture
def mock_harness_dir(tmp_path):
    """
    Build a self-contained harness directory with a patched batch_run_docker.py
    that replaces the agent CLI call with a trivial file mutation.
    """
    harness_dir = tmp_path / "harness"

    # Copy the entire claude_code harness directory (preserves all imports)
    shutil.copytree(HARNESS_DIR, harness_dir)

    # Also copy common.py and base.py from the parent (evaluation_harness/)
    for name in ["common.py", "base.py", "__init__.py"]:
        src = HARNESS_DIR.parent / name
        if src.exists():
            shutil.copy2(src, harness_dir / name)

    # Patch batch_run_docker.py: replace the agent command block with mock
    original = (harness_dir / "batch_run_docker.py").read_text()

    # Replace the multi-line claude_command assignment (ends with lone ')')
    patched = re.sub(
        r'claude_command = \(.*?^\s*\)',
        f'claude_command = "{MOCK_AGENT_CMD}"',
        original,
        count=1,
        flags=re.DOTALL | re.MULTILINE,
    )

    # Skip the setup-env.sh execution to speed things up
    patched = re.sub(
        r'setup_result = integration\.setup_cli_env\(.*?\)',
        'setup_result = {"success": True, "stdout": "", "stderr": ""}',
        patched,
        count=1,
        flags=re.DOTALL,
    )

    (harness_dir / "batch_run_docker.py").write_text(patched)

    return harness_dir


@pytest.fixture
def two_instance_jsonl(tmp_path):
    """Extract the first 2 instances from test_instances.jsonl."""
    instances = []
    with open(TEST_INSTANCES) as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(line)
            if len(instances) >= 2:
                break

    out = tmp_path / "test_2.jsonl"
    out.write_text("\n".join(instances) + "\n")
    return str(out)


@pytest.mark.docker
class TestParallelBatch:
    """Integration tests for parallel_batch_run using mocked agent predictions."""

    def test_parallel_sharding_produces_results(
        self, mock_harness_dir, two_instance_jsonl, tmp_path
    ):
        """
        Run parallel_batch_run with 2 processes on 2 instances.
        Each subprocess uses the mocked batch_run_docker that applies a
        trivial file change instead of calling the LLM.
        Verify: both processes succeed, result files are created with patches.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        env = os.environ.copy()
        # Parent dir for evaluation_harness imports (base.py, common.py)
        env["PYTHONPATH"] = (
            str(HARNESS_DIR.parent) + ":" +
            str(HARNESS_DIR.parent.parent) + ":" +
            env.get("PYTHONPATH", "")
        )
        # Dummy keys (agent won't actually be called)
        env.setdefault("ANTHROPIC_API_KEY", "sk-mock-not-used")
        env.setdefault("ANTHROPIC_MODEL", "mock-model")

        cmd = [
            "python3", "parallel_batch_run.py",
            "--jsonl_file", two_instance_jsonl,
            "--num_instances", "2",
            "--num_processes", "2",
            "--results_dir", str(results_dir),
            "--model", "mock-model",
            "--workspace_root", str(tmp_path / "workspace"),
            "--keep_workspace",
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(mock_harness_dir),
            env=env,
            timeout=600,
        )

        if result.returncode != 0:
            print("STDOUT (last 3000):", result.stdout[-3000:])
            print("STDERR (last 3000):", result.stderr[-3000:])

        assert result.returncode == 0, (
            f"parallel_batch_run failed (rc={result.returncode}): "
            f"{result.stderr[-500:]}"
        )

        # Collect all final_results.json from any subdirectory
        result_files = list(results_dir.rglob("final_results.json"))
        assert len(result_files) >= 1, (
            f"No final_results.json found in {results_dir}. "
            f"Contents: {list(results_dir.rglob('*'))}"
        )

        all_entries = []
        for rf in result_files:
            data = json.loads(rf.read_text())
            entries = data if isinstance(data, list) else [data]
            all_entries.extend(entries)

        assert len(all_entries) == 2, (
            f"Expected 2 result entries, got {len(all_entries)}"
        )

        for entry in all_entries:
            patch = entry.get("model_patch", "")
            assert patch.strip(), (
                f"Empty model_patch for {entry.get('instance_id')}"
            )
            assert "MOCK_PATCH" in patch or "mock" in patch.lower(), (
                f"Mock patch marker missing in diff for {entry.get('instance_id')}. "
                f"Patch: {patch[:200]}"
            )
