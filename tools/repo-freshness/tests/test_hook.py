#!/usr/bin/env python3
"""Tests for `claude-repo-freshness`, the executable hook.

Every sibling tool in this repo drives its hook through `subprocess` — see
CLAUDE.md, "Test the executables, not just the lib." This file had zero lines
of `claude-repo-freshness` exercised before it existed: `test_sync_lib.py` and
`test_freshness_lib.py` are thorough about the two libraries the hook glues
together, but neither one ever runs the hook itself, which is exactly the gap
that let `usage-watch` ship 93 green tests and zero coverage of its own
wiring.

## Why every test builds a disposable COPY of the tool, never runs it in place

`freshness_lib.repo_root()` self-locates "the toolkit" by walking up from
`os.path.realpath(__file__)` until it finds a `.git` — there is no seam to
point it elsewhere. Driving the real `tools/repo-freshness/claude-repo-freshness`
in place would therefore make the freshness-report half operate on THIS
machine's real toolkit checkout: reading its real branch and behind-count, and
— because a repo that has "never fetched" is always due — spawning a REAL,
unbounded `git fetch` against it. `Fixture` copies the three files under test
into a throwaway git repo with no `origin` remote at all, so `repo_root()`
walks up to a fixture instead, and the report phase's own background fetch
fails immediately and locally (no `origin` configured, no network attempted)
rather than needing to be separately suppressed.

## Hermeticity

- `$HOME` is always a sandbox directory, never the real one.
- `CLAUDE_TOOLKIT_CONFIG` always points at a fixture file, never the real
  per-machine config — which is also what makes `sync_lib`'s
  `SYNC_TEST_GIT_BIN`/`SYNC_TEST_GH_BIN` seams live at all: both are honoured
  only when `CLAUDE_TOOLKIT_CONFIG` is redirected AND the override is an
  absolute path (`sync_lib._test_bin_override`).
- Every test that does not deliberately configure a `gh_owner` runs with an
  ABORTING `gh` on both `PATH` and the `SYNC_TEST_GH_BIN` seam — logging its
  own invocation before refusing, so a call that reaches it is provable rather
  than merely a return value a wrongly-wired seam could satisfy for free. A
  positive control proves the harness itself is not simply never being
  reached (see `TestNoGhInvocation`).
- `git` is never blocked: the freshness-report half makes real, LOCAL git
  calls against fixture repos as part of its ordinary operation, and blocking
  it would make every test fail for the wrong reason. What must never happen
  is a NETWORK call, and no fixture repo here is ever given a remote that
  resolves anywhere but another directory on local disk.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_SRC_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
LIB_SRC = os.path.join(REPO_ROOT, "lib", "toolkit_config.py")
INSTALL_RECORD_SRC = os.path.join(REPO_ROOT, "lib", "install_record.py")

_TOOL_FILES = ("claude-repo-freshness", "freshness_lib.py", "sync_lib.py")

# In-process access to the hook's own two dependencies, and to the hook
# module itself -- for criteria 1-4 and 7, which drive `main()` directly
# rather than through `subprocess` (see CLAUDE.md, "Test the executables,
# not just the lib", for why the executable-level tests below still exist
# alongside these). `sys.path` gets TOOL_SRC_DIR so `import freshness_lib`/
# `import sync_lib` here resolve the SAME module objects `main()` binds to
# when it does its own `import freshness_lib as freshness` / `import
# sync_lib` -- Python caches modules by name in `sys.modules`, so patching
# an attribute on the objects imported here is visible inside `main()` too.
# `claude-repo-freshness` has no `.py` extension, hence `SourceFileLoader`
# rather than a plain `import` -- same idiom as
# `tools/session-titler/tests/test_hook.py`.
sys.path.insert(0, TOOL_SRC_DIR)
import freshness_lib  # noqa: E402
import sync_lib  # noqa: E402

_HOOK_PATH = os.path.join(TOOL_SRC_DIR, "claude-repo-freshness")
_hook_spec = importlib.util.spec_from_loader(
    "claude_repo_freshness_hook",
    importlib.machinery.SourceFileLoader("claude_repo_freshness_hook", _HOOK_PATH))
hook = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(hook)


def git(cwd, *args):
    """A hermetic git call for building fixtures — never the machine's own
    global config, never a stray identity. Mirrors `base-pin`'s `git()`."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    })
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)
    return subprocess.run(
        ("git", "-C", cwd) + args, check=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def commit(repo, name, body=None):
    with open(os.path.join(repo, name), "w") as handle:
        handle.write(body if body is not None else name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "commit %s" % name)


def _rev_parse_head(repo):
    return git(repo, "rev-parse", "HEAD")


def _write_gh_shim(tmp_dir, log_path, *, names=None, list_exit=0, clone_exit=0):
    """A fake `gh`: logs argv, then answers just enough of `repo list` /
    `repo clone` for `discover_clones()` to be exercised with no real `gh`
    and no network call. Same shape as `test_sync_lib.py`'s `_write_gh_shim`.
    """
    names = names or []
    path = os.path.join(tmp_dir, "gh-shim")
    lines = [
        "#!/bin/bash",
        "printf '%s\\n' \"$*\" >> {0}".format(shlex.quote(log_path)),
        'if [ "$1" = "repo" ] && [ "$2" = "list" ]; then',
    ]
    for name in names:
        lines.append("  echo {0}".format(shlex.quote(name)))
    lines.append("  exit {0}".format(list_exit))
    lines.append('elif [ "$1" = "repo" ] && [ "$2" = "clone" ]; then')
    lines.append('  dest="$4"')
    lines.append("  if [ {0} -eq 0 ]; then".format(clone_exit))
    lines.append('    mkdir -p "$dest/.git"')
    lines.append("  fi")
    lines.append("  exit {0}".format(clone_exit))
    lines.append("fi")
    lines.append("exit 0")
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


def _write_abort_shim(dir_path, name, log_path):
    """An executable named `name` that logs its own invocation and then
    REFUSES rather than doing anything real — used to prove a code path makes
    zero calls to the binary it stands in for. Written under a caller-chosen
    `name` so the same helper can produce a `PATH`-resolved `gh` AND an
    absolute-path seam target from one function.
    """
    path = os.path.join(dir_path, name)
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n")
        handle.write("printf '%s\\n' \"$*\" >> {0}\n".format(shlex.quote(log_path)))
        handle.write("exit 1\n")
    os.chmod(path, 0o755)
    return path


def _write_slow_git_shim(dir_path, delay_seconds):
    """A `git` that sleeps `delay_seconds`, then execs the real one: a loaded
    machine, made deterministic. Handed in through `SYNC_TEST_GIT_BIN`, so it
    must sit in a temp directory, and it names the real `git` absolutely
    because the hook hardens `PATH`.
    """
    real_git = shutil.which("git")
    path = os.path.join(dir_path, "git-slow-shim")
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n")
        handle.write("sleep {0}\n".format(delay_seconds))
        handle.write("exec {0} \"$@\"\n".format(shlex.quote(real_git)))
    os.chmod(path, 0o755)
    return path


def _read_shim_log(path):
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [line.strip() for line in handle if line.strip()]


class Fixture:
    """A disposable copy of the hook's own three files, plus a sandbox `$HOME`
    and config. See the module docstring for why the copy is mandatory rather
    than a convenience.
    """

    def __init__(self, mutated_sync_lib_source=None,
                 mutated_freshness_lib_source=None):
        """`mutated_sync_lib_source`, if given, REPLACES the copied
        `sync_lib.py`'s content — used exactly once, by
        `TestStampWrittenBeforeThePhase`, to fault-inject a raise into a
        throwaway copy of the dependency without ever touching the real,
        shipped `sync_lib.py` that every other test in this suite (and
        `test_sync_lib.py`) exercises unmodified.

        `mutated_freshness_lib_source` is the same idea for `freshness_lib.py`
        — used by `TestMainPropagatesRatherThanSwallows` to fault-inject a
        raise into `watched_repos()` for the executable half of that
        criterion, again without touching the real, shipped file.
        """
        self.root = tempfile.mkdtemp(prefix="repo-freshness-hook-test-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)

        self.toolkit = os.path.join(self.root, "toolkit")
        tool_dir = os.path.join(self.toolkit, "tools", "repo-freshness")
        os.makedirs(tool_dir)
        for name in _TOOL_FILES:
            src = os.path.join(TOOL_SRC_DIR, name)
            dest = os.path.join(tool_dir, name)
            if name == "sync_lib.py" and mutated_sync_lib_source is not None:
                with open(dest, "w") as handle:
                    handle.write(mutated_sync_lib_source)
            elif name == "freshness_lib.py" and mutated_freshness_lib_source is not None:
                with open(dest, "w") as handle:
                    handle.write(mutated_freshness_lib_source)
            else:
                shutil.copy(src, dest)
        os.chmod(os.path.join(tool_dir, "claude-repo-freshness"), 0o755)

        lib_dir = os.path.join(self.toolkit, "lib")
        os.makedirs(lib_dir)
        shutil.copy(LIB_SRC, os.path.join(lib_dir, "toolkit_config.py"))
        shutil.copy(INSTALL_RECORD_SRC, os.path.join(lib_dir, "install_record.py"))
        # Not created: `run_hook` points CLAUDE_CONFIG_DIR here so the hook
        # never reads the real machine's Claude config.
        self.claude_dir = os.path.join(self.home, ".claude")

        # No `origin` remote — see the module docstring. One commit so the
        # freshness-report half has a real HEAD/branch to read.
        git(self.toolkit, "init", "-q", "-b", "main")
        with open(os.path.join(self.toolkit, "README"), "w") as handle:
            handle.write("fixture toolkit copy\n")
        commit(self.toolkit, "README", "fixture toolkit copy\n")

        self.config_path = os.path.join(self.root, "config.json")
        self.write_config({})

        # Where the SYNC phase looks for candidate repos — unrelated to
        # `self.toolkit` above; kept as two separate trees so a test can put
        # a repo under sync scrutiny without it ever being freshness-watched,
        # and vice versa (see `write_config`'s `watch` parameter for the one
        # test that deliberately wants both).
        self.sync_root = os.path.join(self.root, "sync-root")
        os.makedirs(self.sync_root)

        self._abort_shim_dir = os.path.join(self.root, "path-shim")
        os.makedirs(self._abort_shim_dir)
        self.abort_log = os.path.join(self.root, "abort-gh-calls.log")
        self.abort_gh = _write_abort_shim(
            self._abort_shim_dir, "gh", self.abort_log)

    def write_config(self, blob):
        with open(self.config_path, "w") as handle:
            json.dump(blob, handle)

    def sync_config(self, watch=None, **sync_overrides):
        """Write `{"repo_freshness": {"watch": watch, "sync": {"repo_root":
        self.sync_root, **sync_overrides}}}` — the shape almost every test
        below needs, with `repo_root` defaulted so a caller only has to name
        what varies.
        """
        sync = {"repo_root": self.sync_root}
        sync.update(sync_overrides)
        blob = {"repo_freshness": {"sync": sync}}
        if watch is not None:
            blob["repo_freshness"]["watch"] = watch
        self.write_config(blob)

    def stamp_path(self):
        return os.path.join(self.home, ".cache", "claude-repo-sync.stamp")

    def write_stamp(self, when):
        os.makedirs(os.path.dirname(self.stamp_path()), exist_ok=True)
        with open(self.stamp_path(), "w") as handle:
            handle.write(str(when))

    def build_repo(self, name):
        """origin -> bare remote -> clone, clone living under `sync_root`."""
        base = os.path.join(self.root, "repo-" + name)
        os.makedirs(base)
        origin = os.path.join(base, "origin")
        remote = os.path.join(base, "remote.git")
        clone = os.path.join(self.sync_root, name)
        os.makedirs(origin)
        git(origin, "init", "-q", "-b", "main")
        commit(origin, "first")
        subprocess.run(("git", "clone", "-q", "--bare", origin, remote),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(("git", "clone", "-q", remote, clone),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return origin, remote, clone

    def advance_remote(self, remote, filename):
        pusher = tempfile.mkdtemp()
        subprocess.run(("git", "clone", "-q", remote, pusher),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commit(pusher, filename)
        git(pusher, "push", "-q", "origin", "main")

    def run_hook(self, session="s1", extra_env=None, abort_gh=True):
        """Drive the real, copied executable as a subprocess. Returns
        `(stdout, returncode)`.

        `abort_gh=True` (the default) wires the aborting `gh` from
        `__init__` onto BOTH `PATH` and the `SYNC_TEST_GH_BIN` seam, so a
        test that does not expect any `gh` invocation gets that guarantee
        for free rather than having to opt in. Tests that DO expect a real
        (fake) `gh` call pass `abort_gh=False` and supply their own
        `SYNC_TEST_GH_BIN` via `extra_env`.
        """
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "USERPROFILE": self.home,
            "CLAUDE_CONFIG_DIR": self.claude_dir,
            "CLAUDE_TOOLKIT_CONFIG": self.config_path,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
            # The hook's 1s local-git bound is its budget, not what these
            # tests check; under local-ci load one call overran it and a
            # diverged repo read "could not read".
            "SYNC_TEST_GIT_TIMEOUT": "30",
        })
        for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
            env.pop(var, None)
        if abort_gh:
            env["PATH"] = self._abort_shim_dir + os.pathsep + env.get("PATH", "")
            env["SYNC_TEST_GH_BIN"] = self.abort_gh
        if extra_env:
            env.update(extra_env)
        payload = json.dumps({
            "session_id": session, "hook_event_name": "SessionStart",
            "source": "startup", "cwd": self.root,
        })
        tool = os.path.join(self.toolkit, "tools", "repo-freshness",
                            "claude-repo-freshness")
        result = subprocess.run(
            (sys.executable, tool), input=payload, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        return result.stdout, result.returncode

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)


class HookTestCase(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)


class TestNoConfigAtAll(HookTestCase):
    """The default-install case: nothing under `repo_freshness` at all must
    behave byte-for-byte as it did before this branch existed — reports (if
    any), never pulls. Mutation target #1 (`if sync_lib.sync_enabled() and
    sync_lib.throttle_due():` -> `if True:`) fails THIS test: it performs a
    real, unattended `git pull` against a workspace with no `sync` key in
    config at all, which is the worst thing this feature could do.
    """

    def test_no_config_reports_as_before_and_never_pulls(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "remote-update")
        before = _rev_parse_head(clone)

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        # No `repo_freshness.watch` entry names `alpha`, so it is invisible
        # to the freshness-report half, and the toolkit fixture itself has
        # no origin to be behind — nothing to say.
        self.assertEqual(out, "")
        self.assertEqual(_rev_parse_head(clone), before,
                         "alpha was pulled with no `sync` config at all")
        self.assertFalse(
            os.path.exists(self.fx.stamp_path()),
            "the throttle stamp must not be written when the sync phase "
            "never even attempted to run")
        self.assertEqual(
            _read_shim_log(self.fx.abort_log), [],
            "gh was invoked with no `sync` config at all")


_UNSET = object()


class TestSyncEnabledGate(HookTestCase):
    """`sync.enabled` must be a literal `True` — everything else stays off,
    asserted through the executable rather than the library, so a mutation
    dropping this whole gate (target #1 / #3) has to fail a REAL subprocess
    run to survive.
    """

    def _assert_no_pull(self, enabled_value):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "remote-update")
        before = _rev_parse_head(clone)
        if enabled_value is _UNSET:
            self.fx.write_config({"repo_freshness": {
                "sync": {"repo_root": self.fx.sync_root}}})
        else:
            self.fx.sync_config(enabled=enabled_value)

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertEqual(_rev_parse_head(clone), before,
                         "enabled=%r must not pull" % (enabled_value,))
        self.assertNotIn("NOT SYNCED", out)
        self.assertNotIn("SYNC ATTEMPTED", out)

    def test_enabled_key_absent_does_not_pull(self):
        self._assert_no_pull(_UNSET)

    def test_enabled_false_does_not_pull(self):
        self._assert_no_pull(False)

    def test_enabled_string_true_does_not_pull(self):
        self._assert_no_pull("true")

    def test_enabled_int_one_does_not_pull(self):
        self._assert_no_pull(1)


class TestEnabledTruePullsAndReportsPostPullState(HookTestCase):
    """The positive control for the whole gate: `sync.enabled: true` with the
    throttle due really does pull, and — because the freshness-report half
    reads `alpha` AFTER the sync phase, in the same process — the notice
    that follows reflects the POST-pull state rather than the stale one a
    second, uncoordinated hook would have reported. Kills mutation target #3
    (disabling the sync phase outright): with the phase disabled, `alpha`
    stays behind and this test's silence assertion fails.
    """

    def test_pulls_and_the_notice_shows_zero_behind_afterward(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "one")
        self.fx.advance_remote(remote, "two")
        remote_head = git(remote, "rev-parse", "main")
        # `alpha` is BOTH a sync candidate (lives under sync_root) and
        # freshness-watched (named by absolute path) -- the only way to make
        # one repo exercise both halves of the hook in a single run.
        self.fx.sync_config(enabled=True, watch=[clone])

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertEqual(
            _rev_parse_head(clone), remote_head,
            "alpha was not fast-forwarded to origin's HEAD")
        self.assertTrue(os.path.exists(os.path.join(clone, "one")))
        self.assertTrue(os.path.exists(os.path.join(clone, "two")))
        self.assertEqual(
            out, "",
            "a freshly fast-forwarded, freshness-watched repo must not be "
            "reported as behind — the report ran on stale, pre-pull state: "
            "%r" % (out,))
        self.assertTrue(
            os.path.exists(self.fx.stamp_path()),
            "record_run() must have written the throttle stamp")


class TestThrottleNotDue(HookTestCase):

    def test_a_fresh_stamp_skips_the_pull_and_makes_no_gh_call(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "remote-update")
        before = _rev_parse_head(clone)
        self.fx.sync_config(enabled=True)
        self.fx.write_stamp(time.time())

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertEqual(_rev_parse_head(clone), before,
                         "a not-due throttle must not pull")
        self.assertEqual(
            _read_shim_log(self.fx.abort_log), [],
            "gh was invoked even though the sync phase never ran")


class TestBlockedRepoReachesTheNotice(HookTestCase):
    """Kills mutation target #2: dropping `blocked=` from the `render()`
    call makes the `NOT SYNCED` section vanish, and this is the only test in
    the suite whose passing depends on that argument reaching `render()`.
    """

    def test_a_dirty_repo_is_reported_as_not_synced(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "remote-update")
        with open(os.path.join(clone, "first"), "a") as handle:
            handle.write("dirty-local-edit")
        self.fx.sync_config(enabled=True)

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertIn("=== NOT SYNCED ===", out)
        self.assertIn("alpha", out)
        self.assertIn("uncommitted changes", out)


class TestFailedRepoReachesTheNotice(HookTestCase):

    def test_a_diverged_repo_is_reported_as_sync_attempted_not_applied(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        commit(clone, "local-only")
        self.fx.advance_remote(remote, "remote-only")
        git(clone, "config", "core.editor", "true")
        before = _rev_parse_head(clone)
        self.fx.sync_config(enabled=True)

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertIn("=== SYNC ATTEMPTED, NOT APPLIED ===", out)
        self.assertIn("alpha", out)
        self.assertEqual(
            _rev_parse_head(clone), before,
            "HEAD moved -- --ff-only must have merged rather than refused")


class TestASlowGitStillReadsTheRealState(HookTestCase):
    """Every local git call here takes longer than the hook's real 1s
    bound. Through the test seam the diverged repo still reports what it is;
    with the seam's value gone it reads "could not read", which proves the
    seam, not luck, is what the other hook tests stand on.
    """

    def _diverged_with_slow_git(self, extra_env):
        origin, remote, clone = self.fx.build_repo("alpha")
        commit(clone, "local-only")
        self.fx.advance_remote(remote, "remote-only")
        git(clone, "config", "core.editor", "true")
        self.fx.sync_config(enabled=True)
        env = {"SYNC_TEST_GIT_BIN": _write_slow_git_shim(self.fx.root, 1.5)}
        env.update(extra_env)
        return self.fx.run_hook(extra_env=env)

    def test_a_git_slower_than_the_hook_bound_still_reports_the_diverged_repo(self):
        out, code = self._diverged_with_slow_git({})

        self.assertEqual(code, 0)
        self.assertIn("=== SYNC ATTEMPTED, NOT APPLIED ===", out, out)
        self.assertNotIn("could not read", out, out)

    def test_without_the_seam_the_same_slow_git_reads_could_not_read(self):
        out, code = self._diverged_with_slow_git({"SYNC_TEST_GIT_TIMEOUT": ""})

        self.assertEqual(code, 0)
        self.assertIn("could not read", out, out)


class TestCloneIsAnnouncedByName(HookTestCase):
    """Kills mutation target #4: dropping the clone announcement from the
    hook leaves a newly-cloned repo silent, and this is the only test that
    asserts the `=== NEWLY CLONED ===` block by name.
    """

    def test_a_newly_cloned_repo_is_named_in_the_notice(self):
        log_path = os.path.join(self.fx.root, "gh-calls.log")
        shim = _write_gh_shim(self.fx.root, log_path, names=["brand-new"])
        self.fx.sync_config(enabled=True, gh_owner="some-owner")

        out, code = self.fx.run_hook(
            abort_gh=False, extra_env={"SYNC_TEST_GH_BIN": shim})

        self.assertEqual(code, 0)
        self.assertIn("=== NEWLY CLONED ===", out)
        self.assertIn("brand-new", out)
        self.assertTrue(
            os.path.isdir(os.path.join(self.fx.sync_root, "brand-new", ".git")))
        calls = _read_shim_log(log_path)
        self.assertTrue(any("repo list" in c for c in calls), calls)


class TestStampWrittenBeforeThePhase(HookTestCase):
    """The fix this test pins: `record_run()` runs BEFORE the sync phase,
    not in a `finally` after it, so a hook the harness kills mid-pull still
    counts as an attempt. `sync_lib.run()` is deliberately built to never
    raise — every internal call is already wrapped — so there is no
    configuration that makes it raise through the real, shipped module.
    Proving the ORDERING therefore needs a fault injected into a throwaway
    COPY of `sync_lib.py`, used by this test alone; every other test in this
    suite (and the whole of `test_sync_lib.py`) exercises the real,
    unmodified file.
    """

    def setUp(self):
        with open(os.path.join(TOOL_SRC_DIR, "sync_lib.py")) as handle:
            source = handle.read()
        marker = 'def run(root: str, owner: Optional[str] = None) -> SyncResult:\n'
        self.assertEqual(
            source.count(marker), 1,
            "sync_lib.py's run() signature has changed -- update this "
            "fault-injection marker")
        mutated = source.replace(
            marker,
            marker + '    raise RuntimeError("test-forced-sync-phase-failure")\n',
            1)
        self.assertNotEqual(mutated, source, "the fault was not injected")
        self.fx = Fixture(mutated_sync_lib_source=mutated)
        self.addCleanup(self.fx.destroy)

    def test_stamp_survives_a_raising_sync_phase(self):
        self.fx.sync_config(enabled=True)

        out, code = self.fx.run_hook()

        self.assertEqual(
            code, 0,
            "a raising sync phase must not take the whole hook down")
        self.assertTrue(
            os.path.exists(self.fx.stamp_path()),
            "the stamp must exist even though run() raised")
        with open(self.fx.stamp_path()) as handle:
            stamped = float(handle.read().strip())
        self.assertIs(
            stamped > time.time() - 60, True,
            "the stamp must be recent, i.e. written by THIS invocation")
        # throttle_due() reads the same stamp this test just checked exists;
        # asserted a second, independent way (age) rather than re-deriving
        # throttle_hours() here, which would just restate the constant.


class TestNoGhInvocation(HookTestCase):
    """The offline-hermeticity proof this build's earlier round was missing:
    a real `gh repo list` escaped to GitHub once already. Every default
    `run_hook()` call in this suite already carries the aborting shim; this
    class exists to assert the log directly, and to prove the shim mechanism
    itself is live rather than silently unreached.
    """

    def test_zero_gh_invocations_with_no_owner_configured(self):
        self.fx.sync_config(enabled=True)
        self.fx.write_stamp(0)  # far in the past -> due

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertEqual(_read_shim_log(self.fx.abort_log), [])

    def test_positive_control_the_abort_shim_is_reached_when_gh_would_be_called(self):
        """Without this, the zero-invocations test above would pass for
        free against a seam that is not actually wired to anything -- e.g.
        if `abort_gh=True` failed to reach `SYNC_TEST_GH_BIN` at all.
        """
        self.fx.sync_config(enabled=True, gh_owner="some-owner")
        self.fx.write_stamp(0)

        out, code = self.fx.run_hook()  # abort_gh=True (the default)

        self.assertEqual(code, 0)
        calls = _read_shim_log(self.fx.abort_log)
        self.assertTrue(
            calls,
            "the positive control never reached the abort shim -- the "
            "zero-invocations test above proves nothing")


class _InProcessMainTestCase(unittest.TestCase):
    """Shared plumbing for the in-process `main()` tests below (criteria 1,
    2, 3, 4, 5's first half, and 7). Every one of these drives the REAL
    `hook.main()` -- loaded via `SourceFileLoader` at import time, above --
    against `freshness_lib`/`sync_lib` attributes patched with
    `mock.patch.object`, which is visible inside `main()` because
    `import freshness_lib as freshness` / `import sync_lib` there just
    fetch the SAME module objects from `sys.modules`.

    `stdin` is always patched to a small valid JSON payload, per the "traps"
    note: `main()`'s first act is `json.load(sys.stdin)`, and driving it
    in-process without this makes the test read the TEST PROCESS's own
    stdin.
    """

    def _run_main(self, patches, stdin_payload=None):
        stdin_text = stdin_payload if stdin_payload is not None else json.dumps(
            {"session_id": "s", "hook_event_name": "SessionStart",
             "source": "startup", "cwd": "/tmp"})
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin_text)))
            # An empty config dir, so an in-process main() never reads the real
            # machine's Claude config.
            empty_claude = tempfile.mkdtemp(prefix="repo-freshness-claude-")
            self.addCleanup(shutil.rmtree, empty_claude, True)
            stack.enter_context(mock.patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": empty_claude}))
            for target, attr, value in patches:
                stack.enter_context(mock.patch.object(target, attr, value))
            hook.main()


class TestFetchSpawnOrdering(_InProcessMainTestCase):
    """Criterion 1: driving `main()` with ONE shared recorder list shows
    every fetch spawn after the last sync-phase call and before the first
    `measure_repos` call.

    Per the traps note, a `git` PATH shim cannot prove this -- a spawned
    fetch is a DETACHED child whose log lines land whenever the scheduler
    runs it, so the parent's own first blocking call routinely logs first
    even against CORRECT code. `spawn_due_fetches` and `measure_repos`
    themselves are replaced with recorders instead, so the order asserted
    here is the order `main()` itself calls them in, not an artifact of
    subprocess scheduling.
    """

    def test_fetch_spawn_after_sync_and_before_measure(self):
        calls = []

        def fake_run(root, owner):
            calls.append("sync_run")
            return sync_lib.SyncResult()

        def fake_spawn(repos, interval=None):
            calls.append("spawn_due_fetches")
            return 0

        def fake_measure(repos, deadline):
            calls.append("measure_repos")
            return [], 0

        self._run_main([
            (freshness_lib, "is_enabled", lambda: True),
            (freshness_lib, "watched_repos", lambda: []),
            (freshness_lib, "spawn_due_fetches", fake_spawn),
            (freshness_lib, "measure_repos", fake_measure),
            (sync_lib, "sync_enabled", lambda: True),
            (sync_lib, "throttle_due", lambda: True),
            (sync_lib, "record_run", lambda now=None: calls.append("record_run")),
            (sync_lib, "run", fake_run),
        ])

        self.assertEqual(calls, ["record_run", "sync_run", "spawn_due_fetches",
                                  "measure_repos"], calls)


class TestNoFetchBeforeSyncCompletes(_InProcessMainTestCase):
    """Criterion 2, the race guard -- asserted explicitly with a state flag
    rather than as a side effect of the ordered-list check above. Fetching
    against a repo `sync_lib.run()` is concurrently pulling is the concrete
    hazard: watched repos are typically also sync pull candidates.
    """

    def test_spawn_never_observes_sync_incomplete(self):
        # A LIST of every observation, not one overwritten flag: a spawn call
        # that happens too early and a later, correctly-timed one would
        # otherwise leave only the last (True) observation behind, hiding the
        # early one entirely.
        state = {"sync_done": False, "observations": []}

        def fake_run(root, owner):
            state["sync_done"] = True
            return sync_lib.SyncResult()

        def fake_spawn(repos, interval=None):
            state["observations"].append(state["sync_done"])
            return 0

        self._run_main([
            (freshness_lib, "is_enabled", lambda: True),
            (freshness_lib, "watched_repos", lambda: []),
            (freshness_lib, "spawn_due_fetches", fake_spawn),
            (freshness_lib, "measure_repos", lambda repos, deadline: ([], 0)),
            (sync_lib, "sync_enabled", lambda: True),
            (sync_lib, "throttle_due", lambda: True),
            (sync_lib, "record_run", lambda now=None: None),
            (sync_lib, "run", fake_run),
        ])

        self.assertTrue(state["observations"], "spawn_due_fetches was never called")
        self.assertTrue(
            all(state["observations"]),
            "spawn_due_fetches was called at least once before the sync "
            "phase completed: %r" % (state["observations"],))


class TestReportDeadlineMeasuredFromHookStart(_InProcessMainTestCase):
    """Criterion 3, the design pillar of this build -- and NOT the vacuous
    version. The vacuous version records `freshness._now()` inside the hook
    itself and compares it to the deadline; with `_now` stubbed, that
    reading IS the reading on the `report_deadline_at = ...` line, so the
    difference equals `REPORT_DEADLINE_SECONDS` by construction regardless of
    where that line sits, including below the sync phase.

    The test's OWN `t0`, read independently before `main()` runs, is what
    makes this a real assertion: `sync_lib.run()` is stubbed to advance the
    fake clock by 5 as if the sync phase took 5 seconds, and the deadline
    `measure_repos` actually receives must equal `t0 + REPORT_DEADLINE_
    SECONDS`, NOT `t0 + 5 + REPORT_DEADLINE_SECONDS`. Moving the deadline
    computation below the sync phase makes this fail.
    """

    def test_deadline_ignores_time_spent_in_the_sync_phase(self):
        clock = {"t": 1000.0}
        captured = {}

        def fake_now():
            return clock["t"]

        def fake_run(root, owner):
            clock["t"] += 5
            return sync_lib.SyncResult()

        def fake_measure(repos, deadline):
            captured["deadline_at"] = deadline._at
            return [], 0

        t0 = clock["t"]
        self._run_main([
            (freshness_lib, "_now", fake_now),
            (freshness_lib, "is_enabled", lambda: True),
            (freshness_lib, "watched_repos", lambda: []),
            (freshness_lib, "spawn_due_fetches", lambda repos, interval=None: 0),
            (freshness_lib, "measure_repos", fake_measure),
            (sync_lib, "sync_enabled", lambda: True),
            (sync_lib, "throttle_due", lambda: True),
            (sync_lib, "record_run", lambda now=None: None),
            (sync_lib, "run", fake_run),
        ])

        self.assertEqual(
            captured["deadline_at"], t0 + hook.REPORT_DEADLINE_SECONDS,
            "the report deadline must be measured from HOOK START, before "
            "the sync phase ever runs -- not from wherever it finished")
        self.assertNotEqual(
            captured["deadline_at"], t0 + 5 + hook.REPORT_DEADLINE_SECONDS,
            "this is the exact wrong value a deadline computed AFTER the "
            "sync phase would have produced")


class TestDeadlineAlreadyExpired(_InProcessMainTestCase):
    """Criterion 4: with `_now` driven so the report deadline is already
    past by the time `measure_repos` runs, the REAL `measure_repos` (not a
    stub) makes zero git calls, and `main()` still writes the notice it has
    (the `unmeasured` count alone is enough to earn one -- see
    `freshness_lib.render`'s `_FOOTER_UNMEASURED` branch).
    """

    def test_expired_deadline_skips_every_git_call(self):
        reads = {"n": 0}

        def fake_now():
            reads["n"] += 1
            # First read is the `report_deadline_at = ... + 13` computation,
            # BEFORE the sync phase (which is disabled below and makes no
            # `_now` call of its own). Every read after that is
            # `Deadline.expired()` inside the real `measure_repos` -- driven
            # far enough into the future that the very first check trips.
            return 1000.0 if reads["n"] == 1 else 1_000_000.0

        git_calls = []

        def fake_git(repo, *args, timeout=None):
            git_calls.append((repo, args))
            return None

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self._run_main([
                (freshness_lib, "_now", fake_now),
                (freshness_lib, "_git", fake_git),
                (freshness_lib, "is_enabled", lambda: True),
                (freshness_lib, "watched_repos",
                 lambda: [("/nonexistent/repo", "generic")]),
                (freshness_lib, "spawn_due_fetches", lambda repos, interval=None: 0),
                (sync_lib, "sync_enabled", lambda: False),
            ])

        self.assertEqual(git_calls, [],
                          "measure_repos made a git call after its deadline "
                          "had already expired")
        self.assertTrue(out.getvalue(),
                         "main() must still write the notice it has, even "
                         "though nothing could be measured this session")
        self.assertIn("STALENESS UNKNOWN", out.getvalue())


class TestMainPropagatesRatherThanSwallows(unittest.TestCase):
    """Criterion 5: `main()` propagates, it does not swallow -- the swallow
    lives ONLY in the module-level `if __name__ == "__main__":` block. Both
    halves are asserted: the in-process half proves no `try/except` was
    added inside `main()` itself; the subprocess half proves the
    module-level handler still guarantees exit 0 and silence around a real
    raise, so neither half alone would catch a regression in the other.
    """

    def test_main_raises_in_process_when_watched_repos_raises(self):
        stdin_text = json.dumps({"session_id": "s",
                                 "hook_event_name": "SessionStart",
                                 "source": "startup", "cwd": "/tmp"})
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin_text)))
            stack.enter_context(mock.patch.object(freshness_lib, "is_enabled", lambda: True))
            stack.enter_context(mock.patch.object(sync_lib, "sync_enabled", lambda: False))
            stack.enter_context(mock.patch.object(
                freshness_lib, "watched_repos",
                mock.Mock(side_effect=RuntimeError("test-forced-watched-repos-failure"))))
            with self.assertRaises(RuntimeError):
                hook.main()

    def test_subprocess_exits_zero_and_prints_nothing_when_main_raises(self):
        with open(os.path.join(TOOL_SRC_DIR, "freshness_lib.py")) as handle:
            source = handle.read()
        marker = "def watched_repos(toolkit: Optional[str] = None) -> list:\n"
        self.assertEqual(
            source.count(marker), 1,
            "freshness_lib.py's watched_repos() signature has changed -- "
            "update this fault-injection marker")
        mutated = source.replace(
            marker,
            marker + '    raise RuntimeError("test-forced-main-failure")\n',
            1)
        self.assertNotEqual(mutated, source, "the fault was not injected")

        fx = Fixture(mutated_freshness_lib_source=mutated)
        self.addCleanup(fx.destroy)

        out, code = fx.run_hook()

        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class TestReportPhaseProducesExpectedNoticeOnStdout(HookTestCase):
    """Criterion 6, executable-level: a real SessionStart payload against a
    fixture repo that IS behind produces the expected notice on stdout.
    Asserted on stdout CONTENT specifically, not merely `returncode == 0` --
    the module-level handler hardcodes exit 0 regardless of whether `main()`
    raised on its very first line, so an exit-code-only assertion would pass
    even for a hook whose report phase never ran at all. This exercises the
    real, unmocked `spawn_due_fetches` / `measure_repos` wiring end to end.
    """

    def test_a_behind_watched_repo_produces_the_expected_notice(self):
        origin, remote, clone = self.fx.build_repo("alpha")
        self.fx.advance_remote(remote, "remote-update")
        # Fetch into the clone BEFORE running the hook so `origin/main` is
        # already current when the hook reads it. Production's own
        # `spawn_background_fetch` is a detached, un-awaited `Popen` that
        # refreshes refs for the NEXT session -- relying on it here made the
        # assertion a race against that spawn rather than a test of the
        # report-phase wiring.
        git(clone, "fetch", "-q", "origin")
        self.fx.write_config({"repo_freshness": {"watch": [clone]}})

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertIn("=== REPO UPDATE AVAILABLE ===", out)
        self.assertIn("alpha", out)
        self.assertIn("commit(s) behind", out)


class TestPinNoticeReachesStdout(HookTestCase):
    """A fixture toolkit checkout detached at the recorded rollback commit,
    with the record under the sandbox `$HOME`, prints the pin notice."""

    def test_a_pinned_toolkit_is_reported(self):
        head = git(self.fx.toolkit, "rev-parse", "HEAD").strip()
        git(self.fx.toolkit, "checkout", "-q", "--detach", "HEAD")
        record = os.path.join(
            self.fx.home, ".config", "claude-code-toolkit", "install-record.json")
        os.makedirs(os.path.dirname(record))
        with open(record, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "permissions_added": {},
                       "rollback": {"commit": head,
                                    "recorded_at": "2026-10-12T09:00:00Z",
                                    "pinned_at": "2026-10-13T09:00:00Z"}}, handle)

        out, code = self.fx.run_hook()

        self.assertEqual(code, 0)
        self.assertIn("=== TOOLKIT PINNED ===", out)
        self.assertIn(head[:12], out)


class TestPinComputedBeforeMeasureWithSameDeadline(_InProcessMainTestCase):

    def test_pin_notice_runs_before_measure_repos_on_the_same_deadline(self):
        calls = []

        def fake_pin(repo, record_file, deadline):
            calls.append(("pin", deadline))
            return "PIN NOTICE"

        def fake_measure(repos, deadline):
            calls.append(("measure", deadline))
            return [], 0

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self._run_main([
                (freshness_lib, "is_enabled", lambda: True),
                (freshness_lib, "watched_repos", lambda: []),
                (freshness_lib, "spawn_due_fetches", lambda repos, interval=None: 0),
                # Hermetic: the real repo_root() walks up for a `.git`
                # directory, which a scratch worktree outside the primary
                # checkout does not have, so main() would skip the pin.
                (freshness_lib, "repo_root", lambda start=None: "/fixture/toolkit"),
                (freshness_lib, "pin_notice", fake_pin),
                (freshness_lib, "measure_repos", fake_measure),
                (sync_lib, "sync_enabled", lambda: False),
            ])

        self.assertEqual([name for name, _ in calls], ["pin", "measure"])
        self.assertIs(calls[0][1], calls[1][1])
        self.assertIn("PIN NOTICE", out.getvalue())


class TestStdoutFlushedAfterWrite(_InProcessMainTestCase):
    """Criterion 7: `sys.stdout.flush()` is called after a non-empty write --
    asserted with a fake stdout recording METHOD CALLS, never by inspecting
    source text. stdout is a pipe here and therefore block-buffered; anything
    still sitting in the buffer when the process dies is lost.
    """

    def test_flush_follows_a_non_empty_write(self):
        calls = []

        class _RecordingStdout:
            def write(self, text):
                calls.append(("write", text))

            def flush(self):
                calls.append(("flush",))

        with mock.patch.object(sys, "stdout", _RecordingStdout()):
            self._run_main([
                (freshness_lib, "is_enabled", lambda: True),
                (freshness_lib, "watched_repos", lambda: []),
                (freshness_lib, "spawn_due_fetches", lambda repos, interval=None: 0),
                (freshness_lib, "measure_repos", lambda repos, deadline: ([], 5)),
                (sync_lib, "sync_enabled", lambda: False),
            ])

        self.assertTrue(calls, "nothing was written -- the fixture produced "
                               "no notice at all")
        write_indices = [i for i, c in enumerate(calls) if c[0] == "write"]
        flush_indices = [i for i, c in enumerate(calls) if c[0] == "flush"]
        self.assertTrue(write_indices, "write() was never called")
        self.assertTrue(flush_indices, "flush() was never called")
        self.assertLess(max(write_indices), min(flush_indices),
                         "flush() must follow every write(), not precede it: %r" % (calls,))


if __name__ == "__main__":
    unittest.main()
