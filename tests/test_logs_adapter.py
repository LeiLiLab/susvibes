"""Tests for the test_adapter logs handler and its routing."""

import hashlib
import json
import logging

import pytest

from susvibes.core.constants import TestStatus
from susvibes.core.logs import LOGS_KINDS, LogsAdapter, LogsHandler
from susvibes.core.utils import Route
from susvibes.env_specs import GEN_SEC_TEST_CMD, TEST_ADAPTER_RESULTS_MARKER

SCRIPT = "#!/usr/bin/env bash\nnpm test\necho '{\"passed\": 3, \"failed\": 1, \"errors\": 0, \"skipped\": 0}' > test_results.json\n"
SPEC = {"script": SCRIPT, "script_sha256": hashlib.sha256(SCRIPT.encode()).hexdigest()}
LOGGER = logging.getLogger("test")


def logs_with(result: dict, prefix: str = "runner noise\n") -> str:
    return f"{prefix}{TEST_ADAPTER_RESULTS_MARKER}\n{json.dumps(result)}\n"


def test_registered_kind() -> None:
    assert LOGS_KINDS["test_adapter"] is LogsAdapter
    assert LogsAdapter.from_dict(SPEC).to_dict() == SPEC


def test_handle_completed_counts_failures_and_errors() -> None:
    pf = LogsAdapter.from_dict(SPEC).handle(
        logs_with({"passed": 3, "failed": 2, "errors": 1, "skipped": 4}), False, LOGGER)
    assert pf.status == TestStatus.COMPLETED and pf.failures == 2 and pf.errors == 1


def test_handle_errors_only_is_aborted() -> None:
    pf = LogsAdapter.from_dict(SPEC).handle(
        logs_with({"passed": 0, "failed": 0, "errors": 1, "skipped": 0}), False, LOGGER)
    assert pf.aborted()


def test_handle_no_marker_is_aborted() -> None:
    assert LogsAdapter.from_dict(SPEC).handle("build crashed before any test ran\n", False, LOGGER).aborted()


def test_handle_timeout() -> None:
    assert LogsAdapter.from_dict(SPEC).handle(logs_with({"passed": 1, "failed": 0, "errors": 0}), True, LOGGER).timed_out()


def test_handle_malformed_result_raises() -> None:
    with pytest.raises(RuntimeError):
        LogsAdapter.from_dict(SPEC).handle(f"{TEST_ADAPTER_RESULTS_MARKER}\nnot json\n", False, LOGGER)


def test_handle_by_kind_routes_to_adapter() -> None:
    pf = LogsHandler.handle_by_kind("test_adapter", {"test_adapter": SPEC},
        logs_with({"passed": 5, "failed": 0, "errors": 0, "skipped": 0}), False, LOGGER)
    assert pf.completed() and pf.failures == 0


def test_route_adapter_instance() -> None:
    flags = {"test_adapter": True}
    cmd = Route.route_test_cmd(flags, "func", {"test_adapter": SPEC})
    assert cmd[:2] == ["bash", "-c"] and SCRIPT in cmd[2] and TEST_ADAPTER_RESULTS_MARKER in cmd[2]
    assert Route.route_logs_kind(flags, "func") == "test_adapter"
    # a generated-test run on an adapter instance still runs the synthesized suite
    assert Route.route_test_cmd({"test_adapter": True, "gen_test": True}, "sec", {"test_adapter": SPEC}) == GEN_SEC_TEST_CMD
    assert Route.route_logs_kind({"test_adapter": True, "gen_test": True}, "sec") == "count_gen_sec"


def test_route_rejects_tampered_script() -> None:
    tampered = {"test_adapter": {"script": SCRIPT + "rm -rf /\n", "script_sha256": SPEC["script_sha256"]}}
    with pytest.raises(RuntimeError):
        Route.route_test_cmd({"test_adapter": True}, "func", tampered)


def test_route_plain_instance_unchanged() -> None:
    assert Route.route_test_cmd({}, "func") is None
    assert Route.route_logs_kind({}, "func") == "count"


def test_generated_security_handler_is_not_overridden_by_adapter() -> None:
    handlers = {"test_adapter": SPEC, "count_gen_sec": {
        "logs_parser": {"FAILED": r"failures=(\d+)"}, "logs_checker": None}}
    pf = LogsHandler.handle_by_kind("count_gen_sec", handlers,
        "Ran 1 test in 1s\nFAILED (failures=1)\nRan 1 test in 1s\nOK\n", False, LOGGER)
    assert pf.completed() and pf.failures == 1
