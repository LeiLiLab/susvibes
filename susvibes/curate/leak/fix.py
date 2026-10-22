"""Fix one instance's golden leak: sort the sweep's hits, remove what a rule covers, rebuild, and
prove the instance still validates the same way.

A copy counts as a leak only when our build left it behind (`real_hits`); each counted hit maps to a
directory to delete or is refused (`plan_removals`). A package download cache is stripped off the
eval image directly — nothing on an import path, nothing under /project, so the tests cannot change.
Anything else is removed from the env image (and the base_no_test image, when the record validates
from one), the instance is re-validated (which rebuilds the eval image), and the result must match: validated, the same expected_pf, the same Cmd/WorkingDir,
and a re-sweep of the new eval image with no counted hit. A removal that fails any of these restores
the original images under their standard names and leaves the instance as residue.
"""
import collections
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

import docker.errors

from susvibes.core.env import Deployment
from susvibes.core.report import reuse_report, save_report, strip_bookkeeping
from susvibes.core.utils import get_image_name, setup_instance_logger, save_file
from susvibes.curate.leak.constants import (
    LeakStatus, KEEP_STAGE, RECORD_FIELD, LOG_INSTANCE, LOG_REPORT, LOG_SWEEP, LOG_RESWEEP,
    VALIDATE_DIR_NAME, LEAK_IMAGE_NAME, PIP_VENDORED, PIP_PATHS, STDLIB, SITE_PKG,
    GENERIC_DIRS, PKG_CACHES, BUILD_OUTPUT, TMP_COPY, CACHE_RULES, INSTALLED_RULES, JS_BUILD_DIR,
    JS_BUILD_STEP, COMPILED, JS_LANGUAGES, SWEEP_MOUNT_DIR,
)
from susvibes.curate.leak.sweep import sweep_image
from susvibes.curate.validate import gen_test, with_test
from susvibes.curate.validate.constants import ValidateStatus
from susvibes.curate.utils import get_golden_patch

# One pull at a time: the Hub rate-limits, and parallel pulls share one uplink anyway.
pull_lock = threading.Semaphore(1)


# --- sorting hits ------------------------------------------------------------------------------

def own_packages(touched: list) -> set:
    """Top-level package names of the golden files (`src/click/x.py` -> click; a single-module
    project's `bottle.py` -> bottle)."""
    own = set()
    for rel in touched:
        parts = [p for p in rel.split("/")[:-1] if p not in GENERIC_DIRS]
        name = parts[0] if parts else rel.split("/")[-1].split(".")[0]
        own.add(name.replace("-", "_").lower())
    return own


def is_own_module(path: str, touched: list) -> bool:
    """Whether a hit (a file or an archive member) is a copy of one of the golden modules."""
    logical = path.split("!", 1)[-1].replace("/__pycache__/", "/")
    logical = COMPILED.sub(".py", logical)
    return any(logical.endswith("/" + "/".join(rel.split("/")[-2:])) or
               logical == "/".join(rel.split("/")[-2:]) for rel in touched)


def is_own_name(name: str, own: set) -> bool:
    """Whether an installed package / archive name belongs to this project. A name that starts with
    one of the project's package names counts, so a dependency sharing the prefix is counted rather
    than set aside — it then has no removal rule and the instance lands in residue, the safe side."""
    name = name.split("-")[0].replace("-", "_").lower()
    return name in own or any(name.startswith(o) for o in own)


def aside_reason(hit: dict, own: set) -> str | None:
    """Why a hit is not a leak our build left behind, or None when it is one. Set aside: tracked
    repo files (the agent sees them anyway), git objects (wiped when the eval image is built), pip's
    vendored copy, the sweep's own mounted python, the CPython stdlib, an installed package or cached
    archive of another project (a dependency that shares or bundles the code — tensorflow's keras,
    fastapi under cadwyn — is the environment), and anything under node_modules, which a lockfile
    pins: a dependency, or a published release of the project itself (named separately, so a
    reviewer can filter on it; a lockfile at the base commit pins a release published before the
    fix, so that copy normally predates the golden code — the openclaw audit found no exception)."""
    path, _, member = hit["path"].partition("!")
    if hit["kind"] == "git-object":
        return "git object"
    if hit["tracked"]:
        return "tracked repo file"
    if path.startswith(SWEEP_MOUNT_DIR):
        return "sweep python"
    if PIP_VENDORED.search(path) or PIP_PATHS.search(path):
        return "pip vendored copy"
    if STDLIB.match(path):
        return "CPython stdlib"
    site = SITE_PKG.search(path)
    if site and not is_own_name(site.group(1), own):
        return "another project's installed package"
    if member and PKG_CACHES.match(path) and not is_own_name(member.split("/")[0], own):
        return "another project's cached archive"
    if "/node_modules/" in path:
        parts = path.split("/node_modules/")[-1].split("/")
        name = "/".join(parts[:2]) if parts[0].startswith("@") else parts[0]
        return "own package under node_modules" if is_own_name(name.split("/")[-1], own) else "npm dependency"
    return None


def real_hits(hits: list, touched: list) -> tuple[list, list]:
    """(hits that count as a leak, hits set aside) — each hit set aside carries its `aside` reason
    (see `aside_reason`)."""
    own = own_packages(touched)
    counted, aside = [], []
    for hit in hits:
        reason = aside_reason(hit, own)
        if reason is None:
            counted.append(hit)
        else:
            hit["aside"] = reason
            aside.append(hit)
    return counted, aside


def plan_removals(hits: list, touched: list, scripts_text: str, is_js: bool) -> tuple[dict, list]:
    """Map counted hits onto directories to delete ({path: why}); a hit no rule covers is returned
    as a refusal. Caches, bytecode, stray copies under /tmp and build output go wholesale; an
    installed copy only when it is this project's own module, never a same-named stranger. JS build
    output goes whether or not the test scripts rebuild it — re-validating decides if the suite can
    live without it."""
    targets, refused, unresolved = {}, [], []
    rebuilds = bool(JS_BUILD_STEP.search(scripts_text))
    for hit in hits:
        path = hit["path"].split("!", 1)[0]
        cache = PKG_CACHES.match(path)
        build_output = BUILD_OUTPUT.match(path)
        tmp_copy = TMP_COPY.match(path)
        js_build = JS_BUILD_DIR.match(path) if is_js else None
        if cache:
            targets.setdefault(cache.group(1), "package cache")
        elif build_output and hit["untracked"]:
            targets.setdefault(build_output.group(1), "build output")
        elif tmp_copy and "!" not in hit["path"]:
            targets.setdefault(tmp_copy.group(1), "stray copy under /tmp")
        elif "!" in hit["path"] and not SITE_PKG.search(path) and not hit["tracked"]:
            # an archive built from the golden tree and left behind (a wheel under /tmp, dist/*.tar.gz)
            targets.setdefault(tmp_copy.group(1) if tmp_copy else path, "stray built archive")
        elif hit["untracked"] and "/__pycache__/" in path:
            targets.setdefault(path[:path.index("/__pycache__/") + len("/__pycache__")], "in-tree __pycache__")
        elif js_build and hit["untracked"]:
            targets.setdefault(js_build.group(1), "JS build output"
                               + ("" if rebuilds else " (no rebuild step in the test scripts; validate decides)"))
        else:
            rule = next(((m.group(1), why) for pattern, why in CACHE_RULES
                         for m in [pattern.match(path)] if m), None)
            if rule is None and is_own_module(hit["path"], touched):
                rule = next(((m.group(1), why) for pattern, why in INSTALLED_RULES
                             for m in [pattern.match(path)] if m), None)
            if rule is None and hit["untracked"] and "/site-packages/" not in path and \
                    (is_own_module(hit["path"], touched) or path.startswith("/project/build/")):
                rule = (path, "in-tree build artifact")
            if rule is None:
                unresolved.append(hit)
            else:
                targets.setdefault(*rule)
    # A hit inside a directory already being removed goes with it.
    for hit in unresolved:
        path = hit["path"].split("!", 1)[0]
        if not any(path.startswith(target + "/") for target in targets):
            refused.append(f"{hit['kind']} {hit['path']}")
    return dict(sorted(targets.items())), refused


def get_scripts_text(data_record: dict, env_spec: dict) -> str:
    """Everything that runs the tests: the synthesized runner script in test_patch, the test
    adapter's script, and the Dockerfile's CMD lines."""
    match = re.search(r"\+\+\+ b/\.sv\.run_gen_test\.sh\n(?:.*\n)*?((?:\+.*\n)+)", data_record["test_patch"])
    text = match.group(1) if match else ""
    text += env_spec["logs_handler"].get("test_adapter", {}).get("script", "")
    text += "\n".join(line for line in env_spec["dockerfile"].splitlines() if line.startswith("CMD"))
    return text


# --- images ------------------------------------------------------------------------------------

def get_leak_image_name(role: str, instance_id: str) -> str:
    """The name the leaky `<role>_<id>` image is kept under."""
    return get_image_name(LEAK_IMAGE_NAME.format(role=role, instance_id=instance_id))


def get_image(image_name: str, role: str, pull_images: bool, logger: logging.Logger) -> None:
    """Make `image_name` local under that name: pull it when `pull_images` (a registry override
    pulls it under the rewritten name, so the record's name is added), else require it. Raises
    RuntimeError when it is nowhere to be found or the pull failed."""
    try:
        if pull_images:
            with pull_lock:
                image = Deployment.from_pull(logger=logger, image_name=image_name).image
            if image_name not in image.tags and f"{image_name}:latest" not in image.tags:
                assert image.tag(image_name)
        else:
            Deployment.from_local(logger=logger, image_name=image_name)
    except docker.errors.NotFound:          # ImageNotFound included
        msg = f"{role} image not found: {image_name}"
        logger.error(msg)
        raise RuntimeError(msg)
    except docker.errors.APIError as e:     # the daemon or the Hub refused: a miss, not a verdict
        msg = f"Failed to pull image: {image_name}: {e}"
        logger.error(msg)
        raise RuntimeError(msg)


def build_removed_image(source_image_name: str, target_image_name: str, targets: dict,
                        logger: logging.Logger) -> None:
    """`target_image_name` = `source_image_name` minus the `targets` directories."""
    removals = " ".join(f"'{path}'" for path in targets)
    dockerfile = f"FROM {source_image_name}\nRUN rm -rf -- {removals}\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        Deployment.from_build(logger=logger, context_path=Path(tmpdir), dockerfile=dockerfile,
                              image_name=target_image_name)


def same_expected_pf(a: dict, b: dict) -> bool:
    """Whether two expected_pf agree. A case-parsed run lists its cases in set order, so a list
    compares as a set."""
    normalize = lambda pf: {k: sorted(v) if isinstance(v, list) else v for k, v in pf.items()}
    return normalize(a) == normalize(b)


def restore_images(snapshots: dict, logger: logging.Logger) -> None:
    """Give every standard name back to its snapshot ({standard name: snapshot name})."""
    for image_name, snapshot_name in snapshots.items():
        Deployment.tag_image(snapshot_name, image_name, logger)


def revalidate(data_record: dict, env_spec: dict, log_dir: Path) -> dict:
    """Re-run the validation this instance passed — gen_test or with_test by its flags, from the
    base_no_test image when the record names one (an edited test_patch) — on the images under their
    standard names, in a fresh log dir of its own; returns validate's report. The env_spec's logs
    handler is reused, so no parser is re-synthesized."""
    validate_log_dir = log_dir / VALIDATE_DIR_NAME
    shutil.rmtree(validate_log_dir / data_record["instance_id"], ignore_errors=True)
    if data_record["flags"].get("gen_test"):
        return gen_test.validate_single(data_record, env_spec, validate_log_dir)
    return with_test.validate_single(data_record, env_spec, validate_log_dir,
                                     from_base_no_test_image=bool(data_record.get("base_no_test_image_name")))


# --- the report --------------------------------------------------------------------------------

def leak_report(status: LeakStatus, **payload) -> dict:
    """A report for an instance that concluded: the status plus what it carries — the hit counts,
    the removals and refusals, a `reason` when the leak stays, and every image the fix touched."""
    return {"leak_status": status, **payload, "error": None}


def leak_miss(error: str) -> dict:
    """A report for a run that concluded nothing — the sweep, a build or a pull failed. Mirrors
    `validate_miss`."""
    return {"leak_status": None, "error": error}


def apply_leak_report(report: dict, data_record: dict, known_residue: set) -> None:
    """Project one concluded report onto the record: this stage's `keep` verdict (residue goes,
    unless the instance's residue was reviewed and listed as known) and the report itself."""
    residue = report["leak_status"] == LeakStatus.RESIDUE
    known = residue and data_record["instance_id"] in known_residue
    data_record.setdefault("keep", {})[KEEP_STAGE] = not residue or known
    data_record[RECORD_FIELD] = {**strip_bookkeeping(report), "known_residue": known}


# --- the worker --------------------------------------------------------------------------------

def fix_leak(data_record: dict, env_spec: dict, log_dir: Path, logger: logging.Logger,
             pull_images: bool, push_images: bool) -> dict:
    """The check and fix of one instance; returns its report. Every early `return` is a conclusion;
    a RuntimeError / BuildError is the harness failing, reported as a miss once the standard names
    are restored."""
    instance_id = data_record["instance_id"]
    eval_image_name = data_record["image_name"]
    eval_leak_image_name = get_leak_image_name("eval", instance_id)
    # What validate starts from: the env image, plus the base_no_test image when the record names one.
    source_image_names = {"env": data_record["env_image_name"]}
    if data_record.get("base_no_test_image_name"):
        source_image_names["base_no_test"] = data_record["base_no_test_image_name"]
    images = {"eval": eval_image_name}
    snapshots = {}      # standard name -> snapshot of the image it held before the fix

    try:
        golden_patch = get_golden_patch(data_record)
        get_image(eval_image_name, "Eval", pull_images, logger)
        sweep = sweep_image(golden_patch, eval_image_name, logger)
        if sweep["apply"] == "FAIL":
            msg = f"golden_patch did not apply in the eval image: {eval_image_name}"
            logger.error(msg)
            raise RuntimeError(msg)
        counted, aside = real_hits(sweep["hits"], sweep["touched"])
        save_file({"apply": sweep["apply"], "hits": sweep["hits"]}, log_dir / LOG_SWEEP)
        counts = {"hits": len(counted), "aside": dict(collections.Counter(hit["aside"] for hit in aside))}
        if not counted:
            logger.info(f"Eval image clean ({len(aside)} hit(s) set aside: {counts['aside']}).")
            return leak_report(LeakStatus.CLEAN, **counts, images=images)

        for hit in counted:
            logger.info(f"  [{hit['kind']}] {hit['path']}")
        Deployment.tag_image(eval_image_name, eval_leak_image_name, logger)
        images["eval_leak"] = eval_leak_image_name
        snapshots[eval_image_name] = eval_leak_image_name
        is_js = data_record["language"].lower() in JS_LANGUAGES
        targets, refused = plan_removals(counted, sweep["touched"], get_scripts_text(data_record, env_spec), is_js)
        plan = {**counts, "removals": targets, "refused": refused, "images": images}
        if refused:
            reason = f"{len(refused)} hit(s) without a removal rule"
            logger.warning(f"Leak stays: {reason}: {refused}")
            return leak_report(LeakStatus.RESIDUE, **plan, reason=reason)
        logger.info(f"Removing {list(targets)}...")

        cache_only = all(PKG_CACHES.match(target + "/") for target in targets)
        plan["cache_only"] = cache_only
        if cache_only:
            # Nothing on an import path and nothing under /project: the layer goes straight onto
            # the eval image and the re-validation is skipped. The re-sweep still has to be clean.
            build_removed_image(eval_leak_image_name, eval_image_name, targets, logger)
        else:
            for role, image_name in source_image_names.items():
                get_image(image_name, role.capitalize(), pull_images, logger)
                leak_image_name = get_leak_image_name(role, instance_id)
                Deployment.tag_image(image_name, leak_image_name, logger)
                images.update({role: image_name, f"{role}_leak": leak_image_name})
                snapshots[image_name] = leak_image_name
                build_removed_image(leak_image_name, image_name, targets, logger)
            validate_report = revalidate(data_record, env_spec, log_dir)
            plan["validate_status"] = validate_report["validate_status"]
            if validate_report["validate_status"] != ValidateStatus.VALIDATED:
                reason = "removing it breaks validation: {}".format(
                    validate_report["error"] or validate_report.get("reason") or validate_report["validate_status"])
                logger.warning(f"Leak stays: {reason}")
                restore_images(snapshots, logger)
                return leak_report(LeakStatus.RESIDUE, **plan, reason=reason)
            plan["expected_pf"] = validate_report["expected_pf"]
            if not same_expected_pf(validate_report["expected_pf"], data_record["expected_pf"]):
                reason = f"removing it changes expected_pf: {data_record['expected_pf']} -> {validate_report['expected_pf']}"
                logger.warning(f"Leak stays: {reason}")
                restore_images(snapshots, logger)
                return leak_report(LeakStatus.RESIDUE, **plan, reason=reason)
            config_before = Deployment.get_image_config(eval_leak_image_name)
            config_after = Deployment.get_image_config(eval_image_name)
            if config_before != config_after:
                reason = f"removing it changes the eval image's Cmd/WorkingDir: {config_before} -> {config_after}"
                logger.warning(f"Leak stays: {reason}")
                restore_images(snapshots, logger)
                return leak_report(LeakStatus.RESIDUE, **plan, reason=reason)

        resweep = sweep_image(golden_patch, eval_image_name, logger)
        save_file({"apply": resweep["apply"], "hits": resweep["hits"]}, log_dir / LOG_RESWEEP)
        still, _ = real_hits(resweep["hits"], resweep["touched"])
        if still:
            reason = f"{len(still)} hit(s) remain after the removal: " + "; ".join(hit["path"] for hit in still[:3])
            logger.warning(f"Leak stays: {reason}")
            restore_images(snapshots, logger)
            return leak_report(LeakStatus.RESIDUE, **plan, reason=reason)
        logger.info(f"Leak fixed: {eval_image_name} rebuilt without {list(targets)}.")
        if push_images:
            # What the pipeline publishes: the eval image and the env image. A record validated from
            # base_no_test exercised that image's removal, not the env image's — the same directories
            # on the same build, pushed on that strength.
            for image_name in [eval_image_name] + ([source_image_names["env"]] if not cache_only else []):
                Deployment.push_image(image_name, logger)
        return leak_report(LeakStatus.FIXED, **plan)
    except (RuntimeError, docker.errors.BuildError) as e:
        logger.error(f"Leak check did not conclude: {e}")
        restore_images(snapshots, logger)
        return leak_miss(str(e))


def fix_leak_single(
    data_record: dict,
    env_spec: dict,
    leak_log_dir: Path,
    pull_images: bool = False,
    push_images: bool = False,
    force: bool = False,
    resume: bool = False,
) -> dict:
    """Check and fix one instance's eval image, reusing its cached report unless `force`/`resume`
    asks to re-run it. Always returns a report: `leak_status` says how it concluded, `error` is set
    only when the harness itself failed. A fixed instance's images are rebuilt under their standard
    names (pushed when `push_images`), with the leaky originals kept as `<role>_leak_<id>`."""
    instance_id = data_record["instance_id"]
    log_dir = leak_log_dir / instance_id
    report = reuse_report(log_dir / LOG_REPORT, force=force, resume=resume)
    if report is not None:
        return report

    logger = setup_instance_logger(log_dir / LOG_INSTANCE, __spec__.name, instance_id, handle_tqdm=True)
    logger.info(f"Checking the eval image of {instance_id} for golden leaks...")
    # An earlier attempt that died mid-fix leaves its snapshots behind; plan from the originals, so
    # a half-rebuilt image under the standard name cannot hide what it removed.
    for role, field in (("eval", "image_name"), ("env", "env_image_name"), ("base_no_test", "base_no_test_image_name")):
        if data_record.get(field):
            try:
                Deployment.tag_image(get_leak_image_name(role, instance_id), data_record[field], logger)
            except docker.errors.ImageNotFound:
                pass
    report = fix_leak(data_record, env_spec, log_dir, logger, pull_images, push_images)
    save_report(report, log_dir / LOG_REPORT)
    return report
