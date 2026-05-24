from textwrap import dedent


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


LOGS_CHECKER_PROMPT_TEMPLATE = {
    "system": dedent("""\
        You are a logs checker. When given the raw output of several runs of the same test suite, your job is to produce exactly one Python-runnable regular expression that detects the runs in which the suite aborted before reporting results.

        Your regex must be directly usable as
        ```python
        re.compile(<pattern>, re.MULTILINE)
        ```
        and, via re.search, must match the logs of every aborted run and none of the completed runs. If every run completed, use an empty string for the regex and leave aborted_runs empty.

        RULES:
        - A run is completed if it reported a summary of the tests it ran (counts of how many passed / failed / etc.), and aborted if no such summary appeared because the suite stopped before reporting results; decide on the presence of that summary, not on whether tests began or whether errors appeared.
        - A run that reported a summary completed even if **many tests failed**, or it also printed **tracebacks or a non-zero exit code**; these appear in completed runs too, so they are not abort signals—do not match such a run.
        - A run that produced only **collection / import / setup errors**, with no test ever running, aborted—even if it printed a count of those errors; a run that started and then crashed partway with no summary also aborted. Match these.
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
