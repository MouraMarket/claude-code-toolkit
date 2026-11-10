#!/usr/bin/env python3
"""Behavioural tests for install.sh's "CLAUDE.md excludes ->
~/.claude/settings.json" section, which adds `**/.claude/worktrees/*/CLAUDE.md`
to the harness's `claudeMdExcludes` so a worktree session stops loading the
repo's CLAUDE.md twice.

Drives the REAL install.sh through `subprocess` against a fixture repo with a
sandboxed HOME, reusing test_install_auto_memory.py's fixture helpers (the
section runs right after Auto memory, so the same fixture reaches it).
"""

import glob
import json
import os
import shutil
import tempfile
import unittest

import test_install_auto_memory as fx
import install_fixture as ifx

PATTERN = "**/.claude/worktrees/*/CLAUDE.md"
ADD_LINE = "\033[33m->\033[0m claudeMdExcludes += %s" % PATTERN
OK_LINE = "\033[32mOK\033[0m claudeMdExcludes has %s" % PATTERN


def excludes_section(stdout):
    """This section's own lines only, so a neighbouring section's output can
    never satisfy or break an assertion here."""
    marker = "CLAUDE.md excludes -> ~/.claude/settings.json"
    start = stdout.index(marker)
    end = stdout.index("\n\n", start)
    return stdout[start:end]


class ExcludesTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.repo = fx.make_fixture_repo(self.root)
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        fx.write_config(self.config_path, {})
        fx.seed_home(self.home)
        self.settings = fx.settings_path_for(self.home)

    def install(self, *args):
        if "--check" in args:
            ifx.seed_home_installed(self.home, self.repo)
            ifx.install_links(self.repo, self.home)
        result = fx.run_install(self.repo, self.home, self.config_path, args=args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result, excludes_section(result.stdout)

    def read(self):
        with open(self.settings) as handle:
            return json.load(handle)

    def raw(self):
        with open(self.settings) as handle:
            return handle.read()


class TestAbsentKeyIsSet(ExcludesTestCase):

    def test_pattern_is_set_and_other_keys_survive(self):
        fx.write_settings(self.home, {"otherKey": "keep-me"})
        pre_run = self.raw()
        _, section = self.install()
        self.assertIn(ADD_LINE, section)
        written = self.read()
        self.assertEqual([PATTERN], written["claudeMdExcludes"])
        self.assertEqual("keep-me", written["otherKey"])
        # The run's timed snapshot holds the pre-run file, not a per-step .bak.
        (snapshot,) = glob.glob(self.settings + ".claude-code-toolkit-*.bak")
        with open(snapshot) as handle:
            self.assertEqual(pre_run, handle.read())
        self.assertFalse(os.path.exists(self.settings + ".bak"))


class TestReRunIsIdempotent(ExcludesTestCase):

    def test_second_run_reports_ok_and_changes_nothing(self):
        self.install()
        before = self.raw()
        _, section = self.install()
        self.assertIn(OK_LINE, section)
        self.assertNotIn(ADD_LINE, section)
        self.assertEqual(before, self.raw())


class TestUserEntriesAreKept(ExcludesTestCase):

    def test_pattern_is_appended_after_the_users_own_entries(self):
        fx.write_settings(self.home, {"claudeMdExcludes": ["mine/**", "other/CLAUDE.md"]})
        _, section = self.install()
        self.assertIn(ADD_LINE, section)
        self.assertEqual(["mine/**", "other/CLAUDE.md", PATTERN],
                         self.read()["claudeMdExcludes"])

    def test_list_already_holding_the_pattern_is_not_reordered(self):
        fx.write_settings(self.home, {"claudeMdExcludes": [PATTERN, "mine/**"]})
        _, section = self.install()
        self.assertIn(OK_LINE, section)
        self.assertEqual([PATTERN, "mine/**"], self.read()["claudeMdExcludes"])


class TestForeignValueIsLeftAlone(ExcludesTestCase):

    def assert_untouched(self, value):
        fx.write_settings(self.home, {"claudeMdExcludes": value})
        _, section = self.install()
        self.assertIn("not a list of patterns — leaving it alone", section)
        self.assertNotIn(ADD_LINE, section)
        self.assertEqual(value, self.read()["claudeMdExcludes"])

    def test_a_string_is_left_alone(self):
        self.assert_untouched(PATTERN)

    def test_a_list_with_a_non_string_is_left_alone(self):
        self.assert_untouched(["mine/**", 3])

    def test_null_is_left_alone(self):
        self.assert_untouched(None)


class TestCheckWritesNothing(ExcludesTestCase):

    def test_check_reports_and_leaves_the_file_byte_identical(self):
        fx.write_settings(self.home, {"otherKey": "keep-me"})
        before = self.raw()
        _, section = self.install("--check")
        self.assertIn(ADD_LINE, section)
        self.assertEqual(before, self.raw())
        self.assertFalse(os.path.exists(self.settings + ".bak"))


if __name__ == "__main__":
    unittest.main()
