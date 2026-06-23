import asyncio
import copy
import json
import os
from typing import Any

import pandas as pd
import toml
from jinja2 import Environment, FileSystemLoader

import openhands.agenthub
from evaluation.utils.shared import (
    EvalException,
    EvalMetadata,
    EvalOutput,
    assert_and_raise,
    check_maximum_retries_exceeded,
    codeact_user_response,
    get_default_sandbox_config_for_eval,
    get_metrics,
    get_openhands_config_for_eval,
    is_fatal_evaluation_error,
    make_metadata,
    prepare_dataset,
    reset_logger_for_multiprocessing,
    run_evaluation,
    update_llm_config_for_completions_logging,
)
from openhands.controller.state.state import State
from openhands.core.config import (
    AgentConfig,
    OpenHandsConfig,
    get_evaluation_parser,
    get_llm_config_arg,
)
from openhands.core.config.condenser_config import NoOpCondenserConfig
from openhands.core.config.utils import get_condenser_config_arg
from openhands.core.logger import openhands_logger as logger
from openhands.core.main import create_runtime, run_controller
from openhands.events.action import CmdRunAction, FileReadAction, MessageAction
from openhands.events.observation import (
    CmdOutputObservation,
    FileReadObservation,
    ErrorObservation,
)
from openhands.events.serialization.event import event_to_dict
from openhands.runtime.base import Runtime
from openhands.utils.async_utils import call_async_from_sync
from openhands.utils.shutdown_listener import sleep_if_should_continue

# Global flags - adjust these based on susvibes requirements
USE_HINT_TEXT = os.environ.get('USE_HINT_TEXT', 'false').lower() == 'true'
RUN_WITH_BROWSING = os.environ.get('RUN_WITH_BROWSING', 'false').lower() == 'true'
ENABLE_LLM_EDITOR = os.environ.get('ENABLE_LLM_EDITOR', 'false').lower() == 'true'

AGENT_CLS_TO_FAKE_USER_RESPONSE_FN = {
    'CodeActAgent': codeact_user_response,
}


def get_instruction(instance: pd.Series, metadata: EvalMetadata) -> MessageAction:
    """Create instruction for susvibes task.

    Args:
        instance: Row from susvibes dataset containing 'problem_statement' and 'image_name'
        metadata: Evaluation metadata

    Returns:
        MessageAction with the task instruction
    """
    # For susvibes, we use the problem_statement directly as the instruction
    configs_dir = os.path.join(os.path.dirname(__file__), 'configs')
    env = Environment(loader=FileSystemLoader(configs_dir))
    template = env.get_template('default.j2')
    instruction = template.render(
        working_dir='/project',
        problem_statement=instance['problem_statement'],
        base_commit=instance['base_commit']
    )

    if RUN_WITH_BROWSING:
        instruction += (
            '<IMPORTANT!>\nYou SHOULD NEVER attempt to browse the web. </IMPORTANT!>\n'
        )

    return MessageAction(content=instruction)


def get_config(
    instance: pd.Series,
    metadata: EvalMetadata,
) -> OpenHandsConfig:
    """Get OpenHands config for susvibes evaluation.

    Args:
        instance: Row from susvibes dataset containing 'image_name'
        metadata: Evaluation metadata

    Returns:
        OpenHandsConfig object
    """
    # Use the image_name from the dataset as the base container image
    base_container_image = instance['image_name']
    logger.info(
        f'Using container image: {base_container_image}. '
        f'Please make sure this image exists.'
    )

    sandbox_config = get_default_sandbox_config_for_eval()
    sandbox_config.base_container_image = base_container_image
    sandbox_config.enable_auto_lint = True  # Enable linting for better code quality
    sandbox_config.use_host_network = False
    sandbox_config.platform = 'linux/amd64'  # Standard platform
    # Use default resource factor for susvibes tasks
    sandbox_config.remote_runtime_resource_factor = 1.0

    config = get_openhands_config_for_eval(
        metadata=metadata,
        enable_browser=RUN_WITH_BROWSING,
        runtime=os.environ.get('RUNTIME', 'docker'),
        sandbox_config=sandbox_config,
    )

    config.set_llm_config(
        update_llm_config_for_completions_logging(
            metadata.llm_config, metadata.eval_output_dir, instance['instance_id']
        )
    )

    agent_config = AgentConfig(
        enable_jupyter=False,  # Disable jupyter for susvibes tasks
        enable_browsing=RUN_WITH_BROWSING,
        enable_llm_editor=ENABLE_LLM_EDITOR,
        enable_mcp=False,
        condenser=metadata.condenser_config,
        enable_prompt_extensions=False,
    )
    config.set_agent_config(agent_config)
    return config


def initialize_runtime(
    runtime: Runtime,
    instance: pd.Series,
    metadata: EvalMetadata,
):
    """Initialize the runtime for susvibes tasks.

    This function sets up the runtime environment before the agent runs.
    Unlike swe_bench, susvibes tasks work in the /project directory.
    """
    logger.info('-' * 30)
    logger.info('BEGIN Runtime Initialization Fn')
    logger.info('-' * 30)
    obs: CmdOutputObservation

    # The OpenHands runtime activates a micromamba environment that shadows the
    # container's native Python (which has project dependencies pre-installed).
    # We cannot use `micromamba deactivate` because it sets PS1='' which breaks
    # the BashSession's prompt-based output parsing.
    # Instead, we get the original PATH from the base image (before OpenHands
    # overlay) and prepend it, so the container's native Python takes priority.
    base_image = instance['image_name']
    try:
        import subprocess
        result = subprocess.run(
            ['docker', 'run', '--rm', base_image, 'bash', '-c', 'echo $PATH'],
            capture_output=True, text=True, timeout=30,
        )
        native_path = result.stdout.strip()
    except Exception as e:
        logger.warning(f'Failed to get PATH from base image {base_image}: {e}')
        native_path = '/usr/local/bin:/usr/local/sbin'

    logger.info(f'Native PATH from base image: {native_path}')
    action = CmdRunAction(command=f'export PATH="{native_path}:$PATH"')
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert_and_raise(
        obs.exit_code == 0,
        f'Failed to update PATH: {str(obs)}',
    )

    # Set environment variables and basic configuration
    action = CmdRunAction(
        command=f"""echo 'export SUSVIBES_INSTANCE_ID={instance['instance_id']}' >> ~/.bashrc && echo 'export PIP_CACHE_DIR=~/.cache/pip' >> ~/.bashrc"""
    )
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert_and_raise(
        obs.exit_code == 0,
        f'Failed to export environment variables: {str(obs)}',
    )

    # Navigate to the project directory (where susvibes tasks should be completed)
    action = CmdRunAction(command='cd /project')
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert_and_raise(
        obs.exit_code == 0,
        f'Failed to cd to /project: {str(obs)}',
    )

    # Check if /project directory has any git repository
    action = CmdRunAction(command='git status')
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    if obs.exit_code != 0:
        # Initialize git repository if not exists
        logger.info('Initializing git repository in /project')
        action = CmdRunAction(command='git init')
        action.set_hard_timeout(600)
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})
        assert_and_raise(obs.exit_code == 0, f'Failed to git init: {str(obs)}')

        # Configure git user
        action = CmdRunAction(command='git config user.name "OpenHands" && git config user.email "openhands@example.com"')
        action.set_hard_timeout(600)
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})
        assert_and_raise(obs.exit_code == 0, f'Failed to configure git: {str(obs)}')

        # Create initial commit if needed
        action = CmdRunAction(command='git add . && git commit -m "Initial commit" || echo "No files to commit"')
        action.set_hard_timeout(600)
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    logger.info('-' * 30)
    logger.info('END Runtime Initialization Fn')
    logger.info('-' * 30)


def complete_runtime(
    runtime: Runtime,
    instance: pd.Series,
) -> dict[str, Any]:
    """Complete the runtime and extract git patch for susvibes evaluation.

    This function is called after the agent has run to collect the results.
    It generates a git patch similar to swe_bench format.
    """
    logger.info('-' * 30)
    logger.info('BEGIN Runtime Completion Fn')
    logger.info('-' * 30)
    obs: CmdOutputObservation

    action = CmdRunAction(command='cd /project')
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    if obs.exit_code == -1:
        # Handle running command case
        logger.info('The previous command is still running, trying to interrupt...')
        action = CmdRunAction(command='C-c')
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})

        # Try cd again
        action = CmdRunAction(command='cd /project')
        action.set_hard_timeout(600)
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert_and_raise(
        isinstance(obs, CmdOutputObservation) and obs.exit_code == 0,
        f'Failed to cd to /project: {str(obs)}',
    )

    # Add all files to git
    action = CmdRunAction(command='git add -A')
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert_and_raise(
        isinstance(obs, CmdOutputObservation) and obs.exit_code == 0,
        f'Failed to git add -A: {str(obs)}',
    )

    # Generate git patch from initial state
    n_retries = 0
    git_patch = None
    while n_retries < 5:
        action = CmdRunAction(command='git diff --no-color --cached > patch.diff')
        action.set_hard_timeout(max(300 + 100 * n_retries, 600))
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})
        n_retries += 1

        if isinstance(obs, CmdOutputObservation):
            if obs.exit_code == 0:
                # Read the patch file
                action = FileReadAction(path='patch.diff')
                action.set_hard_timeout(max(300 + 100 * n_retries, 600))
                logger.info(action, extra={'msg_type': 'ACTION'})
                obs = runtime.run_action(action)
                logger.info(obs, extra={'msg_type': 'OBSERVATION'})
                if isinstance(obs, FileReadObservation):
                    git_patch = obs.content
                    break
                elif isinstance(obs, ErrorObservation):
                    # Fall back to cat "patch.diff" to get the patch
                    assert 'File could not be decoded as utf-8' in obs.content
                    action = CmdRunAction(command='cat patch.diff')
                    action.set_hard_timeout(max(300 + 100 * n_retries, 600))
                    logger.info(action, extra={'msg_type': 'ACTION'})
                    obs = runtime.run_action(action)
                    assert isinstance(obs, CmdOutputObservation) and obs.exit_code == 0
                    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
                    git_patch = obs.content
                    break
                else:
                    assert_and_raise(False, f'Unexpected observation type: {str(obs)}')
            else:
                logger.info('Failed to get git diff, retrying...')
                sleep_if_should_continue(10)
        elif isinstance(obs, ErrorObservation):
            logger.error(f'Error occurred: {obs.content}. Retrying...')
            sleep_if_should_continue(10)
        else:
            assert_and_raise(False, f'Unexpected observation type: {str(obs)}')

    assert_and_raise(git_patch is not None, 'Failed to get git diff (None)')

    logger.info('-' * 30)
    logger.info('END Runtime Completion Fn')
    logger.info('-' * 30)
    return {'git_patch': git_patch}


def process_instance(
    instance: pd.Series,
    metadata: EvalMetadata,
    reset_logger: bool = True,
    runtime_failure_count: int = 0,
) -> EvalOutput:
    """Process a single susvibes instance."""
    config = get_config(instance, metadata)

    # Setup the logger properly for multiprocessing
    if reset_logger:
        log_dir = os.path.join(metadata.eval_output_dir, 'infer_logs')
        reset_logger_for_multiprocessing(logger, instance.instance_id, log_dir)
    else:
        logger.info(f'Starting evaluation for instance {instance.instance_id}.')

    # Increase resource_factor with increasing attempt_id
    if runtime_failure_count > 0:
        config.sandbox.remote_runtime_resource_factor = min(
            config.sandbox.remote_runtime_resource_factor * (2**runtime_failure_count),
            8,
        )
        logger.warning(
            f'This is the {runtime_failure_count + 1}th attempt for instance {instance.instance_id}, setting resource factor to {config.sandbox.remote_runtime_resource_factor}'
        )

    metadata = copy.deepcopy(metadata)
    metadata.details['runtime_failure_count'] = runtime_failure_count
    metadata.details['remote_runtime_resource_factor'] = (
        config.sandbox.remote_runtime_resource_factor
    )

    runtime = create_runtime(config)
    call_async_from_sync(runtime.connect)

    try:
        initialize_runtime(runtime, instance, metadata)

        message_action = get_instruction(instance, metadata)

        # Run the agent controller
        state: State | None = asyncio.run(
            run_controller(
                config=config,
                initial_user_action=message_action,
                runtime=runtime,
                fake_user_response_fn=AGENT_CLS_TO_FAKE_USER_RESPONSE_FN[
                    metadata.agent_class
                ],
            )
        )

        # Handle fatal errors
        if is_fatal_evaluation_error(state.last_error):
            raise EvalException('Fatal error detected: ' + state.last_error)

        # Get git patch (similar to SWE-bench format)
        return_val = complete_runtime(runtime, instance)
        git_patch = return_val['git_patch']
        logger.info(
            f'Got git diff for instance {instance.instance_id}:\n--------\n{git_patch}\n--------'
        )
    finally:
        runtime.close()

    # Prepare test result (maintain SWE-bench compatible format)
    test_result = {
        'git_patch': git_patch,
    }

    if state is None:
        raise ValueError('State should not be None.')

    # Get event history and metrics
    histories = [event_to_dict(event) for event in state.history]
    metrics = get_metrics(state)

    # Save the output in SWE-bench compatible format
    instruction = message_action.content
    output = EvalOutput(
        instance_id=instance.instance_id,
        instruction=instruction,
        instance=instance.to_dict(),  # Include full instance data
        test_result=test_result,
        metadata=metadata,
        history=histories,
        metrics=metrics,
        error=state.last_error if state and state.last_error else None,
    )

    # Append prediction incrementally so preds.jsonl is available during evaluation
    predictions_file = os.path.join(metadata.eval_output_dir, 'preds.jsonl')
    prediction = {
        'instance_id': instance.instance_id,
        'model_name_or_path': f"default__{metadata.eval_output_dir.split('/')[-1]}",
        'model_patch': git_patch,
    }
    with open(predictions_file, 'a') as f:
        f.write(json.dumps(prediction) + '\n')

    return output


def filter_dataset(dataset: pd.DataFrame, filter_column: str = 'instance_id') -> pd.DataFrame:
    """Filter dataset based on config.toml or environment variables."""
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.toml')
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            data = toml.load(file)
            if 'selected_ids' in data:
                selected_ids = data['selected_ids']
                logger.info(
                    f'Filtering {len(selected_ids)} tasks from "selected_ids"...'
                )
                subset = dataset[dataset[filter_column].isin(selected_ids)]
                logger.info(f'Retained {subset.shape[0]} tasks after filtering')
                return subset

    skip_ids = os.environ.get('SKIP_IDS', '').split(',')
    if len(skip_ids) > 0 and skip_ids != ['']:
        logger.info(f'Filtering {len(skip_ids)} tasks from "SKIP_IDS"...')
        return dataset[~dataset[filter_column].isin(skip_ids)]
    return dataset


def load_susvibes_dataset(dataset_path: str) -> pd.DataFrame:
    """Load susvibes dataset from JSONL file."""
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f'Dataset file not found: {dataset_path}')

    data = []
    with open(dataset_path, 'r') as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))

    df = pd.DataFrame(data)
    logger.info(f'Loaded {len(df)} instances from {dataset_path}')

    # Ensure required columns exist
    required_columns = ['image_name', 'problem_statement']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f'Missing required columns in dataset: {missing_columns}')

    # Add instance_id if not present (for compatibility with evaluation framework)
    if 'instance_id' not in df.columns:
        df['instance_id'] = df.index.map(lambda x: f'susvibes_{x}')

    return df

def to_preds(output_file: str, metadata: EvalMetadata) -> None:
    """Convert raw output JSONL to predictions.jsonl format."""
    if not os.path.exists(output_file):
        raise FileNotFoundError(f'Output file not found: {output_file}')

    predictions_file = os.path.join(os.path.dirname(output_file), 'preds.jsonl')
    with open(output_file, 'r') as infile, open(predictions_file, 'w') as outfile:
        for line in infile:
            if line.strip():
                record = json.loads(line)
                prediction = {
                    'instance_id': record['instance_id'],
                    'model_name_or_path': f"default__{metadata.eval_output_dir.split('/')[-1]}",
                    'model_patch': record['test_result']['git_patch'],
                }
                outfile.write(json.dumps(prediction) + "\n")

    logger.info(f'Converted output to preds format: {predictions_file}')

if __name__ == '__main__':
    parser = get_evaluation_parser()
    parser.add_argument(
        '--dataset_path',
        type=str,
        required=True,
        help='Path to susvibes dataset JSONL file (absolute, or relative to cwd)',
    )

    args, _ = parser.parse_known_args()

    # Load susvibes dataset
    if os.path.isabs(args.dataset_path):
        dataset_path = args.dataset_path
    else:
        dataset_path = os.path.join(os.getcwd(), args.dataset_path)
    susvibes_dataset = load_susvibes_dataset(dataset_path)

    # Filter dataset if needed
    susvibes_tests = filter_dataset(susvibes_dataset, 'instance_id')
    logger.info(f'Loaded susvibes dataset: {len(susvibes_tests)} tasks')

    # Get LLM config
    llm_config = None
    if args.llm_config:
        llm_config = get_llm_config_arg(args.llm_config)
        llm_config.log_completions = True
        # modify_params must be False for evaluation reproducibility
        llm_config.modify_params = False

    if llm_config is None:
        raise ValueError(f'Could not find LLM config: --llm_config {args.llm_config}')

    # Get condenser config from environment variable
    condenser_name = os.environ.get('EVAL_CONDENSER')
    if condenser_name:
        condenser_config = get_condenser_config_arg(condenser_name)
        if condenser_config is None:
            raise ValueError(
                f'Could not find Condenser config: EVAL_CONDENSER={condenser_name}'
            )
    else:
        # Default to NoOpCondenser
        condenser_config = NoOpCondenserConfig()
        logger.debug(
            'No Condenser config provided via EVAL_CONDENSER, using NoOpCondenser.'
        )

    # Set up evaluation metadata
    details = {'mode': 'susvibes'}  # Custom mode for susvibes
    _agent_cls = openhands.agenthub.Agent.get_cls(args.agent_cls)

    dataset_description = 'susvibes'
    metadata = make_metadata(
        llm_config,
        dataset_description,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details=details,
        condenser_config=condenser_config,
    )

    output_file = os.path.join(metadata.eval_output_dir, 'output.jsonl')
    predictions_file = os.path.join(metadata.eval_output_dir, 'preds.jsonl')
    print(f'### OUTPUT FILE: {output_file} ###')
    print(f'### PREDICTIONS FILE: {predictions_file} ###')

    # Prepare dataset for evaluation
    instances = prepare_dataset(susvibes_tests, output_file, args.eval_n_limit)

    # Run evaluation
    run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        process_instance,
        timeout_seconds=8 * 60 * 60,  # 8 hours per instance should be enough
        max_retries=5,
    )

    # Check for maximum retries exceeded
    check_maximum_retries_exceeded(metadata.eval_output_dir)

    # Rebuild preds from the complete output to ensure consistency
    to_preds(output_file, metadata)
