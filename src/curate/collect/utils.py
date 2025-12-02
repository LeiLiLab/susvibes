import re
import ast
import itertools
import difflib

def merge_file_patches(file_patches):
    """
    Merge multiple file patches into a single patch string.
    Each file patch is a dictionary with (relative_path, hunk_str) pairs.
    """
    merged_patch = []
    for path, hunk in file_patches.items():
        merged_patch.append(f"diff --git a/{path} b/{path}")
        merged_patch.append(f"--- a/{path}")
        merged_patch.append(f"+++ b/{path}")
        merged_patch.append(hunk)
    return "\n".join(merged_patch) + "\n"

def split_to_file_patches(patch: str) -> dict[str, str]:
    """
    Split a multi-file git unified diff string into {relative_path: hunk_str}.
    Raises ValueError if a path changes (rename/copy/create/delete), or headers mismatch.
    """
    lines = patch.splitlines()
    diff_re = re.compile(r'^diff --git a/(.*?) b/(.*?)\s*$')
    n, i, file_patches = len(lines), 0, {}
    while i < n and not lines[i].startswith("diff --git "):
        i += 1
    while i < n:
        m = diff_re.match(lines[i])
        if not m:
            i += 1
            continue
        a_path, b_path = m.groups()
        if a_path != b_path:
            raise ValueError(f"Path changed {a_path} -> {b_path} not allowed.")
        path = a_path
        i += 1
        while i < n and not lines[i].startswith("--- "):
            if (lines[i].startswith("rename from ") or
                lines[i].startswith("rename to ") or
                lines[i].startswith("copy from ") or
                lines[i].startswith("copy to ")):
                raise ValueError(f"Path changed via rename or copy not allowed.")
            i += 1
        if i >= n or not lines[i].startswith("--- "):
            raise ValueError(f"Missing '---' header for {path}")
        old_line = lines[i]
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise ValueError(f"Missing '+++' header for {path}")
        new_line = lines[i]
        i += 1
        old_token, new_token = old_line[4:].split("\t")[0].strip(), \
            new_line[4:].split("\t")[0].strip()
        if old_token == "/dev/null" or new_token == "/dev/null":
            raise ValueError(f"File creation or deletion not allowed.")
        if old_token != f"a/{path}" or new_token != f"b/{path}":
            msg = f"Header paths do not match diff header for {path}: {old_token} , {new_token}"
            raise ValueError(msg)
        start = i
        while i < n and not lines[i].startswith("diff --git "):
            i += 1
        hunk_str = "\n".join(lines[start:i]).rstrip("\n")
        file_patches[path] = hunk_str

    return file_patches

def func_spans(tree_src: ast.AST):
    Func = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree_src):
        if isinstance(node, Func) and isinstance(node.name, str) and node.name.startswith("test"):
            start = node.lineno
            # include decorators in the span
            if getattr(node, "decorator_list", None):
                dec_starts = [getattr(d, "lineno", start) for d in node.decorator_list]
                if dec_starts:
                    start = min(dec_starts + [start])
            end = getattr(node, "end_lineno", None) or start
            yield node.name, start, end
            
def parse_file_patch(file_patch: str, code_before: str, code_after: str) -> tuple:
    """
    Parse a unified diff patch and full old/new file contents, to compute:
      - inserted_lines:    set of line numbers in the new file that were added
      - removed_lines:  set of line numbers in the old file that were removed
      - lineno_map:  mapping of all unchanged lines new → old line no.
    """
    # 1) First, extract all hunks
    hunk_re = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    hunks = []
    lines = file_patch.splitlines()
    i = 0
    while i < len(lines):
        m = hunk_re.match(lines[i])
        if m:
            old_start = int(m.group(1))
            old_len   = int(m.group(2) or '1')
            new_start = int(m.group(3))
            new_len   = int(m.group(4) or '1')
            # collect hunk body
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith('@@ '):
                body.append(lines[i])
                i += 1
            hunks.append((old_start, old_len, new_start, new_len, body))
            continue
        i += 1

    old_lines = code_before.splitlines()
    new_lines = code_after.splitlines()
    inserted_lines: set[int] = set()
    removed_lines: set[int] = set()
    lineno_map: dict[int, int] = {}

    # 2) Walk through the file *in order*, tracking a "delta" = new_lineno - old_lineno
    delta = 0
    prev_old_end = 0

    for old_start, old_len, new_start, new_len, body in hunks:
        # 2a) Map all unchanged lines before this hunk:
        for old_ln in range(prev_old_end + 1, old_start):
            new_ln = old_ln + delta
            # Sanity check: must lie within new file bounds
            if 1 <= new_ln <= len(new_lines):
                lineno_map[new_ln] = old_ln

        # 2b) Now process this hunk’s body
        old_ln = old_start
        new_ln = new_start
        for line in body:
            if line.startswith('+') and not line.startswith('+++'):
                inserted_lines.add(new_ln)
                new_ln += 1
            elif line.startswith('-') and not line.startswith('---'):
                removed_lines.add(old_ln)
                old_ln += 1
            else:
                # context line, unchanged
                lineno_map[new_ln] = old_ln
                old_ln += 1
                new_ln += 1

        # 2c) Advance prev_old_end and update delta
        prev_old_end = old_start + old_len - 1
        delta = (new_start + new_len - 1) - prev_old_end

    # 3) Map any unchanged lines *after* the last hunk
    for old_ln in range(prev_old_end + 1, len(old_lines) + 1):
        new_ln = old_ln + delta
        if 1 <= new_ln <= len(new_lines):
            lineno_map[new_ln] = old_ln

    return inserted_lines, removed_lines, lineno_map

def mask_test_funcs(file_patch: str, code_before: str, code_after: str) -> str:
    """
    Given a unified diff patch and full old/new file contents, generate a removal mask
    that removes all test functions touched by the patch.
    """
    # 1) Fully parse the file patch
    inserted_lines, removed_lines, lineno_map = parse_file_patch(file_patch, code_before, code_after)

    # 2) AST：enumerate test functions in source codes
    try:
        tree_before = ast.parse(code_before)
        tree_after = ast.parse(code_after)
    except SyntaxError:
        raise ValueError("Invalid python syntax.")

    funcs_before: list[tuple[str, int, int]] = list(func_spans(tree_before))
    funcs_after: list[tuple[str, int, int]] = list(func_spans(tree_after))
    
    def span_touched(func: tuple, touched_lines: list) -> bool:
        _, s, e = func
        return any(s <= ln <= e for ln in touched_lines)
    
    touched_funcs_before = [func for func in funcs_before if span_touched(func, removed_lines)]
    touched_funcs_after = [func for func in funcs_after if span_touched(func, inserted_lines)]
    
    extra_funcs_before = []
    for func_after in touched_funcs_after:
        _, s, _ = func_after
        s_before = lineno_map.get(s, None)
        for func_before in funcs_before:
            if s_before == func_before[1]:
                extra_funcs_before.append(func_before)
                break
    
    spans_to_delete: list[tuple[int, int]] = [
        (s, e) for _, s, e in touched_funcs_before + extra_funcs_before
    ]

    src_lines = code_before.splitlines(keepends=True)
    keep = [True] * (len(src_lines) + 1)  # 1-based 
    for s, e in spans_to_delete:
        s = max(1, s)
        e = min(len(src_lines), e)
        for ln in range(s, e + 1):
            keep[ln] = False
    masked_code = "".join(src_lines[i - 1] for i in range(1, len(src_lines) + 1) if keep[i])

    # 3) Generate unified diff based on code_before
    diff_iter = difflib.unified_diff(
        code_before.splitlines(keepends=True),
        masked_code.splitlines(keepends=True),
        fromfile="before.py",
        tofile="after.py",
    )
    diff_no_header = ''.join(
        itertools.dropwhile(lambda s: not s.startswith('@@'), diff_iter)
    )
    return diff_no_header
