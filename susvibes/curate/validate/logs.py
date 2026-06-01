import re
import json
import tiktoken
import logging
import threading
from pathlib import Path
from jinja2 import Template
from litellm import completion, completion_cost, get_max_tokens
from dotenv import load_dotenv

from susvibes.env import Env
from susvibes.curate.validate.prompts import (
    LOGS_PARSER_PROMPT_TEMPLATE,
    LOGS_CHECKER_PROMPT_TEMPLATE,
)
from susvibes.env_specs import (
    FAILURE_STATUSES,
    TestItemStatus,
    TestStatus,
)
from susvibes.utils import load_file, save_file

load_dotenv()

LOG_TEST_LOGS_PARSER = "logs_parser.json"
LOG_TEST_LOGS_CHECKER = "logs_checker.json"


# --- LLM cost tracking (thread-safe; run total over logs-parser + logs-checker calls) ---
_llm_cost_lock = threading.Lock()
_llm_cost_total = 0.0


def _record_llm_cost(response) -> None:
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


# ---------------------------------------------------------------------------
# Logs parser: a per-status regex that counts test outcomes (FAILED/PASSED/...).
# ---------------------------------------------------------------------------
def validate_logs_parser(logs_parser: dict, logger: logging.Logger,
    require_failures: bool = True) -> bool:
    if not isinstance(logs_parser, dict):
        logger.warning(f"Logs parser is not a dict: {type(logs_parser).__name__}.")
        return False
    try:
        logs_parser = {TestItemStatus(status).value: pattern for status, pattern
            in logs_parser.items() if pattern}
    except ValueError as e:
        logger.warning(f"Invalid logs parser: {e}")
        return False
    if not logs_parser:
        logger.warning("Invalid logs parser: no usable pattern.")
        return False
    if require_failures and all(status.value not in logs_parser for status in FAILURE_STATUSES):
        logger.warning(f"Invalid logs parser with no failure status.")
        return False
    return True

def get_logs_parser(
    env: Env,
    test_logs_list: list,
    test_statuses: list,
    model: str,
    log_dir: Path,
    logger: logging.Logger,
    ordering_checks: list[tuple[int, int]],
    max_retries: int = 10,
    conservative_max_retries: int = 5,
    require_failures: bool = True,
    force: bool = False
) -> None:
    """
    Synthesize a logs parser for the environment based on the test logs.
    Raises RuntimeError on failure; env is modified in place with the logs parser on success.
    """
    test_logs_parser_path = log_dir / LOG_TEST_LOGS_PARSER
    if test_logs_parser_path.exists() and not force:
        logger.info("Logs parser found; reusing.")
        env.logs_parser = load_file(test_logs_parser_path)
        return
    def clip_tokens(text: str, model: str, limit: int) -> str:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) > limit:
            tokens = tokens[-limit:]
        return enc.decode(tokens)

    test_logs_list = [clip_tokens(logs, model, limit=(get_max_tokens(model) // 8))
        for logs in test_logs_list]

    messages = []
    for prompt_key, prompt in LOGS_PARSER_PROMPT_TEMPLATE.items():
        if prompt_key == "system":
            messages.append({"role": "system", "content": Template(prompt).render(
                statuses=[status.value for status in TestItemStatus])})
        else:
            messages.append({"role": "user", "content": Template(prompt).render(
                logs=[logs for logs, status in zip(test_logs_list, test_statuses) if status])})

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
        _record_llm_cost(response)
        message = response.choices[0].message
        try:
            logs_parser = json.loads(message.content.split("```")[1].strip()) \
                if "```" in message.content else json.loads(message.content)
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Failed to parse model response as JSON: {e}")
            continue
        if not validate_logs_parser(logs_parser, logger, require_failures):
            continue
        env.logs_parser = logs_parser
        test_result_list, test_failures_list = [], []
        for logs, status in zip(test_logs_list, test_statuses):
            if not status:
                test_result_list.append({})
                continue
            try:
                test_result = env.parse_test_logs(logs, logger)
                test_result_list.append(test_result)
            except Exception as e:
                logger.warning(f"Failed to parse test logs: {e}. logs_parser-{logs_parser}")
                break
            test_failures_list.append(env.get_test_failures(test_result))
        if len(test_result_list) < len(test_logs_list):
            continue
        if any(tf < 0 for tf in test_failures_list):
            logger.warning(f"Invalid (negative) test failures detected. logs_parser-{logs_parser}")
            continue
        if not sum(test_failures_list):
            if require_failures:
                logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
                continue
            if any(ts != TestStatus.COMPLETION for ts in test_statuses):
                # A run did not complete (e.g. the masked task crashes): zero countable
                # failures is the expected structural break. Accept; the functional-break
                # verdict is made from run statuses downstream.
                is_success = True
                break
            # All runs completed yet no countable failures: either the change is not
            # covered by the suite, or the model missed real failures. Retry.
            logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
            continue
        test_completed_list = [ts == TestStatus.COMPLETION for ts in test_statuses]
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
    save_file(logs_parser, test_logs_parser_path)


# ---------------------------------------------------------------------------
# Logs checker: one regex that detects aborted runs (no pass/fail summary).
# ---------------------------------------------------------------------------
def validate_logs_checker(
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
    for idx, logs in enumerate(test_logs_list, start=1):
        matched = checker_re.search(logs) is not None
        if matched != (idx in aborted_runs):
            logger.warning(f"Logs checker mismatch on run {idx}: matched={matched}, expected={idx in aborted_runs}.")
            return False
    return True


def get_logs_checker(
    env: Env,
    test_logs_list: list,
    model: str,
    log_dir: Path,
    logger: logging.Logger,
    max_retries: int = 10,
    force: bool = False
) -> None:
    """
    Synthesize a per-instance startup-error checker (one regex) from the test logs.
    Raises RuntimeError on failure; env is modified in place with the logs checker on
    success (None when no run failed to start).
    """
    logs_checker_path = log_dir / LOG_TEST_LOGS_CHECKER
    if logs_checker_path.exists() and not force:
        logger.info("Logs checker found; reusing.")
        env.logs_checker = load_file(logs_checker_path)
        return
    def clip_tokens(text: str, model: str, limit: int) -> str:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) > limit:
            tokens = tokens[-limit:]
        return enc.decode(tokens)

    logs_for_prompt = [clip_tokens(logs, model, limit=(get_max_tokens(model) // 8))
        for logs in test_logs_list]

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
        _record_llm_cost(response)
        message = response.choices[0].message
        try:
            response = json.loads(message.content.split("```")[1].strip()) \
                if "```" in message.content else json.loads(message.content)
            logs_checker = response["logs_checker"]
            aborted_runs = set(response["aborted_runs"])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse model response as JSON: {e}")
            continue
        if not validate_logs_checker(logs_checker, aborted_runs, logs_for_prompt, logger):
            continue
        is_success = True
        break

    if not is_success:
        msg = "Failed to synthesize logs checker."
        logger.error(msg)
        raise RuntimeError(msg)
    env.logs_checker = logs_checker or None
    logger.info("Logs checker created successfully.")
    save_file(env.logs_checker, logs_checker_path)
