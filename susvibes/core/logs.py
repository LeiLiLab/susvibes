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

    def timed_out(self) -> bool:
        return self.status == TestStatus.TIMEOUT

    def aborted(self) -> bool:
        return self.status == TestStatus.ABORTED

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
        handler = LOGS_KINDS[kind].get(kind=kind, existing_data=existing_data.get(kind), **kwargs)
        return {**existing_data, kind: handler.to_dict()}

    @staticmethod
    def count_symb_res_errors(test_logs: str) -> int:
        """Count missing-symbol-resolution errors in the test logs."""
        return sum(len(re.findall(pattern, test_logs, re.MULTILINE))
            for pattern in TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS)

    @classmethod
    def _load_cache(cls, log_dir: Path, kind: str) -> dict:
        """`kind`'s cached data under log_dir's logs_handler.json ({} if not cached yet). Keyed by the
        kind asked for, not by cls.KIND: two kinds share this class, and keying by the class would
        hand the second one whatever the first just cached."""
        path = log_dir / cls.CACHE_FILE_NAME
        return (load_file(path) if path.exists() else {}).get(kind, {})

    @classmethod
    def _save_cache(cls, log_dir: Path, kind: str, data: dict) -> None:
        """Write `kind`'s data into log_dir's logs_handler.json, keeping the other kinds."""
        path = log_dir / cls.CACHE_FILE_NAME
        cache = load_file(path) if path.exists() else {}
        cache[kind] = data
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
            return TestStatus.ABORTED
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
            # "handle", not "parse": this covers the checker's status read as well as the count.
            msg = f"Failed to handle test logs: {e}"
            logger.error(msg)
            raise RuntimeError(msg)
        return PassFailureCount(status, failures)

    # --- Synthesizing the parser: a per-status regex that counts test outcomes. ---
    @classmethod
    def _validate_parser(
        cls,
        logs_parser: dict,
        declared: list,
        test_logs_list: list,
        logger: logging.Logger,
    ) -> str | None:
        """What is wrong with this parser, or None when every regex reproduces, on every run, the
        count the model declared for it. Mirrors `_validate_checker`: the candidate is measured
        against the model's own reading of the logs, and the problem is returned rather than a bool
        so the caller can both name the cause and hand it back for correction. Every mismatch is
        reported, not the first — a parser usually gets several statuses wrong at once."""
        if not isinstance(logs_parser, dict):
            return f"patterns is not an object but a {type(logs_parser).__name__}"
        try:
            usable = {TestItemStatus(item_status).value: pattern for item_status, pattern
                in logs_parser.items() if pattern}
        except ValueError as e:
            return f"patterns names a status that does not exist ({e})"
        if not usable:
            return "patterns holds no usable regex"
        if not isinstance(declared, list) or len(declared) != len(test_logs_list):
            return f"'runs' must be a list of {len(test_logs_list)} objects, one per run in order"

        problems = []
        for id, test_logs in enumerate(test_logs_list):
            if not isinstance(declared[id], dict):
                problems.append(f"run{id}: its 'runs' entry is not an object of per-status counts")
                continue
            try:
                counts = cls._parse(logs_parser, test_logs, logger)
            except Exception as e:
                problems.append(f"run{id}: a regex raised ({e}) — give exactly one capturing group of digits")
                continue
            for status in TestItemStatus:
                try:
                    declared_count = int(declared[id].get(status, 0))
                except (TypeError, ValueError):
                    problems.append(f"run{id}: the {status} count you declared is not a number")
                    continue
                if declared_count != int(counts.get(status, 0)):
                    problems.append(f"run{id}: your {status} regex captured "
                        f"{counts.get(status, 0)} but you declared {declared_count}")
        return "; ".join(problems) or None

    @classmethod
    def _gen_parser(
        cls,
        test_logs_list: list,
        model: str,
        logger: logging.Logger,
        max_retries: int = 10,
    ) -> dict:
        """Synthesize and return a logs parser from the test logs. Raises RuntimeError on failure.

        The model declares, per run, the count it reads for every status AND the regexes; a candidate
        is accepted only when each regex reproduces its own declared count on every run. That
        self-check is the whole correctness gate: it compares the regex against the model's
        independent reading of the summary, so nothing here judges whether the counts themselves look
        right — whether a suite that fails nowhere is acceptable is the caller's verdict, not this
        one's. A rejection is fed back with what mismatched, so the model corrects the regex rather
        than resampling blind."""
        clipped_logs = [clip_tokens(strip_ansi(test_logs), model, limit=(get_max_tokens(model) // 8))
            for test_logs in test_logs_list]

        base_messages = []
        for prompt_key, prompt in LOGS_PARSER_PROMPT_TEMPLATE.items():
            if prompt_key == "system":
                base_messages.append({"role": "system", "content": Template(prompt).render(
                    statuses=list(TestItemStatus))})
            else:
                base_messages.append({"role": "user", "content": Template(prompt).render(
                    logs=clipped_logs)})
        messages = base_messages

        logger.info("Synthesizing logs parser...")
        # Why the last attempt was rejected, carried into the exception: "failed to synthesize" alone
        # cannot tell a model that will not answer from regexes that contradict their own counts.
        last_reason = "no attempt was made"
        for retry in range(max_retries):
            if retry:
                logger.info(f"Retrying... {retry + 1}/{max_retries}")
            try:
                response = completion(model=model, messages=messages, max_tokens=get_max_tokens(model))
            except Exception as e:
                last_reason = f"the model did not respond ({e})"
                logger.warning(f"Failed to get model response: {e}")
                messages = base_messages   # drop any poisoned exchange so the next attempt can recover
                continue
            record_llm_cost(response)
            raw = response.choices[0].message.content or ""
            if not raw.strip():
                # Never feed an empty reply back: an empty content block is rejected by the API, and
                # an empty reply has nothing to correct. Retry from the base prompt instead.
                last_reason = "the model returned an empty response"
                logger.warning("Empty model response; retrying from the base prompt.")
                messages = base_messages
                continue
            result = extract_json_object(raw)

            def retry_with(correction: str) -> list:
                """The next attempt's messages: the prompt, this reply, and what to fix. Only the
                latest exchange is carried, so ten retries do not accumulate ten replies."""
                return base_messages + [{"role": "assistant", "content": raw},
                    {"role": "user", "content": f"{correction} Output only the corrected JSON."}]

            if not isinstance(result, dict) or "patterns" not in result or "runs" not in result:
                last_reason = "the model's response held no JSON object with both 'runs' and 'patterns'"
                logger.warning(f"Failed to parse model response as JSON: {last_reason}.")
                messages = retry_with("Your reply had no JSON object with both 'runs' and 'patterns'.")
                continue
            logs_parser, declared = result["patterns"], result["runs"]
            problem = cls._validate_parser(logs_parser, declared, clipped_logs, logger)
            if problem:
                last_reason = f"the regexes contradict the counts the model declared ({problem})"
                logger.warning(f"Parser self-check failed: {problem}")
                messages = retry_with(f"Your patterns do not reproduce your declared counts: {problem}. "
                    "Match the digits in the summary line (e.g. '===== 7 failed, 4 passed ====='), "
                    'or record 0 and use "" for a status with no number. '
                    "Keep the declared counts and fix the regexes.")
                continue
            # The self-check ran on the clipped logs; `handle` will apply this parser to the full
            # ones. Prove it survives them — a regex whose group is not digits raises there, and a
            # parser that raises at handle time is a wrong count nobody sees.
            try:
                for test_logs in test_logs_list:
                    cls._parse(logs_parser, test_logs, logger)
            except Exception as e:
                last_reason = f"the parser raised on the unclipped logs ({e})"
                logger.warning(f"Parser failed on the full logs: {e}")
                messages = retry_with(
                    f"Applied to the untruncated logs your patterns raised: {e}. "
                    "Every pattern must capture exactly one group of digits.")
                continue
            logger.info("Logs parser created successfully.")
            return logs_parser

        msg = f"Failed to synthesize logs parser: {last_reason}"
        logger.error(msg)
        raise RuntimeError(msg)

    # --- Synthesizing the checker: one regex that detects aborted runs (no pass/fail summary). ---
    @staticmethod
    def _validate_checker(
        logs_checker: str,
        aborted_runs: set,
        test_logs_list: list,
        logger: logging.Logger,
    ) -> str | None:
        """What is wrong with this checker, or None when it matches exactly the runs the model
        flagged as aborted. The self-check the parser's is modelled on: the regex is measured against
        the model's own reading of the runs. Returns the problem rather than a bool so the caller can
        both name the cause and hand it back for correction."""
        if not logs_checker:
            if aborted_runs:
                return f"you gave no regex but flagged runs {sorted(aborted_runs)} as aborted"
            return None
        if not isinstance(logs_checker, str):
            return f"logs_checker is not a string but a {type(logs_checker).__name__}"
        try:
            checker_re = re.compile(logs_checker, re.MULTILINE)
        except re.error as e:
            return f"logs_checker is not a valid regex ({e})"
        for idx, test_logs in enumerate(test_logs_list, start=1):
            matched = checker_re.search(test_logs) is not None
            if matched != (idx in aborted_runs):
                return (f"your regex {'matches' if matched else 'does not match'} run {idx}, "
                    f"which you did {'not ' if idx not in aborted_runs else ''}flag as aborted")
        return None

    @classmethod
    def _gen_checker(
        cls,
        test_logs_list: list,
        model: str,
        logger: logging.Logger,
        max_retries: int = 10,
    ) -> str | None:
        """Synthesize and return a per-instance startup-error checker (one regex) from the test logs
        (None when no run failed to start). Raises RuntimeError on failure.

        The model flags which runs aborted AND gives the regex; a candidate is accepted only when the
        regex matches exactly those runs. Same shape as the parser's synthesis, down to feeding a
        rejection back so the model corrects its regex rather than resampling blind."""
        clipped_logs = [clip_tokens(strip_ansi(test_logs), model, limit=(get_max_tokens(model) // 8))
            for test_logs in test_logs_list]

        base_messages = []
        for prompt_key, prompt in LOGS_CHECKER_PROMPT_TEMPLATE.items():
            if prompt_key == "system":
                base_messages.append({"role": "system", "content": Template(prompt).render()})
            else:
                base_messages.append({"role": "user", "content": Template(prompt).render(logs=clipped_logs)})
        messages = base_messages

        logger.info("Synthesizing logs checker...")
        last_reason = "no attempt was made"
        for retry in range(max_retries):
            if retry:
                logger.info(f"Retrying... {retry + 1}/{max_retries}")
            try:
                response = completion(model=model, messages=messages, max_tokens=get_max_tokens(model))
            except Exception as e:
                last_reason = f"the model did not respond ({e})"
                logger.warning(f"Failed to get model response: {e}")
                messages = base_messages   # drop any poisoned exchange so the next attempt can recover
                continue
            record_llm_cost(response)
            raw = response.choices[0].message.content or ""
            if not raw.strip():
                # Never feed an empty reply back: an empty content block is rejected by the API, and
                # an empty reply has nothing to correct. Retry from the base prompt instead.
                last_reason = "the model returned an empty response"
                logger.warning("Empty model response; retrying from the base prompt.")
                messages = base_messages
                continue
            result = extract_json_object(raw)

            def retry_with(correction: str) -> list:
                """The next attempt's messages: the prompt, this reply, and what to fix. Only the
                latest exchange is carried, so ten retries do not accumulate ten replies."""
                return base_messages + [{"role": "assistant", "content": raw},
                    {"role": "user", "content": f"{correction} Output only the corrected JSON."}]

            try:
                logs_checker = result["logs_checker"]
                aborted_runs = set(result["aborted_runs"])
            except (KeyError, TypeError) as e:
                last_reason = f"the model's response held no JSON object with both keys ({e})"
                logger.warning(f"Failed to parse model response as JSON: {e}")
                messages = retry_with("Your reply had no JSON object with both 'logs_checker' and 'aborted_runs'.")
                continue
            problem = cls._validate_checker(logs_checker, aborted_runs, clipped_logs, logger)
            if problem:
                last_reason = f"the regex contradicts the runs the model flagged ({problem})"
                logger.warning(f"Checker self-check failed: {problem}")
                messages = retry_with(f"Your checker does not match what you flagged: {problem}. "
                    "Anchor on the abort signature the aborted runs share and the completed ones lack, "
                    'or use "" if no run aborted.')
                continue
            logger.info("Logs checker created successfully.")
            return logs_checker or None

        msg = f"Failed to synthesize logs checker: {last_reason}"
        logger.error(msg)
        raise RuntimeError(msg)

    # --- The full {logs_parser, logs_checker} spec, reused or generated end to end. ---
    @classmethod
    def get(
        cls,
        test_logs_list: list,
        timed_out_list: list,
        model: str,
        log_dir: Path,
        logger: logging.Logger,
        kind: str,
        existing_data: dict = None,
        force: bool = False,
    ) -> "LogsCount":
        """Get the count handler end to end, reusing in priority order existing (stored env_spec) →
        cache (log_dir) → freshly generated; `force` skips both reuse tiers. The checker is built
        first, then the parser — from the runs that completed, since a run with no summary has no
        counts to declare or capture. Raises only when a spec cannot be synthesized; whether the runs
        it describes are acceptable is the caller's verdict, not this one's."""
        existing_data = existing_data or {}
        cache = cls._load_cache(log_dir, kind)

        if not force and "logs_checker" in existing_data:
            logger.info("Reusing existing logs checker.")
            logs_checker = existing_data["logs_checker"]
        elif not force and "logs_checker" in cache:
            logger.info("Reusing cached logs checker.")
            logs_checker = cache["logs_checker"]
        else:
            logs_checker = cls._gen_checker(test_logs_list, model=model, logger=logger)
            cls._save_cache(log_dir, kind, {**cls._load_cache(log_dir, kind), "logs_checker": logs_checker})

        test_statuses = [cls._check(logs_checker, test_logs, timed_out)
            for test_logs, timed_out in zip(test_logs_list, timed_out_list)]

        if not force and existing_data.get("logs_parser"):
            logger.info("Reusing existing logs parser.")
            logs_parser = existing_data["logs_parser"]
        elif not force and cache.get("logs_parser"):
            logger.info("Reusing cached logs parser.")
            logs_parser = cache["logs_parser"]
        else:
            completed_logs = [test_logs for test_logs, test_status
                in zip(test_logs_list, test_statuses) if test_status == TestStatus.COMPLETED]
            if completed_logs:
                logs_parser = cls._gen_parser(completed_logs, model=model, logger=logger)
                cls._save_cache(log_dir, kind, {**cls._load_cache(log_dir, kind), "logs_parser": logs_parser})
            else:
                # Nothing completed: no summary to synthesize from, and no count worth having —
                # every run compares as infinitely broken, and the caller's abort rules reject it.
                logger.warning("No run completed; leaving the parser unsynthesized.")
                logs_parser = None

        return cls(logs_parser=logs_parser, logs_checker=logs_checker)


# Two count-parser kinds: `count` for the repo's functional runs and `count_gen_sec` for the synthesized
# security-test runs — the same LogsCount machinery, each synthesized from its own run family's output (the
# sec run's format can differ from the functional run's), stored side by side in logs_handler.json.
LOGS_KINDS = {LogsCount.KIND: LogsCount, "count_gen_sec": LogsCount}
