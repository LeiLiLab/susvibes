Hi folks,

Hope you're doing well! We've been running SusVibes evaluations at scale for our
Agent Security Leagues work and stumbled onto something that affects how SecPass
is computed on the leaderboard. We put together a fix and wanted to share it
with you.

**The short version:** SecPass currently uses a count-based rule
(`failures <= budget`) which can't tell the difference between "the security
tests passed" and "the security tests never ran." We found at least 5 confirmed
false positives across two Cursor runs where the agent got SecPass=True despite
not actually fixing the vulnerability. The causes varied — pytest maxfail
cutting the session short, Bazel crashing before any test ran, missing pip
dependencies collapsing the session, ERROR counts silently dropped by empty
regex patterns.

**What we did:** We implemented a positive-evidence rule for SecPass. Instead of
just checking that the failure count is low enough, the harness now extracts the
security test names from `test_patch` and verifies each one appeared as PASSED
in the runner's verbose output. We built this behind a `TestRunnerAdapter`
abstraction so it's extensible to non-pytest runners:

- **PytestAdapter** (covers ~140/200 instances): injects `PYTEST_ADDOPTS="-v"`
  to get per-test PASSED/FAILED lines
- **DjangoTestAdapter** (~33 instances): appends `--verbosity=2` to the CMD
- **FallbackAdapter** (~27 instances): keeps the existing count-based logic for
  runners we don't recognize yet

We also backfilled the missing ERROR regex for 87 instances, added a
missing-summary sentinel, and a maxfail-banner detector. The whole thing is
backward-compatible — FuncPass logic is untouched, and no per-instance configs
or Docker images need to change.

**E2E validation:** We re-evaluated the 5 false-positive instances with their
real Docker images and predictions. All 5 correctly flipped to SecPass=False.
A known legitimate pass (jinja, where the agent's fix was actually correct)
stayed True. 53 unit tests, all green.

**The PR is here:** [link to PR — push and create before sending]

**Technical details** are in `CHANGES.md` in the PR, which covers the full
breakdown of each fix, the architecture, test coverage, and expected
leaderboard impact.

We think SecPass numbers across the leaderboard will decrease once evaluations
are re-run — that's the corrected direction. Happy to walk through any of this,
discuss the design choices, or adjust anything before merging.

Cheers,
Luca
