#!/usr/bin/env python3
"""Unit tests for worktree_lib.py: the bounded git runner, context building,
config loading, and shell_dirname. All in-process -- the runner tests stub
subprocess.run and drive a fake clock, so no real git or filesystem timing is
involved.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import worktree_lib  # noqa: E402  (needs sys.path set up first)


def _fake_clock(*values):
    """Returns a zero-arg callable yielding `values` in order, one per call."""
    it = iter(values)
    return lambda: next(it)


def _completed(returncode, stdout):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    return result


class TestMakeRunnersGit(unittest.TestCase):
    """The bounded git runner: ok/failed/stalled/budget, truncation, and the
    POSIX child environment."""

    def test_ok_with_stripped_stdout(self):
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 7, False)
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"main\n\n")) as run:
            result = git("/r", ["rev-parse", "--show-toplevel"])
        self.assertEqual(result, worktree_lib.GitResult("ok", "main"))
        self.assertTrue(run.called)

    def test_nonzero_exit_is_failed(self):
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 7, False)
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(1, b"fatal: nope\n")):
            result = git("/r", ["status"])
        self.assertEqual(result, worktree_lib.GitResult("failed", "fatal: nope"))

    def test_oserror_is_failed(self):
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 7, False)
        with mock.patch("worktree_lib.subprocess.run",
                        side_effect=OSError("no such file")):
            result = git("/r", ["status"])
        self.assertEqual(result, worktree_lib.GitResult("failed", ""))

    def test_timeout_expired_is_stalled(self):
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 7, False)
        with mock.patch(
                "worktree_lib.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=2)):
            result = git("/r", ["status"])
        self.assertEqual(result, worktree_lib.GitResult("stalled", ""))

    def test_truncation_with_1_9_seconds_left_passes_timeout_1(self):
        # start=0.0, the call reads clock()=0.1 -> elapsed 0.1, budget 2.0 ->
        # left = 1.9. cap = min(GIT_TIMEOUT_SECONDS=2, 1.9) = 1.9, which must
        # truncate to 1, not round to 2 -- an untruncated cap would pass 1.9.
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 2.0, False)
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"")) as run:
            git("/r", ["status"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1)

    def test_0_9_seconds_left_is_budget_and_never_runs(self):
        # start=0.0, clock()=1.1 on the call -> elapsed 1.1, budget 2.0 ->
        # left = 0.9 -> icap = 0 -> budget, nothing attempted.
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 1.1), 2.0, False)
        with mock.patch("worktree_lib.subprocess.run") as run:
            result = git("/r", ["status"])
        self.assertEqual(result, worktree_lib.GitResult("budget", ""))
        run.assert_not_called()

    def test_posix_child_env_path(self):
        git, _ = worktree_lib.make_runners(_fake_clock(0.0, 0.1), 7, False)
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"")) as run:
            git("/r", ["status"])
        self.assertEqual(run.call_args.kwargs["env"]["PATH"],
                         worktree_lib.GIT_PATH_POSIX)

    def test_posix_make_runners_returns_no_msys_path(self):
        _, msys_path = worktree_lib.make_runners(_fake_clock(0.0), 7, False)
        self.assertIsNone(msys_path)


class TestContextFromEnvironmentBudgetOverride(unittest.TestCase):
    """The WORKTREE_TEST_BUDGET/WORKTREE_TEST_HARNESS compound gate: honoured
    only when BOTH are set. Observed through ctx.git's behaviour, since
    Context does not expose the resolved budget directly -- a budget small
    enough to exhaust immediately (<1s) proves the override took; the
    default (7s) proves it did not."""

    def _env_without_budget_vars(self):
        env = dict(os.environ)
        for key in ("WORKTREE_TEST_BUDGET", "WORKTREE_TEST_HARNESS"):
            env.pop(key, None)
        return env

    def test_both_set_overrides_to_a_small_budget(self):
        env = self._env_without_budget_vars()
        env["WORKTREE_TEST_BUDGET"] = "0.5"
        env["WORKTREE_TEST_HARNESS"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            ctx = worktree_lib.context_from_environment()
        with mock.patch("worktree_lib.subprocess.run") as run:
            result = ctx.git("/r", ["status"])
        self.assertEqual(result, worktree_lib.GitResult("budget", ""))
        run.assert_not_called()

    def test_budget_alone_without_harness_falls_back_to_7(self):
        env = self._env_without_budget_vars()
        env["WORKTREE_TEST_BUDGET"] = "0.5"
        with mock.patch.dict(os.environ, env, clear=True):
            ctx = worktree_lib.context_from_environment()
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"")) as run:
            result = ctx.git("/r", ["status"])
        self.assertEqual(result.status, "ok")
        run.assert_called_once()

    def test_harness_alone_without_budget_falls_back_to_7(self):
        env = self._env_without_budget_vars()
        env["WORKTREE_TEST_HARNESS"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            ctx = worktree_lib.context_from_environment()
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"")) as run:
            result = ctx.git("/r", ["status"])
        self.assertEqual(result.status, "ok")
        run.assert_called_once()

    def test_unparseable_override_falls_back_to_7(self):
        env = self._env_without_budget_vars()
        env["WORKTREE_TEST_BUDGET"] = "not-a-number"
        env["WORKTREE_TEST_HARNESS"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            ctx = worktree_lib.context_from_environment()
        with mock.patch("worktree_lib.subprocess.run",
                        return_value=_completed(0, b"")) as run:
            result = ctx.git("/r", ["status"])
        self.assertEqual(result.status, "ok")
        run.assert_called_once()


class TestLoadConfig(unittest.TestCase):

    HOME = "/home/testy"

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_missing_file(self):
        self.assertEqual(
            worktree_lib.load_config("/nonexistent/config.json", self.HOME),
            ("", ""))

    def test_invalid_json(self):
        path = self._write("{not json")
        self.assertEqual(worktree_lib.load_config(path, self.HOME), ("", ""))

    def test_top_level_array(self):
        path = self._write(json.dumps(["not", "an", "object"]))
        self.assertEqual(worktree_lib.load_config(path, self.HOME), ("", ""))

    def test_canonical_root_non_string(self):
        path = self._write(json.dumps({"worktree_guard": {"canonical_root": 5}}))
        self.assertEqual(worktree_lib.load_config(path, self.HOME), ("", ""))

    def test_only_workspace_root_set_canonical_stays_empty(self):
        # workspace_root must name a REAL directory here: a fallback-to-
        # workspace_root mutation would otherwise fail its own os.path.isdir
        # check and read as "" either way, hiding the mutation rather than
        # catching it.
        real_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(real_dir))
        path = self._write(json.dumps({"workspace_root": real_dir}))
        workspace_root, canonical_root = worktree_lib.load_config(path, self.HOME)
        self.assertEqual(workspace_root, real_dir)
        self.assertEqual(canonical_root, "")

    def test_home_and_trailing_slash_expansion(self):
        path = self._write(json.dumps({"workspace_root": "~/x/"}))
        workspace_root, _ = worktree_lib.load_config(path, self.HOME)
        self.assertEqual(workspace_root, self.HOME + "/x")

    def test_canonical_root_nonexistent_dir(self):
        path = self._write(json.dumps(
            {"worktree_guard": {"canonical_root": "/no/such/dir/at/all"}}))
        _, canonical_root = worktree_lib.load_config(path, self.HOME)
        self.assertEqual(canonical_root, "")

    def test_canonical_root_symlinked_dir_resolves_realpath(self):
        real_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(real_dir))
        link_dir = tempfile.mktemp()
        os.symlink(real_dir, link_dir)
        self.addCleanup(os.unlink, link_dir)
        path = self._write(json.dumps(
            {"worktree_guard": {"canonical_root": link_dir}}))
        _, canonical_root = worktree_lib.load_config(path, self.HOME)
        self.assertEqual(canonical_root, os.path.realpath(real_dir))


class TestShellDirname(unittest.TestCase):

    def test_table(self):
        cases = [
            ("f", "."),
            ("a/b", "a"),
            ("/a/b/", "/a"),
            ("/a", "/"),
            ("/", "/"),
            ("a/", "."),
        ]
        for given, expected in cases:
            self.assertEqual(worktree_lib.shell_dirname(given), expected,
                             "shell_dirname(%r)" % given)


class TestGuardLibIsLazy(unittest.TestCase):

    def test_guard_lib_not_imported_by_importing_worktree_lib(self):
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import worktree_lib\n"
            "assert 'guard_lib' not in sys.modules, sorted(sys.modules)\n"
            "print('OK')\n"
        ) % _DIR
        completed = subprocess.run([sys.executable, "-c", script],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)


class TestAStallIsNeverCached(unittest.TestCase):
    """One killed branch lookup must not exempt the rest of the command from
    Rule C. The per-target caches are real here (the zsh original's were
    lost inside `$(...)`), so a cached stall would answer every later
    statement against the same checkout with "can't tell" -> allow."""

    TOP = "/r/x"

    def _evaluate(self, stalls):
        """`git commit -am x ; git commit -am y` in a canonical checkout on
        its default branch, where the git calls named in `stalls` are killed
        the FIRST time they run and answer normally after that."""
        seen = set()
        answers = {
            "rev-parse --show-toplevel": "/r/x",
            "symbolic-ref --quiet --short HEAD": "main",
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD": "origin/main",
            "worktree list --porcelain": "worktree /r/x",
        }

        def git(target_dir, args):
            key = " ".join(args)
            if key in stalls and key not in seen:
                seen.add(key)
                return worktree_lib.GitResult("stalled", "")
            if key in answers:
                return worktree_lib.GitResult("ok", answers[key])
            return worktree_lib.GitResult("failed", "")

        ctx = worktree_lib.Context(home="/home/u", config_path="/nonexistent",
                                   windows=False, allow=False, git=git,
                                   clock=lambda: 0.0, msys_path=None)
        payload = {"tool_name": "Bash", "cwd": self.TOP,
                   "tool_input": {"command": "git commit -am x ; git commit -am y"}}
        with mock.patch("worktree_lib.load_config", return_value=("", "/r")):
            return worktree_lib.evaluate(payload, ctx)

    def test_the_fixture_blocks_with_no_stall(self):
        # Positive control: without a stall, the first commit is refused.
        self.assertTrue(self._evaluate(stalls=()).blocked)

    def test_a_stalled_current_branch_lookup_is_not_cached(self):
        self.assertTrue(self._evaluate(
            stalls=("symbolic-ref --quiet --short HEAD",)).blocked)

    def test_a_stalled_default_branch_lookup_is_not_cached(self):
        self.assertTrue(self._evaluate(
            stalls=("symbolic-ref --quiet --short refs/remotes/origin/HEAD",)).blocked)


class TestIsLinkedWorktree(unittest.TestCase):
    """The linked-worktree exclusion's own contract: git's two directories
    compared after resolving, an unreadable answer counted as NOT linked (so
    the exclusion never removes a refusal it cannot justify), a stall or a
    spent budget never cached, and no git call at all for a target outside
    the canonical root."""

    def _walk(self, answers, canonical_root="/r"):
        calls = []

        def git(target_dir, args):
            calls.append((target_dir, tuple(args)))
            status, out = answers.pop(0) if answers else ("failed", "")
            return worktree_lib.GitResult(status, out)

        ctx = worktree_lib.Context(home="/home/u", config_path="/nonexistent",
                                   windows=False, allow=False, git=git,
                                   clock=lambda: 0.0, msys_path=None)
        return worktree_lib._Walk(ctx, canonical_root), calls

    def test_a_primary_checkout_is_not_linked(self):
        walk, _ = self._walk([("ok", ".git\n.git")])
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/x"), False)

    def test_a_linked_worktree_is_linked(self):
        walk, _ = self._walk([("ok", "/r/x/.git/worktrees/y\n/r/x/.git")])
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/y"), True)

    def test_relative_and_absolute_spellings_of_one_directory_agree(self):
        walk, _ = self._walk([("ok", ".git\n/r/x/.git")])
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/x"), False)

    def test_an_unreadable_answer_is_not_linked(self):
        for status, out in (("failed", ""), ("stalled", ""), ("budget", ""),
                            ("ok", ""), ("ok", "/r/x/.git")):
            walk, _ = self._walk([(status, out)])
            self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/x"), False,
                          (status, out))

    def test_an_unreadable_answer_leaves_a_canonical_checkout_canonical(self):
        # The same property through the caller: not knowing must not switch
        # Rule C off for an immediate child of the root.
        walk, _ = self._walk([("failed", "")])
        self.assertIs(worktree_lib.is_canonical_checkout(walk, "/r/x"), True)

    def test_a_stall_is_not_cached_but_an_answer_is(self):
        walk, calls = self._walk([("stalled", ""),
                                  ("ok", "/r/x/.git/worktrees/y\n/r/x/.git")])
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/y"), False)
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/y"), True)
        self.assertIs(worktree_lib.is_linked_worktree(walk, "/r/y"), True)
        self.assertEqual(len(calls), 2, calls)

    def test_a_target_outside_the_root_costs_no_git_call(self):
        walk, calls = self._walk([("ok", "/r/x/.git/worktrees/y\n/r/x/.git")])
        self.assertIs(worktree_lib.is_canonical_checkout(walk, "/elsewhere/x"), False)
        self.assertEqual(calls, [])
        # Positive control: a target under the root does make the call.
        self.assertIs(worktree_lib.is_canonical_checkout(walk, "/r/y"), False)
        self.assertEqual(len(calls), 1, calls)


if __name__ == "__main__":
    unittest.main(verbosity=1)
