"""
Build / push / pull the base_py + dind_py + static_py images for selected Python versions.

  --mode         (required): build | push | pull
  --image_names  (default all three): JSON list of "base_py", "dind_py", "static_py"
  --versions     (required): JSON list of Python versions

dind_py and static_py are built FROM base_py. static_py adds the version-matched jedi/parso stack
check_cov needs (these may fail to install on old interpreters, failing that version's build).
Rebuilds can drift the upstream python base, so pass only the versions you need.

  python -m susvibes.curate.env_setup.build_base --mode build --versions '["3.6"]'
  python -m susvibes.curate.env_setup.build_base --mode build --image_names '["static_py"]' --versions '["3.10"]'
  python -m susvibes.curate.env_setup.build_base --mode pull --versions '["3.10"]'
"""

import argparse
import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker.errors
from tqdm import tqdm

from susvibes.core.env import Deployment
from susvibes.core.constants import ImageLoc
from susvibes.curate.constants import get_log_dir
from susvibes.env_specs import DEV_TOOL_VERSIONS
from susvibes.env_specs.dockerfiles import DOCKERFILE_BASE_PY, DOCKERFILE_DIND_PY, DOCKERFILE_STATIC_PY
from susvibes.core.utils import get_image_name, setup_logger
from susvibes.core.report import get_two_state_summary, print_summary

DEV_TOOL_NAME = "python"
IMAGE_NAMES = ("base_py", "dind_py", "static_py")


def _build_single(image_name: str, version: str, dockerfile_content: str,
                  logger: logging.Logger) -> tuple[str | None, str | None]:
    """Build the get_image_name(image_name):version image. Returns (image_tag, None)
    on success, (None, reason) on build failure."""
    image_tag = f"{get_image_name(image_name)}:{version}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            Deployment.from_build(logger, context_path=Path(tmp),
                dockerfile=dockerfile_content, image_name=image_tag)
    except docker.errors.BuildError as e:
        return None, f"Failed to build image: {e}"
    return image_tag, None


def build_base_py_single(version: str, upstream_image_name: str, logger: logging.Logger) -> tuple[str | None, str | None]:
    """Build the base_py image for this version from upstream <upstream_image_name>."""
    return _build_single("base_py", version,
        DOCKERFILE_BASE_PY.format(upstream_image_name=upstream_image_name), logger)


def build_dind_py_single(version: str, logger: logging.Logger) -> tuple[str | None, str | None]:
    """Build the dind_py image for this version from the base_py image."""
    base_py_image = f'{get_image_name("base_py")}:{version}'
    return _build_single("dind_py", version,
        DOCKERFILE_DIND_PY.format(base_image=base_py_image), logger)


def build_static_py_single(version: str, logger: logging.Logger) -> tuple[str | None, str | None]:
    """Build the static_py image for this version from the base_py image with matched jedi/parso."""
    base_py_image = f'{get_image_name("base_py")}:{version}'
    static_deps = DEV_TOOL_VERSIONS[DEV_TOOL_NAME]["versions"][version]["static_deps"]
    return _build_single("static_py", version,
        DOCKERFILE_STATIC_PY.format(base_image=base_py_image, jedi_parso=static_deps), logger)


def build_threadpool(image_name: str, versions: list, build_fn, max_workers: int) -> list:
    """Build `image_name` images for each version via build_fn(version) in parallel.

    Returns the list of (version, image_tag) pairs that succeeded.
    """
    print(f"\nBuilding {len(versions)} {image_name} images: {versions}")
    built, errored = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(build_fn, version): version for version in versions}
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Building [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                version = futs[f]
                try:
                    image_tag, reason = f.result()
                except Exception as e:
                    raise RuntimeError(f"Internal error for {version}: {e}")
                if image_tag:
                    built.append((version, image_tag))
                else:
                    errored[version] = reason
                pbar.update(1)
                pbar.set_description(
                    f"{len(built)} built, {len(errored)} failed")
    print_summary(get_two_state_summary([tag for _, tag in built], errored))
    return built


def push_threadpool(push_targets: list, max_workers: int) -> None:
    """Push (image_name, version) pairs in parallel; missing locals are skipped."""
    def _push(image_name, version):
        image_tag = f"{get_image_name(image_name)}:{version}"
        try:
            Deployment.collect_image(image_name=image_tag)
        except docker.errors.ImageNotFound:
            return image_tag, f"Image not found locally: {image_tag}"
        try:
            Deployment.push_image(image_tag)
            return image_tag, None
        except Exception as e:
            return image_tag, f"Failed to move image: {e}"

    print(f"\nPushing {len(push_targets)} images...")
    succeeded, errored = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_push, image_name, version) for image_name, version in push_targets]
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Pushing [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                image_tag, err = f.result()
                if err is None:
                    succeeded.append(image_tag)
                else:
                    errored[image_tag] = err
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} pushed, {len(errored)} failed")
    print_summary(get_two_state_summary(succeeded, errored))


def pull_threadpool(pull_targets: list, max_workers: int) -> None:
    """Pull (image_name, version) pairs from the Hub."""
    def _pull(image_name, version):
        image_tag = f"{get_image_name(image_name)}:{version}"
        try:
            Deployment.collect_image(image_name=image_tag, image_loc=ImageLoc.REMOTE)
            return image_tag, None
        except Exception as e:
            return image_tag, f"Failed to move image: {e}"

    print(f"\nPulling {len(pull_targets)} images...")
    succeeded, errored = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_pull, image_name, version) for image_name, version in pull_targets]
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Pulling [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                image_tag, err = f.result()
                if err is None:
                    succeeded.append(image_tag)
                else:
                    errored[image_tag] = err
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} pulled, {len(errored)} failed")
    print_summary(get_two_state_summary(succeeded, errored))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument(
        "--mode", choices=["build", "push", "pull"], required=True,
        help="build | push | pull the selected images.",
    )
    supported_image_names = list(IMAGE_NAMES)
    supported_versions = list(DEV_TOOL_VERSIONS[DEV_TOOL_NAME]["versions"])
    parser.add_argument(
        "--image_names", type=json.loads, default=supported_image_names,
        help=f'JSON list of image names among {supported_image_names}. '
             'Default: all. Example: --image_names \'["static_py"]\'',
    )
    parser.add_argument(
        "--versions", type=json.loads, default=supported_versions,
        help='JSON list of Python versions to operate on. '
             'Default: all. Example: --versions \'["2.7", "3.5"]\'',
    )
    args = parser.parse_args()
    versions = list(args.versions)
    image_names = list(args.image_names)
    invalid_image_names = [name for name in image_names if name not in supported_image_names]
    if invalid_image_names:
        parser.error(f'--image_names must be among {supported_image_names}, got {invalid_image_names}')
    invalid_versions = [v for v in versions if v not in supported_versions]
    if invalid_versions:
        parser.error(f'--versions must be among {supported_versions}, got {invalid_versions}')
    logger = setup_logger(get_log_dir("default", "env_setup"), "build_base.log",
        __spec__.name, add_stdout=False, mode="w")

    if args.mode == "build":
        if "base_py" in image_names:
            build_threadpool(
                "base_py", versions,
                lambda version: build_base_py_single(
                    version, DEV_TOOL_VERSIONS[DEV_TOOL_NAME]["versions"][version]["upstream_image_name"], logger),
                args.max_workers,
            )
        # dind_py/static_py are built FROM base_py, so it must already exist locally.
        if "dind_py" in image_names or "static_py" in image_names:
            missing_base = []
            for version in versions:
                base_py_image = f'{get_image_name("base_py")}:{version}'
                try:
                    Deployment.collect_image(image_name=base_py_image)
                except docker.errors.ImageNotFound:
                    missing_base.append(base_py_image)
            if missing_base:
                raise RuntimeError(f"Base image(s) not found: {missing_base}.")
        if "dind_py" in image_names:
            build_threadpool(
                "dind_py", versions,
                lambda version: build_dind_py_single(version, logger),
                args.max_workers,
            )
        if "static_py" in image_names:
            build_threadpool(
                "static_py", versions,
                lambda version: build_static_py_single(version, logger),
                args.max_workers,
            )
    elif args.mode == "push":
        # missing locals are skipped.
        targets = [(image_name, version) for image_name in image_names for version in versions]
        push_threadpool(targets, args.max_workers)
    elif args.mode == "pull":
        targets = [(image_name, version) for image_name in image_names for version in versions]
        pull_threadpool(targets, args.max_workers)
