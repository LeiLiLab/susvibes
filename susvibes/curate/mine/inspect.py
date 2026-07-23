"""M3 inspect stage — a cheap Haiku agent that, per NVD CVE, resolves the vulnerable project's real
GitHub source repo (web search + WebFetch, no clone), plus the advisory-named vulnerable files and
the fix's language(s). The core job is finding the REAL source repo: an NVD reference is often a
PoC/writeup/advisory-tracking repo, not the source, so inspect searches for the actual project.
It works from the raw NVD description + references — NOT the prefilter's filtered repo guess (the
prefilter is only a conservative screen). Pure fact extraction, no keep/drop decision — `nvd.route`
does that. Parallels `find_commit.py`; `sources/nvd.py` orchestrates prefilter → inspect → route →
finder.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from claude_agent_sdk import ClaudeAgentOptions

from susvibes.core.agents.claude import run_agent_retrying, AGENT_ENV, MAX_BUFFER_SIZE
from susvibes.curate.constants import get_log_dir

INSPECT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Web search + fetch, plus Bash to curl the GitHub API. WebSearch isn't provisioned on Bedrock (the
# CLI silently drops it, so the agent searches via `curl` there); it stays in the list so a future
# WebSearch-capable deployment works with no code change.
INSPECT_TOOLS = ["Bash", "WebFetch", "WebSearch"]
INSPECT_WORKERS = 12
INSPECT_MAX_TURNS = 50

INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {
            "type": "string",
            "description": "the vulnerable PROJECT's own GitHub source repo as \"owner/name\", "
                           "resolved by web search when the NVD reference is a PoC/writeup/"
                           "advisory-tracking repo; \"\" only if the project has no public GitHub "
                           "source",
        },
        "vulnerable_files": {
            "type": "array", "items": {"type": "string"},
            "description": "file paths the advisory names as vulnerable/fixed, verbatim (e.g. "
                           "\"lib/parser/xml.py\"); [] if none are named",
        },
        "languages": {
            "type": "array", "items": {"type": "string"},
            "description": "the FIX's language(s), as complete as possible — from vulnerable_files' "
                           "extensions first, else the real repo's major languages; [] only if "
                           "truly undeterminable",
        },
        "reason": {"type": "string", "description": "brief justification citing what you read/found"},
    },
    "required": ["repo", "vulnerable_files", "languages", "reason"],
    "additionalProperties": False,
}

INSPECT_SYSTEM = """\
You find the real GitHub source repository of the vulnerable project in a CVE, plus the fix's \
language(s) and any advisory-named vulnerable files. Use the web — the references you are given, the \
advisory, the project's homepage, GitHub search. Do not clone anything.

A reference is often NOT the project's source — a PoC, an exploit, a writeup, or an \
advisory-tracking repo. Work out the actual affected project and find ITS GitHub source, searching \
the web when the references don't point there. Return "" for repo only if the project has no public \
GitHub source at all (proprietary, on-chain contract, SVN/hg-only, firmware). Report facts only.
"""

INSPECT_USER = """\
CVE: {cve_id}
Description: {desc}
NVD references:
{refs}
"""


def inspect_miss(record, error, meta=None):
    """An inspect result that resolved nothing (aborted run). `error` set → `resume` re-runs it."""
    return {**record, "repo": "", "vulnerable_files": [], "languages": [], "reason": "",
            "error": error, "_meta": meta or {}}


def refs_block(refs) -> str:
    """The raw NVD references (url + tags) as prompt lines — NVD's own data, not the prefilter's
    filtered repo guess, so inspect resolves the real repo independently."""
    lines = []
    for ref in refs:
        tags = f"  [{', '.join(ref['tags'])}]" if ref.get("tags") else ""
        lines.append(f"  {ref['url']}{tags}")
    return "\n".join(lines) or "  (none)"


def inspect_single(record, run_id):
    """Resolve `{repo, vulnerable_files, languages}` for one NVD CVE from its advisory + web search
    (Haiku, no clone; retryable failures retried). Always returns the record merged with the output
    plus `error`/`_meta`, so the outcome is cacheable and resumable."""
    options = ClaudeAgentOptions(
        model=INSPECT_MODEL,
        system_prompt=INSPECT_SYSTEM,
        tools=INSPECT_TOOLS,                    # availability gate: only these tools exist (see INSPECT_TOOLS)
        allowed_tools=INSPECT_TOOLS,            # pre-approve the whole set; fail-closed on anything else
        setting_sources=[],                     # don't load the project's interactive settings/hooks
        permission_mode="dontAsk",              # fail-closed: pre-approved tools auto-run headless, else denied
        max_turns=INSPECT_MAX_TURNS,
        max_buffer_size=MAX_BUFFER_SIZE,
        env=AGENT_ENV,
        output_format={"type": "json_schema", "schema": INSPECT_SCHEMA},
    )
    prompt = INSPECT_USER.format(
        cve_id=record["cve_id"], desc=record["desc"], refs=refs_block(record["refs"]))
    log_path = get_log_dir(run_id, "mine", "inspect") / record["cve_id"] / "trajectory.jsonl"
    output, meta = run_agent_retrying(prompt, options, log_path=log_path)
    if output is None:
        return inspect_miss(record, meta.get("error", "agent produced no result"), meta)
    return {**record, **output, "error": None, "_meta": meta}


def inspect_threadpool(records, run_id, max_workers=INSPECT_WORKERS):
    """Run inspect over records concurrently (web-only reads, no shared state). One result per record."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(inspect_single, record, run_id): record["cve_id"]
                   for record in records}
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Inspecting [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                cve_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {cve_id}: {e}")
                results.append(result)
                pbar.update(1)
                with_repo = sum(1 for r in results if r["repo"])
                errored = sum(1 for r in results if r["error"])
                cost = sum(r["_meta"].get("cost_usd") or 0 for r in results)
                pbar.set_description(f"{with_repo} with-repo, {errored} err, ${cost:.2f}")
    return results
