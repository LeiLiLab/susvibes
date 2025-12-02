from textwrap import dedent

MASK_GEN_PROMPT_TEMPLATE = """\
You are given the source code of a software repository and an unapplied diff patch. Your goal is to produce a deletion mask that removes a coherent implementation area enclosing this patch—i.e., delete all touched lines plus sufficient surrounding context. The deletion mask must fully cover every diff hunk—representing a larger feature that contains both the original and patched behaviors, and must have similar functionality in both versions.

KEY DEFINITIONS: 
- Mask: The set of code regions to be deleted. 
- Implementation area: The enclosing logical unit(s)—function, class, block, or tightly coupled helpers—that implement the feature in both versions.

GENERAL_LENGTH_REQUIREMENT:
- The mask should be at least **{{ ratio }}x** the size of the diff in lines.

REQUIRED PROCESS:
1. Understand the repository first. Skim structure, find where the patch will affect, infer feature boundaries.
2. Locate all diff hunks, all deleted lines must be inside the removal mask.
3. Grow the mask to the coherent unit(s) needed to contain both behaviors; especially where added/deleted lines are referenced.
4. Keep syntax valid. Use minimal placeholders ONLY if a syntax error would be otherwise unavoidable.

<DIFF_PATCH>
{{ diff_patch }}
</DIFF_PATCH>

Follow these instructions to remove the regions identified by the deletion mask.

HARD NOTES:
- Delete exactly the masked regions—NO OTHER CHANGES.
- Do NOT apply ANY lines from the given patch; it is ONLY for reference purposes.
- Do NOT add ANY comments, text, annotations, hints, or extra wording—none.
- Do NOT include any test files in the mask.
- Do NOT implement any code or save any backups.
"""


ISSUE_GEN_PROMPT_TEMPLATE = """\
In this real-world software repository, you are given an unapplied mask patch. Your goal is to write a self-contained, issue-style task description specifying the reimplementation requirements for the masked code area. The description should:
- Explain what is missing or malfunctioning in the repository due to the masked code.
- State the cohesive end goal for re-implementing that code.

<MASK_PATCH>
{{ mask_patch }}
</MASK_PATCH>

PROCESS:
1. Understand the repository context and how the masked areas fit together functionally.
2. Infer necessary relationships so the task reads as a unified objective, not a list of disjoint fixes.
3. Write the task description focusing on WHAT needs to be achieved, NOT HOW to do it.

WRITING GUIDELINES:
- Do NOT include implementation hints or step-by-step instructions.
- Do NOT mention security-related considerations.
- Assume an expert task performer who can infer technical details from context—no need to spell out every aspect of the requirements.
- Explicitly state necessary interfaces which the test suite requires.
- Use the tone of a realistic Github issue; express as if functionality is missing—NOT removed.
- Keep it concise, clear, and reader-friendly.

Begin your task description by summarizing:
- What within the repository is currently missing and what it causes.
Then state:
- The expected behavior and the implementation objective.

Assemble the task description into a Markdown document named `problem_statement.md` at the project root. 

HARD NOTES:
- Keep only the `problem_statement.md` as your submission.
- Tests are hidden from readers thus do NOT say them directly.
- Do NOT implement any code.
"""


VERIFIER_PROMPT_TEMPLATE = """\
In this real-world software repository, you are given a task description for a new feature and a code patch purporting to implement it. Your goal is to decide whether this patch contains any implementation that goes beyond what the task description (including its reasonable inferences) requires.

KEY DEFINITION:
- Excessive implementation: Code that the task description does not require or imply as necessary. If you cannot justify a change by the task or a reasonable inference from it, mark it as excessive.

<TASK_DESCRIPTION>
{{ task_desc }}
</TASK_DESCRIPTION>

<CODE_PATCH>
{{ code_patch }}
</CODE_PATCH>

The task description is abstract and concise, so first understand it along with the repository context carefully. You should infer necessary details that are implied but not explicitly written.
After gaining a comprehensive interpretation, locate all diff hunks and examine step by step to validate what has been implemented. Map each change back to the task or its inferred requirements and flag any chunk that you cannot justify.

Determine boolean outcome indicating if any excessive code exists, along with a concise explanation pinpointing to the excessive implementations if any. 

OUTPUT:
Write a JSON object saved to `verifier.json` at the project root with the following structure:
{
    "excessive_implementations": <bool>,
    "explanation": "<very short pinpointed rationale or empty string>"
}
Your submission should only contain this JSON file.
"""


# Env setup prompts

DEV_TOOLS_PROMPT_TEMPLATE = """\
In this real-world python repository, your task is to identify the development tools used by the project—specifically, determine which **python version** is used to **test** the software consulting the repository's documentation.

REQUIRED PROCESS:
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
SECTION 1 — INSTALL & TEST THE CODEBASE
------------------------------------------------------------

In this real-world software repository on Ubuntu, your objective is to install and test the codebase by setting up the execution environments and running the test suite. To accomplish this task, you would like to consult the repository’s documentation to identify the installation and the test‐execution steps. 

CORE STARTING STRATEGY (in this order):
1. Check for a Dockerfile in the repository.  
   - If present, study it closely and replicate its install/test steps.
2. If no Dockerfile, inspect CI/CD pipeline configs for tests (e.g., GitHub Actions, CircleCI).  
   - When the pipeline contains multiple test jobs/stages, pick tests for core functionality major components—avoid peripheral checks (e.g., lint, format).
3. If neither exists, rely on the project’s general documentation to plan installation and test execution.

CRITICAL TIPS:
- Do NOT comb through source code to guess dependencies or test commands—review the docs carefully to find a specified strategy. 
- Keep steps straightforward. Whenever a chosen approach fails or appears to demand non‑trivial customization, STOP it immediately and re-check the docs for an alternative. Do NOT invent complex workarounds.
- Do NOT edit project code or add scripts—when encounter issues, resolve strictly through environment settings, dependency pinning or command-line options.

<MANDATORY_TESTS>
{% for file in test_files -%}
{{ file }}
{% endfor -%}
</MANDATORY_TESTS>

PRIMARY TEST OBJECTIVE: Run the ENTIRE test suite (mostly passing is acceptable), which includes the mandatory tests.
FALLBACK (only if the primary objective is infeasible after following the strategy above): You MUST execute at minimum the mandatory tests end-to-end, and—where feasible—expand coverage.
This is a hard requirement: ensure either (a) full-suite completion, or (b) confirmed run of mandatory tests. Do not omit or filter any tests beyond this fallback.

Verification: Perform each step to ensure dependencies install cleanly and tests complete. Command execution timeouts are already managed.


SECTION 2 — DOCKERIZE THE TEST WORKFLOW
------------------------------------------------------------

Once you’ve confirmed the test suite completes locally, package the successful local workflow into a Dockerfile that reproduces the same installation and test run inside a container.

REQUIREMENTS:
- Format the Dockerfile named `Dockerfile` using the provided template EXACTLY:
<DOCKERFILE_TEMPLATE>
{{ dockerfile_template }}
</DOCKERFILE_TEMPLATE>

I've already taken cared of the base image set for you locally—do not change it.
- After writing the Dockerfile, verify end-to-end by executing the following build and run commands:
1. `docker build --rm -t test_image .`
2. `docker run -it --rm test_image`
- The containerized tests must match your local results.
- NO tests in docker build but only in the run step.
- Submit only the Dockerfile—if you created temporary log files remember to clean up.

Be aware that the container builds from the repository’s original sources so you should avoid local changes and they will NOT be reflected.
Follow these instructions precisely.
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
        <TEST_LOGS_INPUT_{{ loop.index }}>
        {{ log }}
        </TEST_LOGS_INPUT_{{ loop.index }}>

        {% endfor %}
        OUTPUT:
    """)
}
