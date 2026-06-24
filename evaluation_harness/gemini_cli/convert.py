#!/usr/bin/env python3
"""
Convert gemini-cli harness output into the standard format (OpenAI messages).

Input is the harness `final_results.json` (a JSON list of per-instance records); the
trajectory lives on each record as `gemini_stdout` — the raw `gemini --output-format
stream-json` text, one JSON event per line. Events are `init` (metadata), `message`
(`{role, content}`), `tool_use` (`{tool_name, tool_id, parameters}`), `tool_result`
(`{tool_id, status, output}`), and `result`.

Output is the OpenAI / ms-swift `messages` format, written in a SPLIT layout
(trajectories are too large to inline): a record's `messages` field is a *path string*
to a file holding the actual messages array.

    <output>/<stem>.trials.json   ->  [ {instance_id, model_patch, model_name_or_path,
                                         run_metadata, tools, messages: "messages/<id>.json"}, ... ]
    <output>/messages/<id>.json   ->  [ ...OpenAI chat messages... ]

gemini-cli streams an assistant turn as many small `message` events (token-level chunks,
often split mid-sentence), so consecutive same-role `message` fragments are concatenated
back into one message. `tool_use` events attach as `tool_calls` on the current assistant
message, and `tool_result` events become `tool` messages paired by `tool_id`.

`tools` is `null`: the stream carries no tool schemas (the `init` event has no tool list),
so nothing is emitted rather than fabricated. The model name is read from the `init` event;
gemini-cli reports no LLM cost, so `total_cost_usd` is `null`.

Usage:
    python convert.py --input_dir <run dir> [--output_dir DIR]
"""

import argparse
import json
import os
import sys


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

def parse_events(stdout):
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
        if isinstance(obj, dict) and "type" in obj:
            events.append(obj)
    return events


def _text(value):
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


def events_to_messages(events):
    """Reconstruct OpenAI messages from gemini-cli's flat events.

    Consecutive same-role `message` fragments are merged into one message (gemini streams
    a turn in chunks). A `tool_use` joins the current assistant message as a tool_call;
    once a message carries tool_calls a new fragment starts a fresh message.
    """
    messages = []
    seen_call_ids = set()
    for ev in events:
        etype = ev.get("type")
        if etype in ("init", "result", "error"):
            continue

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
            tool_id = ev.get("tool_id")
            seen_call_ids.add(tool_id)
            tool_call = {
                "id": tool_id,
                "type": "function",
                "function": {
                    "name": ev.get("tool_name"),
                    "arguments": json.dumps(ev.get("parameters", {}), ensure_ascii=False),
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})

        elif etype == "tool_result":
            tool_id = ev.get("tool_id")
            text = _text(ev.get("output"))
            if tool_id in seen_call_ids:
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": text})
            else:
                messages.append({"role": "user", "content": text})
    return messages


# --------------------------------------------------------------------------- #
# run metadata
# --------------------------------------------------------------------------- #

def build_run_metadata(events, messages):
    """Build the unified run metadata from gemini-cli's `result` event."""
    result = next((ev for ev in events if ev.get("type") == "result"), {}) or {}
    status = result.get("status")
    if status == "error":
        subtype = "error"
    elif status == "success":
        subtype = "completed"
    else:
        subtype = "incomplete"
    stats = result.get("stats") or {}
    num_turns = sum(1 for m in messages if m.get("role") == "assistant")
    return {
        # unified core
        "subtype": subtype,
        "is_error": status == "error",
        "num_turns": num_turns,
        "total_cost_usd": None,
        # scaffold-specific extras
        "exit_status": status,
        "tokens_sent": stats.get("input_tokens"),
        "tokens_received": stats.get("output_tokens"),
        "tool_execution_time_s": None,
    }


def extract_model(events):
    """Read the model id from the `init` event."""
    for ev in events:
        if ev.get("type") == "init" and ev.get("model"):
            return ev["model"]
    return "unknown"


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def verify(events, messages):
    """Structural checks; returns a list of problems (empty == ok)."""
    problems = []
    n_tool_use = sum(1 for ev in events if ev.get("type") == "tool_use")
    call_ids = [tc.get("id") for m in messages for tc in m.get("tool_calls", [])]
    if n_tool_use != len(call_ids):
        problems.append(f"tool_use={n_tool_use} vs emitted tool_calls={len(call_ids)}")
    call_id_set = set(call_ids)
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") not in call_id_set:
            problems.append(f"tool msg references unknown id {m.get('tool_call_id')}")
        for tc in m.get("tool_calls", []):
            try:
                json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, TypeError, KeyError):
                problems.append(f"tool_call {tc.get('id')} args not JSON")
    return problems


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def resolve_input(path):
    """Accept the run dir (containing final_results.json) or the file itself."""
    if os.path.isdir(path):
        return os.path.join(path, "final_results.json")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", required=True,
                    help="harness run dir containing final_results.json")
    ap.add_argument("--output_dir", default=None, help="output dir (default: the input dir)")
    args = ap.parse_args()

    in_path = resolve_input(args.input_dir)
    if not os.path.isfile(in_path):
        print(f"no final_results.json at {in_path}")
        return 1
    out_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(os.path.abspath(in_path))
    msgs_dir = os.path.join(out_dir, "messages")
    os.makedirs(msgs_dir, exist_ok=True)
    stem = os.path.basename(out_dir.rstrip("/")) or "trials"

    with open(in_path, encoding="utf-8") as f:
        records = json.load(f)

    trials, n_msgs, n_fail = [], 0, 0
    for record in records:
        instance_id = record.get("instance_id", "unknown")
        events = parse_events(record.get("gemini_stdout", ""))
        messages = events_to_messages(events)
        probs = verify(events, messages)
        if probs:
            n_fail += 1
            print(f"  ! {instance_id}: {'; '.join(probs[:3])}")

        rel = f"messages/{instance_id}.json"
        with open(os.path.join(out_dir, rel), "w", encoding="utf-8") as mf:
            json.dump(messages, mf, ensure_ascii=False, indent=1)

        trials.append({
            "instance_id": instance_id,
            "model_patch": record.get("model_patch", "") or "",
            "model_name_or_path": extract_model(events),
            "run_metadata": build_run_metadata(events, messages),
            "tools": None,
            "messages": rel,
        })
        n_msgs += len(messages)

    trials.sort(key=lambda x: x["instance_id"])
    out_path = os.path.join(out_dir, f"{stem}.trials.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(trials, f, ensure_ascii=False, indent=1)

    print(f"\nwrote {len(trials)} trials ({n_msgs} messages total)")
    print(f"  index : {out_path}")
    print(f"  splits: {msgs_dir}/<instance_id>.json")
    print(f"verification failures: {n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
