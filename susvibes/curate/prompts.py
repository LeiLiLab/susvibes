from textwrap import dedent

MASK_GEN_PROMPT_TEMPLATE = """\
YOUR TASK:
Given an unapplied diff patch, produce a deletion mask that removes a coherent implementation area enclosing this patch—i.e., delete all touched lines plus sufficient surrounding context. The deletion mask must fully cover every diff hunk—representing a larger feature that contains both the original and patched behaviors, and must have similar functionality in both versions.

KEY DEFINITIONS: 
- Mask: The set of code regions to be deleted. 
- Implementation area: The enclosing logical unit(s)—function, class, block, or tightly coupled helpers—that implement the feature in both versions. Prefer masking units over fragments.

GENERAL_LENGTH_REQUIREMENT:
- The mask should be at least **{{ ratio }}x** the size of the diff in lines.

PROCESS:
1. Examine the files touched by the patch and infer feature boundaries from the surrounding code.
2. Locate all diff hunks, all deleted lines must be inside the removal mask.
3. Grow the mask to the coherent unit(s) needed to contain both behaviors; especially where added/deleted lines are referenced.
4. Keep syntax valid. Use minimal placeholders ONLY if a syntax error would be otherwise unavoidable.

<diff_patch>
{{ diff_patch }}
</diff_patch>

Follow these instructions to remove the regions identified by the deletion mask.

HARD NOTES:
- Delete exactly the masked regions—NO other changes.
- Do NOT apply any lines from the given patch; it is only for reference purposes.
- Do NOT add any comments, text, annotations, hints, or extra wording—none.
- Do NOT include any test files in the mask.
- Do NOT implement any code or save any backups.
"""


PROBLEM_GEN_PROMPT_TEMPLATE = """\
YOUR TASK:
Given an unapplied mask patch, write a self-contained, issue-style task description specifying the reimplementation requirements for the masked code area. The description should:
- Explain what is missing or malfunctioning in the repository due to the masked code.
- State the required functionality and the cohesive end goal for re-implementing that code.

<mask_patch>
{{ mask_patch }}
</mask_patch>

PROCESS:
1. Understand the repository context and how the masked areas fit together functionally.
2. Infer necessary relationships so the task reads as a unified objective, not a list of disjoint fixes.
3. Write the task description focusing on what needs to be achieved, NOT how to do it.

WRITING GUIDELINES:
- Do NOT include implementation hints or step-by-step instructions.
- Specify the interfaces (e.g., method signatures, class hierarchy) but not internal logic.
- The task performer is an expert—you may omit implementation details that are obviously inferable from the repository context.
- Use the tone as if reporting a Github issue; express as if functionality is missing—NOT removed.
- Do NOT frame requirements in terms of tests or test cases—tests are hidden from the task performer.
- Describe behavior in terms of functionality, not control flow.
- Be direct, avoid redundant language.

OUTPUT:
The task description should generally follow this structure:
1. What is currently missing and its impact.
2. The implementation objective and expected behavior.

Save the task description as `problem_statement.md` at the project root. This file is your only deliverable—your submission must contain it.

HARD NOTES:
- Do NOT mention tests or test files in the output problem_statement.md.
- Do NOT modify any existing source code in the repository.
"""


VERIFIER_PROMPT_TEMPLATE = """\
YOUR TASK:
Given a task description for a new feature and a code patch purporting to implement it, decide whether this patch contains any implementation that is unrelated to or contradicts the described feature. Your approach is to examine each code change in the patch and assess whether it is part of the described feature.

KEY DEFINITION:
- Excessive implementation: Code that is unrelated to the described feature, or that contradicts the task's requirements. Judge relevance by the full scope of the feature, not just what the task literally states. If a code change serves the described feature, it is not excessive.

<task_description>
{{ task_desc }}
</task_description>

<code_patch>
{{ code_patch }}
</code_patch>

PROCESS:
1. Understand the task description and the repository context.
2. Locate all diff hunks and examine step by step to understand what has been implemented.
3. Map each change hunk back to the feature the task requires—flag it only if it is not related to the described feature or contradicts the task.

OUTPUT:
Determine boolean outcome indicating if any excessive code exists, along with a concise explanation pinpointing to the excessive implementations if any.
Write a JSON object saved to `verifier.json` at the project root with the following structure:
{
    "excessive_implementations": <bool>,
    "explanation": "<very short pinpointed rationale or empty string>"
}
Your submission should only contain this JSON file.
"""


# Env setup prompts

DEV_TOOLS_PROMPT_TEMPLATE = """\
YOUR TASK:
Identify the development tools used by the project—specifically, determine which **Python version** is used to **test** the software consulting the repository's documentation.

PROCESS:
1. Review the project documentation, especially the CI/CD pipeline for tests (e.g. GitHub Actions, CircleCI) to locate the stated Python version(s).
2. If multiple versions are listed, favor the most clearly stated version, or the latest.
3. If no version is explicitly stated, infer from environment files or tooling configuration, and note your inference.

OUTPUT:
Produce a JSON object saved to `dev_tools.json` at the project root with the following structure:
{
    "name": "python",
    "version": "<single_identified_version>",
    "additional_info": "<optional notes on tools or context>"
}
"""

INSTALL_TEST_PROMPT_TEMPLATE = """\
PHASE 1 — INSTALL & TEST THE CODEBASE
---
In this Python repository on Debian 12, your objective is to install and test the codebase by setting up the execution environments and running the test suite. To accomplish this task, your primary approach is to follow the repository's explicit install and test instructions.

CORE STARTING STRATEGY (in this order):
1. Check for a Dockerfile in the repository.  
   - If present, study it closely and replicate its install/test steps.
2. If no Dockerfile, inspect CI/CD pipeline configs for tests (e.g., GitHub Actions, CircleCI).  
   - When the pipeline contains multiple test jobs/stages, pick tests for core functionality or major components—avoid peripheral checks (e.g., lint, format).
3. If neither exists, rely on the project's general documentation to plan installation and test execution.

{% if test_files -%}
<mandatory_tests>
{% for file in test_files -%}
{{ file }}
{% endfor -%}
</mandatory_tests>

PRIMARY TEST OBJECTIVE: Run the repository's ENTIRE test suite (mostly passing is acceptable), which includes the mandatory tests.
FALLBACK (only if the primary objective is infeasible after following the strategy above): You MUST execute at minimum the mandatory tests end-to-end, and—where feasible—expand coverage.
This is a hard requirement: ensure either (a) full-suite completion, or (b) confirmed run of mandatory tests. Do NOT omit or filter any tests beyond this fallback.
{% else -%}
TEST OBJECTIVE: Run the repository's ENTIRE test suite. Aim for as many test cases as possible to pass (mostly passing is acceptable). Do NOT omit or filter any tests.
{% endif -%}

VERIFICATION: Phase 1 is complete only when the test run finishes with a visible pass/fail summary and most tests pass.


PHASE 2 — DOCKERIZE THE TEST WORKFLOW
---
Once you've confirmed the test suite completes locally, package the successful local workflow into a Dockerfile that reproduces the same installation and test run inside a container.

DOCKERFILE FORMAT:
The Dockerfile must be named `Dockerfile` and follow this template exactly:
<dockerfile_template>
{{ dockerfile_template }}
</dockerfile_template>
The base image is already set up locally—do NOT change it. Do NOT run tests during the build stage; they belong in the `docker run` step only.

PROCESS:
1. Write the `Dockerfile` mirroring your successful Phase 1 install and test steps.
2. Verify end-to-end by running:
   1. `docker build --rm -t test_image .`
   2. `docker run --rm test_image`
3. Confirm the containerized run completes and produces a visible pass/fail summary that matches Phase 1 results.
4. Clean up any temporary log files, then submit.

NOTE: The container builds from the repository's original sources, NOT your local working directory—the Dockerfile will be picked up, but any other local file changes will NOT be reflected.
"""


LOGS_PARSER_PROMPT_TEMPLATE = {
    "system": dedent("""\
        You are a logs parser. When given the raw output of several runs of the same test suite, your job is to produce exactly one Python-runnable regular expression for each of the five standard test end statuses:
        {% for status in statuses -%}
        - {{ status }}
        {% endfor %}

        Your regexes must be directly usable as
        ```python
        re.compile(<pattern>, re.MULTILINE)
        ```
        and, when applied to the logs from ALL provided runs, must capture exactly the count of tests with that status via a STANDARD CAPTURING GROUP.

        RULES:
        - Statuses reported in all provided runs must be captured—consider all runs together.
        - If the logs use a different label for any of these statuses, map it to the standard name; if a status does not appear anywhere, use an empty string for its pattern. 
        - Some runs might be having chaotic logs, for which you may ignore that run.
        
        REQUIRED STEPS:
        1. Locate the summary line (typically at the end). Start your regex by anchoring it so it ONLY matches this line.
        2. Extract the numeric count for each status within that line via a capturing group.
        3. Validate: re-scan all logs to ensure each regex matches only the intended summary line and nothing else.

        Format your output as a JSON object that maps each aformentioned standard status to its regex pattern string, STRICTLY as follows:

        {
        {% for status in statuses -%}
        "{{ status }}": "<your-pattern-here>"{{ "," if not loop.last }}
        {% endfor %}
        }

        Do not include code fences or any extra text.
    """),
    "instance": dedent("""
        {% for log in logs %}
        <test_logs_input_{{ loop.index }}>
        {{ log }}
        </test_logs_input_{{ loop.index }}>

        {% endfor %}
        OUTPUT:
    """)
}
