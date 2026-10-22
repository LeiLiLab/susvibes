"""Constants of the leak stage: its verdict, its artifacts, the python the in-image sweep runs on,
and the rules that sort a sweep hit into removable or not."""
import os
import re
from enum import StrEnum
from pathlib import Path

from susvibes.core.constants import root_dir
from susvibes.curate.constants import KeepStage


class LeakStatus(StrEnum):
    """How the leak check of one instance concluded. Every member is a NORMAL outcome — the harness
    breaking is the report's `error` (see core/report.py), the only thing `--resume` re-runs."""
    CLEAN = "clean"          # no copy of the golden code in the eval image
    FIXED = "fixed"          # copies removed; re-validated with the same expected_pf and image config
    RESIDUE = "residue"      # a copy stays: no removal rule covers it, or removing it broke the instance


KEEP_STAGE = KeepStage.LEAK     # this stage's verdict under record["keep"]
RECORD_FIELD = "leak"           # where the record carries the projected report

LOG_INSTANCE = "leak.log"
LOG_REPORT = "report.json"
LOG_SUMMARY = "summary.json"
LOG_SWEEP = "sweep.json"            # the eval image's hits, before any fix
LOG_RESWEEP = "resweep.json"        # the rebuilt eval image's hits
VALIDATE_DIR_NAME = "validate"      # the re-validation's own log dir, under the instance's
KNOWN_RESIDUE_FILE_NAME = "known_residue.json"   # under datasets/<run_id>/: residue reviewed and accepted

# A leaky `<role>_<id>` image is kept as `<role>_leak_<id>` beside the fixed one under the standard
# name — the eval image, and whichever of env / base_no_test the fix rebuilt.
LEAK_IMAGE_NAME = "{role}_leak_{instance_id}"

# The sweep runs python inside images that may have none (node) or an old one, so it mounts a
# self-contained build read-only. SV_SWEEP_PYTHON_DIR names an unpacked python-build-standalone
# `python/` directory; absent, `ensure_sweep_python` fetches the pinned build into build/.
SWEEP_PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261021/"
                    "cpython-3.11.17%2B20261021-x86_64-unknown-linux-gnu-install_only.tar.gz")
SWEEP_PYTHON_DIR = Path(os.environ.get("SV_SWEEP_PYTHON_DIR", root_dir / "build" / "sweep_python" / "python"))
SWEEP_MOUNT_DIR = "/opt/svpy"
SWEEP_TIMEOUT = 3600            # seconds; a 12 GB image walks in well under an hour
SWEEP_MEM_LIMIT = "8g"
SWEEP_CPU_LIMIT = 4
MIN_MARKERS = 2                 # golden-only lines a derived copy must contain to count

# --- sorting hits: what is a leak, and what can go ---------------------------------------------
# Not a leak of our build (set aside, never counted): pip's vendored copy, the CPython stdlib, the
# sweep's own mounted python, an installed package or cached archive that is NOT this project's own
# package (a dependency that bundles the code is the environment), and npm dependencies, which a
# lockfile pins (a published release of the project itself included).
PIP_VENDORED = re.compile(r"/site-packages/pip/_vendor/")
PIP_PATHS = re.compile(r"/site-packages/pip(?:-[^/]*\.dist-info)?(?:/|$)|/ensurepip/_bundled/pip-[^/]*\.whl$")
STDLIB = re.compile(r"^/usr/(?:local/)?lib/python3\.\d+/(?!site-packages|dist-packages)")
SITE_PKG = re.compile(r"/(?:site|dist)-packages/([^/]+)")
GENERIC_DIRS = {"src", "lib", "python", "packages", "pkg", "source"}   # not a package name
# Package download caches: never imported, never under /project — pip/uv/poetry, deno, npm/yarn,
# bazel, and bun's store wherever BUN_INSTALL points it.
PKG_CACHES = re.compile(r"^((?:/root|/usr/local|/home/[^/]+)(?:/\.cache/(?:pip|uv|pypoetry|deno|node-gyp|yarn)"
                        r"|/\.cache/bazel|/\.bun/install/cache|/install/cache|/\.npm/_cacache))/")
# Copies our own build left behind, removable as a whole directory: (pattern -> the directory).
BUILD_OUTPUT = re.compile(r"^(/project/(?:[^/]+/)*build/lib[^/]*)/")    # setup.py build output, a sub-package's too
TMP_COPY = re.compile(r"^(/tmp/[^/]+)/")                                 # a source tree unpacked under /tmp by the build
# (pattern -> the directory, why)
CACHE_RULES = [
    (re.compile(r"^(.*/\.tox/\.pkg/dist)/"), "tox package dist"),
    (re.compile(r"^(/root/\.cache/pip)/"), "pip cache"),
]
INSTALLED_RULES = [
    (re.compile(r"^(.*/\.tox/[^/]+/lib/[^/]+/site-packages/[^/]+)/"), "tox venv copy"),
    (re.compile(r"^(.*/site-packages/[^/]+)/"), "installed copy"),
    (re.compile(r"^(.*/site-packages/[^/]+\.py)$"), "installed copy"),      # a single-module project
]
# JS build output compiled from the golden tree, incl. a monorepo package's own dist/. Whether the
# suite can live without it is decided by re-validating, not by reading the scripts.
JS_BUILD_DIR = re.compile(r"^(/project/(?:[^/]+/)*(?:dist|lib|build|out|esm|cjs|bundle|\.next|compiled))/")
JS_BUILD_STEP = re.compile(r"\b(npm run (?:build|compile|dist|prepare)|pnpm (?:run )?(?:build|compile|dev:prepare)"
                           r"|yarn (?:run )?(?:build|compile)|tsc\b|tsup\b|rollup\b|vite build|esbuild\b|babel\b)")
COMPILED = re.compile(r"(?:\.cpython-[^/]*)?\.(?:pyc|so|c|o)$")
JS_LANGUAGES = {"javascript", "typescript"}     # record languages whose build output JS_BUILD_DIR covers
