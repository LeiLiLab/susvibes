import shutil
import subprocess
import getpass
from typing import ClassVar
from pathlib import Path

from susvibes.utils import load_file, save_file
from susvibes.curate.utils import run

AGENT_SETTINGS_PATH = Path(__file__).parent / "settings.yaml"

class SWEAgentPort:
    name: ClassVar[str] = "SWE-agent"
    dir: ClassVar[Path]
    run_name: ClassVar[str]
    task_instances: ClassVar[list]
    agent_env: ClassVar[str]
    config_name: ClassVar[str]
    model: ClassVar[dict]
    num_workers: ClassVar[int]
    
    @classmethod
    def init(
        cls, 
        run_name: str = None,
        agent_env: str = None,
        config_name: str = None,
        model: dict = None,
        num_workers: int = None
    ):
        settings = load_file(AGENT_SETTINGS_PATH)[cls.name]
        
        cls.dir = Path(settings["dir"])
        cls.run_name = run_name or settings["run_name"]
        cls.agent_env = agent_env or settings["agent_env"]
        cls.config_name = config_name or settings["config_name"]
        cls.model = model or settings["model"]
        cls.num_workers = num_workers or settings["num_workers"]
        cls.task_instances = []
        cls.get_instances_path().parent.mkdir(parents=True, exist_ok=True)
        
    @classmethod
    def get_instances_path(cls):
        return Path("../logs/agent_runs/{}_instances.yaml".format(cls.run_name))
    
    @classmethod
    def add_task(
        cls,
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
        cls.task_instances.append(task_instance)
        
    @classmethod
    def before_start(cls):
        save_file(cls.task_instances, cls.get_instances_path())
        print(f"{cls.name} tasks saved to {cls.get_instances_path()}")

    @classmethod
    def after_completion(cls, agent_output_dir: Path, submitted_only: bool = False):
        predictions_path = agent_output_dir / "preds.json"
        predictions = load_file(predictions_path)
        if submitted_only:
            exit_statuses_path = agent_output_dir / "run_batch_exit_statuses.yaml"
            exit_statuses = load_file(exit_statuses_path)["instances_by_exit_status"]
            submitted_ids = exit_statuses.get("skipped (submitted)", []) + \
                exit_statuses.get("submitted", [])
        return [pred for pred in predictions.values() if pred['model_patch'] and 
                (not submitted_only or pred['instance_id'] in submitted_ids)]
    
    @classmethod
    def get_output_dir(cls):
        folder_name_template = "{}__{}__t-0.00__p-1.00__c-{:.2f}___{}_instances"
        return (Path(cls.dir) / "trajectories" / getpass.getuser() /    
            folder_name_template.format(
                cls.config_name, cls.model["name"], cls.model["per_instance_cost_limit"], cls.run_name
            )).resolve()
        
    @classmethod
    def remove_results(cls, instance_ids: list):
        num_removed = 0
        for instance_id in instance_ids:
            result_dir = cls.get_output_dir() / instance_id
            if result_dir.exists():
                shutil.rmtree(result_dir)
                num_removed += 1
        print(f"Removed results for {num_removed} instances in run {cls.run_name}.")    
    
    @classmethod
    def run_batch(cls):
        print(f"Running {cls.run_name} on {cls.name} with {len(cls.task_instances)} tasks...")
        cmd = (
            f"conda run -n {cls.agent_env} --live-stream "
            "sweagent run-batch "
            f"--config=config/{cls.config_name}.yaml "
            f"--agent.model.name={cls.model['name']} "
            f"--agent.model.per_instance_cost_limit={cls.model['per_instance_cost_limit']} "
            f"--agent.model.per_instance_call_limit={cls.model['per_instance_call_limit']} "
            "--instances.type=expert_file "
            f"--instances.path={cls.get_instances_path().resolve()} "
            f"--num_workers={cls.num_workers}"
        )
        run(
            cmd=cmd, 
            cwd=cls.dir,
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=False
        )
        return cls.get_output_dir()
    

class EnvAgentPort(SWEAgentPort):
    name: str = "Env-agent"

    @classmethod
    def add_task(cls, **kwargs):
        super().add_task(**kwargs)
        # mount host docker socket for docker-in-docker support
        cls.task_instances[-1]['env']['deployment']['docker_args'] = ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
