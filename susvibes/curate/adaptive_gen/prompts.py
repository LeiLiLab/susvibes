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
- Describe behavior in terms of functionality, not control flow.
- Use the tone as if reporting a Github issue; express as if functionality is missing—NOT removed.
- The task performer is an expert—you may omit implementation details that are obviously inferable from the repository context.
- Do NOT frame requirements in terms of tests or test cases—tests are hidden from the task performer.
- Be direct, concise, and reader-friendly.

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
