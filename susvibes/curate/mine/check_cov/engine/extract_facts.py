# -*- coding: utf-8 -*-
"""Self-contained static parsing for the check_cov engine: the ast -> tree-sitter
-> regex ladder, test-file detection, and relative-import resolution.

ZERO susvibes dependencies and written in the py2/py3 common subset, so the whole
``engine/`` package can be COPYied into a per-version ``cov_py`` container and run
under that interpreter's NATIVE ast/jedi/parso. The parse functions are vendored
from ``mine/utils.py`` (which now re-exports them from here); the language
constants mirror ``mine/constants.py`` (keep the values in sync).

ast notes for cross-version use: ``ast.Constant`` (3.8+) unifies literal nodes,
while <=3.7 and py2 use ``ast.Str``; ``ast.AsyncFunctionDef`` is 3.5+ (absent on
py2). Both are handled so the ast layer stays precise on every interpreter — which
is the whole point of running in a native-version container.
"""
from __future__ import print_function, division, absolute_import, unicode_literals

import re
import ast
import threading

# Language / test-file classification — mirrors mine.constants (vendored so engine
# is dependency-free in the container; keep values in sync with mine/constants.py).
TARGET_EXTENSIONS = (".py",)
TEST_KEYWORDS = ["tests", "test", "testing", "testsuite", "conftest"]


def path_has_keyword(path, keywords):
    """Whether any "/"-, "_"- or "."-delimited token of the (lowercased) path is
    one of `keywords`. Token-based, so "latest.py" does not match "test"."""
    tokens = re.split(r"[/_.]", str(path).replace("\\", "/").lower())
    return any(tok in keywords for tok in tokens)


def is_test_file(path, exts=TARGET_EXTENSIONS):
    """Whether a path is a test file: its suffix is a test-language extension and
    some "/"- or "_"-delimited token of its (lowercased) path is a test keyword.

    This is the PATH-only signal (shared with mine.process). It over-matches files
    that merely have a test-ish name/dir but contain no tests (a production
    ``hit_testing_service.py``, a ``test/`` utility script): check_cov pairs it with
    ``has_test_def`` to exclude those from its coverage test set."""
    if not str(path).lower().endswith(tuple(exts)):
        return False
    return path_has_keyword(path, TEST_KEYWORDS)


# A file that is on a test-ish PATH but defines no test (no `def test_*`, no Test
# class / TestCase, no fixture) is not a test — it is production or utility code that
# happens to live under a test-ish name. Such a file referencing the target is NOT
# coverage (e.g. a production service whose name contains "testing", a `__main__`
# helper under `test/`).
_TEST_DEF = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+test"      # def test_* / def testFoo
    r"|^[ \t]*class[ \t]+Test\w"                  # class Test...
    r"|^[ \t]*class[ \t]+\w+Test(?:Case)?\b"      # class FooTest / FooTestCase
    r"|\b(?:unittest\.)?TestCase\b"               # subclass of (unittest.)TestCase
    r"|@(?:pytest\.)?fixture",                    # @fixture / @pytest.fixture (conftest)
    re.M)


def has_test_def(source):
    """Whether source actually defines a test (function/class/fixture) — the content
    check that, combined with ``is_test_file`` path, identifies a real test file."""
    return bool(source) and _TEST_DEF.search(source) is not None


HTTP_METHODS = {"get", "post", "put", "delete", "patch", "route", "options", "head"}
# Route-declaring call/decorator tails: HTTP verbs plus @expose (Flask-AppBuilder /
# flask-classful / flask-restful declare a route via @expose("/path"), not @app.route).
ROUTE_METHODS = HTTP_METHODS | {"expose"}
_ts_local = threading.local()

# ast.Constant (3.8+) unifies literals; <=3.7 / py2 use ast.Str. function/class
# def node types present in this interpreter (py2 has no AsyncFunctionDef).
HAS_CONSTANT = hasattr(ast, "Constant")
AST_STR = getattr(ast, "Str", None)
FUNC_CLASS_DEFS = tuple(
    getattr(ast, n) for n in ("FunctionDef", "AsyncFunctionDef", "ClassDef")
    if hasattr(ast, n))


def str_constant(node):
    """The str value if `node` is a string literal (ast.Constant on 3.8+, ast.Str
    on <=3.7 / py2), else None. On py2 a non-u'' literal is a bytes str; decode it
    to unicode so subsequent substring / ``in`` comparisons against the engine's
    unicode_literals strings don't raise UnicodeDecodeError on non-ascii bytes.
    (py3 ast.Str.s is always text; a py3 ``b''`` literal is ast.Bytes, not ast.Str.)"""
    if HAS_CONSTANT and isinstance(node, ast.Constant):
        v = node.value
        return v if isinstance(v, str) else None
    if AST_STR is not None and isinstance(node, AST_STR):
        s = node.s
        if isinstance(s, bytes):   # py2 bytes str -> normalize to unicode
            try:
                return s.decode("utf-8", "replace")
            except Exception:
                return None
        return s
    return None


def empty_facts():
    return {
        "import_modules": set(),   # absolute dotted modules: `import a.b`, `from a.b import c`
        "from_imports": [],        # (level:int, module:str|None, names:list[str])
        "imported_names": set(),   # names bound via `from x import name`
        "defined_symbols": set(),  # class/function names defined in the file
        "used_names": set(),       # referenced/called identifiers and attribute tails
        "strings": set(),          # string literal contents (bounded)
        "decorators": set(),       # dotted decorator names, e.g. "app.route", "pytest.fixture"
        "route_paths": set(),      # path literals from route decorators / client calls
        "route_handlers": {},      # route path -> {handler symbol names} (a self-declared
                                   # @app.route("/x") on def x): ties a route to the symbol
                                   # it dispatches to, so H5/H8 can require the matched route
                                   # to be the CHANGED one (ast layer only)
        "url_patterns": [],        # (kind, route_or_prefix, view_symbol, name): a cross-file
                                   # route table — Django path/re_path/url ("path") and
                                   # DRF router.register ("register"); name is the route's
                                   # name=/basename= (None if absent); only ast layer fills it
        "url_names": set(),        # route names a test addresses by name (reverse("ns:x") /
                                   # url_for("x")) — matched against url_patterns' name so a
                                   # named-URL test hit needs no literal path (ast layer only)
        "as_view_targets": {},     # var name -> class name for `v = Cls.as_view()` (Django
                                   # class-based-view exposure): a urlconf routing `v` reaches
                                   # Cls's http methods (get/post/...); ast layer only
        "route_prefix": None,      # Flask Blueprint url_prefix / FastAPI APIRouter prefix
        "parse_level": "none",     # ast | tree_sitter | regex | none
    }


def dotted_name(node):
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


def route_path_of(node):
    """The leading "/..." path of an HTTP route/client call (``@app.route("/x")`` /
    ``client.get("/x")``), or None — the shared path-arg rule for route_paths and
    route_handlers."""
    func = node.func
    attr = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if attr in ROUTE_METHODS and node.args:
        s = str_constant(node.args[0])
        if s is not None and s.startswith("/"):
            return s
    return None


def collect_call_path(node, facts):
    """Record a leading "/..." string arg of route/client calls (e.g. client.get("/x"))."""
    s = route_path_of(node)
    if s is not None:
        facts["route_paths"].add(s)


URL_FUNCS = ("path", "re_path", "url")


def ref_tail(node):
    """Key symbol name of a view reference in a route table:
    ``views.user_list`` -> 'user_list', ``UserView.as_view()`` -> 'UserView',
    ``user_list`` -> 'user_list'. None if it is not a name/attr/call."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        if node.attr == "as_view":
            inner = node.value
            if isinstance(inner, ast.Attribute):
                return inner.attr
            if isinstance(inner, ast.Name):
                return inner.id
            return None
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def keyword_string(node, key):
    """The string value of a keyword arg ``key=...`` on a call, or None."""
    for kw in node.keywords:
        if kw.arg == key:
            return str_constant(kw.value)
    return None


def collect_url_pattern(node, facts):
    """Record a route-table entry as (kind, route_or_prefix, view_symbol, name):
    Django ``path/re_path/url(route, view, name=)`` -> ("path", route, view, name); DRF
    ``router.register(prefix, ViewSet, basename=)`` -> ("register", prefix, ViewSet,
    basename). These link a URL to a view defined in ANOTHER file (no symbol reference);
    name carries the route's name=/basename= so a test addressing it via reverse()/
    url_for() (no literal path) can still be matched."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name in URL_FUNCS and len(node.args) >= 2:
        route = str_constant(node.args[0])
        view = ref_tail(node.args[1])
        if route is not None and view:
            facts["url_patterns"].append(("path", route, view, keyword_string(node, "name")))
    elif name == "register" and len(node.args) >= 2:
        prefix = str_constant(node.args[0])
        vs = ref_tail(node.args[1])
        if prefix is not None and vs:
            facts["url_patterns"].append(("register", prefix, vs, keyword_string(node, "basename")))


def collect_as_view_target(node, facts):
    """Record ``var = SomeClass.as_view()`` (Django class-based-view exposure) as
    var_name -> class_name, so a urlconf entry routing ``var`` resolves to the class
    whose http methods (get/post/...) handle the route."""
    if not (isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "as_view"):
        return
    cls = ref_tail(node.value)            # SomeClass.as_view() -> "SomeClass"
    if not cls:
        return
    for tgt in node.targets:
        if isinstance(tgt, ast.Name):
            facts["as_view_targets"][tgt.id] = cls


URL_NAME_FUNCS = ("reverse", "reverse_lazy", "url_for")


def collect_url_name(node, facts):
    """Record a route NAME a test addresses by name: ``reverse("ns:x")`` /
    ``url_for("x")`` -> the first string arg. Matched against url_patterns' name, so a
    named-URL test client needs no literal path."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name in URL_NAME_FUNCS and node.args:
        s = str_constant(node.args[0])
        if s is not None:
            facts["url_names"].add(s)


def collect_route_prefix(node, facts):
    """Record a Flask ``Blueprint(url_prefix=...)`` / FastAPI ``APIRouter(prefix=...)``
    so routes declared on it can be matched at their full (prefixed) path."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name in ("Blueprint", "APIRouter"):
        for kw in node.keywords:
            if kw.arg in ("url_prefix", "prefix"):
                p = str_constant(kw.value)
                if p:
                    facts["route_prefix"] = p


def facts_from_ast(source):
    tree = ast.parse(source)
    facts = empty_facts()
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
                        facts["import_modules"].add("{0}.{1}".format(mod, n))
            facts["from_imports"].append((level, mod, names))
            for n in names:
                if n != "*":
                    facts["imported_names"].add(n)
        elif isinstance(node, FUNC_CLASS_DEFS):
            facts["defined_symbols"].add(node.name)
            for dec in getattr(node, "decorator_list", []):
                dotted = dotted_name(dec)
                if dotted:
                    facts["decorators"].add(dotted)
                if isinstance(dec, ast.Call):
                    collect_call_path(dec, facts)
                    rp = route_path_of(dec)
                    if rp is not None:
                        facts["route_handlers"].setdefault(rp, set()).add(node.name)
        elif isinstance(node, ast.Name):
            facts["used_names"].add(node.id)
        elif isinstance(node, ast.Attribute):
            facts["used_names"].add(node.attr)
        elif isinstance(node, ast.Call):
            collect_call_path(node, facts)
            collect_url_pattern(node, facts)
            collect_url_name(node, facts)
            collect_route_prefix(node, facts)
        elif isinstance(node, ast.Assign):
            collect_as_view_target(node, facts)
        else:
            s = str_constant(node)
            if s is not None and 0 < len(s) <= 200:
                facts["strings"].add(s)
    return facts


def get_ts_parser():
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


def ts_dotted_name(node, txt):
    """Tree-sitter counterpart of ``dotted_name``: the dotted-name string of ``node``."""
    if node is None:
        return None
    m = re.match(r'[\w\.]+', txt(node))
    return m.group(0) if m else None


def facts_from_tree_sitter(source):
    parser = get_ts_parser()
    if parser is None:
        raise RuntimeError("tree-sitter unavailable")
    data = source.encode("utf-8", "replace")
    tree = parser.parse(data)
    facts = empty_facts()
    facts["parse_level"] = "tree_sitter"

    def txt(node):
        return data[node.start_byte:node.end_byte].decode("utf-8", "replace")

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        t = node.type
        if t == "import_statement":
            for child in node.named_children:
                name = ts_dotted_name(child, txt)
                if name:
                    facts["import_modules"].add(name)
        elif t == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            mod = ts_dotted_name(mod_node, txt) if mod_node else None
            raw = txt(node)
            m = re.match(r'from\s+(\.+)', raw)
            level = len(m.group(1)) if m else 0
            names = []
            for child in node.named_children:
                if child is mod_node:
                    continue
                nm = ts_dotted_name(child, txt)
                if nm and nm != "*":
                    names.append(nm.split(".")[-1])
            if mod and level == 0:
                facts["import_modules"].add(mod)
                for nm in names:
                    facts["import_modules"].add("{0}.{1}".format(mod, nm))
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


def facts_from_regex(source):
    facts = empty_facts()
    facts["parse_level"] = "regex"
    for m in re.finditer(r'^[ \t]*import\s+([\w\., \t]+)', source, re.M):
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
                facts["import_modules"].add("{0}.{1}".format(mod, nm))
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


def extract_module_facts(source):
    """Reduce Python source to static facts via the ast -> tree-sitter -> regex ladder."""
    if not source:
        return empty_facts()
    try:
        return facts_from_ast(source)
    except Exception:
        pass
    try:
        return facts_from_tree_sitter(source)
    except Exception:
        pass
    return facts_from_regex(source)


def resolve_relative_import(file_module, level, module):
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
