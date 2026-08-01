"""Constants shared across the CVE sources.

GitHub URL/identity parsing: pull owner/repo/commit out of an advisory's reference URLs. A
`github.com/advisories/GHSA-…` or `pypa/advisory-database` link is the advisory/data, never the
project source, so GITHUB_NON_OWNER / GITHUB_NON_REPO filter it out. (`GITHUB_HEADERS` stays in
`mine/constants.py` — it authenticates API calls from `mine.utils` too, not only the sources.)
"""

import re

COMMIT_SHA = re.compile(r'[0-9a-f]{40}')
COMMIT_URL = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s]+)/commit/([0-9a-f]{7,40})')
REPO_URL = re.compile(r'github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
REF_REPO = re.compile(r'github\.com/([^/\s]+)/([^/\s?#]+)')
FROM_COMMIT = re.compile(r'^From ([0-9a-f]{40}) ', re.MULTILINE)
GITHUB_NON_OWNER = {"advisories", "security", "sponsors", "marketplace", "orgs", "collections",
                    "topics", "about", "pricing", "settings", "notifications"}
GITHUB_NON_REPO = {"advisory-database", "advisory-db", "security-advisories"}
