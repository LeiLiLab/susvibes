"""Unit and integration tests for Kimi CLI Evaluation Harness."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tests.agent_import import import_agent_module


def test_kimi_prompts():
    """Verify prompts.py defines USER_PROMPT_TEMPLATE properly."""
    mod = import_agent_module("kimi_cli", "prompts")
    assert hasattr(mod, "USER_PROMPT_TEMPLATE")
    formatted = mod.USER_PROMPT_TEMPLATE.format(
        local_work_dir="/project",
        problem_statement="Fix the security vulnerability."
    )
    assert "/project" in formatted
    assert "Fix the security vulnerability." in formatted


def test_kimi_run_docker_structure():
    """Verify run_docker.py class structure, inheritance, and attributes."""
    mod = import_agent_module("kimi_cli", "run_docker")
    assert issubclass(mod.DockerIntegration, mod.DockerHarnessBase)
    assert mod.DockerIntegration.name == "kimi"
    assert "/root/.kimi/.env" in mod.DockerIntegration.env_source_files
    assert isinstance(mod.ALLOWED_TOOLS, list)
    assert "Bash" in mod.ALLOWED_TOOLS


def test_kimi_convert_flat_events(tmp_path):
    """Test convert.py with flat stream-json events."""
    mod = import_agent_module("kimi_cli", "convert")

    sample_stdout = "\n".join([
        json.dumps({"type": "init", "model": "kimi-k2-turbo-preview"}),
        json.dumps({"type": "message", "role": "assistant", "content": "I will inspect "}),
        json.dumps({"type": "message", "role": "assistant", "content": "the code."}),
        json.dumps({
            "type": "tool_use",
            "tool_id": "call_123",
            "tool_name": "Bash",
            "parameters": {"command": "ls -la"}
        }),
        json.dumps({
            "type": "tool_result",
            "tool_id": "call_123",
            "output": "README.md setup.py"
        }),
        json.dumps({"type": "message", "role": "assistant", "content": "The files are listed."}),
        json.dumps({"type": "result", "status": "completed", "finish_reason": "stop"}),
    ])

    events = mod.parse_events(sample_stdout)
    assert len(events) == 7

    messages = mod.events_to_messages(events)
    assert len(messages) == 3

    # Turn 1: Assistant message with concatenated content and tool_call
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "I will inspect the code."
    assert len(messages[0]["tool_calls"]) == 1
    assert messages[0]["tool_calls"][0]["id"] == "call_123"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "Bash"

    # Turn 2: Tool output
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_123"
    assert messages[1]["content"] == "README.md setup.py"

    # Turn 3: Assistant final response
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "The files are listed."

    problems = mod.verify(events, messages)
    assert len(problems) == 0

    model = mod.extract_model(events)
    assert model == "kimi-k2-turbo-preview"

    meta = mod.build_run_metadata(events, messages)
    assert meta["status"] == "completed"
    assert meta["total_messages"] == 3
    assert meta["total_tool_calls"] == 1


def test_kimi_convert_openai_delta_events(tmp_path):
    """Test convert.py with OpenAI-compatible streaming delta events."""
    mod = import_agent_module("kimi_cli", "convert")

    sample_stdout = "\n".join([
        json.dumps({
            "choices": [{
                "delta": {"role": "assistant", "content": "Starting "}
            }]
        }),
        json.dumps({
            "choices": [{
                "delta": {"content": "investigation."}
            }]
        }),
        json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "id": "call_abc",
                        "function": {"name": "Read", "arguments": '{"path": '}
                    }]
                }
            }]
        }),
        json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "id": "call_abc",
                        "function": {"arguments": '"main.py"}'}
                    }]
                }
            }]
        }),
        json.dumps({
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "def main(): pass"
        }),
    ])

    events = mod.parse_events(sample_stdout)
    messages = mod.events_to_messages(events)

    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "Starting investigation."
    assert len(messages[0]["tool_calls"]) == 1
    assert messages[0]["tool_calls"][0]["function"]["name"] == "Read"
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "main.py"}'

    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_abc"
    assert messages[1]["content"] == "def main(): pass"

    problems = mod.verify(events, messages)
    assert len(problems) == 0


def test_kimi_convert_end_to_end(tmp_path):
    """Test convert.py full pipeline from final_results.json to trials.json and message files."""
    mod = import_agent_module("kimi_cli", "convert")

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()

    raw_event = json.dumps({"type": "message", "role": "assistant", "content": "Fixed bug"})
    results = [
        {
            "instance_id": "psf__requests_12345",
            "model_name_or_path": "kimi-k2-turbo-preview",
            "model_patch": "diff --git a/file.py b/file.py\n+new line",
            "kimi_stdout": raw_event,
            "kimi_stderr": "",
            "kimi_success": True
        }
    ]

    with open(run_dir / "final_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f)

    with mock.patch("sys.argv", ["convert.py", "--input_dir", str(run_dir)]):
        ret = mod.main()
        assert ret == 0

    trials_file = run_dir / f"{run_dir.name}.trials.json"
    assert trials_file.exists()

    with open(trials_file, "r", encoding="utf-8") as f:
        trials = json.load(f)

    assert len(trials) == 1
    assert trials[0]["instance_id"] == "psf__requests_12345"
    assert trials[0]["model_name_or_path"] == "kimi-k2-turbo-preview"

    msg_file = run_dir / trials[0]["messages"]
    assert msg_file.exists()

    with open(msg_file, "r", encoding="utf-8") as f:
        msgs = json.load(f)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Fixed bug"


def test_kimi_normalize_prediction():
    """Verify that normalize_prediction properly handles kimi-prefixed result records."""
    from evaluation_harness.base import normalize_prediction

    record = {
        "instance_id": "psf__requests_123",
        "model_name_or_path": "kimi-k2-turbo-preview",
        "model_patch": "diff --git a/a b/b",
        "kimi_stdout": "kimi execution logs",
        "kimi_stderr": "",
        "kimi_success": True,
        "workspace": "/tmp/kimi_ws",
    }

    norm = normalize_prediction(record, "kimi")
    assert norm["instance_id"] == "psf__requests_123"
    assert norm["agent_stdout"] == "kimi execution logs"
    assert norm["agent_stderr"] == ""
    assert norm["agent_success"] is True
    assert norm["model_patch"] == "diff --git a/a b/b"
    assert norm["workspace"] == "/tmp/kimi_ws"


def test_kimi_batch_options():
    """Verify argument parsing in batch_run_docker.py."""
    mod = import_agent_module("kimi_cli", "batch_run_docker")
    parser = mod.get_options()
    args = parser.parse_args(["--model", "moonshot-v1-auto", "--num_instances", "5", "--strategy", "generic"])
    assert args.model == "moonshot-v1-auto"
    assert args.num_instances == 5
    assert args.strategy == "generic"


def test_kimi_parallel_batch_options():
    """Verify argument parsing in parallel_batch_run.py."""
    mod = import_agent_module("kimi_cli", "parallel_batch_run")
    parser = mod.get_options()
    args = parser.parse_args(["--num_processes", "8", "--model", "kimi-k2-turbo-preview"])
    assert args.num_processes == 8
    assert args.model == "kimi-k2-turbo-preview"
