#!/usr/bin/env python3
"""Lint: every production text I/O site names its encoding.

Python decodes text with the ambient locale unless told otherwise (cp1252 on
Windows, ASCII under LC_ALL=C). The toolkit reads UTF-8 payloads, transcripts
and markdown, so a call site that leaves the encoding implicit misdecodes or
raises on a non-ASCII value. This test scans every production Python file with
the AST and fails on four classes of site:

  open        text-mode open / io.open / os.fdopen / tempfile / read_text /
              write_text / Path.open with no encoding
  stdin       sys.stdin used as a text stream (read sys.stdin.buffer instead)
  subprocess  a call with text=/universal_newlines= truthy and no encoding
  stdout      a module-level `__main__` block that does not open with the UTF-8
              stdout/stderr loop, or a tracked executable with no `__main__` block

Suppress one finding with a reason on its line: `# encoding-lint: ok <reason>`.
A bare marker does not suppress.

Run: cd lib && python3 tests/test_text_encoding_lint.py
Report mode (prints every finding plus a summary line):
     cd lib && python3 tests/test_text_encoding_lint.py --report

Code inside string literals (generated scripts) is invisible to this lint.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLASSES = ("open", "stdin", "subprocess", "stdout")
SUPPRESS_RE = re.compile(r"# encoding-lint: ok\s+\S")
TEMPFILE_CALLS = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
STDIN_BINARY_ATTRS = {"buffer", "isatty", "fileno"}
# Modules whose `.open` is not `Path.open`: anything else named `.open` is.
NON_PATH_OPEN_MODULES = {
    "os", "io", "tarfile", "zipfile", "gzip", "bz2", "lzma", "codecs",
    "webbrowser", "shelve", "dbm", "tempfile",
}


class ScanError(Exception):
    pass


def production_files(root):
    """Tracked plus untracked-but-not-ignored .py / python-shebang files, minus tests."""
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=True,
    ).stdout
    keep = []
    for path in out.split("\0"):
        if not path or "/tests/" in "/" + path:
            continue
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            continue
        if not path.endswith(".py"):
            with open(full, "rb") as f:
                first = f.readline()
            if not (first.startswith(b"#!") and b"python" in first):
                continue
        keep.append(path)
    return sorted(keep)


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kw(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _mode_node(call, positional_index):
    kw = _kw(call, "mode")
    if kw is not None:
        return kw.value
    if len(call.args) > positional_index:
        return call.args[positional_index]
    return None


def _is_binary_mode(node):
    value = _const_str(node)
    return value is not None and "b" in value


def _is_name(node, name):
    return isinstance(node, ast.Name) and node.id == name


def _attr_of(node, module, names):
    return (
        isinstance(node, ast.Attribute)
        and node.attr in names
        and _is_name(node.value, module)
    )


def _open_finding(call):
    has_encoding = _kw(call, "encoding") is not None
    func = call.func
    if _is_name(func, "open") or _attr_of(func, "io", {"open"}) or _attr_of(func, "os", {"fdopen"}):
        if has_encoding:
            return False
        mode = _mode_node(call, 1)
        return not (mode is not None and _is_binary_mode(mode))
    if _attr_of(func, "tempfile", TEMPFILE_CALLS):
        if has_encoding:
            return False
        mode = _mode_node(call, 0)
        if mode is None:
            return False  # binary default
        return not _is_binary_mode(mode)
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        return not has_encoding
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and not (isinstance(func.value, ast.Name) and func.value.id in NON_PATH_OPEN_MODULES)
    ):
        # Path.open: mode is the first positional argument.
        if has_encoding:
            return False
        mode = _mode_node(call, 0)
        return not (mode is not None and _is_binary_mode(mode))
    return False


def _subprocess_finding(call):
    if _kw(call, "encoding") is not None:
        return False
    for name in ("text", "universal_newlines"):
        kw = _kw(call, name)
        if kw is not None and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
            return True
    return False


def _is_main_guard(node):
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and _is_name(node.test.left, "__name__")
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and _const_str(node.test.comparators[0]) == "__main__"
    )


def _is_sys_stream(node, attr):
    return isinstance(node, ast.Attribute) and node.attr == attr and _is_name(node.value, "sys")


def _opens_with_stdio_loop(if_node):
    """True iff the block's first statement is the UTF-8 stdout/stderr loop.

    A structural match, not a subtree search, so a conditional or unreachable
    reconfigure call does not count.

    `errors=<stream>.errors` is required, not optional: `reconfigure(encoding=)`
    alone resets the handler to "strict", so stderr loses "backslashreplace" and
    a lone surrogate in a payload makes a blocking hook raise while writing its
    refusal -- exit 1, which Claude Code treats as non-blocking (fail open).
    """
    loop = if_node.body[0]
    if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
        return False
    stream = loop.target.id
    if not (isinstance(loop.iter, ast.Tuple) and len(loop.iter.elts) == 2):
        return False
    first, second = loop.iter.elts
    if not (
        (_is_sys_stream(first, "stdout") and _is_sys_stream(second, "stderr"))
        or (_is_sys_stream(first, "stderr") and _is_sys_stream(second, "stdout"))
    ):
        return False
    if len(loop.body) != 1 or not isinstance(loop.body[0], ast.Try):
        return False
    attempt = loop.body[0].body
    if len(attempt) != 1 or not isinstance(attempt[0], ast.Expr):
        return False
    call = attempt[0].value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "reconfigure"
        and _is_name(call.func.value, stream)
        and not call.args
        and len(call.keywords) == 2
    ):
        return False
    encoding = _kw(call, "encoding")
    errors = _kw(call, "errors")
    return (
        encoding is not None
        and _const_str(encoding.value) == "utf-8"
        and errors is not None
        and isinstance(errors.value, ast.Attribute)
        and errors.value.attr == "errors"
        and _is_name(errors.value.value, stream)
    )


def scan_source(source, filename="<snippet>"):
    """Return a sorted list of (class, lineno) findings for one source string."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise ScanError("%s: %s" % (filename, exc))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    lines = source.splitlines()
    found = []
    for node in tree.body:
        if _is_main_guard(node) and not _opens_with_stdio_loop(node):
            found.append(("stdout", node.lineno))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _open_finding(node):
                found.append(("open", node.lineno))
            if _subprocess_finding(node):
                found.append(("subprocess", node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr == "stdin" and _is_name(node.value, "sys"):
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.attr in STDIN_BINARY_ATTRS:
                continue
            if isinstance(parent, ast.Compare):
                continue
            found.append(("stdin", node.lineno))
    kept = []
    for cls, lineno in found:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if not SUPPRESS_RE.search(line):
            kept.append((cls, lineno))
    return sorted(kept)


def unguarded_executables(root):
    """Tracked 100755 python-shebang files (not under tests/) with no `__main__` block."""
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-s", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=True,
    ).stdout
    missing = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if meta.split()[0] != "100755" or "/tests/" in "/" + path:
            continue
        full = os.path.join(root, path)
        with open(full, "rb") as f:
            raw = f.read()
        first = raw.split(b"\n", 1)[0]
        if not (first.startswith(b"#!") and b"python" in first):
            continue
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=path)
        except SyntaxError as exc:
            raise ScanError("%s: %s" % (path, exc))
        if not any(_is_main_guard(node) for node in tree.body):
            missing.append(path)
    return sorted(missing)


def scan_tree(root):
    """Return (file_count, sorted [(class, path, lineno)])."""
    files = production_files(root)
    findings = []
    for path in files:
        with open(os.path.join(root, path), "rb") as f:
            source = f.read().decode("utf-8")
        for cls, lineno in scan_source(source, path):
            findings.append((cls, path, lineno))
    for path in unguarded_executables(root):
        findings.append(("stdout", path, 1))
    return len(files), sorted(findings)


def report(root):
    count, findings = scan_tree(root)
    for cls, path, lineno in findings:
        print("%s %s:%d" % (cls, path, lineno))
    totals = {c: sum(1 for f in findings if f[0] == c) for c in CLASSES}
    print(
        "files=%d open=%d stdin=%d subprocess=%d stdout=%d"
        % (count, totals["open"], totals["stdin"], totals["subprocess"], totals["stdout"])
    )


def flagged(snippet):
    return [cls for cls, _ in scan_source(snippet)]


class TestDetector(unittest.TestCase):
    def assertFlags(self, snippet, cls):
        self.assertEqual(flagged(snippet), [cls], snippet)

    def assertClean(self, snippet):
        self.assertEqual(flagged(snippet), [], snippet)

    def test_open(self):
        self.assertFlags("open(p)", "open")
        self.assertClean('open(p, "rb")')
        self.assertClean('open(p, "w", encoding="utf-8")')
        self.assertFlags("open(p, mode)", "open")
        self.assertFlags('os.fdopen(fd, "w")', "open")
        self.assertFlags("io.open(p)", "open")

    def test_path_open(self):
        self.assertFlags("Path(p).open()", "open")
        self.assertClean('p.open("rb")')
        self.assertClean('log_path.open("x", encoding="utf-8")')
        self.assertClean("webbrowser.open(u)")
        self.assertClean("tarfile.open(p)")
        self.assertClean("os.open(p, flags)")

    def test_tempfile(self):
        self.assertFlags('tempfile.NamedTemporaryFile("w")', "open")
        self.assertClean("tempfile.NamedTemporaryFile()")

    def test_path_text_methods(self):
        self.assertFlags("Path(p).read_text()", "open")
        self.assertClean('Path(p).read_text(encoding="utf-8")')
        self.assertFlags("Path(p).write_text(s)", "open")

    def test_stdin(self):
        self.assertFlags("json.load(sys.stdin)", "stdin")
        self.assertFlags("sys.stdin.read()", "stdin")
        self.assertClean("sys.stdin.buffer.read()")
        self.assertClean("sys.stdin.isatty()")
        self.assertClean("sys.stdin is None")
        self.assertFlags("f(sys.stdin)", "stdin")

    def test_subprocess(self):
        self.assertFlags("subprocess.run(a, text=True)", "subprocess")
        self.assertClean('run(a, text=True, encoding="utf-8")')
        self.assertClean("run(a, text=False)")
        self.assertFlags("run(a, universal_newlines=flag)", "subprocess")

    def test_marker(self):
        self.assertClean("open(p)  # encoding-lint: ok probe file, never decoded")
        self.assertFlags("open(p)  # encoding-lint: ok", "open")

    def test_stdout_loop(self):
        main = 'if __name__ == "__main__":\n'
        loop = (
            "    for _stream in (sys.stdout, sys.stderr):\n"
            "        try:\n"
            "            _stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)\n"
            "        except (AttributeError, ValueError):\n"
            "            pass\n"
        )
        body = "    main()\n"

        def variant(reconfigure, iterable="(sys.stdout, sys.stderr)", try_body=None):
            return (
                main
                + "    for _stream in %s:\n        try:\n" % iterable
                + (try_body if try_body is not None else "            %s\n" % reconfigure)
                + "        except (AttributeError, ValueError):\n            pass\n"
                + body
            )

        self.assertClean(main + loop + body)
        self.assertFlags(main + body, "stdout")
        self.assertFlags(main + body + loop, "stdout")
        self.assertFlags(variant("_stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)", "(sys.stdout,)"), "stdout")
        self.assertFlags(variant('_stream.reconfigure(errors="replace")'), "stdout")
        self.assertFlags(variant('_stream.reconfigure(encoding="utf-8")'), "stdout")
        self.assertFlags(variant('_stream.reconfigure(encoding="utf-8", errors="strict")'), "stdout")
        self.assertFlags(
            variant(None, try_body=(
                "            if _stream is sys.stdout:\n"
                "                _stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)\n"
            )),
            "stdout",
        )
        self.assertFlags(
            variant(None, try_body=(
                "            pass\n"
                "            _stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)\n"
            )),
            "stdout",
        )
        self.assertClean(variant("_stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)", "(sys.stderr, sys.stdout)"))
        self.assertClean(
            'if __name__ == "__main__":  # encoding-lint: ok keeps the console encoding\n' + body
        )
        self.assertClean("x = 1\n")

    def test_multiline_call_reports_first_line(self):
        found = scan_source("x = 1\nopen(\n    p,\n    'w',\n)\n")
        self.assertEqual(found, [("open", 2)])


class TestFileSet(unittest.TestCase):
    def test_tracked_plus_untracked_minus_ignored_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(
                    ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t",
                     "-c", "commit.gpgsign=false"] + list(args),
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            def write(rel, text):
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)

            git("init", "-q")
            write("a.py", "x = 1\n")
            write("tool", "#!/usr/bin/env python3\nx = 1\n")
            write(".gitignore", "ignored.py\n")
            git("add", ".")
            git("commit", "-q", "-m", "init")
            write("b.py", "x = 1\n")
            write("ignored.py", "x = 1\n")
            write("tests/test_c.py", "x = 1\n")
            self.assertEqual(set(production_files(tmp)), {"a.py", "tool", "b.py"})

    def test_unguarded_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(
                    ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t",
                     "-c", "commit.gpgsign=false"] + list(args),
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            def write(rel, text):
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)

            shebang = "#!/usr/bin/env python3\n"
            guarded = (
                'if __name__ == "__main__":\n'
                "    for _stream in (sys.stdout, sys.stderr):\n"
                "        try:\n"
                '            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)\n'
                "        except (AttributeError, ValueError):\n"
                "            pass\n"
                "    main()\n"
            )
            git("init", "-q")
            write("exe_bad", shebang + "x = 1\n")
            write("exe_ok", shebang + guarded)
            write("lib_noexec", shebang + "x = 1\n")
            write("tests/exe_t", shebang + "x = 1\n")
            git("add", ".")
            git("update-index", "--chmod=+x", "exe_bad", "exe_ok", "tests/exe_t")
            git("commit", "-q", "-m", "init")
            self.assertEqual(unguarded_executables(tmp), ["exe_bad"])


class TestRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.count, cls.findings = scan_tree(REPO_ROOT)
        cls.files = production_files(REPO_ROOT)

    def test_scans_the_production_tree(self):
        # Derived, never a fixed count: the tree grows row by row. Every
        # module under lib/ and one extensionless python executable (found
        # only by its shebang) must be among the files scanned.
        lib_modules = sorted(
            "lib/" + name for name in os.listdir(os.path.join(REPO_ROOT, "lib"))
            if name.endswith(".py"))
        self.assertGreater(len(lib_modules), 0)
        self.assertEqual(self.count, len(self.files))
        for path in lib_modules + ["tools/leak-gate/leak-gate"]:
            self.assertIn(path, self.files)
        self.assertEqual([p for p in self.files if "/tests/" in "/" + p], [])

    def test_scans_the_guards(self):
        self.assertIn("tools/danger-guard/claude-danger-guard", self.files)

    def _assert_none(self, cls):
        hits = ["%s:%d" % (p, n) for c, p, n in self.findings if c == cls]
        self.assertEqual(hits, [], "%d implicit-encoding %s site(s):\n%s" % (len(hits), cls, "\n".join(hits)))

    def test_no_ambient_open(self):
        self._assert_none("open")

    def test_no_ambient_stdin(self):
        self._assert_none("stdin")

    def test_no_ambient_subprocess(self):
        self._assert_none("subprocess")

    def test_no_ambient_stdout(self):
        self._assert_none("stdout")


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        try:
            report(REPO_ROOT)
        except (ScanError, subprocess.CalledProcessError, OSError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    unittest.main()
