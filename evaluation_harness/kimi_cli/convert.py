#!/usr/bin/env python3
"""
Convert kimi-cli harness output into the standard format (OpenAI messages).

Input is the harness `final_results.json` (a JSON list of per-instance records); the
trajectory lives on each record as `kimi_stdout` — the raw `kimi --output-format
stream-json` text, one JSON event per line.

Output is the OpenAI / ms-swift `messages` format, written in a SPLIT layout
(trajectories are too large to inline): a record's `messages` field is a *path string*
to a file holding the actual messages array.

    <output>/<stem>.trials.json   ->  [ {instance_id, model_patch, model_name_or_path,
                                         run_metadata, tools, messages: "messages/<id>.json"}, ... ]
    <output>/messages/<id>.json   ->  [ ...OpenAI chat messages... ]

Usage:
    python convert.py --input_dir <run dir> [--output_dir DIR]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

def parse_events(stdout: str) -> List[Dict[str, Any]]:
    """Parse a raw stream-json string into a list of event dicts (skipping bad lines)."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _text(value: Any) -> str:
    """Flatten a content/output value (str | list-of-blocks | None) into a plain string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for c in value:
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
    return json.dumps(value, ensure_ascii=False)


def events_to_messages(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reconstruct OpenAI messages from kimi-cli's events.

    Supports both flat event streams (type: message / tool_use / tool_result)
    and OpenAI-compatible delta streams (choices[0].delta).
    """
    messages: List[Dict[str, Any]] = []
    seen_call_ids = set()

    for ev in events:
        etype = ev.get("type")

        # 1. Flat message events
        if etype == "message":
            role = ev.get("role") or "user"
            if role not in ("user", "assistant", "system"):
                role = "user"
            text = _text(ev.get("content"))
            last = messages[-1] if messages else None
            if last is not None and last.get("role") == role and "tool_calls" not in last:
                last["content"] += text
            else:
                messages.append({"role": role, "content": text})

        elif etype == "tool_use":
            tool_id = str(ev.get("tool_id", f"call_{len(seen_call_ids)}"))
            seen_call_ids.add(tool_id)
            tool_call = {
                "id": tool_id,
                "type": "function",
                "function": {
                    "name": ev.get("tool_name", "unknown_tool"),
                    "arguments": json.dumps(ev.get("parameters", {}), ensure_ascii=False)
                    if isinstance(ev.get("parameters"), (dict, list))
                    else str(ev.get("parameters", "{}")),
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})

        elif etype == "tool_result":
            tool_id = str(ev.get("tool_id", ""))
            text = _text(ev.get("output", ev.get("content", "")))
            if tool_id in seen_call_ids:
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": text})
            else:
                messages.append({"role": "user", "content": text})

        # 2. OpenAI / Delta streaming events
        elif "choices" in ev and isinstance(ev["choices"], list) and ev["choices"]:
            choice = ev["choices"][0]
            delta = choice.get("delta") or choice.get("message") or {}
            role = delta.get("role") or "assistant"

            # Content delta
            content_delta = delta.get("content")
            if content_delta:
                last = messages[-1] if messages else None
                if last is not None and last.get("role") == role and "tool_calls" not in last:
                    last["content"] += content_delta
                else:
                    messages.append({"role": role, "content": content_delta})

            # Tool call deltas
            for tc in delta.get("tool_calls", []):
                tc_id = tc.get("id")
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                fn_args = fn.get("arguments", "")

                last = messages[-1] if messages else None
                if last is None or last.get("role") != "assistant":
                    last = {"role": "assistant", "content": "", "tool_calls": []}
                    messages.append(last)

                last.setdefault("tool_calls", [])
                matching_tc = None
                if tc_id:
                    seen_call_ids.add(tc_id)
                    for existing in last["tool_calls"]:
                        if existing.get("id") == tc_id:
                            matching_tc = existing
                            break

                if matching_tc is not None:
                    if fn_args:
                        matching_tc["function"]["arguments"] += fn_args
                else:
                    tool_call = {
                        "id": tc_id or f"call_{len(seen_call_ids)}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        },
                    }
                    if tool_call["id"]:
                        seen_call_ids.add(tool_call["id"])
                    last["tool_calls"].append(tool_call)

        # 3. Direct tool message events
        elif ev.get("role") == "tool":
            tool_id = str(ev.get("tool_call_id", ""))
            text = _text(ev.get("content", ""))
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": text})

    return messages


# --------------------------------------------------------------------------- #
# run metadata & model extraction
# --------------------------------------------------------------------------- #

def extract_model(events: List[Dict[str, Any]], fallback: str = "kimi-k2-turbo-preview") -> str:
    """Extract model identifier from stream events if available."""
    for ev in events:
        if ev.get("type") in ("init", "system") and ev.get("model"):
            return str(ev["model"])
        if "model" in ev and isinstance(ev["model"], str) and ev["model"]:
            return ev["model"]
    return fallback


def build_run_metadata(events: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build unified run metadata from stream events and reconstructed messages."""
    result = next((ev for ev in events if ev.get("type") == "result"), {}) or {}
    status = result.get("status", "completed")

    return {
        "status": status,
        "finish_reason": result.get("finish_reason", "stop"),
        "total_messages": len(messages),
        "total_tool_calls": sum(len(m.get("tool_calls", [])) for m in messages),
        "cost_usd": result.get("cost_usd"),
        "execution_time_seconds": result.get("execution_time"),
    }


def verify(events: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> List[str]:
    """Verify semantic validity of reconstructed messages."""
    problems = []
    call_ids = []
    for m in messages:
        for tc in m.get("tool_calls", []):
            if "id" in tc:
                call_ids.append(tc["id"])

    call_id_set = set(call_ids)
    for m in messages:
        if m.get("role") == "tool":
            t_id = m.get("tool_call_id")
            if t_id and t_id not in call_id_set:
                problems.append(f"tool msg references unknown id {t_id}")
        for tc in m.get("tool_calls", []):
            raw_args = tc.get("function", {}).get("arguments", "")
            if raw_args:
                try:
                    json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    problems.append(f"tool_call {tc.get('id')} args not valid JSON: {raw_args[:50]}")
    return problems


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def resolve_input(path: str) -> str:
    """Accept the run dir (containing final_results.json) or the file itself."""
    if os.path.isdir(path):
        return os.path.join(path, "final_results.json")
    return path


def main():
    ap = argparse.ArgumentParser(description="Convert Kimi CLI outputs to OpenAI standard trajectory format")
    ap.add_argument("--input_dir", required=True, help="Harness run dir containing final_results.json")
    ap.add_argument("--output_dir", default=None, help="Output dir (default: same as input dir)")
    args = ap.parse_args()

    in_path = resolve_input(args.input_dir)
    if not os.path.isfile(in_path):
        print(f"Error: no final_results.json found at {in_path}")
        return 1

    out_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(os.path.abspath(in_path))
    msgs_dir = os.path.join(out_dir, "messages")
    os.makedirs(msgs_dir, exist_ok=True)
    stem = os.path.basename(out_dir.rstrip("/\\")) or "trials"

    with open(in_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    trials = []
    n_msgs = 0
    n_fail = 0

    for record in records:
        instance_id = record.get("instance_id", "unknown")
        stdout = record.get("kimi_stdout") or record.get("agent_stdout", "")
        events = parse_events(stdout)
        messages = events_to_messages(events)
        probs = verify(events, messages)

        if probs:
            n_fail += 1
            print(f"  ! {instance_id}: {'; '.join(probs[:3])}")

        rel = f"messages/{instance_id}.json"
        msg_file_path = os.path.join(out_dir, rel)
        with open(msg_file_path, "w", encoding="utf-8") as mf:
            json.dump(messages, mf, ensure_ascii=False, indent=2)

        trials.append({
            "instance_id": instance_id,
            "model_patch": record.get("model_patch", "") or "",
            "model_name_or_path": record.get("model_name_or_path") or extract_model(events),
            "run_metadata": build_run_metadata(events, messages),
            "tools": None,
            "messages": rel,
        })
        n_msgs += len(messages)

    trials.sort(key=lambda x: x["instance_id"])
    out_path = os.path.join(out_dir, f"{stem}.trials.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(trials, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(trials)} trials ({n_msgs} messages total)")
    print(f"  Index : {out_path}")
    print(f"  Splits: {msgs_dir}/<instance_id>.json")
    print(f"Verification warnings: {n_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
