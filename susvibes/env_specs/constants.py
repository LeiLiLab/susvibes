TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS = [
    r"ImportError: cannot import",
    r"AttributeError:.*?attribute",
    r"NameError: name",
    r"UnboundLocalError:",
    r"TypeError:",
    r"pydantic\..*?ValidationError:",
    r"Unknown keyword argument"
]

# Per dev tool: a config dict containing
#   - `versions`: version -> {upstream_image_name, static_deps} — the full upstream
#     image reference to FROM when building base_<tool>, plus the static_py jedi/parso
#     pin (per-version detail inline below; tag rationale in env_specs/dockerfiles.py)
#   - `minimal_compatible_version`: floor below which a discovered version is
#     dropped instead of rounded up to the nearest available.
DEV_TOOL_VERSIONS = {
    "python": {
        "minimal_compatible_version": "2.5",
        # Per version: full upstream image reference + the jedi/parso pin for static_py.
        # jedi 0.19 needs py3.6+, so 2.7/3.5 pin the last py2-capable line (0.17.2 + parso 0.7.x).
        "versions": {
            "2.7":  {"upstream_image_name": "python:2.7-buster",    "static_deps": "jedi==0.17.2 parso==0.7.1 pathlib2"},
            "3.5":  {"upstream_image_name": "python:3.5-buster",    "static_deps": "jedi==0.17.2 parso==0.7.1"},
            "3.6":  {"upstream_image_name": "python:3.6-bullseye",  "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.7":  {"upstream_image_name": "python:3.7-bookworm",  "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.8":  {"upstream_image_name": "python:3.8-bookworm",  "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.9":  {"upstream_image_name": "python:3.9-bookworm",  "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.10": {"upstream_image_name": "python:3.10-bookworm", "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.11": {"upstream_image_name": "python:3.11-bookworm", "static_deps": "jedi==0.19.2 parso==0.8.4"},
            "3.12": {"upstream_image_name": "python:3.12-bookworm", "static_deps": "jedi==0.19.2 parso==0.8.4"},
        },
    },
}
DOCKERFILE_PATTERN = (
    r'^(FROM(?:[^\r\n]*\\\r?\n)*[^\r\n]*\r?\n)'
    r'(.*?)'
    r'^(COPY(?:[^\r\n]*\\\r?\n)*[^\r\n]*\r?\n)'
    r'(.*?)'
    r'^(CMD(?:[^\r\n]*\\\r?\n)*[^\r\n]*(?:\r?\n|$))'
)

WORKSPACE_DIR_NAME = "project"                            # repo working tree (WORKDIR /project)
# susvibes namespace for data it injects into instance images (separate from the project
# copy that the env build already lays down). build_data/ holds build-time inputs and is
# removed at the end of the build; runtime_data/ holds files the container needs at run time.
SUSVIBES_DIR = "/.sv"
SUSVIBES_BUILD_DATA_DIR = f"{SUSVIBES_DIR}/build_data"
SUSVIBES_RUNTIME_DATA_DIR = f"{SUSVIBES_DIR}/runtime_data"
GIT_AUTHOR_CONFIGS = [
    "git config --global user.email setup@susvibes",
    "git config --global user.name SusVibes"
]
BANNED_REINSTALL_FOR_INSTANCE = {}
# Container command for synthesized security tests: run the injected .sv.run_gen_test.sh, which prints
# its single-line JSON pass-map to stdout for the gen_sec logs handler to parse.
GEN_SEC_TEST_CMD = ["bash", "-c", "bash .sv.run_gen_test.sh"]
