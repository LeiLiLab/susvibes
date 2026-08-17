"""Thin Claude Agent SDK glue for the curate read-only agents (finder, inspect, sec_prop).

`run_agent` runs the SDK's `query()` to completion, streams the trajectory to a jsonl log as
each message arrives, and returns the schema-validated structured output — the SDK does the
JSON-schema validation + retry itself via `options.output_format`, so parsing/retry is not our
job. Callers build a full `ClaudeAgentOptions` (model, tools, allowed_tools, permission_mode, cwd,
output_format, …) and pass it through; nothing is hidden. This is deliberately not a Port/class,
which would duplicate and hide the SDK's own option surface.

Each stage caches its own per-item outcome through `core/report.py`; this module only runs the
agent and streams its trajectory, which is the evidence beside that report.

Bedrock config comes from `.env` (`CLAUDE_CODE_USE_BEDROCK=1`, `AWS_BEARER_TOKEN_BEDROCK`,
`AWS_REGION_NAME`); the model is a Bedrock inference profile, e.g. `us.anthropic.claude-sonnet-5`
(finder) or `us.anthropic.claude-haiku-4-5-20251001-v1:0` (inspect). sec_prop overrides this to the
direct Anthropic API, the only provider that serves WebSearch.
"""

import json
import time
import asyncio
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
)

READONLY_TOOLS = ["Bash", "Read", "Grep", "WebFetch", "WebSearch"]  # read-only investigation, no Write/Edit (WebSearch is silently dropped on Bedrock, harmless)

# Shared agent-subprocess hardening. `AGENT_ENV` is overlaid on os.environ (which carries the
# Bedrock creds), so it only adds ANTHROPIC_MAX_RETRIES — the CLI then honours retry-after on
# 429/5xx before surfacing an error. The 1 MB default read buffer overflows on large tool results
# (a big `git show` diff) and crashes the CLI subprocess, so bump it.
AGENT_ENV = {"ANTHROPIC_MAX_RETRIES": "10"}
MAX_BUFFER_SIZE = 10 * 1024 * 1024

# Substrings marking an agent-run failure that a repeat attempt may get past on its own — a
# transient API/network/subprocess hiccup — as opposed to a terminal one (max turns, a refusal).
# Distinct from `--resume`, which re-runs EVERY errored item later: this is the in-loop retry, and
# only a transient failure is worth one. Matched against str(exc); widen as new shapes surface.
TRANSIENT_ERRORS = (
    "Command failed with exit code",
    "rate_limit", "rate limit", "Rate limit", "429",
    "overloaded", "Overloaded", "503", "504",
    "Service Unavailable", "Gateway Timeout",
    "connection reset", "ECONNRESET", "ETIMEDOUT", "EAI_AGAIN",
    "APIConnectionError", "APITimeoutError",
)


AGENT_RETRIES = 2               # extra attempts on a transient failure (the CLI already retries 429/5xx)
AGENT_BACKOFF_SEC = 30          # exponential backoff base: 30s, 60s, 120s, ... per retry


def is_transient_error(error: str) -> bool:
    """Whether an agent run's failure is transient — a repeat attempt right now may get past it —
    rather than terminal (max turns, a refusal). Gates the in-loop retry only; `--resume` re-runs
    every errored item regardless, so do not read this as "retryable"."""
    return any(sub in error for sub in TRANSIENT_ERRORS)


def block_to_dict(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id,
                "content": block.content, "is_error": block.is_error}
    return {"type": type(block).__name__}


def message_to_dict(message) -> dict:
    if isinstance(message, AssistantMessage):
        return {"role": "assistant", "content": [block_to_dict(b) for b in message.content]}
    if isinstance(message, UserMessage):
        content = message.content
        if not isinstance(content, str):
            content = [block_to_dict(b) for b in content]
        return {"role": "user", "content": content}
    if isinstance(message, SystemMessage):
        return {"role": "system", "subtype": message.subtype, "data": message.data}
    if isinstance(message, ResultMessage):
        return {"role": "result", "is_error": message.is_error, "num_turns": message.num_turns,
                "cost_usd": message.total_cost_usd, "structured_output": message.structured_output}
    return {"role": type(message).__name__}


async def run_query(prompt: str, options: ClaudeAgentOptions, log_path):
    """Stream one agent run, returning `(output, meta)`. An aborted run raises AFTER the SDK has
    already yielded its ResultMessage — max turns is reported that way — so the failure is caught
    here and reported through `meta["error"]` instead: letting it propagate would throw away the
    `cost_usd` that run had genuinely spent, which is what used to make every cost figure in this
    repo an under-report of the abort rate."""
    trajectory = open(log_path, "w") if log_path else None
    result, error = None, None
    try:
        async for message in query(prompt=prompt, options=options):
            if trajectory:
                trajectory.write(json.dumps(message_to_dict(message), ensure_ascii=False, default=str) + "\n")
                trajectory.flush()
            if isinstance(message, ResultMessage):
                result = message
    except Exception as e:
        error = str(e)
    finally:
        if trajectory:
            trajectory.close()
    meta = {"cost_usd": result.total_cost_usd, "num_turns": result.num_turns} if result else {}
    if error or (result and result.is_error):
        meta["error"] = error or "agent returned an error result"
    output = result.structured_output if result and not result.is_error and not error else None
    return output, meta


def run_agent(prompt: str, options: ClaudeAgentOptions, *, log_path=None):
    """Run the agent to completion, streaming its trajectory to `log_path` (jsonl, one message per
    line, flushed live), and return `(output, meta)`: `output` is the schema-validated structured
    output `options.output_format` produced, None if the run produced no valid result; `meta`
    carries `cost_usd` / `num_turns` from the SDK's ResultMessage, plus `error` when the run
    aborted. It does not raise — an abort still costs money, so it is reported, not thrown."""
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_query(prompt, options, log_path))


def run_agent_retrying(prompt, options, *, log_path=None,
                       retries=AGENT_RETRIES, backoff=AGENT_BACKOFF_SEC):
    """`run_agent` with retries on a transient failure (exponential backoff). Returns `(output,
    meta)`: `output` is None on a terminal or retries-exhausted failure, `meta` carries
    `cost_usd`/`num_turns` — of the last attempt — plus an `error` string when the run failed.
    Shared by the agent stages; each per-item worker turns `output is None` into its own miss."""
    for attempt in range(retries + 1):
        output, meta = run_agent(prompt, options, log_path=log_path)
        error = meta.get("error")
        if not error or not is_transient_error(error) or attempt == retries:
            return output, meta
        time.sleep(backoff * (2 ** attempt))
