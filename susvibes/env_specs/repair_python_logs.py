"""Repair known Python count specs. Defaults to a read-only preview."""
import argparse
import json
import hashlib
from pathlib import Path


NICEGUI = 'zauberzeug__nicegui_ed12eb14f2a6c48b388a05c04b3c5a107ea9d330'
PYTEST_COUNTS = {
    key: rf'^=+ [^\n]*?\b(\d+) {word}\b[^\n]* in \d+(?:\.\d+)?s(?: \([^\n]*\))? =+\s*$'
    for key, word in [('FAILED', 'failed'), ('PASSED', 'passed'),
                      ('SKIPPED', 'skipped'), ('ERROR', 'errors?'), ('XFAIL', 'xfailed')]
}


UNITTEST_COUNTS = {
    "FAILED": r"^FAILED \([^\n]*\bfailures=(\d+)[^\n]*\)",
    "ERROR": r"^FAILED \([^\n]*\berrors=(\d+)[^\n]*\)",
    "PASSED": r"^Ran ([1-9]\d*) tests? in [^\n]+\n\s*(?:OK|PASSED)(?: \([^\n]*\))?\s*$",
}
REPAIRS = {
    NICEGUI: {"count": PYTEST_COUNTS},
    "pennersr__django-allauth_39f4a4ce9c891795b00914ca5ec32de72d5369c0": {
        "count": PYTEST_COUNTS, "count_gen_sec": PYTEST_COUNTS},
    "bugsink__bugsink_1a98424a87cc95bdb9b2ee3acfc86c0ee67db139": {
        "count": UNITTEST_COUNTS, "count_gen_sec": UNITTEST_COUNTS},
    "takluyver__pyxdg_bd999c1c3fe7ee5f30ede2cf704cf03e400347b4": {
        "count": UNITTEST_COUNTS, "count_gen_sec": UNITTEST_COUNTS},
    "openstack__oslo.utils_e0425691d90bce0bbe847a9ff49468ce0fab5486": {
        "count": UNITTEST_COUNTS, "count_gen_sec": UNITTEST_COUNTS},
}


def repair(specs):
    """Repair identified count regexes; preserve checkers and benchmark thresholds."""
    changed = []
    for iid, kinds in REPAIRS.items():
        if iid not in specs:
            continue
        for kind, patterns in kinds.items():
            if kind in specs[iid] and specs[iid][kind]['logs_parser'] != patterns:
                specs[iid][kind]['logs_parser'] = dict(patterns)
                if iid not in changed:
                    changed.append(iid)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs_handler', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    original = args.logs_handler.read_bytes()
    specs = json.loads(original)
    changed = repair(specs)
    if args.apply and changed:
        backup = args.logs_handler.with_suffix('.json.before-parser-' + hashlib.sha256(original).hexdigest()[:12])
        with backup.open('xb') as output:
            output.write(original)
        pending = args.logs_handler.with_suffix('.json.tmp')
        pending.write_text(json.dumps(specs, indent=2) + '\n')
        pending.replace(args.logs_handler)
    print(json.dumps({'changed': changed, 'applied': args.apply}))


if __name__ == '__main__':
    main()
