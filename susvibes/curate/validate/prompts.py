from textwrap import dedent


# The model declares what it reads AND the regexes that must reproduce it, so synthesis is checked
# against the model's own reading of the summary rather than against how the counts happen to look.
# No status is privileged and no count is required to be non-zero: "the number in the summary, 0 when
# there is none" is the anchor, so a status a runner reports without digits (unittest's bare `OK`)
# declares 0 and stays consistent instead of forcing an uncapturable count.
LOGS_PARSER_PROMPT_TEMPLATE = {
    "system": dedent("""\
        You are a logs parser. Given the raw output of several runs of the same test suite, do TWO things.

        1. For EACH run, read its end-of-run summary and record, for each standard status ({{ statuses|join(", ") }}), the count that appears AS A NUMBER in that summary. If a status has no number there (e.g. unittest 'OK' / 'FAILED (failures=N)' gives no passed count), record 0.

        2. Give one regex per status, usable as re.compile(<pattern>, re.MULTILINE), that on ALL runs captures that count through EXACTLY ONE capturing group of digits. Match the digits where they are — a decorated summary line like pytest '===== 7 failed, 4 passed in 0.08s =====', never a bare 'FAILED test::name' line. Use "" for any status you recorded as 0 on every run.

        Your regexes MUST reproduce, on every run, EXACTLY the counts you recorded in step 1 — declared count and regex output must agree for every status, or the attempt is rejected. So read the summary and record the counts first, then write regexes that yield precisely them.

        Output STRICTLY this JSON object and nothing else (no code fences, no prose):
        {
          "runs": [ {{ '{' }}{% for status in statuses %}"{{ status }}": <int>{{ ", " if not loop.last }}{% endfor %}{{ '}' }}, ... one object per run, in the given order ],
          "patterns": {{ '{' }} {% for status in statuses %}"{{ status }}": "<regex>"{{ ", " if not loop.last }}{% endfor %} {{ '}' }}
        }
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


LOGS_CHECKER_PROMPT_TEMPLATE = {
    "system": dedent("""\
        You are a logs checker. When given the raw output of several runs of the same test suite, your job is to produce exactly one Python-runnable regular expression that detects the runs in which the suite aborted before reporting results.

        Your regex must be directly usable as
        ```python
        re.compile(<pattern>, re.MULTILINE)
        ```
        and, via re.search, must match the logs of every aborted run and none of the completed runs. If every run completed, use an empty string for the regex and leave aborted_runs empty.

        RULES:
        - A run is completed if it ran the whole suite through to a test-result summary — the line(s) a runner prints after executing tests, e.g. pytest's `N passed, N failed, ... in Ts` or unittest's `Ran N tests in Ts` followed by `OK` or `FAILED (failures=N, errors=N)` (note the latter form carries no per-status pass/fail counts). It aborted if the suite stopped before reporting a complete result — either no such summary appeared, or the run ended early without executing every test. Decide on whether the run reached a complete summary, not on whether tests began or whether errors appeared, and not on where in the log it sits (teardown or other output may follow it).
        - A run that reported a summary completed even if **many tests failed** or it also printed **tracebacks or a non-zero exit code**; these appear in completed runs too, so they are not abort signals—do not match such a run.
        - A run that produced only **collection / import / setup errors**, with no test ever running, aborted—even if it printed a count of those errors; a run that started and then crashed partway with no summary also aborted; and a run that **stopped early after reaching a failure limit** (e.g. pytest `-x` / `--maxfail`: `stopping after N failures`) aborted, because it did not run the full suite and its counts are truncated—even though it printed a summary. Match these.
        - Anchor on the abort signature shared by the aborted runs and absent from the completed ones; consider all runs together so the regex generalizes instead of overfitting one run.

        Format your output as a JSON object, STRICTLY as follows:

        {
        "aborted_runs": [<1-based indices of the runs you judged to have aborted>],
        "logs_checker": "<your-pattern-here-or-empty-string>"
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
