"""sec_prop — an optional per-instance security-property + fix-verify stage over mine's dataset.

Given a mined record ({project, cve_id, base_commit, cwe_ids, ...}, post-apply-verify), two agent
phases run back to back:

  Phase 1 (property)      — research the CVE from the public record and state its security
                            property: the invariant, the attacker's entry points, and the attack
                            variants that break it. Web only, no repository and no git.
  Phase 2 (verification)  — only now read `git show <base_commit>` on a blobless clone, and rule
                            on one question: is this commit this CVE's fix? It confirms by
                            default and rejects only against a fixed list — no code change, test
                            only, unrelated component, not this repo, superseded by a later fix.
                            Reading the diff and judging that an attacker could still get around
                            it is not on the list, and grounds no verdict here.

The split is by AUDIENCE, not by information. `property` is what test-gen receives; `verification`
answers mine's gate and never reaches it — which is what keeps a sentence like "upstream added a
test for this" out of a prompt whose tree has had exactly those tests stripped.

It buys no isolation from the fix itself: measured on 30 instances, Phase 1 reaches the commit's
diff page over the web every single time (the advisories link straight to it), and that is where
73% of with_test payloads come from. Dropping Bash moved the path, not the access. So the guard
against an implementation-shaped property — the defect that made synthesized tests fail on
equally-secure implementations — is `generality_selfcheck`, and it is the only one. Phase 2 stays
narrow for a different reason: it may write BACK a payload (`payload_updates`), never the
property's shape. See docs/sec_prop/redesign-v3.md.

Run as a standalone, optional stage (like `mine.post.check_cov`), after `test_mask` so its
rejections never withhold records from that mandatory stage — it annotates `dataset.jsonl`
in place:
    python -m susvibes.curate.mine.post.sec_prop --run_id <id> [--resume] [--force] [--max_workers N]

The agent half mirrors `find_commit.py` (read-only Claude Agent SDK, `dontAsk` + pre-approved
tools), on the direct Anthropic API rather than the other stages' Bedrock — WebSearch is why, see
`SEC_PROP_ENV`; the `main()` half mirrors `mine/core.py`. Each phase caches its own report under
the log dir, so `--resume` re-runs only the phase that errored — a Phase 2 failure never re-spends
Phase 1's research.
"""

import argparse
import json
from enum import StrEnum
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from claude_agent_sdk import ClaudeAgentOptions

from susvibes.core.constants import get_dataset_path
from susvibes.core.utils import load_file, save_file
from susvibes.core.agents.claude import (
    run_agent_retrying, READONLY_TOOLS, AGENT_ENV, MAX_BUFFER_SIZE)
from susvibes.core.report import (
    reuse_report, save_report, strip_bookkeeping, get_report_summary, print_summary)
from susvibes.curate.constants import KeepStage, get_log_dir
from susvibes.curate.utils import should_keep
from susvibes.curate.mine.clone import blobless_clone

LOG_PROPERTY = "property.json"
LOG_VERIFICATION = "verification.json"
LOG_REPORT = "report.json"
LOG_PROPERTY_TRAJECTORY = "property.jsonl"
LOG_VERIFY_TRAJECTORY = "verification.jsonl"
LOG_SUMMARY = "summary.json"

KEEP_STAGE = KeepStage.SEC_PROP   # this stage's verdict under record["keep"]

SEC_PROP_MODEL = "claude-sonnet-5"                        # direct Anthropic API — see SEC_PROP_ENV
SEC_PROP_WORKERS = 16
MIN_STATEMENT_LEN = 40        # below this an invariant/observable is placeholder text, not a claim
PROPERTY_MAX_TURNS = 40       # research: many searches
VERIFY_MAX_TURNS = 30         # the diff to read, then sources to check on any residual variant
# Phase 1 researches from the public record only; giving it no Bash and no cwd is what keeps the
# fix's implementation out of the property (see the module docstring).
PROPERTY_TOOLS = ["WebSearch", "WebFetch"]
# sec_prop must reach the web — grounding the property in the CVE's primary sources IS the job — and
# WebSearch is a tool Anthropic runs server-side, which Amazon Bedrock does not expose. It does not
# fail loudly: the CLI hands the model an empty tool list and the model answers from memory, which
# reads exactly like a researched property. The provider is chosen by environment alone (no model id
# or ClaudeAgentOptions field selects it), so unset Bedrock for sec_prop's subprocess.
SEC_PROP_ENV = {**AGENT_ENV, "CLAUDE_CODE_USE_BEDROCK": ""}


class SecPropVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


PASS_VERDICTS = {SecPropVerdict.CONFIRMED}               # verdicts that clear the sec_prop gate

PROPERTY_SCHEMA = {
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
        "attack_surface": {
            "type": "array", "items": {"type": "string"},
            "description": "the entry points the attacker actually controls: who reaches what "
                           "interface and which value they supply (an HTTP route, a CLI argument, a "
                           "config field, a file format). Name internal functions only when they are "
                           "themselves the public API.",
        },
        "attack_variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {
                        "type": "string",
                        "description": "which entry from `attack_surface` this variant goes through",
                    },
                    "observable": {
                        "type": "string",
                        "description": "what is OBSERVABLE from outside when this variant succeeds — a "
                                       "concrete assertable phenomenon (a file appears at a path, a "
                                       "response header is absent, a string reaches the output), never "
                                       "\"the code executes X\". Required.",
                    },
                    "payload": {
                        "type": "string",
                        "description": "the concrete malicious input, verbatim. LEAVE EMPTY if you did "
                                       "not find a real one — never invent one to fill the field.",
                    },
                    "payload_source": {
                        "type": "string",
                        "description": "where `payload` came from: a URL, or the literal \"inferred\" "
                                       "when you constructed it yourself; \"\" when payload is empty",
                    },
                },
                "required": ["surface", "observable", "payload", "payload_source"],
                "additionalProperties": False,
            },
            "description": "the DIFFERENT WAYS an attacker reaches the same outcome — variants of the "
                           "attack, never descriptions of what a defence gets wrong. One entry per way "
                           "that is independently sufficient to break the invariant.",
        },
        "security_irrelevant_differences": {
            "type": "array", "items": {"type": "string"},
            "description": "differences that vary between correct implementations but do NOT affect the "
                           "property, so a reviewer never penalizes \"not matching the golden fix\" "
                           "(exception type, log level, error wording, which layer rejects); [] if none",
        },
        "generality_selfcheck": {
            "type": "array", "items": {"type": "string"},
            "description": "proof that each statement judges the ATTACK, not one way of stopping it. "
                           "One entry for `invariant` and one per attack_variant `observable`, each "
                           "\"<the statement> | alternative defence: <one concrete way a maintainer "
                           "could have fixed this that you did NOT assume and that you consider "
                           "secure> -> would / would not flag it, because ...\". A statement that "
                           "flags a secure implementation has encoded one particular fix, and would "
                           "make downstream tests fail on every other correct one: rewrite it BEFORE "
                           "submitting, and record the rewritten form here.",
        },
        "unresolved": {
            "type": "array", "items": {"type": "string"},
            "description": "what the public record did not settle, each with a one-line reason — and "
                           "one entry for EVERY variant whose `payload` you left empty, saying what you "
                           "searched and what was missing; [] only if nothing was left open",
        },
    },
    "required": ["vuln_class", "risk_narrative", "invariant", "attack_surface", "attack_variants",
                 "security_irrelevant_differences", "generality_selfcheck", "unresolved"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "commit_verdict": {
            "type": "string", "enum": [SecPropVerdict.CONFIRMED, SecPropVerdict.REJECTED],
            "description": "confirmed unless the commit matches one of `reject_reason`'s "
                           "categories — this stage asks whether the commit is ABOUT this CVE, not "
                           "whether the fix is complete. Partial fixes, unsafe defaults left in "
                           "place, fixes split across commits: all confirmed. Undecidable: confirmed",
        },
        "reject_reason": {
            "type": "string",
            "enum": ["", "no_code_change", "test_only", "unrelated_change", "not_this_repo",
                     "superseded"],
            "description": "why this commit is not this CVE's fix, \"\" when confirmed. "
                           "`no_code_change` (version bumps / release notes / docs / lockfiles / CI "
                           "only), `test_only` (tests changed, code under test untouched), "
                           "`unrelated_change` (nothing in the diff changes behaviour on this "
                           "CVE's attack path — another component, or a pure refactor), "
                           "`not_this_repo` (no vulnerable code here "
                           "— the remedy is in a dependency, or this is a PoC/exploit repo), "
                           "`superseded` (a later commit in this history reworks the same code and "
                           "says it fixes the same CVE or a bypass of this attempt, or a public "
                           "source names this commit insufficient). Never reject on your own "
                           "reading that an attacker could still get around the change",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "string",
            "description": "what THIS COMMIT'S DIFF does, plus — when rejecting — the objective "
                           "ground: which files changed, which component the CVE names, why they do "
                           "not meet. Never "
                           "assert what the repository currently contains — no \"the repo has/lacks "
                           "test X\", no claims about files you did not open. To cite a test, say "
                           "\"upstream added ... at <ref>\" and name the source.",
        },
        "unresolved": {
            "type": "array", "items": {"type": "string"},
            "description": "what you could not determine, each with a one-line reason — including why "
                           "any payload was left empty; [] if none",
        },
        "payload_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "variant_index": {"type": "integer"},
                    "payload": {"type": "string"},
                    "payload_source": {"type": "string"},
                },
                "required": ["variant_index", "payload", "payload_source"],
                "additionalProperties": False,
            },
            "description": "the ONLY thing you may write back into the property, and only from one "
                           "kind of source: an input copied VERBATIM from a regression test added for "
                           "THIS CVE by this commit or one of its immediate parents, for a variant "
                           "that had no payload or a weaker one. Never infer "
                           "or construct one here; [] if no such test exists. `payload_source` names "
                           "it: file, test function, and the commit it landed in. You cannot change a "
                           "variant's surface or observable — if the property itself looks wrong, say "
                           "so in `unresolved` and lower `confidence`.",
        },
    },
    "required": ["commit_verdict", "reject_reason", "confidence", "evidence",
                 "unresolved", "payload_updates"],
    "additionalProperties": False,
}

PROPERTY_SYSTEM = """\
You are a security analyst. Given a CVE, state a security property that lets a reviewer judge \
whether ANY implementation is vulnerable — by whether an attack still succeeds, not by whether the \
code resembles some particular fix.

Research the public record: the NVD entry (incl. its JSON API), the CWE page, the GitHub Security \
Advisory, vendor / oss-security advisories, and the reporter's write-up. Prefer plain-text sources \
over JavaScript-rendered pages.

Those trails lead to the fix commit, and you will end up reading its diff. That is useful for ONE \
thing — lifting concrete inputs (see payloads below) — and disqualifying for everything else. The \
diff shows how ONE project chose to fix this; the property must judge implementations that chose \
differently. So never carry the fix's shape into your statements: the moment you describe the check \
it added, tests built on your property will fail every other correct implementation.

State EFFECT, never MECHANISM. The invariant is what must remain true for the user, not what the \
code should do: "a filename can never be treated as a command-line option", not "the code prepends \
--". If a sentence names a defence technique (an allow-list, escaping, a validator, an exception), \
it is the wrong level — rewrite it as the attack that must not succeed.

`attack_variants` are the DIFFERENT WAYS AN ATTACKER REACHES THE SAME OUTCOME — a second entry \
point, another input encoding, a different value that reaches the same sink. They are NOT a list of \
things a defence might get wrong. Each variant must be independently sufficient to break the \
invariant, and each needs an `observable`: the concrete thing you could check from outside to know \
it succeeded.

Ground every payload. This CVE is already fixed upstream, so the input that triggered it is on the \
record — prefer, in order: an input from the regression test upstream added for this CVE, a \
reproduction step in the advisory, an input value the reporter's write-up quotes. Record where it \
came from in `payload_source`. If you found none, LEAVE `payload` EMPTY and add an \
`unresolved` entry naming what you searched and what was missing — an empty payload costs you \
nothing, an invented one is a defect. The same rule governs everything else: write only what you \
clearly found plus what is obviously inferable; do not guess, over-reason, or pattern-match from \
"similar" CVEs, and put whatever the public record left open in `unresolved`.

Before submitting, run `generality_selfcheck`. You have just read how this project fixed it, so this \
is the step that keeps that out of your statements — not a formality. For the invariant and for each \
variant's observable, first NAME a concrete alternative defence a maintainer could plausibly have chosen instead of the \
obvious one — a block-list where you assumed an allow-list, dropping the offending element where you \
assumed escaping, rejecting at a different layer, or a redesign in which the dangerous value never \
reaches the sink at all — and only then ask whether your statement would judge THAT implementation \
vulnerable. Two rules make this check real: the alternative must be one you did NOT already have in \
mind, and it must be one you judge genuinely SECURE. Condemning a weak alternative proves nothing — \
the question is whether your statement would wrongly condemn a different but sound defence. If it \
would, the statement has encoded one particular defence; rewrite it as the attack that must not \
succeed.
"""

PROPERTY_USER = """\
CVE: {cve_id}
CWE (from source data, may be wrong — say so if the sources disagree): {cwe_ids}
Advisory / info page: {info_page}
Affected project: {project}
"""

VERIFY_SYSTEM = """\
You are a security analyst ruling on one thing about a commit in ONE git repository: is it this \
CVE's fix?

Confirm it unless one of the following holds. These are the only grounds for rejecting; name which \
one in `reject_reason`:

  no_code_change    the diff carries no source change at all — version bumps, release notes, docs,
                    lockfiles or CI config only
  test_only         it changes tests without touching the code under test
  unrelated_change  nothing in the diff changes behaviour on this CVE's attack path — it touches a
                    different component, or it is a rename/move/refactor that leaves behaviour as
                    it was
  not_this_repo     this repository has no vulnerable code to change: the remedy lives in a
                    dependency, or this repo is a PoC/exploit rather than the software being fixed
  superseded        a later commit in this history reworks the same code and states, in its message
                    or PR, that it fixes the same CVE or a bypass of this attempt — `git log` finds
                    this, look here first; or, only if that is silent, a public source names this
                    commit insufficient or points at a different one

Your own reading that an attacker could still get around the change is not one of these grounds. A \
partial fix, an unsafe default left in place, one commit out of a fix spread over several — all \
`confirmed`, so long as this commit itself changes behaviour; so is anything the list does not \
cover.

The security property below describes what the CVE is about. Use it to tell whether the changed \
code belongs to that component — not to grade the fix.

Investigate read-only — `git show <commit>` / `git log` in your current working directory, and the \
web only where a category above calls for it. Do NOT edit anything or run git write commands.

`evidence` records what THIS COMMIT'S DIFF does, and — when you reject — the objective ground: which \
files it changed, which component the CVE names, why the two do not meet. Never assert what the \
repository currently contains: no "the repo has/lacks test X", no claims about files you did not \
open. To cite a test, write "upstream added ... at <ref>" and name the source.

You may write back to the property in exactly one way: `payload_updates`, and from exactly one kind \
of source — a regression test added for THIS CVE by this commit or by one of its immediate parents \
(upstream often splits the fix and its test across a merge, or lands the test one commit ahead). \
Copy the input out of that test verbatim. Never infer, construct, or adapt a payload in this phase: \
if no such test exists, write back nothing. You \
cannot change a variant's surface or observable. If the property itself looks wrong, do not work \
around it — say so in `unresolved` and lower `confidence`.
"""

VERIFY_USER = """\
CVE: {cve_id}
Repository: {project} (bare clone at your current working directory)
Commit claimed to fix it: {commit}

Security property established in phase 1:
{property_json}
"""


def sec_prop_miss(error) -> dict:
    """A sec_prop result that concluded nothing — an aborted run (clone/agent failure). Recorded
    (empty fields + a set `error`) rather than left un-annotated, so downstream tells "sec_prop
    errored" from "sec_prop never ran". Mirrors finder_miss."""
    return {"property": {}, "verification": {}, "commit_verdict": "", "error": error}


def placeholder_reason(prop) -> str | None:
    """The one thing the schema cannot check: that the statements say anything. A model that keeps
    failing structured output starts probing it with placeholders ('invariant': 'test'), and such a
    property is schema-valid but useless downstream. Returns why it is unusable, or None."""
    if len(prop["invariant"]) < MIN_STATEMENT_LEN:
        return f"invariant is placeholder text: {prop['invariant']!r}"
    for i, variant in enumerate(prop["attack_variants"]):
        if len(variant["observable"]) < MIN_STATEMENT_LEN:
            return f"attack_variants[{i}].observable is placeholder text: {variant['observable']!r}"
    return None


def run_property_phase(data_record, log_dir, force, resume) -> tuple[dict | None, dict]:
    """Phase 1: research the CVE from public sources only and state its security property. No
    repository access — that isolation is what keeps the fix's implementation out of the property.
    Returns (property, meta); property is None when the phase failed."""
    cached = reuse_report(log_dir / LOG_PROPERTY, force=force, resume=resume)
    if cached is not None:
        return (None if cached.get("error") else strip_bookkeeping(cached, drop=("error",))), \
               {**(cached.get("meta") or {}), "reused": True}

    options = ClaudeAgentOptions(
        model=SEC_PROP_MODEL,
        system_prompt=PROPERTY_SYSTEM,
        tools=PROPERTY_TOOLS,                   # availability gate: no Bash, so no diff to read
        allowed_tools=PROPERTY_TOOLS,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=PROPERTY_MAX_TURNS,
        max_buffer_size=MAX_BUFFER_SIZE,
        env=SEC_PROP_ENV,
        output_format={"type": "json_schema", "schema": PROPERTY_SCHEMA},
    )
    prompt = PROPERTY_USER.format(
        cve_id=data_record["cve_id"],
        cwe_ids=", ".join(data_record.get("cwe_ids") or []) or "(none)",
        info_page=data_record.get("info_page", ""), project=data_record["project"].lower())
    output, meta = run_agent_retrying(prompt, options, log_path=log_dir / LOG_PROPERTY_TRAJECTORY)
    if output and (reason := placeholder_reason(output)):
        output, meta = None, {**meta, "error": reason}
    save_report({**(output or {}), "error": None if output else meta.get("error", "no result"),
                 "meta": meta}, log_dir / LOG_PROPERTY)
    return output, meta


def run_verify_phase(data_record, prop, repo_dir, log_dir, force, resume) -> tuple[dict | None, dict]:
    """Phase 2: read the fix commit and judge whether it neutralizes every attack variant in `prop`.
    Returns (verification, meta); verification is None when the phase failed."""
    cached = reuse_report(log_dir / LOG_VERIFICATION, force=force, resume=resume)
    if cached is not None:
        return (None if cached.get("error") else strip_bookkeeping(cached, drop=("error",))), \
               {**(cached.get("meta") or {}), "reused": True}

    options = ClaudeAgentOptions(
        model=SEC_PROP_MODEL,
        system_prompt=VERIFY_SYSTEM,
        tools=READONLY_TOOLS,
        allowed_tools=READONLY_TOOLS,
        setting_sources=[],
        permission_mode="dontAsk",
        cwd=str(repo_dir),
        max_turns=VERIFY_MAX_TURNS,
        max_buffer_size=MAX_BUFFER_SIZE,
        env=SEC_PROP_ENV,
        output_format={"type": "json_schema", "schema": VERIFY_SCHEMA},
    )
    prompt = VERIFY_USER.format(
        cve_id=data_record["cve_id"], project=data_record["project"].lower(),
        commit=data_record["base_commit"],
        property_json=json.dumps(prop, indent=2, ensure_ascii=False))
    output, meta = run_agent_retrying(prompt, options, log_path=log_dir / LOG_VERIFY_TRAJECTORY)
    save_report({**(output or {}), "error": None if output else meta.get("error", "no result"),
                 "meta": meta}, log_dir / LOG_VERIFICATION)
    return output, meta


def merge_payload_updates(prop: dict, updates: list) -> dict:
    """Apply phase 2's payloads onto the property — the one channel by which verification may write
    back. Out-of-range indices are dropped: a hallucinated index must not silently grow the
    variant list."""
    variants = [dict(v) for v in prop.get("attack_variants", [])]
    for update in updates or []:
        index = update.get("variant_index")
        if isinstance(index, int) and 0 <= index < len(variants) and update.get("payload"):
            variants[index]["payload"] = update["payload"]
            variants[index]["payload_source"] = update.get("payload_source", "")
    return {**prop, "attack_variants": variants}


def sec_prop_single(data_record, run_id, force=False, resume=False) -> dict:
    """Research the security property (phase 1) and verify `base_commit` against it (phase 2) for
    one data_record, reusing each phase's cached report unless `force`/`resume` asks to re-run it.
    Always returns the report — property + verdict (`error=None`), or an `error`-marked miss —
    plus `meta` (this run's cost/turns, summed over the phases that actually ran)."""
    log_dir = get_log_dir(run_id, "mine", "sec_prop") / data_record["instance_id"]
    report = reuse_report(log_dir / LOG_REPORT, force=force, resume=resume)
    if report is not None:
        return report

    def spend(*metas):
        """This run's cost — a phase served from cache was paid for by an earlier run."""
        ran = [m for m in metas if m and not m.get("reused")]
        return {"cost_usd": sum(m.get("cost_usd") or 0 for m in ran),
                "num_turns": sum(m.get("num_turns") or 0 for m in ran)}

    prop, prop_meta = run_property_phase(data_record, log_dir, force, resume)
    if prop is None:
        report = sec_prop_miss(f"property phase failed: {prop_meta.get('error', 'no result')}")
        report["meta"] = spend(prop_meta)
        save_report(report, log_dir / LOG_REPORT)
        return report

    project = data_record["project"].lower()
    repo_dir = blobless_clone(project)
    if repo_dir is None:
        report = sec_prop_miss(f"clone failed: {project}")
        report["meta"] = spend(prop_meta)
        save_report(report, log_dir / LOG_REPORT)
        return report

    verification, verify_meta = run_verify_phase(data_record, prop, repo_dir, log_dir, force, resume)
    if verification is None:
        report = sec_prop_miss(f"verify phase failed: {verify_meta.get('error', 'no result')}")
        report["meta"] = spend(prop_meta, verify_meta)
        save_report(report, log_dir / LOG_REPORT)
        return report

    report = {
        "property": merge_payload_updates(prop, verification.get("payload_updates")),
        "verification": strip_bookkeeping(verification, drop=("commit_verdict", "payload_updates")),
        "commit_verdict": verification["commit_verdict"],   # the stage's own status, at the top
        "error": None,
        "meta": spend(prop_meta, verify_meta),
    }
    save_report(report, log_dir / LOG_REPORT)
    return report


def sec_prop_threadpool(dataset, run_id, max_workers=SEC_PROP_WORKERS, force=False, resume=False) -> dict:
    """Run sec_prop over dataset concurrently (web + read-only reads, no shared writes), annotating
    each record in place with `sec_prop` + its `keep` verdict. Returns the reports; nothing is ever
    dropped from the dataset."""
    reports = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(sec_prop_single, record, run_id, force, resume): record
                   for record in dataset}
        with tqdm(total=len(futures), dynamic_ncols=True,
            desc=f"Security property [{max_workers} threads]") as pbar:
            for future in as_completed(futures):
                record = futures[future]
                try:
                    report = future.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {record['instance_id']}: {e}")
                reports[record["instance_id"]] = report
                # The dataset are the caller's own dataset entries, so annotating here is what puts
                # the verdict in the dataset — the report is only its cache.
                record[KEEP_STAGE] = strip_bookkeeping(report)
                record.setdefault("keep", {})[KEEP_STAGE] = \
                    report["commit_verdict"] in PASS_VERDICTS
                pbar.update(1)
                confirmed = sum(1 for r in reports.values() if r["commit_verdict"] == "confirmed")
                rejected = sum(1 for r in reports.values() if r["commit_verdict"] == "rejected")
                errored = sum(1 for r in reports.values() if r["error"])
                cost = sum((r.get("meta") or {}).get("cost_usd") or 0
                           for r in reports.values() if not r.get("reused"))
                pbar.set_description(f"{confirmed} confirmed, {rejected} rejected, {errored} err, ${cost:.2f}")
    return reports


def main():
    parser = argparse.ArgumentParser(
        description="Annotate a mine dataset in place with each record's security property and "
                    "a fix-commit verdict (rejected marked, not dropped).")
    parser.add_argument(
        "--run_id",
        required=True,
        help="Run ID whose datasets/<run_id>/dataset.jsonl to annotate in place.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        default=SEC_PROP_WORKERS,
        help="Thread pool size.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run both phases for every instance instead of reusing cached reports.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-run only what errored, per phase: a cached property is kept when only the verify "
             "phase failed. If both --force and --resume are given, --force wins.",
    )
    parser.add_argument(
        "--instance_ids",
        type=json.loads,
        default=None,
        help="Only run for the given instance IDs (JSON list).",
    )
    args = parser.parse_args()

    dataset_path = get_dataset_path("dataset", args.run_id)
    sec_prop_log_dir = get_log_dir(args.run_id, "mine", "sec_prop")
    dataset = load_file(dataset_path)
    gated = [data_record for data_record in dataset
             if should_keep(data_record, exclude=KEEP_STAGE)]
    if args.instance_ids is not None:
        gated = [r for r in gated if r["instance_id"] in set(args.instance_ids)]
    reports = sec_prop_threadpool(gated, args.run_id, args.max_workers, args.force, args.resume)
    save_file(dataset, dataset_path)

    summary = get_report_summary(reports, "commit_verdict")
    summary_path = sec_prop_log_dir / LOG_SUMMARY
    save_report(summary, summary_path)
    print_summary(summary)
    print(f"dataset annotated in place with sec_prop: {dataset_path}.")
    print(f"Logs saved to {sec_prop_log_dir}.")


if __name__ == "__main__":
    main()
