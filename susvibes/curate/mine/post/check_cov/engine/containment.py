# -*- coding: utf-8 -*-
"""Containment seeding: which symbols of a target file the security_patch touches.

The symbol trace is seeded NOT from every symbol in a patched file but only from the
symbols that CONTAIN a security_patch changed line — the method/class/function/global
the change lives in (``symbol_trace.enclosing_symbol``, the same walk-up the trace's
successors use). Deleted lines map into the pre (rollback) tree; added lines map into
the post (base) tree, keeping only names that also exist in the pre tree (a symbol the
fix only ADDS has no counterpart in the vulnerable code, so it seeds nothing).

The post version of each touched file is derived IN-ENGINE by forward-applying the
security_patch to its pre (rollback) source (``apply_hunks`` — a pure-Python analog of
``git apply`` with no git, no subprocess). So the post tree is parsed by the container's
version-matched parso, stable across py2.7-3.12, and the host ships only the patch text
(no git-show post sources).
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import re

import parso

from .symbol_trace import (
    enclosing_symbol,
    seed_symbol_positions,
    assignment_lhs_names,
    definition_scope,
)

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def patch_path(raw):
    """The repo-relative path of a diff ``--- a/x`` / ``+++ b/x`` side, or None for
    ``/dev/null`` (a file added/deleted on that side)."""
    p = raw.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(patch):
    """``[(pre_path, post_path, hunks)]`` per file in a unified diff. A hunk is
    ``(old_start, old_count, new_start, [(sign, text), ...])`` with sign in
    ``' ' / '-' / '+'`` — enough to BOTH locate changed lines and forward-apply."""
    files, pre, post, hunks, hunk = [], None, None, None, None
    for line in patch.splitlines():
        if line.startswith("--- "):
            if hunks is not None:
                if hunk:
                    hunks.append(hunk)
                files.append((pre, post, hunks))
            pre, post, hunks, hunk = patch_path(line[4:]), None, [], None
            continue
        if hunks is None:
            continue                       # preamble before the first file header
        if line.startswith("+++ "):
            post = patch_path(line[4:])
            continue
        m = HUNK_RE.match(line)
        if m:
            if hunk:
                hunks.append(hunk)
            hunk = (int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), [])
            continue
        if hunk is None:
            continue
        sign = line[:1]
        if sign in ("+", "-", " "):
            hunk[3].append((sign, line[1:]))
        # other lines ("\ No newline at end of file", etc.) carry no content -> skip
    if hunks is not None:
        if hunk:
            hunks.append(hunk)
        files.append((pre, post, hunks))
    return files


def apply_hunks(pre_text, hunks):
    """Forward-apply ``hunks`` to ``pre_text`` and return the post text (the +++ side) —
    the pure-text analog of ``git apply`` (cf. ``common.apply_patch``): copy unchanged
    runs, drop '-' lines, insert
    '+' lines. A ``-X,0`` hunk inserts AFTER pre line X; otherwise the first changed line
    is 1-indexed ``old_start``."""
    pre = pre_text.split("\n")
    out, cur = [], 0
    for old_start, old_count, _new_start, lines in hunks:
        start = (old_start - 1) if old_count > 0 else old_start
        out.extend(pre[cur:start])
        cur = start
        for sign, text in lines:
            if sign == " ":
                out.append(text)
                cur += 1
            elif sign == "-":
                cur += 1
            else:                          # "+"
                out.append(text)
    out.extend(pre[cur:])
    return "\n".join(out)


def changed_line_nums(hunks):
    """``(deleted old-line nos, added new-line nos)`` across a file's hunks — deleted
    lines index into the pre tree, added lines into the post tree."""
    dels, adds = set(), set()
    for old_start, _old_count, new_start, lines in hunks:
        old, new = old_start, new_start
        for sign, _text in lines:
            if sign == " ":
                old += 1
                new += 1
            elif sign == "-":
                dels.add(old)
                old += 1
            else:                          # "+"
                adds.add(new)
                new += 1
    return dels, adds


def parse_tree(src):
    try:
        return parso.parse(src) if src is not None else None
    except Exception:
        return None


def name_at(tree, pos):
    try:
        return tree.get_leaf_for_position(pos).value
    except Exception:
        return None


def name_set(tree):
    """Names of every referenceable symbol the trace would seed in this tree."""
    return set(n for pos in seed_symbol_positions(tree) for n in [name_at(tree, pos)] if n)


def leaf_on_line(tree, lineno):
    """First non-trivial leaf whose start line == lineno (skip indent/newline)."""
    if tree is None:
        return None
    leaf = tree.get_first_leaf()
    while leaf is not None:
        if leaf.start_pos[0] == lineno and leaf.value.strip():
            return leaf
        if leaf.start_pos[0] > lineno:
            return None
        leaf = leaf.get_next_leaf()
    return None


def module_global_defined_at(tree, leaf):
    """If ``leaf`` sits on the LHS / definition line of a module-level assignment, the
    global name(s) it defines. ``enclosing_symbol`` is a walk-up from a USE: it treats an
    assignment-target name as "not a use" and yields nothing, so a change landing directly
    on a module global's own definition line (``app = typer.Typer(...)``) would seed
    nothing — this recovers that global (it IS seeded by ``seed_symbol_positions``)."""
    node = leaf.parent
    while node is not None and node.type != "module":
        if node.type == "expr_stmt" and definition_scope(node) == "module":
            return set(n for p in assignment_lhs_names(node) for n in [name_at(tree, p)] if n)
        node = node.parent
    return set()


def enclosing_names(tree, lineno):
    """Names of the symbol(s) CONTAINING the change on ``lineno`` (mostly the same walk-up
    the trace's successors use, plus the module-global definition-line case the walk-up
    skips — see ``module_global_defined_at``)."""
    leaf = leaf_on_line(tree, lineno)
    if leaf is None:
        return set()
    positions, _module_level = enclosing_symbol(leaf)
    names = set(n for p in positions for n in [name_at(tree, p)] if n)
    return names or module_global_defined_at(tree, leaf)


def seed_names_for_targets(security_patch, pre_sources):
    """``{pre_path: set(seed symbol names)}`` — the containing symbols of the patch's
    changed lines. ``pre_sources`` is the rollback working tree (rel -> source); the post
    version of each file is derived in-engine by ``apply_hunks`` (no host post sources)."""
    res = {}
    for pre_path, _post_path, hunks in parse_patch(security_patch):
        if pre_path is None:               # a file the fix only ADDS — absent in rollback
            continue
        pre_text = pre_sources.get(pre_path)
        pre_tree = parse_tree(pre_text)
        if pre_tree is None:
            continue
        dels, adds = changed_line_nums(hunks)
        pre_names = name_set(pre_tree)
        names = set()
        for ln in dels:
            names |= enclosing_names(pre_tree, ln)
        if adds:
            post_tree = parse_tree(apply_hunks(pre_text, hunks))
            if post_tree is not None:
                for ln in adds:
                    names |= set(n for n in enclosing_names(post_tree, ln) if n in pre_names)
        res[pre_path] = names
    return res
