#!/usr/bin/env python3
"""
Convert OpenHands eval `output.jsonl` into the standard format (OpenAI messages).

This converter targets OpenHands 0.54 output.

Output is the OpenAI / ms-swift `messages` format, written in a SPLIT layout
(individual OpenHands trajectories are too large to inline): a record's `messages`
field is a *path string* to a file holding the actual messages array.

    <output>/<stem>.trials.json   ->  [ {instance_id, model_patch, model_name_or_path,
                                         run_metadata, tools, messages: "messages/<id>.json"}, ... ]
    <output>/messages/<id>.json   ->  [ ...OpenAI chat messages... ]

`tools` is the OpenAI tool (function) list available to the agent — OpenHands stores it
verbatim on the system event (`history[0].args.tools`).

Messages are reconstructed faithfully from the OpenHands event `history`:
  - assistant turns come VERBATIM from tool_call_metadata.model_response
    (.choices[0].message) — the exact OpenAI assistant message the API returned;
  - tool results come from the observation events, paired by tool_call_id;
  - the system prompt and the initial user task come from the first two events;
  - non-tool observations (recall context / environment errors) are kept as
    user messages so nothing the model saw is dropped.

Completion is determined precisely (verified across runs): an instance is
`submitted` iff it has a `finish` action, which is equivalent to `error is None`.
The `error` string otherwise gives the exit category (max iterations / stuck / API).

The model name is read from each record's `metadata.llm_config.model`.

Usage:
    python convert.py --input_dir <run dir> [--output_dir DIR]
"""

import argparse
import json
import os
import sys
from datetime import datetime


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #

def _text(content):
    """Flatten an OpenHands content value (str | list-of-blocks | None) to str."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c.get("content"), str):
                    parts.append(c["content"])
                else:
                    parts.append(json.dumps(c, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def _clean_tool_calls(tool_calls):
    """Normalize model_response tool_calls to OpenAI shape (drop index, ensure str args)."""
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": tc.get("id"),
            "type": "function",
            "function": {"name": fn.get("name"), "arguments": args},
        })
    return out


def _assistant_from_response(model_response):
    """Build the assistant message verbatim from a model_response.choices[0].message."""
    msg = (model_response.get("choices") or [{}])[0].get("message", {}) or {}
    out = {"role": "assistant",
           "content": msg.get("content") if isinstance(msg.get("content"), str)
           else _text(msg.get("content"))}
    tcs = _clean_tool_calls(msg.get("tool_calls"))
    if tcs:
        out["tool_calls"] = tcs
    return out


def extract_tools(history):
    """The available OpenAI tools — OpenHands stores them on the system event verbatim."""
    for e in history:
        if e.get("action") == "system":
            tools = (e.get("args") or {}).get("tools")
            return tools if isinstance(tools, list) else []
    return []


def history_to_messages(history):
    """Reconstruct OpenAI messages from the OpenHands event history."""
    messages = []
    seen_responses = set()
    for e in history:
        src = e.get("source")
        action = e.get("action")
        obs = e.get("observation")
        args = e.get("args") or {}
        tcm = e.get("tool_call_metadata") or {}
        mr = tcm.get("model_response")

        # system prompt
        if action == "system":
            messages.append({"role": "system",
                             "content": _text(args.get("content") or e.get("message"))})
            continue

        # initial user task (and any later user message)
        if src == "user" and action == "message":
            messages.append({"role": "user",
                             "content": _text(args.get("content") or e.get("message"))})
            continue

        # assistant turn — verbatim from the model response (once per response)
        if action and action != "None" and obs is None and mr:
            rid = mr.get("id") or id(mr)
            if rid in seen_responses:
                continue  # an extra tool_call of the same assistant message
            seen_responses.add(rid)
            messages.append(_assistant_from_response(mr))
            continue

        # tool result / injected context observation
        if obs is not None:
            content = e.get("content")
            if content is None:
                content = e.get("message", "")
            tcid = tcm.get("tool_call_id")
            if tcid:
                messages.append({"role": "tool", "tool_call_id": tcid,
                                 "content": _text(content)})
            else:
                # recall context / environment error fed back to the model
                messages.append({"role": "user", "content": _text(content)})
            continue
        # recall/message requests without a model_response are skipped
    return messages


# --------------------------------------------------------------------------- #
# result
# --------------------------------------------------------------------------- #

def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _exit_status(error):
    if error is None:
        return "submitted"
    e = str(error)
    if "maximum iteration" in e:
        return "max_iterations"
    if "stuck in a loop" in e or "AgentStuckInLoopError" in e:
        return "stuck_in_loop"
    return "error"


def build_result(record, history, n_assistant):
    """Build the unified run metadata from OpenHands metrics + event history."""
    error = record.get("error")
    exit_status = _exit_status(error)
    submitted = error is None
    subtype = ("completed" if submitted
               else "error" if exit_status == "error" else "incomplete")

    metrics = record.get("metrics") or {}
    tok = metrics.get("accumulated_token_usage") or {}

    # tool execution wall-clock: sum(observation_ts - action_ts) paired by tool_call_id
    action_ts = {}
    for e in history:
        tcm = e.get("tool_call_metadata") or {}
        cid = tcm.get("tool_call_id")
        if e.get("action") and e.get("observation") is None and cid:
            t = _parse_ts(e.get("timestamp"))
            if t is not None:
                action_ts[cid] = t
    tool_time = 0.0
    for e in history:
        tcm = e.get("tool_call_metadata") or {}
        cid = tcm.get("tool_call_id")
        if e.get("observation") is not None and cid in action_ts:
            t = _parse_ts(e.get("timestamp"))
            if t is not None and t >= action_ts[cid]:
                tool_time += t - action_ts[cid]

    return {
        # unified core
        "subtype": subtype,
        "is_error": not submitted,
        "num_turns": n_assistant,
        "total_cost_usd": metrics.get("accumulated_cost"),
        # scaffold-specific extras
        "exit_status": exit_status,
        "tokens_sent": tok.get("prompt_tokens"),
        "tokens_received": tok.get("completion_tokens"),
        "tool_execution_time_s": round(tool_time, 3),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def resolve_input(path):
    """Accept the run dir (containing output.jsonl) or the output.jsonl itself."""
    if os.path.isdir(path):
        return os.path.join(path, "output.jsonl")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", required=True,
                    help="OpenHands run dir containing output.jsonl")
    ap.add_argument("--output_dir", default=None, help="output dir (default: the input dir)")
    args = ap.parse_args()

    in_path = resolve_input(args.input_dir)
    if not os.path.isfile(in_path):
        print(f"no output.jsonl at {in_path}")
        return 1
    out_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(os.path.abspath(in_path))
    msgs_dir = os.path.join(out_dir, "messages")
    os.makedirs(msgs_dir, exist_ok=True)
    stem = os.path.basename(out_dir.rstrip("/")) or "trials"

    trials, n_msgs, n_fail = [], 0, 0
    with open(in_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  ! line {line_num}: bad json ({e})")
                n_fail += 1
                continue

            instance_id = rec.get("instance_id", f"unknown_{line_num}")
            history = rec.get("history", []) or []
            model = (rec.get("metadata", {})
                     .get("llm_config", {}).get("model") or "unknown")
            model_patch = (rec.get("test_result") or {}).get("git_patch", "") or ""

            messages = history_to_messages(history)
            n_assistant = sum(1 for m in messages if m.get("role") == "assistant")
            result = build_result(rec, history, n_assistant)

            # write the per-instance messages file (split format)
            rel = f"messages/{instance_id}.json"
            with open(os.path.join(out_dir, rel), "w", encoding="utf-8") as mf:
                json.dump(messages, mf, ensure_ascii=False, indent=1)

            trials.append({
                "instance_id": instance_id,
                "model_patch": model_patch,
                "model_name_or_path": model,
                "run_metadata": result,
                "tools": extract_tools(history),
                "messages": rel,
            })
            n_msgs += len(messages)

    trials.sort(key=lambda x: x["instance_id"])
    out_path = os.path.join(out_dir, f"{stem}.trials.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(trials, f, ensure_ascii=False, indent=1)

    print(f"wrote {len(trials)} trials ({n_msgs} messages total)")
    print(f"  index : {out_path}")
    print(f"  splits: {msgs_dir}/<instance_id>.json")
    print(f"  parse failures: {n_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
