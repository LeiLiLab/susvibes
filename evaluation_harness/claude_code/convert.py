#!/usr/bin/env python3
"""
Convert claude-cli harness output into the standard format (OpenAI messages).

Input is the harness `final_results.json` (a JSON list of per-instance records); the
trajectory lives on each record as `claude_stdout` — the raw `claude --output-format
stream-json` text, one JSON event per line. Events are `system` (init metadata),
`assistant`, `user` (claude wraps tool results in user events), and `result`.

Output is the OpenAI / ms-swift `messages` format, written in a SPLIT layout
(trajectories are too large to inline): a record's `messages` field is a *path string*
to a file holding the actual messages array.

    <output>/<stem>.trials.json   ->  [ {instance_id, model_patch, model_name_or_path,
                                         run_metadata, tools, messages: "messages/<id>.json"}, ... ]
    <output>/messages/<id>.json   ->  [ ...OpenAI chat messages... ]

Messages are reconstructed from the events: an `assistant` event's content blocks become
the assistant message (text + `tool_use` -> `tool_calls`), and `tool_result` blocks (which
claude nests inside `user` events) become `tool` messages paired by `tool_use_id`. There is
no system prompt or initial user task in the stream (the task is passed via `-p` and not
echoed), so messages begin at the first assistant turn.

`tools` is `null`: the stream only lists tool *names* (no OpenAI schemas), so nothing is
emitted rather than fabricated. The model name is read from the `system` event's `model`.

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
    """Parse a raw stream-json string into a list of event dicts.

    `claude --verbose` may interleave non-JSON log lines, so each line is parsed
    defensively and anything that is not a JSON object with a `type` is dropped.
    """
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


def _text_from(content):
    """Flatten a content value (str | list-of-blocks | None) into a plain string."""
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


def events_to_messages(events):
    """Reconstruct OpenAI messages from claude-cli's Anthropic-nested events."""
    messages = []
    seen_call_ids = set()
    for ev in events:
        etype = ev.get("type")
        if etype in ("system", "result"):
            continue
        message = ev.get("message")
        if isinstance(message, str):
            message = {"content": message}
        elif not isinstance(message, dict):
            message = {}
        role = message.get("role") or etype
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]

        if role == "assistant":
            text_parts, tool_calls = [], []
            for c in blocks:
                if isinstance(c, str):
                    text_parts.append(c)
                elif isinstance(c, dict):
                    ctype = c.get("type")
                    if ctype == "text":
                        text_parts.append(c.get("text", ""))
                    elif ctype == "tool_use":
                        tool_calls.append({
                            "id": c.get("id"),
                            "type": "function",
                            "function": {
                                "name": c.get("name"),
                                "arguments": json.dumps(c.get("input", {}), ensure_ascii=False),
                            },
                        })
                        seen_call_ids.add(c.get("id"))
                    elif ctype == "tool_result":
                        text_parts.append(_text_from(c.get("content")))
                    else:
                        text_parts.append(json.dumps(c, ensure_ascii=False))
            msg = {"role": "assistant", "content": "\n".join(p for p in text_parts if p)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
            continue

        # user (and any non-assistant) event: emit tool_result blocks as `tool`
        # messages, paired by tool_use_id; keep any plain text as a user message.
        text_parts = []
        for c in blocks:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                if text_parts:
                    messages.append({"role": "user", "content": "\n".join(p for p in text_parts if p)})
                    text_parts = []
                tcid = c.get("tool_use_id")
                text = _text_from(c.get("content"))
                if tcid in seen_call_ids:
                    messages.append({"role": "tool", "tool_call_id": tcid, "content": text})
                else:
                    messages.append({"role": "user", "content": text})
            elif isinstance(c, str):
                text_parts.append(c)
            elif isinstance(c, dict) and c.get("type") == "text":
                text_parts.append(c.get("text", ""))
            elif c is not None:
                text_parts.append(json.dumps(c, ensure_ascii=False))
        if text_parts:
            messages.append({"role": "user", "content": "\n".join(p for p in text_parts if p)})
    return messages


# --------------------------------------------------------------------------- #
# run metadata
# --------------------------------------------------------------------------- #

def build_run_metadata(events, messages):
    """Build the unified run metadata from claude-cli's `result` event."""
    result = next((ev for ev in events if ev.get("type") == "result"), {}) or {}
    is_error = bool(result.get("is_error"))
    sub = result.get("subtype")
    if is_error:
        subtype = "error"
    elif sub in ("completed", "success"):
        subtype = "completed"
    else:
        subtype = "incomplete"

    num_turns = result.get("num_turns")
    if num_turns is None:
        num_turns = sum(1 for m in messages if m.get("role") == "assistant")
    usage = result.get("usage") or {}
    return {
        # unified core
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": num_turns,
        "total_cost_usd": result.get("total_cost_usd"),
        # scaffold-specific extras
        "exit_status": result.get("subtype"),
        "tokens_sent": usage.get("input_tokens"),
        "tokens_received": usage.get("output_tokens"),
        "tool_execution_time_s": None,
    }


def extract_model(events):
    """Read the model id from the `system` (init) event."""
    for ev in events:
        if ev.get("type") == "system" and ev.get("model"):
            return ev["model"]
    return "unknown"


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def verify(events, messages):
    """Structural checks; returns a list of problems (empty == ok)."""
    problems = []
    n_tool_use = 0
    for ev in events:
        if ev.get("type") in ("system", "result"):
            continue
        content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
        if isinstance(content, list):
            n_tool_use += sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
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
        events = parse_events(record.get("claude_stdout", ""))
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
