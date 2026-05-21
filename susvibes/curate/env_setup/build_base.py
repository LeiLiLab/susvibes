"""
Build and/or push the canonical base_py + dind_py images for selected Python minors.

`--mode` picks the phase(s) to run:
  build  -> build base_py + dind_py for `--versions`
  push   -> push base_py + dind_py for `--versions`
  all    -> run both (default)

Be conservative with rebuilds: base_py:{3.7..3.12} already exist locally + on
Docker Hub from a prior build; rebuilding could pick up patch-version drift in
the python:X.Y-bookworm base image and break already-validated downstream env
images. Pass only the minors you actually need.

  python -m susvibes.curate.env_setup.build_base --versions '["2.7","3.5","3.6"]'
  python -m susvibes.curate.env_setup.build_base --mode build --versions '["3.6"]'
  python -m susvibes.curate.env_setup.build_base --mode push --versions '["3.10"]'
"""

import argparse
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker
import docker.errors
from tqdm import tqdm

from susvibes.curate.utils import push_image_to_hub
from susvibes.env_specs import DEV_TOOL_VERSIONS
from susvibes.env_specs.dockerfiles import DOCKERFILE_BASE_PY, DOCKERFILE_DIND_PY
from susvibes.utils import get_image_name

DEV_TOOL_NAME = "python"

docker_client = docker.from_env()


def _build_single(short_name: str, short_minor: str, dockerfile_content: str):
    """Build <short_name>:<short_minor>; also tag with hub prefix. Returns (local_tag, hub_tag)."""
    local_tag = f"{short_name}:{short_minor}"
    hub_tag = f"{get_image_name(short_name)}:{short_minor}"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "Dockerfile").write_text(dockerfile_content)
        resp = docker_client.api.build(
            path=tmp,
            tag=local_tag,
            rm=True,
            forcerm=True,
            decode=True,
        )
        buildlog = ""
        for chunk in resp:
            if "stream" in chunk:
                buildlog += chunk["stream"]
            elif "errorDetail" in chunk:
                raise docker.errors.BuildError(
                    chunk["errorDetail"]["message"], buildlog)
    docker_client.images.get(local_tag).tag(hub_tag)
    return local_tag, hub_tag


def build_base_py_single(short_minor: str, base_tag: str):
    """Build base_py:<short_minor> from python:<base_tag>."""
    return _build_single("base_py", short_minor,
        DOCKERFILE_BASE_PY.format(version=base_tag))


def build_dind_py_single(short_minor: str):
    """Build dind_py:<short_minor> from local base_py:<short_minor>."""
    return _build_single("dind_py", short_minor,
        DOCKERFILE_DIND_PY.format(version=short_minor))


def push_single(short_name, version, max_retries=3):
    image = f"{get_image_name(short_name)}:{version}"
    try:
        docker_client.images.get(image)
    except docker.errors.ImageNotFound:
        return image, "Image not found locally."
    try:
        push_image_to_hub(image, max_retries=max_retries)
        return image, None
    except Exception as e:
        return image, str(e)


def build_threadpool(short_name: str, minors: list, build_fn, max_workers: int):
    """Build `short_name` images for each minor via build_fn(minor) in parallel.

    Returns the list of (minor, (local_tag, hub_tag)) pairs that succeeded.
    """
    print(f"\nBuilding {len(minors)} {short_name} images: {minors}")
    built, failed = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(build_fn, m): m for m in minors}
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Building [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                m = futs[f]
                try:
                    built.append((m, f.result()))
                except Exception as e:
                    failed[m] = str(e)
                pbar.update(1)
                pbar.set_description(
                    f"{len(built)} built, {len(failed)} failed")
    if built:
        print(f"Built ({len(built)}):")
        for m, (local_tag, hub_tag) in built:
            print(f"  python {m}: {local_tag} + {hub_tag}")
    if failed:
        print(f"Build failed ({len(failed)}):")
        for m, err in failed.items():
            print(f"  python {m}: {err}")
    return built


def push_threadpool(push_targets: list, max_workers: int):
    """Push (short_name, version) pairs in parallel; missing locals are skipped."""
    print(f"\nPushing {len(push_targets)} images...")
    succeeded, failed = [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(push_single, s, v): (s, v) for s, v in push_targets}
        with tqdm(total=len(futs), dynamic_ncols=True,
            desc=f"Pushing [{max_workers} threads]") as pbar:
            for f in as_completed(futs):
                image, err = f.result()
                if err is None:
                    succeeded.append(image)
                else:
                    failed[image] = err
                pbar.update(1)
                pbar.set_description(
                    f"{len(succeeded)} pushed, {len(failed)} failed")
    if succeeded:
        print(f"Pushed ({len(succeeded)}):")
        for img in succeeded:
            print(f"  {img}")
    if failed:
        print(f"Failed ({len(failed)}):")
        for img, err in failed.items():
            print(f"  {img}: {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument(
        "--mode", choices=["build", "push", "all"], default="all",
        help="Which phase(s) to run. Default: all.",
    )
    parser.add_argument(
        "--versions", type=json.loads, required=True,
        help='JSON list of Python minors to operate on. '
             'Example: --versions \'["2.7", "3.5"]\'',
    )
    args = parser.parse_args()
    versions = list(args.versions)

    if args.mode in ("build", "all"):
        # base_py first, then dind_py only for the base_py builds that succeeded
        # (dind_py FROMs base_py:{minor}).
        base_built = build_threadpool(
            "base_py", versions,
            lambda m: build_base_py_single(m, DEV_TOOL_VERSIONS[DEV_TOOL_NAME]["versions"][m]),
            args.max_workers,
        )
        build_threadpool(
            "dind_py", [m for m, _ in base_built],
            build_dind_py_single,
            args.max_workers,
        )

    if args.mode in ("push", "all"):
        # push_single skips images not present locally.
        push_targets = (
            [("base_py", v) for v in versions] +
            [("dind_py", v) for v in versions]
        )
        push_threadpool(push_targets, args.max_workers)
