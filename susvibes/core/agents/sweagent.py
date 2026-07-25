import os
import signal
import subprocess
from pathlib import Path

from susvibes.core.constants import ContainerLimits
from susvibes.core.utils import load_file, save_file


class SWEAgentPort:
    """Drives a `sweagent run-batch` over a batch of tasks.

    Shared by curation and evaluation. The run configuration (SWE-agent config,
    model, workers, conda env, ...) is supplied by the caller — typically loaded
    from that caller's own settings via `from_settings` — not from any fixed file.
    Env-agent runs are the same port with `mount_docker_socket=True`.
    """

    def __init__(
        self,
        run_name: str,
        sweagent_dir: str | Path,
        conda_env: str,
        config_name: str | list[str],
        model: dict,
        num_workers: int,
        output_dir: str | Path,
        mount_docker_socket: bool = False,
    ) -> None:
        self.run_name = run_name
        self.sweagent_dir = Path(sweagent_dir)
        self.conda_env = conda_env
        self.config_name = config_name
        self.model = model
        self.num_workers = num_workers
        self.output_dir = Path(output_dir).resolve()
        self.mount_docker_socket = mount_docker_socket
        self.task_instances: list[dict] = []
        self.get_instances_path().parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, setting: dict, run_name: str = None, **overrides) -> "SWEAgentPort":
        config = {**setting, **{k: v for k, v in overrides.items() if v is not None}}
        if run_name is not None:
            config["run_name"] = run_name
        return cls(**config)

    def get_instances_path(self) -> Path:
        return self.output_dir / "instances.yaml"

    def add_task(
        self,
        repo_type: str,
        problem_statement: str,
        instance_id: str,
        repo_dir: Path = None,
        repo_name: str = None,
        image: str = None,
        base_commit: str = None,
        extra_fields: dict = None,
    ) -> None:
        assert repo_type in ["local", "preexisting"]
        repo_config = {"type": repo_type, "base_commit": base_commit or "HEAD"}
        if repo_type == "local":
            repo_config["path"] = str(repo_dir.resolve())
        elif repo_type == "preexisting":
            repo_config["repo_name"] = repo_name
        docker_args = [
            f"--memory={ContainerLimits.MEM_LIMIT}",
            f"--cpus={ContainerLimits.CPU_LIMIT}",
        ]
        if self.mount_docker_socket:
            docker_args = ["-v", "/var/run/docker.sock:/var/run/docker.sock"] + docker_args
        task_instance = {
            "env": {
                "deployment": {
                    "type": "docker",
                    "image": image or "python:3.11",
                    "python_standalone_dir": "/root",
                    "docker_args": docker_args,
                },
                "repo": repo_config,
            },
            "problem_statement": {
                "type": "text",
                "text": problem_statement,
                "id": instance_id,
            },
        }
        for key, value in (extra_fields or {}).items():
            task_instance[key].update(value)
        self.task_instances.append(task_instance)

    def before_start(self) -> None:
        save_file(self.task_instances, self.get_instances_path())
        print(f"SWE-agent tasks saved to {self.get_instances_path()}.")

    @staticmethod
    def after_completion(output_dir: Path, submitted_only: bool = False) -> tuple[list, float | None]:
        predictions = load_file(output_dir / "preds.json")
        exit_statuses = load_file(output_dir / "run_batch_exit_statuses.yaml")
        total_cost = exit_statuses.get("total_cost", None)
        if submitted_only:
            instances_by_status = exit_statuses["instances_by_exit_status"]
            submitted_ids = instances_by_status.get("skipped (submitted)", []) + \
                instances_by_status.get("submitted", [])
            predictions = [pred for pred in predictions.values() if
                           pred["instance_id"] in submitted_ids]
        else:
            predictions = list(predictions.values())
        return predictions, total_cost

    def _config_args(self) -> str:
        names = [self.config_name] if isinstance(self.config_name, str) else self.config_name
        return " ".join(f"--config=config/{name}.yaml" for name in names)

    def run_batch(self) -> Path:
        print(f"Running {self.run_name} with {len(self.task_instances)} tasks...")
        cmd = (
            f"conda run -n {self.conda_env} --live-stream "
            "sweagent run-batch "
            f"{self._config_args()} "
            f"--agent.model.name={self.model['name']} "
            f"--agent.model.per_instance_cost_limit={self.model['per_instance_cost_limit']} "
            f"--agent.model.per_instance_call_limit={self.model['per_instance_call_limit']} "
            "--instances.type=expert_file "
            f"--instances.path={self.get_instances_path().resolve()} "
            f"--num_workers={self.num_workers} "
            f"--output_dir={self.output_dir}"
        )
        proc = subprocess.Popen(
            cmd,
            cwd=self.sweagent_dir,
            shell=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait()
        except KeyboardInterrupt:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait()
            raise
        if proc.returncode != 0:
            raise subprocess.SubprocessError(
                f"Command failed with return code {proc.returncode}."
            )
        return self.output_dir
