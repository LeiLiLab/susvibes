import os
import time
import uuid
import re
import signal
import threading
import logging
import tempfile
from pathlib import Path

import docker
import docker.errors
import requests
from docker.models.containers import Container
from docker.models.images import Image

from susvibes.core.constants import ContainerLimits, ImageLoc
from susvibes.env_specs import *
from susvibes.core.utils import get_image_name, get_instance_id, resolve_image_name, save_file
from susvibes.core.logs import LogsHandler, PassFailure

docker_client = docker.from_env()

PATCHES_DIR_NAME = "patches"


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
        return get_image_name("auto_{}".format(uuid.uuid4()))

    @staticmethod
    def collect_image(
        image_name: str = None,
        image_id: str = None,
        image_loc: ImageLoc = ImageLoc.LOCAL,
        logger: logging.Logger = None,
        max_retries: int = 5,
    ) -> Image:
        """Collect an image into the local daemon and return it. ImageLoc.LOCAL
        gets an existing local image (by name or id); ImageLoc.REMOTE pulls
        image_name from the Hub. Raises if absent. logger optional (silent if None)."""
        if image_loc == ImageLoc.REMOTE:
            if not image_name:
                raise ValueError("Image name is required to pull a remote image.")
            resolved = resolve_image_name(image_name)
            if resolved != image_name and logger:
                logger.info(f"Overriding image registry for {image_name} -> {resolved}.")
            try:
                for retry in range(max_retries):
                    try:
                        image = docker_client.images.pull(resolved)
                        break
                    except docker.errors.NotFound:
                        if retry == max_retries - 1:
                            raise
                if logger:
                    logger.info(f"Image {image_name} pulled successfully.")
                return image
            except docker.errors.APIError as e:
                if logger:
                    logger.warning(f"Error pulling {image_name}: {e}")
                raise
        if not image_id and not image_name:
            raise ValueError("Image name or image id is required.")
        ref = image_name or image_id
        try:
            for retry in range(max_retries):
                try:
                    image = docker_client.images.get(ref)
                    break
                except docker.errors.ImageNotFound:
                    if retry == max_retries - 1:
                        raise
            if logger:
                logger.info(f"Image {ref} found locally.")
            if not image.tags:
                default_image_name = Deployment.get_default_image_name()
                if logger:
                    logger.warning(f"Image has no names, tagging a default name {default_image_name}.")
                assert image.tag(default_image_name)
            return image
        except docker.errors.APIError as e:
            if logger:
                logger.warning(f"Error getting {ref}: {e}")
            raise

    @staticmethod
    def push_image(image_name: str, logger: logging.Logger = None,
                   max_retries: int = 5, base_delay: int = 5) -> None:
        """Push image_name to the Docker Hub, retrying transient errors (rate
        limiting, network blips) with exponential backoff. logger optional."""
        for retry in range(max_retries):
            try:
                response = docker_client.images.push(image_name, stream=True, decode=True)
                for chunk in response:
                    if any(k in chunk for k in ["error", "denied"]):
                        raise docker.errors.APIError(chunk["error"])
                if logger:
                    logger.info(f"Image {image_name} pushed successfully.")
                return
            except (docker.errors.APIError, requests.exceptions.RequestException):
                if retry == max_retries - 1:
                    raise
                time.sleep(base_delay * (2 ** retry))

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
            logger.warning(f"Error building {image_name}: {e}")
            logger.warning(f"Build log: {e.build_log}")
            raise
        except docker.errors.APIError as e:
            logger.warning(f"Error building {image_name}: {e}")
            raise docker.errors.BuildError(f"API error: {e}", "")

    @classmethod
    def from_pull(cls,
        logger: logging.Logger,
        image_name: str,
        remove_image: bool = False,
        remove_container: bool = True,
        max_retries: int = 3,
    ) -> "Deployment":
        image = cls.collect_image(image_name=image_name, image_loc=ImageLoc.REMOTE,
            logger=logger, max_retries=max_retries)
        return cls(image, logger, remove_image, remove_container)

    @classmethod
    def from_local(cls,
        logger: logging.Logger,
        image_name: str = None,
        image_id: str = None,
        remove_image: bool = False,
        remove_container: bool = True,
        max_retries: int = 3,
    ) -> "Deployment":
        image = cls.collect_image(image_name=image_name, image_id=image_id,
            image_loc=ImageLoc.LOCAL, logger=logger, max_retries=max_retries)
        return cls(image, logger, remove_image, remove_container)
    
    def create_container(
        self,
        command: str | list = None,
        mem_limit: str = None,
        cpu_limit: int = None,
    ) -> None:
        try:
            container = docker_client.containers.create(
                image=self.image.id,
                detach=True,
                mem_limit=mem_limit,
                nano_cpus=int(cpu_limit * 1e9) if cpu_limit else None,
                command=command
                # command="tail -f /dev/null",
            )
            self.logger.info(f"Container for {self.image.id} created: {container.name}")
            self.container = container
        except docker.errors.APIError as e:
            self.logger.warning(f"Error creating container for {self.image.id}: {e}")
            self.stop()
            raise
    
    def start(self) -> None | str:
        """Start the container and if wait is True return running logs."""
        try:
            self.container.start()
            self.logger.info(f"Container {self.container.name} started.")
        except docker.errors.APIError as e:
            self.logger.warning(f"Failed to start container {self.container.name}: {e}")
            raise

    def _remove_container(self) -> None:
        """Remove the container if it exists."""
        try:
            if self.container:
                self.container.remove(force=True)
                self.logger.info(f"Container {self.container.name} removed.")
        except docker.errors.NotFound as e:
            self.logger.info(f"Container {self.container.name} not found.")
        except Exception as e:
            self.logger.warning(f"Failed to remove container {self.container.name}: {e}")

    def _remove_image(self) -> None:
        self._remove_container()
        try:
            docker_client.images.remove(self.image.id, force=True)
            self.logger.info(f"Image {self.image.id} removed.")
        except docker.errors.ImageNotFound as e:
            self.logger.info(f"Image {self.image.id} not found.")
        except Exception as e:
            self.logger.warning(f"Failed to remove image {self.image.id}: {e}")

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
                        self.logger.warning(f"PID for container {self.container.name}: {pid} - not killing.")
                except Exception as e:
                    self.logger.warning(f"Failed to forcefully kill container {self.container.name}: {e}", exc_info=True)
        if self.remove_image:
            self._remove_image()
        elif self.remove_container:
            self._remove_container()

    def run_with_timeout(self, timeout: int = ContainerLimits.RUN_TIMEOUT, stop_timeout: int = 60) -> tuple[str, bool]:
        try:
            self.start()
        except docker.errors.APIError:
            self.stop()
            raise
        run_logs, timed_out = b"", False
        def run():
            nonlocal run_logs
            for chunk in self.container.logs(stream=True, follow=True, stdout=True, stderr=True):
                run_logs += chunk
            try:
                self.container.wait()
            except docker.errors.NotFound:
                return
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            elapsed = f"{timeout} seconds" if timeout < 300 else f"{timeout // 60} minutes"
            self.logger.warning(f"Container {self.container.name} run timed out after {elapsed}.")
            timed_out = True
            stop_thread = threading.Thread(target=self.stop, daemon=True)
            stop_thread.start()
            stop_thread.join(stop_timeout)
            if stop_thread.is_alive():
                stop_elapsed = f"{stop_timeout}s" if stop_timeout < 300 else f"{stop_timeout // 60} min"
                self.logger.warning(f"Container {self.container.name} stop exceeded {stop_elapsed}. Container may be orphaned.")
        else:
            self.stop()
        return run_logs.decode(), timed_out

class Env:
    project: str
    deployment: Deployment
    dev_tools: dict
    dockerfile: str
    dockerignore: str
    logs_handler: dict

    def __init__(
        self,
        logger: logging.Logger,
        project: str,
        image_name: str,
        dev_tools: dict,
        dockerfile: str,
        dockerignore: str = None,
        image_loc: ImageLoc = ImageLoc.LOCAL,
        logs_handler: dict = None,
        remove_image: bool = False,
        remove_container: bool = True
    ):
        self.project = project
        self.dev_tools = dev_tools
        self.dockerfile = dockerfile
        self.dockerignore = dockerignore
        self.logs_handler = logs_handler
        logger.info(f"Collecting enviroment deployment...")
        collect_method = Deployment.from_local if image_loc == ImageLoc.LOCAL else \
            Deployment.from_pull if image_loc == ImageLoc.REMOTE else None
        self.deployment = collect_method(
            logger=logger,
            image_name=image_name,
            remove_image=remove_image,
            remove_container=remove_container
        )
    
    @staticmethod
    def _apply_patches(patches: list[tuple[str, dict]], pre_install: bool) -> str:
        """Build the `git apply` && chain for patches whose pre_install flag matches,
        kept in their original list order; each patch uses its own `reverse` flag."""
        cmds = []
        for id, (_, flags) in enumerate(patches):
            if bool(flags.get("pre_install", False)) != pre_install:
                continue
            reverse = " --reverse" if flags.get("reverse", False) else ""
            patch_path = f"{SUSVIBES_BUILD_DATA_DIR}/{PATCHES_DIR_NAME}/{id}.patch"
            cmds.append(f"git apply --ignore-space-change{reverse} {patch_path}")
        return " && ".join(cmds)

    def _compose_instance_dockerfile(
        self,
        base_commit: str,
        patches: list[tuple[str, dict]],
        reinstall: bool = True,
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
            'COPY . /\n'
        ])

        pre_cmds = type(self)._apply_patches(patches, pre_install=True)
        post_cmds = type(self)._apply_patches(patches, pre_install=False)
        if pre_cmds:
            instance_dockerfile += run_stm.format(reset_cmds)
            instance_dockerfile += run_stm.format(pre_cmds)
            if reinstall:
                instance_dockerfile += dependency_install_stm
        if post_cmds:
            instance_dockerfile += run_stm.format(post_cmds)

        for id, (_, flags) in enumerate(patches):
            save_to = flags.get("save_to")
            if save_to:
                src = f"{SUSVIBES_BUILD_DATA_DIR}/{PATCHES_DIR_NAME}/{id}.patch"
                instance_dockerfile += run_stm.format(
                    f"mkdir -p $(dirname {save_to}) && cp {src} {save_to}")

        commit_msg = "Instance created."
        commit_cmds = f'git add . && git commit --allow-empty -m "{commit_msg}" --no-verify'
        rm_cmd = f'rm -rf -- {SUSVIBES_BUILD_DATA_DIR}'
        instance_dockerfile += run_stm.format(commit_cmds) + \
            run_stm.format(rm_cmd) + cmd_stm
        return instance_dockerfile

    def build_instance_deployment(
        self,
        base_commit: str,
        patches: list[tuple[str, dict]],
        logger: logging.Logger,
        remove_image: bool = True,
        remove_container: bool = True,
    ) -> Deployment:
        """Build a instance-level Docker image from the environment.

        `patches` is an ordered list of (patch_str, flags); flags keys are all optional:
        `reverse` (git apply -R), `pre_install` (apply before dependency reinstall;
        default post_install), `save_to` (keep this .patch file at the given image
        path). All pre_install patches apply first (in order), then post_install."""
        logger.info(f"Building instance deployment...")
        banned_reinstall = BANNED_REINSTALL_FOR_INSTANCE.get(self.project, [])
        reinstall = True
        if any(base_commit.startswith(commit) for commit in banned_reinstall):
            logger.info(f"Reinstalling {self.project} at commit {base_commit} is banned.")
            reinstall = False

        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = Path(tmpdir)
            patches_dir = context_path / SUSVIBES_BUILD_DATA_DIR.lstrip("/") / PATCHES_DIR_NAME
            patches_dir.mkdir(parents=True, exist_ok=True)
            for id, (patch_str, _) in enumerate(patches):
                save_file(patch_str, patches_dir / f"{id}.patch")
            instance_dockerfile = self._compose_instance_dockerfile(
                base_commit, patches, reinstall)

            deployment = Deployment.from_build(
                logger=logger,
                context_path=context_path,
                dockerfile=instance_dockerfile,
                dockerignore=self.dockerignore,
                image_name=get_image_name(f"instance_{get_instance_id(self.project, base_commit)}"),
                remove_image=remove_image,
                remove_container=remove_container,
            )
        return deployment
    
    def handle_test_logs(self, test_logs: str, timed_out: bool,
        logger: logging.Logger, kind: str | tuple) -> PassFailure:
        """Apply this instance's logs handler for `kind` to the test logs, returning a PassFailure
        (status folded in). `kind` may be a tuple tried in priority order. See validate.logs."""
        return LogsHandler.handle_by_kind(kind, self.logs_handler, test_logs, timed_out, logger)

