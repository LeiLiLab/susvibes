import os
import signal
import shutil
import subprocess
import getpass
from pathlib import Path

from susvibes.constants import CONTAINER_MEM_LIMIT, CONTAINER_CPU_LIMIT
from susvibes.utils import load_file, save_file

from susvibes.curate.constants import AGENT_RUN_LOG_DIR

AGENT_SETTINGS_PATH = Path(__file__).parent / "settings.yaml"

class SWEAgentPort:
    name = "SWE-agent"

    def __init__(
        self,
        run_name: str = None,
        agent_env: str = None,
        config_name: str = None,
        model: dict = None,
        num_workers: int = None
    ):
        settings = load_file(AGENT_SETTINGS_PATH)[self.name]

        self.dir = Path(settings["dir"])
        self.run_name = run_name or settings["run_name"]
        self.agent_env = agent_env or settings["agent_env"]
        self.config_name = config_name or settings["config_name"]
        self.model = model or settings["model"]
        self.num_workers = num_workers or settings["num_workers"]
        self.task_instances = []
        self.get_instances_path().parent.mkdir(parents=True, exist_ok=True)

    def get_instances_path(self):
        return AGENT_RUN_LOG_DIR / "{}_instances.yaml".format(self.run_name)

    def add_task(
        self,
        repo_type: str,
        problem_statement: str,
        instance_id: str,
        repo_dir: Path = None,
        repo_name: str = None,
        image: str = None,
        base_commit: str = None
    ) -> None:
        assert repo_type in ["local", "preexisting"]
        repo_config = {'type': repo_type, 'base_commit': base_commit or "HEAD",}
        if repo_type == "local":
            repo_config['path'] = str(repo_dir.resolve())
        elif repo_type == "preexisting":
            repo_config['repo_name'] = repo_name
        task_instance = {
            'env': {
                'deployment': {
                    'type': "docker",
                    'image': image or "python:3.11",
                    'python_standalone_dir': "/root"
                },
                'repo': repo_config
            },
            'problem_statement': {
                'type': "text",
                'text': problem_statement,
                'id': instance_id,
            },
        }
        self.task_instances.append(task_instance)

    def before_start(self):
        save_file(self.task_instances, self.get_instances_path())
        print(f"{self.name} tasks saved to {self.get_instances_path()}")

    @staticmethod
    def after_completion(agent_output_dir: Path, submitted_only: bool = False):
        predictions_path = agent_output_dir / "preds.json"
        predictions = load_file(predictions_path)
        exit_statuses_path = agent_output_dir / "run_batch_exit_statuses.yaml"
        exit_statuses = load_file(exit_statuses_path)
        total_cost = exit_statuses.get("total_cost", None)
        if submitted_only:
            instances_by_status = exit_statuses["instances_by_exit_status"]
            submitted_ids = instances_by_status.get("skipped (submitted)", []) + \
                instances_by_status.get("submitted", [])
            predictions = [pred for pred in predictions.values() if
                           pred['instance_id'] in submitted_ids]
        else:
            predictions = list(predictions.values())
        return predictions, total_cost

    def get_output_dir(self):
        folder_name_template = "{}__{}__t-0.00__p-1.00__c-{:.2f}___{}_instances"
        return (Path(self.dir) / "trajectories" / getpass.getuser() /
            folder_name_template.format(
                self.config_name, self.model["name"], self.model["per_instance_cost_limit"], self.run_name
            )).resolve()

    def remove_results(self, instance_ids: list):
        num_removed = 0
        for instance_id in instance_ids:
            result_dir = self.get_output_dir() / instance_id
            if result_dir.exists():
                shutil.rmtree(result_dir)
                num_removed += 1
        print(f"Removed results for {num_removed} instances in run {self.run_name}.")

    def run_batch(self):
        print(f"Running {self.run_name} on {self.name} with {len(self.task_instances)} tasks...")
        cmd = (
            f"conda run -n {self.agent_env} --live-stream "
            "sweagent run-batch "
            f"--config=config/{self.config_name}.yaml "
            f"--agent.model.name={self.model['name']} "
            f"--agent.model.per_instance_cost_limit={self.model['per_instance_cost_limit']} "
            f"--agent.model.per_instance_call_limit={self.model['per_instance_call_limit']} "
            "--instances.type=expert_file "
            f"--instances.path={self.get_instances_path().resolve()} "
            f"--num_workers={self.num_workers}"
        )
        proc = subprocess.Popen(
            cmd,
            cwd=self.dir,
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
        return self.get_output_dir()


class EnvAgentPort(SWEAgentPort):
    name = "Env-agent"

    def add_task(self, **kwargs):
        super().add_task(**kwargs)
        # mount host docker socket for docker-in-docker support
        self.task_instances[-1]['env']['deployment']['docker_args'] = [
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            f"--memory={CONTAINER_MEM_LIMIT}",
            f"--cpus={CONTAINER_CPU_LIMIT}",
        ]
