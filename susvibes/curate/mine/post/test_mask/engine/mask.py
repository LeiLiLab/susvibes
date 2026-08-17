# -*- coding: utf-8 -*-
"""Turn a test file's diff into a REMOVAL MASK: the patch that deletes every test function the fix
commit touched, so the task can ship a tree with those tests gone.

Version-sensitive only in `func_spans`: a test file written for Python 2 is unparseable by a Python
3 host, so the spans come from parso driven with the instance's own interpreter version. Everything
else here is plain text work.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import re
import difflib
import itertools

import parso

HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_file_patch(file_patch, code_before, code_after):
    """Line numbers a patch touches: (inserted in the new file, removed from the old, and a
    new -> old map for every unchanged line)."""
    hunks = []
    lines = file_patch.splitlines()
    i = 0
    while i < len(lines):
        m = HUNK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        old_start, old_len = int(m.group(1)), int(m.group(2) or '1')
        new_start, new_len = int(m.group(3)), int(m.group(4) or '1')
        body = []
        i += 1
        while i < len(lines) and not lines[i].startswith('@@ '):
            body.append(lines[i])
            i += 1
        hunks.append((old_start, old_len, new_start, new_len, body))

    old_lines, new_lines = code_before.splitlines(), code_after.splitlines()
    inserted_lines, removed_lines, lineno_map = set(), set(), {}
    delta, prev_old_end = 0, 0
    for old_start, old_len, new_start, new_len, body in hunks:
        for old_ln in range(prev_old_end + 1, old_start):
            new_ln = old_ln + delta
            if 1 <= new_ln <= len(new_lines):
                lineno_map[new_ln] = old_ln
        old_ln, new_ln = old_start, new_start
        for line in body:
            if line.startswith('+') and not line.startswith('+++'):
                inserted_lines.add(new_ln)
                new_ln += 1
            elif line.startswith('-') and not line.startswith('---'):
                removed_lines.add(old_ln)
                old_ln += 1
            else:
                lineno_map[new_ln] = old_ln
                old_ln += 1
                new_ln += 1
        prev_old_end = old_start + old_len - 1
        delta = (new_start + new_len - 1) - prev_old_end
    for old_ln in range(prev_old_end + 1, len(old_lines) + 1):
        new_ln = old_ln + delta
        if 1 <= new_ln <= len(new_lines):
            lineno_map[new_ln] = old_ln
    return inserted_lines, removed_lines, lineno_map


def func_spans(source, version):
    """(name, first_line, last_line) for every test function, 1-based and inclusive.

    Three parso details this depends on: `iter_funcdefs` walks only the top level while test
    methods live inside classes, so the walk is manual; `start_pos` skips decorators and only the
    `decorated` parent covers them; `end_pos` is the line AFTER the body. Raises ValueError when the
    grammar cannot parse the source at all."""
    try:
        tree = parso.load_grammar(version=version).parse(source, error_recovery=False)
    except Exception as e:
        raise ValueError("Unparseable as Python {0}: {1}".format(version, e))
    spans, stack = [], [tree]
    while stack:
        node = stack.pop()
        stack.extend(getattr(node, "children", []))
        if node.type == "funcdef" and node.name.value.startswith("test"):
            parent = node.parent
            start = parent.start_pos[0] if parent.type == "decorated" else node.start_pos[0]
            spans.append((node.name.value, start, node.end_pos[0] - 1))
    return spans


def mask_test_funcs(file_patch, code_before, code_after, version):
    """The diff that removes, from `code_before`, every test function the patch touched."""
    inserted_lines, removed_lines, lineno_map = parse_file_patch(file_patch, code_before, code_after)
    funcs_before = func_spans(code_before, version)
    funcs_after = func_spans(code_after, version)

    def span_touched(func, touched_lines):
        return any(func[1] <= ln <= func[2] for ln in touched_lines)

    touched_before = [f for f in funcs_before if span_touched(f, removed_lines)]
    touched_after = [f for f in funcs_after if span_touched(f, inserted_lines)]
    # A function only ADDED to by the patch has no removed line, so find its pre-image by line map.
    extra_before = []
    for func_after in touched_after:
        start_before = lineno_map.get(func_after[1])
        for func_before in funcs_before:
            if start_before == func_before[1]:
                extra_before.append(func_before)
                break

    src_lines = code_before.splitlines(True)
    keep = [True] * (len(src_lines) + 1)                    # 1-based
    for _, start, end in touched_before + extra_before:
        for ln in range(max(1, start), min(len(src_lines), end) + 1):
            keep[ln] = False
    masked = "".join(src_lines[i - 1] for i in range(1, len(src_lines) + 1) if keep[i])

    diff = difflib.unified_diff(src_lines, masked.splitlines(True),
                                fromfile="before.py", tofile="after.py")
    return "".join(itertools.dropwhile(lambda line: not line.startswith('@@'), diff))
