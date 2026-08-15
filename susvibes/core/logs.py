import re
import json
import tiktoken
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from jinja2 import Template
from litellm import completion, completion_cost, get_max_tokens
from dotenv import load_dotenv

from susvibes.curate.validate.prompts import (
    LOGS_PARSER_PROMPT_TEMPLATE,
    LOGS_CHECKER_PROMPT_TEMPLATE,
)
from susvibes.core.constants import TestStatus, TestItemStatus, FAILURE_STATUSES
from susvibes.env_specs import TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS
from susvibes.core.utils import load_file, save_file

load_dotenv()


# --- LLM cost tracking (thread-safe; run total over logs-parser + logs-checker calls) ---
_llm_cost_lock = threading.Lock()
_llm_cost_total = 0.0


def record_llm_cost(response) -> None:
    """Add this LLM response's USD cost to the run total."""
    cost = (getattr(response, "_hidden_params", None) or {}).get("response_cost")
    if cost is None:
        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            cost = 0.0
    cost = cost or 0.0
    global _llm_cost_total
    with _llm_cost_lock:
        _llm_cost_total += cost


def get_llm_cost() -> float:
    """Total USD cost of logs-parser/checker LLM calls since the last reset_llm_cost()."""
    return _llm_cost_total


def reset_llm_cost() -> None:
    global _llm_cost_total
    with _llm_cost_lock:
        _llm_cost_total = 0.0


def clip_tokens(text: str, model: str, limit: int) -> str:
    """Keep the last `limit` tokens of `text` for `model` (special-token strings encoded
    as plain text); fall back to cl100k_base for unknown models."""
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text, disallowed_special=())
    if len(tokens) > limit:
        tokens = tokens[-limit:]
    return enc.decode(tokens)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Drop terminal colour/SGR escape codes so parsing and parser-synthesis see plain text — some
    runners (e.g. pytest under a pseudo-tty) wrap the count in codes like `\\x1b[1m7 failed\\x1b[0m`."""
    return _ANSI_RE.sub("", text)


def extract_json_object(text: str) -> dict | None:
    """The last top-level JSON object embedded in `text`, or None if there is none. Scans past
    any prose, code fences, or stray scalars a model may wrap around the object."""
    decoder = json.JSONDecoder()
    obj, i, n = None, 0, len(text)
    while i < n:
        if text[i] == "{":
            try:
                candidate, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(candidate, dict):
                obj, i = candidate, end
                continue
        i += 1
    return obj


class MaxSet:
    """Sentinel 'universal' set: a strict superset of every real set. It stands for the excess
    breaks of a run that did not complete (it breaks everything), so it sorts as the maximal
    element in any comparison. Used only by PassFailureCases.excess_breaks_over."""
    def __gt__(self, other): return True
    def __ge__(self, other): return True
    def __lt__(self, other): return False
    def __le__(self, other): return isinstance(other, MaxSet)


class PassFailure(ABC):
    """A test run's pass/failure outcome as a comparable value — a count (PassFailureCount) or a
    per-case map (PassFailureCases). A non-completed run compares as infinitely broken. The
    comparison/quantity methods are granularity-agnostic, so validate/eval read the same across
    subclasses; status stays folded inside — callers never branch on TestStatus."""

    def __init__(self, status: TestStatus):
        self.status = status

    def completed(self) -> bool:
        return self.status == TestStatus.COMPLETED

    @abstractmethod
    def breaks_more_than(self, other: "PassFailure") -> bool:
        """Whether this run is strictly more broken than `other` (a non-completed run always is)."""

    @abstractmethod
    def excess_breaks_over(self, other: "PassFailure", to_raw: bool = False):
        """How much more this run breaks than `other`, as a comparable amount (∞/⊤ if not completed).
        to_raw yields a JSON-serializable form (e.g. a set of cases → a list)."""

    @abstractmethod
    def count_excess_breaks_over(self, other: "PassFailure") -> int | float:
        """Number of extra breaks over `other` (∞ if not completed)."""

    @abstractmethod
    def capped_by(self, other: "PassFailure") -> "PassFailure":
        """This expected threshold capped at `other` (the lesser-broken of the two), tightening it
        toward the observed result for subsequent eval runs."""

    @abstractmethod
    def get_raw(self):
        """The bare JSON-serializable value to store as the expected threshold."""

    @classmethod
    def from_raw(cls, raw) -> "PassFailure":
        """Reconstruct from a stored raw value: a list of passed cases → PassFailureCases, else count."""
        if isinstance(raw, list):
            return PassFailureCases(TestStatus.COMPLETED, {case: True for case in raw})
        return PassFailureCount(TestStatus.COMPLETED, raw)

    @staticmethod
    def add_raw(a, b):
        """Add two stored expected-raw values across eval runs: both counts → sum, both case lists
        → de-duped union; a count mixed with a list (func vs synthesized sec) isn't summed — the
        list is the threshold that matters, so return it."""
        if isinstance(a, list) and isinstance(b, list):
            return list(dict.fromkeys(a + b))
        if isinstance(a, list) or isinstance(b, list):
            return a if isinstance(a, list) else b
        return a + b


class PassFailureCount(PassFailure):
    """The outcome as a failure count."""

    def __init__(self, status: TestStatus, failures: int):
        super().__init__(status)
        self.failures = failures

    def __repr__(self):
        return f"PassFailureCount({self.failures}, {self.status})"

    def breaks_more_than(self, other):
        return not self.completed() or self.failures > other.failures

    def excess_breaks_over(self, other, to_raw=False):
        return float("inf") if not self.completed() else self.failures - other.failures

    def count_excess_breaks_over(self, other):
        return float("inf") if not self.completed() else self.failures - other.failures

    def capped_by(self, other):
        return PassFailureCount(TestStatus.COMPLETED, min(self.failures, other.failures))

    def get_raw(self):
        return self.failures


class PassFailureCases(PassFailure):
    """The outcome as a {case: passed} map; 'breaks' are the common cases this run fails that the
    other passes."""

    def __init__(self, status: TestStatus, cases: dict):
        super().__init__(status)
        self.cases = cases

    def __repr__(self):
        return f"PassFailureCases({self.cases}, {self.status})"

    def _distinguishing(self, other: "PassFailureCases") -> set:
        return {case for case in self.cases.keys() & other.cases.keys()
            if self.cases[case] is False and other.cases[case] is True}

    def breaks_more_than(self, other):
        return not self.completed() or bool(self._distinguishing(other))

    def excess_breaks_over(self, other, to_raw=False):
        if not self.completed():
            return MaxSet()
        distinguishing = self._distinguishing(other)
        return list(distinguishing) if to_raw else distinguishing

    def count_excess_breaks_over(self, other):
        return float("inf") if not self.completed() else len(self._distinguishing(other))

    def capped_by(self, other):
        cases = dict(self.cases)
        for case, passed in other.cases.items():
            if passed is True and cases.get(case) is not True:
                cases[case] = True
        return PassFailureCases(TestStatus.COMPLETED, cases)

    def get_raw(self):
        return [case for case, passed in self.cases.items() if passed is True]


class LogsHandler(ABC):
    """Base for the per-kind logs handlers: each turns a run's container logs into a PassFailure.
    A handler instance carries its data; `handle_by_kind` rebuilds the right one from a stored dict
    and applies it, so callers route by kind alone. The per-instance synthesis cache (log_dir's
    logs_handler.json) is shared and keyed by kind, e.g. {"count": {...}, "count_gen_sec": {...}}."""

    CACHE_FILE_NAME = "logs_handler.json"
    KIND: str

    @abstractmethod
    def handle(self, test_logs: str, timed_out: bool, logger: logging.Logger) -> PassFailure:
        """Apply this handler to one run's test logs, returning its PassFailure."""

    @abstractmethod
    def to_dict(self) -> dict:
        """The bare JSON-serializable dict stored under this kind in logs_handler.json."""

    @classmethod
    def from_dict(cls, data: dict) -> "LogsHandler":
        """Rebuild a handler instance from its stored dict."""
        return cls(**data)

    @classmethod
    def handle_by_kind(cls, kind: str | tuple, logs_handler: dict, test_logs: str, timed_out: bool,
        logger: logging.Logger) -> PassFailure:
        """Route to a handler (rebuilt from `logs_handler`) and apply it. `kind` is a kind name, or a
        tuple of names tried in priority order — the first kind present in `logs_handler` wins.
        Raises RuntimeError if none of the requested kinds is available."""
        kinds = (kind,) if isinstance(kind, str) else kind
        for k in kinds:
            if k in logs_handler:
                return LOGS_KINDS[k].from_dict(logs_handler[k]).handle(test_logs, timed_out, logger)
        msg = "No logs handler available."
        logger.error(msg)
        raise RuntimeError(msg)

    @classmethod
    def get_by_kind(cls, kind: str, existing_data: dict = None, **kwargs) -> dict:
        """Get the kind's handler (its `get`, reusing this kind's existing data) and merge it into
        the existing logs_handler dict, preserving the other kinds' stored data."""
        existing_data = existing_data or {}
        handler = LOGS_KINDS[kind].get(existing_data=existing_data.get(kind), **kwargs)
        return {**existing_data, kind: handler.to_dict()}

    @staticmethod
    def count_symb_res_errors(test_logs: str) -> int:
        """Count missing-symbol-resolution errors in the test logs."""
        return sum(len(re.findall(pattern, test_logs, re.MULTILINE))
            for pattern in TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS)

    @classmethod
    def _load_cache(cls, log_dir: Path) -> dict:
        """This kind's cached data under log_dir's logs_handler.json ({} if not cached yet)."""
        path = log_dir / cls.CACHE_FILE_NAME
        return (load_file(path) if path.exists() else {}).get(cls.KIND, {})

    @classmethod
    def _save_cache(cls, log_dir: Path, data: dict) -> None:
        """Write this kind's data into log_dir's logs_handler.json, keeping the other kinds."""
        path = log_dir / cls.CACHE_FILE_NAME
        cache = load_file(path) if path.exists() else {}
        cache[cls.KIND] = data
        save_file(cache, path)


class LogsCount(LogsHandler):
    """The standard repo-test handler: a {logs_parser, logs_checker} pair — the parser a per-status
    regex counting outcomes (FAILED/PASSED/...), the checker one regex flagging aborted runs.
    `handle` applies the data; `gen` synthesizes (or reuses) one end to end."""

    KIND = "count"

    def __init__(self, logs_parser: dict = None, logs_checker: str = None):
        self.logs_parser = logs_parser
        self.logs_checker = logs_checker

    def to_dict(self) -> dict:
        return {"logs_parser": self.logs_parser, "logs_checker": self.logs_checker}

    # --- Applying a spec to test logs: status (logs_checker) + failure count (logs_parser). ---
    @staticmethod
    def _check(logs_checker: str, test_logs: str, timed_out: bool = False) -> TestStatus:
        """Test status from the test logs, using the logs_checker regex."""
        if timed_out:
            return TestStatus.TIMEOUT
        if logs_checker and re.search(logs_checker, strip_ansi(test_logs), re.MULTILINE):
            return TestStatus.STARTUP_ERROR
        return TestStatus.COMPLETED

    @staticmethod
    def _parse(logs_parser: dict, test_logs: str, logger: logging.Logger) -> dict[str, int]:
        """Count test outcomes in the test logs using the per-status logs_parser regexes."""
        logger.info("Parsing test logs...")
        test_logs = strip_ansi(test_logs)
        test_result = {}
        for item_status, pattern in logs_parser.items():
            if pattern:
                logs_parse_re = re.compile(pattern, re.MULTILINE)
                m = None
                for m in logs_parse_re.finditer(test_logs):
                    pass
                test_result[item_status] = int(m.group(1)) if m else 0
        return test_result

    @staticmethod
    def _count_failures(test_result: dict[str, int]) -> int:
        """Total countable failures (FAILED + ERROR) in a parsed test result."""
        return sum(test_result.get(item_status.value, 0) for item_status in FAILURE_STATUSES)

    def handle(self, test_logs, timed_out, logger) -> PassFailureCount:
        """Status from logs_checker (always), failure count from logs_parser (when present).
        Raises RuntimeError if the logs can't be parsed."""
        try:
            status = self._check(self.logs_checker, test_logs, timed_out)
            failures = None
            if self.logs_parser:
                failures = self._count_failures(self._parse(self.logs_parser, test_logs, logger))
        except Exception as e:
            msg = "Failed to parse test logs."
            logger.error(msg)
            raise RuntimeError(msg)
        return PassFailureCount(status, failures)

    # --- Synthesizing the parser: a per-status regex that counts test outcomes. ---
    @staticmethod
    def _validate_parser(logs_parser: dict, logger: logging.Logger,
        require_failures: bool = True) -> bool:
        if not isinstance(logs_parser, dict):
            logger.warning(f"Logs parser is not a dict: {type(logs_parser).__name__}.")
            return False
        try:
            logs_parser = {TestItemStatus(item_status).value: pattern for item_status, pattern
                in logs_parser.items() if pattern}
        except ValueError as e:
            logger.warning(f"Invalid logs parser: {e}")
            return False
        if not logs_parser:
            logger.warning("Invalid logs parser: no usable pattern.")
            return False
        if require_failures and all(item_status.value not in logs_parser for item_status in FAILURE_STATUSES):
            logger.warning(f"Invalid logs parser with no failure status.")
            return False
        return True

    @classmethod
    def _gen_parser(
        cls,
        test_logs_list: list,
        test_statuses: list,
        model: str,
        logger: logging.Logger,
        ordering_checks: list[tuple[int, int]],
        max_retries: int = 10,
        conservative_max_retries: int = 5,
        require_failures: bool = True,
    ) -> dict:
        """Synthesize and return a logs parser from the test logs. Raises RuntimeError on failure."""
        test_logs_list = [clip_tokens(strip_ansi(test_logs), model, limit=(get_max_tokens(model) // 8))
            for test_logs in test_logs_list]

        messages = []
        for prompt_key, prompt in LOGS_PARSER_PROMPT_TEMPLATE.items():
            if prompt_key == "system":
                messages.append({"role": "system", "content": Template(prompt).render(
                    statuses=[item_status.value for item_status in TestItemStatus])})
            else:
                messages.append({"role": "user", "content": Template(prompt).render(
                    logs=[test_logs for test_logs, test_status in zip(test_logs_list, test_statuses) if test_status])})

        logger.info("Synthesizing logs parser...")
        is_success = False
        conserv_retry = 1
        for retry in range(max_retries):
            if retry:
                logger.info(f"Retrying... {retry + 1}/{max_retries}")
            try:
                response = completion(model=model, messages=messages)
            except Exception as e:
                logger.warning(f"Failed to get model response: {e}")
                continue
            record_llm_cost(response)
            message = response.choices[0].message
            logs_parser = extract_json_object(message.content)
            if logs_parser is None:
                logger.warning("Failed to parse model response as JSON.")
                continue
            if not cls._validate_parser(logs_parser, logger, require_failures):
                continue
            test_result_list, test_failures_list = [], []
            for test_logs, test_status in zip(test_logs_list, test_statuses):
                if not test_status:
                    test_result_list.append({})
                    continue
                try:
                    test_result = cls._parse(logs_parser, test_logs, logger)
                    test_result_list.append(test_result)
                except Exception as e:
                    logger.warning(f"Failed to parse test logs: {e}. logs_parser-{logs_parser}")
                    break
                test_failures_list.append(cls._count_failures(test_result))
            if len(test_result_list) < len(test_logs_list):
                continue
            if any(tf < 0 for tf in test_failures_list):
                logger.warning(f"Invalid (negative) test failures detected. logs_parser-{logs_parser}")
                continue
            if not sum(test_failures_list):
                if require_failures:
                    logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
                    continue
                if any(ts != TestStatus.COMPLETED for ts in test_statuses):
                    # A run did not complete (e.g. the masked task crashes): zero countable
                    # failures is the expected structural break. Accept; the functional-break
                    # verdict is made from run statuses downstream.
                    is_success = True
                    break
                # All runs completed yet no countable failures: either the change is not
                # covered by the suite, or the model missed real failures. Retry.
                logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
                continue
            test_completed_list = [ts == TestStatus.COMPLETED for ts in test_statuses]
            ordering_failed = any(
                test_completed_list[a] and test_failures_list[a] < test_failures_list[b]
                for a, b in ordering_checks
            )
            if ordering_failed:
                if conserv_retry < conservative_max_retries:
                    conserv_retry += 1
                    logger.warning(f"Failed to verify test failures. logs_parser-{logs_parser}")
                    continue
                else:
                    logger.warning(f"Conservative retry limit reached. logs_parser-{logs_parser}")
            is_success = True
            break

        if not is_success:
            msg = "Failed to synthesize logs parser."
            logger.error(msg)
            raise RuntimeError(msg)
        logger.info("Logs parser created successfully.")
        return logs_parser

    # --- Synthesizing the checker: one regex that detects aborted runs (no pass/fail summary). ---
    @staticmethod
    def _validate_checker(
        logs_checker: str,
        aborted_runs: set,
        test_logs_list: list,
        logger: logging.Logger,
    ) -> bool:
        """A logs checker is valid iff it matches exactly the runs flagged as aborted."""
        if not logs_checker:
            if aborted_runs:
                logger.warning("Empty logs checker but runs were flagged as aborted.")
                return False
            return True
        if not isinstance(logs_checker, str):
            logger.warning(f"Logs checker is not a string: {type(logs_checker).__name__}.")
            return False
        try:
            checker_re = re.compile(logs_checker, re.MULTILINE)
        except re.error as e:
            logger.warning(f"Invalid logs checker regex: {e}")
            return False
        for idx, test_logs in enumerate(test_logs_list, start=1):
            matched = checker_re.search(test_logs) is not None
            if matched != (idx in aborted_runs):
                logger.warning(f"Logs checker mismatch on run {idx}: matched={matched}, expected={idx in aborted_runs}.")
                return False
        return True

    @classmethod
    def _gen_checker(
        cls,
        test_logs_list: list,
        model: str,
        logger: logging.Logger,
        max_retries: int = 10,
    ) -> str | None:
        """Synthesize and return a per-instance startup-error checker (one regex) from the test logs
        (None when no run failed to start). Raises RuntimeError on failure."""
        logs_for_prompt = [clip_tokens(strip_ansi(test_logs), model, limit=(get_max_tokens(model) // 8))
            for test_logs in test_logs_list]

        messages = []
        for prompt_key, prompt in LOGS_CHECKER_PROMPT_TEMPLATE.items():
            if prompt_key == "system":
                messages.append({"role": "system", "content": Template(prompt).render()})
            else:
                messages.append({"role": "user", "content": Template(prompt).render(logs=logs_for_prompt)})

        logger.info("Synthesizing logs checker...")
        is_success = False
        for retry in range(max_retries):
            if retry:
                logger.info(f"Retrying... {retry + 1}/{max_retries}")
            try:
                response = completion(model=model, messages=messages)
            except Exception as e:
                logger.warning(f"Failed to get model response: {e}")
                continue
            record_llm_cost(response)
            message = response.choices[0].message
            response = extract_json_object(message.content)
            try:
                logs_checker = response["logs_checker"]
                aborted_runs = set(response["aborted_runs"])
            except (KeyError, TypeError) as e:
                logger.warning(f"Failed to parse model response as JSON: {e}")
                continue
            if not cls._validate_checker(logs_checker, aborted_runs, logs_for_prompt, logger):
                continue
            is_success = True
            break

        if not is_success:
            msg = "Failed to synthesize logs checker."
            logger.error(msg)
            raise RuntimeError(msg)
        logs_checker = logs_checker or None
        logger.info("Logs checker created successfully.")
        return logs_checker

    # --- The full {logs_parser, logs_checker} spec, reused or generated end to end. ---
    @classmethod
    def get(
        cls,
        test_logs_list: list,
        timed_out_list: list,
        model: str,
        log_dir: Path,
        logger: logging.Logger,
        ordering_checks: list[tuple[int, int]],
        allow_timeout,
        allow_startup_error,
        existing_data: dict = None,
        require_failures: bool = True,
        force: bool = False,
    ) -> "LogsCount":
        """Get the count handler end to end, reusing in priority order existing (stored env_spec) →
        cache (log_dir) → freshly generated; `force` skips both reuse tiers. The checker is built
        first, used to classify each run for the critical-abort rules (allow_timeout /
        allow_startup_error), then the parser. Raises on critical abort or synthesis failure."""
        existing_data = existing_data or {}
        cache = cls._load_cache(log_dir)

        if not force and "logs_checker" in existing_data:
            logger.info("Reusing existing logs checker.")
            logs_checker = existing_data["logs_checker"]
        elif not force and "logs_checker" in cache:
            logger.info("Reusing cached logs checker.")
            logs_checker = cache["logs_checker"]
        else:
            logs_checker = cls._gen_checker(test_logs_list, model=model, logger=logger)
            cls._save_cache(log_dir, {**cls._load_cache(log_dir), "logs_checker": logs_checker})

        test_statuses = [cls._check(logs_checker, test_logs, timed_out)
            for test_logs, timed_out in zip(test_logs_list, timed_out_list)]
        for id, test_status in enumerate(test_statuses):
            if test_status == TestStatus.TIMEOUT and not allow_timeout(id):
                msg = "Failed to run tests because of critical timeout."
                logger.error(msg)
                raise RuntimeError(msg)
            if test_status == TestStatus.STARTUP_ERROR and not allow_startup_error(id):
                msg = "Failed to run tests because of critical startup error."
                logger.error(msg)
                raise RuntimeError(msg)

        if not force and existing_data.get("logs_parser"):
            logger.info("Reusing existing logs parser.")
            logs_parser = existing_data["logs_parser"]
        elif not force and cache.get("logs_parser"):
            logger.info("Reusing cached logs parser.")
            logs_parser = cache["logs_parser"]
        else:
            logs_parser = cls._gen_parser(test_logs_list, test_statuses, model=model,
                logger=logger, ordering_checks=ordering_checks, require_failures=require_failures)
            cls._save_cache(log_dir, {**cls._load_cache(log_dir), "logs_parser": logs_parser})

        return cls(logs_parser=logs_parser, logs_checker=logs_checker)


# Two count-parser kinds: `count` for the repo's functional runs and `count_gen_sec` for the synthesized
# security-test runs — the same LogsCount machinery, each synthesized from its own run family's output (the
# sec run's format can differ from the functional run's), stored side by side in logs_handler.json.
LOGS_KINDS = {LogsCount.KIND: LogsCount, "count_gen_sec": LogsCount}
