"""S2 fact-based finder — given a CVE and its repository, pin the single upstream fix commit by
fact (the commit/advisory cites the CVE/GHSA/PR, or the commit is contained in the named fixed
version and touches the named component), not by reasoning about whether a diff "looks like" a
fix. Shared by M2 (OSVResidualSource) and M3-Stage2 (nvd.py): both hand it a record with a repo
but no commit, and it fills in `commit` (or "" when no fact pins one in that repo).

Read-only investigation on a `clone.finder_clone` clone (full or blobless-bare) via the Claude
Agent SDK (`core/agents/claude.run_agent`, Sonnet 5 on Bedrock, READONLY_TOOLS). The agent never
writes to the clone (no fetch/checkout/reset) — clone.py owns every git write. `finder_single`
always returns a result dict (encoding "not found" as `commit=""`) so the source can cache the
outcome and never pay for the same agent run twice.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from claude_agent_sdk import ClaudeAgentOptions

from susvibes.core.agents.claude import (
    run_agent_retrying, READONLY_TOOLS, AGENT_ENV, MAX_BUFFER_SIZE)
from susvibes.curate.constants import get_log_dir
from susvibes.curate.mine.clone import finder_clone

FINDER_MODEL = "us.anthropic.claude-sonnet-5"
FINDER_WORKERS = 8
FINDER_MAX_TURNS = 50

FINDER_SCHEMA = {
    "type": "object",
    "properties": {
        "commit": {
            "type": "string",
            "description": "full 40-char sha of the single primary fix commit on the default "
                           "branch, or \"\" if no fact pins a fix commit in this repository",
        },
        "multi_commit": {
            "type": "boolean",
            "description": "true only if the fix is genuinely split across distinct commits that "
                           "cannot be collapsed to one mainline commit (not backports/cherry-picks)",
        },
        "fact_tier": {
            "type": "integer",
            "description": "1 = the commit/advisory directly cites the CVE/GHSA/PR; 2 = linked via "
                           "component+fixed-version or PR XREF; 0 when commit is \"\"",
        },
        "fact_evidence": {
            "type": "string",
            "description": "the concrete fact(s) that pin the commit, or why none was found",
        },
        "source": {
            "type": "string",
            "description": "the git commands / URLs used to establish the fact",
        },
    },
    "required": ["commit", "multi_commit", "fact_tier", "fact_evidence", "source"],
    "additionalProperties": False,
}

FINDER_PROMPT = """\
Find the single upstream fix commit for a known CVE in ONE specific git repository, by FACT — not \
by reasoning about whether a diff "looks like" a fix.

CVE: {cve_id}
GitHub advisory (GHSA): {ghsa}
Repository: {repo} (cloned at the current working directory)
{hints}
Pin the commit on this repository's default branch that ships the fix, backed by a verifiable \
fact. Work ONLY within this repository — its local clone plus this same repo's GitHub \
advisory/PR/API pages for freshness. Do NOT search other repositories and do NOT go looking for a \
different repo; if the fix is not in THIS repository, return commit="".

The clone is read-only and may be a blobless/bare mirror: use history commands only (git log, \
git show, git grep <rev>, git tag --contains, git cat-file, git merge-base). Do NOT run git \
fetch/checkout/reset/gc or edit anything.

Fact strategies (use whichever land — you rarely need all):
1. git log --all --grep for the CVE id and for the GHSA id.
2. Get the advisory's PR/issue number (fetch the GHSA/NVD advisory), then git log --all --grep \
that number, or resolve the PR's merge_commit_sha via this repo's GitHub API.
3. Extract the specific vulnerable component/feature keyword the advisory names, git grep the \
history for THAT, and cross-check the candidate is contained in the named fixed-version tag \
(git tag --contains <sha>; git log <prev_tag>..<fixed_tag> -- <path>).
4. Diff the fixed release: git log between the last-vulnerable and first-fixed tags over the \
vulnerable path.
5. The advisory or NVD references the commit URL directly.

The GHSA id and any fixed version above are LEADS, not ground truth — if the repository \
contradicts them, trust the repository.

Multi-commit rule: return the SINGLE primary fix commit on the default/main branch. Collapse \
backports/cherry-picks of one change (branch backports, "cherry picked from <sha>", the same PR#) \
to their mainline original. Only set multi_commit=true if the fix is genuinely several DISTINCT \
changes (e.g. two separate PRs fixing two parts) that cannot be reduced to one commit.

Return the full 40-character sha. If no fact in this repository pins a commit, return commit="" \
and explain why in fact_evidence.
"""


def finder_hints(record) -> str:
    lines = []
    if record.get("fixed_versions"):
        lines.append(f"Fixed in version(s): {', '.join(record['fixed_versions'])}")
    if record.get("summary"):
        lines.append(f"Advisory summary: {record['summary']}")
    return "\n".join(lines) + "\n" if lines else ""


def finder_miss(record, error, meta=None):
    """A finder result that pinned nothing. `error=None` is a real "no fix in this repo" (final,
    yielded past); a set `error` is an aborted run (clone/agent failure) that `resume` re-runs."""
    return {**record, "commit": "", "multi_commit": False, "fact_tier": 0,
            "fact_evidence": "", "source": "", "error": error, "_meta": meta or {}}


def finder_single(record, run_id):
    """Pin the fix commit for one CVE (retryable failures retried under the hood). Returns the
    record merged with the finder's output (`commit`/`multi_commit`/`fact_tier`/`fact_evidence`/
    `source`) plus `error` and `_meta` (cost/turns). `commit=""` with `error=None` is a real miss
    (final); `commit=""` with `error` set is an aborted run that `resume` re-runs."""
    project = f"{record['owner']}/{record['repo']}".lower()
    repo_dir = finder_clone(project)
    if repo_dir is None:
        return finder_miss(record, f"clone failed: {project}")
    options = ClaudeAgentOptions(
        model=FINDER_MODEL,
        allowed_tools=READONLY_TOOLS,
        cwd=str(repo_dir),
        max_turns=FINDER_MAX_TURNS,
        max_buffer_size=MAX_BUFFER_SIZE,
        env=AGENT_ENV,
        output_format={"type": "json_schema", "schema": FINDER_SCHEMA},
    )
    prompt = FINDER_PROMPT.format(
        cve_id=record["cve_id"], ghsa=record.get("ghsa", ""),
        repo=project, hints=finder_hints(record),
    )
    log_path = get_log_dir(run_id, "mine", "find_commit") / record["cve_id"] / "trajectory.jsonl"
    output, meta = run_agent_retrying(prompt, options, log_path=log_path)
    if output is None:
        return finder_miss(record, meta.get("error", "agent produced no result"), meta)
    return {**record, **output, "error": None, "_meta": meta}


def finder_threadpool(records, run_id, max_workers=FINDER_WORKERS):
    """Run the finder over records concurrently (reads are concurrency-safe; clone.py serializes
    the writes). Returns one result dict per record."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(finder_single, record, run_id): record["cve_id"]
            for record in records
        }
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Finding commits [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                cve_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {cve_id}: {e}")
                results.append(result)
                pbar.update(1)
                pinned = sum(bool(r["commit"]) for r in results)
                errored = sum(bool(r["error"]) for r in results)
                cost = sum(r["_meta"].get("cost_usd") or 0 for r in results)
                pbar.set_description(f"{pinned} pinned, {errored} err, ${cost:.2f}")
    return results
