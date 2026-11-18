"""Tests for the sync engine's pure/near-pure half: config reads, discovery,
and eligibility.

Hermetic: every git test builds its own throwaway repo (and, where a remote
matters, its own bare "remote") under a temp directory. Nothing here reads the
real toolkit repo or the real workspace_root -- both would make these pass or
fail depending on which machine and which day they run.
"""
import ast
import importlib.machinery
import importlib.util
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_lib
import freshness_lib
import toolkit_config

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
import hook_manifest  # noqa: E402

# In-process access to the hook module's own `REPORT_DEADLINE_SECONDS` --
# `TestWholeHookTimeoutBudget` needs it read LIVE from the executable, not
# restated as a literal here. `claude-repo-freshness` has no `.py` extension,
# hence `SourceFileLoader` rather than a plain `import` -- same idiom
# `test_hook.py` uses for the same file. A distinct module name keeps this
# load independent of `test_hook.py`'s own, in case both ever share a process.
_HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "claude-repo-freshness")
_hook_spec = importlib.util.spec_from_loader(
    "claude_repo_freshness_hook_for_budget_test",
    importlib.machinery.SourceFileLoader(
        "claude_repo_freshness_hook_for_budget_test", _HOOK_PATH))
hook_module = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(hook_module)


# `SYNC_TEST_GIT_BIN` / `SYNC_TEST_GH_BIN` are only honoured inside a
# demonstrable test sandbox (see `sync_lib._is_test_sandbox()`), so every
# shim-based test below must ALSO redirect `CLAUDE_TOOLKIT_CONFIG` -- the
# live marker the gate checks -- alongside the shim path itself. A path that
# merely does not exist is enough; nothing here reads it.
_SANDBOX_CONFIG = "/nonexistent-on-purpose-test-sandbox.json"


def git(repo, *args):
    subprocess.run(("git", "-C", repo) + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def commit(repo, name):
    with open(os.path.join(repo, name), "w") as handle:
        handle.write(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)


class ConfigTestCase(unittest.TestCase):
    """Base for anything that reads config: swaps CONFIG_PATH for a temp file.

    Same pattern as `session-titler`'s TestIsEnabled -- CONFIG_PATH is a
    module-level path resolved once at import from CLAUDE_TOOLKIT_CONFIG, so
    in-process tests move it directly rather than re-invoking the interpreter.
    """

    def setUp(self):
        self._orig_config_path = toolkit_config.CONFIG_PATH
        self.addCleanup(self._restore)

    def _restore(self):
        toolkit_config.CONFIG_PATH = self._orig_config_path
        toolkit_config.load(refresh=True)

    def _write_config(self, blob):
        import json
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(blob, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)

    def _no_config(self):
        toolkit_config.CONFIG_PATH = "/nonexistent-on-purpose.json"
        toolkit_config.load(refresh=True)


class TestSyncEnabled(ConfigTestCase):
    """Only a literal `True` turns pulling on. Everything else is OFF."""

    def test_absent_config_is_off(self):
        self._no_config()
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_explicit_false_is_off(self):
        self._write_config({"repo_freshness": {"sync": {"enabled": False}}})
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_the_string_true_is_off(self):
        # The whole point of the rule: a truthy value that merely LOOKS like
        # an opt-in must never behave like one.
        self._write_config({"repo_freshness": {"sync": {"enabled": "true"}}})
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_the_integer_one_is_off(self):
        self._write_config({"repo_freshness": {"sync": {"enabled": 1}}})
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_an_empty_list_is_off(self):
        self._write_config({"repo_freshness": {"sync": {"enabled": []}}})
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_explicit_null_is_off(self):
        self._write_config({"repo_freshness": {"sync": {"enabled": None}}})
        self.assertIs(sync_lib.sync_enabled(), False)

    def test_only_literal_true_is_on(self):
        self._write_config({"repo_freshness": {"sync": {"enabled": True}}})
        self.assertIs(sync_lib.sync_enabled(), True)


class TestSyncRoot(ConfigTestCase):
    """Root resolution: the CONFIGURED path, or None. Never derived.

    An earlier version fell back to `<workspace_root>/repos` when no root was
    configured, and its suite pinned that fallback. These tests pin the
    OPPOSITE property -- see `sync_lib`'s module docstring, "This function
    does not derive a root".

    The two tests asserting a workspace root alone yields nothing are the ones
    that matter. They are what a future "restore the convenient default"
    edit has to get past, and they are written against BOTH plausible
    derivations -- the `repos/` subdirectory and the bare root -- because a
    fallback reintroduced in either spelling would start pulling trees nobody
    named.
    """

    def test_none_when_nothing_is_configured(self):
        self._no_config()
        self.assertIsNone(sync_lib.sync_root())

    def test_a_workspace_root_with_a_real_repos_dir_still_derives_NOTHING(self):
        """The `<workspace_root>/repos` fallback, killed.

        Everything the old derivation needs is present and valid: a
        configured workspace root, and a `repos/` directory that really
        exists. The answer is still None, because no `repo_root` was named.
        """
        with tempfile.TemporaryDirectory() as workspace:
            repos_dir = os.path.join(workspace, "repos")
            os.makedirs(repos_dir)
            self._write_config({"workspace_root": workspace})
            self.assertIsNone(
                sync_lib.sync_root(),
                "a `repos/` directory beside the workspace root is not "
                "permission to pull the trees inside it")

    def test_a_flat_workspace_layout_derives_NOTHING_either(self):
        """The bare-workspace-root fallback, killed.

        The other plausible derivation, and the more dangerous one: plenty
        of workspaces keep their clones flat directly under the root, so a
        bare-root fallback would resolve to a directory full of real
        checkouts -- scratch worktrees included -- on a machine where nobody
        configured sync at all.
        """
        with tempfile.TemporaryDirectory() as workspace:
            os.makedirs(os.path.join(workspace, "some-clone", ".git"))
            self._write_config({"workspace_root": workspace})
            self.assertIsNone(sync_lib.sync_root())

    def test_a_configured_repo_root_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = os.path.join(tmp, "workspace")
            explicit = os.path.join(tmp, "elsewhere")
            # A `repos/` sibling exists and is NOT what comes back: the
            # positive half of the two tests above, proving the configured
            # value is genuinely read rather than the function merely being
            # broken in a way that returns None for everything.
            os.makedirs(os.path.join(workspace, "repos"))
            os.makedirs(explicit)
            self._write_config({
                "workspace_root": workspace,
                "repo_freshness": {"sync": {"repo_root": explicit}},
            })
            self.assertEqual(sync_lib.sync_root(), explicit)

    def test_none_when_the_configured_repo_root_does_not_exist(self):
        self._write_config({
            "repo_freshness": {"sync": {"repo_root": "/nonexistent/repo/root"}},
        })
        self.assertIsNone(sync_lib.sync_root())

    def test_a_configured_root_that_is_a_FILE_is_refused(self):
        """`isdir`, not `exists` -- a root pointing at a regular file is None
        rather than something handed to the discovery walk.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "not-a-dir")
            with open(path, "w") as handle:
                handle.write("")
            self._write_config({
                "repo_freshness": {"sync": {"repo_root": path}},
            })
            self.assertIsNone(sync_lib.sync_root())


class TestThrottleHours(ConfigTestCase):

    def test_defaults_to_twenty_when_unconfigured(self):
        self._no_config()
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_configured_value_is_honoured(self):
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": 6}}})
        self.assertEqual(sync_lib.throttle_hours(), 6)

    def test_zero_falls_back_to_the_default(self):
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": 0}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_negative_value_falls_back_to_the_default(self):
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": -5}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_non_int_value_falls_back_to_the_default(self):
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": "soon"}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_float_value_falls_back_to_the_default(self):
        # `toolkit_config.get_int` would truncate this to `1` rather than
        # fall back -- `int(1.5) == 1` raises nothing. The documented
        # promise in config.example.json is stricter than that shared
        # helper, so this function validates locally instead of trusting it.
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": 1.5}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_numeric_string_falls_back_to_the_default(self):
        # `toolkit_config.get_int` would coerce this to `6` -- `int("6")`
        # raises nothing either. Only a genuine JSON integer is trusted.
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": "6"}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)

    def test_a_bool_value_falls_back_to_the_default(self):
        # `isinstance(True, int)` is true in Python -- a stray `true` in the
        # config must not silently become `1`.
        self._write_config({"repo_freshness": {"sync": {"throttle_hours": True}}})
        self.assertEqual(sync_lib.throttle_hours(), 20)


class TestCandidateRepos(unittest.TestCase):
    """Discovery is a filesystem scan, not a git call: no fixture git repo
    needed, just directories and files shaped the right way."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_a_directory_with_dot_git_is_included(self):
        repo = os.path.join(self._tmp, "has-git")
        os.makedirs(os.path.join(repo, ".git"))
        self.assertEqual(sync_lib.candidate_repos(self._tmp), [repo])

    def test_a_plain_directory_is_omitted(self):
        os.makedirs(os.path.join(self._tmp, "plain-dir"))
        self.assertEqual(sync_lib.candidate_repos(self._tmp), [])

    def test_a_file_is_omitted(self):
        with open(os.path.join(self._tmp, "just-a-file"), "w") as handle:
            handle.write("x")
        self.assertEqual(sync_lib.candidate_repos(self._tmp), [])

    def test_results_are_sorted(self):
        for name in ("zeta", "alpha", "mid"):
            os.makedirs(os.path.join(self._tmp, name, ".git"))
        self.assertEqual(
            sync_lib.candidate_repos(self._tmp),
            [os.path.join(self._tmp, n) for n in ("alpha", "mid", "zeta")])

    def test_never_recurses_into_subdirectories(self):
        nested = os.path.join(self._tmp, "container", "nested-repo")
        os.makedirs(os.path.join(nested, ".git"))
        self.assertEqual(sync_lib.candidate_repos(self._tmp), [])

    def test_a_missing_root_returns_empty_rather_than_raising(self):
        self.assertEqual(
            sync_lib.candidate_repos(os.path.join(self._tmp, "nope")), [])

    def test_an_empty_root_returns_empty(self):
        self.assertEqual(sync_lib.candidate_repos(""), [])
        self.assertEqual(sync_lib.candidate_repos(None), [])


def _give_real_git_room(test):
    """Run `test` with a 30s `GIT_TIMEOUT_SECONDS` instead of the hook's 1s.

    The 1s bound is the hook's budget, not what the real-git tests check. On a
    loaded machine one local git call overran it, `_git` returned None, and a
    dirty repo read "could not read". Scoped per test
    rather than per module because `TestTimeoutBudget` reads the constant live
    and must keep seeing the real value.
    """
    patcher = mock.patch.object(sync_lib, "GIT_TIMEOUT_SECONDS", 30)
    patcher.start()
    test.addCleanup(patcher.stop)


class TestGitTimeoutSeam(unittest.TestCase):
    """`SYNC_TEST_GIT_TIMEOUT` moves `_git`'s bound only inside a test
    sandbox, and only to a finite value above zero."""

    def _timeout(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("CLAUDE_TOOLKIT_CONFIG", "SYNC_TEST_GIT_TIMEOUT"):
                if name not in env:
                    os.environ.pop(name, None)
            return sync_lib._git_timeout()

    def test_a_sandboxed_override_is_used(self):
        self.assertEqual(self._timeout(CLAUDE_TOOLKIT_CONFIG="/x.json",
                                       SYNC_TEST_GIT_TIMEOUT="7"), 7.0)

    def test_an_override_outside_a_sandbox_is_ignored(self):
        self.assertEqual(self._timeout(SYNC_TEST_GIT_TIMEOUT="7"),
                         sync_lib.GIT_TIMEOUT_SECONDS)

    def test_an_unusable_override_falls_back_to_the_real_bound(self):
        for value in ("inf", "-inf", "nan", "0", "-3", "soon", ""):
            with self.subTest(value=value):
                self.assertEqual(
                    self._timeout(CLAUDE_TOOLKIT_CONFIG="/x.json",
                                  SYNC_TEST_GIT_TIMEOUT=value),
                    sync_lib.GIT_TIMEOUT_SECONDS)

    def test_git_is_called_with_the_seams_bound(self):
        seen = {}

        def fake_run(*args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(args, 0, b"main\n", b"")

        with mock.patch.dict(os.environ, {"CLAUDE_TOOLKIT_CONFIG": "/x.json",
                                          "SYNC_TEST_GIT_TIMEOUT": "9"}), \
                mock.patch.object(sync_lib.subprocess, "run", fake_run):
            self.assertEqual(sync_lib._git("/repo", "rev-parse", "HEAD"), "main")
        self.assertEqual(seen["timeout"], 9.0)


class GitRepoTestCase(unittest.TestCase):
    """A real clone with a real remote, for the git-backed eligibility checks."""

    def setUp(self):
        if not self._git_available():
            self.skipTest("git not available")
        _give_real_git_room(self)
        self._tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self._tmp, "remote.git")
        self.clone = os.path.join(self._tmp, "clone")

        origin = os.path.join(self._tmp, "origin")
        os.makedirs(origin)
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "T")
        commit(origin, "first")
        subprocess.run(("git", "clone", "-q", "--bare", origin, self.remote),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(("git", "clone", "-q", self.remote, self.clone),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "T")

    @staticmethod
    def _git_available():
        try:
            subprocess.run(("git", "--version"), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


class TestEligibility(GitRepoTestCase):

    def test_a_clean_tracking_branch_is_eligible(self):
        self.assertIsNone(sync_lib.eligibility(self.clone))

    def test_detached_head_is_blocked(self):
        git(self.clone, "checkout", "-q", "--detach", "main")
        self.assertEqual(sync_lib.eligibility(self.clone), "detached HEAD")

    def test_uncommitted_changes_are_blocked(self):
        with open(os.path.join(self.clone, "first"), "a") as handle:
            handle.write("more")
        self.assertEqual(sync_lib.eligibility(self.clone), "uncommitted changes")

    def test_an_untracked_file_also_counts_as_uncommitted(self):
        with open(os.path.join(self.clone, "new-file"), "w") as handle:
            handle.write("x")
        self.assertEqual(sync_lib.eligibility(self.clone), "uncommitted changes")

    def test_a_branch_with_no_upstream_is_blocked(self):
        git(self.clone, "checkout", "-q", "-b", "no-tracking")
        self.assertEqual(sync_lib.eligibility(self.clone), "no upstream")

    def test_checks_run_in_order_detached_beats_dirty(self):
        # A detached, dirty tree is still reported as "detached HEAD" first --
        # the order in the docstring is a contract, not an implementation
        # detail, because the caller uses the reason to decide what to say.
        git(self.clone, "checkout", "-q", "--detach", "main")
        with open(os.path.join(self.clone, "first"), "a") as handle:
            handle.write("more")
        self.assertEqual(sync_lib.eligibility(self.clone), "detached HEAD")

    def test_dirty_beats_no_upstream(self):
        git(self.clone, "checkout", "-q", "-b", "no-tracking")
        with open(os.path.join(self.clone, "first"), "a") as handle:
            handle.write("more")
        self.assertEqual(sync_lib.eligibility(self.clone), "uncommitted changes")

    def test_a_nonexistent_repo_reads_as_could_not_read_not_a_real_state(self):
        # Every `_git` call here fails (not a repo at all), so this must land
        # on the "cannot tell" reason -- not "detached HEAD", which is what a
        # bare `if not branch` check would confuse it with.
        self.assertEqual(
            sync_lib.eligibility(os.path.join(self._tmp, "no-such-repo")),
            "could not read")

    def test_an_empty_path_reads_as_could_not_read_not_a_real_state(self):
        self.assertEqual(sync_lib.eligibility(""), "could not read")


class TestEligibilityDoesNotConflateFailureWithARealState(unittest.TestCase):
    """The defect this task's follow-up fixed: `_git` returns `None` for BOTH
    "the real answer is no" and "the call failed or timed out", and the old
    code could not tell them apart -- a timeout on the very first git call
    read as "detached HEAD", a failed `status` read as "uncommitted changes".
    Both are confident, specific, WRONG answers printed to the human.

    Forced through a seam (`mock.patch.object` on `_git`) rather than by
    corrupting a real repo, so each case is deterministic: it isolates
    exactly which call failed, without depending on git's own error timing
    or a real timeout.
    """

    def test_a_failure_on_the_first_call_is_could_not_read_not_detached(self):
        with mock.patch.object(sync_lib, "_git", return_value=None):
            self.assertEqual(sync_lib.eligibility("/some/repo"), "could not read")

    def test_a_failure_on_the_status_call_is_could_not_read_not_dirty(self):
        # First call (branch) succeeds with a real branch name; second call
        # (status) fails. Must not be reported as "uncommitted changes".
        with mock.patch.object(sync_lib, "_git", side_effect=["main", None]):
            self.assertEqual(sync_lib.eligibility("/some/repo"), "could not read")

    def test_a_failure_on_the_upstream_call_is_could_not_read_not_no_upstream(self):
        # branch and status both succeed (clean); the upstream lookup fails.
        # Must not be reported as "no upstream" -- that would claim there is
        # definitely no tracking ref configured, when the truth is unknown.
        with mock.patch.object(sync_lib, "_git", side_effect=["main", "", None]):
            self.assertEqual(sync_lib.eligibility("/some/repo"), "could not read")

    def test_a_real_no_upstream_is_still_reported_correctly(self):
        # The complement: when the upstream call SUCCEEDS with empty output
        # (for-each-ref's real behaviour when nothing is configured), the
        # fix above must not have turned every "no upstream" into
        # "could not read".
        with mock.patch.object(sync_lib, "_git", side_effect=["main", "", ""]):
            self.assertEqual(sync_lib.eligibility("/some/repo"), "no upstream")

    def test_a_clean_eligible_repo_is_still_none(self):
        with mock.patch.object(sync_lib, "_git",
                               side_effect=["main", "", "refs/remotes/origin/main"]):
            self.assertIsNone(sync_lib.eligibility("/some/repo"))


class TestHardenedEnv(unittest.TestCase):
    """Direct, deterministic coverage of what `PATH_ENV` strips -- independent
    of subprocess timing. `TestPathEnvGitDirHardening` below proves the same
    thing end to end through `eligibility()`; this pins the exact variable
    list `_hardened_env` uses to build it.
    """

    def test_git_redirection_vars_are_stripped(self):
        base = {
            "GIT_DIR": "/somewhere/else/.git",
            "GIT_WORK_TREE": "/somewhere/else",
            "GIT_COMMON_DIR": "/somewhere/else/.git",
            "GIT_SSH_COMMAND": "echo pwned",
            "HOME": "/home/sam",
        }
        env = sync_lib._hardened_env(base)
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_SSH_COMMAND"):
            self.assertNotIn(var, env, var)

    def test_the_hardened_path_replaces_whatever_was_inherited(self):
        env = sync_lib._hardened_env({"PATH": "/evil/bin"})
        self.assertEqual(env["PATH"], sync_lib._SAFE_PATH)

    def test_other_environment_variables_survive(self):
        env = sync_lib._hardened_env({"HOME": "/home/sam", "PATH": "/x"})
        self.assertEqual(env["HOME"], "/home/sam")

    def test_git_config_global_is_deliberately_not_stripped(self):
        # Named explicitly so a future tightening pass has to change this
        # test, not silently narrow the module's own documented decision.
        env = sync_lib._hardened_env({"GIT_CONFIG_GLOBAL": "/somewhere/else"})
        self.assertIn("GIT_CONFIG_GLOBAL", env)

    def test_module_level_path_env_has_the_same_shape(self):
        # PATH_ENV is _hardened_env() applied once at import, over the real
        # os.environ -- pin that relationship so the two cannot drift apart.
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_SSH_COMMAND"):
            self.assertNotIn(var, sync_lib.PATH_ENV)
        self.assertEqual(sync_lib.PATH_ENV["PATH"], sync_lib._SAFE_PATH)


class TestPathEnvGitDirHardening(GitRepoTestCase):
    """`PATH_ENV` is captured ONCE, at import time, from `os.environ` -- so
    setting `os.environ["GIT_DIR"]` after `import sync_lib` changes nothing
    about it (`TestHardenedEnv` above proves the stripping logic directly).
    The only faithful way to prove "a value in the PARENT environment cannot
    leak through and redirect a real call" is a subprocess that has it set
    from the moment it starts -- exactly how a real hook inherits whatever
    its launching shell exported.
    """

    def _poisoned_repo(self):
        # A second, independent repo that tracks the SAME filename as
        # `self.clone` ("first") with DIFFERENT content. `git -C <dir>`
        # leaves the working tree at `<dir>` even when GIT_DIR is overridden
        # -- GIT_WORK_TREE is untouched here -- so the only way a leaked
        # GIT_DIR shows up is as a content mismatch between the poisoned
        # HEAD's blob and the real file sitting in `self.clone`. Measured:
        # `GIT_DIR=<poisoned>/.git git -C <clean> status --porcelain` prints
        # " M first" when the guard is missing, and nothing when it holds.
        path = os.path.join(self._tmp, "poisoned")
        os.makedirs(path)
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "T")
        with open(os.path.join(path, "first"), "w") as handle:
            handle.write("POISONED-CONTENT")
        git(path, "add", "first")
        git(path, "commit", "-q", "-m", "poisoned")
        return path

    def _eligibility_via_subprocess(self, target, extra_env):
        tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # A fresh interpreter never sees this process's mock.patch, so the
        # fixture's git timeout is handed over explicitly.
        script = ("import sys; sys.path.insert(0, {tools!r}); import sync_lib; "
                  "sync_lib.GIT_TIMEOUT_SECONDS = {timeout!r}; "
                  "print(sync_lib.eligibility({target!r}))"
                  ).format(tools=tools_dir, target=target,
                           timeout=sync_lib.GIT_TIMEOUT_SECONDS)
        env = dict(os.environ)
        env.update(extra_env)
        result = subprocess.run(
            (sys.executable, "-c", script),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def test_a_stray_git_dir_does_not_redirect_eligibility(self):
        poisoned = self._poisoned_repo()
        answer = self._eligibility_via_subprocess(
            self.clone, {"GIT_DIR": os.path.join(poisoned, ".git")})
        self.assertEqual(
            answer, "None",
            "eligibility() answered about the GIT_DIR-poisoned repo rather "
            "than the path it was given")

    def test_the_positive_control_a_tainted_git_dir_really_is_visible_to_git(self):
        # Proves the fixture itself is capable of poisoning an UNGUARDED git
        # call -- without this, a bug that made stripping a no-op would still
        # pass the test above for the wrong reason (e.g. if the fixture
        # simply failed to redirect anything). Runs plain `git`, no
        # PATH_ENV, to show GIT_DIR is genuinely load-bearing on this
        # machine for this exact command.
        poisoned = self._poisoned_repo()
        env = dict(os.environ)
        env["GIT_DIR"] = os.path.join(poisoned, ".git")
        result = subprocess.run(
            ("git", "-C", self.clone, "status", "--porcelain"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=15)
        self.assertEqual(
            result.stdout.decode().strip(), "M first",
            "the fixture's GIT_DIR did not redirect an unguarded git call, so "
            "the test above would pass even if PATH_ENV did nothing")

    def _poisoned_repo_with_an_origin_ahead(self):
        """A poisoned repo that a leaked GIT_DIR could actually FAST-FORWARD.

        `_poisoned_repo` above is enough to prove a redirect on a read
        (`status`, `eligibility`). Proving it on the WRITE needs the poisoned
        repo to have somewhere to pull FROM and something to pull: with
        `GIT_DIR` set, `git -C <clean> pull` reads the poisoned repo's config
        and advances the poisoned repo's HEAD, leaving the directory named on
        the command line untouched. That is "measure one repo, fast-forward
        another" -- the hazard `_STRIPPED_ENV_VARS` exists for.
        """
        origin = os.path.join(self._tmp, "p-origin")
        os.makedirs(origin)
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "T")
        commit(origin, "p-first")
        bare = os.path.join(self._tmp, "p-remote.git")
        subprocess.run(("git", "clone", "-q", "--bare", origin, bare), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        poisoned = os.path.join(self._tmp, "poisoned-ff")
        subprocess.run(("git", "clone", "-q", bare, poisoned), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Advance the bare remote so the poisoned clone is genuinely BEHIND
        # and a fast-forward is available to it.
        commit(origin, "p-second")
        subprocess.run(("git", "-C", origin, "push", "-q", bare, "main"), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return poisoned

    @staticmethod
    def _head_of(repo):
        result = subprocess.run(("git", "-C", repo, "rev-parse", "HEAD"),
                                stdout=subprocess.PIPE, timeout=15)
        return result.stdout.decode().strip()

    def _pull_via_subprocess(self, target, extra_env):
        tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = ("import sys; sys.path.insert(0, {tools!r}); import sync_lib; "
                  "sync_lib._pull({target!r})").format(
                      tools=tools_dir, target=target)
        env = dict(os.environ)
        env.update(extra_env)
        subprocess.run((sys.executable, "-c", script),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=env, timeout=30)

    def test_a_stray_git_dir_does_not_redirect_the_PULL(self):
        """The write-side counterpart of the eligibility test above.

        `_pull` is the highest-stakes call site in this module -- it is the
        one that changes a working tree -- and it was the one whose
        `env=PATH_ENV` no test could see: deleting that kwarg left the whole
        suite green.
        """
        poisoned = self._poisoned_repo_with_an_origin_ahead()
        before = self._head_of(poisoned)
        self._pull_via_subprocess(
            self.clone, {"GIT_DIR": os.path.join(poisoned, ".git")})
        self.assertEqual(
            self._head_of(poisoned), before,
            "_pull() fast-forwarded the GIT_DIR-poisoned repo instead of the "
            "path it was given -- env=PATH_ENV is not reaching this call site")

    def test_the_positive_control_an_unguarded_pull_really_does_get_redirected(self):
        """Without this, the test above passes for free if the fixture simply
        cannot be fast-forwarded (nothing to pull, no remote, wrong branch).
        Runs plain `git pull --ff-only` with no PATH_ENV to show the redirect
        is genuinely available on this machine for this exact command.
        """
        poisoned = self._poisoned_repo_with_an_origin_ahead()
        before = self._head_of(poisoned)
        env = dict(os.environ)
        env["GIT_DIR"] = os.path.join(poisoned, ".git")
        subprocess.run(("git", "-C", self.clone, "pull", "--ff-only", "--quiet"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=env, timeout=30)
        self.assertNotEqual(
            self._head_of(poisoned), before,
            "the fixture's GIT_DIR did not redirect an unguarded pull, so the "
            "test above would pass even if PATH_ENV did nothing")


class TestEverySubprocessCallIsHardened(unittest.TestCase):
    """`env=PATH_ENV` on EVERY `subprocess.run` in `sync_lib`, not just the
    ones a behavioural test happens to reach.

    The module docstring makes a universal claim -- "`PATH_ENV` is the
    explicit `env=` every subprocess call in this module must pass" -- and
    before this test only one of the four call sites was enforced by
    anything. Deleting the kwarg from `_pull`, `_clone` or `_gh_repo_names`
    left all 104 tests green.

    Keyed on the module's own syntax rather than on a list of function names,
    so a call site added later is covered without anyone remembering to
    extend this test. That is the same reason the prune exclusion elsewhere
    in this repo is keyed on what the code did rather than on a parallel
    array: a check whose correctness depends on being updated has the defect
    it replaces, one step later.
    """

    def _run_calls(self):
        with open(sync_lib.__file__) as handle:
            tree = ast.parse(handle.read())
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"):
                calls.append(node)
        return calls

    def test_the_canary_finds_the_call_sites_at_all(self):
        """Positive control on the finder. Without it, a walker that matched
        nothing -- a renamed import, an `ast` API change -- would make the
        assertion below pass vacuously for every possible source file.
        """
        self.assertGreaterEqual(
            len(self._run_calls()), 4,
            "the AST walker found fewer subprocess.run call sites than this "
            "module is known to have; the walker is broken, not the module")

    def test_every_subprocess_run_passes_env_PATH_ENV(self):
        offenders = []
        for node in self._run_calls():
            env = [kw for kw in node.keywords if kw.arg == "env"]
            if not env:
                offenders.append((node.lineno, "no env= at all"))
                continue
            value = env[0].value
            if not (isinstance(value, ast.Name) and value.id == "PATH_ENV"):
                offenders.append((node.lineno, "env= is not PATH_ENV"))
        self.assertEqual(
            offenders, [],
            "subprocess.run call site(s) in sync_lib.py not hardened: %s -- "
            "the module docstring says every one of them must pass "
            "env=PATH_ENV" % (offenders,))


class TestBinOverrideGate(unittest.TestCase):
    """`SYNC_TEST_GIT_BIN` / `SYNC_TEST_GH_BIN` are a backdoor unless EVERY
    part of `_test_bin_override()`'s compound gate holds: the sandbox marker
    (`CLAUDE_TOOLKIT_CONFIG` redirected), an absolute path, and that path
    resolving inside the system temp directory. The marker alone is not proof:
    `CLAUDE_TOOLKIT_CONFIG` is also a real runtime override.
    Every test below removes `CLAUDE_TOOLKIT_CONFIG` from the real
    environment first and restores whatever was there, rather than assuming
    it is absent -- the property under test is the GATE, and a test that
    only works by accident of an empty ambient environment would prove
    nothing about it.
    """

    def _temp_shim(self):
        # A real temp-dir path, as every shim-based test uses. Need not exist.
        root = tempfile.mkdtemp(prefix="sync-gate-")
        self.addCleanup(shutil.rmtree, root, True)
        return os.path.join(root, "shim")

    def _without_sandbox_marker(self):
        saved = os.environ.pop("CLAUDE_TOOLKIT_CONFIG", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CLAUDE_TOOLKIT_CONFIG", saved)

    def test_git_override_is_ignored_without_the_sandbox_marker(self):
        self._without_sandbox_marker()
        with mock.patch.dict(os.environ, {"SYNC_TEST_GIT_BIN": "/some/shim"}):
            self.assertEqual(sync_lib._git_bin(), "git")

    def test_gh_override_is_ignored_without_the_sandbox_marker(self):
        self._without_sandbox_marker()
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": "/some/shim"}):
            self.assertEqual(sync_lib._gh_bin(), "gh")

    def test_git_override_is_ignored_when_relative_even_inside_a_sandbox(self):
        self._without_sandbox_marker()
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": "relative-shim",
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            self.assertEqual(sync_lib._git_bin(), "git")

    def test_gh_override_is_ignored_when_relative_even_inside_a_sandbox(self):
        self._without_sandbox_marker()
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GH_BIN": "relative-shim",
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            self.assertEqual(sync_lib._gh_bin(), "gh")

    def test_positive_control_git_override_applies_inside_a_sandbox_with_an_absolute_path(self):
        # Without this, the negative tests above would pass for free against
        # a gate that is wired up wrong -- e.g. if the override were dead
        # code and always fell through to "git" regardless of the gate.
        self._without_sandbox_marker()
        shim = self._temp_shim()
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": shim,
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            self.assertEqual(sync_lib._git_bin(), shim)

    def test_positive_control_gh_override_applies_inside_a_sandbox_with_an_absolute_path(self):
        self._without_sandbox_marker()
        shim = self._temp_shim()
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GH_BIN": shim,
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            self.assertEqual(sync_lib._gh_bin(), shim)

    def test_an_absolute_path_outside_the_temp_dir_is_ignored_even_with_the_marker(self):
        # The reviewer's repro: an ordinary alternate config (a real file the
        # user points CLAUDE_TOOLKIT_CONFIG at) plus a stray override naming a
        # system binary. Both executables must stay the hardened defaults.
        self._without_sandbox_marker()
        with tempfile.NamedTemporaryFile(suffix=".json") as config:
            with mock.patch.dict(os.environ, {
                "SYNC_TEST_GIT_BIN": "/bin/echo",
                "SYNC_TEST_GH_BIN": "/bin/echo",
                "CLAUDE_TOOLKIT_CONFIG": config.name,
            }):
                self.assertEqual(sync_lib._git_bin(), "git")
                self.assertEqual(sync_lib._gh_bin(), "gh")

    def test_a_temp_path_that_escapes_through_dotdot_is_ignored(self):
        self._without_sandbox_marker()
        escaping = os.path.join(tempfile.gettempdir(), "..", "..", "bin", "echo")
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": escaping,
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            self.assertEqual(sync_lib._git_bin(), "git")

    def test_an_empty_sandbox_marker_does_not_count_as_redirected(self):
        # "" is falsy -- `os.environ.get("CLAUDE_TOOLKIT_CONFIG")` returning
        # it must read the same as the variable being absent altogether.
        self._without_sandbox_marker()
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": "/abs/shim",
            "CLAUDE_TOOLKIT_CONFIG": "",
        }):
            self.assertEqual(sync_lib._git_bin(), "git")


class TestNeverRaisesOnBrokenConfig(ConfigTestCase):
    """The acceptance bar this task names explicitly: a missing/corrupt config
    file must never take a caller down. `CLAUDE_TOOLKIT_CONFIG=/nonexistent.json`
    (set by the verification command re-running this whole file as a
    subprocess) exercises the same path at import time; this exercises it
    in-process too, function by function.
    """

    def test_sync_enabled_returns_rather_than_raises(self):
        self._no_config()
        sync_lib.sync_enabled()  # must not raise

    def test_sync_root_returns_rather_than_raises(self):
        self._no_config()
        sync_lib.sync_root()  # must not raise

    def test_throttle_hours_returns_rather_than_raises(self):
        self._no_config()
        sync_lib.throttle_hours()  # must not raise

    def test_a_corrupt_config_file_does_not_raise(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write("{not valid json")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)
        sync_lib.sync_enabled()
        sync_lib.sync_root()
        sync_lib.throttle_hours()


def _rev_parse_head(repo):
    result = subprocess.run(
        ("git", "-C", repo, "rev-parse", "HEAD"), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.decode().strip()


def _write_git_shim(tmp_dir, *, sleep_on_arg=None, sleep_seconds=0):
    """An executable git shim that execs the real `git`, optionally sleeping
    first when `sleep_on_arg` appears anywhere in argv. Real git resolution
    inside the shim goes through the shell's own PATH lookup -- the shim
    itself runs under `PATH_ENV` (same as every call `sync_lib` makes), which
    already includes /usr/bin and /opt/homebrew/bin, so `exec git "$@"`
    reliably finds the real binary.
    """
    path = os.path.join(tmp_dir, "git-shim")
    lines = ["#!/bin/bash"]
    if sleep_on_arg:
        lines.append(
            'for a in "$@"; do if [ "$a" = {0} ]; then sleep {1}; fi; done'.format(
                shlex.quote(sleep_on_arg), sleep_seconds))
    lines.append('exec git "$@"')
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


def _write_logging_git_shim(tmp_dir, log_path):
    """An executable git shim that appends every invocation's argv to
    `log_path` before execing the real `git` -- the mechanism the
    zero-network-call tests use to prove `_pull()` was never reached, rather
    than merely asserting a return value that a wrongly-wired seam could
    satisfy for free.
    """
    path = os.path.join(tmp_dir, "git-shim")
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n")
        handle.write("printf '%s\\n' \"$*\" >> {0}\n".format(shlex.quote(log_path)))
        handle.write('exec git "$@"\n')
    os.chmod(path, 0o755)
    return path


def _read_shim_log(path):
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [line.strip() for line in handle if line.strip()]


class _FakeClock:
    """A stand-in for `sync_lib._now`, driven by `mock.patch.object(sync_lib,
    "_now", _FakeClock(...))` -- one reading per call, in order, so a
    deadline/budget test can force exactly which side of a deadline each
    `_now()` call lands on without any real time passing. The last reading
    repeats forever once exhausted, rather than raising `StopIteration`, so a
    call this test did not anticipate lands on a stable (already-expired-or-
    not) value instead of an opaque test-harness error.
    """

    def __init__(self, *readings):
        self._readings = list(readings)
        self._index = 0

    def __call__(self):
        value = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return value


def _write_gh_shim(tmp_dir, log_path, *, names=None, list_exit=0, clone_exit=0):
    """An executable `gh` shim: logs every invocation's argv to `log_path`,
    then fakes just enough of `gh repo list` / `gh repo clone` for
    `discover_clones()` to be exercised without a real `gh` or a real
    network call.

    `repo list <owner> -L 200 --json name --jq '.[].name'` prints each of
    `names` on its own line and exits `list_exit`. `repo clone <owner>/<name>
    <dest>` creates `<dest>/.git` (a plausible enough stand-in for a real
    clone) when `clone_exit` is 0, and exits `clone_exit` either way.
    Anything else exits 0 having done nothing -- there is no other `gh`
    subcommand this module calls. Deadline/budget tests no longer need this
    shim to sleep: `sync_lib._now` is mocked instead (see `_FakeClock`
    above), so every call here completes as fast as the real `git`/`gh`
    calls underneath it allow.
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


class TestDiscoverClonesRequiresAnOwner(unittest.TestCase):
    """The single most important property of this function: with no owner,
    it never even considers calling `gh`.

    EVERY case here runs through `SYNC_TEST_GH_BIN`, never bare -- `None`,
    `0`, and `False` are not valid `subprocess` argv members and would raise
    a `TypeError` before any process is even spawned, so testing only those
    would look like it proves "no gh call" while actually proving nothing
    about the code's own owner check. `""` IS a valid argv member: a version
    of this function missing the check would pass it straight to a real
    `gh repo list ""` on this machine's PATH (`gh` lives in `PATH_ENV`'s
    `/opt/homebrew/bin`) -- which is exactly what happened once while this
    module's mutation test was being developed, before this test carried its
    own shim. Never drop the shim from this test to "simplify" it.
    """

    def test_falsy_owner_returns_empty_and_makes_no_gh_call(self):
        for owner in (None, "", 0, False):
            tmp = tempfile.mkdtemp()
            log_path = os.path.join(tmp, "calls.log")
            shim = _write_gh_shim(tmp, log_path, names=["should-never-be-listed"])
            with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
                cloned, skipped = sync_lib.discover_clones("/some/root", owner)
            self.assertEqual(cloned, [], "owner={0!r}".format(owner))
            self.assertEqual(skipped, 0, "owner={0!r}".format(owner))
            self.assertEqual(
                _read_shim_log(log_path), [],
                "gh was invoked for a falsy owner={0!r}".format(owner))


class TestDiscoverClonesZeroGhCalls(unittest.TestCase):
    """Proved with a logging `gh` shim passed by ABSOLUTE PATH through an env
    seam (`SYNC_TEST_GH_BIN`) -- never through `PATH`, which `PATH_ENV`
    hardens to a fixed system list. A `PATH`-based shim would be invisible to
    this module, so the seam has to be the same shape as `SYNC_TEST_GIT_BIN`.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = os.path.join(self._tmp, "root")
        os.makedirs(self.root)
        self.log_path = os.path.join(self._tmp, "calls.log")
        self.shim = _write_gh_shim(self._tmp, self.log_path)

    def test_run_makes_zero_gh_calls_when_owner_is_unset(self):
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": self.shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root)
        self.assertEqual(result.cloned, [])
        self.assertEqual(
            _read_shim_log(self.log_path), [],
            "gh was invoked even though no owner was configured")

    def test_run_makes_zero_gh_calls_when_owner_is_an_empty_string(self):
        # `""` is the case that actually exercises `discover_clones`'s own
        # `if not owner` check -- unlike `None`, it is a valid `subprocess`
        # argv member, so this is the one falsy value a missing/broken check
        # would let all the way through to a real `gh` call.
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": self.shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root, owner="")
        self.assertEqual(result.cloned, [])
        self.assertEqual(
            _read_shim_log(self.log_path), [],
            "gh was invoked for an empty-string owner")

    def test_positive_control_run_does_call_gh_when_owner_is_set(self):
        # Without this, the "zero gh calls" test above would pass for free
        # against a seam that is wired up wrong -- e.g. if SYNC_TEST_GH_BIN
        # were silently ignored and `run()` never reached `discover_clones`
        # at all, or `discover_clones` never actually invoked `gh`.
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": self.shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            sync_lib.run(self.root, owner="some-owner")
        calls = _read_shim_log(self.log_path)
        self.assertTrue(
            calls, "the positive control never saw a gh call -- the "
            "SYNC_TEST_GH_BIN seam is not actually wired in, so the "
            "zero-calls test above would pass for a reason unrelated to "
            "the code under test")
        self.assertTrue(any("repo list" in call for call in calls), calls)


class TestDiscoverClonesSkipsExistingDirectories(unittest.TestCase):

    def test_a_name_already_present_as_a_directory_is_not_cloned(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        # "already-there" exists as a plain directory under root -- not
        # necessarily a git checkout, per the plan's own wording: an
        # existing directory is skipped whether or not it is a git repo.
        os.makedirs(os.path.join(root, "already-there"))
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["already-there", "new-one"])

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner")

        self.assertEqual(cloned, [os.path.join(root, "new-one")])
        self.assertEqual(skipped, 0)
        calls = _read_shim_log(log_path)
        self.assertFalse(
            any("clone" in call and "already-there" in call for call in calls),
            "an existing directory was cloned over: {0}".format(calls))


class TestDiscoverClonesDegradesOnGhFailure(unittest.TestCase):

    def test_a_nonzero_gh_list_exit_yields_empty_and_is_not_an_error(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["would-have-cloned"], list_exit=1)

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner")

        self.assertEqual(cloned, [])
        self.assertEqual(skipped, 0)


class TestDiscoverClonesActuallyClones(unittest.TestCase):

    def test_a_new_name_is_cloned_and_reported(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["brand-new"])

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner")

        expected = os.path.join(root, "brand-new")
        self.assertEqual(cloned, [expected])
        self.assertEqual(skipped, 0)
        self.assertTrue(os.path.isdir(os.path.join(expected, ".git")))

    def test_a_failed_clone_is_not_reported_and_does_not_raise(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["broken"], clone_exit=1)

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner")

        self.assertEqual(cloned, [])
        self.assertEqual(skipped, 0)
        self.assertFalse(os.path.exists(os.path.join(root, "broken")))


class TestDiscoverClonesRespectsTheDeadline(unittest.TestCase):
    """`deadline` (a `sync_lib._now()` value -- see `_FakeClock` above) is
    checked BEFORE each individual clone, the same idiom `run()`'s pull loop
    uses before each repo -- required because this function runs inside a
    SessionStart hook, where an unbounded clone phase would stall every
    prompt behind however many new repos an owner happens to have.
    """

    def test_a_deadline_that_expires_during_the_first_clone_stops_the_rest(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["alpha", "beta"])

        # First _now() call (before "alpha") reads BEFORE the deadline, so
        # alpha's clone is attempted; the second (before "beta") reads AFTER
        # it, so beta is never attempted. No sleeping and no real elapsed
        # time is needed to force that -- the clock itself is scripted.
        fake_clock = _FakeClock(0.0, 1.0)
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}), \
                mock.patch.object(sync_lib, "_now", fake_clock):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner", deadline=0.5)

        self.assertEqual(cloned, [os.path.join(root, "alpha")])
        self.assertEqual(
            skipped, 1,
            "the shortfall must be REPORTED, not silently dropped")
        calls = _read_shim_log(log_path)
        self.assertFalse(
            any("clone" in call and "beta" in call for call in calls),
            "beta's clone started even though the deadline had passed: {0}".format(calls))

    def test_a_deadline_already_past_before_the_loop_starts_clones_nothing(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["alpha", "beta"])

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            already_past = time.monotonic() - 1.0
            cloned, skipped = sync_lib.discover_clones(root, "some-owner", deadline=already_past)

        self.assertEqual(cloned, [])
        self.assertEqual(skipped, 2)
        calls = _read_shim_log(log_path)
        self.assertTrue(
            any("repo list" in call for call in calls),
            "the list call itself is not deadline-gated, matching how "
            "candidate_repos() is not gated in the pull loop")
        self.assertFalse(any("clone" in call for call in calls), calls)

    def test_no_deadline_means_unbounded_exactly_like_before_this_fix(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        log_path = os.path.join(tmp, "calls.log")
        shim = _write_gh_shim(tmp, log_path, names=["alpha", "beta"])

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            cloned, skipped = sync_lib.discover_clones(root, "some-owner")

        self.assertEqual(
            sorted(cloned),
            sorted([os.path.join(root, "alpha"), os.path.join(root, "beta")]))
        self.assertEqual(skipped, 0)


class StampTestCase(ConfigTestCase):
    """Sandboxes both `$HOME` (so `stamp_path()` never touches the real
    machine's cache) and config (so `throttle_hours()` reads its documented
    default rather than whatever this machine's own config happens to say).
    """

    def setUp(self):
        super().setUp()
        self._no_config()
        self._tmp_home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"HOME": self._tmp_home})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestStampPath(StampTestCase):

    def test_honours_home(self):
        self.assertEqual(
            sync_lib.stamp_path(),
            os.path.join(self._tmp_home, ".cache", "claude-repo-sync.stamp"))


class TestThrottleDue(StampTestCase):

    def test_absent_stamp_is_due(self):
        self.assertIs(sync_lib.throttle_due(), True)

    def test_aged_one_hour_under_the_throttle_is_not_due(self):
        sync_lib.record_run(now=1_000_000.0)
        hours = sync_lib.throttle_hours()
        later = 1_000_000.0 + (hours - 1) * 3600
        self.assertIs(sync_lib.throttle_due(now=later), False)

    def test_aged_one_hour_over_the_throttle_is_due(self):
        sync_lib.record_run(now=1_000_000.0)
        hours = sync_lib.throttle_hours()
        later = 1_000_000.0 + (hours + 1) * 3600
        self.assertIs(sync_lib.throttle_due(now=later), True)

    def test_a_garbage_stamp_is_due(self):
        path = sync_lib.stamp_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("not-a-timestamp")
        self.assertIs(sync_lib.throttle_due(), True)

    def test_a_future_dated_stamp_is_due(self):
        # Fails toward running: an idempotent fast-forward pull is cheap, and
        # a stamp that claims to be from the future cannot be trusted to mean
        # anything -- it is exactly the shape corruption would take.
        sync_lib.record_run(now=2_000_000.0)
        self.assertIs(sync_lib.throttle_due(now=1_000_000.0), True)

    def test_an_unreadable_stamp_is_due(self):
        sync_lib.record_run()
        os.chmod(sync_lib.stamp_path(), 0o000)
        self.addCleanup(os.chmod, sync_lib.stamp_path(), 0o600)
        self.assertIs(sync_lib.throttle_due(), True)


class TestRecordRun(StampTestCase):

    def test_writes_a_stamp_throttle_due_then_reads_as_not_due(self):
        sync_lib.record_run(now=1_000_000.0)
        self.assertTrue(os.path.exists(sync_lib.stamp_path()))
        self.assertIs(sync_lib.throttle_due(now=1_000_000.0 + 60), False)

    def test_an_unwritable_cache_dir_does_not_raise(self):
        cache_dir = os.path.join(self._tmp_home, ".cache")
        os.makedirs(cache_dir)
        os.chmod(cache_dir, 0o500)
        self.addCleanup(os.chmod, cache_dir, 0o700)
        sync_lib.record_run()  # must not raise

    # A test formerly lived here asserting that `record_run()` cannot undo a
    # `SyncResult` the caller already holds. Deleted 2026-08-26: it built the
    # `SyncResult` locally and never handed it to `record_run()`, which takes
    # no such argument -- the assertion was true by Python's object model for
    # any implementation `record_run()` could have, so no mutation of the
    # function under test could ever make it fail. Confirmed by mutating
    # `record_run()` to `return` immediately: three other tests in this class
    # failed, this one did not. The property itself (recording the stamp
    # cannot reach back and change a result already returned) is guaranteed
    # by `record_run()`'s signature -- it receives nothing to mutate -- so
    # there is nothing left for a test to exercise.


class SyncRunTestCase(unittest.TestCase):
    """Base for `run()`-level tests: a scannable `root` directory, plus a
    helper that builds one origin -> bare remote -> clone triple with the
    clone living directly under `root` so `candidate_repos()` finds it.
    """

    def setUp(self):
        if not GitRepoTestCase._git_available():
            self.skipTest("git not available")
        _give_real_git_room(self)
        self._tmp = tempfile.mkdtemp()
        self.root = os.path.join(self._tmp, "root")
        os.makedirs(self.root)

    def _build_repo(self, name):
        base = os.path.join(self._tmp, name)
        os.makedirs(base)
        origin = os.path.join(base, "origin")
        remote = os.path.join(base, "remote.git")
        clone = os.path.join(self.root, name)
        os.makedirs(origin)
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "T")
        commit(origin, "first")
        subprocess.run(("git", "clone", "-q", "--bare", origin, remote),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(("git", "clone", "-q", remote, clone),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "T")
        return origin, remote, clone

    def _advance_remote(self, remote, filename):
        """Push a new commit onto `remote`'s default branch from a throwaway
        pusher clone -- so an existing clone tracking `remote` becomes
        genuinely behind, the way a collaborator's push would leave it."""
        pusher = tempfile.mkdtemp()
        subprocess.run(("git", "clone", "-q", remote, pusher),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(pusher, "config", "user.email", "t@example.com")
        git(pusher, "config", "user.name", "T")
        commit(pusher, filename)
        git(pusher, "push", "-q", "origin", "main")


class TestRunCompletesItsPullPhaseWhenGhFails(SyncRunTestCase):
    """`gh` failing must never take the pull phase down with it -- discovery
    runs first in `run()`, so a failure there has to leave the rest of the
    function reachable.
    """

    def test_pull_phase_still_runs_and_completes_when_gh_list_fails(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        log_path = os.path.join(self._tmp, "calls.log")
        shim = _write_gh_shim(self._tmp, log_path, names=[], list_exit=1)

        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root, owner="some-owner")

        self.assertEqual(result.cloned, [])
        self.assertEqual(result.synced, [clone])


class TestRunFastForwardsACleanBehindRepo(SyncRunTestCase):

    def test_synced_and_working_tree_has_the_new_commit(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        result = sync_lib.run(self.root)
        self.assertEqual(result.synced, [clone])
        self.assertEqual(result.blocked, [])
        self.assertEqual(result.failed, [])
        self.assertTrue(os.path.exists(os.path.join(clone, "remote-update")))


class TestRunLeavesADirtyRepoUntouched(SyncRunTestCase):
    """The safety claim under test is "it cannot lose local work" -- which
    only the file's BYTES, not merely its `blocked` membership, can prove.
    """

    def test_dirty_repo_is_blocked_and_its_bytes_are_unchanged(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        target = os.path.join(clone, "first")
        original_bytes = b"first-DIRTY-LOCAL-EDIT"
        with open(target, "wb") as handle:
            handle.write(original_bytes)

        result = sync_lib.run(self.root)

        self.assertEqual(result.blocked, [(clone, "uncommitted changes")])
        self.assertEqual(result.synced, [])
        self.assertEqual(result.failed, [])
        with open(target, "rb") as handle:
            self.assertEqual(
                handle.read(), original_bytes,
                "the dirty file's bytes changed -- a blocked repo must "
                "never be touched")


class TestRunRefusesADivergedRepo(SyncRunTestCase):
    """The load-bearing test for `--ff-only`. Mutation-tested separately:
    deleting `--ff-only` from `_pull()`'s argv must make THIS test fail.
    """

    def test_diverged_repo_is_failed_and_head_is_unchanged(self):
        _, remote, clone = self._build_repo("alpha")
        # A local commit the remote does not have...
        commit(clone, "local-only")
        # ...and a DIFFERENT remote commit the clone does not have, on a
        # different file so an accidental (mutated) merge would succeed
        # cleanly rather than conflict. A conflict would leave HEAD
        # unchanged too, which would let this test pass for the wrong
        # reason even with `--ff-only` deleted.
        self._advance_remote(remote, "remote-only")
        # Auto-accept the merge commit message if `--ff-only` is ever
        # removed from the pull argv. Without this, an unmutated,
        # non-interactive `git merge` has no terminal to edit a commit
        # message on, so the mutated run would hang or abort on the
        # missing editor -- and the test would pass for the wrong reason
        # either way, never actually exercising whether HEAD moved.
        git(clone, "config", "core.editor", "true")

        before = _rev_parse_head(clone)
        result = sync_lib.run(self.root)
        after = _rev_parse_head(clone)

        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0][0], clone)
        self.assertTrue(result.failed[0][1], "expected a captured git message")
        self.assertEqual(result.blocked, [])
        self.assertEqual(result.synced, [])
        self.assertEqual(
            after, before,
            "HEAD moved -- --ff-only must have merged rather than refused")


class TestRunBlocksWithoutEligibilityFalseStates(SyncRunTestCase):

    def test_detached_head_is_blocked_and_makes_no_network_call(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        git(clone, "checkout", "-q", "--detach", "main")
        shim_dir = tempfile.mkdtemp()
        log_path = os.path.join(shim_dir, "calls.log")
        shim = _write_logging_git_shim(shim_dir, log_path)
        with mock.patch.dict(os.environ, {"SYNC_TEST_GIT_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root)
        self.assertEqual(result.blocked, [(clone, "detached HEAD")])
        calls = _read_shim_log(log_path)
        self.assertFalse(any("pull" in call for call in calls), calls)

    def test_no_upstream_is_blocked_and_makes_no_network_call(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        git(clone, "checkout", "-q", "-b", "no-tracking")
        shim_dir = tempfile.mkdtemp()
        log_path = os.path.join(shim_dir, "calls.log")
        shim = _write_logging_git_shim(shim_dir, log_path)
        with mock.patch.dict(os.environ, {"SYNC_TEST_GIT_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root)
        self.assertEqual(result.blocked, [(clone, "no upstream")])
        calls = _read_shim_log(log_path)
        self.assertFalse(any("pull" in call for call in calls), calls)


class TestRunMakesZeroNetworkCallsForABlockedRepo(SyncRunTestCase):
    """Proved with a logging shim passed by ABSOLUTE PATH through an env
    seam (`SYNC_TEST_GIT_BIN`) rather than through `PATH` -- `PATH_ENV`
    hardens `PATH` to a fixed system list, so a shim dropped into a test's
    own temp directory would be invisible to a bare `"git"` lookup and this
    proof would silently test nothing.
    """

    def test_a_dirty_repo_reaches_blocked_with_zero_pull_calls(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        with open(os.path.join(clone, "first"), "a") as handle:
            handle.write("dirty")
        shim_dir = tempfile.mkdtemp()
        log_path = os.path.join(shim_dir, "calls.log")
        shim = _write_logging_git_shim(shim_dir, log_path)

        with mock.patch.dict(os.environ, {"SYNC_TEST_GIT_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root)

        self.assertEqual(result.blocked, [(clone, "uncommitted changes")])
        calls = _read_shim_log(log_path)
        self.assertTrue(calls, "expected the local eligibility calls to be logged")
        self.assertFalse(
            any("pull" in call for call in calls),
            "a blocked repo reached the network: {0}".format(calls))

    def test_positive_control_an_eligible_repo_really_does_call_pull(self):
        # Without this, the "zero pull calls" assertion above would pass for
        # free against a seam that never actually intercepts anything --
        # e.g. if SYNC_TEST_GIT_BIN were silently ignored.
        _, remote, clone = self._build_repo("beta")
        self._advance_remote(remote, "remote-update")
        shim_dir = tempfile.mkdtemp()
        log_path = os.path.join(shim_dir, "calls.log")
        shim = _write_logging_git_shim(shim_dir, log_path)

        with mock.patch.dict(os.environ, {"SYNC_TEST_GIT_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}):
            result = sync_lib.run(self.root)

        self.assertEqual(result.synced, [clone])
        calls = _read_shim_log(log_path)
        self.assertTrue(
            any("pull" in call for call in calls),
            "the positive control never saw a pull call -- the shim seam "
            "is not actually wired in, so the zero-calls test above would "
            "pass for a reason unrelated to the code under test")


class TestRunKillsAHangingPull(SyncRunTestCase):
    """The one deliberate exception in this file: it tests that `subprocess`'s
    own `timeout=` actually kills a hanging child, which needs a REAL slow
    child -- there is no seam to fake that without stubbing out the very
    mechanism under test. Both numbers are a catastrophe bound, not a
    budget: `sleep_seconds` (16s) only has to comfortably outlast
    `SYNC_TEST_NET_TIMEOUT` (2s) so the kill is observed rather than the
    child exiting on its own first, and 2s is deliberately generous headroom
    over any scheduling jitter a saturated machine could introduce before
    `subprocess.run` even starts timing -- not a tight regression bound on
    how fast a real pull should be.
    """

    def test_a_hanging_pull_is_killed_by_net_timeout_and_run_still_returns(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        shim_dir = tempfile.mkdtemp()
        shim = _write_git_shim(shim_dir, sleep_on_arg="pull", sleep_seconds=16)

        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": shim,
            "SYNC_TEST_NET_TIMEOUT": "2",
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            result = sync_lib.run(self.root)

        self.assertEqual(result.synced, [])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0][0], clone)
        self.assertIn("timed out", result.failed[0][1])


def _write_slow_pull_shim(tmp_dir, slow_seconds_by_repo_name, log_path=None):
    """A git shim that sleeps during a `pull` whose `-C` target's basename is
    in `slow_seconds_by_repo_name`, around a call to the real `git`. Matching
    the `-C` path rather than any argv element is what lets two repos in one
    run get DIFFERENT delays -- `_write_git_shim` can only slow every pull
    equally, which cannot make workers finish out of candidate order.

    With `log_path`, each slowed pull appends `start <name>` before sleeping
    and `end <name>` after git returns. The ORDER of those lines is what the
    concurrency tests assert -- not elapsed time, which is ambient state.
    """
    path = os.path.join(tmp_dir, "slow-pull-shim")
    log = shlex.quote(log_path) if log_path else "/dev/null"
    lines = ["#!/bin/bash", 'target=""; is_pull=0; prev=""',
             'for a in "$@"; do',
             '  if [ "$prev" = "-C" ]; then target="$a"; fi',
             '  if [ "$a" = "pull" ]; then is_pull=1; fi',
             '  prev="$a"', "done",
             'name="$(basename "$target")"; delay=""',
             'if [ "$is_pull" = 1 ]; then', '  case "$name" in']
    for name, seconds in slow_seconds_by_repo_name.items():
        lines.append("    {0}) delay={1} ;;".format(shlex.quote(name), seconds))
    lines += ["  esac", "fi",
              'if [ -z "$delay" ]; then exec git "$@"; fi',
              'echo "start $name" >> {0}'.format(log),
              'sleep "$delay"',
              'git "$@"; status=$?',
              'echo "end $name" >> {0}'.format(log),
              'exit $status']
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


class TestRunPullsConcurrently(SyncRunTestCase):
    """The fix for a starved sync phase: pulls are network round-trips of
    ~0.7-2s each over SSH, so a serial loop under a whole-phase budget only
    ever reached the first repo or two -- always the same ones, since the
    candidate order is sorted.

    Overlap is asserted from the ORDER of start/end events the shim logs,
    never from elapsed time: concurrent means every pull started before any
    finished; serial means each start follows the previous end. The sleep
    only holds each pull open long enough for the order to be observable,
    and is far longer than admitting three repos takes.
    """

    SLOW = 1.5
    NAMES = ("alpha", "beta", "gamma")

    def _three_stale_repos(self):
        clones = []
        for name in self.NAMES:
            _, remote, clone = self._build_repo(name)
            self._advance_remote(remote, "remote-update")
            clones.append(clone)
        return clones

    def _run_with_shim(self, shim):
        # NET_BUDGET is pinned high so these tests measure concurrency and
        # ordering only -- left at the production value, a later budget cut
        # would fail the serial control with a message blaming the pool.
        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": shim,
            "SYNC_TEST_NET_TIMEOUT": "10",
            "SYNC_TEST_NET_BUDGET": "60",
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }):
            return sync_lib.run(self.root)

    def _events(self, log_path):
        with open(log_path) as handle:
            return [line.split() for line in handle.read().splitlines()]

    def test_pulls_overlap_rather_than_running_one_after_another(self):
        clones = self._three_stale_repos()
        log_path = os.path.join(self._tmp, "pulls.log")
        shim = _write_slow_pull_shim(
            self._tmp, {name: self.SLOW for name in self.NAMES}, log_path)

        result = self._run_with_shim(shim)

        self.assertEqual(result.synced, clones)
        self.assertEqual((result.failed, result.unchecked), ([], 0))
        events = self._events(log_path)
        self.assertEqual(len(events), 6, events)
        self.assertEqual(
            sorted(kind for kind, _ in events[:3]), ["start"] * 3,
            "a pull finished before all three had started -- they ran "
            "serially, not in parallel: %r" % events)

    def test_positive_control_one_worker_really_is_serial(self):
        # Without this, the overlap test above could pass because the shim
        # never held a pull open. Same fixture, pool forced to one slot:
        # every start must follow the previous pull's end.
        clones = self._three_stale_repos()
        log_path = os.path.join(self._tmp, "pulls.log")
        shim = _write_slow_pull_shim(
            self._tmp, {name: self.SLOW for name in self.NAMES}, log_path)

        with mock.patch.object(sync_lib, "MAX_PARALLEL_PULLS", 1):
            result = self._run_with_shim(shim)

        self.assertEqual(result.synced, clones)
        self.assertEqual(
            self._events(log_path),
            [[kind, name] for name in self.NAMES for kind in ("start", "end")])

    def test_results_keep_candidate_order_when_workers_finish_out_of_order(self):
        # alpha is slowest, so it finishes LAST -- `synced` must still list
        # it first, matching candidate order rather than completion order.
        # The pool is cut to TWO slots on purpose: completion order only
        # leaks in when admission has to wait for a free slot (beta finishes
        # first and is collected while alpha is still running). With a slot
        # per repo, collection happens in submission order regardless, and a
        # mutation emitting completion order went uncaught -- measured.
        clones = self._three_stale_repos()
        shim = _write_slow_pull_shim(self._tmp, {"alpha": self.SLOW})

        with mock.patch.object(sync_lib, "MAX_PARALLEL_PULLS", 2):
            result = self._run_with_shim(shim)

        self.assertEqual(result.synced, clones)


class TestRunStopsAdmittingAtTheDeadline(SyncRunTestCase):
    """What keeps a pool of concurrent pulls inside the hook timeout:
    `run()` waits for a free slot BEFORE checking the deadline, and checks
    the deadline before every admission. `TestTimeoutBudget` only does
    arithmetic on constants, so it cannot see either rule break. Two
    plausible regressions both stayed green before this test existed:
    checking the deadline BEFORE waiting for a slot (one extra pull admitted
    after the deadline), and dropping the slot wait entirely because "the
    executor already caps workers" (every repo admitted at once, so the
    budget stops nothing).

    This one has to use real time: the property IS "stop admitting once the
    deadline passes while waiting on a full pool", and a scripted `_now()`
    reads the same whether or not `run()` waited, so it passes both mutants.
    The margins are what keep it from being a budget assertion: the deadline
    lands at 2s and the first two pulls cannot end before 3s, so a slow
    machine only widens the gap the assertion relies on, except for the
    admission of the first two repos, which must happen inside 2s.
    """

    def test_admission_stops_at_the_deadline_while_the_pool_is_full(self):
        clones = []
        for name in ("a1", "a2", "a3", "a4", "a5", "a6"):
            _, remote, clone = self._build_repo(name)
            self._advance_remote(remote, "remote-update")
            clones.append(clone)
        shim = _write_slow_pull_shim(self._tmp, {name: 3 for name in
                                                 ("a1", "a2", "a3", "a4", "a5", "a6")})

        with mock.patch.dict(os.environ, {
            "SYNC_TEST_GIT_BIN": shim,
            "SYNC_TEST_NET_TIMEOUT": "10",
            "SYNC_TEST_NET_BUDGET": "2",
            "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG,
        }), mock.patch.object(sync_lib, "MAX_PARALLEL_PULLS", 2):
            result = sync_lib.run(self.root)

        # Two slots, both busy until ~3s; the next admission waits for a
        # slot, then finds the 2s deadline passed. Exactly the first two
        # pulled, the other four never started.
        self.assertEqual(
            (result.synced, result.unchecked, result.failed),
            (clones[:2], 4, []))


class TestRunRespectsTheWholePhaseBudget(SyncRunTestCase):

    def test_a_budget_smaller_than_one_repo_counts_the_rest_as_unchecked(self):
        _, remote_a, alpha = self._build_repo("alpha")
        self._advance_remote(remote_a, "remote-update")
        _, remote_b, beta = self._build_repo("beta")
        self._advance_remote(remote_b, "remote-update")

        # `_now()` (see `_FakeClock` above) is scripted instead of sleeping:
        # the first reading computes the deadline, the second (checked
        # before "alpha") is still inside it, the third (checked before
        # "beta", sorted after alpha) is already past it. Deterministic --
        # no real elapsed time, and no real budget shrinkage, required.
        fake_clock = _FakeClock(0.0, 0.0, 999.0)
        with mock.patch.object(sync_lib, "_now", fake_clock):
            result = sync_lib.run(self.root)

        self.assertEqual(result.unchecked, 1)
        seen = (set(result.synced) | {p for p, _ in result.blocked}
                | {p for p, _ in result.failed})
        self.assertIn(alpha, seen)
        self.assertNotIn(beta, seen)


class TestRunSharesTheBudgetAcrossCloneAndPullPhases(SyncRunTestCase):
    """The fix this class covers: `run()` computes ONE `NET_BUDGET` deadline,
    before `discover_clones()` even runs, and both phases check against that
    SAME deadline -- not each phase getting its own fresh budget. Without
    this, an owner with many new repos could stall a SessionStart hook well
    past the intended whole-phase bound before a single pull was ever
    attempted, because cloning is both the first phase to run and the slower
    one per call.

    Both tests here drive `sync_lib._now` with a scripted `_FakeClock`
    instead of a sleeping `gh` shim -- the property under test is which
    DEADLINE VALUE each phase compares against, not how much wall-clock time
    a slow clone actually takes, so a real sleep was never buying anything
    but flakiness.
    """

    def test_pull_phase_still_runs_after_clone_phase_consumes_part_of_the_budget(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        log_path = os.path.join(self._tmp, "calls.log")
        shim = _write_gh_shim(self._tmp, log_path, names=["brand-new"])

        # Readings: deadline computed at 0.0 (-> sync_lib.NET_BUDGET); the
        # clone-phase check and the pull-loop check are both scaled OFF that
        # constant, at 25% and 50% of it, rather than hardcoded -- so this
        # stays correct however NET_BUDGET is tuned, and both readings are
        # still inside the deadline.
        budget = sync_lib.NET_BUDGET
        fake_clock = _FakeClock(0.0, budget * 0.25, budget * 0.5)
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}), \
                mock.patch.object(sync_lib, "_now", fake_clock):
            result = sync_lib.run(self.root, owner="some-owner")

        self.assertEqual(result.cloned, [os.path.join(self.root, "brand-new")])
        self.assertEqual(result.cloned_unchecked, 0)
        self.assertEqual(
            result.synced, [clone],
            "the pull phase must still run and respect what budget remains "
            "after a clone phase that only consumed PART of it")
        self.assertEqual(result.unchecked, 0)

    def test_a_budget_exhausted_by_the_clone_phase_does_not_raise_and_leaves_the_pull_loop_unchecked(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        log_path = os.path.join(self._tmp, "calls.log")
        shim = _write_gh_shim(self._tmp, log_path, names=["brand-new"])

        # The clone-phase reading (half of NET_BUDGET) is still inside the
        # deadline, so the clone is allowed to start and DOES complete --
        # but the next reading, taken by the pull loop's own first check,
        # comes back at 999.0: already past the SAME deadline value. If a
        # regression gave the pull loop a fresh deadline of its own instead
        # of reusing this one, that extra `_now()` call would consume the
        # repeated final reading too and the loop would proceed rather than
        # stopping.
        fake_clock = _FakeClock(0.0, sync_lib.NET_BUDGET * 0.5, 999.0)
        with mock.patch.dict(os.environ, {"SYNC_TEST_GH_BIN": shim,
                                           "CLAUDE_TOOLKIT_CONFIG": _SANDBOX_CONFIG}), \
                mock.patch.object(sync_lib, "_now", fake_clock):
            result = sync_lib.run(self.root, owner="some-owner")  # must not raise

        # `candidate_repos()` now also sees "brand-new" (the shim really did
        # create a `.git` directory under it), so the pull loop has TWO
        # candidates -- alpha and brand-new -- and the exhausted deadline
        # means neither is looked at: unchecked == 2.
        self.assertEqual(result.cloned, [os.path.join(self.root, "brand-new")])
        self.assertEqual(
            result.unchecked, 2,
            "the pull loop must respect budget already spent by cloning, "
            "not start a fresh one of its own")
        self.assertEqual(result.synced, [])
        self.assertEqual(result.failed, [])


class TestRunNeverRaises(SyncRunTestCase):

    def test_root_removed_before_the_loop_starts(self):
        self._build_repo("alpha")
        shutil.rmtree(self.root)
        result = sync_lib.run(self.root)
        self.assertEqual(result.synced, [])
        self.assertEqual(result.blocked, [])
        self.assertEqual(result.failed, [])
        self.assertEqual(result.unchecked, 0)

    def test_a_repo_removed_between_listing_and_processing(self):
        _, _, clone = self._build_repo("alpha")
        with mock.patch.object(sync_lib, "candidate_repos", return_value=[clone]):
            shutil.rmtree(clone)
            result = sync_lib.run(self.root)
        self.assertEqual(result.blocked, [(clone, "could not read")])
        self.assertEqual(result.failed, [])

    def test_an_unreadable_dot_git_does_not_raise(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        git_dir = os.path.join(clone, ".git")
        os.chmod(git_dir, 0o000)
        self.addCleanup(os.chmod, git_dir, 0o700)
        result = sync_lib.run(self.root)
        self.assertEqual(result.blocked, [(clone, "could not read")])
        self.assertEqual(result.failed, [])

    def test_git_absent_from_path_env_does_not_raise(self):
        _, remote, clone = self._build_repo("alpha")
        self._advance_remote(remote, "remote-update")
        empty_bin_dir = tempfile.mkdtemp()
        poisoned_env = dict(sync_lib.PATH_ENV)
        poisoned_env["PATH"] = empty_bin_dir
        with mock.patch.object(sync_lib, "PATH_ENV", poisoned_env):
            result = sync_lib.run(self.root)
        self.assertEqual(result.blocked, [(clone, "could not read")])
        self.assertEqual(result.failed, [])


class TestTimeoutBudget(unittest.TestCase):
    """The REAL worst case for one `claude-repo-freshness` invocation's sync
    phase, not a subset of it. Same shape as `session-claim`'s
    `TestTimeoutBudget` (`claim_lib.py`), `base-pin`'s (`test_base_pin.py`)
    and `toolkit-integrity`'s (`test_toolkit_integrity.py`) -- the harness
    kills this hook at the timeout declared for it in
    `plugin/hooks/hooks.json`, and a kill mid-phase used to mean
    `record_run()` never ran (fixed separately, in `claude-repo-freshness`
    itself, by writing the stamp BEFORE the phase rather than in a `finally`
    after it). This test is about a DIFFERENT failure: whether the numbers
    even fit, regardless of that fix.

    `run()`'s deadline is checked only BETWEEN items, never during one, so it
    bounds how much NEW work may START -- not the wall-clock time of the
    call. One item already admitted when the deadline lands still runs to
    completion: `eligibility()`'s local git calls (GIT_TIMEOUT_SECONDS each)
    plus one `_pull()` (NET_TIMEOUT). Separately, `discover_clones()`'s
    `gh repo list` call is not deadline-gated at all -- it is the first thing
    `run()` does, before any check is possible.

    **Every call count below is DERIVED from `sync_lib.py`'s source via
    `ast`, never hardcoded.** A literal `3` for "eligibility's git calls" is
    prose in Python: the moment a fourth `_git(` call is added to
    `eligibility()` -- or a second `subprocess.run(` anywhere on this
    worst-case path -- the literal stops describing the code and the test
    stays green, which is the exact failure this test exists to prevent,
    arriving one refactor later. Counting call sites over-counts if a call
    site is not genuinely on the worst-case path, which is the safe
    direction: it forces whoever adds one to look at the budget.

    The declared timeout is read LIVE from `plugin/hooks/hooks.json`
    too, rather than restated as a literal here, for the identical reason.

    **SCOPE: this bounds the SYNC phase only.** One `claude-repo-freshness`
    invocation also runs the REPORT phase afterwards, on `freshness_lib`'s own
    separate `GIT_TIMEOUT_SECONDS`, at roughly four git calls per watched
    repo. `TestWholeHookTimeoutBudget` below is what asserts the WHOLE hook --
    both phases, against the one declared timeout -- fits; this class alone
    was never that, and is named for the budget rather than for the phase for
    exactly that reason.

    Scale, so nobody reads the report phase as urgent: on the live
    path it makes a handful of fast blocking git calls (12 worst case); you can
    time it on your own machine. Its worst case is no longer 12 calls at
    the old `GIT_TIMEOUT_SECONDS=5` (60s) -- this branch tightened that to 2
    AND wrapped the report loop in a `Deadline`, so the real bound is the
    25s `report_end` `TestWholeHookTimeoutBudget` derives below (against a
    28s ceiling), a wedged-repo or network-filesystem bound, not anything a
    person has sat through.

    The fetch-ordering defect this paragraph used to describe -- each repo's
    fetch spawning behind the previous repo's blocking calls -- is fixed by
    this branch: `spawn_due_fetches` now runs before the measurement loop
    (`claude-repo-freshness:121`), so a killed hook no longer leaves every
    later repo unfetched.
    """

    MARGIN = 2

    def _declared_timeout(self):
        # tests/test_sync_lib.py -> tests -> repo-freshness -> tools -> repo root
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        for row in hook_manifest.registrations(hook_manifest.load(repo)):
            if row["event"] == "SessionStart" and row["exe"] == "claude-repo-freshness":
                self.assertEqual("tools/repo-freshness/claude-repo-freshness",
                                 row["relpath"])
                return row["timeout"]
        return None

    @staticmethod
    def _call_name(call_node):
        """The dotted name a `Call` node invokes -- `"_git"` for a bare
        `_git(...)`, `"subprocess.run"` for `subprocess.run(...)`. `None`
        for anything else (a call through a variable, a comprehension, etc.),
        which simply never matches what `_count_calls` is looking for.
        """
        func = call_node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            parts = []
            while isinstance(func, ast.Attribute):
                parts.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                parts.append(func.id)
                return ".".join(reversed(parts))
        return None

    @classmethod
    def _count_calls(cls, func_name, callee_name):
        """How many `callee_name(...)` calls appear anywhere in the body of
        `func_name`, read straight from `sync_lib.py` via `ast` -- not
        counted by a hardcoded literal, and not a text grep, so a call
        spanning multiple lines or nested in a comprehension is still found.
        """
        with open(sync_lib.__file__) as handle:
            tree = ast.parse(handle.read(), filename=sync_lib.__file__)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return sum(
                    1 for inner in ast.walk(node)
                    if isinstance(inner, ast.Call)
                    and cls._call_name(inner) == callee_name)
        raise AssertionError(
            "function %r not found in %s" % (func_name, sync_lib.__file__))

    def test_hook_is_registered_on_session_start(self):
        """A tool nothing invokes is installed inert, and looks fine."""
        self.assertIsNotNone(
            self._declared_timeout(),
            "claude-repo-freshness missing from the plugin manifest "
            "SessionStart")

    def test_call_site_counts_match_current_expectations(self):
        """A canary, not the budget itself. If this drifts, a call site was
        added or removed on the worst-case path -- the PRECEDING test is
        what actually enforces the timeout and moves with it automatically;
        this one just proves `_count_calls` is not silently returning 0
        (which would make the budget test pass for a reason unrelated to the
        code under test) and gives the drift a name instead of a bare
        arithmetic failure.
        """
        self.assertEqual(self._count_calls("eligibility", "_git"), 3)
        self.assertEqual(self._count_calls("_pull", "subprocess.run"), 1)
        self.assertEqual(self._count_calls("_gh_repo_names", "subprocess.run"), 1)

    def test_the_sync_phase_worst_case_fits_inside_the_hook_timeout(self):
        declared = self._declared_timeout()
        self.assertIsNotNone(declared)

        eligibility_git_calls = self._count_calls("eligibility", "_git")
        pull_network_calls = self._count_calls("_pull", "subprocess.run")
        gh_list_network_calls = self._count_calls(
            "_gh_repo_names", "subprocess.run")

        one_item_post_check_cost = (
            eligibility_git_calls * sync_lib.GIT_TIMEOUT_SECONDS
            + pull_network_calls * sync_lib.NET_TIMEOUT)
        worst = (sync_lib.NET_BUDGET
                 + one_item_post_check_cost
                 # discover_clones()'s ungated `gh repo list`:
                 + gh_list_network_calls * sync_lib.NET_TIMEOUT)
        self.assertLessEqual(
            worst, declared - self.MARGIN,
            "sync phase worst case %ss vs %ss ceiling (%d eligibility git "
            "call(s), %d pull network call(s), %d gh-list network call(s))"
            % (worst, declared, eligibility_git_calls, pull_network_calls,
               gh_list_network_calls))


def _direct_calls(module_path, func_name, callee):
    """How many bare `callee(...)` calls appear directly in `func_name`'s
    body, read from `module_path` via `ast`. The general, module-path-taking
    counterpart of `TestTimeoutBudget._count_calls`, which stays hardcoded to
    `sync_lib.__file__` because every one of its callers -- `TestTimeoutBudget`
    itself and `_whole_hook_worst_case`'s sync-side terms below -- is
    genuinely sync-phase-only and never needs to count calls in
    `freshness_lib.py`. This is what lets `_transitive_calls` below reach
    `freshness_lib.py` instead.
    """
    with open(module_path) as handle:
        tree = ast.parse(handle.read(), filename=module_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return sum(
                1 for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and (TestTimeoutBudget._call_name(inner) or "").split(".")[-1]
                == callee)
    raise AssertionError(
        "function %r not found in %s" % (func_name, module_path))


def _transitive_calls(module_path, func_name, callee="_git"):
    """Worst-case `callee` invocations one call level below `func_name`.

    `measure_repos` reaches git only through module-level helpers, so counting
    direct `callee` calls in its body returns 0. Sum each helper's own direct
    count instead. Over-counts a helper that is not on the worst-case path,
    which is the safe direction: it forces whoever adds one to look at the
    budget.

    A call directly naming `callee` counts as 1 on the spot -- so calling
    this on one of the four helpers themselves (each of which calls `_git`
    directly) still returns 1, matching `_direct_calls`. Only a call to some
    OTHER function defined in the same module recurses -- exactly one level,
    via `_direct_calls` -- rather than continuing to chase calls-of-calls
    indefinitely.
    """
    with open(module_path) as handle:
        tree = ast.parse(handle.read(), filename=module_path)
    defined = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)}
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            target = node
            break
    if target is None:
        raise AssertionError(
            "function %r not found in %s" % (func_name, module_path))
    total = 0
    for inner in ast.walk(target):
        if not isinstance(inner, ast.Call):
            continue
        name = (TestTimeoutBudget._call_name(inner) or "").split(".")[-1]
        if name == callee:
            total += 1
        elif name in defined and name != func_name:
            total += _direct_calls(module_path, name, callee)
    return total


def _whole_hook_worst_case():
    """`(declared, sync_end, report_end, report_git_calls,
    report_deadline_seconds)` computed from the LIVE values of
    `sync_lib.NET_BUDGET` / `GIT_TIMEOUT_SECONDS` / `NET_TIMEOUT`,
    `freshness_lib.GIT_TIMEOUT_SECONDS`, and the hook module's own
    `REPORT_DEADLINE_SECONDS` -- read as module attributes at call time, so
    a caller that `mock.patch.object`s any of them beforehand sees it
    reflected here. Shared by `TestWholeHookTimeoutBudget`'s budget test and
    its sync-half liveness test, so both read the identical arithmetic.
    """
    declared = TestTimeoutBudget()._declared_timeout()
    eligibility_git_calls = TestTimeoutBudget._count_calls("eligibility", "_git")
    pull_network_calls = TestTimeoutBudget._count_calls("_pull", "subprocess.run")
    gh_list_network_calls = TestTimeoutBudget._count_calls(
        "_gh_repo_names", "subprocess.run")
    report_git_calls = _transitive_calls(freshness_lib.__file__, "measure_repos")

    # ADDITIVE, matching `TestTimeoutBudget.test_the_sync_phase_worst_case_
    # fits_inside_the_hook_timeout`'s own `worst` exactly -- a deliberate
    # over-estimate (the true bound is lower, since `gh repo list` burning its
    # full budget and the pull loop admitting nothing cannot both happen), and
    # the alternative -- a second, disagreeing model of the same phase in the
    # same test file -- is itself a defect.
    sync_end = (sync_lib.NET_BUDGET
                + eligibility_git_calls * sync_lib.GIT_TIMEOUT_SECONDS
                + pull_network_calls * sync_lib.NET_TIMEOUT
                + gh_list_network_calls * sync_lib.NET_TIMEOUT)
    report_deadline_seconds = hook_module.REPORT_DEADLINE_SECONDS
    report_end = (max(sync_end, report_deadline_seconds)
                  + report_git_calls * freshness_lib.GIT_TIMEOUT_SECONDS)
    return declared, sync_end, report_end, report_git_calls, report_deadline_seconds


class TestWholeHookTimeoutBudget(unittest.TestCase):
    """The property `TestTimeoutBudget` explicitly disclaims in its own
    docstring: not just the sync phase, but ONE `claude-repo-freshness`
    invocation -- sync phase AND report phase, sharing the one `Deadline`
    computed at hook start -- fits inside the single timeout
    `plugin/hooks/hooks.json` declares for it.

    `report_end`'s model, expected values `sync_end = 17`, `report_end = 25`
    against ceiling `30 - MARGIN 2 = 28` (headroom 3) with everything in
    place today, live in `_whole_hook_worst_case()` above rather than
    repeated here -- see its own docstring for the arithmetic and why it is
    additive.
    """

    MARGIN = TestTimeoutBudget.MARGIN

    def test_the_direct_only_count_for_measure_repos_is_zero(self):
        """Positive control, and the exact defect that killed the previous
        revision of this task: `measure_repos` reaches git only THROUGH
        `default_branch`/`current_branch`/`commits_behind`/`commits_ahead`,
        never directly -- so a derivation counting only DIRECT `_git` calls
        in its own body returns 0, which would make the budget assertion
        below pass for ANY declared timeout, for a reason unrelated to the
        code under test.
        """
        self.assertEqual(
            _direct_calls(freshness_lib.__file__, "measure_repos", "_git"), 0)

    def test_transitive_calls_sees_each_helper_directly_calling_git_once(self):
        """More positive controls, on `_transitive_calls` itself rather than
        on `measure_repos`. Without these, a counter that is simply broken --
        always returns 0, or always returns some other wrong constant -- could
        still make the canary below read 4 by accident.
        """
        for helper in ("default_branch", "current_branch",
                       "commits_behind", "commits_ahead"):
            self.assertEqual(
                _transitive_calls(freshness_lib.__file__, helper), 1, helper)

    def test_canary_report_git_calls_derives_to_four(self):
        """A canary, not the budget itself -- same role as `TestTimeoutBudget
        .test_call_site_counts_match_current_expectations`. If
        `_transitive_calls` regresses to returning 0 (the defect that killed
        the previous revision of this task) or to the direct-only count, THIS
        test fails -- rather than the budget assertion below silently passing
        for a reason unrelated to the code under test.
        """
        self.assertEqual(
            _transitive_calls(freshness_lib.__file__, "measure_repos"), 4)

    def test_the_whole_hook_worst_case_fits_its_declared_timeout(self):
        (declared, sync_end, report_end, report_git_calls,
         report_deadline_seconds) = _whole_hook_worst_case()
        self.assertIsNotNone(declared)

        self.assertLessEqual(
            report_end, declared - self.MARGIN,
            "whole-hook worst case %ss vs %ss ceiling -- sync_end %ss "
            "(sync_lib.NET_BUDGET=%s), claude-repo-freshness."
            "REPORT_DEADLINE_SECONDS=%ss, %d report-phase git call(s) at "
            "freshness_lib.GIT_TIMEOUT_SECONDS=%ss"
            % (report_end, declared, sync_end, sync_lib.NET_BUDGET,
               report_deadline_seconds, report_git_calls,
               freshness_lib.GIT_TIMEOUT_SECONDS))

    def test_the_sync_half_is_live_not_decorative(self):
        """Raising `sync_lib.NET_BUDGET` must push `report_end` over the
        ceiling -- proving the sync-phase term is genuinely ADDITIVE into
        `report_end`, not merely present as one operand of a `max()` a
        mutated sync side could never move. A previous revision used
        `max(sync_end, REPORT_DEADLINE_SECONDS)` as the WHOLE expression,
        discarding the sync term the moment it was smaller than the report
        deadline -- with `REPORT_DEADLINE_SECONDS` fixed at 13 and `sync_end`
        also at 13 at the time (NET_BUDGET was 2), that revision could not
        have told a `NET_BUDGET` of 2 from one of 6, making it a
        "cross-module" test that was single-module in effect.
        """
        raised = sync_lib.NET_BUDGET + 4  # 6 -> 10: pushes report_end 25 -> 29, past the 28 ceiling
        with mock.patch.object(sync_lib, "NET_BUDGET", raised):
            declared, _sync_end, report_end, _calls, _deadline = (
                _whole_hook_worst_case())
        self.assertGreater(
            report_end, declared - self.MARGIN,
            "raising sync_lib.NET_BUDGET to %s did not move report_end -- "
            "the sync half is decorative" % raised)


if __name__ == "__main__":
    unittest.main()
