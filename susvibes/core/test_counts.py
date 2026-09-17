"""Validate count-based test output without treating missing output as success."""
import re


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
EXECUTED = ("PASSED", "FAILED", "ERROR", "XFAIL", "XPASS")


def strip_ansi(logs: str) -> str:
    return ANSI_ESCAPE.sub("", logs)


def validated_test_counts(logs: str, counts: dict[str, int], *, aggregate=False) -> dict[str, int]:
    """Keep configured counts, or recover an unmatched standard summary.

    A complete zero-test or skipped-only summary always invalidates the result.
    Generated-security mode sums unittest suites so a later OK cannot erase
    an earlier failure. Functional parsing retains configured counts.
    Failure-only legacy specs need a completed runner summary as evidence of
    execution when all their failure patterns are absent or zero.
    """
    logs = strip_ansi(logs)
    summaries = []
    unittest_summaries = []
    for match in re.finditer(
        r"^=+ ([^\n]+) in \d+(?:\.\d+)?s(?: \([^\n]*\))? =+\s*$", logs, re.MULTILINE
    ):
        names = {"passed": "PASSED", "failed": "FAILED", "error": "ERROR",
                 "errors": "ERROR", "skipped": "SKIPPED", "xfailed": "XFAIL", "xpassed": "XPASS"}
        result = {names[name]: int(n) for n, name in re.findall(
            r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b", match[1])}
        summaries.append((match.start(), result))
    for match in re.finditer(
        r"^\s*Ran (\d+) tests? in [^\n]+\n\s*"
        r"(OK(?: \([^\n]*\))?|PASSED(?: \([^\n]*\))?|FAILED \([^\n]+\))\s*$",
        logs, re.MULTILINE,
    ):
        fields = {key: int(n) for key, n in re.findall(
            r"(?:\(|,\s*)(failures|errors|skipped|SKIP|expected failures|unexpected successes)=(\d+)", match[2])}
        result = {"FAILED": fields.get("failures", 0), "ERROR": fields.get("errors", 0),
                  "SKIPPED": fields.get("skipped", fields.get("SKIP", 0)),
                  "XFAIL": fields.get("expected failures", 0), "XPASS": fields.get("unexpected successes", 0)}
        if match[2].startswith("FAILED") and not result["FAILED"] + result["ERROR"]:
            raise ValueError("Unrecognized unittest failure counts")
        # unittest counts failed subtests separately, so failures can exceed testsRun.
        result["PASSED"] = max(0, int(match[1]) - sum(result.values()))
        summaries.append((match.start(), result))
        unittest_summaries.append((match.start(), result))
    for match in re.finditer(
        r"^\s*Ran (\d+) tests with (\d+) failures, (\d+) errors(?: and|,) (\d+) skipped in [^\n]+$",
        logs, re.MULTILINE,
    ):
        result = {"FAILED": int(match[2]), "ERROR": int(match[3]), "SKIPPED": int(match[4])}
        result["PASSED"] = int(match[1]) - sum(result.values())
        summaries.append((match.start(), result))
    if any(n < 0 for n in counts.values()):
        raise ValueError("Negative test counts")
    has_counts = any(counts.get(k, 0) > 0 for k in EXECUTED)
    if summaries:
        selected = unittest_summaries if aggregate and unittest_summaries else summaries
        if aggregate and (unittest_summaries or not has_counts):
            if any(n < 0 for _, counts_ in selected for n in counts_.values()):
                raise ValueError("Negative test counts in completed summary")
            result = {}
            for _, counts_ in selected:
                for key, n in counts_.items():
                    result[key] = result.get(key, 0) + n
        else:
            result = max(selected, key=lambda item: item[0])[1]
        if any(n < 0 for n in result.values()) or sum(result.get(k, 0) for k in EXECUTED) <= 0:
            raise ValueError("No executed tests in completed summary")
        return result if aggregate and unittest_summaries else (counts if has_counts else result)
    if has_counts:
        return counts
    # Bikeshed's runner prints numbered cases followed by a final success marker.
    match = re.search(r"^([1-9]\d*)/\1: [^\n]+\n(?:\s*\n)*✔ All tests passed\.\s*\Z", logs, re.MULTILINE)
    if match:
        return {"PASSED": int(match[1]), "FAILED": 0}
    raise ValueError("No executed test counts found in logs")


def parse_test_counts(patterns, logs, *, aggregate=False):
    """Apply instance regexes and validate runner evidence in one place."""
    logs = strip_ansi(logs)
    counts = {}
    for status, pattern in patterns.items():
        if pattern:
            matches = list(re.finditer(pattern, logs, re.MULTILINE))
            counts[status] = int(matches[-1][1]) if matches else 0
    return validated_test_counts(logs, counts, aggregate=aggregate)
