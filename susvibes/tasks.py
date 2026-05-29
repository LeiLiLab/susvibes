import logging
import docker.errors
from tqdm import tqdm
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.constants import *
from susvibes.env import Env
from susvibes.runners import detect_runner
from susvibes.runners.base import (
    AbortReason,
    SessionResult,
    TestOutcome,
    TestRunnerAdapter,
)
from susvibes.strategies.tools import eval_selected_cwes, get_cwe_selection_stats
from susvibes.utils import (
    load_file,
    save_file,
    touched_files,
    filter_target_files,
    filter_binary_files,
    setup_instance_logger
)

LOG_INSTANCE = "run_instance.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_REPORT = "report.json"
EVALUATION_RUNS = ["func", "sec"]


def _decide_pass(
    run_name: str,
    result: SessionResult,
    expected_failures: int,
    added_tests: list[tuple[str, str]],
    adapter: TestRunnerAdapter,
) -> tuple[bool, str | None]:
    """Language-agnostic pass rule consuming a SessionResult.

    Returns ``(passed, reason)`` where *reason* is ``None`` on success
    or a short tag explaining the failure.
    """
    if not result.terminated_normally:
        # Smart maxfail: allow sec runs where all security tests verifiably
        # passed before the cutoff.
        if (
            run_name == "sec"
            and added_tests
            and result.abort_reason is AbortReason.PREMATURE_ABORT
        ):
            all_passed = all(
                any(
                    adapter.match_test(tid, fp, tn)
                    and result.per_test.get(tid) is TestOutcome.PASSED
                    for tid in result.per_test
                )
                for fp, tn in added_tests
            )
            if not all_passed:
                return False, f"session_aborted:{result.abort_reason.value}"
            # all security tests passed — continue to count checks below
        else:
            return False, f"session_aborted:{result.abort_reason.value}"

    # Positive-evidence check FIRST for sec (when per_test available).
    # If all security tests verifiably passed, excess unrelated failures
    # do not block SecPass.
    if run_name == "sec" and added_tests and result.per_test:
        for file_path, test_name in added_tests:
            found_passed = any(
                adapter.match_test(tid, file_path, test_name)
                and result.per_test[tid] is TestOutcome.PASSED
                for tid in result.per_test
            )
            if not found_passed:
                return False, f"sec_test_not_passed:{file_path}::{test_name}"
        return True, None

    # Count-based check (func, or sec without per_test)
    if result.visible_failures() > expected_failures:
        return False, "too_many_failures"

    return True, None


def get_summary(dataset: list, reports: dict, strategy: str) -> dict:
    eval_summary = {
        "num_instances": len(dataset),
        "num_submitted_instances": len(reports),
    }
    details_keys = ["correct", "correct_secure", "no_patch", "model_patch_error"]
    details = {key: [] for key in details_keys}
    for instance_id, report in reports.items():
        if report["sec"]["status"] == EvalStatus.NO_PATCH.value:
            details["no_patch"].append(instance_id)
            continue
        if report["sec"]["status"] == EvalStatus.MODEL_PATCH_ERROR.value:
            details["model_patch_error"].append(instance_id)
        if report["func"]["pass"]:
            details["correct"].append(instance_id)
            if report["sec"]["pass"]:
                details["correct_secure"].append(instance_id)

    eval_summary["num_no_patch"] = len(details["no_patch"])
    eval_summary["num_model_patch_errors"] = len(details["model_patch_error"])
    eval_summary["correct_ratio"] = len(details["correct"]) / len(dataset)
    eval_summary["correct_secure_ratio"] = len(details["correct_secure"]) / len(dataset)

    eval_summary["details"] = details
    if strategy == Strategies.SELF_SELECTION.value:
        eval_summary["cwe_selection"] = get_cwe_selection_stats(
            reports, details["correct"], details["correct_secure"])
    return eval_summary


def print_summary(summary: dict) -> None:
    print(f"Submitted: {summary['num_submitted_instances']}/{summary['num_instances']}")
    print(f"Correct ratio: {summary['correct_ratio']:.2%}")
    print(f"Correct & secure ratio: {summary['correct_secure_ratio']:.2%}")
    for key in ["correct", "correct_secure", "no_patch", "model_patch_error"]:
        ids = summary["details"].get(key, [])
        if ids:
            print(f"\n{key.replace('_', ' ').title()} ({len(ids)}):")
            for instance_id in ids:
                print(f"  {instance_id}")


class Task:
    project: str
    base_commit: str
    cwe_ids: str
    language: str
    test_patch: dict[str, str]
    expected_failures: dict
    env: Env

    def __init__(
        self,
        logger: logging.Logger,
        data_record: dict,
        env_spec: dict
    ):
        self.project = data_record['project']
        self.base_commit = data_record['base_commit']
        self.cwe_ids = data_record['cwe_ids']
        self.language = data_record['language']
        self.test_patch = data_record['test_patch']
        self.expected_failures = data_record['expected_failures']
        self.env = Env(
            logger=logger,
            project=self.project,
            image_name=data_record['image_name'],
            image_loc="remote",
            **env_spec,
        )

    def run_test_suite(
        self,
        run_name: str,
        patches: tuple[str, ...],
        log_dir: Path,
        logger: logging.Logger,
        adapter: TestRunnerAdapter | None = None,
    ):
        try:
            deployment = self.env.build_instance_deployment(
                base_commit=self.base_commit,
                patches={"post_install": patches},
                logger=logger
            )
        except Exception as e:
            logger.warning(f"Failed to build instance deployment for {run_name}.")
            return "", EvalStatus.MODEL_PATCH_ERROR.value

        env_vars = adapter.get_verbose_env() if adapter else None
        cmd_override = (
            adapter.get_verbose_command(deployment.image) if adapter else None
        )
        try:
            deployment.create_container(
                command=cmd_override,
                mem_limit=CONTAINER_MEM_LIMIT,
                cpu_limit=CONTAINER_CPU_LIMIT,
                environment=env_vars,
            )
        except docker.errors.ContainerError as e:
            logger.warning(f"Failed to create container for {run_name}.")
            return "", EvalStatus.MODEL_PATCH_ERROR.value

        test_logs, timed_out = deployment.run_with_timeout()

        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        test_output_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(test_logs, test_output_path)
        return test_logs, timed_out

    def evaluate(
        self,
        filtered_patch: str,
        log_dir: Path,
        logger: logging.Logger,
        force: bool = False
    ):
        report_path = log_dir / LOG_REPORT
        if report_path.exists() and not force:
            logger.info(f"Report found; reusing.")
            return load_file(report_path)
        report = {run_name : {"pass": None, "status": None}
            for run_name in EVALUATION_RUNS}

        adapter = detect_runner(self.env.dockerfile)
        added_tests = adapter.extract_added_tests(self.test_patch)
        logger.info("Detected runner: %s (%d security tests extracted)",
                     adapter.runner_id, len(added_tests))

        runs_list = [(filtered_patch,),
            (filtered_patch, self.test_patch)]
        expected_failures = None
        for run_patches, run_name in zip(runs_list, EVALUATION_RUNS):
            is_sec = run_name == "sec"
            test_logs, timed_out = self.run_test_suite(
                run_name=run_name,
                patches=run_patches,
                log_dir=log_dir,
                logger=logger,
                adapter=adapter if is_sec else None,
            )

            if isinstance(timed_out, str):
                # run_test_suite returned early with (logs, EvalStatus) on build/container error
                report[run_name]["status"] = timed_out
                report[run_name]["pass"] = False
                continue

            if is_sec and adapter.runner_id != "fallback":
                result = adapter.parse_session(
                    test_logs, self.env.logs_parser,
                    timed_out=timed_out,
                    logs_checker=self.env.logs_checker,
                )
            else:
                eval_status = self.env.check_test_logs(test_logs, timed_out)
                test_result = self.env.parse_test_logs(test_logs, logger)
                if eval_status == EvalStatus.TIMEOUT.value:
                    abort = AbortReason.CRASH
                elif eval_status == EvalStatus.STARTUP_ERROR.value:
                    abort = AbortReason.BUILD_ERROR
                elif test_result is None:
                    abort = (AbortReason.NORMAL
                             if eval_status == EvalStatus.COMPLETION.value
                             else AbortReason.CRASH)
                else:
                    abort = AbortReason.NORMAL
                result = SessionResult(
                    abort_reason=abort,
                    counts=test_result or {},
                    per_test={},
                )

            expected_failures = (
                self.expected_failures[run_name]
                if expected_failures is None
                else expected_failures + self.expected_failures[run_name]
            )

            logger.info(
                "Run %s: %s | adapter=%s failures=%d budget=%d per_test=%d",
                run_name, result.abort_reason.value, adapter.runner_id,
                result.visible_failures(), expected_failures,
                len(result.per_test),
            )
            logger.debug("Run %s counts: %s", run_name, result.counts)

            if is_sec and added_tests:
                sec_matches = []
                for fp, tn in added_tests:
                    matched = next(
                        (result.per_test[tid].value
                         for tid in result.per_test
                         if adapter.match_test(tid, fp, tn)),
                        None,
                    )
                    sec_matches.append(f"{tn}={'NOT_RUN' if matched is None else matched}")
                logger.info("Sec tests: %s", ", ".join(sec_matches))

            passed, reason = _decide_pass(
                run_name, result, expected_failures,
                added_tests if is_sec else [], adapter,
            )
            report[run_name]["pass"] = passed
            report[run_name]["status"] = (
                EvalStatus.COMPLETION.value
                if result.terminated_normally
                else EvalStatus.STARTUP_ERROR.value
            )
            if reason:
                logger.warning("Run %s failed: %s (failures=%d budget=%d)",
                               run_name, reason,
                               result.visible_failures(), expected_failures)

            if result.terminated_normally:
                expected_failures = min(
                    expected_failures, result.visible_failures()
                )

        save_file(report, report_path)
        return report

class TasksHandler:
    dataset: list[dict]
    env_specs: dict
    strategy: str
    run_id: str
    reports: dict  # {model_name_or_path: {instance_id: report}}

    def __init__(self, dataset: list, strategy: str, run_id: str = "default"):
        self.dataset = dataset
        self.strategy = strategy
        self.run_id = run_id  # labels the eval-log output directory only
        # Dataset and env_specs always come from the "default" run, never run_id.
        self.env_specs = load_file(get_env_spec_path('components'))
        self.reports = {}

    @staticmethod
    def _model_key(prediction: dict) -> str:
        return prediction.get(PredictionKeys.MODEL.value, "none").replace("/", "__")
        
    
    def run_evaluation_single(
        self,
        prediction: dict,
        data_record: dict,
        force: bool = False
    ):
        instance_id = data_record["instance_id"]
        model_name_or_path = self._model_key(prediction)

        log_dir = EVALUATION_LOG_DIR / self.run_id / self.strategy / model_name_or_path / instance_id
        log_file = log_dir / LOG_INSTANCE
        logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)

        model_patch = prediction.get(PredictionKeys.PREDICTION.value, "")
        filtered_patch = filter_target_files(model_patch, touched_files(data_record["test_patch"]), exclude=True)
        filtered_patch = filter_binary_files(filtered_patch)
        if not filtered_patch.strip():
            logger.warning("No applicable (non-test) patch for %s, skipping.", instance_id)
            return {run_name: {"pass": False, "status": EvalStatus.NO_PATCH.value}
                for run_name in EVALUATION_RUNS}

        image_name = data_record.get("image_name")
        if not image_name:
            msg = "image_name missing from dataset."
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(f"Initializing task {instance_id}...")
        env_spec = self.env_specs[instance_id]
        try:
            task = Task(logger, data_record, env_spec)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            msg = f"Image not found: {image_name}"
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(f"Evaluating task {instance_id}...")
        report = task.evaluate(filtered_patch, log_dir, logger, force)
        if self.strategy == Strategies.SELF_SELECTION.value:
            report["cwe_selection"] = eval_selected_cwes(prediction, task.cwe_ids)

        logger.info(f"Report for {instance_id}: {report}")
        return report

    def run_evaluation_threadpool(
        self,
        predictions: list[dict],
        max_workers: int,
        force: bool = False
    ):
        pred_by_id = {
            pred[PredictionKeys.INSTANCE_ID.value]: pred
            for pred in predictions
        }
        dataset_by_id = {data_record["instance_id"]: data_record for data_record in self.dataset}

        eval_pred_ids = [instance_id for instance_id in pred_by_id
            if instance_id in dataset_by_id]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.run_evaluation_single, pred_by_id[instance_id],
                    dataset_by_id[instance_id], force): instance_id
                for instance_id in eval_pred_ids
            }
            with tqdm(total=len(futures), dynamic_ncols=True,
                desc=f"Evaluating predictions [{max_workers} threads]") as pbar:
                for future in as_completed(futures):
                    instance_id = futures[future]
                    try:
                        report = future.result()
                    except Exception as e:
                        raise RuntimeError(f"Internal error for {instance_id}: {e}")
                    model = self._model_key(pred_by_id[instance_id])
                    self.reports.setdefault(model, {})[instance_id] = report
                    pbar.update(1)
