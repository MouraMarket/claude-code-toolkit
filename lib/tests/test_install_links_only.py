#!/usr/bin/env python3
"""Tests for `install.sh --links-only`.

**Why this flag exists.** A machine that pulled a newly added tool and never
ran the installer has no symlink for it, and a check that enumerates the
install directories cannot see an entry that was never created. `--check`
already knows the answer, but it runs every suite in the repo first, and the
link work is under a second of that.

**What the design turns on, and what these tests therefore have to pin.** The
flag REUSES the `link` calls in report mode rather than reading a list of
them: a checker that parsed or copied the list would rebuild the hazard
`EXECUTABLE_NAMES` already demonstrates, a hand-maintained declaration that
can drift from the calls. So these tests drive **the real `install.sh`**, and
two properties matter:

  1. the exit status tracks reality: 0 only when every link is in place;
  2. it stops before the plugin step and the tests, which is the entire
     reason it is fast enough that someone will actually run it.

Property 2 is asserted against a full `--check` run rather than in isolation:
"the output lacks a Tests header" passes trivially if the header is renamed, so
the control run proves the header is there to be missing. No count of links
or suites appears here: each is derived from the files themselves.

`HOME` is redirected, so nothing here touches the real machine.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# Two tools this checkout ships, used as "one link" in the cases below.
ONE_TOOL = ("tools/memory-lint/claude-memory-lint", "claude-memory-lint")
OTHER_TOOL = ("tools/caffeinate/claude-caffeinate", "claude-caffeinate")


def _bash_available():
    return shutil.which("bash") is not None


class LinksOnlySandbox(unittest.TestCase):
    """A throwaway HOME plus a throwaway copy of the repo."""

    def setUp(self):
        if not _bash_available():
            self.skipTest("bash not available")
        if not os.path.isfile(os.path.join(REPO, "install.sh")):
            self.skipTest("install.sh not found next to lib/")

        self.tmp = tempfile.mkdtemp(prefix="install-links-only-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")

        # Strip `tests` so the installer's suite runner finds nothing: without
        # this a full --check run inside this test runs the whole toolkit suite.
        # load_guard_lib.py is left out so the shim step has nothing to import and
        # stands down: these suites assert a clean or exact --check, and which
        # shims it proposes would depend on which heavy tools the machine has on PATH.
        # The shim step has its own suite (test_install_load_guard_shims.py).
        shutil.copytree(
            REPO, self.repo,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".claude", "tests",
                                          "__pycache__", "load_guard_lib.py"))
        fx.stub_pending_components(self.repo)
        os.makedirs(self.home + "/.local/bin", exist_ok=True)

    def run_install(self, *args, repo=None):
        repo = repo or self.repo
        env = dict(os.environ)
        # The redirected HOME is what makes this hermetic: install.sh reads
        # $HOME/.config/claude-code-toolkit/config.json, so HOME is the lever.
        env["HOME"] = self.home
        env.pop("CLAUDE_CONFIG_DIR", None)
        env = fx.shim_env_for_home(env, self.home)
        if "--check" in args:
            fx.seed_home_installed(self.home, repo)
        return subprocess.run(
            ["bash", os.path.join(repo, "install.sh")] + list(args),
            capture_output=True, text=True, env=env, cwd=repo,
            # REQUIRED, not tidiness: capture_output leaves stdin INHERITED, and
            # install.sh prompts when it sees a TTY and no config. Run from a
            # real terminal without this, the test blocks forever and silently
            # eats the operator's keystrokes.
            stdin=subprocess.DEVNULL,
            timeout=600)

    def install_everything(self):
        """A real apply, so the sandbox HOME has every link in place."""
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def link_path(self, name):
        return os.path.join(self.home, ".local/bin", name)


class TestTheExitStatusTracksReality(LinksOnlySandbox):

    def test_a_fully_linked_machine_exits_zero(self):
        self.install_everything()
        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("every link is in place", proc.stdout)

    def test_one_removed_link_is_enough_to_fail(self):
        """The case the flag exists for, minus the pull: a single entry point
        with nothing pointing at it. One, not all: a checker that only noticed
        a wholly uninstalled machine would miss every real occurrence, which is
        exactly one new tool arriving in a pull."""
        self.install_everything()
        gone = self.link_path(ONE_TOOL[1])
        self.assertTrue(os.path.lexists(gone), "fixture never created the link")
        os.remove(gone)

        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("1 link(s) not in place", proc.stdout)
        self.assertIn(ONE_TOOL[1], proc.stdout)

    def test_an_uninstalled_machine_counts_every_link(self):
        """No install first. The count is asserted against the `link` calls
        themselves rather than a number typed here, so adding a tool cannot
        quietly leave this test measuring an old inventory."""
        with open(os.path.join(self.repo, "install.sh"), encoding="utf-8") as handle:
            calls = sum(1 for line in handle if line.startswith('link "'))
        self.assertGreater(calls, 0, "no link calls found to compare against")

        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("%d link(s) not in place" % calls, proc.stdout)

    def test_it_reports_without_creating_anything(self):
        """`--links-only` implies `--check`. A checker with a side effect makes
        the second run disagree with the first, and the first is the true one."""
        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertFalse(os.path.lexists(self.link_path(OTHER_TOOL[1])),
                         "--links-only created a link")

        again = self.run_install("--links-only")
        self.assertEqual(again.returncode, 1, again.stdout + again.stderr)


class TestEveryDriftBranchReachesTheExitStatus(LinksOnlySandbox):
    """`link`'s branches other than "needs creating", which a fixture built
    only from "remove a link and re-run" never reaches. Each starts from a
    machine where every other link is settled, puts exactly ONE path into the
    state under test, and asserts exit 1, the path NAMED, and a count of one."""

    def test_a_link_source_missing_from_the_repo_counts(self):
        self.install_everything()
        target = os.path.join(self.repo, ONE_TOOL[0])
        self.assertTrue(os.path.isfile(target), "fixture is stale")
        os.remove(target)

        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("missing in repo, skipped", proc.stdout)
        self.assertIn(ONE_TOOL[0], proc.stdout)
        self.assertIn("1 link(s) not in place", proc.stdout)

    def test_a_real_file_in_the_way_counts_and_is_kept(self):
        """A real file where a link belongs is never replaced, and it still
        leaves that entry point uninstalled, so it counts."""
        self.install_everything()
        path = self.link_path(ONE_TOOL[1])
        self.assertTrue(os.path.islink(path), "fixture did not link it")
        os.remove(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("mine\n")

        proc = self.run_install("--links-only")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("exists and is a real file", proc.stdout)
        self.assertIn(ONE_TOOL[1], proc.stdout)
        self.assertIn("1 link(s) not in place", proc.stdout)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual("mine\n", handle.read())


class TestAWorktreeRunSaysSoInsteadOfReportingACount(LinksOnlySandbox):
    """Run from a linked worktree, `link()` compares each symlink against THAT
    worktree's $REPO, so on a fully installed machine every link reads as
    absent and the flag exits 1, indistinguishable from a machine that never
    installed. Concurrent sessions are pushed into worktrees, and a one-second
    check that cries wolf from there is a check nobody runs twice.
    """

    def make_worktree(self):
        """A real linked worktree of the sandbox repo copy.

        `core.hooksPath` is pointed at an empty directory because this machine
        may set it GLOBALLY (`scripts/setup-git.sh` does exactly that), and a
        global pre-commit hook could then reject this fixture's commit. That is
        ambient state deciding whether a test can run, and the local setting
        overrides the global one for every hook.
        """
        sp = subprocess
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True,
               capture_output=True)
        for k, v in (("user.email", "t@e.com"), ("user.name", "T"),
                     ("commit.gpgsign", "false"),
                     ("core.hooksPath", os.path.join(self.tmp, "no-hooks"))):
            sp.run(["git", "config", k, v], cwd=self.repo, check=True,
                   capture_output=True)
        os.makedirs(os.path.join(self.tmp, "no-hooks"), exist_ok=True)
        sp.run(["git", "add", "-A"], cwd=self.repo, check=True,
               capture_output=True)
        sp.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo,
               check=True, capture_output=True)
        wt = os.path.join(self.tmp, "wt")
        sp.run(["git", "worktree", "add", "-q", "-b", "side", wt],
               cwd=self.repo, check=True, capture_output=True)
        return wt

    def assert_names_the_worktree(self, proc):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("LINKED WORKTREE", proc.stdout)
        self.assertIn("says nothing", proc.stdout)
        self.assertNotIn("link(s) not in place", proc.stdout)

    def test_a_worktree_run_names_the_worktree_rather_than_a_count(self):
        self.install_everything()                    # every link IS in place
        wt = self.make_worktree()
        self.assert_names_the_worktree(self.run_install("--links-only", repo=wt))

    def test_a_never_installed_worktree_names_the_worktree_too(self):
        """Nothing installed at all: the worktree branch must not depend on an
        earlier apply having left links to compare against."""
        wt = self.make_worktree()
        self.assert_names_the_worktree(self.run_install("--links-only", repo=wt))

    def test_the_primary_checkout_still_gets_the_ordinary_message(self):
        """The control. Without it the worktree branch could fire everywhere and
        a genuinely uninstalled machine would be told to go somewhere else."""
        proc = self.run_install("--links-only")      # nothing installed
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("link(s) not in place", proc.stdout)
        self.assertNotIn("LINKED WORKTREE", proc.stdout)

    def test_a_worktree_with_everything_linked_is_still_not_called_clean(self):
        """The other direction: the early `ok` path must not be reachable from a
        worktree by accident. If it were, the flag would report a clean machine
        from a tree whose links point somewhere else entirely."""
        self.install_everything()
        wt = self.make_worktree()
        proc = self.run_install("--links-only", repo=wt)
        self.assertNotIn("every link is in place", proc.stdout)


class TestItStopsBeforeTheExpensiveHalf(LinksOnlySandbox):
    """Each of these pairs the absent section with a control run that has it.

    Asserting only the absence passes trivially if a header is renamed or a
    section is deleted outright, which would report a flag working while it had
    quietly stopped checking anything.
    """

    def assert_section_skipped(self, header):
        full = self.run_install("--check")
        self.assertIn(header, full.stdout,
                      "control failed: --check no longer prints %r, so its "
                      "absence below proves nothing" % header)
        links = self.run_install("--links-only")
        self.assertNotIn(header, links.stdout)

    def test_it_does_not_reach_the_plugin_step(self):
        self.assert_section_skipped("Hooks -> profile plugins")

    def test_it_does_not_reach_the_test_suite(self):
        """The several minutes. This is the whole saving."""
        self.assert_section_skipped("Tests")

    def test_it_does_not_report_on_settings(self):
        """Not a symlink finding. A flag named links-only that prints other
        findings is one whose output has to be re-read to be trusted."""
        self.assert_section_skipped("Permissions -> ~/.claude/settings.json")

    def test_it_still_reaches_every_link_section(self):
        """The control for this whole class: the early exit is placed after the
        last `link` call, and an exit one section too high would make all three
        tests above pass while checking almost nothing."""
        proc = self.run_install("--links-only")
        self.assertIn("Executables -> ~/.local/bin", proc.stdout)
        self.assertIn(ONE_TOOL[1], proc.stdout)


class TestTheOtherModesAreUnchanged(LinksOnlySandbox):
    """`--links-only` adds a counter to `link`, which every mode runs."""

    def test_a_plain_apply_still_succeeds_and_links(self):
        proc = self.install_everything()
        self.assertIn("Tests", proc.stdout)
        self.assertTrue(os.path.lexists(self.link_path(OTHER_TOOL[1])))

    def test_a_plain_check_reports_absent_links_as_a_required_failure(self):
        """Nothing is installed, so every link is absent: `--check` still
        reports the rest of the machine, then exits 1 on the required-component
        block."""
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Required components failed:", proc.stdout)
        self.assertIn("link(s) not in place", proc.stdout)
        self.assertIn("Tests", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
