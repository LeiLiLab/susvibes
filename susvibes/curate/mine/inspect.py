"""M3 inspect stage — a cheap Haiku agent that, per NVD CVE, reads the advisory (web only, no
clone) and extracts the vulnerable project's repo, the advisory-named vulnerable files, and the
fix's language(s). Pure fact extraction: it makes no keep/drop decision — `nvd.route` does that
from these facts. Parallels `find_commit.py` (the finder agent); `sources/nvd.py` orchestrates
prefilter → inspect → route → finder.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from claude_agent_sdk import ClaudeAgentOptions

from susvibes.core.agents.claude import run_agent_retrying, AGENT_ENV, MAX_BUFFER_SIZE
from susvibes.curate.constants import get_log_dir

INSPECT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
INSPECT_TOOLS = ["WebFetch"]
INSPECT_WORKERS = 12
INSPECT_MAX_TURNS = 30

INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {
            "type": "string",
            "description": "the vulnerable project's GitHub source repo as \"owner/name\", or \"\" "
                           "if there is only a PoC/writeup dump or no GitHub source",
        },
        "is_poc_only_ref": {
            "type": "boolean",
            "description": "true if the only GitHub reference is a PoC/writeup/exploit dump, not "
                           "the project source",
        },
        "vulnerable_files": {
            "type": "array", "items": {"type": "string"},
            "description": "file paths the advisory names as vulnerable/fixed, verbatim (e.g. "
                           "\"lib/parser/xml.py\"); [] if none are named",
        },
        "languages": {
            "type": "array", "items": {"type": "string"},
            "description": "the FIX's language(s), as complete as possible — from vulnerable_files' "
                           "extensions first, else the repo's major languages; [] only if truly "
                           "undeterminable",
        },
        "reason": {"type": "string", "description": "brief justification citing what you read"},
    },
    "required": ["repo", "is_poc_only_ref", "vulnerable_files", "languages", "reason"],
    "additionalProperties": False,
}

INSPECT_PROMPT = """\
Extract facts about a CVE from its public advisory — do NOT guess, and you have web access only \
(no repository clone). Read the NVD page and the referenced pages (advisory, PR, issue, repo).

CVE: {cve_id}
NVD description: {desc}
GitHub URLs referenced by NVD: {repos}

Report:
1. repo — the vulnerable PROJECT's GitHub source repo as "owner/name". NOT a PoC/writeup/exploit \
dump, NOT an advisory-database. If the only GitHub reference is a PoC/exploit repo, or the project \
has no GitHub source, return "".
2. is_poc_only_ref — true if the referenced GitHub repo(s) are only PoC/writeup/exploit dumps.
3. vulnerable_files — the file paths the advisory/NVD names as vulnerable or fixed, VERBATIM (e.g. \
"lib/parser/xml.py"). [] if none are named.
4. languages — the language(s) of the FIX, as complete as possible: derive from the \
vulnerable_files' extensions first; if none are named, the repo's major languages. [] only if \
truly undeterminable. Describe the fix, not just the repo.

Report facts only — do NOT decide whether this CVE is kept or dropped; that happens downstream.
"""


def inspect_miss(record, error, meta):
    """An inspect result that extracted nothing (aborted run). `error` set → `resume` re-runs it."""
    return {**record, "repo": "", "is_poc_only_ref": False, "vulnerable_files": [],
            "languages": [], "reason": "", "error": error, "_meta": meta or {}}


def inspect_single(record, run_id):
    """Extract `{repo, is_poc_only_ref, vulnerable_files, languages}` for one NVD CVE from its
    advisory (Haiku, web-only, no clone; retryable failures retried). Always returns the record
    merged with the output plus `error`/`_meta`, so the outcome is cacheable and resumable."""
    options = ClaudeAgentOptions(
        model=INSPECT_MODEL,
        allowed_tools=INSPECT_TOOLS,
        max_turns=INSPECT_MAX_TURNS,
        max_buffer_size=MAX_BUFFER_SIZE,
        env=AGENT_ENV,
        output_format={"type": "json_schema", "schema": INSPECT_SCHEMA},
    )
    repos = ", ".join(f"{owner}/{repo}" for owner, repo in record["repos"])
    prompt = INSPECT_PROMPT.format(cve_id=record["cve_id"], desc=record["desc"], repos=repos)
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
