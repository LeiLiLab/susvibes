"""Sweep one image's whole filesystem for any readable copy of an instance's golden code.

The golden code is rebuilt inside the image (golden_patch applied to a scratch copy of the work
tree), then the filesystem is walked:

- exact copies: every file whose size matches a golden file is hashed, and so is every member of
  every zip / tar archive (found by magic bytes, so pip's extension-less HTTP cache is included);
- derived copies: every file or archive member named after a golden module (`.pyc`, `.so`, `.c`,
  `.o`, another version's `.py`, ...), and every file under /project the upstream repo does not
  track, is grepped for golden-only lines — at least MIN_MARKERS of them, so one shared line is
  not a hit;
- every git object in /project is hashed.

A hit records whether its file is tracked by the upstream repo at the base commit (the tree the
agent is given, read from the local clone — the eval image's own git history is a fresh commit of
everything), untracked under /project, or outside /project.
"""
import json
import logging
import tarfile
import tempfile
import threading
from pathlib import Path

import requests

from susvibes.core.env import Deployment
from susvibes.core.utils import touched_files
from susvibes.curate.leak.constants import (
    SWEEP_PYTHON_URL, SWEEP_PYTHON_DIR, SWEEP_MOUNT_DIR, SWEEP_INPUT_DIR, SWEEP_TIMEOUT,
    SWEEP_MEM_LIMIT, SWEEP_CPU_LIMIT, MIN_MARKERS,
)

# Runs inside the image on the mounted python. Golden-only lines (markers) are computed against the
# image's own work tree, so the image must hold the task state (the eval image).
SWEEP_SRC = r'''
import base64, hashlib, json, os, subprocess, tarfile, zipfile

SKIP_DIRS = {"/proc", "/sys", "/dev", "/run", "/tmp/gold", "/project/.git", MOUNT_DIR, INPUT_DIR}
MAX_READ = 64 << 20

def sha_bytes(blob):
    return hashlib.sha256(blob).hexdigest()

def module_stem(name):
    return name.split(".", 1)[0]

inputs = json.load(open(INPUT_DIR + "/inputs.json"))
touched, tracked_files = inputs["touched"], set(inputs["tracked"])
gold, work, markers = {}, {}, {}
for rel in touched:
    gold_path, work_path = "/tmp/gold/" + rel, "/project/" + rel
    if not os.path.exists(gold_path):       # a file the golden patch deletes
        continue
    gold_text = open(gold_path, "rb").read()
    work_text = open(work_path, "rb").read() if os.path.exists(work_path) else b""
    gold[rel] = (sha_bytes(gold_text), len(gold_text))
    lines = {l.strip() for l in gold_text.split(b"\n") if len(l.strip()) >= 24}
    markers[rel] = sorted({l for l in lines if l not in work_text}, key=len, reverse=True)[:40]
for rel in touched:
    work_path = "/project/" + rel
    work[rel] = sha_bytes(open(work_path, "rb").read()) if os.path.exists(work_path) else None
stem_to_rels = {}
for rel in gold:
    stem_to_rels.setdefault(module_stem(os.path.basename(rel)), []).append(rel)

sha_to_rel = {s: rel for rel, (s, _) in gold.items()}
sizes = {size for _, size in gold.values()}
work_paths = {"/project/" + rel for rel in touched}

def best_markers(blob, rels):
    best = None
    for rel in rels:
        found = [m for m in markers.get(rel, []) if m in blob]
        if len(found) >= MIN_MARKERS and (best is None or len(found) > len(best[1])):
            best = (rel, found)
    return best

hits = []
def add(kind, path, rel, found=None):
    disk = path.split("!", 1)[0]
    tracked = disk.startswith("/project/") and disk[len("/project/"):] in tracked_files
    hit = {"kind": kind, "path": path, "rel": rel,
           "tracked": tracked,
           "untracked": disk.startswith("/project/") and not tracked}
    if found is not None:
        hit.update(markers=len(found), of=len(markers[rel]),
                   example=found[0][:80].decode("utf-8", "replace"))
    hits.append(hit)

def scan_members(path, members):
    for name, size, read in members:
        if size in sizes:
            digest = sha_bytes(read())
            if digest in sha_to_rel:
                add("archive", f"{path}!{name}", sha_to_rel[digest])
                continue
        rels = stem_to_rels.get(module_stem(os.path.basename(name)))
        if rels and size <= MAX_READ:
            best = best_markers(read(), rels)
            if best:
                add("archive-markers", f"{path}!{name}", best[0], best[1])

def zip_members(z):
    for info in z.infolist():
        if not info.is_dir():
            yield info.filename, info.file_size, (lambda i=info: z.read(i))

def tar_members(t):
    for info in t.getmembers():
        if info.isfile():
            yield info.name, info.size, (lambda i=info: t.extractfile(i).read())

for dirpath, dirnames, filenames in os.walk("/", topdown=True):
    dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in SKIP_DIRS]
    for name in filenames:
        path = os.path.join(dirpath, name)
        if path in work_paths:
            continue
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                magic = f.read(4)
        except OSError:
            continue
        if size in sizes:
            try:
                digest = sha_bytes(open(path, "rb").read())
            except OSError:
                digest = None
            if digest in sha_to_rel:
                add("file", path, sha_to_rel[digest])
                continue
        try:
            if magic == b"PK\x03\x04":
                with zipfile.ZipFile(path) as z:
                    scan_members(path, zip_members(z))
            elif magic[:2] == b"\x1f\x8b" and size <= (512 << 20):
                with tarfile.open(path, "r:*") as t:
                    scan_members(path, tar_members(t))
        except Exception:
            pass
        rels = stem_to_rels.get(module_stem(name))
        untracked = path.startswith("/project/") and path[len("/project/"):] not in tracked_files
        if (rels or untracked) and size <= MAX_READ:
            try:
                blob = open(path, "rb").read()
            except OSError:
                continue
            best = best_markers(blob, rels or list(markers))
            if best:
                add("markers" if rels else "untracked-markers", path, best[0], best[1])

objects = subprocess.run(["git", "-C", "/project", "cat-file", "--batch-all-objects",
                          "--batch-check=%(objectname) %(objecttype)"],
                         capture_output=True, text=True)
if objects.returncode != 0:
    raise SystemExit("git cat-file failed in /project: " + objects.stderr.strip()[-300:])
for line in objects.stdout.splitlines():
    name, _, kind = line.partition(" ")
    if kind != "blob":
        continue
    blob = subprocess.run(["git", "-C", "/project", "cat-file", "blob", name],
                          capture_output=True).stdout
    if sha_bytes(blob) in sha_to_rel:
        add("git-object", name, sha_to_rel[sha_bytes(blob)])

print("__RESULT__" + json.dumps({"gold": {r: s for r, (s, _) in gold.items()}, "work": work, "hits": hits}))
'''.replace("MOUNT_DIR", repr(SWEEP_MOUNT_DIR)).replace("INPUT_DIR", repr(SWEEP_INPUT_DIR)).replace("MIN_MARKERS", str(MIN_MARKERS))

# Rebuilds the golden files under /tmp/gold from the work tree + golden_patch, then runs the sweep.
# Inputs come by bind mount: an environment variable caps at 128 KiB, a patch does not.
CONTAINER_SH = f'''
PY={SWEEP_MOUNT_DIR}/bin/python3; export LD_LIBRARY_PATH={SWEEP_MOUNT_DIR}/lib:${{LD_LIBRARY_PATH:-}}
IN={SWEEP_INPUT_DIR}
mkdir -p /tmp/gold
cd /project
while read -r f; do [ -f "$f" ] && cp --parents "$f" /tmp/gold/ || true; done < $IN/touched.txt
cd /tmp/gold
if git apply -p1 $IN/golden.patch 2>/dev/null; then echo "__APPLY__git"
elif patch -p1 --batch --forward < $IN/golden.patch >/dev/null 2>&1; then echo "__APPLY__patch"
else echo "__APPLY__FAIL"; fi
$PY $IN/sweep.py
'''

sweep_python_lock = threading.Lock()


def ensure_sweep_python(logger: logging.Logger) -> Path:
    """The self-contained python the sweep mounts: SWEEP_PYTHON_DIR, fetched from SWEEP_PYTHON_URL
    when it is the default location and absent."""
    with sweep_python_lock:
        if (SWEEP_PYTHON_DIR / "bin" / "python3").exists():
            return SWEEP_PYTHON_DIR
        if SWEEP_PYTHON_DIR.name != "python":
            raise RuntimeError(f"Sweep python not found: {SWEEP_PYTHON_DIR} (set SV_SWEEP_PYTHON_DIR to an "
                               f"unpacked python-build-standalone 'python' directory)")
        logger.info(f"Fetching the sweep python from {SWEEP_PYTHON_URL}...")
        SWEEP_PYTHON_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            with requests.get(SWEEP_PYTHON_URL, stream=True, timeout=600) as response:
                response.raise_for_status()
                for chunk in response.iter_content(1 << 20):
                    tmp.write(chunk)
            tmp.flush()
            with tarfile.open(tmp.name, "r:gz") as tar:      # the archive's top-level dir is `python/`
                tar.extractall(SWEEP_PYTHON_DIR.parent)
        if not (SWEEP_PYTHON_DIR / "bin" / "python3").exists():
            raise RuntimeError(f"Sweep python not found after fetching: {SWEEP_PYTHON_DIR}")
        logger.info(f"Sweep python unpacked at {SWEEP_PYTHON_DIR}.")
        return SWEEP_PYTHON_DIR


def sweep_image(golden_patch: str, tracked_files: set, image_name: str, logger: logging.Logger) -> dict:
    """Walk the local image for copies of the golden code `golden_patch` produces; `tracked_files`
    is the upstream repo's tree at the base commit. Returns {touched, apply, gold, work, hits}:
    `touched` the files the patch touches on either side, `apply` how it applied (`git` / `patch`
    / `FAIL`), `hits` the copies found.

    Raises RuntimeError when the sweep itself did not conclude (the container timed out or
    produced no result) — the harness failing, not a verdict about the image."""
    python_dir = ensure_sweep_python(logger)
    # Both sides: a file the patch deletes must be in the scratch copy for the patch to apply.
    touched = sorted(touched_files(golden_patch, side="post") | touched_files(golden_patch, side="pre"))
    with tempfile.TemporaryDirectory() as input_dir:
        input_dir = Path(input_dir)
        (input_dir / "golden.patch").write_text(golden_patch)
        (input_dir / "sweep.py").write_text(SWEEP_SRC)
        (input_dir / "touched.txt").write_text("\n".join(touched) + "\n")
        (input_dir / "inputs.json").write_text(json.dumps({"touched": touched, "tracked": sorted(tracked_files)}))
        input_dir.chmod(0o755)
        deployment = Deployment.from_local(logger=logger, image_name=image_name)
        deployment.create_container(
            command=["-c", CONTAINER_SH],
            entrypoint="/bin/sh",
            volumes={str(python_dir): {"bind": SWEEP_MOUNT_DIR, "mode": "ro"},
                     str(input_dir): {"bind": SWEEP_INPUT_DIR, "mode": "ro"}},
            mem_limit=SWEEP_MEM_LIMIT,
            cpu_limit=SWEEP_CPU_LIMIT,
        )
        run_logs, timed_out = deployment.run_with_timeout(timeout=SWEEP_TIMEOUT)
    if timed_out:
        raise RuntimeError(f"Sweep timed out: {image_name}")
    lines = run_logs.splitlines()
    payload = next((l[len("__RESULT__"):] for l in lines if l.startswith("__RESULT__")), None)
    if not payload:
        raise RuntimeError(f"Sweep produced no result: {image_name}: {run_logs[-1500:]}")
    result = json.loads(payload)
    result["apply"] = next((l[len("__APPLY__"):] for l in lines if l.startswith("__APPLY__")), None)
    result["touched"] = touched
    logger.info(f"Swept {image_name}: {len(result['hits'])} hit(s), golden_patch applied via {result['apply']}.")
    return result
