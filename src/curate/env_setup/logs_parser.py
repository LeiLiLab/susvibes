import json
import tiktoken
import logging
from pathlib import Path
from jinja2 import Template
from litellm import completion, get_max_tokens
from dotenv import load_dotenv

from constants import *
from env import Env
from curate.prompts import LOGS_PARSER_PROMPT_TEMPLATE
from env_specs import (
    FAILURE_STATUSES, 
    TestItemStatus, 
    TestStatus,
)
from curate.utils import load_file, save_file

load_dotenv()

LOG_TEST_LOGS_PARSER = "logs_parser.json"

def validate_logs_parser(logs_parser: dict, logger: logging.Logger) -> bool:
    try:
        logs_parser = {TestItemStatus(status).value: pattern for status, pattern 
            in logs_parser.items() if pattern} 
    except ValueError as e:
        logger.warning(f"Invalid logs parser: {e}")
        return False
    if all(status.value not in logs_parser for status in FAILURE_STATUSES):
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
    max_retries: int = 10,
    conservative_max_retries: int = 5,
    force: bool = False
) -> bool:
    """
    Synthesize a logs parser for the environment based on the test logs.
    Returns a boolean success flag, env is modified in place with the logs parser.
    """
    test_logs_parser_path = log_dir / LOG_TEST_LOGS_PARSER
    if test_logs_parser_path.exists() and not force:
        logger.info("Logs parser found; reusing.")
        env.logs_parser = load_file(test_logs_parser_path)
        return True
    def clip_tokens(text: str, model: str, limit: int) -> str:
        enc = tiktoken.encoding_for_model(model)
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
            message = completion(model=model, messages=messages).choices[0].message
        except Exception as e:
            logger.warning(f"Failed to get model response: {e}")
            continue
        logs_parser = json.loads(message.content.split("```")[1].strip()) \
            if "```" in message.content else json.loads(message.content)
        if not validate_logs_parser(logs_parser, logger):
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
        if not sum(test_failures_list) or any(tf < 0 for tf in test_failures_list):
            logger.warning(f"Invalid test failures detected. logs_parser-{logs_parser}")
            continue
        base_tf, rollback_tf, _, sec_test_tf, task_tf = test_failures_list
        test_completed_list = [ts == TestStatus.COMPLETION.value for ts in test_statuses]
        _, _, _, sec_test_completed, task_completed = test_completed_list
        if sec_test_completed and sec_test_tf < base_tf or \
            task_completed and task_tf < rollback_tf:
                if conserv_retry < conservative_max_retries:
                    conserv_retry += 1
                    logger.warning(f"Failed to verify test failures. logs_parser-{logs_parser}")
                    continue
                else:
                    logger.warning(f"Conservative retry limit reached. logs_parser-{logs_parser}")
        is_success = True
        break

    if not is_success:
        logger.error("Failed to synthesize logs parser.")
        return False
    logger.info("Logs parser created successfully.")
    save_file(logs_parser, test_logs_parser_path)
    return True