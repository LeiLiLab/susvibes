import re
import ast
import threading
import itertools
import difflib

from susvibes.curate.collect.constants import TARGET_EXTENSIONS, TEST_KEYWORDS


def path_has_keyword(path, keywords) -> bool:
    """Whether any "/"-, "_"- or "."-delimited token of the (lowercased) path is
    one of `keywords`. Token-based, so "latest.py" does not match "test"."""
    tokens = re.split(r"[/_.]", str(path).replace("\\", "/").lower())
    return any(tok in keywords for tok in tokens)


def is_test_file(path, exts=TARGET_EXTENSIONS) -> bool:
    """Whether a path is a test file: its suffix is a test-language extension and
    some "/"- or "_"-delimited token of its (lowercased) path is a test keyword."""
    if not str(path).lower().endswith(tuple(exts)):
        return False
    return path_has_keyword(path, TEST_KEYWORDS)


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


# ---------------------------------------------------------------------------
# Static module-fact extraction for file-level test-coverage analysis (check_cov).
#
# A source file is reduced to a small set of "facts" (imports, defined symbols,
# referenced names, decorators, route paths, string literals). Parsing is
# tolerant across Python 2/3 via a fallback ladder: stdlib `ast` (precise) ->
# tree-sitter-python (error-recovering; install via `pip install tree-sitter
# tree-sitter-python`) -> regex (last resort). A single unparseable file thus
# never blocks analysis of the rest of a repo.
# ---------------------------------------------------------------------------

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "route", "options", "head"}
_ts_local = threading.local()


def _empty_facts() -> dict:
    return {
        "import_modules": set(),   # absolute dotted modules: `import a.b`, `from a.b import c`
        "from_imports": [],        # (level:int, module:str|None, names:list[str])
        "imported_names": set(),   # names bound via `from x import name`
        "defined_symbols": set(),  # class/function names defined in the file
        "used_names": set(),       # referenced/called identifiers and attribute tails
        "strings": set(),          # string literal contents (bounded)
        "decorators": set(),       # dotted decorator names, e.g. "app.route", "pytest.fixture"
        "route_paths": set(),      # path literals from route decorators / client calls
        "parse_level": "none",     # ast | tree_sitter | regex | none
    }


def _dotted_name(node: ast.AST):
    """Render an ast Attribute/Name (or a Call's func) chain as a dotted string."""
    if isinstance(node, ast.Call):
        node = node.func
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts)) if parts else None


def _collect_call_path(node: ast.Call, facts: dict) -> None:
    """Record a leading "/..." string arg of route/client calls (e.g. client.get("/x"))."""
    func = node.func
    attr = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if attr in _HTTP_METHODS and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                and arg.value.startswith("/"):
            facts["route_paths"].add(arg.value)


def _facts_from_ast(source: str) -> dict:
    tree = ast.parse(source)
    facts = _empty_facts()
    facts["parse_level"] = "ast"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                facts["import_modules"].add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = node.module
            names = [a.name for a in node.names]
            if level == 0 and mod:
                facts["import_modules"].add(mod)
                for n in names:
                    if n != "*":
                        facts["import_modules"].add(f"{mod}.{n}")
            facts["from_imports"].append((level, mod, names))
            for n in names:
                if n != "*":
                    facts["imported_names"].add(n)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts["defined_symbols"].add(node.name)
            for dec in getattr(node, "decorator_list", []):
                dotted = _dotted_name(dec)
                if dotted:
                    facts["decorators"].add(dotted)
                if isinstance(dec, ast.Call):
                    _collect_call_path(dec, facts)
        elif isinstance(node, ast.Name):
            facts["used_names"].add(node.id)
        elif isinstance(node, ast.Attribute):
            facts["used_names"].add(node.attr)
        elif isinstance(node, ast.Call):
            _collect_call_path(node, facts)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if 0 < len(node.value) <= 200:
                facts["strings"].add(node.value)
    return facts


def _get_ts_parser():
    """Return a thread-local tree-sitter Python parser, or None if unavailable."""
    parser = getattr(_ts_local, "parser", None)
    if parser is not None:
        return parser or None  # `False` (cached negative) -> None
    try:
        import tree_sitter_python
        from tree_sitter import Language, Parser
        parser = Parser(Language(tree_sitter_python.language()))
    except Exception:
        parser = False
    _ts_local.parser = parser
    return parser or None


def _ts_dotted(node, txt):
    if node is None:
        return None
    m = re.match(r'[\w\.]+', txt(node))
    return m.group(0) if m else None


def _facts_from_tree_sitter(source: str) -> dict:
    parser = _get_ts_parser()
    if parser is None:
        raise RuntimeError("tree-sitter unavailable")
    data = source.encode("utf-8", "replace")
    tree = parser.parse(data)
    facts = _empty_facts()
    facts["parse_level"] = "tree_sitter"

    def txt(node):
        return data[node.start_byte:node.end_byte].decode("utf-8", "replace")

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        t = node.type
        if t == "import_statement":
            for child in node.named_children:
                name = _ts_dotted(child, txt)
                if name:
                    facts["import_modules"].add(name)
        elif t == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            mod = _ts_dotted(mod_node, txt) if mod_node else None
            raw = txt(node)
            m = re.match(r'from\s+(\.+)', raw)
            level = len(m.group(1)) if m else 0
            names = []
            for child in node.named_children:
                if child is mod_node:
                    continue
                nm = _ts_dotted(child, txt)
                if nm and nm != "*":
                    names.append(nm.split(".")[-1])
            if mod and level == 0:
                facts["import_modules"].add(mod)
                for nm in names:
                    facts["import_modules"].add(f"{mod}.{nm}")
            facts["from_imports"].append((level, mod, names))
            for nm in names:
                facts["imported_names"].add(nm)
        elif t in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                facts["defined_symbols"].add(txt(name_node))
        elif t == "decorator":
            d = txt(node).lstrip("@").strip()
            m = re.match(r'[\w\.]+', d)
            if m:
                facts["decorators"].add(m.group(0))
            for p in re.findall(r'["\'](/[^"\']*)["\']', d):
                facts["route_paths"].add(p)
        elif t == "identifier":
            facts["used_names"].add(txt(node))
        elif t == "string":
            inner = re.sub(
                r'^[rbfuRBFU]*("""|\'\'\'|"|\')|("""|\'\'\'|"|\')$', '', txt(node).strip())
            if 0 < len(inner) <= 200:
                facts["strings"].add(inner)
        stack.extend(node.named_children)
    return facts


def _facts_from_regex(source: str) -> dict:
    facts = _empty_facts()
    facts["parse_level"] = "regex"
    for m in re.finditer(r'^[ \t]*import\s+([\w\.,\s]+)', source, re.M):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if re.match(r'^[\w\.]+$', name):
                facts["import_modules"].add(name)
    for m in re.finditer(r'^[ \t]*from\s+(\.*)([\w\.]*)\s+import\s+(.+)$', source, re.M):
        level = len(m.group(1))
        mod = m.group(2) or None
        names = []
        for part in m.group(3).split(","):
            nm = part.strip().split(" as ")[0].strip().strip("()").strip()
            if nm and nm != "*" and re.match(r'^\w+$', nm):
                names.append(nm)
        if mod and level == 0:
            facts["import_modules"].add(mod)
            for nm in names:
                facts["import_modules"].add(f"{mod}.{nm}")
        facts["from_imports"].append((level, mod, names))
        for nm in names:
            facts["imported_names"].add(nm)
    for m in re.finditer(r'^[ \t]*(?:async\s+)?def\s+(\w+)', source, re.M):
        facts["defined_symbols"].add(m.group(1))
    for m in re.finditer(r'^[ \t]*class\s+(\w+)', source, re.M):
        facts["defined_symbols"].add(m.group(1))
    for m in re.finditer(r'@([\w\.]+)', source):
        facts["decorators"].add(m.group(1))
    for m in re.finditer(r'\b([A-Za-z_]\w*)\s*\(', source):
        facts["used_names"].add(m.group(1))
    for m in re.finditer(r'["\'](/[\w\-/\{\}:.]*)["\']', source):
        facts["route_paths"].add(m.group(1))
    for m in re.finditer(r'["\']([\w\.\-/]{1,200})["\']', source):
        facts["strings"].add(m.group(1))
    return facts


def extract_module_facts(source: str) -> dict:
    """Reduce Python source to static facts via the ast -> tree-sitter -> regex ladder."""
    if not source:
        return _empty_facts()
    try:
        return _facts_from_ast(source)
    except Exception:
        pass
    try:
        return _facts_from_tree_sitter(source)
    except Exception:
        pass
    return _facts_from_regex(source)


def resolve_relative_import(file_module: str, level: int, module: str | None) -> str:
    """Resolve a (possibly relative) `from` import to an absolute package/module.

    `file_module` is the dotted module of the importing file. For an absolute
    import (level 0) the module is returned unchanged. For a relative import the
    leading-dot `level` walks up from the importing file's package.
    """
    if not level:
        return module or ""
    parts = file_module.split(".")
    base = parts[:-level] if len(parts) >= level else []
    if module:
        return ".".join(base + module.split("."))
    return ".".join(base)
