"""Structured Bash test adapters supplied with the JS/TS benchmark."""

import base64
import hashlib
import json
import shlex

BEGIN = "SUSVIBES_ADAPTER_RESULT_BEGIN"
END = "SUSVIBES_ADAPTER_RESULT_END"
EXIT_CODE = "SUSVIBES_ADAPTER_EXIT_CODE="


def adapter_command(spec: dict) -> str:
    script = spec["script"]
    if not isinstance(script, str) or not script.strip():
        raise ValueError("Empty test adapter script")
    if hashlib.sha256(script.encode()).hexdigest() != spec["script_sha256"]:
        raise ValueError("Test adapter script SHA256 mismatch")
    # Execute only inside the deployment. A separate Bash process isolates exit,
    # return, shell options and cwd changes in the supplied script.
    wrapper = (
        "rm -f test_results.json || exit 1\n"
        'adapter_script=$(mktemp) || exit 1\n'
        "trap 'rm -f -- \"$adapter_script\"' EXIT\n"
        f"printf %s {shlex.quote(base64.b64encode(script.encode()).decode())} | base64 -d > \"$adapter_script\" || exit 1\n"
        'bash "$adapter_script"\n'
        "adapter_rc=$?\n"
        'if [ ! -f test_results.json ]; then exit 1; fi\n'
        f'printf "\\n{EXIT_CODE}%s\\n" "$adapter_rc"\n'
        f"printf '\\n{BEGIN}\\n'\n"
        "cat test_results.json\n"
        f"printf '\\n{END}\\n'\n"
    )
    return "bash -c " + shlex.quote(wrapper)


def parse_adapter_result(logs: str) -> dict:
    lines = logs.splitlines()
    if lines.count(BEGIN) != 1 or lines.count(END) != 1:
        raise ValueError("Missing or ambiguous test adapter result")
    start, end = lines.index(BEGIN), lines.index(END)
    if start >= end:
        raise ValueError("Invalid test adapter result framing")
    result = json.loads("\n".join(lines[start + 1:end]))
    if not isinstance(result, dict):
        raise ValueError("Test adapter result must be an object")
    for key in ("passed", "failed", "errors", "skipped"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise ValueError(f"Invalid test adapter count: {key}")
    if result["errors"]:
        raise ValueError("Test adapter reported execution errors")
    if result["passed"] + result["failed"] == 0:
        raise ValueError("Test adapter did not execute any tests")
    exit_lines = [line[len(EXIT_CODE):] for line in lines if line.startswith(EXIT_CODE)]
    if exit_lines:
        if len(exit_lines) != 1 or not exit_lines[0].isdigit():
            raise ValueError("Invalid test adapter exit code")
        code = int(exit_lines[0])
        if code >= 128 or (code != 0 and result["failed"] == 0):
            raise ValueError("Test adapter failed without reliable test failures")
    return result


