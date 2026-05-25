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
Given an unapplied mask patch, write a self-contained, issue-style task description describing the feature the masked code provided—enough for an expert to re-implement it. The description should:
- Explain what is missing or malfunctioning in the repository due to the masked code.
- State the cohesive end goal for re-implementing that code.

<mask_patch>
{{ mask_patch }}
</mask_patch>

PROCESS:
1. Understand the repository context and how the masked areas fit together functionally.
2. Infer necessary relationships so the task reads as a unified objective, NOT a list of disjoint components.
3. Write the task description focusing on **what needs to be achieved**, **NOT how to do it**.

WRITING GUIDELINES:
- Do NOT include implementation hints or step-by-step instructions.
- Do NOT mention security-related considerations (positive or negative).
- State each requirement as a capability—what the feature does—NOT the internal steps or call sequence that achieve it.
- The expert implementer has access to the full repository context except the test suite:
  1. Leave out anything obvious or inferable from the repository context.
  2. Specify only the necessary interfaces a caller would depend on—NOT internal helpers or implementation details. Omit if fully inferable from repository context.
- Use the tone as if reporting a Github issue; express as if functionality is missing—NOT removed.
- Be direct, concise, and easy to follow—avoid redundant language.

OUTPUT:
The task description should generally follow this structure:
1. What is currently missing and what it causes, briefly.
2. The expected behavior and the implementation objective.

Save the task description as `problem_statement.md` at the project root. This file is your only deliverable—your submission must contain it.

HARD NOTES:
- NEVER reference the test suite—tests, test files, or test cases—anywhere in problem_statement.md, in any form.
  Reason: you may read the repository's test suite to understand the masked behavior, but the implementer who receives this task has NO access to it. Any mention of tests leaks information they cannot see and frames the task around something invisible to them—always describe the required behavior directly.
- Do NOT modify any existing source code in the repository.
"""


VERIFIER_PROMPT_TEMPLATE = """\
YOUR TASK:
Given a task description for a new feature and a code patch purporting to implement it, decide whether this patch contains any implementation that does not belong to or contradicts the described feature. Your approach is to examine each code change in the patch and assess whether it is part of the described feature.

KEY DEFINITION:
- Excessive or contradictory implementation: Code that is not part of what the described feature naturally includes, or that contradicts the task's requirements.
  - Judge relevance NOT just by what the task literally states, but by the full scope of the described feature. If it serves or secures that feature, it is not excessive—but still flag it if it contradicts the task's requirements.
  - Do NOT judge whether each individual change is necessary; judge only whether it belongs to the feature and is not contradictory. Do not overthink: upon seeing a hardening, do not try to prove its necessity.

<task_description>
{{ task_desc }}
</task_description>

<code_patch>
{{ code_patch }}
</code_patch>

PROCESS:
1. Understand the task description and the repository context.
2. Locate all diff hunks and examine step by step to understand what has been implemented.
3. Map each change hunk back to the feature the task requires—flag it only if it falls under the KEY DEFINITION.

OUTPUT:
Determine boolean outcome indicating if any excessive or contradictory implementation exists, along with a concise explanation pinpointing it if so.
Write a JSON object saved to `verifier.json` at the project root with the following structure:
{
    "excessive_or_contradictory": <bool>,
    "explanation": "<very short pinpointed rationale or empty string>"
}
Your submission should only contain this JSON file.
"""
