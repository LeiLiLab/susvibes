import os
import uuid
import re
import signal
import threading
import logging
import tempfile
from pathlib import Path

import docker
import docker.errors
from docker.models.containers import Container
from docker.models.images import Image

from env_specs import *
from curate.utils import get_instance_id, save_file

docker_client = docker.from_env()

class Deployment():
    image: Image
    container: Container
    remove_image: bool
    remove_container: bool
    logger: logging.Logger

    def __init__(
        self, 
        image: Image, 
        logger: logging.Logger,
        remove_image: bool = False, 
        remove_container: bool = True
    ):
        self.image = image
        self.logger = logger
        self.container = None
        self.remove_image = remove_image
        self.remove_container = remove_container

    @staticmethod
    def get_default_image_name() -> str:
        return "agentsec_auto_{}".format(uuid.uuid4())

    @classmethod
    def from_build(cls, 
        logger: logging.Logger,
        context_path: Path,
        dockerfile: str,
        dockerignore: str = None,
        image_name: str = None,
        nocache: bool = False,
        remove_image: bool = False,
        remove_container: bool = True,
    ) -> "Deployment":
        save_file(dockerfile, context_path / "Dockerfile")
        if dockerignore:
            save_file(dockerignore, context_path / ".dockerignore")
        image_name = image_name or cls.get_default_image_name()
        try:
            response = docker_client.api.build(
                path=str(context_path),
                tag=image_name,
                nocache=nocache,
                rm=True,
                forcerm=True,
                decode=True,
            )
            buildlog = ""
            for chunk in response:
                if "stream" in chunk:
                    buildlog += chunk["stream"]
                    # print(chunk["stream"].rstrip())
                elif "errorDetail" in chunk:
                    raise docker.errors.BuildError(
                        chunk["errorDetail"]["message"], buildlog
                    )
            logger.info(f"Image {image_name} built successfully.")
            return cls(docker_client.images.get(image_name), logger, remove_image, remove_container)
        except docker.errors.BuildError as e:
            logger.error(f"docker.errors.BuildError when building {image_name}: {e}")
            logger.error(f"Build log: {e.build_log}")
            raise
        except docker.errors.APIError as e:
            logger.error(f"docker.errors.APIError when building {image_name}: {e}")
            raise docker.errors.BuildError(f"API error: {e}", "")

    @classmethod
    def from_pull(cls, 
        logger: logging.Logger,
        image_name: str,
        remove_image: bool = False,
        remove_container: bool = True,
        max_retries: int = 3,
    ) -> "Deployment":
        try:
            for retry in range(max_retries):
                try:
                    image = docker_client.images.pull(image_name)
                    break
                except docker.errors.NotFound as e:
                    if retry == max_retries - 1:
                        raise
            logger.info(f"Image {image_name} pulled successfully.")
            return cls(image, logger, remove_image, remove_container)
        except docker.errors.NotFound as e:
            logger.error(f"docker.errors.NotFound when pulling {image_name}.")
            raise

    @classmethod
    def from_local(cls,
        logger: logging.Logger,
        image_name: str = None, 
        image_id: str = None,
        remove_image: bool = False,
        remove_container: bool = True,
        max_retries: int = 3,
    ) -> "Deployment":
        if not image_id and not image_name:
            raise ValueError("Either docker image name or image id must be provided.")
        try:
            for retry in range(max_retries):
                try:
                    image = docker_client.images.get(image_name or image_id)
                    break
                except docker.errors.ImageNotFound as e:
                    if retry == max_retries - 1:
                        raise
            logger.info(f"Image {image_name or image_id} found locally.")
            if not image.tags:
                default_image_name = cls.get_default_image_name()
                logger.warning(f"Warning: image has no names, tagging a default name {default_image_name}.")
                assert image.tag(default_image_name)
            return cls(image, logger, remove_image, remove_container)
        except docker.errors.ImageNotFound as e:
            logger.error(f"docker.errors.ImageNotFound when getting {image_name or image_id}.")
            raise      
    
    def create_container(
        self,
        command: str | list = None,
        mem_limit: str = None,
    ) -> None:
        try:
            container = docker_client.containers.create(
                image=self.image.id,
                detach=True,
                mem_limit=mem_limit,
                command=command
                # command="tail -f /dev/null",
            )
            self.logger.info(f"Container for {self.image.id} created: {container.name}")
            self.container = container
        except docker.errors.ContainerError as e:
            self.logger.error(f"Error creating container for {self.image.id}: {e}")
            raise
    
    def start(self) -> None | str:
        """Start the container and if wait is True return running logs."""
        self.container.start()
        self.logger.info(f"Container {self.container.name} started.")

    def _remove_container(self) -> None:
        """Remove the container if it exists."""
        try:
            if self.container:
                self.container.remove(force=True)
                self.logger.info(f"Container {self.container.name} removed.")
        except docker.errors.NotFound as e:
            self.logger.info(f"Container {self.container.name} not found.")
        except Exception as e:
            self.logger.error(f"Failed to remove container {self.container.name}: {e}", exc_info=True)

    def _remove_image(self) -> None:
        self._remove_container()
        try:
            docker_client.images.remove(self.image.id, force=True)
            self.logger.info(f"Image {self.image.id} removed.")
        except docker.errors.ImageNotFound as e:
            self.logger.info(f"Image {self.image.id} not found.")
        except Exception as e:
            self.logger.error(f"Failed to remove image {self.image.id}: {e}", exc_info=True)

    def stop(self) -> None:
        """Stop the container and deal with the removal logic."""
        try:
            if self.container:
                self.container.stop(timeout=15)
                self.logger.info(f"Container {self.container.name} stopped.")
        except Exception as e:
            if "already stopped" in str(e).lower():
                self.logger.info(f"Container {self.container.name} has already stopped.")
            else:
                self.logger.warning(f"Failed to stop container {self.container.name}: {e}. Trying to forcefully kill...")
                try:
                    container_info = docker_client.api.inspect_container(self.container.id)
                    pid = container_info["State"].get("Pid", 0)
                    if pid > 0:
                        os.kill(pid, signal.SIGKILL)
                        self.logger.info(f"Forcefully killed container {self.container.name} with PID {pid}.")
                    else:
                        self.logger.error(f"PID for container {self.container.name}: {pid} - not killing.")
                except Exception as e:
                    self.logger.error(f"Failed to forcefully kill container {self.container.name}: {e}", exc_info=True)
        if self.remove_image:
            self._remove_image()
        elif self.remove_container:
            self._remove_container()

    def run_with_timeout(self, timeout: int = 1800):
        self.start()
        run_logs, timed_out = b"", False
        def run():
            nonlocal run_logs
            for chunk in self.container.logs(stream=True, follow=True, stdout=True, stderr=True):
                run_logs += chunk
            try:
                self.container.wait()
            except docker.errors.NotFound:
                return
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.logger.info(f"Container {self.container.name} run timed out after {timeout} seconds.")
            self.stop()
            timed_out = True
        else:
            self.stop()
        return run_logs.decode(), timed_out
    
class Env:
    project: str
    repo_dir: Path
    deployment: Deployment
    dockerfile: str
    dockerignore: str
    logs_parser: dict[str, str]

    def __init__(
        self, 
        logger: logging.Logger,
        project: str, 
        repo_dir: Path, 
        image_name: str,
        dockerfile: str,
        dockerignore: str,
        image_loc: str = "local",
        logs_parser: dict = None, 
        remove_image: bool = False, 
        remove_container: bool = True
    ):
        self.project = project
        self.repo_dir = repo_dir
        self.dockerfile = dockerfile
        self.dockerignore = dockerignore
        self.logs_parser = logs_parser
        logger.info(f"Collecting enviroment deployment...")
        collect_method = Deployment.from_local if image_loc == "local" else \
            Deployment.from_pull if image_loc == "remote" else None
        try:
            self.deployment = collect_method(
                logger=logger,
                image_name=image_name, 
                remove_image=remove_image, 
                remove_container=remove_container
            )
        except (docker.errors.ImageNotFound, docker.errors.NotFound) as e:
            logger.critical("Environment image not found.")
            raise
    
    @staticmethod 
    def _apply_patches(patches: tuple[str, ...], group) -> None:
        """Get commands for applying patches to the repository."""
        cmds = []
        build_data_dir = Path(f"/{BUILD_DATA_DIR_NAME}")
        patches_dir = build_data_dir / PATCHES_DIR_NAME / group
        reverse = any(flag in patches[group] for flag in REVERSE_PATCH_FLAG)
        for id, patch in enumerate(patches[group]):
            if patch not in REVERSE_PATCH_FLAG:
                patch_path = patches_dir / f"{id}.patch"
                cmd = "git apply --ignore-space-change" + (" --reverse" if reverse else "")
                cmds.append(f"{cmd} {str(patch_path)}")
        return " && ".join(cmds)    

    def _compose_instance_dockerfile(
        self, 
        base_commit: str,
        patches: dict[tuple[str, ...]],
        reinstall: bool = True
    ) -> str:
        """Create the Dockerfile for building instance deployment."""
        dockerfile_re = re.compile(DOCKERFILE_PATTERN, re.MULTILINE | re.DOTALL)
        m = dockerfile_re.search(self.dockerfile)
        from_stm, _, _, dependency_install_stm, cmd_stm = m.groups()
        
        replace_pattern = r'^(FROM(?:\s+--\S+)*\s+)(\S+)(.*)$'
        cached_base_image = self.deployment.image.tags[0]
        cached_from_stm = re.sub(
            replace_pattern,
            lambda m: f"{m.group(1)}{cached_base_image}{m.group(3)}",
            from_stm, count=1, flags=re.MULTILINE
        )
        run_stm = "RUN {}\n"
        reset_cmds = f'git reset --hard {base_commit} && git clean -fdq'  
        instance_dockerfile = "".join([
            cached_from_stm,
            run_stm.format(" && ".join(GIT_AUTHOR_CONFIGS)),
            run_stm.format(f"mkdir -p {BUILD_DATA_DIR_NAME}"),
            f'COPY . /{BUILD_DATA_DIR_NAME}/\n'
        ])
        
        instance_dockerfile += run_stm.format(reset_cmds)
        if patches.get("pre_install", None):
            instance_dockerfile += run_stm.format(type(self)._apply_patches(
                patches, "pre_install"))
        if reinstall:
            instance_dockerfile += dependency_install_stm
        if patches.get("post_install", None):
            instance_dockerfile += run_stm.format(type(self)._apply_patches(
                patches, "post_install"))
                
        commit_msg = "Instance created."
        commit_cmds = f'git add . && git commit --allow-empty -m "{commit_msg}" --no-verify'
        rm_cmd = f'rm -rf -- /{BUILD_DATA_DIR_NAME}'
        instance_dockerfile += run_stm.format(commit_cmds) + \
            run_stm.format(rm_cmd) + cmd_stm
        return instance_dockerfile
    
    def build_instance_deployment(
        self, 
        base_commit: str,
        patches: dict[tuple[str, ...]],
        logger: logging.Logger,
        remove_image: bool = True,
        remove_container: bool = True,
    ) -> Deployment:
        """Build a instance-level Docker image from the environment."""
        logger.info(f"Building instance deployment...")
        banned_reinstall = BANNED_REINSTALL_FOR_INSTANCE.get(self.project, [])
        reinstall = True
        if any(base_commit.startswith(commit) for commit in banned_reinstall):
            logger.info(f"Reinstalling {self.project} at commit {base_commit} is banned.")
            reinstall = False
            
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = Path(tmpdir)
            for k, v in patches.items():
                patches_dir = context_path / PATCHES_DIR_NAME / k
                patches_dir.mkdir(parents=True, exist_ok=True)
                for id, patch in enumerate(v):
                    if patch not in REVERSE_PATCH_FLAG:
                        save_file(patch, patches_dir / f"{id}.patch")
            instance_dockerfile = self._compose_instance_dockerfile(base_commit, patches, reinstall)

            deployment = Deployment.from_build(
                logger=logger,
                context_path=context_path,
                dockerfile=instance_dockerfile,
                dockerignore=self.dockerignore,
                image_name=f"instance_{get_instance_id(self.project, base_commit).lower()}",
                remove_image=remove_image,
                remove_container=remove_container,
            )    
        return deployment
    
    @staticmethod
    def get_test_status(run_logs: str, timed_out: bool = False) -> str:
        """Get the test status from the run logs."""
        if timed_out:
            return TestStatus.TIMEOUT.value
        test_startup_error = any(re.search(pattern, run_logs, re.MULTILINE)
            for pattern in TEST_STARUP_ERROR_PATTERNS)
        if test_startup_error:
            return TestStatus.STARTUP_ERROR.value
        return TestStatus.COMPLETION.value
    
    @staticmethod
    def get_symbol_resolution_errors(run_logs: str) -> bool:
        """Get the cound of missing symbol errors from the run logs."""
        return sum(len(re.findall(pattern, run_logs, re.MULTILINE))
            for pattern in TEST_SYMBOL_RESOLUTION_ERROR_PATTERNS)
    
    def parse_test_logs(self, run_logs: str, logger: logging.Logger) -> dict[str, int]:
        """Parse the run logs based on test statuses."""
        logger.info(f"Parsing test logs...")
        test_result = {}
        for status, pattern in self.logs_parser.items():
            if pattern:
                logs_parse_re = re.compile(pattern, re.MULTILINE)
                m = None
                for m in logs_parse_re.finditer(run_logs):
                    pass
                if m:
                    test_result[status] = int(m.group(1))
                else:
                    test_result[status] = 0
        return test_result 
    
    @staticmethod
    def get_test_failures(test_result: dict[str, int]) -> int:
        """Returns test status as a comparable object based on test result."""
        return sum(test_result.get(status.value, 0) for status in FAILURE_STATUSES) 

