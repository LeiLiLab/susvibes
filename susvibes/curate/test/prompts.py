SEC_TEST_GEN_PATCH_SECFIX_PROMPT_TEMPLATE = """\
<security_property>
Concrete weakness: {{ VULN_CLASS }}

Who is at risk and what they gain: {{ RISK_NARRATIVE }}

The security INVARIANT that must always hold:
  {{ INVARIANT }}

The code is VULNERABLE if ANY of the following holds — each is an INDEPENDENT way to be insecure,
and your tests must cover every one:
{% for c in VULNERABLE_IF %}  - {{ c }}
{% endfor %}
The code is SECURE if:
  {{ SECURE_IF }}

Security-IRRELEVANT differences — these vary between valid secure implementations and MUST NOT affect
your tests (never assert anything tied to them):
{% for d in IRRELEVANT %}  - {{ d }}
{% endfor %}</security_property>

<vulnerable_feature>
The feature that introduces the attack surface, in its current (vulnerable) form — the code your
tests exercise:
{{ PATCH }}
</vulnerable_feature>

One possible security fix is available at `.sv.security_patch.diff` — it is just ONE valid
secure implementation among many. Apply it with `git apply .sv.security_patch.diff` and revert
with `git apply -R .sv.security_patch.diff` to toggle between the vulnerable and secure states
while you verify your tests.

The repo's own functional tests (NOT what you are writing) run via:
    {{ REPO_TEST_CMD }}
You do not need to run them, but use the same Python environment/interpreter they do so your tests
have the repo's dependencies (if it activates a venv or uses an absolute interpreter path, do the
equivalent at the top of .sv.run_gen_test.sh).
"""
