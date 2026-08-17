import logging
import docker.errors
from tqdm import tqdm
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from susvibes.core.constants import *
from susvibes.core.env import Env
from susvibes.core.logs import PassFailure
from susvibes.core.report import reuse_report, save_report, get_report_summary, print_summary
from susvibes.eval.strategies.tools import eval_selected_cwes, get_cwe_selection_stats
from susvibes.core.utils import (
    PatchError,
    is_patch_error,
    load_file,
    save_file,
    touched_files,
    filter_target_files,
    filter_binary_files,
    setup_instance_logger,
    get_env_specs,
    Route,
)

LOG_INSTANCE = "run_instance.log"
LOG_TEST_OUTPUT = "test_outputs/{}.txt"
LOG_REPORT = "report.json"
EVAL_RUNS = ["func", "sec"]


class EvalStatus(StrEnum):
    """How an evaluation of one submission concluded. Every member is a NORMAL outcome — the
    harness breaking is not a status but the report's `error`, which is what `--resume` re-runs."""
    TESTED = "tested"                       # both suites ran — see the report's `run`
    EMPTY_PATCH = "empty_patch"             # the submission carried no patch
    PATCH_ERROR = "patch_error"             # the patch does not apply to the task tree

def get_eval_summary(dataset: list, reports: dict, strategy: str, instance_ids: list = None) -> dict:
    """The shared per-item split (concluded by `eval_status` vs errored), plus what only eval can
    say: the pass ratios, taken over the whole candidate dataset so an unsubmitted or errored
    instance counts against the score rather than vanishing from the denominator."""
    if instance_ids is not None:
        dataset = [data_record for data_record in dataset
                   if data_record["instance_id"] in set(instance_ids)]
    tested = [instance_id for instance_id, report in reports.items()
              if report.get("eval_status") == EvalStatus.TESTED]
    func_pass = [instance_id for instance_id in tested if reports[instance_id]["run"]["func"]["pass"]]
    sec_pass = [instance_id for instance_id in func_pass if reports[instance_id]["run"]["sec"]["pass"]]

    eval_summary = {
        "num_candidates": len(dataset),
        "num_submitted": len(reports),
        **get_report_summary(reports, "eval_status"),
        "func_pass": len(func_pass) / len(dataset) if dataset else 0.0,
        "sec_pass": len(sec_pass) / len(dataset) if dataset else 0.0,
        "details": {"func_pass": sorted(func_pass), "sec_pass": sorted(sec_pass)},
    }
    if strategy == Strategies.SELF_SELECTION:
        eval_summary["cwe_selection"] = get_cwe_selection_stats(reports, func_pass, sec_pass)
    return eval_summary


def print_eval_summary(summary: dict) -> None:
    print(f"Submitted: {summary['num_submitted']}/{summary['num_candidates']}")
    print(f"Func pass ratio: {summary['func_pass']:.2%}")
    print(f"Sec pass ratio: {summary['sec_pass']:.2%}")
    print_summary(summary)


class Task:
    project: str
    base_commit: str
    cwe_ids: str
    language: str
    test_patch: dict[str, str]
    expected_pf: dict
    flags: dict
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
        self.expected_pf = data_record['expected_pf']
        self.flags = data_record['flags']
        self.env = Env(
            logger=logger,
            project=self.project,
            image_name=data_record['image_name'],
            image_loc=ImageLoc.REMOTE,
            **env_spec,
        )

    def _run_test_suite(
        self,
        run_name: str,
        patches: list[tuple[str, dict]],
        command: str | list,
        log_dir: Path,
        logger: logging.Logger
    ) -> tuple[str, bool]:
        """Run one configuration; cache and return (test_logs, timed_out).
        Raises RuntimeError on a model-patch build/run failure; classification is done by the caller."""
        try:
            deployment = self.env.build_instance_deployment(
                base_commit=self.base_commit,
                patches=patches,
                logger=logger
            )
        except docker.errors.BuildError as e:
            msg = f"Failed to build instance deployment: {e}"
            logger.error(msg)
            # Decide here, where the build log is: `git apply` refusing the patch is a verdict on
            # the submission, anything else is the harness breaking.
            if is_patch_error(str(e)):
                raise PatchError(msg)
            raise RuntimeError(f"{msg}\n{e.build_log}")
        try:
            deployment.create_container(command=command, mem_limit=ContainerLimits.MEM_LIMIT, cpu_limit=ContainerLimits.CPU_LIMIT)
        except docker.errors.APIError as e:
            msg = f"Failed to create container: {e}"
            logger.error(msg)
            raise RuntimeError(msg)
        try:
            test_logs, timed_out = deployment.run_with_timeout()
        except docker.errors.APIError as e:
            msg = f"Failed to start container: {e}"
            logger.error(msg)
            raise RuntimeError(msg)

        test_output_path = log_dir / LOG_TEST_OUTPUT.format(run_name)
        test_output_path.parent.mkdir(parents=True, exist_ok=True)
        save_file(test_logs, test_output_path)
        return test_logs, timed_out

    def evaluate(
        self,
        filtered_patch: str,
        log_dir: Path,
        logger: logging.Logger,
        force: bool = False,
        resume: bool = False,
    ):
        report_path = log_dir / LOG_REPORT
        report = reuse_report(report_path, force=force, resume=resume)
        if report is not None:
            logger.info("Report found; reusing.")
            return report

        if not filtered_patch.strip():
            report = {"eval_status": EvalStatus.EMPTY_PATCH, "run": {}, "error": None}
            save_report(report, report_path)
            return report

        runs_list = [[(filtered_patch, {})],
            [(self.test_patch, {}), (filtered_patch, {})]]
        run, status, error_msg = {}, EvalStatus.TESTED, None
        expected_raw = None
        for run_patches, run_name in zip(runs_list, EVAL_RUNS):
            try:
                test_logs, timed_out = self._run_test_suite(
                    run_name=run_name,
                    patches=run_patches,
                    command=Route.route_test_cmd(self.flags, run_name),
                    log_dir=log_dir,
                    logger=logger
                )
            except PatchError:
                # Every run applies this same patch, so the rest would fail identically.
                status, run = EvalStatus.PATCH_ERROR, {}
                break
            except RuntimeError as e:
                error_msg = str(e)
                run[run_name] = {}
                continue
            test_pf = self.env.handle_test_logs(test_logs, timed_out, logger,
                kind=Route.route_logs_kind(self.flags, run_name))
            if not test_pf.completed():
                run[run_name] = {"pass": False, "test_status": test_pf.status}
                continue
            expected_raw = self.expected_pf[run_name] if expected_raw is None \
                else PassFailure.add_raw(expected_raw, self.expected_pf[run_name])
            expected_pf = PassFailure.from_raw(expected_raw)
            run[run_name] = {"pass": not test_pf.breaks_more_than(expected_pf),
                "test_status": test_pf.status}
            expected_pf = expected_pf.capped_by(test_pf)
            expected_raw = expected_pf.get_raw()

        report = {"eval_status": None if error_msg else status, "run": run, "error": error_msg}
        save_report(report, report_path)
        return report

class TasksHandler:
    dataset: list[dict]
    env_specs: dict
    strategy: str
    run_id: str
    reports: dict  # {model_name_or_path: {instance_id: report}}

    def __init__(self, strategy: str, run_id: str = "default", dataset_id: str = "default"):
        self.strategy = strategy
        self.run_id = run_id  # labels the eval-log output directory only
        # Dataset and env_specs both come from dataset_id, never run_id.
        self.dataset = load_file(get_dataset_path('susvibes_dataset', dataset_id))
        self.env_specs = get_env_specs(dataset_id)
        self.reports = {}

    @staticmethod
    def _model_key(prediction: dict) -> str:
        return prediction.get(PredictionKeys.MODEL, "none").replace("/", "__")
        
    
    def run_evaluation_single(
        self,
        prediction: dict,
        data_record: dict,
        force: bool = False,
        resume: bool = False,
    ):
        instance_id = data_record["instance_id"]
        model_name_or_path = self._model_key(prediction)

        log_dir = EVAL_LOG_DIR / self.run_id / self.strategy / model_name_or_path / instance_id
        log_file = log_dir / LOG_INSTANCE
        logger = setup_instance_logger(log_file, __spec__.name, instance_id, handle_tqdm=True)

        model_patch = prediction.get(PredictionKeys.PREDICTION, "")
        filtered_patch = filter_target_files(model_patch, touched_files(data_record["test_patch"]), exclude=True)
        filtered_patch = filter_binary_files(filtered_patch)

        image_name = data_record.get("image_name")
        if not image_name:
            msg = "image_name missing from dataset."
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(f"Initializing {instance_id}...")
        env_spec = self.env_specs[instance_id]
        try:
            task = Task(logger, data_record, env_spec)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            msg = f"Eval image not found: {image_name}"
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info(f"Evaluating {instance_id}...")
        report = task.evaluate(filtered_patch, log_dir, logger, force, resume)
        if self.strategy == Strategies.SELF_SELECTION:
            report["cwe_selection"] = eval_selected_cwes(prediction, task.cwe_ids)

        logger.info(f"Report for {instance_id}: {report}")
        return report

    def run_evaluation_threadpool(
        self,
        predictions: list[dict],
        max_workers: int,
        force: bool = False,
        resume: bool = False,
        instance_ids: list = None
    ):
        pred_by_id = {
            pred[PredictionKeys.INSTANCE_ID]: pred
            for pred in predictions
        }
        dataset_by_id = {data_record["instance_id"]: data_record for data_record in self.dataset}

        eval_pred_ids = [instance_id for instance_id in pred_by_id
            if instance_id in dataset_by_id]
        if instance_ids is not None:
            eval_pred_ids = [instance_id for instance_id in eval_pred_ids
                if instance_id in set(instance_ids)]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.run_evaluation_single, pred_by_id[instance_id],
                    dataset_by_id[instance_id], force, resume): instance_id
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
