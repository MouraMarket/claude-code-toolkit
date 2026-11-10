#!/usr/bin/env python3
"""Behavioural tests for install.sh's CLAUDE_CONFIG_DIR relocation check under --check.

install.sh writes everything to ~/.claude. When CLAUDE_CONFIG_DIR points
anywhere else, Claude Code reads from there instead and the install is inert,
so install.sh says so on STDERR before anything else, and `--check` also fails
with a required-failure line on stdout. (The writing modes' refusal and the other
read-only modes are in test_install_relocation.py.) These drive the REAL install.sh through
`subprocess` against a minimal fixture repo with `$HOME` sandboxed, and set
CLAUDE_CONFIG_DIR explicitly in every case, never inherited from the
runner's own shell.
"""

import os
import subprocess
import tempfile
import unittest

import install_fixture as fx

TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
LIB_DIR = os.path.dirname(TESTS_DIR)
INSTALL_SH = os.path.join(os.path.dirname(LIB_DIR), "install.sh")
WORKTREE_DETECT_SH = os.path.join(LIB_DIR, "worktree_detect.sh")

MARKER = "CLAUDE_CONFIG_DIR is set to"


def git(*args, cwd):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    for key in [k for k in env if k.startswith("GIT_") and k not in (
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")]:
        del env[key]
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                   capture_output=True, env=env)


class ConfigDirTripwireTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.home = os.path.join(root, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        config_dir = os.path.join(self.home, ".config", "claude-code-toolkit")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w") as handle:
            handle.write("{}\n")

        self.repo = os.path.join(root, "fixture")
        for sub in ("claude", "tools", "lib"):
            os.makedirs(os.path.join(self.repo, sub))
        os.symlink(INSTALL_SH, os.path.join(self.repo, "install.sh"))
        os.symlink(WORKTREE_DETECT_SH,
                   os.path.join(self.repo, "lib", "worktree_detect.sh"))
        fx.add_plugin_layout(self.repo)
        # A missing link source or an absent link would fail --check on its own.
        fx.add_link_targets(self.repo, self.home)
        with open(os.path.join(self.repo, "claude", "permissions.baseline.json"), "w") as h:
            h.write('{"permissions": {}}\n')
        git("init", "-q", "-b", "main", self.repo, cwd=root)
        git("-c", "user.email=t@example.com", "-c", "user.name=Test",
            "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def run_install(self, config_dir, args=("--check",)):
        env = dict(os.environ, HOME=self.home,
                   CLAUDE_TOOLKIT_CONFIG=os.path.join(self._tmp.name, "none.json"))
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env = fx.shim_env_for_home(env, self.home)
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        if "--check" in args:
            fx.seed_home_installed(self.home, self.repo)
            fx.install_links(self.repo, self.home)
        proc = subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)
        return proc

    def assert_ran_to_the_end(self, proc):
        # Positive control: a run that died early would also print no warning.
        self.assertIn("Check complete", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def assert_failed_at_the_end(self, proc):
        # A relocated config dir is a required failure under --check: exit 1,
        # and the block that lists it is printed last.
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Required components failed:", proc.stdout)
        self.assertIn("CLAUDE_CONFIG_DIR points away from ~/.claude", proc.stdout)

    def assert_silent(self, proc):
        self.assert_ran_to_the_end(proc)
        self.assertNotIn(MARKER, proc.stdout)
        self.assertNotIn(MARKER, proc.stderr)

    def test_a_relocated_config_dir_warns_on_stderr(self):
        elsewhere = os.path.join(self._tmp.name, "relocated")
        os.makedirs(elsewhere)
        proc = self.run_install(elsewhere)
        self.assert_failed_at_the_end(proc)
        self.assertIn(MARKER + " " + elsewhere, proc.stderr)
        self.assertIn("none of this install is active", proc.stderr)
        self.assertNotIn(MARKER, proc.stdout)

    def test_a_config_dir_that_does_not_exist_yet_still_warns(self):
        proc = self.run_install(os.path.join(self._tmp.name, "not-made"))
        self.assert_failed_at_the_end(proc)
        self.assertIn(MARKER, proc.stderr)

    def test_the_report_precedes_the_first_section(self):
        # Printed first, before the checks that follow it can bury it.
        elsewhere = os.path.join(self._tmp.name, "relocated")
        os.makedirs(elsewhere)
        proc = self.run_install(elsewhere)
        self.assertIn(MARKER, proc.stderr)
        self.assertIn("Directories", proc.stdout)
        # Streams are separate, so also prove nothing was printed first: the
        # report is the whole of stderr's start and stdout opens at the heading.
        self.assertTrue(proc.stderr.lstrip().startswith("\x1b[31m!!") or
                        proc.stderr.lstrip().startswith("!!"), proc.stderr)
        self.assertLess(proc.stdout.index("Directories"), proc.stdout.index("Tests"))

    def test_unset_is_silent(self):
        proc = self.run_install(None)
        self.assert_silent(proc)

    def test_empty_is_silent(self):
        proc = self.run_install("")
        self.assert_silent(proc)

    def test_pointing_at_home_claude_is_silent(self):
        proc = self.run_install(os.path.join(self.home, ".claude"))
        self.assert_silent(proc)

    def test_a_trailing_slash_on_home_claude_is_silent(self):
        proc = self.run_install(os.path.join(self.home, ".claude") + "/")
        self.assert_silent(proc)

    def test_a_trailing_slash_is_silent_before_home_claude_exists(self):
        # A fresh machine: nothing to `cd` into, so the raw strings are
        # compared and only the trailing-slash strip keeps this quiet.
        os.rmdir(os.path.join(self.home, ".claude"))
        proc = self.run_install(os.path.join(self.home, ".claude") + "/")
        self.assert_silent(proc)

    def test_a_symlink_to_home_claude_is_silent(self):
        link = os.path.join(self._tmp.name, "claude-link")
        os.symlink(os.path.join(self.home, ".claude"), link)
        proc = self.run_install(link)
        self.assert_silent(proc)

    def test_a_tilde_path_to_home_claude_is_silent(self):
        proc = self.run_install("~/.claude")
        self.assert_silent(proc)


if __name__ == "__main__":
    unittest.main()
