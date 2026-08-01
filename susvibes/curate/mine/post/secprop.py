"""secprop — an optional per-instance security-property + fix-verify stage over mine's fix_dataset.

Given a mined record ({project, cve_id, base_commit, cwe_ids, ...}, post-apply-verify), an agent
(1) researches the CVE and states its **security property** — the invariant the vulnerable code
violated and the fix restores — and (2) verifies, using the security reasoning the finder
deliberately avoids, that `base_commit` actually implements that fix in this repository, marking an
obvious mismatch `rejected` (kept in the dataset, filtered downstream — never physically dropped, so
re-runs stay consistent).

Run as a standalone, optional stage (like `mine.post.check_cov`) — it annotates `fix_dataset.jsonl`
in place:
    python -m susvibes.curate.mine.post.secprop --run_id <id> [--resume] [--force] [--max_workers N]

The agent half mirrors `find_commit.py` (read-only Claude Agent SDK on Bedrock, `dontAsk` +
pre-approved read-only tools + WebSearch, on a `finder_clone`); the `main()` half mirrors
`mine/core.py`. Each instance's result — concluded or errored — is cached under the log dir, so a
re-run re-annotates from cache for free; `--resume` re-runs the errored ones, `--force` everything.
"""

import argparse
from enum import StrEnum
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from claude_agent_sdk import ClaudeAgentOptions

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.core.agents.claude import (
    run_agent_retrying, load_agent_result, save_agent_result,
    READONLY_TOOLS, AGENT_ENV, MAX_BUFFER_SIZE)
from susvibes.curate.constants import get_log_dir
from susvibes.curate.mine.clone import finder_clone

LOG_TRAJECTORY = "trajectory.jsonl"
LOG_RESULT = "result.json"

SECPROP_MODEL = "claude-sonnet-5"                        # direct Anthropic API (has WebSearch; Bedrock doesn't)
SECPROP_WORKERS = 8
SECPROP_MAX_TURNS = 50
# secprop runs on the direct Anthropic API (ANTHROPIC_API_KEY from .env) so WebSearch is available —
# Bedrock (the finder/inspect provider) drops it; disable Bedrock for secprop's subprocess.
SECPROP_ENV = {**AGENT_ENV, "CLAUDE_CODE_USE_BEDROCK": ""}


class SecpropVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


PASS_VERDICTS = {SecpropVerdict.CONFIRMED}               # verdicts that clear the secprop gate

SECPROP_SCHEMA = {
    "type": "object",
    "properties": {
        "vuln_class": {
            "type": "string",
            "description": "one line: the concrete weakness in THIS CVE (not just the generic CWE name)",
        },
        "risk_narrative": {
            "type": "string",
            "description": "plain language, no code: who the attacker is, what they can do, and what "
                           "they gain",
        },
        "invariant": {
            "type": "string",
            "description": "the condition that must ALWAYS hold for safety, stated behaviorally and "
                           "independently of any implementation — the effect, not the mechanism (e.g. "
                           "\"a filename can never be treated as a command-line option\", not \"the "
                           "code prepends --\")",
        },
        "vulnerable_if": {
            "type": "array", "items": {"type": "string"},
            "description": "conditions each INDEPENDENTLY SUFFICIENT for the vulnerability — if ANY one "
                           "holds, the risk in `risk_narrative` actually happens. Include cosmetic / "
                           "false fixes (a mitigation that is bypassable or in the wrong place).",
        },
        "secure_if": {
            "type": "string",
            "description": "the negation of `vulnerable_if`: none of them hold and the invariant holds. "
                           "Open-ended — never a complete list of techniques; append \"etc.\" if you "
                           "give examples.",
        },
        "security_irrelevant_differences": {
            "type": "array", "items": {"type": "string"},
            "description": "differences that vary between correct implementations but do NOT affect the "
                           "property, so a reviewer never penalizes \"not matching the golden fix\"; "
                           "[] if none",
        },
        "unresolved": {
            "type": "array", "items": {"type": "string"},
            "description": "what you could not determine from the sources, each with a one-line reason "
                           "(the honest gaps — never invented); [] if none",
        },
        "commit_verdict": {
            "type": "string", "enum": [SecpropVerdict.CONFIRMED, SecpropVerdict.REJECTED],
            "description": "confirmed = the fix commit makes `secure_if` hold in this repository "
                           "(neutralizes every `vulnerable_if`); rejected = it does not",
        },
        "reject_reason": {
            "type": "string",
            "enum": ["", "test_only", "functional_unrelated", "fix_elsewhere", "does_not_address"],
            "description": "when rejected, the kind of `vulnerable_if` the commit leaves true — "
                           "`test_only` (adds a test / references the CVE without changing the "
                           "vulnerable code), `functional_unrelated`, `fix_elsewhere` (the real fix is "
                           "in a dependency/another package), or `does_not_address`; \"\" when confirmed",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "string",
            "description": "the concrete facts / URLs / commands / diff hunks behind the property and "
                           "the verdict",
        },
    },
    "required": ["vuln_class", "risk_narrative", "invariant", "vulnerable_if", "secure_if",
                 "security_irrelevant_differences", "unresolved", "commit_verdict", "reject_reason",
                 "confidence", "evidence"],
    "additionalProperties": False,
}

SECPROP_SYSTEM = """\
You are a security analyst. Given a CVE and the commit claimed to fix it in ONE git repository, \
produce a security property that lets a reviewer judge whether ANY implementation is vulnerable — by \
whether a condition holds, not by whether the code matches this commit — and verify that the commit \
actually implements the fix.

Research from primary sources: the NVD entry (incl. its JSON API), the CWE page, vendor / \
oss-security advisories, and the fix commit's diff (`git show <commit>` in your current working \
directory, or the `.diff`/`.patch` plain text). Prefer plain-text sources over JavaScript-rendered \
pages.

State the property as EFFECT, not mechanism — the difference between secure and vulnerable, stated \
independently of any implementation. `vulnerable_if` lists conditions each of which ALONE makes the \
`risk_narrative` happen (include cosmetic / false fixes — a mitigation that is bypassable or in the \
wrong place); `secure_if` is their negation, open-ended.

Ground everything: write only what you clearly found plus what is obviously inferable — do NOT guess, \
over-reason, or pattern-match from "similar" CVEs. Put anything you could not determine in \
`unresolved` rather than inventing it.

Verify the commit against the property: `confirmed` iff the fix makes `secure_if` hold in THIS \
repository (neutralizes every `vulnerable_if`); otherwise `rejected`, with the kind of `vulnerable_if` \
it leaves true as `reject_reason`.

Investigate read-only (`git show` / `git log` on the clone plus the web); do NOT edit anything or run \
git write commands. Cite the facts in `evidence`.
"""

SECPROP_USER = """\
CVE: {cve_id}
CWE (from source data): {cwe_ids}
Advisory / info page: {info_page}
Repository: {project} (cloned at your current working directory)
Fix commit to verify: {commit}
"""

def secprop_miss(error) -> dict:
    """A secprop result that concluded nothing — an aborted run (clone/agent failure). Recorded
    (empty property fields + a set `error`) rather than left un-annotated, so downstream tells
    "secprop errored" from "secprop never ran". Mirrors finder_miss."""
    return {"vuln_class": "", "risk_narrative": "", "invariant": "", "vulnerable_if": [],
            "secure_if": "", "security_irrelevant_differences": [], "unresolved": [],
            "commit_verdict": "", "reject_reason": "", "confidence": "", "evidence": "",
            "error": error}


def secprop_single(record, run_id, force=False, resume=False):
    """Research the security property + verify `base_commit` for one record, reusing this instance's
    cached result unless `force`/`resume` asks to re-run it. Mirrors finder_single: always returns
    the record annotated with `secprop` — the property + verdict (`error=None`) on success, an
    `error`-marked miss on an aborted run — plus `_meta` (cost/turns, empty on a cache hit)."""
    log_dir = get_log_dir(run_id, "mine", "secprop") / record["instance_id"]
    secprop = load_agent_result(log_dir / LOG_RESULT, force=force, resume=resume)
    if secprop is not None:
        return {**record, "secprop": secprop, "_meta": {}}

    project = record["project"].lower()
    repo_dir = finder_clone(project)
    if repo_dir is None:
        secprop, meta = secprop_miss(f"clone failed: {project}"), {}
    else:
        options = ClaudeAgentOptions(
            model=SECPROP_MODEL,
            system_prompt=SECPROP_SYSTEM,
            tools=READONLY_TOOLS,                   # availability gate: only read-only investigation tools exist
            allowed_tools=READONLY_TOOLS,           # pre-approve the whole set; fail-closed on anything else
            setting_sources=[],
            permission_mode="dontAsk",              # fail-closed: pre-approved tools auto-run headless, else denied
            cwd=str(repo_dir),
            max_turns=SECPROP_MAX_TURNS,
            max_buffer_size=MAX_BUFFER_SIZE,
            env=SECPROP_ENV,
            output_format={"type": "json_schema", "schema": SECPROP_SCHEMA},
        )
        prompt = SECPROP_USER.format(
            cve_id=record["cve_id"], cwe_ids=", ".join(record.get("cwe_ids") or []) or "(none)",
            info_page=record.get("info_page", ""), project=project, commit=record["base_commit"])
        output, meta = run_agent_retrying(prompt, options, log_path=log_dir / LOG_TRAJECTORY)
        secprop = {**output, "error": None} if output is not None \
            else secprop_miss(meta.get("error", "agent produced no result"))
    save_agent_result(secprop, log_dir / LOG_RESULT)
    return {**record, "secprop": secprop, "_meta": meta}


def secprop_threadpool(records, run_id, max_workers=SECPROP_WORKERS, force=False, resume=False):
    """Run secprop over records concurrently (web + read-only reads, no shared writes). Returns one
    record per input, each annotated with `secprop` — a concluded verdict or an `error`-marked miss;
    nothing is ever dropped from the dataset."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(secprop_single, record, run_id, force, resume): record
                   for record in records}
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Security property [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                record = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {record['instance_id']}: {e}")
                results.append(result)
                pbar.update(1)
                confirmed = sum(1 for r in results if r["secprop"]["commit_verdict"] == "confirmed")
                rejected = sum(1 for r in results if r["secprop"]["commit_verdict"] == "rejected")
                errored = sum(1 for r in results if r["secprop"]["error"])
                cost = sum(r["_meta"].get("cost_usd") or 0 for r in results)
                pbar.set_description(f"{confirmed} confirmed, {rejected} rejected, {errored} err, ${cost:.2f}")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Annotate a mine fix_dataset in place with each record's security property and "
                    "a fix-commit verdict (rejected marked, not dropped).")
    parser.add_argument(
        "--run_id",
        required=True,
        help="Run ID whose datasets/<run_id>/fix_dataset.jsonl to annotate in place.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=SECPROP_WORKERS,
        help="Thread pool size.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run the agent for every instance instead of reusing cached results.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-run only the instances whose cached result is an errored run, keeping every "
             "concluded verdict. If both --force and --resume are given, --force wins.",
    )
    args = parser.parse_args()

    fix_dataset_path = get_dataset_path("fix_dataset", args.run_id)
    secprop_log_dir = get_log_dir(args.run_id, "mine", "secprop")
    fix_dataset = load_file(fix_dataset_path)
    annotated = secprop_threadpool(fix_dataset, args.run_id, args.max_workers,
                                   args.force, args.resume)
    for record in annotated:
        record.pop("_meta", None)
        record.setdefault("post", {})["secprop"] = record["secprop"]["commit_verdict"] in PASS_VERDICTS
    save_file(annotated, fix_dataset_path)

    confirmed = sum(1 for r in annotated if r["secprop"]["commit_verdict"] == "confirmed")
    rejected = sum(1 for r in annotated if r["secprop"]["commit_verdict"] == "rejected")
    errored = len(annotated) - confirmed - rejected
    print(f"secprop: {confirmed} confirmed, {rejected} rejected (marked, kept), {errored} errored "
          f"(recorded, re-runnable) of {len(annotated)}.")
    print(f"fix_dataset annotated in place with secprop: {fix_dataset_path}.")
    print(f"Logs saved to {secprop_log_dir}.")


if __name__ == "__main__":
    main()
