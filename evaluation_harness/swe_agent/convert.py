#!/usr/bin/env python3
"""
Convert SWE-agent .traj files into the standard format (OpenAI messages).

Output is the OpenAI / ms-swift `messages` format, written in a SPLIT layout
(trajectories are too large to inline): a record's `messages` field is a *path string*
to a file holding the actual messages array.

    <output>/<stem>.trials.json   ->  [ {instance_id, model_patch, model_name_or_path,
                                         run_metadata, tools, messages: "messages/<id>.json"}, ... ]
    <output>/messages/<id>.json   ->  [ ...OpenAI chat messages... ]

The messages are taken verbatim from the .traj `history` (the COMPLETE message list:
the real system prompt, the initial user task, and every assistant + tool turn through
the end), not reconstructed from per-step action strings — so nothing is dropped.
`run_metadata` carries run metadata (subtype, is_error, num_turns, total_cost_usd, plus
scaffold extras: exit_status, tokens_sent, tokens_received, tool_execution_time_s).
`tools` is the OpenAI tool (function) list available to the agent, parsed from the .traj's
`replay_config.agent.tools.command_docs`.

The model name is read from the run's `run_batch.config.yaml` (`agent.model.name`).

Usage:
    python convert.py --input_dir <run dir> [--output_dir DIR]
"""

import argparse
import glob
import json
import os
import re
import sys


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _text_from(content):
    """Flatten a content value (str | list-of-blocks) into a plain string."""
    if isinstance(content, str):
        return content
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
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _clean_tool_calls(tool_calls):
    """Normalize SWE-agent tool_calls to OpenAI shape (drop `index`, ensure str args)."""
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


def history_to_messages(history):
    """Convert the .traj `history` (full message list) into OpenAI messages."""
    messages = []
    for m in history:
        role = m.get("role")
        if role == "system":
            messages.append({"role": "system", "content": _text_from(m.get("content"))})
        elif role == "user":
            messages.append({"role": "user", "content": _text_from(m.get("content"))})
        elif role == "assistant":
            msg = {"role": "assistant",
                   "content": m["content"] if isinstance(m.get("content"), str)
                   else _text_from(m.get("content"))}
            tcs = _clean_tool_calls(m.get("tool_calls"))
            if tcs:
                msg["tool_calls"] = tcs
            messages.append(msg)
        elif role == "tool":
            ids = m.get("tool_call_ids") or []
            content = m.get("content")
            # SWE-agent: one tool result per call. Pair when counts match, else join.
            if isinstance(content, list) and len(content) == len(ids) and ids:
                for cid, c in zip(ids, content):
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "content": _text_from([c])})
            else:
                text = _text_from(content)
                if ids:
                    messages.append({"role": "tool", "tool_call_id": ids[0], "content": text})
                else:
                    messages.append({"role": "tool", "content": text})
        # unknown roles ignored
    return messages


def build_result(info, trajectory):
    """Build the unified run metadata from .traj info + per-step execution times."""
    stats = info.get("model_stats", {}) or {}
    exit_status = info.get("exit_status", "")
    submitted = isinstance(exit_status, str) and exit_status.startswith("submitted")
    subtype = "completed" if submitted else ("error" if "error" in (exit_status or "") else "incomplete")
    tool_time = sum(s.get("execution_time", 0) or 0 for s in trajectory)
    return {
        # unified core
        "subtype": subtype,
        "is_error": not submitted,
        "num_turns": stats.get("api_calls"),
        "total_cost_usd": stats.get("instance_cost"),
        # scaffold-specific extras
        "exit_status": exit_status,
        "tokens_sent": stats.get("tokens_sent"),
        "tokens_received": stats.get("tokens_received"),
        "tool_execution_time_s": round(tool_time, 3),
    }


def verify(history, messages):
    """Structural checks; returns list of problems (empty == ok)."""
    problems = []
    n_tool_use = sum(len(m.get("tool_calls") or []) for m in history if m.get("role") == "assistant")
    call_ids = {tc["id"] for m in messages for tc in m.get("tool_calls", [])}
    if n_tool_use != len(call_ids):
        problems.append(f"tool_use={n_tool_use} vs emitted tool_calls={len(call_ids)}")
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") not in call_ids:
            problems.append(f"tool msg references unknown id {m.get('tool_call_id')}")
        for tc in m.get("tool_calls", []):
            try:
                json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, TypeError, KeyError):
                problems.append(f"tool_call {tc.get('id')} args not JSON")
    # message count: 1 per history message, except tool messages may fan out per id
    return problems


def model_from_run_config(in_dir):
    """Read the model name from the run-level run_batch.config.yaml (one per run).

    The file is a YAML single-quoted scalar wrapping a JSON object, so it needs PyYAML to
    unfold before json.loads. Returns the litellm model id (e.g. "bedrock/zai.glm-5").
    """
    p = os.path.join(in_dir, "run_batch.config.yaml")
    if not os.path.isfile(p):
        return None
    try:
        import yaml  # lazy: only model auto-detection needs it
        obj = yaml.safe_load(open(p, encoding="utf-8"))
        if isinstance(obj, str):
            obj = json.loads(obj)
        return ((obj.get("agent") or {}).get("model") or {}).get("name")
    except Exception:
        return None


def model_from_dirname(path):
    m = re.search(r"susvibes_eval__(.+?)__t-", os.path.basename(path.rstrip("/")))
    return m.group(1) if m else "unknown"


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def _parse_command_docs(text):
    """Parse SWE-agent `command_docs` text into OpenAI tool (function) schemas.

    The block looks like:
        <tool>:
          docstring: <maybe multi-line text>
          signature: <...>
          arguments:
            - <name> (<type>) [required|optional]: <description>
    """
    if not text:
        return []
    lines = text.split("\n")
    # a tool block starts at a bare `name:` at column 0
    starts = [i for i, ln in enumerate(lines) if re.match(r"^[A-Za-z_]\w*:\s*$", ln)]
    tools = []
    for k, s in enumerate(starts):
        name = lines[s].rstrip()[:-1]
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        doc, arg_lines, section = [], [], None
        for ln in lines[s + 1:end]:
            t = ln.strip()
            if t.startswith("docstring:"):
                section = "doc"
                rest = t[len("docstring:"):].strip()
                if rest:
                    doc.append(rest)
            elif t.startswith("signature:"):
                section = "sig"
            elif t.startswith("arguments:"):
                section = "args"
            elif section == "doc" and t:
                doc.append(t)
            elif section == "args" and t.startswith("-"):
                arg_lines.append(t)
        props, required = {}, []
        for a in arg_lines:
            m = re.match(r"-\s*(\w+)\s*\(([^)]+)\)\s*\[(required|optional)\]:\s*(.*)", a)
            if not m:
                continue
            an, atype, req, desc = m.groups()
            jtype = atype.strip().lower()
            props[an] = {"type": jtype if jtype in _TYPES else "string",
                         "description": desc.strip()}
            if req == "required":
                required.append(an)
        params = {"type": "object", "properties": props}
        if required:
            params["required"] = required
        tools.append({"type": "function",
                      "function": {"name": name,
                                   "description": " ".join(doc).strip(),
                                   "parameters": params}})
    return tools


def tools_from_traj(d):
    """Extract the available OpenAI tools from a .traj's replay_config."""
    rc = d.get("replay_config")
    if not isinstance(rc, str):
        return []
    try:
        cfg = json.loads(rc)
    except (ValueError, TypeError):
        return []
    cd = ((cfg.get("agent") or {}).get("tools") or {}).get("command_docs")
    return _parse_command_docs(cd or "")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", default=here,
                    help="run dir containing <instance>/<instance>.traj subfolders")
    ap.add_argument("--output_dir", default=None, help="output dir (default: the input dir)")
    args = ap.parse_args()

    in_dir = os.path.abspath(args.input_dir)
    out_dir = os.path.abspath(args.output_dir) if args.output_dir else in_dir
    # model is read from the run's run_batch.config.yaml (falls back to the dir name)
    model = model_from_run_config(in_dir) or model_from_dirname(in_dir)
    msgs_dir = os.path.join(out_dir, "messages")
    os.makedirs(msgs_dir, exist_ok=True)
    stem = os.path.basename(out_dir.rstrip("/")) or "trials"

    # optional base preds.json fallback for patches
    preds = {}
    pj = os.path.join(in_dir, "preds.json")
    if os.path.isfile(pj):
        with open(pj, encoding="utf-8") as f:
            preds = json.load(f)

    traj_files = sorted(glob.glob(os.path.join(in_dir, "*", "*.traj")))
    print(f"model={model}  found {len(traj_files)} .traj files")

    trials, n_msgs, n_fail = [], 0, 0
    for traj_path in traj_files:
        instance_id = os.path.basename(os.path.dirname(traj_path))
        try:
            with open(traj_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  ! {instance_id}: cannot read ({e})")
            n_fail += 1
            continue

        history = d.get("history", []) or []
        info = d.get("info", {}) or {}
        trajectory = d.get("trajectory", []) or []

        messages = history_to_messages(history)
        model_patch = info.get("submission") or preds.get(instance_id, {}).get("model_patch", "") or ""

        probs = verify(history, messages)
        if probs:
            n_fail += 1
            print(f"  ! {instance_id}: {'; '.join(probs[:3])}")

        # split layout: each instance's messages go to messages/<id>.json,
        # the index keeps a path string (trajectories are too large to inline).
        rel = f"messages/{instance_id}.json"
        with open(os.path.join(out_dir, rel), "w", encoding="utf-8") as mf:
            json.dump(messages, mf, ensure_ascii=False, indent=1)

        trials.append({
            "instance_id": instance_id,
            "model_patch": model_patch,
            "model_name_or_path": model,
            "run_metadata": build_result(info, trajectory),
            "tools": tools_from_traj(d),
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
