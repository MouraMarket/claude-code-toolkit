#!/usr/bin/env python3
"""install.sh refuses a relocated CLAUDE_CONFIG_DIR before it changes anything.

- Every mode that writes (a plain run, --setup, --autonomous, --update,
  --rollback, --uninstall --yes) refuses, and the whole sandbox HOME, the
  `claude` shim's call log and the checkout's HEAD are unchanged.
- Every read-only mode (--check, --links-only, --manifest, --verify-live,
  --version, --uninstall without --yes) reports on stderr and carries on;
  --check also fails with the required-failure line.
- An equivalent spelling of ~/.claude lets a writing mode run, and a symlink
  to somewhere else does not. (The spellings under --check are asserted in
  test_install_config_dir.py.)

Built on test_install_preflight.py's fixture and whole-HOME snapshot().
"""

import os
import subprocess
import unittest

import install_fixture as fx
from test_install_preflight import PreflightTestCase, snapshot

REFUSAL = "refusing to install: CLAUDE_CONFIG_DIR is set to"
REPORT = "so none of this install is active"
FAILURE_LINE = "CLAUDE_CONFIG_DIR points away from ~/.claude, so none of this install is active"

WRITING_MODES = [
    (),
    ("--setup",),
    ("--autonomous",),
    ("--update",),
    ("--rollback",),
    ("--uninstall", "--yes"),
]
READ_ONLY_MODES = [
    ("--check",),
    ("--links-only",),
    ("--manifest",),
    ("--verify-live",),
    ("--version",),
    ("--uninstall",),
]


class RelocationTestCase(PreflightTestCase):

    def setUp(self):
        super().setUp()
        self.seed({"workspace_root": self.workspace})
        self.elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(self.elsewhere)

    def run_install(self, *args, config_dir=None):
        env = dict(os.environ)
        env["HOME"] = self.home
        env.pop("CLAUDE_TOOLKIT_CONFIG", None)
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        env = fx.shim_env_for_home(env, self.home)
        env["PATH"] = self.fake_bin + os.pathsep + env["PATH"]
        return subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, universal_newlines=True).stdout.strip()


class TestWritingModesRefuse(RelocationTestCase):

    def test_each_writing_mode_refuses_and_changes_nothing(self):
        head = self.head()
        self.assertTrue(head, "the fixture is not a git checkout")
        before = snapshot(self.home)
        for args in WRITING_MODES:
            with self.subTest(args=args):
                proc = self.run_install(*args, config_dir=self.elsewhere)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(REFUSAL, proc.stderr)
                self.assertIn(self.elsewhere, proc.stderr)
                self.assertIn("Nothing was changed.", proc.stderr)
                self.assertIn("unset CLAUDE_CONFIG_DIR", proc.stderr)
                self.assertChangedNothing(before, proc)
                self.assertEqual(self.head(), head)

    def test_refusal_comes_before_any_other_output(self):
        """Before the link calls: no section of the run was printed."""
        proc = self.run_install(config_dir=self.elsewhere)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")


class TestReadOnlyModesReportAndContinue(RelocationTestCase):

    def test_each_read_only_mode_reports_on_stderr_and_is_not_refused(self):
        before = snapshot(self.home)
        for args in READ_ONLY_MODES:
            with self.subTest(args=args):
                proc = self.run_install(*args, config_dir=self.elsewhere)
                self.assertNotIn(REFUSAL, proc.stderr)
                self.assertNotIn("Nothing was changed.", proc.stderr)
                self.assertIn(REPORT, proc.stderr)
                self.assertIn(self.elsewhere, proc.stderr)
                if args != ("--check",):  # --check's summary repeats it on stdout
                    self.assertNotIn(REPORT, proc.stdout)
                self.assertEqual(snapshot(self.home), before, proc.stdout + proc.stderr)

    def test_check_carries_on_past_the_report(self):
        proc = self.run_install("--check", config_dir=self.elsewhere)
        self.assertIn("Directories", proc.stdout)

    def test_check_fails_with_the_required_failure_line(self):
        proc = self.run_install("--check", config_dir=self.elsewhere)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(FAILURE_LINE, proc.stdout)

    def test_check_without_relocation_has_no_such_line(self):
        proc = self.run_install("--check")
        self.assertNotIn(FAILURE_LINE, proc.stdout)
        self.assertNotIn(REPORT, proc.stderr)


class TestEquivalentSpellingsAndWritingModes(RelocationTestCase):

    def test_a_symlink_is_not_exempt_when_it_points_elsewhere(self):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        link = os.path.join(self.root, "other-link")
        os.symlink(self.elsewhere, link)
        proc = self.run_install(config_dir=link)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)

    def test_an_equivalent_spelling_lets_a_writing_mode_run(self):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        proc = self.run_install(config_dir=os.path.join(self.home, ".claude") + "/")
        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn("Directories", proc.stdout)


if __name__ == "__main__":
    unittest.main()
