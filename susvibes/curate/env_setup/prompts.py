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

{% if coverage_files -%}
COVERAGE HINT — your starting point is still the repo's normal test flow. Two moments to apply this:
- If you must narrow the test scope, prefer subsets that exercise: {{ coverage_files | join(', ') }}
- Before finalizing your test command, verify those files are touched. If not, broaden the command to include tests that do.
  - If no existing tests touch them at all, document it and use your broadest workable command.

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
- The base image is already set up locally—do NOT change it.
- Do NOT run tests during the build stage; they belong in the `docker run` step only.
- The CMD must finish within a single foreground command run (no `timeout` or similar wrappers in CMD).

PROCESS:
1. Write the `Dockerfile` mirroring your successful Phase 1 install and test steps.
2. Verify end-to-end by running:
   1. `docker build --rm -t test_image .`
   2. `docker run --rm test_image`
3. Confirm the containerized run completes and produces a visible pass/fail summary that matches Phase 1 results.
4. Clean up any temporary log files, then submit.

NOTE: The container builds from the repository's original sources, NOT your local working directory—the Dockerfile will be picked up, but any other local file changes will NOT be reflected.
"""
