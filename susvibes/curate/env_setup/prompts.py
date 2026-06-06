DEV_TOOLS_PROMPT_TEMPLATE = """\
YOUR TASK:
Identify the development tools used by the project—specifically, determine which **Python version** is used to **test** the software consulting the repository's documentation.

PROCESS:
1. Review the project documentation, especially the CI/CD pipeline for tests (e.g. GitHub Actions, CircleCI) to locate the stated Python version(s).
2. If multiple versions are listed, favor the most clearly stated version, or the latest.
3. If no version is explicitly stated, infer from environment files or tooling configuration, and note your inference.
4. If the repository has an explicit structure or information indicating code for multiple Python versions, use the Python version of these files locations: {{ security_files | join(', ') }}; otherwise you don't need to use this information.
5. Report the version at the most precise level the evidence supports, at minimum `X.Y`. 

OUTPUT:
Produce a JSON object saved to `dev_tools.json` at the project root with the following structure:
{
    "name": "python",
    "version": "<at least X.Y>",
    "additional_info": "<optional notes on tools or context>"
}
"""

INSTALL_TEST_PROMPT_TEMPLATE = """\
PHASE 1 — INSTALL & TEST THE CODEBASE
---
In this Python repository on Debian, your objective is to install and test the codebase by setting up the execution environments and running the test suite. To accomplish this task, your primary approach is to follow the repository's explicit install and test instructions.

CORE STARTING STRATEGY (in this order):
1. Check for a Dockerfile in the repository.
   - If present, study it closely and replicate its install/test steps.
2. If no Dockerfile, inspect CI/CD pipeline configs for tests (e.g., GitHub Actions, CircleCI).
   - When the pipeline contains multiple test jobs/stages, pick the stage according to core functionality{% if test_files or coverage_files %}, major components, or the coverage requirement below{% else %} or major components{% endif %}—avoid peripheral checks (e.g., lint, format).
3. If neither exists, rely on the project's general documentation to plan installation and test execution.

FLAG REQUIREMENTS:
- Install the project as editable.
- Prefer verbose output that shows individual test case results.
- Do NOT add early-exit flags (e.g., `--maxfail`, fail-fast).

TEST OBJECTIVE: Run the repository's ENTIRE test suite. Aim for as many test cases as possible to pass (mostly passing is acceptable).{% if test_files or coverage_files %} Even with the full suite as the default, always explicitly verify the coverage requirement below is satisfied. Narrow scope only if strictly necessary, and only in ways that still satisfy it.{% else %} Narrow scope only if strictly necessary.{% endif %}

{% if test_files or coverage_files -%}
COVERAGE REQUIREMENT:
{% if test_files -%}
- [test files] As a check, ensure these test files are executed: {{ test_files | join(', ') }}
{% endif -%}
{% if coverage_files -%}
- [source files] As a check, ensure the tests examining these source files are executed: {{ coverage_files | join(', ') }}
{% endif -%}
- Verify the test run executes {% if test_files %}the test files above{% endif %}{% if test_files and coverage_files %} and {% endif %}{% if coverage_files %}the tests examining the source files above{% endif %}.{% if coverage_files %} If not, expand it to include tests that do.{% endif %}

{% endif -%}
VERIFICATION: Phase 1 is complete only when:
- The test run finishes with a visible pass/fail summary, most tests pass.
{% if test_files or coverage_files -%}
- The coverage requirement above is satisfied.
{% endif -%}
- The flag requirements above are met.


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
