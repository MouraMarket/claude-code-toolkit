"""Fault-injection counter for the interrupted-install sweeps.

Test-only. `install(env, home, checkout, scratch)` returns a new environment in
which every mutation a run makes under HOME or the checkout, and every
`claude` call, appends one line to FAULT_LOG as `<n>\\t<kind>\\t<detail>`.
When FAULT_AT equals `n` the whole process group is SIGKILLed right after the
event was performed.

Two producers share one counting routine (`fault_count.py`, written next to
`sitecustomize.py` in `scratch/fault-py/`):
  * Python wrappers for claude/git/ln/rm/cp/mkdir/mv in `scratch/fault-bin/`
    (run the real tool with FAULT_OFF=1, then count);
  * `sitecustomize.py`, which patches os/io/builtins in every Python child.

POSIX only: callers skip the suite on Windows.
"""

import os
import shutil
import signal
import sys

# The tools that get a wrapper on PATH. `claude` is the fixture's shim.
TOOLS = ("claude", "git", "ln", "rm", "cp", "mkdir", "mv")

# Source of fault_count.py. A raw string: no triple double quotes inside.
FAULT_COUNT_SOURCE = r'''
import os
import signal
import subprocess
import sys
import threading

import fcntl

# Captured at import, before sitecustomize patches anything, so the counter's
# own file operations never count.
_orig_open = os.open
_orig_write = os.write
_orig_read = os.read
_orig_close = os.close
_orig_lseek = os.lseek

_local = threading.local()
_roots = None

READ_ONLY = {
    "rev-parse", "status", "show", "log", "diff", "merge-base", "cat-file",
    "ls-files", "ls-remote", "for-each-ref", "describe", "rev-list",
}
# git options that take their value as the next argument.
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def active():
    return bool(os.environ.get("FAULT_LOG")) and not os.environ.get("FAULT_OFF")


def roots():
    global _roots
    if _roots is None:
        _roots = [
            os.path.realpath(os.environ[key])
            for key in ("FAULT_HOME", "FAULT_CHECKOUT")
            if os.environ.get(key)
        ]
    return _roots


def under_root(path):
    # Two readings of the path: the full realpath (what an open or chmod
    # touches) and realpath(parent) + name (what a remove, rename or symlink
    # touches, and the only form that works once the path is gone).
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return False
    absolute = os.path.abspath(text)
    candidates = (
        os.path.realpath(absolute),
        os.path.join(
            os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute)
        ),
    )
    for candidate in candidates:
        for root in roots():
            if candidate == root or candidate.startswith(root + os.sep):
                return True
    return False


def _count_lines(fd):
    _orig_lseek(fd, 0, 0)
    total = 0
    while True:
        chunk = _orig_read(fd, 65536)
        if not chunk:
            return total
        total += chunk.count(b"\n")


def _ident(call, *args):
    try:
        return str(call(*args))
    except OSError:
        return "?"


def _write_kill_report(n):
    # Only the captured functions: nothing here may count, fork or print.
    ppid = os.getppid()
    sid = _ident(os.getsid, 0)
    fields = (
        ("n", str(n)),
        ("pid", str(os.getpid())),
        ("ppid", str(ppid)),
        ("pgrp", str(os.getpgrp())),
        ("sid", sid),
        ("pgid_ppid", _ident(os.getpgid, ppid)),
        ("pgid_sid", _ident(os.getpgid, int(sid)) if sid != "?" else "?"),
    )
    line = " ".join("%s=%s" % pair for pair in fields) + "\n"
    try:
        fd = _orig_open(
            os.environ["FAULT_LOG"] + ".kill",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644,
        )
        try:
            _orig_write(fd, line.encode("utf-8", "replace"))
        finally:
            _orig_close(fd)
    except OSError:
        pass


def count(kind, detail=""):
    if not active() or getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        detail = str(detail).replace("\t", " ").replace("\n", " ")
        fd = _orig_open(
            os.environ["FAULT_LOG"], os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            n = _count_lines(fd) + 1
            line = "%d\t%s\t%s\n" % (n, kind, detail)
            _orig_write(fd, line.encode("utf-8", "replace"))
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _orig_close(fd)
    finally:
        _local.busy = False
    at = os.environ.get("FAULT_AT")
    if at and at.isdigit() and int(at) == n:
        _write_kill_report(n)
        os.killpg(os.getpgrp(), signal.SIGKILL)


def git_is_read_only(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_VALUE_OPTIONS else 1
    if i >= len(args):
        return True  # bare `git` or `git --version`
    sub, rest = args[i], args[i + 1:]
    if sub in READ_ONLY:
        return True
    if sub == "symbolic-ref":
        operands = [a for a in rest if not a.startswith("-")]
        return len(operands) == 1 and not ({"-d", "--delete", "-m"} & set(rest))
    if sub == "config":
        return any(a in ("--get", "--get-all", "-l", "--list") for a in rest)
    if sub == "remote":
        return rest[:1] == ["get-url"]
    if sub == "branch":
        return "--show-current" in rest
    if sub == "worktree":
        return rest[:1] == ["list"]
    return False


def tool_should_count(name, args):
    if name == "claude":
        return True
    if name == "git":
        return not git_is_read_only(args)
    return any(under_root(a) for a in args if not a.startswith("-"))


def run_tool(name, real, args):
    env = dict(os.environ)
    env["FAULT_OFF"] = "1"
    code = subprocess.call([real] + list(args), env=env)
    if tool_should_count(name, args):
        count(name, " ".join([name] + list(args)))
    sys.exit(code if code >= 0 else 128 - code)
'''

# Source of sitecustomize.py.
SITECUSTOMIZE_SOURCE = r'''
import os

if os.environ.get("FAULT_LOG") and not os.environ.get("FAULT_OFF"):

    def _install():
        import builtins
        import io

        import fault_count  # first: captures the unpatched os functions
        from fault_count import count, under_root

        fds = {}  # fd -> path, for write-capable os.open calls under a root

        def text(value):
            if isinstance(value, int):
                return str(fds.get(value, value))
            return str(value)

        def hit(value):
            if isinstance(value, int):
                value = fds.get(value)
                if value is None:
                    return False
            return under_root(value)

        specs = [
            ("replace", ((0, "src"), (1, "dst"))),
            ("rename", ((0, "src"), (1, "dst"))),
            ("remove", ((0, "path"),)),
            ("unlink", ((0, "path"),)),
            ("symlink", ((1, "dst"),)),  # the link path; the target is only text
            ("link", ((0, "src"), (1, "dst"))),
            ("mkdir", ((0, "path"),)),
            ("rmdir", ((0, "path"),)),
            ("chmod", ((0, "path"),)),
            ("utime", ((0, "path"),)),
            ("truncate", ((0, "path"),)),
        ]

        def make(name, slots, original):
            def wrapper(*args, **kwargs):
                result = original(*args, **kwargs)
                paths = []
                for index, keyword in slots:
                    if index < len(args):
                        paths.append(args[index])
                    elif keyword in kwargs:
                        paths.append(kwargs[keyword])
                if any(hit(p) for p in paths):
                    count(name, " -> ".join(text(p) for p in paths))
                return result

            wrapper.__name__ = name
            return wrapper

        for name, slots in specs:
            original = getattr(os, name)
            wrapper = make(name, slots, original)
            # shutil.copystat consults these sets to decide what it may call.
            for support in (
                os.supports_follow_symlinks,
                os.supports_dir_fd,
                os.supports_fd,
                os.supports_effective_ids,
            ):
                if original in support:
                    support.add(wrapper)
            setattr(os, name, wrapper)

        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
        original_os_open = os.open
        original_os_close = os.close

        def os_open(path, flags, *args, **kwargs):
            fd = original_os_open(path, flags, *args, **kwargs)
            if flags & write_flags and under_root(path):
                fds[fd] = os.fsdecode(os.fspath(path))
                count("open", fds[fd])
            return fd

        def os_close(fd):
            result = original_os_close(fd)
            fds.pop(fd, None)
            return result

        os.open = os_open
        os.close = os_close

        class Proxy(object):
            # Delegates everything; the first close counts one `close` event.
            def __init__(self, handle, path):
                self._handle = handle
                self._path = path
                self._counted = False

            def __getattr__(self, name):
                return getattr(self._handle, name)

            def __enter__(self):
                self._handle.__enter__()
                return self

            def __exit__(self, *exc_info):
                self.close()

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._handle)

            def close(self):
                self._handle.close()
                if not self._counted:
                    self._counted = True
                    count("close", self._path)

        original_open = io.open

        def open_wrapper(file, mode="r", *args, **kwargs):
            handle = original_open(file, mode, *args, **kwargs)
            if isinstance(file, int):
                # A remembered fd: os.open already counted the open.
                path = fds.pop(file, None)
                return handle if path is None else Proxy(handle, path)
            if not any(c in mode for c in "wax+"):
                return handle
            try:
                path = os.fsdecode(os.fspath(file))
            except TypeError:
                return handle
            try:
                opened = fds.pop(handle.fileno(), None)
            except (AttributeError, OSError, ValueError):
                opened = None
            if opened is not None:
                # An `opener` (tempfile's, since 3.12) went through the wrapped
                # os.open, which already counted the open.
                return Proxy(handle, opened)
            if under_root(path):
                count("open", path + " " + mode)
                return Proxy(handle, path)
            return handle

        builtins.open = open_wrapper
        io.open = open_wrapper

    _install()
'''

WRAPPER_TEMPLATE = '''#!@@EXE@@
import sys

sys.path.insert(0, @@PYDIR@@)
import fault_count

fault_count.run_tool(@@NAME@@, @@REAL@@, sys.argv[1:])
'''


def install(env, home, checkout, scratch):
    """Return a NEW env with the counter wired in; `env` is not modified."""
    new = dict(env)
    path = env.get("PATH", os.defpath)
    bin_dir = os.path.join(scratch, "fault-bin")
    py_dir = os.path.join(scratch, "fault-py")
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(py_dir, exist_ok=True)
    with open(os.path.join(py_dir, "fault_count.py"), "w") as handle:
        handle.write(FAULT_COUNT_SOURCE)
    with open(os.path.join(py_dir, "sitecustomize.py"), "w") as handle:
        handle.write(SITECUSTOMIZE_SOURCE)
    for name in TOOLS:
        real = shutil.which(name, path=path)  # before fault-bin is on PATH
        if not real:
            continue
        source = (
            WRAPPER_TEMPLATE.replace("@@EXE@@", sys.executable)
            .replace("@@PYDIR@@", repr(py_dir))
            .replace("@@NAME@@", repr(name))
            .replace("@@REAL@@", repr(real))
        )
        target = os.path.join(bin_dir, name)
        with open(target, "w") as handle:
            handle.write(source)
        os.chmod(target, 0o755)
    new["PATH"] = bin_dir + os.pathsep + path
    old_pythonpath = env.get("PYTHONPATH")
    new["PYTHONPATH"] = py_dir + (os.pathsep + old_pythonpath if old_pythonpath else "")
    new["FAULT_LOG"] = os.path.join(os.path.realpath(scratch), "fault.log")
    new["FAULT_HOME"] = os.path.realpath(home)
    new["FAULT_CHECKOUT"] = os.path.realpath(checkout)
    return new


def events(log):
    """Parse a fault log into [(n, kind, detail)]; a missing log is []."""
    try:
        with open(log, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    parsed = []
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit():
            parsed.append((int(parts[0]), parts[1], parts[2]))
    return parsed


def kill_report(log):
    """Parse `log + ".kill"` into [{key: value}], one dict per line; missing is []."""
    try:
        with open(log + ".kill", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    return [
        dict(pair.split("=", 1) for pair in line.split() if "=" in pair)
        for line in lines if line.strip()
    ]


def diagnose(returncode, reports, events_reached, k, outer_pid=None):
    """Explain a kill run that did not die by SIGKILL; "" when it did."""
    if returncode == -signal.SIGKILL:
        return ""
    # Only this kill point's line counts: a stale file left by another run
    # must not name a class for this one.
    reports = [r for r in reports if r.get("n") == str(k)]
    lines = [
        "kill run did not die by SIGKILL (returncode=%s, events reached=%s of "
        "kill point %s)" % (returncode, events_reached, k)
    ]
    if outer_pid is not None:
        lines.append("outer pid=%s" % outer_pid)
    if not reports:
        lines.append(
            "CLASS stray-kill: the emitter never fired its killpg; something "
            "outside the run killed a process in it")
    elif reports[0].get("pgrp") != reports[0].get("sid"):
        lines.append(
            "CLASS emitter-outside-group: the emitter's process group %s is not "
            "the run's session group %s, so killpg missed the outer"
            % (reports[0].get("pgrp"), reports[0].get("sid")))
    else:
        lines.append(
            "CLASS partial-group-kill: killpg(%s) fired from inside the run's "
            "group but the outer survived" % reports[0].get("pgrp"))
    lines.extend(" ".join("%s=%s" % pair for pair in report.items())
                 for report in reports)
    return "\n".join(lines)
