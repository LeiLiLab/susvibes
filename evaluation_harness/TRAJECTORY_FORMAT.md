# Trajectory Format

The standard format produced by `convert.py` — one record per instance, with the
conversation as OpenAI-style `messages`.

## Record

```jsonc
{
  "instance_id": "django__django_<hash>",                // owner__repo_commitHash
  "model_patch": "diff --git ...",                       // the agent's predicted patch (may be empty)
  "model_name_or_path": "gemini/gemini-3-pro-preview",   // model id parsed from the run
  "run_metadata": { ... },                               // run metadata (below)
  "tools": [ ... ],                                      // tool schemas available to the agent (below)
  "messages": [ ... ]                                    // OpenAI chat messages (below)
}
```

## `messages`

A list of chat messages, in order: the `system` prompt, the `user` task, then the agent's
`assistant`/`tool` turns. `role` is one of `system`, `user`, `assistant`, `tool`, with these
fields:

| role | fields |
|------|--------|
| `system` | `content` (string) |
| `user` | `content` (string) — the task, or an injected observation that isn't a tool result |
| `assistant` | `content` (string; `""` when the turn only calls tools); `tool_calls` (omit when none) |
| `tool` | `tool_call_id` (string); `content` (string) — the tool's output |

```jsonc
{"role": "assistant", "content": "Reading the file.", "tool_calls": [
  {"id": "toolu_001", "type": "function",
   "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]}
{"role": "tool", "tool_call_id": "toolu_001", "content": "<tool output>"}
```

A `tool_call` is `{"id": <unique string>, "type": "function", "function": {"name": <string>,
"arguments": <JSON string>}}` — `type` is always `"function"`, and `arguments` is a JSON
**string**, not an object. Each `id` is answered by exactly one `tool` message with the same
`tool_call_id`.

## `tools`

A list of available tools. Each is `{"type": "function", "function": {...}}` (`type` is always
`"function"`), where `function` is `{name, description, parameters}` and `parameters` is a
JSON Schema:

```jsonc
{"type": "function", "function": {
  "name": "execute_bash",
  "description": "Run a bash command.",
  "parameters": {
    "type": "object",
    "properties": {
      "command": {"type": "string", "description": "...", "enum": ["..."]}  // description, enum optional
    },
    "required": ["command"]
  }}}
```

Each property's `type` is one of `string`, `integer`, `number`, `boolean`, `array`, `object`;
`required` lists the mandatory property names.

## `run_metadata`

A core set of fields, plus optional extras.

| Field | Meaning |
|-------|---------|
| `subtype` | `completed` / `error` / `incomplete`. |
| `is_error` | Whether the run failed to complete/submit. |
| `num_turns` | Number of assistant turns. |
| `total_cost_usd` | Total LLM cost in USD (`null` if untracked). |

Optional extras — may be absent or `null`, so don't rely on them: `exit_status`,
`tokens_sent`, `tokens_received`, `tool_execution_time_s`.
